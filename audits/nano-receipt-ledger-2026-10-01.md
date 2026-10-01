# nano-receipt-ledger — code audit, 2026-10-01

Scope: the tree at `14ebb01` (`main`). Baseline, before any change:

```
$ python3 -m unittest discover -s tests   # Ran 50 tests — OK
$ python3 e2e_check.py                    # 23/23 checks pass
$ python3 -m py_compile ledger/*.py serve.py e2e_check.py tests/*.py   # clean
$ <the two CI secret guards, run verbatim>                             # clean
```

Read end to end this run: `money.py`, `nanoaddr.py`, `core.py`, `node.py`, `app.py`, `serve.py`,
through the one question that matters here — can an agent that has been paid in XNO get a receipt
it can show, and can a reader check it against the public ledger.

## Found and fixed

**A Nano node that is configured but unreachable closed the caller's connection with no HTTP
response at all.** `_attach` (`app.py:114`) already answers `503 no_node` when no node is
configured:

```python
if self.node is None:
    raise LedgerError(503, "no_node", "no Nano node is configured; nothing was attached")
info = self.node.block_info(block_hash) if isinstance(block_hash, str) else {"found": False}
```

The line after it reached the network. `NanoNode.rpc` (`node.py:22`) called
`urllib.request.urlopen` unguarded, and `Handler._dispatch` (`app.py:223`) catches **only**
`LedgerError`. So a refused connection, a timeout, or a body that is not JSON went straight past
both.

Measured, real socket, node URL pointing at a closed port, after a successful `POST /v1/receipts`:

```
POST /v1/receipts/<id>/attach-block
  ->  http.client.RemoteDisconnected: Remote end closed connection without response
      (and a urllib traceback on the server's stderr)
```

No status, no body, nothing. `/attach-block` is *the* endpoint an agent calls to prove its XNO
payment settled, and the three answers it most needs to tell apart — the node was down (retry),
the block was refused (do not retry), the server broke — all collapse into a dropped socket. A
node that is configured and down is the ordinary case, not an exotic one: it is the same situation
as the 503 one line above, and it got strictly less than an error.

**Fixed** in the two places that own the two halves:

- `node.py` now turns transport failures into its own existing `NodeError`:
  `except (OSError, ValueError)` — `URLError` and socket timeouts are both `OSError`,
  `JSONDecodeError` and `UnicodeDecodeError` are both `ValueError`. Deliberately **not**
  `except Exception`: the action guard above it (`this client issues only block_info`) must keep
  raising loudly, and so must the suite's `FakeNode`, which raises `RefusesToSend` if the ledger
  ever asks for something send-shaped. Turning either of those into "the node is down" would hide
  exactly what they exist to reveal. A third test pins that they still raise.
- `app.py` catches `NodeError` and answers `503 node_unavailable`, saying the block was **not
  judged** and the request can be retried. Nothing has been written at that point, which the test
  asserts by re-reading the receipt and finding it still pending — so the retry the message
  promises is in fact safe.

The import is `from node import NodeError` in `app.py` only. `core.py`'s import law
(`test_11_ledger_cannot_move_money`) is untouched and still passes: the core still imports nothing
that could sign or send, and `node.py` itself still has no send, no `block_create` and no signing.

Proved both directions. Against the unfixed tree:

```
ERROR: test_a_node_that_refuses_the_connection_is_a_503_not_a_dropped_connection
ERROR: test_a_node_that_answers_with_something_that_is_not_json
  http.client.RemoteDisconnected: Remote end closed connection without response
Ran 3 tests — FAILED (errors=2)
```

(The third, `test_a_send_shaped_action_still_raises_loudly`, passes both before and after on
purpose: it is the guard that this fix did not soften the tripwires.)

With the fix: **53 unit tests OK**, **23/23 e2e**, `py_compile` clean, both CI secret guards clean.
The README's advertised unit count moved 50 → 53 in the same change, because the suite's own count
law (`tests/test_ledger.py:819`) reads the README and fails until it matches.

## Checked and clean

- **Amounts are integers end to end.** `money.py` is exact decimal → `int` raw with
  `RAW_PER_XNO = 10**30`; no float appears on any amount path. `parse_xno` refuses more precision
  than the ledger can publish rather than printing a number that is not the number paid, and
  `format_xno` refuses to render a raw value it cannot render exactly. `totals` sums `int` and
  formats once. Re-derived: every stored amount is a multiple of `10**24`, so the sum always
  renders.
- **`raw_amount` compares amounts, not spellings** — `"0500"` and `"500"` are one amount — and it
  is ASCII-guarded, because `"²".isdigit()` is True while `int("²")` raises, which would have let a
  `ValueError` escape a money comparison. Correct, and the non-ASCII case is the subtle one.
- **`account_key` compares accounts, not spellings.** `nano_` and `xrb_` are one account; the node
  always answers the `nano_` form. Verified the decoder itself rather than trusting it:
  `decode` takes 52 base32 characters (260 bits), renders 33 bytes and drops byte 0 — which is
  exactly the four pad bits plus four zero bits above them — and the `body[0] not in "13"` guard is
  what makes those pad bits provably zero. Without that guard two distinct addresses would map to
  one key. The checksum is blake2b-5 of the key, reversed, base32 over the alphabet with 0/2/l/v
  absent. All correct.
- **One block pays one receipt.** `attach_block` scans every stored receipt for the hash before
  anything else, and `BLOCK_HASH_RE` forces upper case at the door so both sides of that comparison
  are already canonical. `subtype == "send"` is checked (the check `nano-settlement-verify` shipped
  without), as are `confirmed`, `found`, destination and amount, and a `None` on either side still
  refuses.
- **Append-only is a rule, not an omission.** PUT/PATCH/DELETE are answered `405 append_only` by an
  explicit rule in `handle`, and `supersede` adds a correction row while leaving the block hash,
  amount and counterparty untouched.
- **Auth.** `tokens_match` is `hmac.compare_digest`, an empty configured token refuses every write
  rather than admitting one, and `log_message` is suppressed so a token cannot reach a request log.
  The e2e run asserts the token is nowhere in the store on disk.
- **No secrets in the tree or history.** Both CI guards run clean verbatim, including on the new
  test file. No tracked `.env`, `.pem` or `.key`.
- **The README's quickstart runs.** The documented commands are the ones run above, and its
  advertised counts now match the suites exactly (53 and 23).

## Not verified

- **Against a real Nano node.** Everything here was driven against `FakeNode`, the e2e `NodeStub`,
  and — for this run's fix — two real sockets: a closed port and an HTTP server answering HTML.
  No public node was contacted. `normalise`'s field reads (`confirmed` as the string `"true"`,
  destination as `contents.link_as_account`) match what the sibling `tollstile-rail-nano` measured
  against rpc.nano.to on 2026-09-28, which is evidence but not a measurement taken here.
- **A node that answers slowly rather than not at all.** The timeout path is `OSError` and so takes
  the same branch as a refused connection, but a genuinely slow node was not simulated; the
  10-second default in `NanoNode.__init__` was not tuned or tested.
- **Concurrency.** `Store.lock` serialises writes and `ThreadingHTTPServer` is used, but no
  concurrent attach-the-same-block race was driven. The single-use check and the write are inside
  one `with self.store.lock`, which is the right shape; it was read, not stress-tested.
