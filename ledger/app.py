"""HTTP layer: routing, auth, and the two representations of a receipt.

Every route is in ROUTES, so a test can enumerate them and assert what is
NOT there. PUT, PATCH and DELETE on a receipt are answered 405 append_only
by an explicit rule rather than by a missing handler, because "we do not
support that" and "that is not a thing this ledger can do" are different
answers and the second is the true one.
"""

import hmac
import io
import json
import math
import os
import re
import socket
import time
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

#: The largest request body this service will read. Every body it accepts is a
#: small JSON object - the longest field, `reason`, is capped at 2000 characters
#: by `core.MAX_REASON` - so a megabyte is already far more than any real caller
#: sends, and a declared length beyond it is answered rather than read.
MAX_BODY_BYTES = 1_000_000

#: Seconds a single read or write on a connection may wait for progress. A
#: caller whose Content-Length is larger than the body it sends otherwise holds
#: a worker thread in `rfile.read` for as long as it keeps the socket open.
REQUEST_TIMEOUT_SECONDS = 30

#: Seconds the whole request - request line, headers and body - may take to
#: arrive, counted from the moment the connection is accepted. The idle timeout
#: alone is reset by every byte, so a caller dripping one byte every 29 seconds
#: would hold a worker thread (and `ThreadingHTTPServer` has no thread cap)
#: indefinitely. Past the deadline a body read is answered 408 and a request
#: line or header read closes the connection, as the standard library does when
#: its own timeout fires there. The server speaks HTTP/1.0, so a connection
#: carries one request and is closed after the response.
REQUEST_DEADLINE_SECONDS = 60


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
                "no usable answer came from the Nano node (it could not be reached, or "
                "its reply could not be read), so this block was not checked and "
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


def _encode(payload):
    """`payload` as the bytes of a JSON response, or None if it cannot be one.

    `json.dumps(..., indent=2)` recurses once per level of nesting and raises
    RecursionError on a value nested deeply enough; it raises ValueError on a
    circular reference and TypeError on a value JSON has no spelling for.
    """
    try:
        return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (RecursionError, ValueError, TypeError):
        return None


def _encode_text(text):
    """`text` as the bytes of a text/plain response, or None if it cannot be one.

    A stored field can hold a lone surrogate ("\\ud800"): JSON parses one
    happily and `_require_string` has no reason to refuse it. `_encode` writes
    it as an escape, but a plain `text.encode("utf-8")` raised
    UnicodeEncodeError out of `_send_text`, and `GET /v1/receipts/{id}` with
    `Accept: text/plain` closed the connection with no status line. Such a
    character is shown as its backslash escape instead; anything that still
    cannot be written comes back as None, exactly as `_encode` does.
    """
    try:
        return text.encode("utf-8", errors="backslashreplace")
    except (UnicodeError, AttributeError, TypeError):
        return None


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

class _DeadlineReader(io.RawIOBase):
    """The connection's read side, with each recv bounded by the request deadline.

    Before every recv the socket timeout is set to the idle timeout or the time
    left before the deadline, whichever is shorter; once the deadline has passed
    a read raises `socket.timeout` without waiting at all.
    """

    def __init__(self, sock, idle, deadline_at):
        self._sock = sock
        self._idle = idle
        self._deadline_at = deadline_at

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("the request deadline passed")
        self._sock.settimeout(min(self._idle, remaining) if self._idle is not None else remaining)
        return self._sock.recv_into(buffer)


class Handler(BaseHTTPRequestHandler):
    server_version = "receipt-ledger"
    application = None
    deadline = None

    def setup(self):
        super().setup()
        self.started_at = time.monotonic()
        if self.deadline is not None:
            self.rfile.close()
            self.rfile = io.BufferedReader(_DeadlineReader(
                self.connection, self.timeout, self.started_at + self.deadline))

    def _read_body(self, length):
        """The body, read in pieces so a timeout can say how much arrived."""
        chunks, received = [], 0
        try:
            while received < length:
                piece = self.rfile.read1(length - received)
                if not piece:
                    break
                chunks.append(piece)
                received += len(piece)
        except socket.timeout:
            raise _BodyTimeout(received) from None
        return b"".join(chunks)

    def send_response(self, code, message=None):
        # Every response - ours, and the standard library's `send_error` - starts
        # here, after the last read of the request. The read side leaves the
        # socket timeout at whatever was left of the deadline, which can be a
        # fraction of a second, so the response is written under the idle
        # timeout instead: CPython's `sendall` counts that timeout across the
        # whole write, so the entire response must be taken within it.
        self.connection.settimeout(self.timeout)
        super().send_response(code, message)

    def _content_length(self):
        """The declared body length, or a LedgerError naming what is wrong with it.

        `int()` on this header was unguarded, and the header is whatever the
        caller wrote. `Content-Length: abc` raised ValueError out of the handler,
        so the request was answered with NOTHING - the connection was closed with
        no status line at all - and `Content-Length: -1` was worse: it is truthy,
        so `rfile.read(-1)` read until the socket closed and the request hung.
        Both land on `POST /v1/receipts/{id}/attach-block`, the call an agent
        makes to have its XNO settlement recorded, where "retry this" and "your
        block was refused" have to be told apart - and neither could be, because
        there was no reply to read. `node.NodeError` exists for exactly this
        reason one layer down; this is the same rule at the socket.
        """
        declared = self.headers.get("Content-Length")
        if declared is None:
            return 0
        text = declared.strip()
        if not text:
            return 0
        if not text.isascii() or not text.isdigit():
            raise LedgerError(
                400, "bad_content_length",
                "Content-Length must be a non-negative number of bytes, got %r" % declared)
        length = int(text)
        if length > MAX_BODY_BYTES:
            raise LedgerError(
                413, "body_too_large",
                "Content-Length declares %d bytes; this ledger reads at most %d"
                % (length, MAX_BODY_BYTES))
        return length

    def _dispatch(self, method):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        try:
            length = self._content_length()
        except LedgerError as exc:
            return self._send(exc.status, exc.body())
        try:
            raw = self._read_body(length) if length else b""
        except _BodyTimeout as exc:
            self.close_connection = True
            return self._send(408, {
                "error": "request_timeout",
                "message": "the request body did not arrive in time (%d of %d bytes after %.1f s)"
                           % (exc.received, length, time.monotonic() - self.started_at)})
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError: a body nested too deeply for the parser is not
            # JSON this service can read, and on 3.10 and 3.11 it raised past
            # this guard and dropped the connection.
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
        body = _encode(payload)
        if body is None:
            # The answer itself could not be written as JSON. That used to raise
            # out of the handler and close the connection with no status line,
            # so the caller could not tell a refusal from a crash. Say so instead.
            return self._send_unencodable()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_unencodable(self):
        body = _encode({
            "error": "unencodable_response",
            "message": "the answer to this request could not be written; "
                       "nothing about it is reported here",
        })
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        body = _encode_text(text)
        if body is None:
            return self._send_unencodable()
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


def _positive_seconds(name, value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("%s must be a finite number of seconds above zero, got %r" % (name, value))
    return value


class _BodyTimeout(Exception):
    def __init__(self, received):
        super().__init__(received)
        self.received = received


def make_server(application, host="127.0.0.1", port=0,
                request_timeout=None, request_deadline=None):
    """`request_timeout` and `request_deadline` default to the module constants.

    Both must be a finite number of seconds above zero: a timeout of 0 would make
    the socket non-blocking, and a deadline of 0 or less would either switch the
    deadline off or drop every connection.
    """
    request_timeout = _positive_seconds(
        "request_timeout", REQUEST_TIMEOUT_SECONDS if request_timeout is None else request_timeout)
    request_deadline = _positive_seconds(
        "request_deadline", REQUEST_DEADLINE_SECONDS if request_deadline is None else request_deadline)
    handler = type("BoundHandler", (Handler,), {
        "application": application,
        "timeout": request_timeout,
        "deadline": request_deadline,
    })
    return ThreadingHTTPServer((host, port), handler)
