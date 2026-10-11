# nano-receipt-ledger — audit 2026-10-11

Previous audit 2026-10-10. Audited at `009f4de` (`main` after #8).
Lens: can an agent get PAID in XNO and have it recorded with this today without
being hurt — recorded as paid when it was not, refused when it was paid, or
dropped mid-request.

Baseline on `009f4de`: `python3 -m pytest -q` **75 passed, 7 subtests**,
`python3 e2e_check.py` **23/23**. On this branch: **78 passed, 12 subtests**,
**23/23**, `python3 -m compileall ledger serve.py` clean.

## What I checked

- `ledger/money.py` end to end: `xno_to_raw`, `raw_to_xno`, `parse_xno`,
  `format_xno`. Integers throughout; no float on an amount anywhere in the
  repository.
- `ledger/core.py`: `create`'s address and amount refusals, `attach_block`'s
  five checks (block reused, found, confirmed, subtype, destination, amount),
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
  and after: this adds a hold and takes no verdict away.

README's advertised unit-test count moved 75 → 78 (a test asserts the two
agree), and the refusal list now states the 503 hold, which it did not mention
at all.

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
  turns a crash into the documented retry and changes no verdict.
- The amount and destination are still taken from the node's answer rather than
  re-derived from the block's own fields, so a lying node remains outside what
  this service checks. That is the documented design boundary
  (`ledger/node.py`'s docstring, README's "Fetch this block from any public Nano
  node"); `nano-accept-settle` is the repository that recomputes the hash.
  Unchanged, noted again.
