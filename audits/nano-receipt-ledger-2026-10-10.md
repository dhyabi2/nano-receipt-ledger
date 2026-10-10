# nano-receipt-ledger - audit 2026-10-10

One finding, fixed in this pull request. The money path itself is unchanged.

## Checked

- `python3 -m unittest discover -s tests` and `python3 e2e_check.py` on `main`
  before anything was touched: **71 passed**, **23/23 e2e checks pass**.
- `ledger/money.py` end to end. `xno_to_raw` / `raw_to_xno` are integer-only;
  `parse_xno` refuses more than six decimal places at the door and
  `format_xno` refuses any raw it cannot render exactly, so every amount the
  ledger stores is a multiple of 10**24 raw and `totals()` sums integers of
  raw before rendering once. No float reaches an amount anywhere in the
  package (`grep -n "float\|/ 10\|\*\* 0.5"` is clean on the money path).
- `ledger/core.py` `attach_block`, the one place a block becomes a settlement:
  hash shape, already-attached, cross-receipt reuse, `found`, `confirmed`,
  `subtype == "send"`, destination compared **by public key** (so the `xrb_`
  and `nano_` spellings of one account are one account), and amount compared
  as integers of raw with a non-numeric value refused rather than crashing.
  The reuse scan runs before the node is consulted, and no check can be
  skipped by a value the caller chooses.
- `ledger/core.py`'s import list, which still holds nothing that could sign or
  send; `tests/test_11_ledger_cannot_move_money` asserts that statically.
- `ledger/store.py`: the ledger file is written to a temporary file, fsynced
  and `os.replace`d, so a crash mid-write leaves the previous ledger intact.
- `ledger/node.py`: `block_info` is the whole node interface, and transport
  and non-JSON failures arrive as one `NodeError` that the HTTP layer turns
  into `503 node_unavailable` rather than a dropped connection.
- README claims against the tree: the advertised counts match a real run
  (`ReadmeCounts` enforces this), the error table matches the codes raised,
  and the documented `curl` line is executed verbatim in `e2e_check.py`
  check 14.

## Found and fixed

**`Handler._dispatch` parsed `Content-Length` with a bare `int()`, so two
header values a caller controls got no HTTP response at all.**
`ledger/app.py:233` (before this change):

    length = int(self.headers.get("Content-Length") or 0)

- `Content-Length: abc` raised `ValueError` out of `do_POST`. `socketserver`
  logged a traceback and closed the socket, so the caller read **zero bytes** -
  no status line, no body.
- `Content-Length: -1` is truthy, so `self.rfile.read(-1)` read until the
  socket closed: the request **never returned** and the worker thread stayed
  parked on it.

Both land on `POST /v1/receipts/{id}/attach-block`, the call an agent makes to
have its XNO settlement recorded. This repository already treats "the caller
could not tell retry from refused" as a defect one layer down - that is what
`node.NodeError` and `503 node_unavailable` exist for - and the same rule was
missing at the socket. There was also no cap on a declared body length.

The fix adds `Handler._content_length`, which refuses a non-numeric or
negative header with `400 bad_content_length` and a declared length above
`MAX_BODY_BYTES` (1 MB) with `413 body_too_large`, answered before the body is
read. It only ever adds a refusal: no amount, destination, block check or
stored field is touched, and a well-formed request takes exactly the path it
took before.

Four tests in `tests/test_ledger.py::AMalformedContentLength` drive a raw
socket, because `urllib` computes `Content-Length` itself and will not send
either value. Against `main`'s `app.py` they are 2 failures and 1 error; with
the fix, 4 pass.

Suite after the change: **75 passed**, **23/23 e2e checks pass**,
`python3 -m compileall` clean. README's advertised unit-test count was moved
from 71 to 75 (a test asserts the two agree) and the two new refusals were
added to the error list.

## Could not verify

- Nothing here talks to mainnet; the node is a loopback stub in both suites.
  The destination and amount a block carries are taken from the node's
  `block_info` answer and are **not** re-derived from the block's own fields,
  so a lying node is still outside what this service checks. That is the
  documented design (`ledger/node.py`'s docstring, README's "Fetch this block
  from any public Nano node") and the caller is expected to supply a node it
  trusts; `nano-accept-settle` is the repository that recomputes the hash.
  Noted, not changed - it is a design boundary, not a defect.
- `Content-Length` larger than the body actually sent still blocks until the
  client disconnects, as HTTP requires; the new cap bounds how much can be
  claimed, which is the part that was unbounded.
