# nano-receipt-ledger — audit 2026-10-08

Previous audits 2026-10-04 and 2026-10-05, both of which changed nothing.
Audited at `4510e3e`. Lens: can an agent prove it was paid in XNO with this,
today, without being hurt?

**Nothing is changed on the paying path by this pass.** Two findings are recorded
below and deliberately not fixed; the reasoning for each is given so a later run
does not re-litigate it. This file is the whole result.

## Checked

- `python3 -m unittest discover -s tests` — **71 passed**, the count the README
  states. `python3 e2e_check.py` — **23/23**. `py_compile` over `ledger/`,
  `serve.py`, `tests/` and `survey/` — clean.
- **The README quickstart, run as written, against a real server on a real
  socket** — not read, and not through the test harness. `serve.py` came up on
  the four documented flags with the token from the environment only; step 1
  (`POST /v1/receipts`, the README's exact JSON body including the example
  address) answered **201** with `block_hash: null, confirmed: false`; step 3's
  public index answered **200** with no credentials, and the `Accept: text/plain`
  form rendered in **7 lines**, inside the documented 20. So a new agent's first
  two steps work. Step 2 (`attach-block`) needs a live node this environment
  cannot reach and is covered against the repository's stub by `e2e_check.py`.
- **The refusals an outside caller actually meets**, driven over HTTP: a POST
  with no token → `401 unauthorized`; `?limit=abc` → `400 bad_limit`; `DELETE`
  on a receipt → `405 append_only` (by the explicit rule, not by a missing
  handler).
- `ledger/app.py` end to end: every route is in `ROUTES` with its own
  `needs_auth`, `_authorise` fails closed when the token is unset (`not
  self.token or not tokens_match(...)`), `tokens_match` is `hmac.compare_digest`
  over bytes and returns `False` for a non-string rather than raising, and
  `log_message` is silenced so a token cannot reach a request log.
- `ledger/core.py` end to end. `attach_block`'s legs all fail closed, in an
  order that matters: hash shape, already-attached, **block reused across
  receipts (scanned before the node is consulted)**, found, confirmed, subtype
  `send`, destination compared as a **public key** via `account_key` so the
  `xrb_`/`nano_` spellings of one account settle, and amount compared as an
  **integer** via `raw_amount`, which returns `None` rather than letting `int()`
  raise out of a money comparison. A `None` on either side of either comparison
  still refuses.
- `supersede` adds and never erases: the block hash, amount and counterparty are
  untouched, a correction row is appended, and a pending receipt cannot be
  superseded.
- **Money is integers throughout.** `parse_xno` refuses more than six decimal
  places at the door rather than publishing a number that is not the number
  paid; `format_xno` refuses a raw amount it cannot render exactly instead of
  rounding it; `raw_to_xno` is integer `divmod`. No `float(`, `round(`, `1e…` or
  `Decimal` touches an amount in `ledger/` or `serve.py`.
- `ledger/core.py`'s import list is still `re`, `secrets`, `nanoaddr`, `errors`,
  `money` — no socket, no signer, no wallet — and `test_ledger_cannot_move_money`
  still asserts it. `ledger/node.py` issues `block_info` and refuses any other
  action by name.
- Secret scan of the tree and of `git log -p`: none found. The write token is
  read from `RECEIPT_LEDGER_TOKEN` and there is no `--token` flag, so it cannot
  land in a shell history or a process list.

## Found, NOT fixed — recorded for a person to decide

**1. `node.normalise` reads a state block only, so a legacy send block refuses
and the row is then stuck for good.** `ledger/node.py:67-74` reads

```python
"subtype": answer.get("subtype"),
"destination": contents.get("link_as_account"),
```

A **pre-state (legacy) send block** names neither: its destination is
`contents.destination` and its operation is `contents.type == "send"`, with no
top-level `subtype`. Measured by driving `normalise` with a legacy send block's
shape:

```
legacy send -> {'found': True, 'confirmed': True, 'subtype': None,
                'destination': None, 'amount_raw': '1000000000000000000000000'}
state  send -> {'found': True, 'confirmed': True, 'subtype': 'send',
                'destination': 'nano_3seller…', 'amount_raw': '1000000000000000000000000'}
```

So `attach_block` would answer `409 block_mismatch` on **both** the `subtype` and
`destination` legs for a real confirmed payment of exactly the right amount to
exactly the right account — and by `account_key`'s own argument in this
repository, there is no way back: *"attach_block will not confirm it and
supersede refuses a receipt that carries no block, so the row is stuck for
good."* Two siblings read both shapes for exactly this reason
(`nano-settlement-verify._paid_account` and `_operation`,
`nano-invoice._resolve_send`), so this module is the outlier.

**Not fixed, on purpose, for two reasons.** First, the exposure is narrow in a
way that matters: state blocks replaced legacy blocks in 2018, so any payment an
agent makes *today* is a state block and normalises correctly. A legacy send
could only be a pre-2018 block, which is not how anyone pays for agent work.
Second, and decisively for a routine: the fix **widens what is accepted** — it
makes a block attachable that is refused today — so it is outside the standing
"only adds a refusal" merge grant and is not a routine's to land. If it is
wanted, the shape is `contents.get("link_as_account") or contents.get("destination")`
and `answer.get("subtype") or (contents.get("type") if it names an operation)`,
with the `_OPERATIONS` membership test `nano-settlement-verify` uses so that
`"state"` is never read as a send.

**2. A request with a non-numeric `Content-Length` gets no HTTP response at
all.** `ledger/app.py:218` is `length = int(self.headers.get("Content-Length") or 0)`,
outside the `try` that answers `LedgerError`, so the `ValueError` escapes
`_dispatch` and the caller's connection closes with nothing on it. Measured on a
raw socket against the running server:

```
POST /v1/receipts HTTP/1.1  +  Content-Length: abc   ->   <empty>
```

This is the same failure shape this repository has already named and fixed twice
on the node path — *"the caller's connection was closed with no HTTP
response"* — in the one place it was not applied. **Not fixed** because no
client an agent would use sends a non-numeric `Content-Length`: it is reachable
only by hand, costs nothing but one closed connection, and moves no money. It is
a one-line `try`/`except (TypeError, ValueError)` → `400 bad_request` whenever
somebody is in this file anyway, and it belongs in the audit rather than in a
pull request of its own.

## Could not verify

- **No live Nano node.** The network policy here reaches none, so `attach-block`
  was exercised only against the repository's stub and against `normalise`
  directly. No XNO moved, and `normalise` has still not been checked against a
  real node's `block_info` wire shape on any run.
- **The receipt's strongest claim is the one a stranger cannot check.**
  `render()` tells the reader *"We could not have produced it without holding
  the sending key"*, but `attach_block` never checks the block's **sender**, and
  the receipt does not record it — so a block that paid the right account the
  right amount settles a receipt whoever sent it. Only a token holder (the
  operator) can attach, so this is the operator's own record of its own
  payments, not an opening for an outsider; but the sentence promises more than
  the data supports. Recording the payer would be an addition rather than a
  defect fix, so it is named here and not built.
- `survey/` is a table of what other agents said about their own ledgers;
  re-deriving those claims is research, not this repository's XNO payment path.
  Its generated-file guard (`tests/test_survey.py` rebuilds `README.md` and
  fails on a difference) passes.
