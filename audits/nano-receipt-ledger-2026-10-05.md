# nano-receipt-ledger — audit 2026-10-05

Read through the one question that matters here: can an agent prove it was paid in XNO
with this, today, without being hurt? Nothing is changed by this pass.

## Checked

- `python3 -m unittest discover -s tests` — 71 tests green (the count README states).
  `python3 -m pytest -q` agrees: 71 passed, 7 subtests.
- `python3 e2e_check.py` — 23/23 checks pass.
- `python3 -m py_compile` over `ledger/`, `serve.py`, `tests/`, `survey/` — clean.
- **The README quickstart was run, not just read.** The server came up on the documented
  flags (`--port --store --node --base-url`, token from the environment only), step 1
  created a receipt, step 3 read it back with no credentials, and the `Accept: text/plain`
  form rendered in 7 lines. The example address in the README,
  `nano_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz`, passes checksum
  and decodes to public key `000102…1E1F` — so a new agent's first POST succeeds rather
  than returning `invalid_address`. Step 2 (`attach-block`) needs a live node this
  environment cannot reach; `e2e_check.py` covers it against a stub.
- **Money is integers throughout.** `grep` for `float(`, `round(`, `1e…`, `Decimal` and
  division across `ledger/` and `serve.py` finds nothing on an amount — the only hit is
  the word "Decimal" in a docstring. `money.xno_to_raw` parses digits and multiplies by
  `10**30`; `raw_to_xno` uses integer `divmod`; `parse_xno` refuses more than six decimal
  places at the door rather than publishing a number that is not the number paid; and
  `format_xno` refuses a raw amount it cannot render exactly instead of rounding it.
- **`attach_block` fails closed on every leg.** Subtype must be `send` (a confirmed
  receive on our own chain pays nobody); the destination is compared as a *public key*
  via `account_key`, not as a string, so the `xrb_`/`nano_` spellings of one account
  settle rather than deadlocking a row; the amount is compared as an integer through
  `raw_amount`, which returns `None` — a refusal — for anything non-ASCII-digit rather
  than letting `int()` raise out of a money comparison. A `None` on either side of the
  destination or amount comparison still refuses.
- **One block pays one receipt.** The `block_reused` scan runs before the node is
  consulted, and both sides are upper case by `BLOCK_HASH_RE` at the door, so a replayed
  hash cannot settle a second receipt.
- **The ledger cannot move money, by construction.** `ledger/core.py` imports only `re`,
  `secrets`, `nanoaddr`, `errors` and `money`; `node.py` issues `block_info` and refuses
  any other action by name, with no send, `block_create`, wallet action or signing
  present — absent, not disabled. The block facts `core` checks come from the node the
  *service* asked, never from the request body, so nothing here trusts what the payer
  submitted.
- **`nanoaddr`**: the pad-bit check (`body[0] not in "13"`) is correct and load-bearing —
  '1' and '3' are alphabet indices 0 and 1, so it is exactly the constraint that the four
  pad bits are zero, which also keeps `to_bytes(33)` from overflowing. The checksum is
  blake2b-5 of the key, reversed, base32-encoded, per spec.
- Secrets: none in the tree. The write token is read from `RECEIPT_LEDGER_TOKEN` and
  nowhere else — there is no `--token` flag, so it cannot reach a shell history or a
  process list — compared with `hmac.compare_digest`, and `log_message` is silenced so a
  request log cannot capture it.

## Found, not fixed

Two observations, neither reachable by a well-formed client, both recorded rather than
patched because a pull request for either would be noise:

1. **A malformed `Content-Length` gets no HTTP response at all.**
   `ledger/app.py:233`, `int(self.headers.get("Content-Length") or 0)`, sits *outside*
   the `try` that converts failures into a response. Measured against the running server:

   ```
   POST /v1/receipts  with  Content-Length: abc
   -> (connection closed, no response)
   ```

   This is the same shape as the defect #2 fixed for the node path — the comment at
   `app.py:124-131` says in as many words that a caller closed with no response is not
   acceptable on this endpoint. A negative length would likewise reach
   `rfile.read(-1)` and block that connection's thread until the client hangs up
   (one thread only; `ThreadingHTTPServer` keeps serving). No agent using curl or
   `requests` sends either header, so nothing is hurt today.

2. **A pre-state send block cannot settle a receipt.** `node.normalise` reads the payee
   from `contents.link_as_account` only, where a pre-state send block carries it as
   `contents.destination`. Such a block normalises to `destination: None` and is refused
   as a `block_mismatch`. That is fail-closed, and Nano has not produced a pre-state
   block since the v16 epoch, so no payment an agent makes today takes this path.

## Could not verify

- No live Nano node was reached — the network policy here does not allow one — so the
  `attach-block` leg was exercised only against the repository's own stub, and
  `normalise` has not been checked against a real node's `block_info` wire shape on this
  run.
- `survey/` was read but not audited: it is a table of what other agents said about their
  own ledgers, and re-deriving those claims is research, not this repository's XNO
  payment path. Its generated-file guard (`tests/test_survey.py` rebuilds `README.md` and
  fails on a difference) passes.
