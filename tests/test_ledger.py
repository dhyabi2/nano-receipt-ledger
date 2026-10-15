"""The fourteen tests the spec names, plus the error table around them.

Nothing here touches the network. The node is a fake that refuses every
RPC action except block_info and records what it was asked, so a test can
assert not only what happened but what was never attempted.
"""

import ast
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ledger"))

import app as app_module  # noqa: E402
import node as node_module  # noqa: E402
from app import (Application, MAX_TIMEOUT_SECONDS, ROUTES,  # noqa: E402
                 make_server, tokens_match)
from core import Ledger  # noqa: E402
from errors import LedgerError  # noqa: E402
from money import AmountError, format_xno, parse_xno  # noqa: E402
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


class ANodeThatDoesNotAnswer(unittest.TestCase):
    """The node is configured and down - the ordinary case, not an exotic one.

    `_attach` already answers 503 `no_node` when no node is configured at all.
    A node that IS configured and cannot be reached is the same situation for
    the caller, and it used to get something strictly worse than an error: the
    transport exception went past `Handler._dispatch`, which catches only
    LedgerError, and the connection was closed with NO HTTP RESPONSE. An agent
    proving its XNO payment settled could not tell "the node was down, retry"
    from "the ledger refused my block", and the server printed a traceback.
    """

    def test_a_node_that_refuses_the_connection_is_a_503_not_a_dropped_connection(self):
        import threading
        application = Application(
            store=Store(), node=node_module.NanoNode("http://127.0.0.1:1", timeout=2),
            token=TOKEN, clock=Clock())
        server = make_server(application, "127.0.0.1", 0)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def fetch(method, path, body=None, headers=None):
                data = json.dumps(body).encode("utf-8") if body is not None else None
                request = urllib.request.Request(
                    base + path, data=data, method=method,
                    headers={"Content-Type": "application/json", **(headers or {})})
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        return response.status, response.read().decode("utf-8")
                except urllib.error.HTTPError as error:
                    return error.code, error.read().decode("utf-8")

            status, body = fetch("POST", "/v1/receipts", {
                "kind": "work_settlement", "counterparty": "arion",
                "counterparty_address": SELLER, "amount_xno": "0.050000",
                "reason": "delivered the extraction job",
            }, {"X-Ledger-Token": TOKEN})
            self.assertEqual(status, 201, body)
            receipt = json.loads(body)

            # Before the fix this raised http.client.RemoteDisconnected
            # ("Remote end closed connection without response") out of fetch:
            # no status, no body, nothing for a caller to act on.
            status, body = fetch(
                "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                {"block_hash": block()}, {"X-Ledger-Token": TOKEN})
            self.assertEqual(status, 503, body)
            answer = json.loads(body)
            self.assertEqual(answer["error"], "node_unavailable")
            # It must say the block was not judged, not that it was bad.
            self.assertIn("retried", answer["message"])
        finally:
            server.shutdown()
            server.server_close()

        # And nothing was written: the receipt is still pending, so the retry
        # the message promises is in fact safe.
        _, pending = application.handle(
            "GET", "/v1/receipts/%s" % receipt["id"], {}, {}, None)
        self.assertIsNone(pending["block_hash"])
        self.assertFalse(pending["confirmed"])

    def test_a_node_that_answers_with_something_that_is_not_json(self):
        """An HTML error page from a proxy in front of the node is the same
        answer as no answer: the node did not tell us about the block. This
        drives the real NanoNode against a real socket, not a stand-in."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class BadGateway(BaseHTTPRequestHandler):
            def do_POST(self):
                page = b"<html><body>502 Bad Gateway</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass

        proxy = HTTPServer(("127.0.0.1", 0), BadGateway)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            node = node_module.NanoNode(
                "http://127.0.0.1:%d" % proxy.server_address[1], timeout=5)
            with self.assertRaises(node_module.NodeError):
                node.block_info(block())

            application = Application(store=Store(), node=node, token=TOKEN, clock=Clock())
            _, receipt = application.handle(
                "POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
                    "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
                    "amount_xno": "0.010000", "reason": "why"})
            with self.assertRaises(LedgerError) as caught:
                application.handle(
                    "POST", "/v1/receipts/%s/attach-block" % receipt["id"], {},
                    {"X-Ledger-Token": TOKEN}, {"block_hash": block()})
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(caught.exception.code, "node_unavailable")
        finally:
            proxy.shutdown()
            proxy.server_close()

    def test_a_node_that_ignores_json_block_is_a_503_not_a_dropped_connection(self):
        """`contents` as an opaque string crashed `normalise` with AttributeError.

        `block_info` is asked with `json_block: "true"`, and a node that honours
        it answers `contents` as an object. An endpoint that ignores the flag
        answers it as a JSON *string* instead - the same class of behaviour this
        repository's sibling records first-hand of `rpc.nano.to`, which silently
        ignores `raw: "true"` on `account_history`. `normalise` then ran
        `"...".get("link_as_account")`:

            AttributeError: 'str' object has no attribute 'get'

        raised outside `rpc`'s own `except (OSError, ValueError)`, so it went
        past `Handler._dispatch` (LedgerError only) exactly as the transport
        exception above used to, and the agent proving its XNO settlement got
        its connection closed with no HTTP response at all.

        It must be a hold, not a verdict: nothing was read, so nothing is known
        about the block. That is what `NodeError` and 503 `node_unavailable`
        mean here, and `node.py`'s own docstring says so - "it says the node did
        not answer, never that a block is bad".
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class IgnoresJsonBlock(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({
                    "block_account": SELLER,
                    "amount": str(XNO // 100),
                    "confirmed": "true",
                    "subtype": "send",
                    # A real node asked without json_block answers this as a
                    # string. Here the flag was sent and ignored.
                    "contents": json.dumps({"type": "state",
                                            "link_as_account": SELLER}),
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        endpoint = HTTPServer(("127.0.0.1", 0), IgnoresJsonBlock)
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()
        try:
            node = node_module.NanoNode(
                "http://127.0.0.1:%d" % endpoint.server_address[1], timeout=5)
            with self.assertRaises(node_module.NodeError):
                node.block_info(block())

            application = Application(store=Store(), node=node, token=TOKEN, clock=Clock())
            _, receipt = application.handle(
                "POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
                    "kind": "work_settlement", "counterparty": "arion",
                    "counterparty_address": SELLER, "amount_xno": "0.010000",
                    "reason": "delivered the extraction job"})
            with self.assertRaises(LedgerError) as caught:
                application.handle(
                    "POST", "/v1/receipts/%s/attach-block" % receipt["id"], {},
                    {"X-Ledger-Token": TOKEN}, {"block_hash": block()})
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(caught.exception.code, "node_unavailable")

            # And the retry the message promises is safe: nothing was written.
            _, pending = application.handle(
                "GET", "/v1/receipts/%s" % receipt["id"], {}, {}, None)
            self.assertIsNone(pending["block_hash"])
            self.assertFalse(pending["confirmed"])
        finally:
            endpoint.shutdown()
            endpoint.server_close()

    def test_normalise_holds_on_a_reply_whose_contents_cannot_be_read(self):
        """The unit form of the case above, for every shape `contents` can take.

        A reply that names a block and carries no readable `contents` says
        nothing about where the money went. Refusing it as a mismatch would
        accuse a buyer who paid; this raises instead, and the HTTP layer turns
        it into the documented retry.
        """
        for contents in ('{"link_as_account": "%s"}' % SELLER, None, 7, [], "null"):
            with self.subTest(contents=contents):
                with self.assertRaises(node_module.NodeError):
                    node_module.normalise({"confirmed": "true", "subtype": "send",
                                           "amount": "1", "contents": contents})

    def test_normalise_still_reads_every_answer_it_read_before(self):
        """Control: the two shapes that worked are untouched, field for field.

        A node error answer that SAYS not-found is still a not-found, and a
        well-formed answer still yields the same five facts. The one verdict
        that did change is for a reply with `contents` missing, null or not an
        object: it was a terminal 409 (block_unconfirmed or block_mismatch) and
        is now the retryable 503 hold above. (Any OTHER error string is now
        that same hold too - see ANodeErrorThatIsNotANotFound.)
        """
        self.assertEqual(
            node_module.normalise({"error": "Block not found"}),
            {"found": False, "confirmed": False, "subtype": None,
             "destination": None, "amount_raw": None})
        self.assertEqual(
            node_module.normalise({"confirmed": "true", "subtype": "send",
                                   "amount": "10", "contents": {"link_as_account": SELLER}}),
            {"found": True, "confirmed": True, "subtype": "send",
             "destination": SELLER, "amount_raw": "10"})

    def test_normalise_holds_on_a_reply_that_is_not_a_json_object(self):
        """A reply that is not an object was answered 404 block_not_found.

        `[]`, a bare string, a number and `null` all used to fold into the
        not-found answer, so the caller was told "the node does not know
        block X" although NOTHING about the block was read. A public endpoint
        behind a proxy answers 200 with its own JSON - a status page, a
        rate-limit envelope - far more often than it answers with HTML.

        This is the same wrong verdict the `contents` case argues against,
        one step earlier: unknown reported as a fact. The 2026-10-11 note
        left the `error` FIELD as a known limitation on the same reasoning,
        and that limitation is now closed as well: only an error that says
        "not found" is read as one.
        """
        for answer in ([], ["error"], "ok", "error", 1, None, True, 0.5):
            with self.subTest(answer=answer):
                with self.assertRaises(node_module.NodeError):
                    node_module.normalise(answer)

    def test_only_a_not_found_error_is_read_as_a_not_found(self):
        """The error FIELD is split on what it says, not folded wholesale.

        `{"error": "Block not found"}` is evidence the block is absent and
        keeps its 404. `{"error": "Too many requests"}` is not evidence of
        anything about the block, so it is the same 503 hold as a reply that
        carries no error string at all - see ANodeErrorThatIsNotANotFound,
        which owns this behaviour. The 2026-10-11 note treated the whole
        field as a known limitation on the grounds that the two could not be
        told apart; the one string every Nano node uses tells them apart.
        """
        self.assertEqual(
            node_module.normalise({"error": "Block not found"}),
            {"found": False, "confirmed": False, "subtype": None,
             "destination": None, "amount_raw": None})
        with self.assertRaises(node_module.NodeError):
            node_module.normalise({"error": "Too many requests"})

    def test_a_send_shaped_action_still_raises_loudly(self):
        """The fix must not turn the repository's own tripwire into a 503.

        `NanoNode.rpc` refuses any action but block_info, and the suite's
        FakeNode raises if the ledger ever asks for something send-shaped.
        Those are safety properties, not outages: catching them here would
        hide exactly what they exist to reveal.
        """
        node = node_module.NanoNode("http://127.0.0.1:1", timeout=2)
        with self.assertRaises(node_module.NodeError) as caught:
            node.rpc({"action": "send", "wallet": "x"})
        self.assertIn("only block_info", str(caught.exception))
        with self.assertRaises(RefusesToSend):
            FakeNode().rpc({"action": "process", "block": {}})


class NodeStubs:
    """Helpers for running the real HTTP server against a real stub node."""

    def serve_node(self, body):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Stub(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the client stopped reading at its limit

            def log_message(self, *args):
                pass

        stub = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        return node_module.NanoNode("http://127.0.0.1:%d" % stub.server_address[1], timeout=10)

    def attach_over_http(self, node):
        """Create a receipt and attach a block through the real HTTP server,
        and assert nothing was recorded as paid."""
        status, answer, stored = self.attach_and_read_back(node)
        self.assertIsNone(stored["block_hash"])
        self.assertFalse(stored["confirmed"])
        return status, answer

    def attach_and_read_back(self, node):
        """Create a receipt, attach a block over HTTP, return what was stored."""
        import threading
        application = Application(store=Store(), node=node, token=TOKEN, clock=Clock())
        server = make_server(application, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]

        def fetch(method, path, body=None):
            request = urllib.request.Request(
                base + path, data=json.dumps(body).encode("utf-8"), method=method,
                headers={"Content-Type": "application/json", "X-Ledger-Token": TOKEN})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))

        status, receipt = fetch("POST", "/v1/receipts", {
            "kind": "work_settlement", "counterparty": "arion",
            "counterparty_address": SELLER, "amount_xno": "0.010000",
            "reason": "delivered the extraction job"})
        self.assertEqual(status, 201, receipt)
        # Before the fixes this raised http.client.RemoteDisconnected.
        status, answer = fetch(
            "POST", "/v1/receipts/%s/attach-block" % receipt["id"], {"block_hash": block()})
        _, stored = application.handle("GET", "/v1/receipts/%s" % receipt["id"], {}, {}, None)
        return status, answer, stored

    @staticmethod
    def reply(**over):
        answer = {"amount": str(XNO // 100), "confirmed": "true", "subtype": "send",
                  "contents": {"type": "state", "link_as_account": SELLER}}
        answer.update(over)
        return json.dumps(answer).encode("utf-8")


class RepliesThatStillDroppedTheConnection(NodeStubs, unittest.TestCase):
    """Three node replies that got past the guards above, each over real sockets.

    Each one used to raise something neither `rpc` nor `_dispatch` catches -
    a ValueError from `int()` past Python's 4300-digit limit, a RecursionError
    from `json.loads` - or, for the oversized body, was read whole with no
    bound at all. Each test runs the real ledger HTTP server against a real
    stub node and asserts the caller gets a status and a JSON body, and that
    no receipt is recorded as paid.
    """

    def test_an_amount_past_the_integer_digit_limit_is_refused_not_dropped(self):
        """`"amount": "9" * 5000` made `int()` raise ValueError inside
        `attach_block`. It is not an amount any block can carry, so it is
        refused as a mismatch, exactly as any other unreadable amount is."""
        status, answer = self.attach_over_http(
            self.serve_node(self.reply(amount="9" * 5000)))
        self.assertEqual(status, 409, answer)
        self.assertEqual(answer["error"], "block_mismatch")
        self.assertIn("amount", answer["detail"])
        self.assertLess(len(answer["detail"]["amount"]["got"]), 200)

    def test_raw_amount_never_raises_on_a_long_or_oversized_amount(self):
        from core import raw_amount
        for value in ("9" * 5000, "1" + "0" * 39, str(2 ** 128), 10 ** 40):
            with self.subTest(value=str(value)[:20]):
                self.assertIsNone(raw_amount(value))
        # Padding still reads, however long, and the cap itself is an amount.
        self.assertEqual(raw_amount("0" * 5000 + "500"), 500)
        self.assertEqual(raw_amount(str(2 ** 128 - 1)), 2 ** 128 - 1)
        # Zero is an amount: stripping every leading zero must not leave nothing.
        self.assertEqual(raw_amount("0"), 0)
        self.assertEqual(raw_amount("000"), 0)

    def test_a_deeply_nested_reply_is_a_503_not_a_dropped_connection(self):
        """`json.loads` raises RecursionError, not ValueError, on deep nesting."""
        depth = 200000
        node = self.serve_node(b"[" * depth + b"]" * depth)
        with self.assertRaises(node_module.NodeError):
            node.block_info(block())
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")
        # The node answered; the message must not claim it was unreachable.
        self.assertIn("could not be read", answer["message"])

    def padded_reply(self, size):
        """A matching, confirmed reply padded with trailing whitespace to
        exactly `size` bytes. Whitespace keeps every prefix of the padding
        valid JSON, so the only thing that can refuse it is the size limit."""
        body = self.reply()
        self.assertLess(len(body), size)
        return body + b" " * (size - len(body))

    def test_a_reply_over_the_size_limit_is_refused_unread(self):
        """An otherwise matching, confirmed reply one byte past the limit.

        Before, the whole body was read into memory with no bound and the
        block was accepted. A block_info answer is never that large, so it is
        a hold, not a verdict, and nothing is recorded.
        """
        node = self.serve_node(self.padded_reply(node_module.MAX_REPLY_BYTES + 1))
        with self.assertRaises(node_module.NodeError):
            node.block_info(block())
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def test_a_reply_of_exactly_the_size_limit_is_still_read(self):
        """The limit is inclusive: MAX_REPLY_BYTES itself is an answer."""
        node = self.serve_node(self.padded_reply(node_module.MAX_REPLY_BYTES))
        self.assertTrue(node.block_info(block())["found"])
        status, answer, stored = self.attach_and_read_back(node)
        self.assertEqual(status, 200, answer)
        self.assertTrue(stored["confirmed"])
        self.assertEqual(stored["block_hash"], block())

    def test_an_absurd_amount_xno_is_a_400_not_a_dropped_connection(self):
        """`str(raw)` in `create` raised ValueError past the digit limit."""
        application = Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock())
        with self.assertRaises(LedgerError) as caught:
            application.handle("POST", "/v1/receipts", {}, {"X-Ledger-Token": TOKEN}, {
                "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
                "amount_xno": "9" * 4290, "reason": "why"})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "amount_out_of_range")


class NodeValuesEchoedIntoARefusal(NodeStubs, unittest.TestCase):
    """A node's `subtype` and `link_as_account` are echoed into a 409's detail.

    They were echoed as sent. Nested a thousand lists deep, Python 3.12's
    parser read them and `json.dumps(..., indent=2)` then raised
    RecursionError while the refusal was being written, so the caller's
    connection was dropped. On 3.10 and 3.11 the parse itself fails (a 503
    hold); on 3.12 and 3.13 it is a 409 whose detail is short. Either is an
    answer; a dropped connection is not.
    """

    def nested_reply(self, field, depth):
        answer = {"amount": str(XNO // 100), "confirmed": "true", "subtype": "send",
                  "contents": {"type": "state", "link_as_account": SELLER}}
        marker = '"__NESTED__"'
        if field == "subtype":
            answer["subtype"] = "__NESTED__"
        else:
            answer["contents"]["link_as_account"] = "__NESTED__"
        return json.dumps(answer).replace(marker, "[" * depth + "]" * depth).encode("utf-8")

    def assert_answered(self, field, depth):
        status, answer = self.attach_over_http(
            self.serve_node(self.nested_reply(field, depth)))
        self.assertIn(status, (409, 503), answer)
        if status == 503:
            self.assertEqual(answer["error"], "node_unavailable")
        else:
            self.assertEqual(answer["error"], "block_mismatch")
            shown = answer["detail"][field]["got"]
            self.assertIsInstance(shown, str)
            self.assertLess(len(shown), 200)

    def test_a_nested_destination_1000_deep_is_answered(self):
        self.assert_answered("destination", 1000)

    def test_a_nested_destination_5000_deep_is_answered(self):
        self.assert_answered("destination", 5000)

    def test_a_nested_subtype_1000_deep_is_answered(self):
        self.assert_answered("subtype", 1000)

    def test_a_nested_subtype_5000_deep_is_answered(self):
        self.assert_answered("subtype", 5000)

    def test_echoed_node_values_are_short_strings(self):
        from core import _shown_value
        self.assertIsNone(_shown_value(None))
        self.assertEqual(_shown_value("receive"), "receive")
        self.assertLess(len(_shown_value("x" * 100000)), 200)
        self.assertEqual(_shown_value([[[]]]), "<a list, not a string>")
        self.assertEqual(_shown_value({"a": 1}), "<a dict, not a string>")
        self.assertEqual(_shown_value(10 ** 5000), "<a int, not a string>")
        app = Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock())
        receipt = app.ledger.create({
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why"})
        info = {"found": True, "confirmed": True, "subtype": "x" * 5000,
                "destination": [[["deep"]]], "amount_raw": str(XNO)}
        with self.assertRaises(LedgerError) as caught:
            app.ledger.attach_block(receipt["id"], block(), info)
        detail = caught.exception.detail
        self.assertLess(len(detail["subtype"]["got"]), 200)
        self.assertEqual(detail["destination"]["got"], "<a list, not a string>")


class AnAnswerThatCannotBeWritten(unittest.TestCase):
    """Whatever the answer is, the caller gets a status line and a JSON body.

    `Handler._send` called `json.dumps` unguarded. Anything it could not encode
    - nested past the recursion limit, circular, or of a type JSON has no
    spelling for - raised out of the handler and closed the connection.
    """

    def serve(self, payload):
        import threading

        class Returns(Application):
            def handle(self, method, path, query, headers, body):
                return 200, payload

        application = Returns(store=Store(), node=None, token=TOKEN, clock=Clock())
        server = make_server(application, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/v1/receipts" % server.server_address[1],
                    timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def assert_500(self, payload):
        status, answer = self.serve(payload)
        self.assertEqual(status, 500, answer)
        self.assertEqual(answer["error"], "unencodable_response")

    def test_a_payload_nested_past_the_recursion_limit_is_a_500(self):
        deep = []
        for _ in range(100000):
            deep = [deep]
        self.assert_500({"detail": deep})

    def test_a_circular_payload_is_a_500(self):
        loop = {}
        loop["self"] = loop
        self.assert_500(loop)

    def test_a_payload_json_cannot_spell_is_a_500(self):
        self.assert_500({"value": object()})

    def test_a_request_body_nested_too_deeply_is_a_400(self):
        import threading
        application = Application(store=Store(), node=None, token=TOKEN, clock=Clock())
        server = make_server(application, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = b"[" * 200000 + b"]" * 200000
        request = urllib.request.Request(
            "http://127.0.0.1:%d/v1/receipts" % server.server_address[1],
            data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Ledger-Token": TOKEN})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, answer = response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status, answer = error.code, json.loads(error.read())
        self.assertEqual(status, 400, answer)
        self.assertEqual(answer["error"], "bad_json")


class ADeeplyNestedLinked(unittest.TestCase):
    """A caller's `linked` is stored and echoed; it must be writable as JSON.

    On 3.12 and 3.13 a `linked` nested 1500 deep parsed, was appended to the
    ledger, and then made `Store.flush` raise RecursionError: the connection
    was dropped and the receipt was in memory but not on disk.
    """

    def test_it_is_refused_before_anything_is_written(self):
        import threading
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        store = Store(os.path.join(directory, "ledger.json"))
        application = Application(store=store, node=None, token=TOKEN, clock=Clock())
        server = make_server(application, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = json.dumps({
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why", "linked": {"x": "__NESTED__"},
        }).replace('"__NESTED__"', "[" * 1500 + "]" * 1500).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/v1/receipts" % server.server_address[1],
            data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Ledger-Token": TOKEN})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, answer = response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status, answer = error.code, json.loads(error.read())
        self.assertEqual(status, 400, answer)
        # bad_json on 3.10/3.11, whose parser gives up first; bad_linked after.
        self.assertIn(answer["error"], ("bad_json", "bad_linked"))
        self.assertEqual(store.receipts(), [])

    def test_a_shallow_linked_is_still_stored(self):
        application = Application(store=Store(), node=None, token=TOKEN, clock=Clock())
        receipt = application.ledger.create({
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why",
            "linked": {"job_id": "job-1", "refs": [{"a": [1, 2]}]}})
        self.assertEqual(receipt["linked"]["refs"], [{"a": [1, 2]}])
        with self.assertRaises(LedgerError) as caught:
            deep = []
            for _ in range(40):
                deep = [deep]
            application.ledger.create({
                "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
                "amount_xno": "1", "reason": "why", "linked": {"x": deep}})
        self.assertEqual(caught.exception.code, "bad_linked")


class AReplyThatIsNotHTTP(NodeStubs, unittest.TestCase):
    """Replies http.client itself refuses, written byte for byte on a raw socket.

    Each raises an http.client.HTTPException subclass - BadStatusLine,
    LineTooLong, HTTPException("got more than 100 headers"), IncompleteRead -
    and none of those is an OSError or a ValueError, so each went past `rpc`'s
    guard and dropped the caller's connection. Each is a reply that could not
    be read: a 503 hold, with nothing written.
    """

    def serve_raw(self, raw):
        import socket
        import threading
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head, _, rest = data.partition(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1])
                    while len(rest) < length:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        rest += chunk
                    conn.sendall(raw)
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=loop, daemon=True).start()
        return node_module.NanoNode(
            "http://127.0.0.1:%d" % listener.getsockname()[1], timeout=10)

    def assert_held(self, raw):
        node = self.serve_raw(raw)
        with self.assertRaises(node_module.NodeError):
            node.block_info(block())
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def test_a_garbage_status_line(self):
        self.assert_held(b"HELLO WORLD\r\n\r\n" + self.reply())

    def test_a_status_code_that_is_not_a_number(self):
        self.assert_held(b"HTTP/1.1 abc OK\r\n\r\n")

    def test_a_header_line_over_64_kib(self):
        self.assert_held(b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000 + b"\r\n\r\n" + self.reply())

    def test_more_than_100_headers(self):
        self.assert_held(b"HTTP/1.1 200 OK\r\n" + b"X: a\r\n" * 200 + b"\r\n" + self.reply())

    def test_a_chunk_size_that_is_not_hex(self):
        self.assert_held(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n"
                         + self.reply())

    def test_a_chunked_body_cut_short(self):
        self.assert_held(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nabc")

    def test_a_well_formed_chunked_reply_is_still_read(self):
        body = self.reply()
        node = self.serve_raw(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                              + b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        self.assertTrue(node.block_info(block())["confirmed"])


class AmountXnoAtTheDoor(Base):
    """`parse_xno` edge cases: the 128-bit ceiling, script digits, padding."""

    def test_the_largest_amount_at_six_places_is_accepted(self):
        self.assertEqual(parse_xno("340282366.920938"), 340282366920938 * 10 ** 24)

    def test_one_step_past_the_ceiling_is_refused(self):
        from money import AmountError
        with self.assertRaises(AmountError):
            parse_xno("340282366.920939")

    def test_digits_that_are_not_ascii_are_refused(self):
        # Arabic-Indic one, fullwidth one, an Arabic-Indic fraction, a
        # superscript two: str.isdigit() is True for every one of them.
        for amount in ("١", "１", "1.٥", "٠.5", "²"):
            with self.subTest(amount=amount):
                with self.assertRaises(LedgerError) as caught:
                    self.create(amount_xno=amount)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "amount_out_of_range")

    def test_zero_padding_past_the_digit_limit_is_read_not_dropped(self):
        self.assertEqual(parse_xno("0" * 5000 + "1"), XNO)
        self.assertEqual(parse_xno("0" * 5000 + "1.5"), XNO + XNO // 2)
        self.assertEqual(parse_xno("0" * 5000 + ".5"), XNO // 2)
        receipt = self.create(amount_xno="0" * 5000 + "0.05")
        self.assertEqual(receipt["amount_xno"], "0.050000")

    def test_padding_does_not_open_a_way_round_the_other_refusals(self):
        for amount in ("0" * 5000, "0" * 5000 + ".0", "0" * 10 + "1" + "0" * 9,
                       "-" + "0" * 5000 + "1", "0" * 5000 + "1.0000001"):
            with self.subTest(amount=amount[:20]):
                with self.assertRaises(LedgerError) as caught:
                    self.create(amount_xno=amount)
                self.assertEqual(caught.exception.code, "amount_out_of_range")


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


class AMalformedContentLength(unittest.TestCase):
    """A bad Content-Length header must be answered, not dropped.

    Written against a raw socket because `urllib` will not send either header:
    it computes Content-Length itself. The two values below are what a broken
    or hostile client actually puts on the wire, and both used to reach
    `int(...)` unguarded in `Handler._dispatch`. `abc` raised ValueError out of
    the handler and the connection closed with no status line; `-1` is truthy,
    so `rfile.read(-1)` read until the socket closed and the request never
    returned. The endpoint is the one an agent calls to have its XNO settlement
    recorded, so a caller that cannot tell "retry" from "refused" is the harm.
    """

    def setUp(self):
        import threading
        self.application = Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock())
        self.server = make_server(self.application, "127.0.0.1", 0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def raw_request(self, header_value, body=b"{}"):
        """Send one request verbatim and return whatever came back, if anything."""
        import socket
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            sock.sendall(
                b"POST /v1/receipts/rcpt_0000000000000000/attach-block HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"X-Ledger-Token: " + TOKEN.encode() + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + header_value + b"\r\n\r\n" + body)
            chunks = []
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
            return b"".join(chunks)
        except socket.timeout:
            return b"<the request never returned>"
        finally:
            sock.close()

    def assert_json_error(self, answer, status, code):
        self.assertTrue(answer.startswith(b"HTTP/1."),
                        "no HTTP response at all: %r" % answer[:120])
        self.assertIn(b" %d " % status, answer.split(b"\r\n")[0])
        payload = json.loads(answer.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertEqual(payload["error"], code)

    def test_a_content_length_that_is_not_a_number_is_answered_400(self):
        self.assert_json_error(self.raw_request(b"abc"), 400, "bad_content_length")

    def test_a_negative_content_length_is_answered_400_and_does_not_hang(self):
        self.assert_json_error(self.raw_request(b"-1", body=b""), 400, "bad_content_length")

    def test_a_body_larger_than_the_cap_is_answered_413_without_reading_it(self):
        # The body is never sent: the cap is in the declared length, so the
        # answer must come back without the service waiting for the bytes.
        self.assert_json_error(
            self.raw_request(str(app_module.MAX_BODY_BYTES + 1).encode(), body=b""),
            413, "body_too_large")

    def test_a_well_formed_length_still_works(self):
        # The control: the same path with a correct header reaches the ledger
        # and gets its ordinary 404 for an id that does not exist.
        self.assert_json_error(self.raw_request(b"2"), 404, "not_found")


class ABodyShorterThanItsContentLength(unittest.TestCase):
    """A caller that declares more bytes than it sends must get a 408, not a hung worker.

    `rfile.read(length)` had no socket timeout, so `Content-Length: 100` with a
    two-byte body held a worker thread for as long as the caller kept the
    socket open. Over a real socket: the client sends the short body and waits.
    """

    def setUp(self):
        import threading
        from unittest import mock
        patch = mock.patch.object(app_module, "REQUEST_TIMEOUT_SECONDS", 0.5, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.server = make_server(Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock()),
                                  "127.0.0.1", 0)
        self.host, self.port = self.server.server_address[:2]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_short_body_is_answered_408_and_the_connection_closed(self):
        import socket
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            sock.sendall(
                b"POST /v1/receipts/rcpt_0000000000000000/attach-block HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"X-Ledger-Token: " + TOKEN.encode() + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{}")
            chunks = []
            try:
                while True:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    chunks.append(piece)
            except socket.timeout:
                self.fail("the request never returned: a worker is still waiting for 98 bytes")
            answer = b"".join(chunks)
        finally:
            sock.close()
        self.assertTrue(answer.startswith(b"HTTP/1."), "no HTTP response at all: %r" % answer[:120])
        self.assertIn(b" 408 ", answer.split(b"\r\n")[0])
        error = json.loads(answer.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(error["error"], "request_timeout")
        self.assertIn("did not arrive in time (2 of 100 bytes after", error["message"])

    def test_a_body_cut_short_by_the_caller_closing_is_answered_400_and_not_recorded(self):
        """EOF before Content-Length bytes is not a body: nothing is parsed or written.

        A caller that shuts down its write side after a valid JSON prefix used to
        get that prefix processed as the whole body - a 201 and a receipt in the
        append-only ledger, from a request that never finished arriving.
        """
        import socket
        body = json.dumps({
            "kind": "work_settlement", "counterparty": "arion",
            "counterparty_address": SELLER, "amount_xno": "0.050000",
            "reason": "delivered the extraction job"}).encode()
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            sock.sendall(
                b"POST /v1/receipts HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"X-Ledger-Token: " + TOKEN.encode() + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % (len(body) + 50) + body)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
            answer = b"".join(chunks)
        finally:
            sock.close()
        self.assertTrue(answer.startswith(b"HTTP/1."), "no HTTP response at all: %r" % answer[:120])
        self.assertIn(b" 400 ", answer.split(b"\r\n")[0])
        error = json.loads(answer.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(error["error"], "incomplete_body")
        self.assertIn("%d of %d bytes, then the connection closed" % (len(body), len(body) + 50),
                      error["message"])
        self.assertEqual(self.server.RequestHandlerClass.application.store.receipts(), [])


class ARequestDripFedPastItsDeadline(unittest.TestCase):
    """The idle timeout is reset by every byte; the deadline bounds the whole request.

    A caller sending one byte just inside the idle timeout, forever, used to hold
    a worker thread forever. With the idle timeout at 1 s and the deadline at
    2 s, bytes arriving every 0.3 s never trip the first and must trip the second.
    """

    IDLE, DEADLINE, DRIP = 1.0, 2.0, 0.3
    HEAD = (b"POST /v1/receipts/rcpt_0000000000000000/attach-block HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Ledger-Token: " + TOKEN.encode() + b"\r\n"
            b"Content-Type: application/json\r\n")

    def setUp(self):
        import threading
        from unittest import mock
        for name, value in (("REQUEST_TIMEOUT_SECONDS", self.IDLE),
                            ("REQUEST_DEADLINE_SECONDS", self.DEADLINE)):
            patch = mock.patch.object(app_module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.server = make_server(Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock()),
                                  "127.0.0.1", 0)
        self.host, self.port = self.server.server_address[:2]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def exchange(self, opening, drip=b"", gap=None):
        """Send `opening`, then `drip` one piece every `gap` seconds; return (answer, seconds)."""
        import socket
        import threading
        import time
        sock = socket.create_connection((self.host, self.port), timeout=10)
        stop = threading.Event()

        def dripper():
            for piece in drip:
                if stop.wait(gap):
                    return
                try:
                    sock.sendall(piece)
                except OSError:
                    return

        started = time.monotonic()
        sock.sendall(opening)
        thread = threading.Thread(target=dripper, daemon=True)
        thread.start()
        chunks = []
        try:
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
        except socket.timeout:
            self.fail("the connection was still open after 10 s")
        except ConnectionResetError:
            pass
        finally:
            elapsed = time.monotonic() - started
            stop.set()
            thread.join()
            sock.close()
        return b"".join(chunks), elapsed

    def test_a_drip_fed_body_is_answered_408_at_the_deadline(self):
        answer, elapsed = self.exchange(self.HEAD + b"Content-Length: 1000\r\n\r\n",
                                        [b" "] * 1000, self.DRIP)
        self.assertIn(b" 408 ", answer.split(b"\r\n")[0], answer[:200])
        error = json.loads(answer.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(error["error"], "request_timeout")
        self.assertRegex(error["message"], r"\(\d+ of 1000 bytes after 2\.\d s\)")
        self.assertGreaterEqual(elapsed, self.DEADLINE - 0.1)
        self.assertLess(elapsed, self.DEADLINE + 1.0)

    def test_drip_fed_headers_are_closed_at_the_deadline(self):
        answer, elapsed = self.exchange(self.HEAD, [b"X"] * 1000, self.DRIP)
        self.assertEqual(answer, b"")
        self.assertGreaterEqual(elapsed, self.DEADLINE - 0.1)
        self.assertLess(elapsed, self.DEADLINE + 1.0)

    def test_an_honest_body_in_a_few_quick_chunks_is_read_whole(self):
        body = json.dumps({"block_hash": "A" * 64}).encode()
        pieces = [body[:20], body[20:50], body[50:]]
        answer, elapsed = self.exchange(
            self.HEAD + b"Content-Length: %d\r\n\r\n" % len(body), pieces, 0.4)
        # 404 not_found is the answer for this body to an unknown receipt: it was
        # read whole and parsed, which a 400 bad_json or a 408 would deny.
        self.assertIn(b" 404 ", answer.split(b"\r\n")[0], answer[:200])
        self.assertEqual(json.loads(answer.split(b"\r\n\r\n", 1)[1])["error"], "not_found")
        self.assertLess(elapsed, self.DEADLINE)


class AResponseAfterHeadersThatFinishNearTheDeadline(unittest.TestCase):
    """The deadline bounds the request; the response is written under the idle timeout.

    The read side leaves the socket timeout at whatever was left of the deadline.
    A GET (no body read at all) whose headers finished 0.2 s before the deadline
    was written under a 0.2 s timeout, and a large response read at an ordinary
    pace was cut off part way. Idle timeout 4 s, deadline 2 s, an 8 MB answer
    read 512 KB every 0.1 s: it must arrive whole.
    """

    IDLE, DEADLINE, SIZE = 4.0, 2.0, 8_000_000

    def setUp(self):
        import threading
        size = self.SIZE

        class BigAnswer(Application):
            def handle(self, method, path, query, headers, body):
                return 200, {"blob": "x" * size}

        self.server = make_server(BigAnswer(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock()),
                                  "127.0.0.1", 0, request_timeout=self.IDLE,
                                  request_deadline=self.DEADLINE)
        self.host, self.port = self.server.server_address[:2]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_large_response_read_slowly_arrives_whole(self):
        import socket
        import time
        sock = socket.create_connection((self.host, self.port), timeout=10)
        try:
            started = time.monotonic()
            sock.sendall(b"GET /v1/receipts HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            while time.monotonic() - started < self.DEADLINE - 0.25:
                sock.sendall(b"X-Pad: 1\r\n")
                time.sleep(0.3)
            sock.sendall(b"\r\n")
            headers_done = time.monotonic() - started
            chunks, since_pause = [], 0
            while True:
                piece = sock.recv(64 * 1024)
                if not piece:
                    break
                chunks.append(piece)
                since_pause += len(piece)
                if since_pause >= 512 * 1024:
                    since_pause = 0
                    time.sleep(0.1)
        finally:
            sock.close()
        self.assertLess(headers_done, self.DEADLINE)
        answer = b"".join(chunks)
        head, _, body = answer.partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        declared = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        self.assertGreater(declared, self.SIZE)
        self.assertEqual(len(body), declared, "the response was cut off")


class TimeoutsOutOfRange(unittest.TestCase):
    """A timeout of 0 makes the socket non-blocking; a deadline of 0 or less
    switches the deadline off or drops every connection; a value above
    MAX_TIMEOUT_SECONDS overflows socket.settimeout and drops every connection
    too. All of them are refused, and so is anything that is not a number."""

    BAD = (0, 0.0, -1, -0.5, float("nan"), float("inf"), True, "30",
           1e10, 10**10, MAX_TIMEOUT_SECONDS + 1, 10**400, -10**400)

    def test_make_server_refuses_them(self):
        for bad in self.BAD:
            for keyword in ("request_timeout", "request_deadline"):
                with self.subTest(keyword=keyword, value=bad):
                    with self.assertRaises(ValueError):
                        make_server(Application(store=Store()), "127.0.0.1", 0, **{keyword: bad})

    def test_make_server_accepts_positive_seconds(self):
        server = make_server(Application(store=Store()), "127.0.0.1", 0,
                             request_timeout=0.5, request_deadline=2)
        server.server_close()
        self.assertEqual((server.RequestHandlerClass.timeout,
                          server.RequestHandlerClass.deadline), (0.5, 2))

    def test_make_server_accepts_the_upper_bound(self):
        """At MAX_TIMEOUT_SECONDS for both limits a real request is still answered."""
        import threading
        server = make_server(Application(store=Store()), "127.0.0.1", 0,
                             request_timeout=MAX_TIMEOUT_SECONDS,
                             request_deadline=MAX_TIMEOUT_SECONDS)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/v1/receipts" % server.server_address[1]
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()

    def test_serve_py_refuses_them(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("serve", os.path.join(ROOT, "serve.py"))
        serve = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(serve)
        for bad in ("0", "-1", "-0.5", "nan", "inf", "abc", "1e10", "1000000001"):
            for flag in ("--request-timeout", "--request-deadline"):
                with self.subTest(flag=flag, value=bad):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                        serve.main([flag, bad, "--port", "0"])
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn(flag, stderr.getvalue())
        self.assertEqual(serve.positive_seconds("1.5"), 1.5)
        self.assertEqual(serve.positive_seconds("1e9"), MAX_TIMEOUT_SECONDS)


GUARD_STEP = "No token, seed or key may be committed"
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")

# Every fixture below joins this to its value at runtime. Spelling the
# assignment out in one piece would plant a literal in a tracked file, which is
# exactly what the guard exists to refuse - it would fail on this very file.
TOKEN_VAR = "RECEIPT_LEDGER_TOKEN"


def guard_script():
    """CI's secret-guard step, lifted out of the workflow rather than restated.

    Reading it from the workflow is the point: the suite then checks the guard
    CI actually runs, so the two cannot drift into disagreeing about what
    counts as a committed secret.
    """
    with open(WORKFLOW) as fh:
        lines = fh.read().splitlines()

    start = next((i for i, line in enumerate(lines) if GUARD_STEP in line), None)
    assert start is not None, "the workflow no longer has a step named %r" % GUARD_STEP
    run = next((j for j in range(start + 1, len(lines))
                if lines[j].strip().startswith("run:")), None)
    assert run is not None, "the %r step has no run: block" % GUARD_STEP

    body, indent = [], None
    for line in lines[run + 1:]:
        if not line.strip():
            body.append("")
            continue
        here = len(line) - len(line.lstrip())
        if indent is None:
            indent = here
        if here < indent:
            break
        body.append(line[indent:])
    return "\n".join(body)


class SecretGuard(unittest.TestCase):
    """The secret guard, run here so a false positive costs a local run, not a red main.

    This repository's first CI run ever failed on that step and stayed red for
    thirty hours. The guard was wrong, not the tree: it flagged the README's own
    start command, which generates a fresh token and commits nothing. Every
    audit ran this suite, found it green, and had no reason to look at Actions.
    So the guard belongs in the suite.
    """

    def run_guard(self, cwd):
        return subprocess.run(["bash", "-e", "-c", guard_script()],
                              cwd=cwd, capture_output=True, text=True)

    def guard_in(self, *files):
        """Run the guard over a tree containing only the given (name, text) files."""
        with tempfile.TemporaryDirectory() as d:
            for name, text in files:
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(text + "\n")
            return self.run_guard(d)

    def test_the_guard_passes_on_the_tracked_tree(self):
        """What CI checks out is the tracked files, so that is what gets scanned."""
        try:
            listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                                    capture_output=True)
        except FileNotFoundError:
            self.skipTest("git is not available to enumerate the tracked tree")
        if listed.returncode != 0:
            self.skipTest("not a git checkout")

        with tempfile.TemporaryDirectory() as d:
            for name in listed.stdout.decode().split("\0"):
                src = os.path.join(ROOT, name)
                if not name or not os.path.isfile(src):
                    continue
                dst = os.path.join(d, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            result = self.run_guard(d)

        self.assertEqual(result.returncode, 0,
                         "CI's secret guard rejects this tree:\n"
                         + result.stdout + result.stderr)

    def test_a_literal_token_is_still_refused(self):
        for spelling in ('="hunter2secret"',
                         "='hunter2secret'",
                         ' = "hunter2secret"',
                         '="Ab3-xY_9kQ"'):
            leak = TOKEN_VAR + spelling
            with self.subTest(spelling=leak):
                result = self.guard_in(("leak.md", leak))
                self.assertEqual(result.returncode, 1,
                                 "the guard let a literal token through: " + leak)

    def test_a_bare_64_hex_secret_is_still_refused(self):
        result = self.guard_in(("seed.md", "seed: " + "A1" * 32))
        self.assertEqual(result.returncode, 1,
                         "the guard let 64 standalone hex characters through")

    def test_a_token_the_reader_generates_is_not_a_literal(self):
        """The README's start command, and anything else holding no secret itself."""
        for value in ("\"$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\"",
                      '"$MY_TOKEN"',
                      '""'):
            with self.subTest(value=value):
                result = self.guard_in(("run.md", TOKEN_VAR + "=" + value))
                self.assertEqual(result.returncode, 0,
                                 "the guard flagged a line carrying no secret: "
                                 + value + "\n" + result.stdout)

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



class ReadmeCounts(unittest.TestCase):
    """The README's advertised test counts, checked against the suites themselves.

    They had drifted: the README offered "34 unit tests" and "21 end-to-end
    checks" to a reader whose run prints 48 and 23. Nothing was broken by it,
    but this repository's whole argument is that its claims can be checked
    rather than believed, and the first two numbers a reader can check were
    wrong. Counting the suites here instead of restating a number is the same
    move `guard_script` makes with the workflow: the two cannot drift into
    disagreeing, because only one of them is written down.
    """

    def readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            return fh.read()

    def advertised(self, pattern):
        found = re.search(pattern, self.readme())
        self.assertIsNotNone(
            found, "the README no longer advertises a count matching %r" % pattern)
        return int(found.group(1))

    def test_the_readme_unit_test_count_is_the_suite_size(self):
        # Exactly what the README's own command does: discover -s tests.
        suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
        self.assertEqual(
            self.advertised(r"#\s*(\d+)\s+unit tests"), suite.countTestCases())

    def test_the_readme_end_to_end_count_is_the_number_of_checks(self):
        """Counted from e2e_check.py's own `check(...)` calls, not from a run.

        The unit suite opens no socket and starts no subprocess, and running the
        end-to-end script here to count its output would break that. Every
        `check(...)` in that file sits at module level -- none is inside a loop,
        which this test also asserts -- so the call sites are the count.
        """
        with open(os.path.join(ROOT, "e2e_check.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "check"]

        looped = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.While, ast.comprehension)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == "check"):
                    looped += 1
        self.assertEqual(looped, 0,
                         "a check() inside a loop means the call sites are no "
                         "longer the count; count a real run instead")

        self.assertEqual(
            self.advertised(r"#\s*(\d+)\s+end-to-end checks"), len(calls))


# --------------------------------------------------------------------------
# the fourth review of the unreadable-reply fixes
# --------------------------------------------------------------------------

class AWrongDestinationIsShownWhole(Base):
    """A 409 that says the money went elsewhere has to say where, exactly.

    The echo limit was 64 characters and a `nano_` address is 65, so every
    wrong destination came back missing its last character: the one field a
    payer needs to compare against their own records was the one cut.
    """

    def refusal(self, destination):
        receipt = self.create()
        with self.assertRaises(LedgerError) as caught:
            self.settle(receipt, destination=destination)
        self.assertEqual(caught.exception.code, "block_mismatch")
        return caught.exception.detail["destination"]

    def test_a_wrong_nano_address_is_echoed_byte_exact(self):
        self.assertEqual(len(OTHER), 65)
        shown = self.refusal(OTHER)
        self.assertEqual(shown["got"], OTHER)
        self.assertEqual(shown["expected"], SELLER)

    def test_a_wrong_xrb_address_is_echoed_byte_exact(self):
        self.assertEqual(self.refusal(OTHER_XRB)["got"], OTHER_XRB)

    def test_a_long_junk_destination_is_still_clipped(self):
        from core import MAX_SHOWN, _shown_value
        self.assertEqual(_shown_value("a" * MAX_SHOWN), "a" * MAX_SHOWN)
        self.assertNotEqual(_shown_value("a" * (MAX_SHOWN + 1)), "a" * (MAX_SHOWN + 1))
        shown = self.refusal("nano_" + "1" * 5000)["got"]
        self.assertLess(len(shown), 200)
        self.assertIn("5005 characters", shown)


class APlainTextReceiptIsAlwaysAnswered(unittest.TestCase):
    """`Accept: text/plain` on a receipt, over a real socket.

    A lone surrogate is valid in a JSON string and so reaches the store, and
    `text.encode("utf-8")` in `_send_text` raised UnicodeEncodeError on it:
    the pasteable form of that receipt was a closed connection.
    """

    def serve(self, application):
        import threading
        server = make_server(application, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def fetch(self, base, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_a_lone_surrogate_in_a_stored_field_is_answered(self):
        application = Application(store=Store(), node=None, token=TOKEN, clock=Clock())
        base = self.serve(application)
        status, body, _ = self.fetch(base, "POST", "/v1/receipts", {
            "kind": "tip", "counterparty": "a\ud800b", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why \udfff here"}, {"X-Ledger-Token": TOKEN})
        self.assertEqual(status, 201, body)
        receipt_id = json.loads(body)["id"]
        status, body, headers = self.fetch(
            base, "GET", "/v1/receipts/%s" % receipt_id, headers={"Accept": "text/plain"})
        self.assertEqual(status, 200, body)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        text = body.decode("utf-8")
        self.assertIn("a\\ud800b", text)
        self.assertIn("why \\udfff here", text)
        # The JSON form was never affected and still round-trips the value.
        status, body, _ = self.fetch(base, "GET", "/v1/receipts/%s" % receipt_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["counterparty"], "a\ud800b")

    def test_a_plaintext_that_cannot_be_written_is_a_500(self):
        class Unwritable(Application):
            def plaintext(self, receipt):
                return 42

        application = Unwritable(store=Store(), node=None, token=TOKEN, clock=Clock())
        receipt = application.ledger.create({
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why"})
        status, body, _ = self.fetch(
            self.serve(application), "GET", "/v1/receipts/%s" % receipt["id"],
            headers={"Accept": "text/plain"})
        self.assertEqual(status, 500, body)
        self.assertEqual(json.loads(body)["error"], "unencodable_response")


class ScriptedNode(NodeStubs):
    """A raw socket node that plays a script of (pause, bytes) steps."""

    TIMEOUT = 1.0

    def serve_script(self, steps, timeout=None, tls=None, scheme="http"):
        """A raw socket node that reads the request, then sends each
        (pause, bytes) step in turn and stops early once the client hangs up.
        `tls` is a server-side SSLContext the connection is wrapped in."""
        import socket
        import threading
        import time
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(5)
                    if tls is not None:
                        conn = tls.wrap_socket(conn, server_side=True)
                    data = b""
                    while not self.whole_request(data):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    for pause, piece in steps:
                        time.sleep(pause)
                        conn.sendall(piece)
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=loop, daemon=True).start()
        return node_module.NanoNode(
            "%s://127.0.0.1:%d" % (scheme, listener.getsockname()[1]),
            timeout=timeout or self.TIMEOUT)

    @staticmethod
    def whole_request(data):
        """True once the request's headers and its Content-Length of body are
        in: over TLS they arrive as separate records, and closing with the
        body unread resets the connection before the client reads a reply."""
        head, found, body = data.partition(b"\r\n\r\n")
        if not found:
            return False
        length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
        return len(body) >= (int(length.group(1)) if length else 0)



class ANodeThatIsSlowOrLarge(ScriptedNode, unittest.TestCase):
    """`timeout` bounds each connect attempt and each read, as on main; a
    reply is also bounded in size."""

    def test_a_slow_reply_that_finishes_in_time_is_still_read(self):
        body = self.reply()
        head = (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body))
        steps = [(0, head), (0.1, body[:10]), (0.1, body[10:20]), (0.1, body[20:])]
        self.assertTrue(self.serve_script(steps, timeout=5).block_info(block())["found"])

    def test_a_node_that_goes_silent_is_a_503_hold_after_one_read_timeout(self):
        import time
        head = (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100000\r\n\r\n")
        steps = [(0, head), (10, b"")]
        node = self.serve_script(steps)
        started = time.monotonic()
        with self.assertRaises(node_module.NodeError):
            node.block_info(block())
        self.assertLess(time.monotonic() - started, node.timeout * 3)
        status, answer = self.attach_over_http(self.serve_script(steps))
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def test_a_node_streaming_without_end_is_cut_at_the_size_limit(self):
        """The read stops at MAX_REPLY_BYTES + 1 and says so."""
        import time
        head = b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n"
        steps = [(0, head)] + [(0, b" " * 65536)] * 1000
        node = self.serve_script(steps, timeout=30)
        started = time.monotonic()
        with self.assertRaises(node_module.NodeError) as caught:
            node.block_info(block())
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("more than %d bytes" % node_module.MAX_REPLY_BYTES,
                      str(caught.exception))

    def test_the_size_limit_is_the_one_the_readme_states(self):
        self.assertEqual(node_module.MAX_REPLY_BYTES, 1024 * 1024)
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            self.assertRegex(fh.read(), r"over\s+1\s+MiB")

    def test_the_readme_says_the_timeout_is_per_attempt_and_per_read(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(text, r"each\s+connect\s+attempt\s+and\s+each\s+read")
        self.assertNotRegex(text, r"one\s+deadline\s+for\s+the\s+whole")


class ARedirectOrATLSReply(ScriptedNode, unittest.TestCase):
    """Redirects are followed only to http and https; https is verified with
    the standard context."""

    def redirect(self, location):
        return [(0, b"HTTP/1.1 302 Found\r\nLocation: " + location.encode("ascii")
                 + b"\r\nConnection: close\r\n\r\n")]

    def test_a_redirect_to_ftp_is_refused_at_once(self):
        """`build_opener` installed an FTP handler, and a redirect to ftp://
        was followed: a banner dripped a byte at a time held the RPC."""
        import time
        banner = self.serve_script([(0.25, b"2")] * 200)
        ftp = banner.url.replace("http://", "ftp://") + "/x.json"
        node = self.serve_script(self.redirect(ftp))
        started = time.monotonic()
        with self.assertRaises(node_module.NodeError) as caught:
            node.block_info(block())
        self.assertLess(time.monotonic() - started, node.timeout * 1.5)
        self.assertIn("ftp", str(caught.exception))
        status, answer = self.attach_over_http(self.serve_script(self.redirect(ftp)))
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def test_file_and_data_redirects_are_refused(self):
        import time
        for location in ("file:///etc/hostname", "data:application/json,{}"):
            with self.subTest(location=location):
                started = time.monotonic()
                with self.assertRaises(node_module.NodeError):
                    self.serve_script(self.redirect(location)).block_info(block())
                self.assertLess(time.monotonic() - started, 1.5)

    def test_a_redirect_to_another_http_node_is_followed(self):
        body = self.reply()
        target = self.serve_script([(0, b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n"
                                     % len(body) + body)], timeout=5)
        node = self.serve_script(self.redirect(target.url + "/x"), timeout=5)
        self.assertTrue(node.block_info(block())["found"])

    def tls_pair(self):
        """A throwaway certificate authority and a certificate for 127.0.0.1
        it signed, made for this test: (server context, CA file to trust)."""
        import ssl
        import subprocess
        if shutil.which("openssl") is None:
            self.skipTest("openssl is needed to make a test certificate")
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)

        def run(*args):
            subprocess.run(("openssl",) + args, cwd=work, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
        run("req", "-x509", *curve, "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
            "-subj", "/CN=test-only-ca", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        run("req", *curve, "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=127.0.0.1")
        with open(os.path.join(work, "ext"), "w") as fh:
            fh.write("subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                     "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
                     "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
        run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", "srv.pem", "-days", "2", "-extfile", "ext")
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(os.path.join(work, "srv.pem"), os.path.join(work, "srv.key"))
        return server, os.path.join(work, "ca.pem")

    def trusting(self, ca_file):
        from unittest import mock
        patch = mock.patch.dict(os.environ, {"SSL_CERT_FILE": ca_file})
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_https_reply_that_finishes_is_read(self):
        server, ca_file = self.tls_pair()
        self.trusting(ca_file)
        body = self.reply()
        head = (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body))
        node = self.serve_script([(0, head + body)], timeout=5, tls=server, scheme="https")
        self.assertTrue(node.block_info(block())["found"])

    def test_an_https_node_with_an_untrusted_certificate_is_a_503_hold(self):
        server, _ = self.tls_pair()
        body = self.reply()
        head = (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body))
        node = self.serve_script([(0, head + body)], timeout=5, tls=server, scheme="https")
        with self.assertRaises(node_module.NodeError) as caught:
            node.block_info(block())
        self.assertIn("CERTIFICATE", str(caught.exception).upper())


class TheEchoLimitAndItsClip(unittest.TestCase):
    def test_128_characters_are_shown_whole_and_129_are_clipped(self):
        from core import MAX_SHOWN, _shown_value
        self.assertEqual(MAX_SHOWN, 128)
        self.assertEqual(_shown_value("a" * 128), "a" * 128)
        self.assertEqual(_shown_value("a" * 128 + "b"), "a" * 128 + "... (129 characters)")
        self.assertEqual(_shown_value("a" * 1000), "a" * 128 + "... (1000 characters)")

    def test_a_non_string_amount_is_named_by_its_type(self):
        app = Application(store=Store(), node=FakeNode(), token=TOKEN, clock=Clock())
        receipt = app.ledger.create({
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why"})
        for amount, shown in ((7, "<a int, not a string>"),
                              (10 ** 5000, "<a int, not a string>"),
                              ([1], "<a list, not a string>"),
                              ("9" * 200, "9" * 128 + "... (200 characters)")):
            with self.subTest(shown=shown):
                info = {"found": True, "confirmed": True, "subtype": "send",
                        "destination": SELLER, "amount_raw": amount}
                with self.assertRaises(LedgerError) as caught:
                    app.ledger.attach_block(receipt["id"], block(), info)
                self.assertEqual(caught.exception.detail["amount"]["got"], shown)

    def test_the_readme_states_the_clip_format(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue("first 128 characters followed by `... (N characters)`" in readme,
                        "the README does not state the clip format")


class AnAmountRefusalSaysWhatIsWrong(unittest.TestCase):
    """The shape is judged before the range, on the caller's own text."""

    def refused(self, sent):
        with self.assertRaises(AmountError) as caught:
            parse_xno(sent)
        return str(caught.exception)

    def test_each_refusal_names_the_right_reason_and_quotes_the_text(self):
        for sent, reason in (("abcdefghijk", "not a decimal number"),
                             ("-0000000001", "must not be negative"),
                             ("-1", "must not be negative"),
                             ("00-1", "not a decimal number"),
                             ("1.2.3", "not a decimal number"),
                             ("+1", "not a decimal number"),
                             (".", "not a decimal number"),
                             ("", "empty"),
                             ("0", "greater than zero"),
                             ("0.0000001", "7 decimal places"),
                             ("340282366.920939", "2**128 - 1"),
                             ("9" * 30, "2**128 - 1"),
                             ("\u0661", "ASCII")):
            with self.subTest(sent=sent):
                message = self.refused(sent)
                self.assertIn(reason, message)
                self.assertIn(repr(sent), message)

    def test_the_quoted_text_is_clipped_at_40_characters(self):
        self.assertIn(repr("x" * 40), self.refused("x" * 40))
        message = self.refused("x" * 41)
        self.assertIn(repr("x" * 40 + "..."), message)
        self.assertNotIn("x" * 41, message)
        message = self.refused("0" * 5000 + "-1")
        self.assertIn(repr("0" * 40 + "..."), message)
        self.assertLess(len(message), 200)

    def test_well_formed_amounts_still_parse(self):
        for sent, raw in (("1", XNO), (".5", XNO // 2), ("1.", XNO),
                          (" 0001.250000 ", XNO * 5 // 4),
                          ("0" * 5000 + "1", XNO),
                          ("340282366.920938", 340282366920938 * 10 ** 24)):
            with self.subTest(sent=sent[:20]):
                self.assertEqual(parse_xno(sent), raw)


class TheLinkedDepthLimit(Base):
    """`linked` may nest exactly MAX_LINKED_DEPTH (16) levels; the 17th is refused.

    The object `linked` itself is level one, and a list counts as a level
    exactly as an object does.
    """

    def create_linked(self, linked):
        return self.call("POST", "/v1/receipts", {
            "kind": "tip", "counterparty": "a", "counterparty_address": SELLER,
            "amount_xno": "1", "reason": "why", "linked": linked}, token=TOKEN)

    @staticmethod
    def objects(depth):
        value = "x"
        for _ in range(depth):
            value = {"k": value}
        return value

    @staticmethod
    def lists_inside_an_object(depth):
        value = "x"
        for _ in range(depth - 1):
            value = [value]
        return {"k": value}

    def assert_limit(self, build):
        from core import MAX_LINKED_DEPTH
        self.assertEqual(MAX_LINKED_DEPTH, 16)
        status, receipt = self.create_linked(build(16))
        self.assertEqual(status, 201, receipt)
        with self.assertRaises(LedgerError) as caught:
            self.create_linked(build(17))
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "bad_linked")

    def test_sixteen_objects_deep_is_stored_and_seventeen_refused(self):
        self.assert_limit(self.objects)

    def test_lists_inside_linked_count_as_levels(self):
        self.assert_limit(self.lists_inside_an_object)


class AnAmountErrorQuotesTheCallersText(unittest.TestCase):
    def test_the_refusal_quotes_what_was_sent_not_what_was_stripped(self):
        for sent in (" 0x1", "00x1", "0001.2.3"):
            with self.subTest(sent=sent):
                with self.assertRaises(AmountError) as caught:
                    parse_xno(sent)
                self.assertIn(repr(sent), str(caught.exception))


# --------------------------------------------------------------------------
# the transport is the standard one again
# --------------------------------------------------------------------------

def open_fds():
    return len(os.listdir("/proc/self/fd"))


# Other tests' stub servers close their sockets on their own threads, so the
# count moves by a few on its own; a leak in the loops below is 20 or more.
FD_SLACK = 5


class EveryAddressIsTried(NodeStubs, unittest.TestCase):
    """Each address the name resolves to is tried in turn, by the standard
    `socket.create_connection`, with the timeout applying to each attempt.

    A replacement transport tried here once shared one budget across all
    attempts: an address family the host cannot open (`::1` on a host with
    IPv6 turned off) ended the RPC, and a first address that silently drops
    packets used up the whole budget so the working one was never tried.
    """

    HOST = "several.addresses.test"

    def setUp(self):
        import socket
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("counting open descriptors needs /proc/self/fd")
        # Family 255 is no address family at all: socket() fails with
        # EAFNOSUPPORT on every host, as AF_INET6 does on one without IPv6.
        self.unopenable = (255, socket.SOCK_STREAM, 6, "", ("::1", 1))
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        self.refused = (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                        ("127.0.0.1", spare.getsockname()[1]))
        spare.close()

    def resolving_to(self, *addresses):
        import socket
        from unittest import mock
        real = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if host == self.HOST:
                return list(addresses)
            return real(host, port, *args, **kwargs)
        patch = mock.patch.object(socket, "getaddrinfo", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def live(self):
        import socket
        node = self.serve_node(self.reply())
        port = int(node.url.rsplit(":", 1)[1])
        return port, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))

    def test_an_unopenable_then_a_refused_address_falls_back_to_a_live_one(self):
        port, live = self.live()
        self.resolving_to(self.unopenable, self.refused, live)
        node = node_module.NanoNode("http://%s:%d/" % (self.HOST, port), timeout=5)
        self.assertTrue(node.block_info(block())["found"])

    def test_when_every_address_fails_it_is_a_503_hold(self):
        self.resolving_to(self.unopenable, self.refused)
        node = node_module.NanoNode("http://%s:7076/" % self.HOST, timeout=5)
        with self.assertRaises(node_module.NodeError) as caught:
            node.block_info(block())
        self.assertIn("refused", str(caught.exception).lower())
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def blackhole(self):
        """A loopback address whose connects hang: its accept queue is full
        and never drained, so every further SYN is dropped."""
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        self.addCleanup(listener.close)
        address = listener.getsockname()
        for _ in range(8):
            probe = socket.socket()
            probe.settimeout(0.2)
            try:
                probe.connect(address)
            except OSError:
                probe.close()
                return address
            self.addCleanup(probe.close)
        self.skipTest("this host does not drop connects to a full accept queue")

    def test_a_blackholed_first_address_then_a_live_one_succeeds(self):
        """The first attempt times out after `timeout`, then the live address
        answers: within about twice the timeout, as on main."""
        import socket
        import time
        host, dropped = self.blackhole()
        port, live = self.live()
        self.resolving_to(
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, dropped)), live)
        node = node_module.NanoNode("http://%s:%d/" % (self.HOST, port), timeout=0.5)
        started = time.monotonic()
        self.assertTrue(node.block_info(block())["found"])
        self.assertLess(time.monotonic() - started, 2 * node.timeout + 0.5)

    def test_the_sockets_of_failed_attempts_are_closed(self):
        port, live = self.live()
        self.resolving_to(self.unopenable, self.refused, self.refused, live)
        node = node_module.NanoNode("http://%s:%d/" % (self.HOST, port), timeout=5)
        node.block_info(block())
        before = open_fds()
        for _ in range(20):
            node.block_info(block())
        self.assertLess(open_fds() - before, FD_SLACK,
                        "a failed attempt's socket was left open")


class NoResourceWarnings(ScriptedNode, unittest.TestCase):
    """Every way an RPC ends closes what it opened."""

    def test_no_resource_warning_on_any_ending(self):
        import gc
        import warnings
        body = self.reply()
        ok = (b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
              b"Content-Length: %d\r\n\r\n" % len(body)) + body
        scripts = [
            [(0, ok)],
            [(0, b"HTTP/1.1 500 Oops\r\nContent-Length: 2\r\n\r\n{}")],
            [(0, b"HTTP/1.1 302 Found\r\nLocation: ftp://127.0.0.1/x\r\n"
                 b"Connection: close\r\n\r\n")],
            [(0, b"this is not HTTP\r\n\r\n")],
            [(0, b"HTTP/1.0 200 OK\r\n\r\n[[[[" + b"[" * 100000)],
        ]
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always", ResourceWarning)
            for steps in scripts:
                try:
                    self.serve_script(steps, timeout=5).block_info(block())
                except node_module.NodeError:
                    pass
            gc.collect()
        self.assertEqual([str(w.message) for w in seen
                          if issubclass(w.category, ResourceWarning)], [])


class ANodeAnsweringAnHTTPError(ScriptedNode, unittest.TestCase):
    """A plain 500 or 404 from the node is a 503 hold. Without
    HTTPDefaultErrorHandler in the opener the error response fell through
    unhandled and a TypeError escaped `rpc`."""

    def test_the_connection_is_closed_when_the_rpc_ends(self):
        """The error reply is held by the exception that reports it, and its
        connection used to stay open for as long as that exception lived."""
        import socket
        import threading
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        seen = []

        def serve():
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                data = b""
                while not self.whole_request(data):
                    data += conn.recv(65536)
                conn.sendall(b"HTTP/1.1 500 Oops\r\nContent-Length: 2\r\n\r\n{}")
                try:
                    seen.append(conn.recv(1))
                except OSError as exc:
                    seen.append(exc)
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        node = node_module.NanoNode("http://127.0.0.1:%d/" % listener.getsockname()[1], 5)
        with self.assertRaises(node_module.NodeError) as caught:
            node.block_info(block())
        server.join(5)
        self.assertEqual(seen, [b""], "the connection was still open after the RPC")
        self.assertIn("500", str(caught.exception))

    def test_500_and_404_are_a_503_hold(self):
        for status in (b"500 Oops", b"404 Not Found"):
            with self.subTest(status=status):
                steps = [(0, b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\n"
                          b"Connection: close\r\n\r\n{}")]
                with self.assertRaises(node_module.NodeError) as caught:
                    self.serve_script(steps, timeout=5).block_info(block())
                self.assertIn(status.split()[0].decode(), str(caught.exception))
                status_code, answer = self.attach_over_http(
                    self.serve_script(steps, timeout=5))
                self.assertEqual(status_code, 503, answer)
                self.assertEqual(answer["error"], "node_unavailable")


class AVeryLongWholePart(unittest.TestCase):
    def test_5000_digits_are_refused_as_too_large_by_length(self):
        """Refused by its length, before `int()` meets Python's 4300-digit
        limit and the refusal names that instead of the 2**128 bound."""
        sent = "9" * 5000
        with self.assertRaises(AmountError) as caught:
            parse_xno(sent)
        message = str(caught.exception)
        self.assertEqual(message, "amount_xno is more than the 2**128 - 1 raw any Nano "
                         "account can hold: %r" % (sent[:40] + "...",))


class ANodeErrorThatIsNotANotFound(NodeStubs, unittest.TestCase):
    """A node that says anything but "not found" has not said the block is absent.

    `normalise` folded EVERY `{"error": ...}` answer into the not-found reply,
    so a node answering `{"error": "Too many requests"}` - a public endpoint
    rate-limiting the read - was reported as `404 block_not_found`, "the node
    does not know block X". Nothing about the block had been read. That is the
    same "unknown reported as a fact" the non-object guard one case over exists
    to prevent, and the reason given for leaving it alone (that telling a real
    not-found from a transient refusal needs each endpoint's error strings) is
    answered by the one string every Nano node uses: `block_info` says "Block
    not found", and `nano-settlement-verify`'s `nano_claim.py` already splits
    on exactly that.

    It matters because the verdicts are not interchangeable to the caller. This
    is the endpoint an agent calls to prove its XNO settlement; a 404 says the
    payment does not exist and there is nothing to retry, while the truth -
    the node could not be read - is the 503 hold that says ask again.
    """

    TRANSIENT = ("Too many requests", "RPC control is disabled",
                 "Bad hash number", "Unable to parse JSON", "",
                 # Each contains "not found" but none is this node saying it
                 # does not know this block, so none is evidence of absence.
                 "Not Found", "Method not found", "Account not found",
                 "Block not found on this endpoint", "Unknown block: not found")

    def test_a_transient_node_error_is_a_hold_not_a_not_found(self):
        for message in self.TRANSIENT:
            with self.subTest(error=message):
                with self.assertRaises(node_module.NodeError) as caught:
                    node_module.normalise({"error": message})
                self.assertIn("says nothing about this block", str(caught.exception))

    def test_block_not_found_is_still_a_not_found(self):
        """The one answer that IS evidence of absence keeps its verdict."""
        for message in ("Block not found", "block not found", " Block not found\n"):
            with self.subTest(error=message):
                self.assertEqual(
                    node_module.normalise({"error": message}),
                    {"found": False, "confirmed": False, "subtype": None,
                     "destination": None, "amount_raw": None})

    def test_a_rate_limited_read_is_503_over_http_and_records_nothing(self):
        """End to end: the agent is told to retry, not that it never paid."""
        node = self.serve_node(json.dumps({"error": "Too many requests"}).encode("utf-8"))
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")
        self.assertIn("could not be read", answer["message"])

    def test_a_non_string_error_is_a_hold(self):
        for error in (None, 404, ["Block not found"], {"message": "Block not found"}):
            with self.subTest(error=error):
                with self.assertRaises(node_module.NodeError):
                    node_module.normalise({"error": error})

    def test_a_gateway_not_found_is_503_over_http_not_404(self):
        node = self.serve_node(json.dumps({"error": "Not Found"}).encode("utf-8"))
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer["error"], "node_unavailable")

    def test_a_genuine_not_found_is_still_404_over_http(self):
        node = self.serve_node(json.dumps({"error": "Block not found"}).encode("utf-8"))
        status, answer = self.attach_over_http(node)
        self.assertEqual(status, 404, answer)
        self.assertEqual(answer["error"], "block_not_found")


class RefusalsMadeBeforeTheNodeIsAsked(Base):
    """A refusal the ledger can make on its own does not wait on the node.

    A malformed hash was passed to the node, whose "Bad hash number" was once
    folded into a not-found, and `attach_block` then answered 400
    bad_block_hash. Once a node error other than "Block not found" became the
    503 hold, the same request was told to retry - and no retry can make a
    malformed hash valid. The hash is now checked before the node is asked.
    """

    class RateLimitedNode(FakeNode):
        def rpc(self, payload):
            self.calls.append(payload)
            return {"error": "Too many requests"}

    def attach(self, block_hash):
        return self.call("POST", "/v1/receipts/%s/attach-block" % self.receipt["id"],
                         {"block_hash": block_hash}, token=TOKEN)

    def setUp(self):
        super().setUp()
        self.receipt = self.create()

    def test_a_malformed_hash_is_400_and_the_node_is_never_asked(self):
        for node in (self.node, self.RateLimitedNode()):
            self.application.node = node
            for bad in (block().lower(), block()[:63], block() + "0", "", "G" * 64,
                        None, 12, ["A" * 64]):
                with self.subTest(node=type(node).__name__, block_hash=bad):
                    with self.assertRaises(LedgerError) as caught:
                        self.attach(bad)
                    self.assertEqual(caught.exception.status, 400)
                    self.assertEqual(caught.exception.code, "bad_block_hash")
            self.assertEqual(node.calls, [])

    def test_a_well_formed_hash_still_reaches_the_node(self):
        """Control: the 503 hold for a rate-limited node is unchanged."""
        node = self.application.node = self.RateLimitedNode()
        with self.assertRaises(LedgerError) as caught:
            self.attach(block())
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "node_unavailable")
        self.assertEqual(len(node.calls), 1)

    def test_re_attaching_is_409_even_while_the_node_is_rate_limited(self):
        self.settle(self.receipt)
        node = self.application.node = self.RateLimitedNode()
        with self.assertRaises(LedgerError) as caught:
            self.attach(block("CAFE"))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "block_already_attached")
        self.assertEqual(node.calls, [])

    def test_a_reused_block_is_409_even_while_the_node_is_rate_limited(self):
        used = block("BEEF")
        first = self.create()
        status, _ = self.settle(first, used)
        self.assertEqual(status, 200)
        node = self.application.node = self.RateLimitedNode()
        with self.assertRaises(LedgerError) as caught:
            self.attach(used)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "block_reused")
        self.assertEqual(caught.exception.detail, {"receipt_id": first["id"]})
        self.assertEqual(node.calls, [])


if __name__ == "__main__":
    unittest.main()
