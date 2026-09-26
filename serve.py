#!/usr/bin/env python3
"""Run the receipt ledger.

    RECEIPT_LEDGER_TOKEN=... python3 serve.py --port 8080 \
        --store data/ledger.json --node http://127.0.0.1:7076 \
        --base-url https://receipts.example.org

The write token comes from the environment and from nowhere else: there is
no --token flag, so it cannot end up in a shell history, a process list, or
a systemd unit that somebody commits.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger"))

from app import Application, make_server  # noqa: E402
from node import NanoNode  # noqa: E402
from store import Store  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="data/ledger.json")
    parser.add_argument("--node", default=None, help="Nano node RPC URL, read-only")
    parser.add_argument("--base-url", default="")
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
    server = make_server(application, args.host, args.port)
    host, port = server.server_address[:2]
    print("receipt ledger on http://%s:%d  (reads are public, writes need the token)"
          % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
