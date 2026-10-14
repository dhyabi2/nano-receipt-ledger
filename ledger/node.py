"""The one place this service talks to a Nano node, and it only ever reads.

`block_info` is the whole interface. There is no send, no block_create, no
wallet action and no signing here - not disabled, absent. The ledger core
does not import this module at all; the HTTP layer is handed a node object
and passes its *answer* to the core.
"""

import http.client
import json
import urllib.request

# The most this client reads of one node reply. A block_info answer is a few
# kilobytes; one mebibyte is generous and still bounded.
MAX_REPLY_BYTES = 1024 * 1024


class NodeError(Exception):
    """This client could not get an answer out of the node.

    Everything the network can do to a read - refuse the connection, time
    out, hang up mid-body, answer with an HTML error page a JSON parser
    chokes on - arrives here under one name, so the caller has one thing to
    catch. It says the node did not answer, never that a block is bad: the
    two are different answers and only one of them means "do not retry".
    """


def _opener():
    """An opener that speaks http and https and nothing else.

    `build_opener` also installs ftp, file and data handlers, so a node that
    redirected to `ftp://` or `file://` was followed there. Built from the
    standard handlers without those, a redirect to any other scheme reaches
    `UnknownHandler`, which raises URLError at once: the same 503 hold.
    Proxies, certificate verification and timeouts behave as with `urlopen`.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPRedirectHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class NanoNode:
    def __init__(self, url, timeout=10):
        """`timeout` is in seconds and bounds each connect attempt and each
        read, as `urlopen` does; it is not a deadline for the whole RPC."""
        self.url = url
        self.timeout = timeout

    def rpc(self, payload):
        """POST a read-only RPC payload and return the node's JSON answer."""
        action = payload.get("action")
        if action != "block_info":
            raise NodeError(
                "this client issues only block_info; %r was asked for" % action)
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # A node on the far end of a socket fails in ways `normalise` below
        # cannot describe, and none of them were caught: a refused connection
        # raised URLError straight through the HTTP layer, which answers only
        # LedgerError, and the caller's connection was closed with no response
        # at all. OSError covers the transport (URLError and socket timeouts
        # are both OSError); ValueError covers a body that is not the JSON we
        # asked for (JSONDecodeError and UnicodeDecodeError are both
        # ValueError). Deliberately not `except Exception`: a wrong *action*
        # still raises NodeError above, loudly, and nothing else here should
        # be turned into "the node is down".
        #
        # Two more ways a reply escaped that guard. A deeply nested body
        # (`[[[[...]]]]`) makes `json.loads` raise RecursionError, which is
        # neither OSError nor ValueError, and the caller's connection was
        # dropped again. And `response.read()` had no bound, so an endpoint
        # could stream an arbitrarily large body into memory. A block_info
        # answer is a few kilobytes; anything over MAX_REPLY_BYTES is refused
        # unread past the limit. RecursionError is named, not `Exception`,
        # for the reason given above.
        #
        # http.client.HTTPException is the last family. It is not an OSError:
        # a status line that is not HTTP (BadStatusLine), a status code that is
        # not a number, a header line over 64 KiB (LineTooLong), more than 100
        # headers, a chunk size that is not hex and a chunked body cut short
        # (IncompleteRead) all raised it straight past this guard, and each one
        # dropped the caller's connection. They are a reply that could not be
        # read, so they are the same 503 hold.
        try:
            with _opener().open(request, timeout=self.timeout) as response:
                body = response.read(MAX_REPLY_BYTES + 1)
            if len(body) > MAX_REPLY_BYTES:
                raise NodeError(
                    "%s answered %s with more than %d bytes; a block_info reply is "
                    "never that large, so it was not read"
                    % (self.url, action, MAX_REPLY_BYTES))
            return json.loads(body.decode("utf-8"))
        except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # an error reply holds its connection open otherwise
            raise NodeError("%s did not answer %s: %s" % (self.url, action, exc)) from None

    def block_info(self, block_hash):
        return normalise(self.rpc(
            {"action": "block_info", "json_block": "true", "hash": block_hash}))


def _shown(value, width=200):
    """The node's own error text, clipped, for a message the caller reads."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= width else text[:width] + "..."


def normalise(answer):
    """Reduce a node's block_info answer to the five facts the ledger checks.

    Raises NodeError for an answer that is not a JSON object at all, for one
    that names a block whose `contents` cannot be read, and for a node `error`
    that does not itself say the block was not found. `block_info` is asked with `json_block: "true"`; an endpoint that
    ignores the flag answers `contents` as an opaque JSON *string*, and
    `"...".get("link_as_account")` is an AttributeError - raised outside
    `rpc`'s own guard, so it went past the HTTP layer, which answers only
    LedgerError, and closed the caller's connection with no response. A public
    endpoint silently dropping a request flag is not hypothetical: the sibling
    repository records `rpc.nano.to` doing exactly that with `raw: "true"` on
    `account_history`.

    It is a NodeError and not a mismatch because nothing was read: where the
    money went is unknown, not wrong. Reporting it as a wrong destination would
    accuse a buyer who really had paid, and `attach_block` cannot be retried
    into a different verdict - a mismatch is the end of that receipt. 503
    `node_unavailable` tells the caller to ask again, which is the truth.
    """
    if not isinstance(answer, dict):
        # Valid JSON that is not an object at all - a proxy's `[]`, a bare
        # string, a number, `null`. This used to be folded into the
        # not-found answer below, so the caller was told "the node does not
        # know block X" (404 block_not_found) although NOTHING about the
        # block was read. That is the same wrong verdict the `contents`
        # case above argues against, one step earlier: unknown reported as
        # a fact.
        #
        # The 2026-10-11 audit left the `error` FIELD as a known limitation
        # on the grounds that telling a real not-found from a transient
        # refusal needs each endpoint's error strings. That reasoning never
        # reached here - a reply that is not an object carries no error
        # string to interpret - and it no longer holds below either: one
        # string does it, so the two cases now answer alike.
        raise NodeError(
            "the endpoint's reply is %s, not a JSON object, so it is not a "
            "node's block_info answer and says nothing about this block"
            % type(answer).__name__)
    if "error" in answer:
        # Only an error that SAYS the block is absent is evidence of absence.
        # Every other thing a node can answer - a rate limit, an RPC it will
        # not serve, a hash it would not parse - says nothing about the block,
        # and folding them all into the not-found reply told the caller "the
        # node does not know block X" (404 block_not_found) when nothing had
        # been read. That is the same "unknown reported as a fact" the
        # non-object guard above exists to prevent; the 2026-10-11 note left it
        # because telling the two apart was thought to need each endpoint's
        # error strings. It needs one: `block_info` answers "Block not found",
        # and the sibling repository nano-settlement-verify already splits on
        # exactly that (`nano_claim.py`, `_Unknown` for everything else).
        #
        # The verdicts are not interchangeable to the caller. This is the read
        # behind the endpoint an agent calls to prove its XNO settlement: a 404
        # says the payment does not exist and there is nothing to retry, while
        # an unreadable node is the 503 hold that says ask again. Holding is
        # also the safe direction - it never turns an unpaid receipt into a
        # settled one, only a false "you never paid" into "ask again".
        if "not found" in str(answer["error"]).lower():
            return {"found": False, "confirmed": False, "subtype": None,
                    "destination": None, "amount_raw": None}
        raise NodeError(
            "the node answered an error (%s) rather than this block, so it was "
            "not read and says nothing about this block" % _shown(answer["error"]))
    contents = answer.get("contents")
    if not isinstance(contents, dict):
        raise NodeError(
            "the reply names a block but its contents are %s, not an object, so the "
            "account it paid cannot be read; nothing was judged"
            % type(contents).__name__)
    return {
        "found": True,
        "confirmed": str(answer.get("confirmed", "false")).lower() == "true",
        "subtype": answer.get("subtype"),
        "destination": contents.get("link_as_account"),
        "amount_raw": answer.get("amount"),
    }
