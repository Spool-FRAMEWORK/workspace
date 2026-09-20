#!/usr/bin/env sh
# Builds Spool modules in dependency order through the workspace reactor.
#
#   ./build.sh                     build and install everything
#   ./build.sh infrastructure      that module, what depends on it, and whatever is needed to build them
#   ./build.sh core --tests        same, running the tests too
#   ./build.sh dsl --fast          skip rebuilding upstream modules (they must already be in ~/.m2)
#   ./build.sh dsl --no-install    package only, leave ~/.m2 untouched
#
# The scope of a module is worked out in two passes: its dependents can need other modules too
# (infrastructure needs janitor, mounter and ingester), so first the module and its dependents are listed,
# then they are built together with everything upstream of them.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
module=""
tests=0
fast=0
install=1

for arg in "$@"; do
    case "$arg" in
        --tests)      tests=1 ;;
        --fast)       fast=1 ;;
        --no-install) install=0 ;;
        -h|--help)    sed -n '2,8p' "$0"; exit 0 ;;
        -*)           echo "Unknown option: $arg" >&2; exit 2 ;;
        *)            module="$arg" ;;
    esac
done

goal=install
if [ "$install" -eq 0 ]; then goal=package; fi

set -- -f "$here/pom.xml" -T 1C "$goal"
if [ "$tests" -eq 0 ]; then set -- "$@" -DskipTests; fi
if [ -n "$module" ]; then
    if [ "$fast" -eq 1 ]; then
        set -- "$@" -pl ":$module" -amd
    else
        listing=$(sh "$here/mvnw" -B -f "$here/pom.xml" -pl ":$module" -amd validate 2>&1) || {
            printf '%s\n' "$listing" >&2
            exit 1
        }
        scope=$(printf '%s\n' "$listing" \
            | sed -n 's|^\[INFO\] Building [^ ]*:\([^ :][^ :]*\) [^ ]*\( *\[[0-9]*/[0-9]*\]\)\{0,1\} *$|:\1|p' \
            | paste -sd, -)
        if [ -z "$scope" ]; then
            echo "Could not work out which modules to build for '$module'" >&2
            exit 1
        fi
        set -- "$@" -pl "$scope" -am
    fi
fi

echo "mvnw $*"
exec sh "$here/mvnw" "$@"
