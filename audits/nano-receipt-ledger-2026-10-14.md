# nano-receipt-ledger — audit, 2026-10-14

Lens: can an agent get paid in XNO and prove it settled, today, without being hurt?

## Checked

- `python3 -m unittest discover -s tests` — 151 passing before any change.
- `python3 e2e_check.py` — 23/23 before any change.
- The settlement path end to end: `serve.py` → `ledger/app.py` routing and
  `_attach` → `ledger/node.py` (`rpc`, `normalise`) → `ledger/core.py`
  (`attach_block`, `raw_amount`, `account_key`) → `ledger/store.py`.
- `ledger/money.py` in full: `xno_to_raw`, `raw_to_xno`, `parse_xno`,
  `format_xno`. Integer-only, as the module claims. No float reaches an
  amount; ASCII-digit-only at the door, `MAX_RAW` bounded both ways, leading
  zeros stripped before `int()` so Python's 4300-digit limit cannot be reached
  through `amount_xno`. Nothing to fix.
- `attach_block`'s refusals: block-hash shape, one-block-one-receipt reuse,
  not-found, unconfirmed, and the three mismatch checks (`subtype == "send"`,
  destination, amount). All three mismatch checks refuse on a `None` on either
  side. Nothing to fix.
- `ledger/core.py`'s import list, which the README makes a claim about, and the
  test that pins it. Still `re`, `secrets`, `nanoaddr`, `errors`, `money` — no
  socket, no signer, no wallet.
- The CI secret guard, run locally over the tree: clean.

## Found

**`ledger/node.py:151` — every node `error` was reported as "block not found".**
`normalise` folded any `{"error": ...}` answer into the not-found reply, so a
node answering `{"error": "Too many requests"}`, `{"error": "RPC control is
disabled"}` or `{"error": "Bad hash number"}` became `404 block_not_found` out
of `core.attach_block` — "the node does not know block X" — although nothing
about the block had been read.

The two verdicts are not interchangeable to the caller. This is the endpoint an
agent calls to prove its XNO settlement: a 404 says the payment does not exist
and there is nothing to retry, so an agent that really did pay is told it did
not, and the honest answers to that are to abandon a settled payment or to pay
again. The truth — the node could not be read — is the retryable `503
node_unavailable` hold the module already returns for a reply that is not a JSON
object and for one whose `contents` cannot be read.

The module's own comment for that non-object guard makes exactly this argument
("unknown reported as a fact") and names the `error` field as a deliberate
known limitation, on the grounds that telling a real not-found from a transient
refusal needs each endpoint's error strings. It needs one string: `block_info`
answers `"Block not found"`, and the sibling repository
`nano-settlement-verify` already splits on precisely that
(`nano_claim.py:159-165` — `"not found"` in the error is `_NotFound`, every
other error is `_Unknown`). A test asserted the old behaviour as settled policy
(`test_a_node_error_field_is_still_a_not_found_and_not_a_hold`, with
`{"error": "Too many requests"}` as its own example), so this was category 3 as
well: a test asserting the wrong thing.

## Fixed

Only `"Block not found"` — the whole string, case and surrounding space
ignored — is read as a not-found; every other error, and a non-string error,
raises `NodeError` and becomes the 503 hold. The node's own error text is
echoed in the message, clipped to 200 characters so an endpoint cannot choose
an unbounded one.

This only narrows what the service will call a verdict. It cannot turn an
unpaid receipt into a settled one — a hold writes nothing and `attach_block`
still refuses everything it refused before. It converts a confident false "you
never paid" into "ask again".

Tests: `ANodeErrorThatIsNotANotFound` — the transient strings hold,
`"Block not found"`/`"block not found"` still read as not-found, a rate-limited
read is 503 over the real HTTP server with nothing recorded, and a genuine
not-found is still 404. 6 failures before the change, all passing after. The
test asserting the old policy is replaced by one asserting the split, and the
two stale comments calling the error field a known limitation now say it is
closed.

Suite after: **160 unit tests, 23/23 end-to-end.** The README's count law
updated 151 → 160 in the same change.

### Two corrections to the above, from the review of this branch (2026-10-15)

The first version of this fix was wrong in two ways, both found by the
independent review of pull request #13 and fixed in `2f60c36` on the same
branch. Recorded here rather than quietly amended, because the second was a
regression this fix introduced:

1. **A malformed `block_hash` reached the node before `attach_block` validated
   it, and this change turned that into a 503.** `_attach` passed any string
   through to `block_info`; the node answers `"Bad hash number"`, which on
   `main` folded into a not-found, so `attach_block`'s own `BLOCK_HASH_RE` check
   then answered `400 bad_block_hash`. Once a non-not-found error became a hold,
   that same request was told to **retry a hash that can never succeed**. I read
   that ordering during this audit and judged it "one wasted node round trip,
   not worth a change" — without noticing that this very fix changed what the
   round trip returns. `_attach` now makes the refusals it can make without the
   node (400 malformed hash, 409 already attached) before asking it, in
   `attach_block`'s order.

2. **The not-found match was a substring, so it was too loose.** `"not found" in
   error.lower()` also matched a gateway's `"Not Found"`, an endpoint's `"Method
   not found"` and `"Account not found"` — none of which is this node saying it
   does not know this block. My own test asserted `"Account not found"` as a
   not-found, and the section above cited the sibling repository's substring
   rule as justification. That rule is right where `nano-settlement-verify` uses
   it (`account_info`, `blocks_info`, where an account's absence is meaningful)
   and wrong here: for `block_info` the only answer that is evidence of absence
   is `"Block not found"`. Now matched whole.

A third, cosmetic: the new test class was appended below
`if __name__ == "__main__"`, so it ran under `unittest discover` (which CI and
the README both use) but not under `python3 tests/test_ledger.py`. Moved
above it.

## Could not verify

- The real error strings of the public endpoints this would be pointed at. The
  discriminator is Nano's own node wording, `"Block not found"`, matched as
  the whole string with case and surrounding space ignored. An endpoint that
  signals a genuine absence in some other wording is a 503 hold rather than a
  404 — the safe direction, and a retry costs one more read.
- Nothing on this path was exercised against a live node; the node is a stub
  throughout the suite and the e2e script, by design (`ledger/core.py` is
  asserted to import no socket).
