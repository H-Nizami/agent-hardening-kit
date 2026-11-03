#!/usr/bin/env bash
# Fail if any commit (all refs) has an author/committer email on the blocked
# employer domain, or a commit message with a co-author trailer.
# Blocked strings are built from parts so this file never matches itself.
set -euo pipefail
cd "$(dirname "$0")/.."

domain="ch""kk.io"
trailer="co-authored""-by"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "author-check: not a git repository" >&2
  exit 1
fi
if [ -z "$(git rev-list --all --max-count=1 2>/dev/null)" ]; then
  echo "author-check: no commits yet, ok"
  exit 0
fi

fail=0
while read -r sha author committer; do
  a=$(tr '[:upper:]' '[:lower:]' <<<"$author")
  c=$(tr '[:upper:]' '[:lower:]' <<<"$committer")
  # Match the domain itself and any subdomain of it, never a look-alike suffix.
  if [[ "$a" == *"@$domain" || "$a" == *".$domain" || "$c" == *"@$domain" || "$c" == *".$domain" ]]; then
    echo "author-check: blocked email domain in $sha" >&2
    fail=1
  fi
done < <(git log --all --format='%H %ae %ce')

while read -r sha; do
  [ -n "$sha" ] || continue
  echo "author-check: co-author trailer in $sha" >&2
  fail=1
done < <(git log --all -i --grep="$trailer" --format='%H')

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "author-check: ok"
