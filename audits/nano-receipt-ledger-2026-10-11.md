# nano-receipt-ledger — audit 2026-10-11

Previous audit 2026-10-10. Audited at `009f4de` (`main` after #8).
Lens: can an agent get PAID in XNO and have it recorded with this today without
being hurt — recorded as paid when it was not, refused when it was paid, or
dropped mid-request.

Baseline on `009f4de`: `python3 -m pytest -q` **75 passed, 7 subtests**,
`python3 e2e_check.py` **23/23**. On this branch after the first fix: **78
passed, 12 subtests**, **23/23**, `python3 -m compileall ledger serve.py` clean.
After the follow-up fixes below: `python3 -m unittest discover -s tests` **139
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

## Found in a second review and fixed on this branch

A second adversarial review, run on `4f3d570`, showed the branch's claim — a
node reply that cannot be read is a 503 hold, never a dropped connection — still
did not hold. Each item below has a test over a real socket that fails on
`4f3d570`, unless it says otherwise.

- **Node values echoed unclipped into a 409, and an unguarded response writer.**
  `attach_block` put the node's `subtype` and `link_as_account` into the
  `block_mismatch` detail exactly as sent. Nested 1000 or 5000 lists deep,
  Python 3.12's parser read them, and `json.dumps(..., indent=2)` in
  `Handler._send` then raised RecursionError: the caller's connection was
  dropped. (On 3.10 and 3.11 the parse fails first, which was already the 503
  hold; on 3.13 the encoder copes.) Both are now shown through `_shown_value`:
  None or a string of at most 64 characters as itself, a longer string clipped,
  anything else named by its type. `_send` no longer trusts its payload either:
  an answer that cannot be encoded (RecursionError, a circular reference's
  ValueError, a TypeError) is `500 unencodable_response` with a JSON body.
  (`NodeValuesEchoedIntoARefusal` and `AnAnswerThatCannotBeWritten`. On
  `4f3d570` the four nested-reply tests drop the connection on 3.12, fail on
  3.13 because the detail echoes the nested list unclipped, and pass on 3.10
  and 3.11, where the reply was already a 503.)
- **Replies that are not well-formed HTTP.** `rpc` caught OSError, ValueError
  and RecursionError, but `http.client.HTTPException` is none of them. A garbage
  status line, a status code that is not a number, a header line over 64 KiB,
  more than 100 headers, a chunk size that is not hex and a chunked body cut
  short each dropped the connection. HTTPException is now caught as `NodeError`
  → `503 node_unavailable` (`AReplyThatIsNotHTTP`, written byte for byte on a
  raw socket).
- **`amount_xno` in digits that are not ASCII, and zero padding.**
  `xno_to_raw` checks with `str.isdigit`, which is True for Arabic-Indic and
  fullwidth digits, and `int()` reads them, so `"\u0661"` was accepted as 1 XNO.
  `parse_xno` now refuses any non-ASCII amount as `400 amount_out_of_range`
  before the vendored function is called (the vendored functions are unchanged).
  It also drops leading zeros first: `"0" * 5000 + "1"` raised ValueError past
  Python's integer-digit limit inside `create` and dropped the connection; it now
  reads as 1 XNO, and a whole part longer than any amount under `2**128 - 1` raw
  is refused without being handed to `int()` (`AmountXnoAtTheDoor`).
- **A request body or `linked` nested too deeply.** Not a node reply, but the
  same drop: a request body nested 200000 deep raised RecursionError out of
  `_dispatch` (now `400 bad_json`), and on 3.12 and 3.13 a `linked` nested 1500
  deep parsed, was appended, and then made `Store.flush` raise — the connection
  was dropped and the receipt was in memory but not on disk. `linked` deeper
  than 16 levels is now `400 bad_linked` before anything is written
  (`ADeeplyNestedLinked`).
- **Tests tightened** (these pass on `4f3d570` too; they pin behaviour rather
  than catch a bug): the size-limit test pads with whitespace, so cutting the
  read cannot itself make the body invalid JSON, and asserts both sides — a
  reply of `MAX_REPLY_BYTES + 1` is a 503 and one of exactly `MAX_REPLY_BYTES`
  is read and settles. `raw_amount("0")` and `raw_amount("000")` are 0;
  `parse_xno` accepts `340282366.920938` and refuses `340282366.920939`.

The README's refusal list now names these cases and its unit-test count moved
83 → 107.

### Known limitation, left as is

A reply that is readable JSON carrying an `error` field — `Block not found`, but
also a rate limit or any other transient refusal from a public endpoint — is
still read as "not found" and answered `404 block_not_found`. It fails closed
(nothing is written and nothing is recorded as paid, so the caller can call
again), but the code does not tell the caller the refusal may be transient.
Telling a real not-found from a transient error needs the error strings of each
endpoint, which this audit did not collect. The README now says so rather than
listing every unreadable answer under the 503 hold.

## Found in a third review and fixed on this branch

A third review, run on `c4843ed`, found one regression the second round
introduced and four smaller gaps. Each test named below fails on `c4843ed`
unless it says otherwise.

- **The wrong destination was shown with its last character cut off
  (regression).** The echo limit `MAX_SHOWN` added in the second round was 64
  characters, and a `nano_` address is 65, so every `block_mismatch` that named
  a wrong destination clipped it — on `main` the full address was shown. The
  limit is now 128: any `nano_` or `xrb_` address, amount or subtype is echoed
  exactly, and longer junk is still clipped
  (`AWrongDestinationIsShownWhole`; the `nano_` test fails on `c4843ed`, the
  `xrb_` one, at 64 characters, passed already and pins it).
- **`Accept: text/plain` dropped the connection on a lone surrogate
  (pre-existing).** JSON allows `"\ud800"`, so it reaches the store; `_encode`
  writes it as an escape, but `_send_text` called `text.encode("utf-8")`, which
  raised UnicodeEncodeError. The plain-text form now writes such a character as
  its backslash escape, and `_send_text` falls back to
  `500 unencodable_response` exactly as `_send` does if its text still cannot be
  written (`APlainTextReceiptIsAlwaysAnswered`, over a real socket; both tests
  drop the connection on `c4843ed`).
- **The node timeout bounded each read, not the reply.** A node sending eight
  bytes every quarter second never let one read time out, so it held the attach
  request for as long as it kept sending — in the body or in the headers. `rpc`
  now arms a timer for the whole call; when it fires, the connection's socket
  is shut down, the read in progress returns, and the call is the same
  `503 node_unavailable` hold, even if the part that arrived would have parsed.
  The README says the timeout covers the whole reply (`ANodeThatTrickles`: the
  body and header trickle tests take ten seconds or more on `c4843ed` and fail
  their time bound; a slow reply that finishes in time is still read).
  **Later removed; see "The total deadline was removed" below.**
- **Limits pinned by tests (these pass on `c4843ed`; they kill mutants that
  survived before).** `linked` of exactly 16 levels is stored and 17 is
  `400 bad_linked`, counted both as nested objects and as lists inside the
  object (`TheLinkedDepthLimit`; kills `MAX_LINKED_DEPTH` 15 or 17, `>=` for
  `>`, and starting the count at 0). `MAX_REPLY_BYTES` is asserted to be the
  1 MiB the README states, and a node streaming a body with no end is cut at
  the size limit — the refusal names the limit and arrives long before the
  deadline, so an unbounded read cannot pass by waiting for the timer. All
  eleven mutants tried (the five above, `MAX_SHOWN = 64`, an unbounded read, a
  timer that never starts, a connection the timer cannot see, a plain
  `encode("utf-8")`, and the unquoted amount message) fail the suite.
- **An amount refusal quoted altered text (cosmetic).** After leading zeros
  were dropped, the vendored `xno_to_raw` quoted what it was handed, so
  `" 0x1"` was reported as `'x1'`. `parse_xno` now quotes the caller's own
  input, clipped (`AnAmountErrorQuotesTheCallersText`). The vendored functions
  are unchanged.

The README's unit-test count moved 107 → 120.

## The total deadline was removed

The one-total-deadline transport in `ledger/node.py` (its own
`create_connection`, a `_Deadline` timer, `_PlainSocket`/`_TLSSocket`,
`_BoundedContext` and `release()`) was taken out again. It was added for a low
issue `main` also has — a node trickling bytes can hold a request — and it
produced a regression in three review rounds in a row. The last one: a first
resolved address that silently drops packets used up the whole shared budget,
so the working IPv4 address after it was never tried; `main`, with
`socket.create_connection` and a timeout per attempt, succeeded in about ten
seconds where the branch always answered 503. It was also slower under load
(12 to 20 of 50 concurrent attaches to a slow node timed out, none on `main`),
started a timer thread per RPC, and no longer honoured `install_opener`.

`rpc` now uses the standard `http.client` connections through urllib with
`timeout=self.timeout`, which bounds each connect attempt and each read as on
`main`, with the standard TLS context. A node that keeps trickling bytes can
still hold a request longer than the timeout; that is unchanged from `main`,
and the README says so. Everything else this branch fixed stays: an unreadable
`contents` is a NodeError, the reply is read only up to `MAX_REPLY_BYTES` + 1,
RecursionError, ValueError, OSError and `http.client.HTTPException` are the 503
hold, and redirects are followed only to http and https. The opener is built
from the standard handlers without the FTP, file and data ones (proxy, unknown,
HTTP, HTTPS, default error, redirect and error processor), so proxies and
certificate verification behave as on `main`; because it is built per call,
`install_opener` is still not consulted. An HTTP error reply is closed when the
RPC ends. Tests that exercised only the removed machinery were deleted; tests
for the redirect refusal, HTTP 500 and 404, the size cap, the HTTPException
cases, falling back across addresses (an unopenable `::1` then a live
127.0.0.1, and a blackholed first address then a live one within about twice
the timeout) and the absence of ResourceWarnings remain or were added. The
README's unit-test count moved 160 → 139.

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
