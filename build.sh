#!/usr/bin/env sh
# Builds Spool modules in dependency order through the workspace reactor.
#
#   ./build.sh                     build and install everything
#   ./build.sh infrastructure      that module, what it depends on, and what depends on it
#   ./build.sh core --tests        same, running the tests too
#   ./build.sh dsl --fast          skip rebuilding upstream modules (they must already be in ~/.m2)
#   ./build.sh dsl --no-install    package only, leave ~/.m2 untouched
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
    set -- "$@" -pl ":$module" -amd
    if [ "$fast" -eq 0 ]; then set -- "$@" -am; fi
fi

echo "mvnw $*"
exec sh "$here/mvnw" "$@"
