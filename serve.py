#!/usr/bin/env python3
"""Run the receipt ledger from a checkout, without installing it.

    RECEIPT_LEDGER_TOKEN=... python3 serve.py --port 8080 \
        --store data/ledger.json --node http://127.0.0.1:7076 \
        --base-url https://receipts.example.org

This is a thin wrapper kept so older instructions still work. The server
lives in `nano_receipt_ledger/cli.py`; once installed, the same flags are
taken by the `nano-receipt-ledger` console script.
"""

import sys

from nano_receipt_ledger.cli import main, positive_seconds  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
