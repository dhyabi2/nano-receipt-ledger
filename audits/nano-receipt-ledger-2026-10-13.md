# nano-receipt-ledger — audit 2026-10-13

Audited at `9e553df` (`main`). Ninth audit; all eight earlier notes
(2026-09-27 … 2026-10-11) and the full README read first.

**The last note was written at `009f4de`.** Commits #9 (`e15edb2`) and #10
(`9e553df`) landed after it, so the caller-side idle timeout, the request
deadline, `408 request_timeout` and `400 incomplete_body` in `ledger/app.py`
are ground no note covers. That is where most of the effort went.

Lens: can an agent get paid in XNO and hand a stranger a receipt they can
check, without being hurt.

Baseline on `main`, exactly what `.github/workflows/test.yml` runs:

```
$ python3 -m unittest discover -s tests          # Ran 149 tests - OK   (3.11.17)
$ python3 e2e_check.py                           # 23/23 checks pass
$ /usr/bin/python3.12 -m unittest discover -s tests  # OK   + 23/23
$ /usr/bin/python3.13 -m unittest discover -s tests  # OK   + 23/23
$ python3 -m compileall -q ledger serve.py e2e_check.py tests survey   # clean
$ both CI secret-guard greps, verbatim                                 # clean
```

`README.md:194` advertises 149 unit tests and 23 end-to-end checks; both match,
and `ReadmeCounts` pins them by actually counting. CI's 3.10 leg could not be
run (no 3.10 interpreter here).

## Found and fixed — a reply that is not a JSON object was answered `404 block_not_found`

`ledger/node.py:132` read

```python
if not isinstance(answer, dict) or "error" in answer:
    return {"found": False, ...}
```

so `[]`, `["error"]`, a bare string, a number, `null`, `true` and a float all
folded into the not-found answer. The caller was told **"the node does not know
block X"** although nothing about the block was read. Measured directly:

```
[]       -> {'found': False, 'confirmed': False, 'subtype': None, 'destination': None, 'amount_raw': None}
'ok'     -> {'found': False, ...}
1        -> {'found': False, ...}
```

and over real HTTP through `serve.py` against a stub node: a list reply and a
string reply each gave `404 block_not_found`.

**This is the wrong verdict by the function's own stated rule.** `normalise`'s
docstring argues the case two lines further down: *"It is a NodeError and not a
mismatch because nothing was read: where the money went is unknown, not
wrong… 503 `node_unavailable` tells the caller to ask again, which is the
truth."* A 404 says the block does not exist. A caller that believes it can
refuse a sale that was genuinely paid for — and XNO does not come back.

**It does not undo the 2026-10-11 decision.** That note deliberately left the
`error` **field** as a known limitation, with a reason: telling a real
not-found from a transient refusal needs the error strings of each endpoint,
which that audit did not collect. **That reasoning does not reach this case** —
a reply that is not an object carries no error string to interpret, so there is
nothing to collect and no ambiguity to resolve. It is unreadable, full stop.
The fix therefore splits the condition and changes only the shape the earlier
reasoning does not cover; a node's own `{"error": ...}` object, including a
rate limit, still reads as a not-found and still answers 404. A control test
pins that in both directions.

The change adds a refusal and nothing else: one `raise NodeError`, which the
HTTP layer already turns into the documented `503 node_unavailable` hold with
nothing written and a safe retry. No amount, destination, rounding or key path
is touched. `README.md:108-119` now lists the shape under the 503 hold and says
why the `error` field stays a 404.

### Failing, then passing

Two tests added. With `ledger/node.py` **alone** reverted to `main` and the
tests kept, **8 subtests fail** — one per shape:

```
FAIL: test_normalise_holds_on_a_reply_that_is_not_a_json_object (answer=[])
                                                               (answer=['error'])
                                                               (answer='ok')
                                                               (answer='error')
                                                               (answer=1)
                                                               (answer=None)
                                                               (answer=True)
                                                               (answer=0.5)
Ran 151 tests - FAILED (failures=8)
```

`test_a_node_error_field_is_still_a_not_found_and_not_a_hold` passes either
way, which is what proves the known limitation is untouched. With the fix:
**151 passed** on 3.11, 3.12 and 3.13, and 23/23 e2e on each.

## Found, NOT fixed — listed rather than built

- **`ledger/node.py:42` — a node answering `302` is re-asked with a bare `GET`
  and no payload, and that answer settles the receipt.** `_opener()` installs
  `HTTPRedirectHandler`, which turns the POST into a GET and drops the body.
  Reproduced with two raw-socket stubs:

  ```
  node saw: POST / HTTP/1.1   | body: {"action": "block_info", "json_block": "true", "hash": "AAAA…
  node saw: GET /x HTTP/1.1   | body: <empty>
  block_info -> {'found': True, 'confirmed': True, 'subtype': 'send',
                 'destination': 'nano_11131a3ia…', 'amount_raw': '50000000000000000000000000000'}
  ```

  The redirect target was never told which block to look up, yet its reply
  confirmed the receipt. **The existing test
  `test_a_redirect_to_another_http_node_is_followed` (`tests/test_ledger.py:2205`)
  passes for the wrong reason**: its stub answers whatever it is asked, so it
  cannot see this. That makes it a test asserting the wrong thing — category 3 —
  and it also weakens e2e check 11's claim that "the node was only ever asked
  for `block_info`".

  Not fixed here because the remedy (drop the redirect handler, or re-issue the
  POST on redirect) **changes a behaviour a test deliberately asserts**, and
  deciding whether this node should follow redirects at all is the owner's call.
  The node is an operator-configured, trusted component, so this sits inside the
  documented boundary ("a lying node is outside what this service checks") —
  but a misconfigured URL or an on-path actor without TLS is not a lying node.

- **`ledger/app.py:519` — a burst of ~40+ concurrent writes gets connections
  reset with no HTTP response.** `make_server` returns a plain
  `ThreadingHTTPServer` and never sets `request_queue_size`, so the listen
  backlog is the stdlib default of 5, while each write serialises on
  `store.lock` and ends in an `os.fsync`. On the settlement endpoint itself,
  150 distinct receipts and blocks:

  ```
  MODE=attach N=150 ok=128 errors=22 settled=128
    errors: Counter({'ConnectionResetError': 22})
  ```

  22 callers got nothing back and those receipts stayed pending. It **fails
  closed** (the request never reaches the handler, so nothing is written) and
  the read path is clean (reads do no fsync). Proven to be the backlog, by a
  subclass whose only difference is that attribute:

  ```
  request_queue_size=5    N=150 ok=108 dropped=42  stored=108
  request_queue_size=128  N=150 ok=150 dropped=0   stored=150
  ```

  **Not fixed, and the caveat is why:** a plain stdlib `ThreadingHTTPServer`
  with a comparably slow handler shows the same rate, so this is an inherited
  default plus this service's fsync-per-write, not a logic error. Raising a
  queue size is tuning, not a defect fix, and it needs ~40 simultaneous write
  connections, which a single-token operator write path rarely sees. Recorded
  because it produces exactly the "no response at all" outcome this repository
  treats as a defect by its own rule, and the README promises an answer for
  every case it lists and never mentions a connection limit.

- **`ledger/custody.py:14` — a dead pointer in the "citable custody answer".**
  `GET /v1/custody` publishes `structural_proof.spec =
  "specs/agent-tool-wallet-mcp.md"` and `check_it_yourself = "nano-wallet
  selfcheck"`. `ls specs` → *No such file or directory*; neither the path nor
  the command exists in this repository, and the README gives no base for them.
  The one field a reader would follow to check the structural half of the claim
  resolves nowhere. Left alone: whether to write the spec, repoint the field or
  drop it is the owner's call, not a routine's.

- **`README.md:203` — the vendoring credit still 404s.** `curl` →
  `http=404` for `https://github.com/dhyabi2/swarm-decisions` (a private
  repository, so a 404 is what an anonymous reader sees). Recorded on
  2026-09-27 as needing an owner decision; re-confirmed, still open.

## Also checked, found clean

- **`ledger/money.py` — no float, no `Decimal`, no rounding.** Integer-only by
  construction (`RAW_PER_XNO = 10**30`); `parse_xno` refuses more than 6 decimal
  places, non-ASCII digits, non-decimal shapes and anything above `2**128-1`
  raw; `format_xno` refuses a raw it cannot render exactly rather than rounding.
  There is no `decimal` import anywhere, so the `getcontext().prec` hazard does
  not apply. **200,000 random amounts fuzzed** over the whole accepted range:
  **0 round-trip mismatches** — every accepted `amount_xno` gives
  `w*10**30 + f*10**24` exactly and formats back byte-for-byte.
  `340282366.920938` accepted, `340282366.920939` refused.
- **`ledger/nanoaddr.py` — correct codec, and the checksum is load-bearing.**
  20,000 random keys round-trip; the `xrb_` form yields the same public key.
  **19,378 single-character mutations of valid addresses: 0 still decoded to the
  original key.**
- **Verification trusts nothing the submitter sends.** `ledger/app.py:164`
  passes only the caller's `block_hash` to the node; every compared fact is the
  node's. `attach-block` bodies carrying forged `amount_raw`, `confirmed`,
  `destination`, `subtype`, `found` and a whole fake `block_info` object were
  all ignored — each took the normal path and answered identically to the bare
  request.
- **All 33 node answers produce the right verdict.** Only a confirmed `send` of
  the exact raw amount to the right account key attaches. `confirmed: 1`,
  `"yes"`, absent, `"false"` → `409 block_unconfirmed`; `subtype: "SEND"`,
  `"receive"`, `"epoch"` → `409 block_mismatch`; one raw over or short, a
  JSON-float amount, Arabic-Indic digits, `balance` in place of `amount` →
  `409 block_mismatch`; a stranger payee in either spelling → `409`.
- **One block pays one receipt, under concurrency.** 20 threads attaching the
  same block to 20 different receipts:
  `Counter({(409, 'block_reused'): 19, (200, None): 1})`, and the store holds
  exactly one receipt carrying that hash. The check-and-set is inside one
  `store.lock` critical section and it holds.
- **190-case HTTP fuzz: 0 dropped connections, 0 500s.** Hostile `block_hash`
  (null, int, float, bool, list, dict, 5000 characters, Arabic-Indic digits,
  `../../etc/passwd`, an embedded NUL), 30 hostile `amount_xno`, 11 hostile
  addresses, 72 method×path combinations, 18 non-object bodies: every one came
  back as a complete HTTP response with a real status.
- **The old-file class does not apply, and it was proven rather than argued.**
  There is no SQLite and no `CREATE TABLE`; storage is one JSON file replaced
  wholesale, and the receipt record has had the **same 13 fields since the first
  commit** (`git show <commit>:ledger/core.py` across `2f3a2c3 → fdc75cb →
  73f0618 → e15edb2`: identical key sets). A ledger written with the **oldest**
  checkout, including a row carrying the pre-canonicalisation `xrb_` spelling,
  reopened with current code:

  ```
  index/totals:  {"confirmed":1,"counterparties":1,"paid_xno_total":"0.010000","pending":1,"receipts":2}
  legacy row settled: True AAAA0123
  totals after:  {"confirmed":2,"counterparties":1,"paid_xno_total":"0.060000","pending":0,"receipts":2}
  ```

  No migration needed, the legacy-spelling row still settles, and it counts as
  one counterparty.
- **README quickstart runs as written.** Step 1 with the README's exact JSON
  body → 201, `amount_raw: "50000000000000000000000000000"` (0.05 × 10^30 to the
  digit). Step 3 index → 200 with no credentials; `Accept: text/plain` → 200 and
  7 lines (≤ 20 as promised). A >1 KB body → 201 in 4 ms, no
  `Expect: 100-continue` stall.
- **Secrets: clean, tree and full history.** 16 commits, 29 distinct paths ever
  committed; `git log --all -p` scanned for standalone 64-hex,
  `-----BEGIN … PRIVATE KEY`, `sk-`, `ghp_`, `AKIA`, `xox[baprs]-`, `AIza` —
  not one hit. No `subprocess`, `os.system`, `eval`, `exec` or `shell=` outside
  the tests and `e2e_check.py`. No unchecked input reaches a shell or a
  filesystem path: the store path is a CLI flag, and `RECEIPT_PATH` admits only
  `[A-Za-z0-9_]+`, so no path component from a URL reaches the filesystem.
- **`ledger/core.py` still cannot move money** — `test_11_ledger_cannot_move_money`
  asserts it at AST level and passes; `NanoNode.rpc` still refuses every action
  but `block_info`.
- **README claim by claim.** The whole "What is refused, and why" list
  (lines 80-164), the endpoint table and the Tests section were walked against
  the code and against live requests. **No README claim was found that the code
  does not support**, other than the 404's reach fixed above.
- **No other test asserts the wrong thing.** `ReadmeCounts` genuinely counts
  (`defaultTestLoader.discover` for the unit number, an AST walk of `check(...)`
  call sites for the e2e number, plus an assertion that none sits in a loop).
  The one exception is the redirect test named above.

## What I could not verify

- **Anything on mainnet.** No XNO moved. Outbound HTTPS here is restricted to a
  short allowlist, so `ledger/app.py:85`'s `block_explorer_url`
  (`https://nanolooker.com/block/`) **could not be tested in either
  direction** — no 404 is claimed. It is the link a stranger follows to check a
  payment and the one docs URL on the verification path still unmeasured;
  previous audits from 2026-10-02 record the same gap. Worth one check from an
  unrestricted network.
- **Whether a real public endpoint serves `contents` as a string**, or any real
  `block_info` wire shape. Every reply above is constructed.
- **Python 3.10**, one leg of the CI matrix — no 3.10 interpreter in this
  container. 3.11, 3.12 and 3.13 are green.
- **The receipt's strongest sentence, re-confirmed as still unsupported.**
  `render()` (`ledger/app.py:188`) tells the reader "We could not have produced
  it without holding the sending key", but `attach_block` never checks the
  block's **sender** and the receipt does not record it. Recorded by the
  2026-10-08 note; still true, and still an addition rather than a defect fix,
  so it was not built.
- **`README.md:143` says the whole response must be written within a 30-second
  idle timeout "in total, not per write".** `send_response` sets the socket
  timeout once (`ledger/app.py:348`) but `_send` performs two `sendall` calls,
  and CPython counts a timeout per `sendall`, so the real bound may be up to
  2 × 30 s. Reasoned, **not measured.**
- **The amount and destination are still taken from the node's answer rather
  than re-derived from the block's own fields**, so a lying node remains
  outside what this service checks — the documented design boundary. Unchanged.
