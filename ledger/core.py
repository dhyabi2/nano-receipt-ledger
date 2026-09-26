"""The ledger itself: append-only receipts, and the totals they add up to.

READ THE IMPORTS. This module holds no key, opens no socket, and imports
nothing that could sign or send anything. It cannot move money, by
construction rather than by policy, and `test_ledger_cannot_move_money`
asserts exactly that against this file's import list. Payment is made by
whichever tool owns it; this module only records what a Nano node already
says happened.
"""

import re
import secrets

import nanoaddr
from errors import LedgerError
from money import AmountError, format_xno, parse_xno

KINDS = ("grant", "work_settlement", "tip", "refund")
BLOCK_HASH_RE = re.compile(r"\A[0-9A-F]{64}\Z")
RECEIPT_ID_RE = re.compile(r"\Arcpt_[0-9a-f]{16}\Z")
MAX_COUNTERPARTY = 80
MAX_REASON = 2000


def new_receipt_id():
    return "rcpt_" + secrets.token_hex(8)


def _require_string(value, field, low, high):
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(400, "%s_required" % field, "'%s' must be a non-empty string" % field)
    text = value.strip()
    if not (low <= len(text) <= high):
        raise LedgerError(
            400, "%s_out_of_range" % field,
            "'%s' must be %d..%d characters, got %d" % (field, low, high, len(text)),
        )
    return text


class Ledger:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    # -- writes -----------------------------------------------------------

    def create(self, body):
        if not isinstance(body, dict):
            raise LedgerError(400, "bad_request", "body must be a JSON object")

        kind = body.get("kind")
        if kind not in KINDS:
            raise LedgerError(
                400, "bad_kind", "'kind' must be one of %s" % ", ".join(KINDS))

        counterparty = _require_string(
            body.get("counterparty"), "counterparty", 1, MAX_COUNTERPARTY)

        address = body.get("counterparty_address")
        verdict = nanoaddr.validate(address)
        if not verdict["valid"]:
            # Nothing is created. This is the eddie_researcher failure: an
            # address that fails checksum was accepted once and the payment
            # it named went nowhere.
            raise LedgerError(
                400, "invalid_address",
                "counterparty_address failed validation: %s" % verdict["message"],
                detail={"reason": verdict["reason"]},
            )

        try:
            amount_raw = parse_xno(body.get("amount_xno"))
        except AmountError as exc:
            raise LedgerError(400, "amount_out_of_range", str(exc)) from None

        if not body.get("reason") or not str(body["reason"]).strip():
            raise LedgerError(400, "reason_required", "'reason' must say why this was paid")
        reason = _require_string(body.get("reason"), "reason", 1, MAX_REASON)

        linked = body.get("linked", {}) or {}
        if not isinstance(linked, dict):
            raise LedgerError(400, "bad_linked", "'linked' must be an object")

        receipt = {
            "id": new_receipt_id(),
            "kind": kind,
            "counterparty": counterparty,
            "counterparty_address": verdict["address"],
            "amount_raw": str(amount_raw),
            "amount_xno": format_xno(amount_raw),
            "block_hash": None,
            "confirmed": False,
            "reason": reason,
            "linked": {k: v for k, v in linked.items() if v is not None},
            "created_at": self.clock(),
            "confirmed_at": None,
            "superseded_by": None,
        }
        with self.store.lock:
            self.store.receipts().append(receipt)
            self.store.flush()
        return receipt

    def attach_block(self, receipt_id, block_hash, block_info):
        """Attach a block the node has already been asked about.

        `block_info` is the node's normalised answer, passed in by the
        caller. This module never asks a node anything itself - it is handed
        a fact and checks the fact against the receipt.
        """
        with self.store.lock:
            receipt = self.get(receipt_id)
            if not isinstance(block_hash, str) or not BLOCK_HASH_RE.match(block_hash):
                raise LedgerError(
                    400, "bad_block_hash", "'block_hash' must be 64 uppercase hex characters")
            if receipt["block_hash"] is not None:
                raise LedgerError(
                    409, "block_already_attached",
                    "receipt %s already carries block %s" % (receipt_id, receipt["block_hash"]))
            for other in self.store.receipts():
                if other["block_hash"] == block_hash:
                    raise LedgerError(
                        409, "block_reused",
                        "block %s is already the settlement of receipt %s; one block pays "
                        "one receipt" % (block_hash, other["id"]),
                        detail={"receipt_id": other["id"]},
                    )
            if not block_info.get("found"):
                raise LedgerError(404, "block_not_found", "the node does not know block %s" % block_hash)
            if not block_info.get("confirmed"):
                raise LedgerError(
                    409, "block_unconfirmed",
                    "block %s is not confirmed; an unconfirmed block is not a payment" % block_hash)

            mismatches = {}
            if block_info.get("subtype") != "send":
                # A confirmed receive on our own chain pays nobody. This check
                # exists because nano-settlement-verify shipped without it.
                mismatches["subtype"] = {
                    "expected": "send", "got": block_info.get("subtype")}
            if block_info.get("destination") != receipt["counterparty_address"]:
                mismatches["destination"] = {
                    "expected": receipt["counterparty_address"],
                    "got": block_info.get("destination"),
                }
            if str(block_info.get("amount_raw")) != receipt["amount_raw"]:
                mismatches["amount"] = {
                    "expected": receipt["amount_raw"], "got": str(block_info.get("amount_raw"))}
            if mismatches:
                raise LedgerError(
                    409, "block_mismatch",
                    "block %s does not say what receipt %s says" % (block_hash, receipt_id),
                    detail=mismatches,
                )

            receipt["block_hash"] = block_hash
            receipt["confirmed"] = True
            receipt["confirmed_at"] = self.clock()
            self.store.flush()
            return receipt

    def supersede(self, receipt_id, replacement_id, reason):
        with self.store.lock:
            receipt = self.get(receipt_id)
            if not receipt["block_hash"]:
                raise LedgerError(
                    409, "nothing_to_correct",
                    "receipt %s carries no block; a pending receipt either confirms or is "
                    "superseded after confirming" % receipt_id)
            if not isinstance(replacement_id, str) or self.store.by_id(replacement_id) is None:
                raise LedgerError(
                    404, "not_found", "replacement receipt %r does not exist" % replacement_id)
            if replacement_id == receipt_id:
                raise LedgerError(
                    400, "bad_replacement", "a receipt cannot supersede itself")
            if receipt["superseded_by"]:
                raise LedgerError(
                    409, "already_superseded",
                    "receipt %s was already superseded by %s"
                    % (receipt_id, receipt["superseded_by"]))
            reason = _require_string(reason, "reason", 1, MAX_REASON)

            receipt["superseded_by"] = replacement_id
            # The block hash, the amount and the counterparty are untouched.
            # A correction is an addition, never an erasure.
            self.store.corrections().append({
                "receipt_id": receipt_id,
                "replacement_id": replacement_id,
                "reason": reason,
                "at": self.clock(),
            })
            self.store.flush()
            return receipt

    # -- reads ------------------------------------------------------------

    def get(self, receipt_id):
        receipt = self.store.by_id(receipt_id)
        if receipt is None:
            raise LedgerError(404, "not_found", "no receipt with id %r" % receipt_id)
        return receipt

    def index(self, kind=None, counterparty=None, limit=50, cursor=None, base_url=""):
        if kind is not None and kind not in KINDS:
            raise LedgerError(400, "bad_kind", "'kind' must be one of %s" % ", ".join(KINDS))
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise LedgerError(400, "bad_limit", "'limit' must be an integer 1..100") from None
        if not 1 <= limit <= 100:
            raise LedgerError(400, "bad_limit", "'limit' must be 1..100, got %d" % limit)

        matching = [
            r for r in self.store.receipts()
            if (kind is None or r["kind"] == kind)
            and (counterparty is None or r["counterparty"] == counterparty)
        ]
        matching.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)

        start = 0
        if cursor:
            ids = [r["id"] for r in matching]
            if cursor not in ids:
                raise LedgerError(400, "bad_cursor", "cursor %r is not in this result set" % cursor)
            start = ids.index(cursor) + 1
        page = matching[start:start + limit]
        following = matching[start + limit:]

        return {
            "receipts": [self.summary(r, base_url) for r in page],
            "next_cursor": page[-1]["id"] if page and following else None,
            "totals": self.totals(),
        }

    def summary(self, receipt, base_url=""):
        return {
            "id": receipt["id"],
            "kind": receipt["kind"],
            "counterparty": receipt["counterparty"],
            "amount_xno": receipt["amount_xno"],
            "block_hash": receipt["block_hash"],
            "confirmed": receipt["confirmed"],
            "superseded_by": receipt["superseded_by"],
            "created_at": receipt["created_at"],
            "url": "%s/v1/receipts/%s" % (base_url.rstrip("/"), receipt["id"]),
        }

    def totals(self):
        receipts = self.store.receipts()
        confirmed = [r for r in receipts if r["confirmed"]]
        # Confirmed only. A pending amount is not money anybody has received,
        # and folding it into the total would make this document a claim
        # rather than a record.
        paid_raw = sum(int(r["amount_raw"]) for r in confirmed)
        created = sorted(r["created_at"] for r in receipts)
        return {
            "receipts": len(receipts),
            "confirmed": len(confirmed),
            "pending": len(receipts) - len(confirmed),
            "paid_xno_total": format_xno(paid_raw),
            "counterparties": len({r["counterparty_address"] for r in confirmed}),
            "first_receipt_at": created[0] if created else None,
        }
