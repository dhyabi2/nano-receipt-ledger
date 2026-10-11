"""The one place this service talks to a Nano node, and it only ever reads.

`block_info` is the whole interface. There is no send, no block_create, no
wallet action and no signing here - not disabled, absent. The ledger core
does not import this module at all; the HTTP layer is handed a node object
and passes its *answer* to the core.
"""

import http.client
import json
import socket
import ssl
import threading
import time
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


class _Deadline:
    """Cuts every connection one RPC opened once its time is up.

    A socket timeout bounds each read, not the reply: a node that trickles
    eight bytes a second never lets one read time out, and could hold an
    attach request, and the server thread answering it, for as long
    as it liked. When `expire` runs, each connection's socket is shut down, so
    whatever read is in progress returns at once and the RPC fails as a reply
    that could not be read. A connection that finishes connecting after that
    is shut down as soon as it exists.

    A socket is watched from the moment it is created, before it connects:
    an HTTPS proxy's CONNECT reply is read on that socket and used to run
    before it was watched, so a proxy dripping its reply held the RPC past
    the deadline. The TLS socket that takes the connection over is watched
    before its handshake starts, not after it ends: `wrap_socket` detaches
    the plain socket, so shutting that one no longer reaches the connection,
    and a server dripping its ServerHello held the RPC for about one more
    timeout.

    As a second bound, every socket operation that waits - each connect
    attempt, the handshake and each read - is given a socket timeout of the
    per-read timeout or the time that is left, whichever is smaller, so
    nothing waits past the deadline even before the shutdown arrives.
    """

    def __init__(self, timeout):
        self.timeout = timeout
        self.expired = False
        self._at = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._sockets = []

    def remaining(self):
        return self._at - time.monotonic()

    def passed(self):
        """True once the timer has run or the time is up, whichever came
        first: a socket timeout capped at the time left can end a read a
        moment before the timer fires."""
        return self.expired or self.remaining() <= 0

    def watch(self, sock):
        with self._lock:
            self._sockets.append(sock)
            if self.expired:
                _shut(sock)

    def expire(self):
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                _shut(sock)

    def release(self):
        """Close every connection the RPC opened, once it is over.

        A connection is otherwise closed only when the garbage collector gets
        to it: an error reply (a 500, or a redirect whose body was cut at the
        deadline) is held by the exception that reported it, and a socket the
        collector reached before the file object reading it was reported as
        never closed. Nothing reads from these sockets after the RPC, so the
        descriptor is closed outright, even while a file object still holds a
        reference to it."""
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            try:
                sock.close()
                sock._real_close()
            except OSError:
                pass

    def wait_for(self, per_read):
        """The socket timeout for the next wait: `per_read` (None meaning
        no per-read bound) or the time left, whichever is smaller. Once no
        time is left the deadline is expired here, so the RPC reports it in
        the same words as one the timer ended, and socket.timeout is raised
        without anything being waited for."""
        left = self.remaining()
        if self.expired or left <= 0:
            self.expire()
            raise socket.timeout(
                "the deadline of %s seconds passed before this step" % self.timeout)
        return left if per_read is None else min(per_read, left)

    def create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                          source_address=None, *args, **kwargs):
        """`socket.create_connection`, with each socket watched before it
        connects and each connect attempt bounded by the time that is left.

        Like `socket.create_connection`, every address the name resolves to is
        tried in turn: a socket that cannot be opened (an address family the
        host does not support, as IPv6 on a host with it turned off) or cannot
        connect is closed and the next address is tried, and the last error is
        raised only when none connected. The deadline is checked before each
        attempt, before any socket is opened for it.

        The name lookup (`getaddrinfo`) is the one step not bounded: it cannot
        be interrupted without a thread of its own, and runs for as long as the
        system resolver takes.
        """
        host, port = address
        per_read = (socket.getdefaulttimeout()
                    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout)
        error = None
        for family, kind, proto, _, sockaddr in socket.getaddrinfo(
                host, port, 0, socket.SOCK_STREAM):
            wait = self.wait_for(per_read)
            sock = None
            try:
                sock = _PlainSocket(family, kind, proto)
                self.watch(sock)
                sock.settimeout(wait)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                sock.bound_by(self, per_read)
                return sock
            except OSError as exc:
                error = exc
                if sock is not None:
                    sock.close()
        try:
            raise error if error is not None else OSError(
                "getaddrinfo returned nothing for %r" % (host,))
        finally:
            error = None  # as socket.create_connection does: no reference cycle


class _Bounded:
    """Sets the socket timeout to `_Deadline.wait_for` before each read."""

    _deadline = None
    _per_read = None

    def bound_by(self, deadline, per_read):
        self._deadline, self._per_read = deadline, per_read
        self.settimeout(deadline.wait_for(per_read))

    def _rebound(self):
        if self._deadline is not None:
            self.settimeout(self._deadline.wait_for(self._per_read))

    def recv(self, *args, **kwargs):
        self._rebound()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._rebound()
        return super().recv_into(*args, **kwargs)


class _PlainSocket(_Bounded, socket.socket):
    pass


class _TLSSocket(_Bounded, ssl.SSLSocket):
    pass


class _BoundedContext:
    """An SSLContext whose `wrap_socket` watches the TLS socket and bounds its
    handshake before the handshake starts. Everything else is the context's."""

    def __init__(self, context, deadline):
        # Set on this instance only. The context is this RPC's own: urllib and
        # http.client make one per handler or per connection when, as here,
        # none is passed in.
        context.sslsocket_class = _TLSSocket
        self._context = context
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._context, name)

    def wrap_socket(self, sock, *args, **kwargs):
        handshake = kwargs.pop("do_handshake_on_connect", True)
        per_read = sock._per_read if isinstance(sock, _Bounded) else sock.gettimeout()
        tls = self._context.wrap_socket(
            sock, *args, do_handshake_on_connect=False, **kwargs)
        try:
            self._deadline.watch(tls)
            tls.bound_by(self._deadline, per_read)
            if handshake:
                tls.do_handshake()
        except BaseException:
            tls.close()
            raise
        return tls


def _shut(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed, which is what was wanted


def _watched(connection_class, deadline):
    class Watched(connection_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = deadline.create_connection
            context = getattr(self, "_context", None)
            if context is not None:
                # HTTPS: the TLS socket that takes over the watched plain
                # one is watched, and bounded, before its handshake.
                self._context = _BoundedContext(context, deadline)
    return Watched


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def do_open(self, http_class, req, **kwargs):
        return super().do_open(_watched(http_class, self.deadline), req, **kwargs)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def do_open(self, http_class, req, **kwargs):
        return super().do_open(_watched(http_class, self.deadline), req, **kwargs)


def _opener(deadline):
    """An opener that speaks http and https and nothing else.

    `build_opener` also installs ftp, file and data handlers, so a node that
    redirected to `ftp://` was followed over a connection this module does not
    watch: a slow FTP banner held the RPC for as long as it dripped. Built by
    hand, a redirect to any other scheme reaches `UnknownHandler`, which
    raises URLError at once, and that is the same 503 hold.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    _HTTPHandler(deadline), _HTTPSHandler(deadline),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPRedirectHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class NanoNode:
    def __init__(self, url, timeout=10):
        """`timeout` is in seconds and bounds the whole RPC: each connect, an
        HTTPS proxy's CONNECT reply, the TLS handshake, the status line, the
        headers and the body together, across redirects, not only each read.
        The host name lookup is the one step it does not bound."""
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
        #
        # And `timeout` bounded each socket read, not the reply, so a node
        # sending a few bytes a second held the request for as long as it
        # kept sending. `_Deadline` shuts the socket down once `timeout`
        # seconds have passed in total; a reply not finished by then is the
        # same 503 hold, even if what had arrived happened to parse.
        deadline = _Deadline(self.timeout)
        opener = _opener(deadline)
        timer = threading.Timer(self.timeout, deadline.expire)
        timer.daemon = True
        late = NodeError(
            "%s did not finish answering %s within %s seconds; a reply still "
            "arriving then is not read" % (self.url, action, self.timeout))
        timer.start()
        try:
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_REPLY_BYTES + 1)
            finally:
                timer.cancel()
                deadline.release()
            if deadline.passed():
                raise late
            if len(body) > MAX_REPLY_BYTES:
                raise NodeError(
                    "%s answered %s with more than %d bytes; a block_info reply is "
                    "never that large, so it was not read"
                    % (self.url, action, MAX_REPLY_BYTES))
            return json.loads(body.decode("utf-8"))
        except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
            if deadline.passed():
                raise late from None
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
