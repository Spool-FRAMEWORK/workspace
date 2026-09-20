#!/usr/bin/env sh
# Clones the Spool repositories as siblings of this workspace folder.
# The module list is read from pom.xml, so it is defined in a single place.
# Existing clones are left untouched, so it is safe to run again.
#
#   ./bootstrap.sh                 clone every module on its develop branch
#   ./bootstrap.sh --with-devenv   also clone the observability stack
#   BRANCH=main ./bootstrap.sh     use another branch
set -eu

here=$(cd "$(dirname "$0")" && pwd)
parent=$(dirname "$here")
org="https://github.com/Spool-FRAMEWORK"
branch="${BRANCH:-develop}"
with_devenv=0

for arg in "$@"; do
    case "$arg" in
        --with-devenv) with_devenv=1 ;;
        -h|--help)     sed -n '2,8p' "$0"; exit 0 ;;
        *)             echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

modules=$(sed -n 's|.*<module>\.\./\([^<]*\)</module>.*|\1|p' "$here/pom.xml")

for name in $modules; do
    target="$parent/$name"
    if [ -d "$target/.git" ]; then
        echo "$name already cloned"
        continue
    fi
    echo "$name cloning ($branch)"
    git clone --branch "$branch" "$org/$name.git" "$target"
done

if [ "$with_devenv" -eq 1 ]; then
    target="$parent/devenv"
    if [ -d "$target/.git" ]; then
        echo "devenv already cloned"
    else
        echo "devenv cloning"
        git clone "$org/devenv.git" "$target"
    fi
fi

echo
echo "Done. Build everything with: ./build.sh"
