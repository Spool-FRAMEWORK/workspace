#!/usr/bin/env sh
# Changes the version of a module and propagates it to the modules that depend on it.
#
#   ./bump.sh janitor 1.3.0-SNAPSHOT     set janitor's version and bump its dependents
#   ./bump.sh janitor 1.3.0 --dry-run    show the plan without touching any file
#   ./bump.sh janitor 1.3.0 --patch      bump the dependents by a patch whatever the change was
#   ./bump.sh --check                    verify that every dependency matches the module's real version
#
# The part of the version that grows in the module you change (major, minor or patch) is the part that
# grows in each of its dependents, and their dependency references are rewritten to match.
# Nothing is committed: review and commit each repository yourself.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
module=""
new=""
check=0
dry=0
yes=0
force_level=""

die() { echo "$*" >&2; exit 1; }

for arg in "$@"; do
    case "$arg" in
        --check)                check=1 ;;
        --dry-run)              dry=1 ;;
        --yes)                  yes=1 ;;
        --major|--minor|--patch) force_level=${arg#--} ;;
        -h|--help)              sed -n '2,10p' "$0"; exit 0 ;;
        -*)                     die "Unknown option: $arg" ;;
        *)
            if [ -z "$module" ]; then module="$arg"
            elif [ -z "$new" ]; then new="$arg"
            else die "Unexpected argument: $arg"
            fi ;;
    esac
done

modules=$(sed -n 's|.*<module>\.\./\([^<]*\)</module>.*|\1|p' "$here/pom.xml")

for m in $modules; do
    [ -f "$root/$m/pom.xml" ] || die "Module '$m' is not cloned next to the workspace. Run ./bootstrap.sh first."
done

# Prints "<artifactId> <version>" for every io.github.spool-framework groupId/artifactId/version
# triplet in a pom. The first one is the module itself, the rest are its dependencies.
triplets() {
    awk '
        /<groupId>io\.github\.spool-framework<\/groupId>/ { state = 1; next }
        state == 1 && /<artifactId>/ { art = $0; gsub(/.*<artifactId>|<\/artifactId>.*/, "", art); state = 2; next }
        state == 2 && /<version>/ { ver = $0; gsub(/.*<version>|<\/version>.*/, "", ver); print art, ver; state = 0; next }
        NF { state = 0 }
    ' "$1"
}

# Rewrites the version of every listed artifact, both where it is the module itself and where it is
# a dependency. $2 is a space separated list of artifactId=version.
apply_versions() {
    # awk handles CRLF differently on each platform, so strip the CRs, edit, and put them back
    # if the file had them. A file that ends up identical is left untouched.
    eol='\n'
    eol_bytes=1
    if [ "$(tr -cd '\r' < "$1" | wc -c)" -gt 0 ]; then eol='\r\n'; eol_bytes=2; fi
    tr -d '\r' < "$1" | awk -v map="$2" -v ors="$eol" '
        BEGIN { ORS = ors; n = split(map, kv, " "); for (i = 1; i <= n; i++) { split(kv[i], p, "="); newv[p[1]] = p[2] } }
        /<groupId>io\.github\.spool-framework<\/groupId>/ { state = 1; print; next }
        state == 1 && /<artifactId>/ { art = $0; gsub(/.*<artifactId>|<\/artifactId>.*/, "", art); state = 2; print; next }
        state == 2 && /<version>/ {
            if (art in newv) sub(/<version>[^<]*<\/version>/, "<version>" newv[art] "</version>")
            state = 0; print; next
        }
        NF { state = 0 }
        { print }
    ' > "$1.tmp"
    # awk always ends the last line with a newline; keep the file as it was if it had none
    if [ "$(tail -c 1 "$1" | od -An -tx1 | tr -d ' \n')" != "0a" ]; then
        head -c $(( $(wc -c < "$1.tmp") - eol_bytes )) "$1.tmp" > "$1.tmp2" && mv "$1.tmp2" "$1.tmp"
    fi
    if cmp -s "$1" "$1.tmp"; then rm "$1.tmp"; else mv "$1.tmp" "$1"; fi
}

version_of() { triplets "$root/$1/pom.xml" | head -1 | cut -d' ' -f2; }
base_of() { printf '%s' "${1%-SNAPSHOT}"; }
suffix_of() { case "$1" in *-SNAPSHOT) printf '%s' '-SNAPSHOT' ;; esac; }
is_semver() { printf '%s' "$(base_of "$1")" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; }

# Which part grew between two versions: major, minor, patch, none, or down when it went backwards.
level_of() {
    awk -v o="$(base_of "$1")" -v n="$(base_of "$2")" 'BEGIN {
        split(o, a, "."); split(n, b, ".")
        part[1] = "major"; part[2] = "minor"; part[3] = "patch"
        for (i = 1; i <= 3; i++) {
            if (b[i] + 0 > a[i] + 0) { print part[i]; exit }
            if (b[i] + 0 < a[i] + 0) { print "down"; exit }
        }
        print "none"
    }'
}

# Increments the given part of a version, resetting the parts to its right and keeping -SNAPSHOT.
bump_of() {
    awk -v v="$(base_of "$1")" -v s="$(suffix_of "$1")" -v l="$2" 'BEGIN {
        split(v, a, ".")
        if (l == "major") { a[1]++; a[2] = 0; a[3] = 0 } else if (l == "minor") { a[2]++; a[3] = 0 } else { a[3]++ }
        printf "%d.%d.%d%s\n", a[1], a[2], a[3], s
    }'
}

# Modules that depend, directly or not, on the given one. Maven works the order out from the poms.
dependents_of() {
    listing=$(sh "$here/mvnw" -B -f "$here/pom.xml" -pl ":$1" -amd validate 2>&1) || {
        printf '%s\n' "$listing" >&2
        exit 1
    }
    printf '%s\n' "$listing" \
        | sed -n 's|^\[INFO\] Building [^ ]*:\([^ :][^ :]*\) [^ ]*\( *\[[0-9]*/[0-9]*\]\)\{0,1\} *$|\1|p' \
        | grep -vx "$1" || true
}

run_check() {
    for m in $modules; do
        triplets "$root/$m/pom.xml" | awk -v m="$m" 'NR == 1 { print "own", m, $1, $2; next } { print "dep", m, $1, $2 }'
    done | awk '
        $1 == "own" { ver[$3] = $4; count++; next }
        $1 == "dep" { n++; dm[n] = $2; da[n] = $3; dv[n] = $4 }
        END {
            bad = 0
            for (i = 1; i <= n; i++)
                if ((da[i] in ver) && dv[i] != ver[da[i]]) {
                    printf "%s depends on %s %s, but %s is %s\n", dm[i], da[i], dv[i], da[i], ver[da[i]]
                    bad++
                }
            if (bad) exit 1
            printf "Versions are consistent across %d modules\n", count
        }'
}

if [ "$check" -eq 1 ]; then
    run_check || exit 1
    exit 0
fi

[ -n "$module" ] && [ -n "$new" ] || die "Usage: ./bump.sh <module> <new-version> [--dry-run] [--yes] [--patch|--minor|--major]   or   ./bump.sh --check"
printf '%s\n' "$modules" | grep -qx "$module" || die "Unknown module '$module'. Known modules: $(printf '%s' "$modules" | tr '\n' ' ')"
is_semver "$new" || die "'$new' is not a version like 1.3.0 or 1.3.0-SNAPSHOT"

old=$(version_of "$module")
level=$(level_of "$old" "$new")
case "$level" in
    none) die "$module is already at $old: $new does not change any part of the version" ;;
    down) die "$new is lower than the current version of $module ($old)" ;;
esac
dep_level=${force_level:-$level}

map="$module=$new"
plan=$(printf '%-16s %-18s -> %-18s %s' "$module" "$old" "$new" "the module you changed ($level)")
for d in $(dependents_of "$module"); do
    cur=$(version_of "$d")
    nv=$(bump_of "$cur" "$dep_level")
    map="$map $d=$nv"
    plan=$(printf '%s\n%-16s %-18s -> %-18s %s' "$plan" "$d" "$cur" "$nv" "depends on it ($dep_level)")
done

echo "Plan:"
printf '%s\n' "$plan"

if [ "$dry" -eq 1 ]; then
    echo "Dry run: no file was changed."
    exit 0
fi

if [ "$yes" -eq 0 ]; then
    printf 'Apply? [y/N] '
    read -r answer
    case "$answer" in y|Y|yes|s|S|si) ;; *) echo "Aborted."; exit 1 ;; esac
fi

for m in $modules; do
    apply_versions "$root/$m/pom.xml" "$map"
done

echo
run_check || die "The versions are still inconsistent after applying the plan."
echo
echo "Changed files (nothing was committed):"
for m in $modules; do
    git -C "$root/$m" diff --stat -- pom.xml 2>/dev/null || true
done
