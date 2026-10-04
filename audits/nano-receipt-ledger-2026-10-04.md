# nano-receipt-ledger — audit 2026-10-04

Lens: can an agent prove it got paid in XNO with this, today, without being hurt?
Previous audits: 2026-09-27, 2026-10-01, 2026-10-02. **Nothing to change this run.**

## Checked

- The commands `.github/workflows/test.yml` runs: `python3 -m unittest discover -s tests`
  (**53 passed**), `python3 e2e_check.py` (**23/23 checks pass**), and the two secret-scan grep
  steps. Green.
- **The README's documented flow, run for real** against `serve.py` on a live port with a
  generated token, using the README's own request bodies verbatim: create a pending receipt,
  read the index anonymously, read `/v1/custody`. All three answer as documented. The amount
  round-trip is exact — `"amount_xno":"0.050000"` is recorded as
  `"amount_raw":"50000000000000000000000000000"`, which is 0.05 × 10**30 to the digit.
- `attach_block` (`ledger/core.py:143-210`) end to end — every check and the order they run in.
- Where the block facts come from. `ledger/app.py:133` calls `self.node.block_info(block_hash)`:
  the request body supplies **only** the hash, and every fact compared against the receipt is
  the node's. Nothing is taken from the submitter.
- `ledger/node.py:normalise`, field by field.
- `ledger/money.py` in full, plus 16 adversarial amount strings through `parse_xno`.
- Authentication: `hmac.compare_digest` on the write token (`ledger/app.py:48`), read endpoints
  unauthenticated by design, token read from the environment only.
- Concurrency on the one check-and-set that matters.
- Float search across `serve.py` and all of `ledger/`: **no `float(`, no `/ 10**`, no `round(`**
  anywhere.

## Found

Nothing worth changing.

## Checked and clean

- **The replay hole that `nano-settlement-verify` leaves to the caller is closed here.**
  `attach_block` scans every stored receipt and refuses `409 block_reused` — "one block pays one
  receipt". That is the check that stops one payment being displayed as two, and it is the
  thing this repository adds over bare settlement verification.
- **The string-truthiness trap is avoided.** A node answers `confirmed` as the *string*
  `"false"`, and `not "false"` is `False` — so a naive truthiness test would read an unconfirmed
  block as confirmed and settle a payment that had not happened. `ledger/node.py:71` converts
  properly: `str(answer.get("confirmed", "false")).lower() == "true"`, defaulting to unconfirmed
  when the field is absent. Checked because this is the most common way code of this shape
  accepts a payment it should refuse.
- **Check-and-set is atomic.** The server is a `ThreadingHTTPServer` (`ledger/app.py:14`), so
  two concurrent `attach-block` calls naming one block could otherwise both pass the
  `block_reused` scan. `store.lock` is a `threading.RLock` (`ledger/store.py:18`) and
  `attach_block` holds it from `self.get(receipt_id)` through `self.store.flush()` — the scan,
  the decision and the write are one critical section.
- **A receive cannot pass as a payment**: `subtype != "send"` is a mismatch, with a comment
  recording that the check exists because a sibling repository shipped without it.
- **Mismatches are reported all at once and refuse all-or-nothing** — subtype, destination and
  amount are collected into one `409 block_mismatch` whose `detail` names each field, so a
  receipt can never carry a block that disagrees with it on any axis.
- **A `None` on either side of a comparison still refuses.** A block with no destination, or an
  unparseable amount, is rejected rather than compared loosely.
- **A node that cannot be reached is distinguished from a block that was refused**
  (`ledger/app.py:130-137`): "not checked" is not reported as "refused", which is the
  distinction that decides whether a payer is told to retry or told their payment is bad.
- **`ledger/core.py` still cannot move money.** Its imports are `re`, `secrets`, `nanoaddr`,
  `errors`, `money` — no socket, no signer, no wallet — and a test asserts that import list and
  fails if it grows. Re-confirmed by reading the file, not only by the README's claim.
- **The secret-scan CI steps are deliberately narrow and the reasoning is recorded in the
  workflow**: the broad forms flagged the repository's own README (which *generates* a token
  rather than committing one) and left CI red for 30 hours. `tests/test_ledger.py` runs the step
  against the tracked tree so a false positive fails the suite. No secret in the tree.
- **Non-ASCII digits in `amount_xno` are accepted, and that is not a defect here.** `xno_to_raw`
  guards with `str.isdigit()`, which is true for every Unicode decimal digit, so `"١"` (Arabic-
  Indic one) and `"１"` (fullwidth one) both parse. This is the hole that `agent-wallet-multirail`
  added an `isascii()` guard for and that the `nano-mcp-public` 09-30 note describes as
  "accepts what a JavaScript `/^\d+$/` peer refuses". Measured here, it has no consequence:
  Python's `int()` converts those digits to the *correct* value, the write path is token-gated so
  the string is not attacker-supplied, and the receipt republishes
  `"amount_xno": format_xno(amount_raw)` (`ledger/core.py:129`) — derived from the integer and
  always ASCII. So no stranger-verifiable field ever carries a non-ASCII digit and no amount is
  misread. Recorded rather than changed, so a later run does not re-open it.
- Also refused correctly: `"1e3"`, `"+1"`, `"1_0"`, `"0x10"`, `"nan"`, `"inf"`, `""`, `"0"`,
  a negative amount, and more than six decimal places (because the ledger will not publish a
  number that is not the number paid).

## Could not verify

- **No live Nano node was reachable from this session**, so `attach_block` was exercised against
  `normalise`'s output from recorded replies and through `e2e_check.py`'s stub, not against a
  real `block_info` answer. The field names `normalise` reads (`confirmed`, `subtype`,
  `contents.link_as_account`, `amount`) are from the node RPC's documented response. The
  previous audits record the same limit.
- **Pre-state (legacy) send blocks would be refused**, because `normalise` reads only
  `contents.link_as_account` and does not fall back to `contents.destination` as
  `nano-settlement-verify` does. This fails *closed* — a legitimate payment refused, never a bad
  one accepted — and no newly created block can be a pre-state block, so it is noted rather than
  reported as a defect. It would matter only for attaching a block minted before the state-block
  era.
- Node independence and the honesty of any single node's answer are outside this repository, as
  the previous audits record.
