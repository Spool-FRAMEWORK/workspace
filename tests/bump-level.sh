#!/usr/bin/env sh
# Checks how bump.sh decides which part of a version grew. Run it from anywhere: sh tests/bump-level.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)

# Load only the two functions under test, so the script is not run.
eval "$(sed -n '/^base_of()/p' "$here/bump.sh")"
eval "$(sed -n '/^level_of()/,/^}/p' "$here/bump.sh")"

failures=0
check() {
    got=$(level_of "$1" "$2")
    if [ "$got" != "$3" ]; then
        echo "FAIL level_of $1 -> $2: expected $3, got $got"
        failures=$((failures + 1))
    fi
}

check 1.2.0-SNAPSHOT 1.2.1-SNAPSHOT patch
check 1.2.1-SNAPSHOT 1.3.0-SNAPSHOT minor
check 1.2.9 2.0.0 major
check 1.9.9 1.10.0 minor
check 1.2.1-SNAPSHOT 1.2.0-SNAPSHOT down
check 1.3.0 1.2.9 down
check 2.0.0 1.9.9 down
check 1.2.3 1.2.3 none
check 1.2.3-SNAPSHOT 1.2.3 none

if [ "$failures" -eq 0 ]; then
    echo "bump.sh version comparison: all checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
