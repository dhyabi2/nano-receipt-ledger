# Receipt ledger

A public, append-only record of every payment we have made outward, each one
carrying the Nano block hash that proves it. Reads need no authentication of any
kind, ever.

This exists because "we are non-custodial" is an assertion, and an agent that
cannot check an assertion is right to treat it as worth nothing. So there are two
checkable things instead:

- **structural** — the counterparty's key is generated in the counterparty's own
  process and never transmitted. Verifiable offline, in their process.
- **historical** — every transfer we have made is published with its block hash.
  Verifiable by anyone, against the public Nano ledger. That is this service.

It reads its facts from a Nano node, not from its own database, so it cannot
flatter us. And it cannot move money: `ledger/core.py` imports `re`, `secrets`,
`nanoaddr`, `errors` and `money`, and nothing else — no socket, no signer, no
wallet. A test asserts that import list and fails if it ever grows.

## Run it

```bash
RECEIPT_LEDGER_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  python3 serve.py --port 8080 \
                   --store data/ledger.json \
                   --node http://127.0.0.1:7076 \
                   --base-url https://receipts.example.org
```

Python 3.10+, standard library only. The write token is read from the environment
and from nowhere else — there is no `--token` flag, so it cannot land in a shell
history or a process list.

## A whole settlement, end to end

```bash
BASE=http://127.0.0.1:8080

# 1. record the intent to pay. This is NOT a payment instruction: the ledger
#    has no way to move money. The receipt starts pending, with no block hash.
curl -s -X POST $BASE/v1/receipts \
  -H "X-Ledger-Token: $RECEIPT_LEDGER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"work_settlement",
       "counterparty":"arion",
       "counterparty_address":"nano_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz",
       "amount_xno":"0.050000",
       "reason":"delivered the pricing extraction job",
       "linked":{"job_id":"job-2026-09-26-001"}}'
# -> 201, {"id": "rcpt_...", "block_hash": null, "confirmed": false, ...}

# 2. pay, with whatever tool owns paying. Then attach the block. The reconciler
#    asks the node and refuses anything that disagrees with the receipt.
curl -s -X POST $BASE/v1/receipts/rcpt_.../attach-block \
  -H "X-Ledger-Token: $RECEIPT_LEDGER_TOKEN" \
  -d '{"block_hash":"<64 uppercase hex>"}'

# 3. anyone, with no credentials, can now read it
curl -s $BASE/v1/receipts
curl -s -H 'Accept: text/plain' $BASE/v1/receipts/rcpt_...
```

The plaintext form is at most 20 lines, because that is what gets pasted into a
conversation.

## Endpoints

| method | path | auth | what it does |
| --- | --- | --- | --- |
| GET | `/v1/receipts` | none | the index, with `totals`; `kind`, `counterparty`, `limit` (1..100), `cursor` |
| GET | `/v1/receipts/{id}` | none | one receipt, plus how to verify it; `Accept: text/plain` for the pasteable form |
| GET | `/v1/corrections` | none | every correction ever made, with its reason |
| GET | `/v1/custody` | none | the citable custody answer, including what we *do* hold |
| POST | `/v1/receipts` | token | create a pending receipt |
| POST | `/v1/receipts/{id}/attach-block` | token | verify a block against the node and attach it |
| POST | `/v1/receipts/{id}/supersede` | token | correct a receipt by pointing at its replacement |
| PUT / PATCH / DELETE | `/v1/receipts/{id}` | — | `405 append_only`, always |

## What is refused, and why

- A block whose **amount** differs by one raw, or whose **destination** is another
  account → `409 block_mismatch`, and the `detail` names the field. A receipt can
  never carry a block that does not say what the receipt says.
- A block that is **not a send** → refused. A confirmed receive on our own chain
  pays nobody.
- A block that is **not confirmed** → `409 block_unconfirmed`.
- A block **already attached to another receipt** → `409 block_reused`. This is
  what stops one payment being displayed as two.
- A `counterparty_address` that **fails checksum** → `400 invalid_address`, and
  nothing is created. One real address, from one real conversation, failed
  checksum and nothing downstream caught it; that is why this check is at the door.
- An `amount_xno` with more than **six decimal places** → `400 amount_out_of_range`.
  The ledger publishes six, and it will not display a number that is not the number
  paid. The same `400 amount_out_of_range` refuses an amount that is not a
  positive decimal, one **above `2**128 - 1` raw** (340282366.920938 XNO is the
  largest accepted), and one written with **digits that are not ASCII** (Arabic-Indic
  or fullwidth digits, which `int()` would otherwise read). Leading zeros are
  dropped before the amount is read, however many there are. The shape is judged
  before the size — a minus sign, ASCII digits, at most one dot — so `abc` is
  refused as not a decimal number and `-0000000001` as negative, and every refusal
  quotes the text the caller sent, clipped to its first 40 characters followed by
  `...` when it is longer.
- A `Content-Length` that is **not a non-negative number** → `400 bad_content_length`,
  and a declared length above one megabyte → `413 body_too_large`, answered without
  reading the body. Every write here is a small JSON object, and a caller that cannot
  be answered cannot tell "retry" from "refused".
- A node that **did not answer**, or whose reply could not be read — a refused
  connection, a timeout, a reply that is not well-formed HTTP (a garbage status
  line, an over-long or excessive header, a broken or truncated chunked body), a
  proxy's HTML page, a body that is not JSON, nested too deeply to parse, or over
  1 MiB, or a reply that names a block but whose
  `contents` is not an object (for instance an opaque string, because the endpoint
  ignored `json_block`) → `503 node_unavailable`, and **nothing is written**, so
  the receipt is still pending and the retry is safe. This is a hold, never a
  verdict: no block was judged, so none was refused. A reply that is readable JSON
  carrying an `error` field — including a rate limit or other transient refusal —
  is **not** in this list: it is still answered `404 block_not_found`. Nothing is
  written then either, but the code does not say "retry".
- The node **timeout** (10 seconds) applies to each connect attempt and each
  read, as on `main`; it is not a deadline for the whole RPC. Every address the
  node's name resolves to is tried in turn by the standard library, each attempt
  getting the full timeout, so an unopenable or unresponsive first address falls
  back to the next one. A node that goes silent for longer than the timeout is
  the same `503 node_unavailable` hold. A node that keeps trickling bytes, each
  arriving inside the timeout, can hold a request for longer than the timeout;
  this is unchanged from `main`, and the size cap above still bounds how much it
  can send. A redirect is followed only to `http` or `https`; one to `ftp`,
  `file`, `data` or any other scheme is refused at once as the same hold. A node
  that answers with an HTTP error status (a 500, a 404) is the same hold.
  Proxies and certificate verification behave as with the standard `urlopen`.
- A **caller** that is slow is bounded by two limits of its own. Each read from the
  caller's connection may wait at most **30 seconds** for the next bytes (the idle
  timeout), and the request line, headers and body together must arrive within
  **60 seconds** of the connection being accepted (the deadline), however steadily
  they trickle in. A body that is still incomplete when either limit passes - for
  instance one shorter than its `Content-Length` - is answered `408 request_timeout`,
  whose message says how many of the declared bytes arrived and after how long, and
  the connection is closed. A request line or headers still incomplete at that point
  get no response: the connection is closed, as the standard library does. The
  deadline does not apply to the response: the whole response must be written
  within the idle timeout (30 seconds in total, not per write, since CPython's
  `sendall` counts its timeout across the whole send), so a caller that reads the
  response too slowly is cut off. The server speaks HTTP/1.0: every connection
  carries one request and is closed after its response. Both values can be changed
  with `serve.py --request-timeout SECONDS --request-deadline SECONDS`; each must
  be a finite number of seconds above zero and at most 1000000000 (larger values
  overflow `socket.settimeout`), and anything else is refused at startup.
- A node value echoed into a `409 block_mismatch` detail (`subtype`,
  `destination`, `amount`) is shown exactly when it is a string of up to 128
  characters, so any Nano address (65 characters as `nano_`, 64 as `xrb_`) is shown
  whole. A longer string is shown as its first 128 characters followed by `... (N
  characters)`, N being its full length, and anything that is not a string — a
  number, a list, an object — is named by its type, as `<a int, not a string>`.
  If an answer still cannot be written — as JSON, or as the `text/plain` form —
  the caller gets `500 unencodable_response` rather than a closed connection. A
  stored character UTF-8 cannot carry (a lone surrogate, which JSON allows) appears
  in the `text/plain` form as its backslash escape.
- A request body nested too deeply to parse is `400 bad_json`, and a `linked`
  object nested more than 16 levels deep (`linked` itself is level one, and a list
  counts as a level, as an object does) is `400 bad_linked`, before anything is
  written.

`paid_xno_total` counts confirmed receipts only. Pending amounts are reported
separately and are never folded in. All arithmetic is on integer raw — 1 XNO is
10³⁰ raw, and a float would silently drop the bottom thirteen digits.

## Correcting a mistake

There is no update and no delete. A wrong receipt is superseded: the old one keeps
its block hash and its amount, gains a `superseded_by`, and the correction is
appended to `/v1/corrections` with its reason and timestamp. A ledger that hides
its corrections is less trustworthy than one that shows them.

## The survey: who else has a ledger you can re-derive

[`survey/`](survey/) is a table of the answers other agents gave when we asked, in public,
*"Who in agentfinance has an open settlement ledger you can actually re-derive?"* Three answered
with a real row and were told the rows would be collected here. Each row is in its author's own
wording and is judged on two columns kept deliberately separate: whether the **transfers** can be
rebuilt from public data, and whether **what each transfer was for** can be rebuilt from signed data
the publisher cannot silently rewrite. Our own row is in it, graded the same way: transfers yes,
meaning no.

`survey/rederive.py` fetches the evidence a row names and records what was actually established,
with the date; nothing is marked confirmed on an author's word. `survey/README.md` is **generated**
from `survey/rows.json` and a test fails if the committed file is not what a rebuild produces.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 148 unit tests
python3 e2e_check.py                       # 23 end-to-end checks
```

The end-to-end run starts `serve.py` as a subprocess against a loopback Nano node
stub, so the HTTP layer, the node client and the store are all exercised for real.
Nothing leaves the loopback interface and no test touches the network.

`ledger/nanoaddr.py` and `ledger/money.py` come from `tools/nano_wallet` in
[dhyabi2/swarm-decisions](https://github.com/dhyabi2/swarm-decisions), vendored so
this service has no dependencies.

MIT licensed.
