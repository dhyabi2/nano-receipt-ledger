"""The one place this service talks to a Nano node, and it only ever reads.

`block_info` is the whole interface. There is no send, no block_create, no
wallet action and no signing here - not disabled, absent. The ledger core
does not import this module at all; the HTTP layer is handed a node object
and passes its *answer* to the core.
"""

import json
import urllib.request


class NodeError(Exception):
    """This client could not get an answer out of the node.

    Everything the network can do to a read - refuse the connection, time
    out, hang up mid-body, answer with an HTML error page a JSON parser
    chokes on - arrives here under one name, so the caller has one thing to
    catch. It says the node did not answer, never that a block is bad: the
    two are different answers and only one of them means "do not retry".
    """


class NanoNode:
    def __init__(self, url, timeout=10):
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise NodeError("%s did not answer %s: %s" % (self.url, action, exc)) from None

    def block_info(self, block_hash):
        return normalise(self.rpc(
            {"action": "block_info", "json_block": "true", "hash": block_hash}))


def normalise(answer):
    """Reduce a node's block_info answer to the five facts the ledger checks.

    Raises NodeError for an answer that names a block whose `contents` cannot
    be read. `block_info` is asked with `json_block: "true"`; an endpoint that
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
    if not isinstance(answer, dict) or "error" in answer:
        return {"found": False, "confirmed": False, "subtype": None,
                "destination": None, "amount_raw": None}
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
