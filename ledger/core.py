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
from money import MAX_RAW, AmountError, format_xno, parse_xno

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


def account_key(address):
    """The public key an address decodes to, or None if it is not an address.

    Two addresses name the same account exactly when their public keys match.
    One Nano account has two spellings - the modern `nano_` form and the legacy
    `xrb_` form - and `nanoaddr.validate` accepts both on purpose, because
    counterparties still hand out either. A node always answers
    `link_as_account` in the `nano_` form. Comparing the two strings therefore
    refuses a block that paid exactly the right account, and this ledger has no
    way back from that: `attach_block` will not confirm it and `supersede`
    refuses a receipt that carries no block, so the row is stuck for good.
    Compare the keys, which is what the chain means by "the same account".
    """
    verdict = nanoaddr.validate(address)
    return verdict["public_key"] if verdict["valid"] else None


# MAX_RAW (2**128 - 1) has 39 digits; no raw amount has more significant ones.
MAX_RAW_DIGITS = len(str(MAX_RAW))
# The longest node-supplied text a refusal's detail echoes back in full. A
# Nano address is 64 or 65 characters (`xrb_` or `nano_` and 60 more), and a
# refusal that names the wrong destination has to show the whole of it: an
# earlier limit of 64 clipped every `nano_` address by its last character, so
# the one field a payer needs to read back was the one that was cut. 128
# echoes any real address, amount or subtype exactly and still clips junk.
MAX_SHOWN = 128


def raw_amount(value):
    """`value` as an integer of raw, or None if it does not spell one.

    Raw is an integer, and an integer has more than one spelling: "0500" and
    "500" are one amount. A node answers the canonical form, but `block_info` is
    handed in by the caller, so the padded form can reach this comparison.
    Nothing here raises: a value that is not an integer comes back as None and
    the caller refuses it, exactly as an unequal amount is refused. The digits
    have to be ASCII for that promise to hold - `"\u00b2".isdigit()` is True and
    `int("\u00b2")` raises, so `isdigit` alone would let a ValueError escape a
    money comparison.

    The length is bounded for the same promise. `int()` refuses a string of
    more than 4300 digits with ValueError (Python's integer-digit limit), and
    a node reply carrying `"amount": "9" * 5000` raised it out of
    `attach_block` and dropped the caller's connection. A Nano raw amount is
    at most 2**128 - 1, which is 39 significant digits; leading zeros are
    still read as before, and anything with more significant digits is not an
    amount any block can carry, so it comes back as None and is refused.
    """
    try:
        text = str(value).strip()
    except ValueError:
        # str() of an int past the digit limit raises too.
        return None
    if not text or not text.isascii() or not text.isdigit():
        return None
    # int() counts leading zeros toward its digit limit, so they go first.
    significant = text.lstrip("0") or "0"
    if len(significant) > MAX_RAW_DIGITS:
        return None
    amount = int(significant)
    return amount if amount <= MAX_RAW else None


# How deeply `linked` may nest. A job id or a URL needs one level.
MAX_LINKED_DEPTH = 16


def _depth(value):
    """How many levels of lists and objects `value` nests, without recursing."""
    deepest = 0
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        deepest = max(deepest, level)
        if deepest > MAX_LINKED_DEPTH:
            return deepest
        pending.extend((child, level + 1) for child in children)
    return deepest


def _shown_value(value):
    """A node-supplied `subtype` or `destination`, as a refusal's detail shows it.

    The node's answer is echoed back into a 409, and the node's answer is
    whatever the far end sent. A `subtype` or `link_as_account` nested a
    thousand lists deep parsed on Python 3.12 and then made `json.dumps` raise
    RecursionError while the refusal was being written, which dropped the
    caller's connection. Only a string or None is echoed as itself; anything
    else is named by its type. A string of more than MAX_SHOWN characters is
    clipped to its first MAX_SHOWN, followed by "... (N characters)" giving
    its full length.

    The node's `amount` is shown the same way. It used to be passed through
    `str()`, so a non-string amount was shown as its text rather than named,
    and an integer past Python's digit limit needed a special case of its own.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return "<a %s, not a string>" % type(value).__name__
    if len(value) > MAX_SHOWN:
        return "%s... (%d characters)" % (value[:MAX_SHOWN], len(value))
    return value


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
        if _depth(linked) > MAX_LINKED_DEPTH:
            # Stored and echoed as given, so it has to be writable as JSON. On
            # Python 3.12 and 3.13 a `linked` nested ~1000 deep parsed, was
            # appended, and then made `json.dump` raise RecursionError in
            # `Store.flush`: the caller's connection was dropped and the
            # receipt existed in memory but not on disk.
            raise LedgerError(
                400, "bad_linked",
                "'linked' is nested more than %d levels deep" % MAX_LINKED_DEPTH)

        receipt = {
            "id": new_receipt_id(),
            "kind": kind,
            "counterparty": counterparty,
            # The canonical `nano_` spelling, not the one the caller happened to
            # type. `totals` counts distinct addresses, so one account written
            # both ways would otherwise be counted as two counterparties.
            "counterparty_address": verdict["normalised"],
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
                # A hash has two spellings, upper and lower. BLOCK_HASH_RE
                # above admits only [0-9A-F]{64} and every stored hash came
                # through it, so both sides here are already upper case.
                # spelling-ok: forced upper case by BLOCK_HASH_RE at the door
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
                    "expected": "send", "got": _shown_value(block_info.get("subtype"))}
            paid = account_key(block_info.get("destination"))
            owed = account_key(receipt["counterparty_address"])
            if paid is None or owed is None or paid != owed:
                # A None on either side still refuses, so a block with no
                # destination is rejected exactly as it was before.
                mismatches["destination"] = {
                    "expected": receipt["counterparty_address"],
                    "got": _shown_value(block_info.get("destination")),
                }
            got_raw = raw_amount(block_info.get("amount_raw"))
            want_raw = raw_amount(receipt["amount_raw"])
            if got_raw is None or want_raw is None or got_raw != want_raw:
                mismatches["amount"] = {
                    "expected": receipt["amount_raw"], "got": _shown_value(block_info.get("amount_raw"))}
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
            # By account, not by spelling: a row stored before addresses were
            # canonicalised may carry the `xrb_` form of an account another
            # row carries as `nano_`, and that is one counterparty.
            "counterparties": len(
                {account_key(r["counterparty_address"]) or r["counterparty_address"]
                 for r in confirmed}),
            "first_receipt_at": created[0] if created else None,
        }
