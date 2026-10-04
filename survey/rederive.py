#!/usr/bin/env python3
"""Check the evidence each survey row names, and record what was actually established.

The survey asked other agents whether their settlement ledger can be
re-derived. Publishing their answers and marking them "verified" on their own
word would be the failure the question is about. So this script fetches what a
row names, writes down what came back, and says plainly when it could not look.

Three outcomes, and nothing else is ever written:

  confirmed     the named document was fetched, and what was established is
                stated exactly - usually "this document exists at this URL and
                its bytes digest to X on this date", which is NOT the same as
                "the transfer happened".
  unreachable   the fetch was attempted and failed. The reason is recorded, and
                a denial by this machine's own network policy is distinguished
                from a 404, because they mean opposite things about the row.
  not_checkable the row names no public endpoint at all. Most rows in this
                survey are this, because most answers in the thread described a
                record without linking one.

`transfers_confirmed_against_chain` stays false unless a chain RPC was actually
queried and answered, which no row in this survey yet supports: a reachable
gist or receipt file is a document, not a ledger read.

    python3 survey/rederive.py                 # check and write survey/checked.json
    python3 survey/rederive.py --offline        # record every row as unreachable
    python3 survey/rederive.py --print          # show the result without writing
"""
import argparse
import datetime
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = os.path.join(HERE, "rows.json")
CHECKED = os.path.join(HERE, "checked.json")
TIMEOUT_S = 20
MAX_BYTES = 1 << 20


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def fetch(url, opener=None):
    """GET `url`. Returns (status, body) or raises. Read-only by construction:
    no method, header or body is settable from a row."""
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "nano-receipt-ledger-survey/1 (+https://github.com/dhyabi2/nano-receipt-ledger)",
        "Accept": "*/*",
    })
    open_it = opener or urllib.request.urlopen
    with open_it(request, timeout=TIMEOUT_S) as response:
        return response.status, response.read(MAX_BYTES)


def check_row(row, offline=False, opener=None):
    """What was established about one row, as of today. Never 'verified'."""
    result = {
        "id": row["id"],
        "checked_at": today(),
        "transfers_confirmed_against_chain": False,
        "evidence": [],
    }
    if not row.get("published", False):
        result["outcome"] = "not_checkable"
        result["detail"] = ("the row is not published, so nothing is checked - including its evidence: "
                            "fetching what a withheld row names would be acting on it. Reason: "
                            + row.get("withheld_reason", "none recorded"))
        return result
    url = row.get("record_lives", "")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        result["outcome"] = "not_checkable"
        result["detail"] = ("the row names no public endpoint: what it describes is "
                            + (row.get("record_lives") or "nothing") + ". An author's description of "
                            "their own record is not evidence about it, and is not treated as any.")
        return result
    if offline:
        result["outcome"] = "unreachable"
        result["detail"] = "--offline: no request was made"
        return result
    try:
        status, body = fetch(url, opener=opener)
    except urllib.error.HTTPError as e:
        result["outcome"] = "unreachable"
        result["detail"] = f"HTTP {e.code} from {url}: the document a stranger is pointed at did not answer"
        return result
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        reason = getattr(e, "reason", e)
        # A refusal by the machine doing the checking says nothing about the row.
        # Conflating it with a 404 would publish a false negative about someone
        # else's ledger, which is worse than publishing no answer.
        blocked = any(token in str(reason).lower() for token in
                      ("forbidden", "407", "403", "proxy", "tunnel", "denied", "certificate"))
        result["outcome"] = "unreachable"
        result["detail"] = (f"{type(e).__name__}: {reason}. "
                            + ("This looks like a refusal by the network this check ran on, not by the "
                               "publisher - it is not evidence against the row."
                               if blocked else "The endpoint did not answer."))
        return result
    result["outcome"] = "confirmed"
    result["evidence"].append({
        "url": url,
        "http_status": status,
        "bytes": len(body),
        # The first 32 hex characters of the sha256, deliberately, and the field
        # is named for it. This repository's CI refuses ANY standalone 64-hex
        # string in a tracked file, because at a glance a seed and a digest are
        # the same shape - and weakening that guard so a survey can print a
        # digest would be the wrong trade. 128 bits answers the only question
        # this field is for ("are these the same bytes I read on that date"),
        # and a truncated digest cannot be mistaken for a key.
        "sha256_prefix_32": hashlib.sha256(body).hexdigest()[:32],
    })
    result["detail"] = (
        f"the document at {url} answered {status} with {len(body)} bytes on {result['checked_at']}. "
        "That establishes the record exists and is public, and nothing more: no chain was read, so "
        "whether the transfers it describes occurred is still unchecked here.")
    return result


def run(offline=False, opener=None, rows_path=ROWS):
    with open(rows_path) as fh:
        survey = json.load(fh)
    results = [check_row(row, offline=offline, opener=opener) for row in survey["rows"]]
    return {
        "schema": "nano-receipt-ledger/survey/checked/v1",
        "checked_at": today(),
        "note": ("Outcomes are confirmed | unreachable | not_checkable. Nothing is marked verified on "
                 "an author's word, and `transfers_confirmed_against_chain` is false on every row "
                 "until a chain RPC is actually queried."),
        "results": results,
        "counts": {
            outcome: sum(1 for r in results if r["outcome"] == outcome)
            for outcome in ("confirmed", "unreachable", "not_checkable")
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true",
                        help="make no request; record every published row as unreachable")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the result instead of writing survey/checked.json")
    args = parser.parse_args(argv)
    report = run(offline=args.offline)
    text = json.dumps(report, indent=2) + "\n"
    if args.print_only:
        sys.stdout.write(text)
    else:
        with open(CHECKED, "w") as fh:
            fh.write(text)
        print(f"wrote {CHECKED}: {report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
