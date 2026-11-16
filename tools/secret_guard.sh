#!/usr/bin/env bash
# No token, seed or key may be committed.
#
# Run from the repository root:  bash -e tools/secret_guard.sh
# Exit 0 means the tree is clean; exit 1 prints the offending lines.
#
# This was the secret-guard step of the GitHub Actions workflow, which has been
# removed (no GitHub Actions). It lives here unchanged so it can still be run by
# hand, and tests/test_ledger.py runs this file itself, against the tracked
# tree and against fixtures, so the guard and its tests cannot drift apart.

if grep -rInP '(?<![0-9A-Za-z])[0-9a-fA-F]{64}(?![0-9A-Za-z])' \
     --include='*.py' --include='*.md' --include='*.json' --include='*.yml' . ; then
  echo "64 hex characters standing alone: if that is a key or a seed it must not be here"
  exit 1
fi
# The value must not START with `$`: `RECEIPT_LEDGER_TOKEN="$(python3 -c
# 'import secrets;print(secrets.token_urlsafe(32))')"` is the README's own
# run command, which GENERATES a fresh token and commits nothing. Without
# that exclusion this step failed on the commit that created the
# repository, and stayed red for 30 hours: the only CI run this repo ever
# had was a false positive on its own documentation. A guard that cries
# wolf on the README is a guard whose next real finding nobody reads.
#
# Deliberately quoted-only. Extending it to unquoted assignments also
# flags e2e_check.py's env=dict(os.environ, RECEIPT_LEDGER_TOKEN=TOKEN)
# and serve.py's own usage line - correct code, flagged - which is the
# same cry-wolf failure in a new place. tests/test_ledger.py runs this
# step against the tracked tree so a false positive fails the suite.
if grep -rIn 'RECEIPT_LEDGER_TOKEN[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"'$]' \
     --include='*.py' --include='*.md' --include='*.yml' . ; then
  echo "a literal token value is assigned in a tracked file"
  exit 1
fi
