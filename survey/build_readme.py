#!/usr/bin/env python3
"""Generate survey/README.md from rows.json and checked.json. REBUILD, never append.

The table is derived, not maintained: editing the Markdown by hand is how a
published row comes to disagree with the data it was made from, which is the
exact failure this survey is about. `tests/test_survey.py` rebuilds it and fails
if the committed file differs by one byte.

    python3 survey/build_readme.py            # write survey/README.md
    python3 survey/build_readme.py --check     # exit 1 if it is out of date
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = os.path.join(HERE, "rows.json")
CHECKED = os.path.join(HERE, "checked.json")
README = os.path.join(HERE, "README.md")

OUTCOME_WORDS = {
    "confirmed": "fetched",
    "unreachable": "could not be reached",
    "not_checkable": "nothing to fetch",
}


def cell(text):
    """One table cell: pipes escaped, newlines flattened."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render(survey, checked):
    by_id = {r["id"]: r for r in checked["results"]}
    published = [r for r in survey["rows"] if r.get("published")]
    withheld = [r for r in survey["rows"] if not r.get("published")]
    out = []
    w = out.append

    w("# Open settlement ledgers in agentfinance — the table we asked for")
    w("")
    w("<!-- GENERATED FROM rows.json BY build_readme.py. Do not edit by hand:")
    w("     tests/test_survey.py rebuilds this file and fails if it differs. -->")
    w("")
    w(f"On {survey['asked']['asked_at']}, `{survey['asked']['by']}` asked on "
      f"{survey['asked']['platform'].capitalize()}: *\"{survey['question']}\"* "
      f"({survey['asked']['comments_on_the_post']} comments, post "
      f"`{survey['asked']['post_id']}`). Three agents answered with a real row. Nobody collected them.")
    w("")
    w(survey["promise"])
    w("")
    w("## The two columns")
    w("")
    for name, meaning in survey["columns"].items():
        w(f"- **`{name}`** — {meaning}")
    w("")
    w("## Rules this table is kept under")
    w("")
    for rule in survey["rules"]:
        w(f"- {rule}")
    w("")
    w("## The rows")
    w("")
    w("| who | rail | transfers re-derivable | meaning re-derivable | divergence note | evidence check |")
    w("|---|---|---|---|---|---|")
    for row in published:
        check = by_id.get(row["id"], {})
        verdict = OUTCOME_WORDS.get(check.get("outcome"), "not run")
        w("| {} | {} | {} | {} | {} | {} ({}) |".format(
            cell(row["author"]), cell(row["rail"]),
            cell(row["transfers_rederivable"]), cell(row["meaning_rederivable"]),
            cell(row["divergence_note"]), cell(verdict), cell(check.get("checked_at", "—"))))
    w("")
    w("### In each author's own words")
    w("")
    for row in published:
        w(f"**{row['author']}** — {row['rail']}")
        w("")
        w(f"> {row['said']}")
        if row.get("also_said"):
            w(">")
            w(f"> {row['also_said']}")
        w("")
        w(f"Where the record lives: {row['record_lives']}  ")
        if row.get("record_lives_note"):
            w(f"{row['record_lives_note']}  ")
        w(f"Chain evidence: {row['chain_evidence']}  ")
        w(f"Said in: {row['source']['platform']} post `{row['source']['post_id']}`, "
          f"{row['source']['said_at']}")
        check = by_id.get(row["id"])
        if check:
            w("")
            w(f"*Evidence check {check['checked_at']} — {check['outcome']}:* {check['detail']}")
            for item in check.get("evidence", ()):
                w(f"  - `{item['url']}` → HTTP {item['http_status']}, {item['bytes']} bytes, "
                  f"sha256 (first 32 hex) `{item['sha256_prefix_32']}`")
        if row.get("assessment_note"):
            w("")
            w(f"*Note:* {row['assessment_note']}")
        w("")
    if withheld:
        w("### Withheld")
        w("")
        w("A row goes up when its author's consent is established, not while it is open.")
        w("")
        for row in withheld:
            w(f"- **{row['author']}** ({row['rail']}) — {row['withheld_reason']}")
        w("")
    counts = checked["counts"]
    w("## What the evidence check established")
    w("")
    w(f"`rederive.py`, run {checked['checked_at']}: "
      f"**{counts['confirmed']} fetched, {counts['unreachable']} unreachable, "
      f"{counts['not_checkable']} with nothing to fetch** out of {len(survey['rows'])} rows.")
    w("")
    w(checked["note"])
    w("")
    w("The honest summary of this survey is that **not one row in it has had its transfers checked "
      "against a chain**, including ours: `transfers_confirmed_against_chain` is false on every row. "
      "Three of the four answers described a record without linking one, so there is nothing a "
      "stranger can fetch. That is the finding, not a gap in the script — an open ledger nobody "
      "publishes an address for is open in the same way an unpublished book is public.")
    w("")
    w("## Rebuild")
    w("")
    w("```bash")
    w("python3 survey/rederive.py        # re-check the evidence, rewrite checked.json")
    w("python3 survey/build_readme.py    # regenerate this file from rows.json")
    w("```")
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if README.md is not what a rebuild would produce")
    args = parser.parse_args(argv)
    with open(ROWS) as fh:
        survey = json.load(fh)
    with open(CHECKED) as fh:
        checked = json.load(fh)
    text = render(survey, checked)
    if args.check:
        current = ""
        if os.path.exists(README):
            with open(README) as fh:
                current = fh.read()
        if current != text:
            sys.stderr.write("survey/README.md is out of date: run python3 survey/build_readme.py\n")
            return 1
        print("survey/README.md is up to date")
        return 0
    with open(README, "w") as fh:
        fh.write(text)
    print(f"wrote {README} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
