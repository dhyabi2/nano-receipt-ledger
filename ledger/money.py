"""Exact XNO <-> raw conversion. Integers only; no float touches money.

1 XNO = 10**30 raw. A float carries ~15-17 significant digits, so a single
float anywhere in this path silently loses the bottom 13+ digits of every
amount. Both functions below are copied verbatim from
`tools/nano_wallet/wallet.py` in dhyabi2/swarm-decisions, which is the
source of truth for them; they are vendored here so this repository has no
dependency of any kind, including on that one.
"""

RAW_PER_XNO = 10 ** 30


def xno_to_raw(amount: str) -> int:
    """Exact decimal XNO -> integer raw. No float is used anywhere."""
    text = str(amount).strip()
    if not text:
        raise ValueError("amount is empty")
    negative = text.startswith("-")
    if negative:
        raise ValueError("amount must not be negative")
    if text.count(".") > 1:
        raise ValueError("amount is not a decimal number: %r" % amount)
    whole, _, frac = text.partition(".")
    whole = whole or "0"
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError("amount is not a decimal number: %r" % amount)
    if len(frac) > 30:
        raise ValueError("Nano has 30 decimal places; %d were given" % len(frac))
    return int(whole) * RAW_PER_XNO + int(frac.ljust(30, "0") or 0)


def raw_to_xno(raw: int) -> str:
    """Integer raw -> exact decimal XNO string, no trailing-zero noise."""
    if not isinstance(raw, int):
        raise ValueError("raw must be an integer, got %s" % type(raw).__name__)
    if raw < 0:
        raise ValueError("raw must not be negative")
    whole, frac = divmod(raw, RAW_PER_XNO)
    if frac == 0:
        return str(whole)
    return "%d.%s" % (whole, str(frac).zfill(30).rstrip("0"))


DISPLAY_PLACES = 6


class AmountError(ValueError):
    """An amount that cannot be represented exactly at display precision."""


def parse_xno(amount) -> int:
    """Decimal XNO string -> integer raw, rejecting anything we cannot render back.

    The ledger publishes amounts at six decimal places. Accepting an amount
    with more precision than that would mean publishing a number that is not
    the number we paid, so it is refused at the door instead.
    """
    if not isinstance(amount, str):
        raise AmountError("amount_xno must be a string, got %s" % type(amount).__name__)
    text = amount.strip()
    whole, _, frac = text.partition(".")
    if len(frac) > DISPLAY_PLACES:
        raise AmountError(
            "amount_xno carries %d decimal places; this ledger publishes %d, and it "
            "will not display an amount that is not the amount paid"
            % (len(frac), DISPLAY_PLACES)
        )
    try:
        raw = xno_to_raw(text)
    except ValueError as exc:
        raise AmountError(str(exc)) from None
    if raw <= 0:
        raise AmountError("amount_xno must be greater than zero")
    return raw


def format_xno(raw: int) -> str:
    """Integer raw -> the exact six-decimal-place string the ledger publishes."""
    if not isinstance(raw, int) or raw < 0:
        raise AmountError("raw must be a non-negative integer")
    scale = 10 ** (30 - DISPLAY_PLACES)
    if raw % scale:
        raise AmountError(
            "%d raw cannot be rendered exactly at %d decimal places" % (raw, DISPLAY_PLACES)
        )
    units = raw // scale
    whole, frac = divmod(units, 10 ** DISPLAY_PLACES)
    return "%d.%0*d" % (whole, DISPLAY_PLACES, frac)
