"""The fourteen tests the spec names, plus the error table around them.

Nothing here touches the network. The node is a fake that refuses every
RPC action except block_info and records what it was asked, so a test can
assert not only what happened but what was never attempted.
"""

import ast
import inspect
import json
import os
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ledger"))

import app as app_module  # noqa: E402
import node as node_module  # noqa: E402
from app import Application, ROUTES, make_server, tokens_match  # noqa: E402
from core import Ledger  # noqa: E402
from errors import LedgerError  # noqa: E402
from money import format_xno, parse_xno  # noqa: E402
from store import Store  # noqa: E402

TOKEN = "test-token-not-a-real-one"
SELLER = "nano_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz"
OTHER = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
XNO = 10 ** 30


def block(prefix="9F2C"):
    return (prefix + "0123456789ABCDEF" * 4)[:64]


class RefusesToSend(Exception):
    """Raised by the fake node if anything send-shaped is ever attempted."""


class FakeNode:
    """A node that can answer block_info and nothing else, ever.

    Any action that could move money raises. If the ledger ever grows a code
    path that tries to send, every test that exercises it turns red rather
    than quietly succeeding.
    """

    SEND_SHAPED = ("send", "process", "receive", "block_create", "sign",
                   "wallet_send", "account_move", "republish")

    def __init__(self, blocks=None):
        self.blocks = dict(blocks or {})
        self.calls = []

    def add(self, block_hash, destination, amount_raw, confirmed=True, subtype="send"):
        self.blocks[block_hash] = {
            "amount": str(amount_raw),
            "confirmed": "true" if confirmed else "false",
            "subtype": subtype,
            "contents": {"link_as_account": destination},
        }
        return block_hash

    def rpc(self, payload):
        action = payload.get("action")
        self.calls.append(payload)
        if action in self.SEND_SHAPED:
            raise RefusesToSend("the ledger attempted %r" % action)
        if action != "block_info":
            raise RefusesToSend("unexpected action %r" % action)
        answer = self.blocks.get(payload.get("hash"))
        return dict(answer) if answer else {"error": "Block not found"}

    def block_info(self, block_hash):
        return node_module.normalise(
            self.rpc({"action": "block_info", "json_block": "true", "hash": block_hash}))


class Clock:
    def __init__(self):
        self.tick = 0

    def __call__(self):
        self.tick += 1
        return "2026-09-26T%02d:%02d:00Z" % (divmod(self.tick, 60)[0] % 24, self.tick % 60)


class Base(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()
        self.application = Application(
            store=Store(), node=self.node, token=TOKEN, clock=Clock(),
            base_url="https://ledger.example.invalid", node_rpc_url="http://node.invalid")

    def call(self, method, path, body=None, token=None, headers=None, query=None):
        from urllib.parse import parse_qs
        all_headers = dict(headers or {})
        if token:
            all_headers["X-Ledger-Token"] = token
        return self.application.handle(
            method, path, parse_qs(query or ""), all_headers, body)

    def create(self, **over):
        payload = {
            "kind": "work_settlement", "counterparty": "arion",
            "counterparty_address": SELLER, "amount_xno": "0.050000",
            "reason": "delivered the pricing extraction job",
        }
        payload.update(over)
        status, receipt = self.call("POST", "/v1/receipts", payload, token=TOKEN)
        self.assertEqual(status, 201, receipt)
        return receipt

    def settle(self, receipt, block_hash=None, **over):
        block_hash = block_hash or block()
        self.node.add(block_hash,
                      over.get("destination", receipt["counterparty_address"]),
                      over.get("amount_raw", receipt["amount_raw"]),
                      confirmed=over.get("confirmed", True),
                      subtype=over.get("subtype", "send"))
        return self.call("POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                         {"block_hash": block_hash}, token=TOKEN)


# --------------------------------------------------------------------------
# the fourteen numbered tests
# --------------------------------------------------------------------------

class SpecTests(Base):

    def test_01_no_mutation_endpoints(self):
        receipt = self.create()
        path = "/v1/receipts/%s" % receipt["id"]
        for method in ("PUT", "PATCH", "DELETE"):
            with self.assertRaises(LedgerError) as caught:
                self.call(method, path, {}, token=TOKEN)
            self.assertEqual(caught.exception.status, 405)
            self.assertEqual(caught.exception.code, "append_only")
        # and no such verb is bound to a receipt path in the route table
        bound = {(method, pattern.pattern) for method, pattern, _, _ in ROUTES}
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertFalse([m for m, _ in bound if m == method],
                             "%s is bound to a route" % method)

    def test_02_block_must_match_receipt(self):
        receipt = self.create()
        one_raw_off = str(int(receipt["amount_raw"]) + 1)
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, amount_raw=one_raw_off)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "block_mismatch")
        self.assertIn("amount", caught.exception.detail)
        self.assertIsNone(self.application.ledger.get(receipt["id"])["block_hash"])

        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, block_hash=block("AAAA"), destination=OTHER)
        self.assertEqual(caught.exception.code, "block_mismatch")
        self.assertIn("destination", caught.exception.detail)
        self.assertIsNone(self.application.ledger.get(receipt["id"])["block_hash"])

    def test_02b_a_confirmed_receive_pays_nobody(self):
        receipt = self.create()
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, subtype="receive")
        self.assertEqual(caught.exception.code, "block_mismatch")
        self.assertIn("subtype", caught.exception.detail)

    def test_03_unconfirmed_block_not_attached(self):
        receipt = self.create()
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, confirmed=False)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "block_unconfirmed")
        self.assertIsNone(self.application.ledger.get(receipt["id"])["block_hash"])
        self.assertFalse(self.application.ledger.get(receipt["id"])["confirmed"])

    def test_04_block_cannot_be_reused(self):
        first, second = self.create(), self.create(counterparty="bart")
        shared = block("1A2B")
        self.settle(first, block_hash=shared)
        with self.assertRaises(LedgerError) as caught:
            self.settle(second, block_hash=shared)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "block_reused")
        self.assertEqual(caught.exception.detail["receipt_id"], first["id"])
        self.assertIsNone(self.application.ledger.get(second["id"])["block_hash"])
        # one payment is shown once
        _, index = self.call("GET", "/v1/receipts")
        self.assertEqual(index["totals"]["confirmed"], 1)

    def test_05_totals_are_confirmed_only(self):
        confirmed = self.create(amount_xno="0.05")
        self.settle(confirmed)
        self.create(amount_xno="1.00", counterparty="pending-one")
        _, index = self.call("GET", "/v1/receipts")
        totals = index["totals"]
        self.assertEqual(totals["paid_xno_total"], "0.050000")
        self.assertEqual(totals["pending"], 1)
        self.assertEqual(totals["confirmed"], 1)
        self.assertNotIn("1.000000", json.dumps(totals))

    def test_06_totals_are_exact_decimal(self):
        for index, amount in enumerate(("0.1", "0.2", "0.000001")):
            receipt = self.create(amount_xno=amount, counterparty="seller-%d" % index)
            self.settle(receipt, block_hash=block("%04X" % (0x1000 + index)))
        _, listing = self.call("GET", "/v1/receipts")
        self.assertEqual(listing["totals"]["paid_xno_total"], "0.300001")
        self.assertEqual(listing["totals"]["counterparties"], 1)  # same address

    def test_07_bad_address_creates_nothing(self):
        _, before = self.call("GET", "/v1/receipts")
        altered = SELLER[:-1] + ("a" if SELLER[-1] != "a" else "b")
        with self.assertRaises(LedgerError) as caught:
            self.call("POST", "/v1/receipts", {
                "kind": "tip", "counterparty": "eddie_researcher",
                "counterparty_address": altered, "amount_xno": "0.010000",
                "reason": "an address that fails checksum must never be stored",
            }, token=TOKEN)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "invalid_address")
        self.assertEqual(caught.exception.detail["reason"], "bad_checksum")
        _, after = self.call("GET", "/v1/receipts")
        self.assertEqual(before["totals"]["receipts"], after["totals"]["receipts"])

    def test_08_supersede_appends_and_is_public(self):
        wrong = self.create(reason="paid the wrong amount")
        self.settle(wrong)
        block_before = self.application.ledger.get(wrong["id"])["block_hash"]
        replacement = self.create(counterparty="arion", reason="the corrected settlement")
        self.settle(replacement, block_hash=block("BEEF"))

        status, corrected = self.call(
            "POST", "/v1/receipts/%s/supersede" % wrong["id"],
            {"replacement_id": replacement["id"], "reason": "amount was 0.05 too low"},
            token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(corrected["superseded_by"], replacement["id"])
        self.assertEqual(corrected["block_hash"], block_before)
        self.assertTrue(corrected["confirmed"])

        _, corrections = self.call("GET", "/v1/corrections")
        entry = corrections["corrections"][0]
        self.assertEqual(entry["receipt_id"], wrong["id"])
        self.assertEqual(entry["replacement_id"], replacement["id"])
        self.assertEqual(entry["reason"], "amount was 0.05 too low")
        self.assertTrue(entry["at"])

    def test_09_public_reads_need_no_auth(self):
        receipt = self.create()
        self.settle(receipt)
        for path in ("/v1/receipts", "/v1/receipts/%s" % receipt["id"],
                     "/v1/corrections", "/v1/custody"):
            status, _ = self.application.handle("GET", path, {}, {}, None)
            self.assertEqual(status, 200, path)

    def test_10_writes_need_auth_and_the_compare_is_constant_time(self):
        receipt = self.create()
        writes = [
            ("POST", "/v1/receipts", {}),
            ("POST", "/v1/receipts/%s/attach-block" % receipt["id"], {}),
            ("POST", "/v1/receipts/%s/supersede" % receipt["id"], {}),
        ]
        for method, path, body in writes:
            for token in (None, "wrong-token", ""):
                with self.assertRaises(LedgerError) as caught:
                    self.call(method, path, body, token=token)
                self.assertEqual(caught.exception.status, 401, path)
                self.assertEqual(caught.exception.code, "unauthorized")
        # The comparison must be constant-time, and asserting that the module
        # merely imports hmac proves nothing: `a == b` would still pass. Read
        # the helper's own body instead.
        source = ast.parse(inspect.getsource(tokens_match)).body[0]
        calls = [
            n for n in ast.walk(source)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "compare_digest"
        ]
        self.assertTrue(calls, "tokens_match does not call hmac.compare_digest")
        comparisons = [
            n for n in ast.walk(source)
            if isinstance(n, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops)
        ]
        self.assertFalse(
            comparisons,
            "tokens_match compares with == or !=, which short-circuits on the "
            "first differing byte and leaks the token one character at a time")
        self.assertTrue(tokens_match(TOKEN, TOKEN))
        self.assertFalse(tokens_match(TOKEN, TOKEN + "x"))
        self.assertFalse(tokens_match(None, TOKEN))

    def test_10b_an_unset_token_does_not_mean_open_writes(self):
        open_app = Application(store=Store(), node=self.node, token="", clock=Clock())
        with self.assertRaises(LedgerError) as caught:
            open_app.handle("POST", "/v1/receipts", {}, {"X-Ledger-Token": ""}, {})
        self.assertEqual(caught.exception.status, 401)

    def test_11_ledger_cannot_move_money(self):
        # static: the core module imports nothing that could sign or send
        with open(os.path.join(ROOT, "ledger", "core.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                imported.add(statement.module.split(".")[0])
        self.assertEqual(imported, {"re", "secrets", "nanoaddr", "errors", "money"})
        for forbidden in ("urllib", "http", "socket", "requests", "node",
                          "ed25519_blake2b", "wallet", "subprocess", "os"):
            self.assertNotIn(forbidden, imported)

        # behavioural: run the whole flow and assert the node was only ever read
        receipt = self.create()
        self.settle(receipt)
        self.call("GET", "/v1/receipts")
        self.assertTrue(self.node.calls)
        self.assertEqual({c["action"] for c in self.node.calls}, {"block_info"})

    def test_12_plaintext_receipt_under_twenty_lines(self):
        receipt = self.create()
        _, settled = self.settle(receipt)
        text = self.application.plaintext(self.application.ledger.get(receipt["id"]))
        lines = text.rstrip("\n").split("\n")
        self.assertLessEqual(len(lines), 20)
        self.assertIn(settled["block_hash"], text)
        self.assertIn(settled["amount_xno"], text)
        self.assertIn(SELLER, text)
        self.assertIn("verify", text)

    def test_13_custody_document_says_what_we_do_hold(self):
        _, document = self.call("GET", "/v1/custody")
        self.assertTrue(document["what_we_do_hold"].strip())
        self.assertIn("Our own funding account's key", document["what_we_do_hold"])
        self.assertGreaterEqual(len(document["what_we_cannot_do"]), 4)

    def test_14_the_verify_instructions_we_publish_actually_work(self):
        receipt = self.create()
        _, settled = self.settle(receipt)
        _, document = self.call("GET", "/v1/receipts/%s" % receipt["id"])
        example = document["verify"]["node_rpc_example"]

        # pull the exact JSON payload out of the published curl line
        start, end = example.index("{"), example.rindex("}") + 1
        payload = json.loads(example[start:end])
        self.assertEqual(payload["action"], "block_info")
        self.assertEqual(payload["hash"], settled["block_hash"])

        answer = node_module.normalise(self.node.rpc(payload))
        expect = document["verify"]["expect"]
        self.assertEqual(answer["destination"], expect["destination"])
        self.assertEqual(format_xno(int(answer["amount_raw"])), expect["amount_xno"])
        self.assertEqual(answer["subtype"], expect["subtype"])
        self.assertEqual(str(answer["confirmed"]).lower(), expect["confirmed"])


# --------------------------------------------------------------------------
# the error table
# --------------------------------------------------------------------------

class ErrorTable(Base):

    def test_unknown_receipt_is_404(self):
        with self.assertRaises(LedgerError) as caught:
            self.call("GET", "/v1/receipts/rcpt_0000000000000000")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))

    def test_amount_out_of_range(self):
        for amount in ("0", "-1", "0.0000001", "abc", 0.05, None, ""):
            with self.assertRaises(LedgerError) as caught:
                self.create(amount_xno=amount)
            self.assertEqual(caught.exception.code, "amount_out_of_range", amount)

    def test_reason_required(self):
        for reason in (None, "", "   "):
            with self.assertRaises(LedgerError) as caught:
                self.create(reason=reason)
            self.assertEqual(caught.exception.code, "reason_required")

    def test_kind_must_be_known(self):
        with self.assertRaises(LedgerError) as caught:
            self.create(kind="donation")
        self.assertEqual(caught.exception.code, "bad_kind")

    def test_counterparty_length(self):
        with self.assertRaises(LedgerError) as caught:
            self.create(counterparty="x" * 81)
        self.assertEqual(caught.exception.code, "counterparty_out_of_range")

    def test_block_already_attached(self):
        receipt = self.create()
        self.settle(receipt)
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, block_hash=block("CAFE"))
        self.assertEqual(caught.exception.code, "block_already_attached")

    def test_block_not_found(self):
        receipt = self.create()
        with self.assertRaises(LedgerError) as caught:
            self.call("POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                      {"block_hash": block("FFFF")}, token=TOKEN)
        self.assertEqual(caught.exception.code, "block_not_found")

    def test_bad_block_hash_shape(self):
        receipt = self.create()
        for bad in (block().lower(), block()[:63], "", None, 12):
            with self.assertRaises(LedgerError) as caught:
                self.call("POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                          {"block_hash": bad}, token=TOKEN)
            self.assertIn(caught.exception.code, ("bad_block_hash", "block_not_found"))

    def test_nothing_to_correct(self):
        pending, replacement = self.create(), self.create()
        with self.assertRaises(LedgerError) as caught:
            self.call("POST", "/v1/receipts/%s/supersede" % pending["id"],
                      {"replacement_id": replacement["id"], "reason": "x"}, token=TOKEN)
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "nothing_to_correct"))

    def test_supersede_with_unknown_replacement(self):
        receipt = self.create()
        self.settle(receipt)
        with self.assertRaises(LedgerError) as caught:
            self.call("POST", "/v1/receipts/%s/supersede" % receipt["id"],
                      {"replacement_id": "rcpt_ffffffffffffffff", "reason": "x"}, token=TOKEN)
        self.assertEqual(caught.exception.status, 404)

    def test_supersede_twice_is_refused(self):
        receipt, first, second = self.create(), self.create(), self.create()
        self.settle(receipt)
        for replacement in (first, second):
            self.settle(replacement, block_hash=block("%04X" % (0xA000 + id(replacement) % 4095)))
        self.call("POST", "/v1/receipts/%s/supersede" % receipt["id"],
                  {"replacement_id": first["id"], "reason": "first correction"}, token=TOKEN)
        with self.assertRaises(LedgerError) as caught:
            self.call("POST", "/v1/receipts/%s/supersede" % receipt["id"],
                      {"replacement_id": second["id"], "reason": "second"}, token=TOKEN)
        self.assertEqual(caught.exception.code, "already_superseded")

    def test_limit_and_cursor(self):
        made = [self.create(counterparty="c%d" % i) for i in range(5)]
        with self.assertRaises(LedgerError):
            self.call("GET", "/v1/receipts", query="limit=0")
        with self.assertRaises(LedgerError):
            self.call("GET", "/v1/receipts", query="limit=101")
        with self.assertRaises(LedgerError) as caught:
            self.call("GET", "/v1/receipts", query="cursor=rcpt_0000000000000000")
        self.assertEqual(caught.exception.code, "bad_cursor")

        _, page = self.call("GET", "/v1/receipts", query="limit=2")
        self.assertEqual(len(page["receipts"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        seen = [r["id"] for r in page["receipts"]]
        while page["next_cursor"]:
            _, page = self.call("GET", "/v1/receipts",
                                query="limit=2&cursor=%s" % page["next_cursor"])
            seen.extend(r["id"] for r in page["receipts"])
        self.assertEqual(sorted(seen), sorted(r["id"] for r in made))

    def test_filters(self):
        self.create(kind="tip", counterparty="a")
        self.create(kind="grant", counterparty="b")
        _, tips = self.call("GET", "/v1/receipts", query="kind=tip")
        self.assertEqual([r["counterparty"] for r in tips["receipts"]], ["a"])
        _, by_name = self.call("GET", "/v1/receipts", query="counterparty=b")
        self.assertEqual([r["kind"] for r in by_name["receipts"]], ["grant"])
        with self.assertRaises(LedgerError):
            self.call("GET", "/v1/receipts", query="kind=nonsense")

    def test_unknown_route(self):
        with self.assertRaises(LedgerError) as caught:
            self.call("GET", "/v1/nothing")
        self.assertEqual(caught.exception.status, 404)

    def test_attach_without_a_node_configured(self):
        nodeless = Application(store=Store(), node=None, token=TOKEN, clock=Clock())
        status, receipt = nodeless.handle("POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "0.010000", "reason": "why"})
        with self.assertRaises(LedgerError) as caught:
            nodeless.handle("POST", "/v1/receipts/%s/attach-block" % receipt["id"], {},
                            {"X-Ledger-Token": TOKEN}, {"block_hash": block()})
        self.assertEqual(caught.exception.code, "no_node")


# --------------------------------------------------------------------------
# persistence, and the real socket
# --------------------------------------------------------------------------

class Persistence(unittest.TestCase):

    def test_the_ledger_survives_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        node = FakeNode()
        first = Application(store=Store(path), node=node, token=TOKEN, clock=Clock())
        _, receipt = first.handle("POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
            "kind": "grant", "counterparty": "arion", "counterparty_address": SELLER,
            "amount_xno": "0.050000", "reason": "a grant"})
        node.add(block(), SELLER, receipt["amount_raw"])
        first.handle("POST", "/v1/receipts/%s/attach-block" % receipt["id"], {},
                     {"X-Ledger-Token": TOKEN}, {"block_hash": block()})

        second = Application(store=Store(path), node=node, token=TOKEN, clock=Clock())
        _, index = second.handle("GET", "/v1/receipts", {}, {}, None)
        self.assertEqual(index["totals"]["paid_xno_total"], "0.050000")
        self.assertEqual(index["totals"]["confirmed"], 1)

    def test_no_token_is_written_to_the_store(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        application = Application(store=Store(path), node=FakeNode(), token=TOKEN, clock=Clock())
        application.handle("POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "0.010000", "reason": "why"})
        with open(path, encoding="utf-8") as handle:
            self.assertNotIn(TOKEN, handle.read())


class OverARealSocket(unittest.TestCase):
    """One pass over an actual HTTP socket, because routing is where the
    difference between a handler and a server shows up."""

    def setUp(self):
        import threading
        self.node = FakeNode()
        self.application = Application(store=Store(), node=self.node, token=TOKEN, clock=Clock())
        self.server = make_server(self.application, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def fetch(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), error.headers

    def test_a_whole_settlement_over_http(self):
        status, body, _ = self.fetch("POST", "/v1/receipts", {
            "kind": "work_settlement", "counterparty": "arion",
            "counterparty_address": SELLER, "amount_xno": "0.050000",
            "reason": "delivered the extraction job",
        }, {"X-Ledger-Token": TOKEN})
        self.assertEqual(status, 201, body)
        receipt = json.loads(body)

        self.node.add(block(), SELLER, receipt["amount_raw"])
        status, body, _ = self.fetch(
            "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
            {"block_hash": block()}, {"X-Ledger-Token": TOKEN})
        self.assertEqual(status, 200, body)

        status, body, _ = self.fetch("GET", "/v1/receipts")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["totals"]["paid_xno_total"], "0.050000")

        status, body, headers = self.fetch(
            "GET", "/v1/receipts/%s" % receipt["id"], headers={"Accept": "text/plain"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertLessEqual(len(body.rstrip("\n").split("\n")), 20)

        for method in ("PUT", "PATCH", "DELETE"):
            status, body, _ = self.fetch(
                method, "/v1/receipts/%s" % receipt["id"], {}, {"X-Ledger-Token": TOKEN})
            self.assertEqual(status, 405, method)
            self.assertEqual(json.loads(body)["error"], "append_only")

        status, body, _ = self.fetch("POST", "/v1/receipts", {}, None)
        self.assertEqual(status, 401)

        status, body, _ = self.fetch("GET", "/v1/custody")
        self.assertEqual(status, 200)
        self.assertIn("Our own funding account's key", body)


# --------------------------------------------------------------------------
# One account, two spellings; one amount, two spellings.
#
# A Nano account has a modern `nano_` form and a legacy `xrb_` form, and
# `nanoaddr.validate` accepts both because counterparties still hand out
# either. A node always answers the `nano_` form. An amount of raw is an
# integer, and "0500" and "500" are one amount. Comparing either as text
# refuses a block that paid exactly the right account exactly the right
# amount - and this ledger has no way back from that, because `supersede`
# refuses a receipt that carries no block.
# --------------------------------------------------------------------------

SELLER_XRB = "xrb_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz"
OTHER_XRB = "xrb_1111111111111111111111111111111111111111111111111111hifc8npp"


class Spellings(Base):
    def test_the_two_spellings_carry_the_same_account(self):
        """The premise of every test below, stated once."""
        self.assertEqual(SELLER_XRB.split("_", 1)[1], SELLER.split("_", 1)[1])
        self.assertNotEqual(SELLER_XRB, SELLER)

    def test_a_receipt_records_the_canonical_spelling(self):
        receipt = self.create(counterparty_address=SELLER_XRB)
        self.assertEqual(receipt["counterparty_address"], SELLER)

    def test_a_counterparty_who_gave_the_legacy_spelling_is_still_settled(self):
        """The bug: the block paid this account, and the ledger said it did not."""
        receipt = self.create(counterparty_address=SELLER_XRB)
        status, body = self.settle(receipt, destination=SELLER)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["confirmed"])

    def test_a_row_stored_before_canonicalisation_still_settles(self):
        """A receipt already on disk carries whatever spelling it was given, so
        the comparison has to identify accounts too - canonicalising new writes
        alone would leave those rows permanently unsettleable."""
        receipt = self.create()
        for row in self.application.ledger.store.receipts():
            if row["id"] == receipt["id"]:
                row["counterparty_address"] = SELLER_XRB
        receipt["counterparty_address"] = SELLER_XRB
        status, body = self.settle(receipt, destination=SELLER)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["confirmed"])

    def test_one_account_spelled_two_ways_is_one_counterparty(self):
        """`totals` counted distinct strings. Until the comparison above was
        fixed an `xrb_` row could never be confirmed, so that was right by
        accident; fixing it alone would have made one seller count as two."""
        first = self.create()
        for row in self.application.ledger.store.receipts():
            row["counterparty_address"] = SELLER_XRB
        first["counterparty_address"] = SELLER_XRB
        self.assertEqual(
            self.settle(first, block_hash=block("A1B2"), destination=SELLER)[0], 200)
        second = self.create()
        self.assertEqual(
            self.settle(second, block_hash=block("C3D4"), destination=SELLER)[0], 200)

        status, listing = self.call("GET", "/v1/receipts")
        self.assertEqual(status, 200)
        self.assertEqual(listing["totals"]["confirmed"], 2)
        self.assertEqual(listing["totals"]["counterparties"], 1)

    def test_a_padded_amount_from_the_node_is_the_same_amount(self):
        receipt = self.create()
        status, body = self.settle(receipt, amount_raw="0" + receipt["amount_raw"])
        self.assertEqual(status, 200, body)
        self.assertTrue(body["confirmed"])

    # -- and it did not loosen ------------------------------------------

    def test_a_stranger_is_still_refused_in_either_spelling(self):
        for prefix, paid_to in (("A1B2", OTHER), ("C3D4", OTHER_XRB)):
            receipt = self.create()
            with self.assertRaises(LedgerError) as caught:
                self.settle(receipt, block_hash=block(prefix), destination=paid_to)
            self.assertEqual(caught.exception.code, "block_mismatch")
            self.assertIn("destination", caught.exception.detail)
            self.assertIsNone(
                self.application.ledger.get(receipt["id"])["block_hash"])

    def test_a_short_payment_is_still_refused_against_a_padded_price(self):
        """The padded-amount fix must not turn into "any amount will do"."""
        receipt = self.create()
        short = str(int(receipt["amount_raw"]) - 1)
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, amount_raw="0" + short)
        self.assertEqual(caught.exception.code, "block_mismatch")
        self.assertIn("amount", caught.exception.detail)

    def test_an_amount_that_is_not_an_integer_is_still_refused(self):
        """`raw_amount` never raises, so each of these has to refuse rather
        than escape as a ValueError out of a money comparison."""
        # "\u00b2" is the one that matters: str.isdigit() is True for it and
        # int() raises, so the guard cannot be isdigit alone.
        amounts = ("fifty", "", "-" + str(10 ** 28), "5e28", "  ", "50.0",
                   "\u00b2", "\u0665" * 29)
        prefixes = ("A1B2", "C3D4", "E5F6", "0708", "090A", "0B0C", "0D0E", "0F10")
        for prefix, amount in zip(prefixes, amounts):
            receipt = self.create()
            with self.assertRaises(LedgerError) as caught:
                self.settle(receipt, block_hash=block(prefix), amount_raw=amount)
            self.assertEqual(caught.exception.code, "block_mismatch", amount)
            self.assertIn("amount", caught.exception.detail)

    def test_a_block_with_no_destination_is_still_refused(self):
        receipt = self.create()
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, destination=None)
        self.assertEqual(caught.exception.code, "block_mismatch")
        self.assertIn("destination", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
