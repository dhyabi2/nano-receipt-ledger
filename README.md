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
  paid.

`paid_xno_total` counts confirmed receipts only. Pending amounts are reported
separately and are never folded in. All arithmetic is on integer raw — 1 XNO is
10³⁰ raw, and a float would silently drop the bottom thirteen digits.

## Correcting a mistake

There is no update and no delete. A wrong receipt is superseded: the old one keeps
its block hash and its amount, gains a `superseded_by`, and the correction is
appended to `/v1/corrections` with its reason and timestamp. A ledger that hides
its corrections is less trustworthy than one that shows them.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 53 unit tests
python3 e2e_check.py                       # 23 end-to-end checks
```

The end-to-end run starts `serve.py` as a subprocess against a loopback Nano node
stub, so the HTTP layer, the node client and the store are all exercised for real.
Nothing leaves the loopback interface and no test touches the network.

`ledger/nanoaddr.py` and `ledger/money.py` come from `tools/nano_wallet` in
[dhyabi2/swarm-decisions](https://github.com/dhyabi2/swarm-decisions), vendored so
this service has no dependencies.

MIT licensed.
