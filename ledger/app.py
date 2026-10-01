"""HTTP layer: routing, auth, and the two representations of a receipt.

Every route is in ROUTES, so a test can enumerate them and assert what is
NOT there. PUT, PATCH and DELETE on a receipt are answered 405 append_only
by an explicit rule rather than by a missing handler, because "we do not
support that" and "that is not a thing this ledger can do" are different
answers and the second is the true one.
"""

import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import Ledger
from custody import CUSTODY
from errors import LedgerError
from money import format_xno
from node import NodeError
from store import Store

RECEIPT_PATH = re.compile(r"\A/v1/receipts/(?P<receipt_id>[A-Za-z0-9_]+)\Z")
ATTACH_PATH = re.compile(r"\A/v1/receipts/(?P<receipt_id>[A-Za-z0-9_]+)/attach-block\Z")
SUPERSEDE_PATH = re.compile(r"\A/v1/receipts/(?P<receipt_id>[A-Za-z0-9_]+)/supersede\Z")
INDEX_PATH = re.compile(r"\A/v1/receipts\Z")
CORRECTIONS_PATH = re.compile(r"\A/v1/corrections\Z")
CUSTODY_PATH = re.compile(r"\A/v1/custody\Z")

# (method, pattern, handler, needs_auth)
ROUTES = [
    ("GET", INDEX_PATH, "index", False),
    ("GET", RECEIPT_PATH, "detail", False),
    ("GET", CORRECTIONS_PATH, "corrections", False),
    ("GET", CUSTODY_PATH, "custody", False),
    ("POST", INDEX_PATH, "create", True),
    ("POST", ATTACH_PATH, "attach", True),
    ("POST", SUPERSEDE_PATH, "supersede", True),
]

APPEND_ONLY_METHODS = ("PUT", "PATCH", "DELETE")


def tokens_match(supplied, expected):
    """Constant-time token comparison. Called directly by the auth test."""
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class Application:
    def __init__(self, store=None, node=None, token=None, clock=None,
                 base_url="", node_rpc_url="https://<any-public-nano-node>",
                 explorer="https://nanolooker.com/block/"):
        self.store = store if store is not None else Store()
        self.node = node
        self.token = token if token is not None else os.environ.get("RECEIPT_LEDGER_TOKEN", "")
        self.base_url = base_url
        self.node_rpc_url = node_rpc_url
        self.explorer = explorer
        self.ledger = Ledger(self.store, clock or _utc_now)

    # -- dispatch ---------------------------------------------------------

    def handle(self, method, path, query, headers, body):
        if method in APPEND_ONLY_METHODS and (
                RECEIPT_PATH.match(path) or INDEX_PATH.match(path)):
            raise LedgerError(
                405, "append_only",
                "this ledger is append-only: a receipt is never edited or deleted. "
                "Correct a mistake with POST /v1/receipts/{id}/supersede.")
        for route_method, pattern, handler, needs_auth in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            if route_method != method:
                continue
            if needs_auth:
                self._authorise(headers)
            return getattr(self, "_" + handler)(match.groupdict(), query, body)
        raise LedgerError(404, "not_found", "no route for %s %s" % (method, path))

    def _authorise(self, headers):
        supplied = headers.get("X-Ledger-Token") or ""
        if not self.token or not tokens_match(supplied, self.token):
            raise LedgerError(401, "unauthorized", "a valid X-Ledger-Token is required")

    # -- handlers ---------------------------------------------------------

    def _index(self, params, query, body):
        return 200, self.ledger.index(
            kind=_first(query, "kind"),
            counterparty=_first(query, "counterparty"),
            limit=_first(query, "limit") or 50,
            cursor=_first(query, "cursor"),
            base_url=self.base_url,
        )

    def _detail(self, params, query, body):
        receipt = self.ledger.get(params["receipt_id"])
        return 200, self.render(receipt)

    def _corrections(self, params, query, body):
        return 200, {"corrections": list(self.store.corrections())}

    def _custody(self, params, query, body):
        document = json.loads(json.dumps(CUSTODY))
        for key in ("index", "corrections"):
            document["historical_proof"][key] = self.base_url + document["historical_proof"][key]
        return 200, document

    def _create(self, params, query, body):
        return 201, self.render(self.ledger.create(body))

    def _attach(self, params, query, body):
        if not isinstance(body, dict):
            raise LedgerError(400, "bad_request", "body must be a JSON object")
        block_hash = body.get("block_hash")
        # The receipt is looked up first so an unknown id is 404 rather than a
        # pointless round trip to the node.
        self.ledger.get(params["receipt_id"])
        if self.node is None:
            raise LedgerError(503, "no_node", "no Nano node is configured; nothing was attached")
        # A node that is CONFIGURED BUT DOWN is the same situation as the 503
        # one line above, and it used to get nothing at all: `NanoNode.rpc`
        # let the transport error out past `_dispatch`, which answers only
        # LedgerError, and the caller's connection was closed with no HTTP
        # response. This is the endpoint an agent calls to prove its XNO
        # payment settled, so "the node was unreachable, retry" has to be
        # distinguishable from "your block was refused". Nothing has been
        # written at this point, so the retry is safe and the message says so.
        try:
            info = self.node.block_info(block_hash) if isinstance(block_hash, str) else {"found": False}
        except NodeError as exc:
            raise LedgerError(
                503, "node_unavailable",
                "the Nano node could not be reached, so this block was not checked and "
                "nothing was attached; the receipt is unchanged and the request can be "
                "retried: %s" % exc) from None
        return 200, self.render(self.ledger.attach_block(params["receipt_id"], block_hash, info))

    def _supersede(self, params, query, body):
        if not isinstance(body, dict):
            raise LedgerError(400, "bad_request", "body must be a JSON object")
        return 200, self.render(self.ledger.supersede(
            params["receipt_id"], body.get("replacement_id"), body.get("reason")))

    # -- representations --------------------------------------------------

    def render(self, receipt):
        document = dict(receipt)
        document["url"] = "%s/v1/receipts/%s" % (self.base_url.rstrip("/"), receipt["id"])
        if receipt["block_hash"]:
            document["block_explorer_url"] = self.explorer + receipt["block_hash"]
            document["verify"] = {
                "how": "Fetch this block from any public Nano node or explorer. It is a "
                       "send of exactly amount_xno to counterparty_address. We could not "
                       "have produced it without holding the sending key, and we do not "
                       "hold the receiving key.",
                "node_rpc_example": (
                    "curl -d '{\"action\":\"block_info\",\"json_block\":\"true\","
                    "\"hash\":\"%s\"}' %s" % (receipt["block_hash"], self.node_rpc_url)),
                "expect": {
                    "destination": receipt["counterparty_address"],
                    "amount_xno": receipt["amount_xno"],
                    "subtype": "send",
                    "confirmed": "true",
                },
            }
        else:
            document["block_explorer_url"] = None
            document["verify"] = {
                "how": "Nothing to verify yet: this receipt is pending and carries no "
                       "block hash. It is not counted in paid_xno_total.",
            }
        document["custody_statement"] = (
            "The counterparty generated this address in its own process. No key of theirs "
            "has ever been transmitted to us, and no code path in our tools can request "
            "one. Verify structurally with: nano-wallet selfcheck")
        return document

    def plaintext(self, receipt):
        """<= 20 lines, because this is what gets pasted into a conversation."""
        lines = [
            "RECEIPT %s" % receipt["id"],
            "%s to %s" % (receipt["kind"].replace("_", " "), receipt["counterparty"]),
            "amount   %s XNO" % receipt["amount_xno"],
            "address  %s" % receipt["counterparty_address"],
        ]
        if receipt["block_hash"]:
            lines += [
                "block    %s" % receipt["block_hash"],
                "settled  %s" % receipt["confirmed_at"],
                "verify   %s%s" % (self.explorer, receipt["block_hash"]),
                "         it is a confirmed send of exactly this amount to that address.",
            ]
        else:
            lines += ["block    (pending - not counted as paid)"]
        if receipt["superseded_by"]:
            lines.append("NOTE     superseded by %s; the payment above still happened"
                         % receipt["superseded_by"])
        lines += [
            "why      %s" % _clip(receipt["reason"], 120),
            "custody  your key was generated in your process and never sent to us.",
        ]
        return "\n".join(lines[:20]) + "\n"


def _clip(text, width):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _first(query, key):
    values = query.get(key)
    return values[0] if values else None


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the server ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "receipt-ledger"
    application = None

    def _dispatch(self, method):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "bad_json", "message": "body is not valid JSON"})
        try:
            status, payload = self.application.handle(
                method, parsed.path, parse_qs(parsed.query), self.headers, body)
        except LedgerError as exc:
            return self._send(exc.status, exc.body())
        accept = (self.headers.get("Accept") or "").lower()
        if method == "GET" and "text/plain" in accept and RECEIPT_PATH.match(parsed.path):
            receipt = self.application.ledger.get(RECEIPT_PATH.match(parsed.path)["receipt_id"])
            return self._send_text(status, self.application.plaintext(receipt))
        return self._send(status, payload)

    def _send(self, status, payload):
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):
        pass  # a request log is one more place a token could end up


def make_server(application, host="127.0.0.1", port=0):
    handler = type("BoundHandler", (Handler,), {"application": application})
    return ThreadingHTTPServer((host, port), handler)
