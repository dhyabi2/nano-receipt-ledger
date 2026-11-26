"""`python3 -m nano_receipt_ledger` runs the server, as the console script does."""

import sys

from .cli import main

sys.exit(main())
