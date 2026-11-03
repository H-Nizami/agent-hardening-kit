#!/usr/bin/env bash
# Repo hygiene gate. Fails if:
#   1. the blocked employer string appears in any tracked, staged or working-tree file;
#   2. a co-author trailer appears in any file;
#   3. the README headline does not mark unmeasured numbers with 'Not yet measured';
#   4. any commit message contains the co-author trailer.
# Blocked strings are built from parts so this file does not match itself.
set -euo pipefail
cd "$(dirname "$0")/.."

blocked_stem="ch""kk"
trailer="co-authored""-by"
fail=0

in_git() { git rev-parse --git-dir >/dev/null 2>&1; }

# Rendered SVGs embed fonts and icons as base64 data URIs; a short case-insensitive
# pattern can match base64 by chance, so data URIs are stripped before grepping them.
rendered_svg='docs/img/*.svg'
strip_data_uris() { sed -E 's#data:[a-z/+.-]+;base64,[A-Za-z0-9+/=]+##g'; }

grep_svgs() {
  local pattern="$1" f
  for f in $rendered_svg; do
    [ -f "$f" ] || continue
    strip_data_uris <"$f" | grep -n -i -F -e "$pattern" | cut -d: -f1 | sed "s|^|$f:|" || true
  done
  if in_git; then
    git ls-files --cached -- "$rendered_svg" | while read -r f; do
      git show ":$f" | strip_data_uris | grep -n -i -F -e "$pattern" | cut -d: -f1 | sed "s|^|$f:|" || true
    done
  fi
}

find_string() {
  local pattern="$1" label="$2" hits
  hits=$(
    {
      if in_git; then
        git grep --cached -n -i -I -F -e "$pattern" -- . ":(exclude)$rendered_svg" 2>/dev/null || true
      fi
      grep -rIn -i -F --exclude-dir=.git --exclude-dir=.bin --exclude-dir=.venv \
        --exclude-dir=.cache --exclude-dir=.terraform --exclude-dir=__pycache__ \
        --exclude-dir=.pytest_cache -e "$pattern" . 2>/dev/null |
        grep -v '^\./docs/img/[^/:]*\.svg:' || true
      grep_svgs "$pattern"
    } | cut -d: -f1,2 | sed 's|^\./||' | sort -u
  )
  if [ -n "$hits" ]; then
    echo "check-hygiene: $label found at:" >&2
    echo "$hits" >&2
    fail=1
  fi
}

find_string "$blocked_stem" "blocked employer string"
find_string "$trailer" "co-author trailer"

headline=$(grep -n -m1 '^> \*\*Result:\*\*' README.md || true)
if [ -z "$headline" ]; then
  echo "check-hygiene: README.md has no '> **Result:**' headline" >&2
  fail=1
elif ! grep -qF 'Not yet measured' <<<"$headline"; then
  echo "check-hygiene: README.md:${headline%%:*} headline does not mark unmeasured numbers with 'Not yet measured'" >&2
  fail=1
fi

if in_git && [ -n "$(git rev-list --all -n1 2>/dev/null)" ]; then
  if git log --all --format=%B | grep -qiF "$trailer"; then
    echo "check-hygiene: a commit message contains the co-author trailer" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "check-hygiene: ok"
