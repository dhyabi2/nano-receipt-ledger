# nano-receipt-ledger — audit 2026-10-11

Previous audit 2026-10-10. Audited at `009f4de` (`main` after #8).
Lens: can an agent get PAID in XNO and have it recorded with this today without
being hurt — recorded as paid when it was not, refused when it was paid, or
dropped mid-request.

Baseline on `009f4de`: `python3 -m pytest -q` **75 passed, 7 subtests**,
`python3 e2e_check.py` **23/23**. On this branch after the first fix: **78
passed, 12 subtests**, **23/23**, `python3 -m compileall ledger serve.py` clean.
After the follow-up fixes below: `python3 -m unittest discover -s tests` **83
tests OK**, `python3 e2e_check.py` **23/23**.

## What I checked

- `ledger/money.py` end to end: `xno_to_raw`, `raw_to_xno`, `parse_xno`,
  `format_xno`. Integers throughout; no float on an amount anywhere in the
  repository.
- `ledger/core.py`: `create`'s address and amount refusals, `attach_block`'s
  six checks (block reused, found, confirmed, subtype, destination, amount),
  `supersede`, and `totals` (confirmed only, counterparties counted by account
  key rather than by spelling).
- `ledger/node.py` end to end, and `ledger/app.py`'s handling of what it raises.
- `raw_amount`'s ASCII-digit guard and `account_key`'s two spellings.

## Found and fixed

**A node that ignores `json_block` closed the caller's connection with no
response at all.** `ledger/node.py` asks `block_info` with `json_block: "true"`.
An endpoint that honours the flag answers `contents` as an object; one that
ignores it answers `contents` as an opaque JSON **string**, and `normalise` ran

```python
contents = answer.get("contents") or {}
...
"destination": contents.get("link_as_account"),
```

→ `AttributeError: 'str' object has no attribute 'get'`. Shown directly:

```
$ python3 -c '... normalise({"confirmed":"true","subtype":"send","amount":"1",
                             "contents":"{\"link_as_account\": \"nano_1...\"}"})'
RAISED AttributeError: 'str' object has no attribute 'get'
```

That raise is outside `rpc`'s own `except (OSError, ValueError)`, and `app.py`
catches only `NodeError` around `self.node.block_info(...)` (`app.py:139-142`)
while `Handler._dispatch` answers only `LedgerError`. So it reached
`BaseHTTPRequestHandler` and the agent calling
`POST /v1/receipts/{id}/attach-block` — the one call it makes to have its XNO
settlement recorded — got its connection closed with no status and no body.

This is the identical shape as the defect #5 fixed for a refused connection and
a proxy's HTML page, in a reply that is valid JSON and therefore survives both
of those guards. A public endpoint silently dropping a request flag is not
hypothetical: `nano-settlement-verify`'s `nano_claim.py:186-188` records
`rpc.nano.to` ignoring `raw: "true"` on `account_history`, and that repository
shipped the same `contents`-as-a-string fix on 2026-10-10 (#23).

Fixed in `normalise`: a reply that names a block whose `contents` is not an
object raises `NodeError`, which `app.py`'s existing arm turns into
`503 node_unavailable` — and **nothing is written**, so the receipt stays
pending and the retry the message promises is safe (asserted).

It is deliberately a hold and not a `409 block_mismatch`. Nothing was read, so
where the money went is unknown, not wrong — and a mismatch is terminal for that
receipt (`attach_block` cannot be retried into a different verdict, and
`supersede` refuses a receipt carrying no block), so reporting it would strand a
buyer who really had paid. `ledger/node.py`'s own docstring already states the
rule: `NodeError` "says the node did not answer, never that a block is bad".

Three tests, failing on `009f4de`:

- `test_a_node_that_ignores_json_block_is_a_503_not_a_dropped_connection` —
  drives the real `NanoNode` against a real socket serving `contents` as a
  string. Before: `AttributeError` out of the handler. After: `503
  node_unavailable`, receipt still pending.
- `test_normalise_holds_on_a_reply_whose_contents_cannot_be_read` — the unit
  form, over a string, `None`, an int, a list and `"null"` (5 subtests, all
  failing before).
- `test_normalise_still_reads_every_answer_it_read_before` — the control, field
  for field, for a node-error answer and a well-formed one. Passes both before
  and after.

**This does change one verdict, and the change is deliberate.** On `009f4de`
`normalise` ran `answer.get("contents") or {}`, so a reply whose `contents` was
missing, `null` or `[]` read as a block with no destination: `409
block_unconfirmed` if the reply did not say confirmed, otherwise `409
block_mismatch` on the destination. Both are terminal for that receipt. Those
replies now get the `503 node_unavailable` hold instead. That is the safer
answer: nothing about where the money went was read, so a retryable hold cannot
strand a buyer who paid, and it still records nothing as paid.

README's advertised unit-test count moved 75 → 78 (a test asserts the two
agree), and the refusal list now states the 503 hold, which it did not mention
at all.

## Found in review and fixed on this branch

An adversarial review found the same bug class — a node reply that raises
something nothing catches, so the caller's connection is dropped — still
reachable three more ways. All three are fixed, each with a test that drives
the real ledger HTTP server against a real stub node and fails on `3a00741`:

- **An amount past Python's integer-digit limit.** `raw_amount` promised
  "nothing here raises", but `int("9" * 5000)` raises ValueError (4300 digits is
  the default limit), out of `attach_block`. It now refuses anything with more
  significant digits than `2**128 - 1` (39), strips leading zeros before
  `int()` so padding still reads, and returns None for a value above that cap.
  The existing refusal path answers `409 block_mismatch` on the amount; nothing
  is recorded. The refusal's `detail` no longer echoes a 5000-character value.
  (`test_an_amount_past_the_integer_digit_limit_is_refused_not_dropped`,
  `test_raw_amount_never_raises_on_a_long_or_oversized_amount`.) The same limit
  reached `Ledger.create`: an `amount_xno` of 4290 digits parsed, then
  `str(raw)` raised. `parse_xno` now refuses anything above `2**128 - 1` raw
  as `400 amount_out_of_range`
  (`test_an_absurd_amount_xno_is_a_400_not_a_dropped_connection`).
- **A deeply nested reply.** `json.loads("[" * 200000 + "]" * 200000)` raises
  RecursionError, which is neither OSError nor ValueError, so it escaped `rpc`.
  It is now caught as `NodeError` → `503 node_unavailable`
  (`test_a_deeply_nested_reply_is_a_503_not_a_dropped_connection`).
- **An unbounded read.** `response.read()` had no limit. `rpc` now reads at
  most 1 MiB + 1 byte and refuses a longer reply as `NodeError` → 503. A
  block_info answer is a few kilobytes. The test pads an otherwise matching,
  confirmed reply past the limit; on `3a00741` that receipt was confirmed
  (`test_a_reply_over_the_size_limit_is_refused_unread`).

The 503 message said the node "could not be reached" even when it had answered
something unreadable; it now says either case. Status and error code are
unchanged.

### Known limitation, left as is

A reply that is readable JSON carrying an `error` field — `Block not found`, but
also a rate limit or any other transient refusal from a public endpoint — is
still read as "not found" and answered `404 block_not_found`. It fails closed
(nothing is written and nothing is recorded as paid, so the caller can call
again), but the code does not tell the caller the refusal may be transient.
Telling a real not-found from a transient error needs the error strings of each
endpoint, which this audit did not collect. The README now says so rather than
listing every unreadable answer under the 503 hold.

## Also checked, found clean

- **No float on an amount.** `money.py` is integer-only by construction;
  `format_xno` refuses a raw amount it cannot render exactly rather than
  rounding it.
- **Nothing accepted on the payer's word.** The amount, destination, subtype and
  confirmation all come from the node's `block_info`; the caller supplies only a
  block hash, and one block pays one receipt (`block_reused`).
- **The ledger cannot move money**, still asserted against `core.py`'s import
  list by `test_ledger_cannot_move_money`, and `NanoNode.rpc` still refuses every
  action but `block_info`.
- No secret in the tree or in `git log -p` over the ledger package; no
  `subprocess`, `os.system`, `eval`, `exec` or `shell=` outside the test suite.

## Could not verify

- Nothing here talks to mainnet; the node is a loopback stub in both suites, so
  no XNO moved in this run.
- **Whether a real endpoint in the wild serves `contents` as a string on
  `block_info`.** That `rpc.nano.to` drops `raw` on `account_history` is
  first-hand in the sibling repository; that some endpoint drops `json_block` is
  inferred from the same class of behaviour. The fix is safe either way — it
  turns a crash into the documented retry, and the only verdict it changes is
  the terminal 409 for an unreadable `contents`, which becomes the same retry.
- The amount and destination are still taken from the node's answer rather than
  re-derived from the block's own fields, so a lying node remains outside what
  this service checks. That is the documented design boundary
  (`ledger/node.py`'s docstring, README's "Fetch this block from any public Nano
  node"); `nano-accept-settle` is the repository that recomputes the hash.
  Unchanged, noted again.
