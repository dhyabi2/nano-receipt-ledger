# nano-receipt-ledger — audit, 2026-09-27

First audit of this repository. Read in full: `ledger/core.py`, `ledger/money.py`,
`ledger/store.py`, `ledger/app.py`, `ledger/node.py`, `ledger/custody.py`, the
README, and the test bootstrap.

## What was checked, and how

Baseline before any change, all green: `python3 -m pytest -q` (48 tests,
7 subtests), `python3 -m unittest discover -s tests` (48), and
`python3 e2e_check.py` (23/23).

**Secrets, tree and full history.** Scanned every commit reachable from every
ref for 64-hex values, token-shaped assignments and the usual provider key
prefixes. Nothing found — not one candidate, in the tree or in history. This
repository also runs its CI secret guard from inside its own suite
(`SecretGuard`), which is the right place for it.

**`core.py` cannot move money, and the test that says so is real.**
`test_11_ledger_cannot_move_money` AST-parses the module's import list and
asserts it is exactly `{re, secrets, nanoaddr, errors, money}`. That is a
structural claim rather than a policy note, and it holds.

**The money path is integer-only.** `parse_xno` refuses non-strings, negatives,
`"1e3"`, `"Inf"`, `"NaN"`, `"1_0"` (which `int()` would otherwise accept as 10),
whitespace-separated digits and anything past six decimal places, and
`format_xno` refuses a raw amount it cannot render exactly. No float is reachable.

**`attach_block` compares accounts and amounts, not spellings.** `account_key`
compares decoded public keys, so the `xrb_` and `nano_` forms of one account
match; `raw_amount` compares integers, so a zero-padded amount from a node
matches, and it guards `isdigit()` with `isascii()` because `"²".isdigit()` is
True while `int("²")` raises. Both were fixed before this audit and both are
sound. One block pays one receipt, enforced by the `block_reused` check.

**Node client reads only.** `NanoNode.rpc` refuses any action but `block_info`,
and `normalise` reads `confirmed`, `subtype` and `amount` from the response top
level and `link_as_account` from `contents` — which is where a Nano node puts
them. Correct.

**Authentication.** Writes require `X-Ledger-Token` through
`hmac.compare_digest`; an unset token does not mean open writes
(`test_10b`); the request logger is disabled so a token cannot reach a log.

**Fuzzed the HTTP surface for uncaught exceptions.** 60-odd hostile inputs
across `POST /v1/receipts`, `attach-block`, `supersede`, the index query string
and seven odd paths under four methods: non-dict bodies, `None`, lists, wrong
types for every field, Unicode-digit amounts, a 40-digit amount, `limit` as
`"abc"`, `"-1"`, `"1e3"` and a 24-digit integer, and `%2e%2e` in a receipt id.
**Every one came back as a `LedgerError` with a real status; zero uncaught
exceptions.** Nothing here can turn user input into a 500.

## Fixed

**The README advertised test counts that were both wrong** (`README.md:111-112`).
It offered the reader

```
python3 -m unittest discover -s tests -v   # 34 unit tests
python3 e2e_check.py                       # 21 end-to-end checks
```

against a suite that reports **48** and **23**. Nothing is broken by it, but the
first two numbers a reader can check for themselves were wrong, in a repository
whose entire argument is that its claims can be checked rather than believed —
and a reader who runs both commands has no way to tell which of the two numbers
is the stale one.

Corrected, and pinned so it cannot drift again: `ReadmeCounts` counts the suite
with `unittest.defaultTestLoader.discover` — literally what the README's own
command does — and counts the end-to-end checks from `e2e_check.py`'s own
`check(...)` call sites. Counting rather than restating is the move
`guard_script` already makes with the workflow: only one of the two numbers is
written down, so they cannot disagree.

The end-to-end count is taken statically, by AST, rather than from a run,
because the unit suite deliberately opens no socket and starts no subprocess.
The same test asserts that no `check(...)` sits inside a loop — if one ever
does, the call sites stop being the count and the test says so rather than
quietly measuring the wrong thing.

Both tests fail against the stale README (`AssertionError: 34 != 50` and
`AssertionError: 21 != 23`) and pass after. Suites after the change: 50 unit
tests + 7 subtests, and 23/23 end-to-end.

## Found, not fixed — needs an owner decision

**The README sends a public reader to a private repository.** `README.md:120`
credits `ledger/nanoaddr.py` and `ledger/money.py` to
[dhyabi2/swarm-decisions](https://github.com/dhyabi2/swarm-decisions), and
`ledger/money.py:6` names the same repository as "the source of truth" for both
functions. That repository is **private**, so the link is a 404 for everyone the
README is written for, and the provenance it offers cannot be followed up.

Left alone because the two honest fixes pull in opposite directions — say the
source is private and keep the credit, or drop the link — and which one is right
depends on whether that repository is ever meant to be public. It is also not
this repository's problem alone: the identical link was recorded in
`paid-work-queue`'s audit on 2026-09-27 and is still there, so it wants one
decision applied across the account rather than a different guess per repo.

## Found, not fixed — too small to be worth a commit

- `money.xno_to_raw` guards its digits with `isdigit()` alone, without the
  `isascii()` that `core.raw_amount` documents and applies for exactly this
  reason. It is contained today: the only caller is `parse_xno`, which catches
  the `ValueError` that `int("²")` raises and turns it into a 400. Worth
  knowing if that function is ever called from somewhere new, since it is
  vendored code that other repositories copy.
- `Application._custody` concatenates `self.base_url` without the `rstrip("/")`
  that `render` applies, so a base URL with a trailing slash yields
  `//v1/receipts` in the custody document only.
- `Store.flush` leaves its temporary file behind if `json.dump` raises. The
  ledger itself is safe — `os.replace` is never reached, so a partial file is
  never promoted.
- `core.RECEIPT_ID_RE` and `money.raw_to_xno` are unused.
