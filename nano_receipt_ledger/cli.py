"""Run the receipt ledger.

    RECEIPT_LEDGER_TOKEN=... nano-receipt-ledger --port 8080 \
        --store data/ledger.json --node http://127.0.0.1:7076 \
        --base-url https://receipts.example.org

The write token comes from the environment and from nowhere else: there is
no --token flag, so it cannot end up in a shell history, a process list, or
a systemd unit that somebody commits.
"""

import argparse
import math
import os
import sys

from . import __version__
from .app import (Application, MAX_TIMEOUT_SECONDS,
                  REQUEST_DEADLINE_SECONDS, REQUEST_TIMEOUT_SECONDS, make_server)
from .node import NanoNode
from .store import Store


def positive_seconds(text):
    """argparse type: a finite number of seconds above zero, at most MAX_TIMEOUT_SECONDS."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number of seconds" % text) from None
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError("%r must be a finite number of seconds above zero "
                                         "and at most %d" % (text, MAX_TIMEOUT_SECONDS))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nano-receipt-ledger",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="data/ledger.json")
    parser.add_argument("--node", default=None, help="Nano node RPC URL, read-only")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--request-timeout", type=positive_seconds,
                        default=REQUEST_TIMEOUT_SECONDS,
                        help="seconds one read may wait for the next bytes, and the whole "
                             "response must be written within (default %(default)s)")
    parser.add_argument("--request-deadline", type=positive_seconds,
                        default=REQUEST_DEADLINE_SECONDS,
                        help="seconds the request line, headers and body may take "
                             "to arrive in all (default %(default)s)")
    args = parser.parse_args(argv)

    token = os.environ.get("RECEIPT_LEDGER_TOKEN", "")
    if not token:
        print("RECEIPT_LEDGER_TOKEN is not set: every write endpoint will answer 401.",
              file=sys.stderr)

    application = Application(
        store=Store(args.store),
        node=NanoNode(args.node) if args.node else None,
        token=token,
        base_url=args.base_url,
        node_rpc_url=args.node or "https://<any-public-nano-node>",
    )
    server = make_server(application, args.host, args.port,
                         request_timeout=args.request_timeout,
                         request_deadline=args.request_deadline)
    host, port = server.server_address[:2]
    print("receipt ledger on http://%s:%d  (reads are public, writes need the token)"
          % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
