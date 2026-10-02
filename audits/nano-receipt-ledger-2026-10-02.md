# nano-receipt-ledger — code audit, 2026-10-02

Clone of `main` at `0bfd69c`. Baseline, before any change:

```
$ python3 -m unittest discover -s tests   # Ran 53 tests - OK
$ python3 e2e_check.py                    # 23/23 checks pass
```

Both counts match what `README.md:111-112` advertises. This run read the whole XNO path —
`money.py`, `nanoaddr.py`, `core.py`, `node.py`, `store.py`, `app.py`, `serve.py` — and
drove the HTTP surface directly with adversarial amounts, limits and node replies.

## Nothing was changed

No defect was found that hurts an agent being paid in XNO today. The three things worth
recording are below; none of them is reachable through the API by a caller, and the
judgement on each is to leave the code alone rather than widen this repository on a
hypothesis.

## Found, NOT fixed — recorded for a person

**A store row this service did not write can kill the public index with no response at
all.** `money.format_xno` (`ledger/money.py:77-89`) raises `AmountError` for a raw amount
that is not a whole number of 10⁻⁶ XNO. `Ledger.totals` (`ledger/core.py:296`) calls it on
the sum of every confirmed receipt, `AmountError` is a `ValueError` and **not** a
`LedgerError`, and `Handler._dispatch` (`ledger/app.py:233-236`) catches only
`LedgerError` — so it escapes and the caller's connection is closed with no HTTP response,
with nothing in a log either (`log_message` is suppressed on purpose, `app.py:275`). Driven
with one confirmed row of `amount_raw: "1"` in the store:

```
GET /v1/receipts  ->  *** AmountError: 1 raw cannot be rendered exactly at 6 decimal places
```

That is the same shape as the defect fixed in #2 (a configured-but-down node "answered
nothing at all"), and it takes out the one endpoint a counterparty agent reads to check it
was paid.

**Not reachable through the API**, which is why nothing was changed: `parse_xno`
(`money.py:57-75`) refuses more than six decimal places at the door, so every row this
service writes is a whole multiple of 10²⁴ raw and every sum of them is too — confirmed by
reading `git log` over `money.py`, where the six-place cap has been there since the first
commit (`2f3a2c3`), so no ledger file this service ever wrote can hold such a row. It needs
a row written by hand or by another tool, which is a plausible operational foot-gun — a
maintainer backfilling history would naturally paste the node's raw amount, and most raw
amounts are not whole micro-XNO — but it is not something an agent can trigger. If it is
worth closing, the honest fix is at the door (refuse a store file carrying a row this
ledger cannot publish, at load, where the operator sees it) rather than a broad `except`
in the HTTP layer, which this repository is right to avoid.

**A pre-state (legacy) send block is refused even when it paid exactly the right
account.** `node.normalise` (`ledger/node.py:66`) reads the destination only from
`contents.link_as_account`, which a node sends for a **state** block; for a pre-state send
it sends `contents.destination`. Driven with a legacy send reply that pays the receipt's
account for the receipt's amount:

```
409 block_mismatch  {"subtype": {"expected":"send","got":null},
                     "destination": {"expected":"nano_1113...","got":null}}
```

The sibling verifier handles both shapes (`nano-settlement-verify/_paid_account` reads
`link_as_account` *or* `destination`). Left alone deliberately: every Nano block produced
since the state-block epoch is a state block, so a payment an agent makes today cannot hit
this, and the failure is a refusal — fail-closed — not a false settlement. Worth a line in
`normalise` only if a historical payment ever needs attaching.

**Non-ASCII decimal digits are accepted in `amount_xno`.** `money.xno_to_raw`
(`money.py:19-32`) gates on `str.isdigit()` without the `isascii()` that
`core.raw_amount` (`core.py:60-73`) is careful to require and documents. So
`{"amount_xno": "١"}` (Arabic-Indic one) creates a receipt, as does `"0.٥"`:

```
amount '١'    -> 201  raw=1000000000000000000000000000000  xno=1.000000
amount '0.٥'  -> 201  raw=500000000000000000000000000000   xno=0.500000
```

Lenient, but **not a wrong amount**: the value is converted exactly and published back in
ASCII, and the stored `amount_raw` is what the node is then held to. The `isdigit`-but-not-
`isdecimal` characters that would break `int()` (`"²"`) raise `ValueError` inside
`xno_to_raw` and come back as a clean `400 amount_out_of_range`. Nothing changed: there is
no defect to show, and `money.py` is vendored verbatim from `swarm-decisions`
`tools/nano_wallet/wallet.py` (its docstring says so), so a divergence here would be worse
than the leniency.

## Checked, nothing to fix

- **Verification does not trust the payer.** `attach_block` (`core.py:131-200`) refuses a
  block the node does not know, an unconfirmed one, anything whose `subtype` is not
  `send`, a destination that is not the receipt's counterparty, an amount that differs by
  one raw, and a block already attached to another receipt (`block_reused` — what stops
  one payment being shown as two). `block_info` is the node's answer, passed in; the core
  never asks a node anything and imports no socket and no signer, which
  `test_ledger_cannot_move_money` asserts against the import list.
- **Accounts are compared as accounts.** `core.account_key` compares decoded public keys,
  so the `nano_`/`xrb_` spellings of one account settle; a stranger is refused in either
  spelling (e2e checks 17 and 18). A `None` on either side still refuses.
- **`nanoaddr.py` is a correct Nano codec.** Checked the pad-bit rule (only `1`/`3` can
  lead, so the decoded value is always < 2²⁵⁶ and `to_bytes(33)[1:]` is exact), the
  alphabet (no `0`, `2`, `l`, `v`), and the blake2b-5 reversed-digest checksum. The
  address the README publishes in its quickstart validates.
- **Amount arithmetic is integer raw throughout.** No `float()` anywhere on an amount;
  `totals` sums `int(r["amount_raw"])`; pending receipts are never folded into
  `paid_xno_total`.
- **The write token.** Read from the environment only (there is no `--token` flag),
  compared with `hmac.compare_digest`, and every write route 401s when it is unset —
  fail-closed. Request logging is suppressed so it cannot land in a log; e2e check 15
  confirms it is nowhere in the store on disk.
- **The store is replaced wholesale** via `NamedTemporaryFile` + `fsync` + `os.replace`,
  so a crash mid-write leaves the previous ledger intact, and writes are serialised under
  an `RLock` the handlers all take.
- `PUT`/`PATCH`/`DELETE` answer `405 append_only` by an explicit rule, not by a missing
  handler. `limit` outside 1..100, a non-integer `limit`, an unknown `kind` and a cursor
  not in the result set are all clean 400s. The README's refusal list matches the code.

## Not verified here

No live Nano node and no live payment: `e2e_check.py` runs `serve.py` as a subprocess
against a loopback stub node, which is what passed. The explorer URL
(`https://nanolooker.com/block/`) and the published `curl` line were not fetched over the
network this run; e2e check 14 verifies the curl line we publish returns what the receipt
says against the stub.

## Secrets

Clean. No credential, key or seed in the tree or in `git log -p`. The ledger holds public
addresses and block hashes only, and the token never enters it.
