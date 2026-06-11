#!/usr/bin/env bash
# Pre-tag release gate: for a given vX.Y.Z, assert the target commit agrees
# on the version everywhere a version lives. Run before pushing ANY release
# tag (release plan Phases 2 and 10):
#
#   scripts/release_check.sh v0.9.0 [commit-ish]   # default commit: HEAD
#
# Checks, all against <commit-ish> (not the working tree):
#   1. pyproject.toml  [project] version == X.Y.Z
#   2. uv.lock         onoats package entry version == X.Y.Z
#   3. Info.plist      CFBundleShortVersionString == X.Y.Z (menu-bar app)
#   4. CHANGELOG.md    has a "## [X.Y.Z] - YYYY-MM-DD" entry
set -euo pipefail

tag="${1:?usage: release_check.sh vX.Y.Z [commit-ish]}"
commit="${2:-HEAD}"

if [[ "$tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    version="${BASH_REMATCH[1]}"
else
    echo "FAIL: tag '$tag' is not of the form vX.Y.Z" >&2
    exit 1
fi

fail=0
check() { # <label> <expected-marker-found 0|1> <detail>
    if [ "$2" -eq 1 ]; then
        echo "ok:   $1"
    else
        echo "FAIL: $1 — $3" >&2
        fail=1
    fi
}

# Capture each file once; parsing a variable avoids SIGPIPE killing the
# `git show` pipeline under pipefail when a parser exits early.
fetch() {
    git show "${commit}:$1" || {
        echo "FAIL: $1 not readable at ${commit} — cannot verify release" >&2
        exit 1
    }
}
pyproject=$(fetch pyproject.toml)
lockfile=$(fetch uv.lock)
plist=$(fetch native/onoats-menubar/Info.plist)
changelog=$(fetch CHANGELOG.md)

py_ver=$(sed -n 's/^version = "\(.*\)"$/\1/p' <<<"$pyproject" | sed -n 1p)
check "pyproject.toml version == $version" \
    "$([ "$py_ver" = "$version" ] && echo 1 || echo 0)" \
    "found '$py_ver'"

lock_ver=$(awk '
    /^\[\[package\]\]$/ { found = 0 }
    /^name = "onoats"$/ { found = 1; next }
    found && /^version = / { gsub(/version = |"/, ""); print; exit }
' <<<"$lockfile")
check "uv.lock onoats version == $version" \
    "$([ "$lock_ver" = "$version" ] && echo 1 || echo 0)" \
    "found '$lock_ver'"

plist_ver=$(awk '/CFBundleShortVersionString/ { getline; gsub(/[ \t]*<\/?string>/, ""); print; exit }' <<<"$plist")
check "Info.plist CFBundleShortVersionString == $version" \
    "$([ "$plist_ver" = "$version" ] && echo 1 || echo 0)" \
    "found '$plist_ver'"

if grep -Eq "^## \[$(printf '%s' "$version" | sed 's/\./\\./g')\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" <<<"$changelog"; then
    check "CHANGELOG.md entry for $version" 1 ""
else
    check "CHANGELOG.md entry for $version" 0 "no '## [$version] - YYYY-MM-DD' heading"
fi

if [ "$fail" -ne 0 ]; then
    echo "release_check: NOT safe to tag $tag at $commit" >&2
    exit 1
fi
echo "release_check: $tag at $commit is consistent — safe to tag"
