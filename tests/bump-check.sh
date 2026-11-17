#!/usr/bin/env sh
# Checks bump.sh --check and --align against a fake workspace and a fake Central. Run it from anywhere:
# sh tests/bump-check.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
GIT_CONFIG_GLOBAL="$scratch/gitconfig"
GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM
trap 'rm -rf "$scratch"' EXIT

failures=0
fail() {
    echo "FAIL $case_name: $*"
    failures=$((failures + 1))
}

module_pom() {
    dependencies=""
    if [ -n "${3:-}" ]; then
        dependencies="
    <dependencies>
        <dependency>
            <groupId>io.github.spool-framework</groupId>
            <artifactId>$3</artifactId>
            <version>$4</version>
        </dependency>
    </dependencies>"
    fi
    cat <<EOF
<project>
    <groupId>io.github.spool-framework</groupId>
    <artifactId>$1</artifactId>
    <version>$2</version>$dependencies
</project>
EOF
}

commit_all() {
    git -C "$1" add -A
    git -C "$1" -c user.name=test -c user.email=test@example.invalid commit -q -m "$2"
}

publish_on_central() {
    folder="$central/io/github/spool-framework/$1"
    mkdir -p "$folder"
    {
        echo "<metadata><versioning><versions>"
        shift
        for version in "$@"; do echo "<version>$version</version>"; done
        echo "</versions></versioning></metadata>"
    } > "$folder/maven-metadata.xml"
}

# core 1.2.0-SNAPSHOT and janitor 1.2.0-SNAPSHOT depending on it, both tagged v1.2.0 and on Central,
# with nothing committed after the tags.
new_world() {
    case_name=$1
    world="$scratch/$1"
    central="$world/central"
    mkdir -p "$world/workspace" "$central"
    cp "$here/bump.sh" "$world/workspace/bump.sh"
    cat > "$world/workspace/pom.xml" <<EOF
<project>
    <modules>
        <module>../core</module>
        <module>../janitor</module>
    </modules>
</project>
EOF
    for module in core janitor; do
        mkdir -p "$world/$module"
        git -C "$world/$module" init -q
    done
    module_pom core 1.2.0-SNAPSHOT > "$world/core/pom.xml"
    module_pom janitor 1.2.0-SNAPSHOT core 1.2.0-SNAPSHOT > "$world/janitor/pom.xml"
    for module in core janitor; do
        commit_all "$world/$module" "chore: release 1.2.0"
        git -C "$world/$module" tag v1.2.0
        publish_on_central "$module" 1.1.0 1.2.0
    done
    central_url="file://$(cd "$central" && { pwd -W 2>/dev/null || pwd; })"
}

# Gives a module a cucumber.version property, without committing it.
uses_cucumber() {
    pom="$world/$1/pom.xml"
    sed "s|</project>|<properties><cucumber.version>$2</cucumber.version></properties></project>|" "$pom" > "$pom.new"
    mv "$pom.new" "$pom"
}

run_bump() {
    output=$(SPOOL_CENTRAL_URL="$central_url" sh "$world/workspace/bump.sh" "$@" 2>&1)
    status=$?
}

expect_success() {
    [ "$status" -eq 0 ] || fail "expected success, got exit code $status: $output"
}

expect_failure_saying() {
    [ "$status" -ne 0 ] || fail "expected a failure, it passed: $output"
    printf '%s' "$output" | grep -qF "$1" || fail "expected the output to say '$1', got: $output"
}

new_world consistent
run_bump --check
expect_success

new_world dependency_behind
module_pom core 1.3.0-SNAPSHOT > "$world/core/pom.xml"
run_bump --check
expect_failure_saying "janitor depends on core 1.2.0-SNAPSHOT, but core is 1.3.0-SNAPSHOT"

new_world commits_after_published_version
echo "change" > "$world/janitor/change.txt"
commit_all "$world/janitor" "feat: change after the release"
run_bump --check
expect_failure_saying "janitor has 1 commit(s) after v1.2.0, and 1.2.0 is already on Central"

new_world published_version_without_tag
git -C "$world/core" tag -d v1.2.0 > /dev/null
run_bump --check
expect_failure_saying "core is at 1.2.0-SNAPSHOT, which is on Central, but its clone has no tag v1.2.0"

new_world unpublished_version_with_commits
module_pom janitor 1.3.0-SNAPSHOT core 1.2.0-SNAPSHOT > "$world/janitor/pom.xml"
commit_all "$world/janitor" "chore: start 1.3.0"
run_bump --check
expect_success

new_world central_unreachable
central_url="file://$scratch/missing-central"
rm -rf "$central"
run_bump --check
expect_failure_saying "Cannot read the versions of core on Central"

new_world cucumber_same_version
uses_cucumber core 7.20.1
uses_cucumber janitor 7.20.1
run_bump --check
expect_success

new_world cucumber_only_in_some_modules
uses_cucumber janitor 7.20.1
run_bump --check
expect_success

new_world cucumber_versions_differ
uses_cucumber core 7.20.1
uses_cucumber janitor 7.18.0
run_bump --check
expect_failure_saying "Modules use different Cucumber versions: core 7.20.1, janitor 7.18.0"

tab=$(printf '\t')

new_world porcelain_consistent
run_bump --check --porcelain
expect_success
[ -z "$output" ] || fail "expected no output when everything is fine, got: $output"

new_world porcelain_names_the_module_of_each_problem
module_pom core 1.3.0-SNAPSHOT > "$world/core/pom.xml"
echo "change" > "$world/janitor/change.txt"
commit_all "$world/janitor" "feat: change after the release"
run_bump --check --porcelain
expect_failure_saying "janitor${tab}janitor depends on core 1.2.0-SNAPSHOT, but core is 1.3.0-SNAPSHOT"
expect_failure_saying "janitor${tab}janitor has 1 commit(s) after v1.2.0, and 1.2.0 is already on Central"
[ "$(printf '%s\n' "$output" | grep -vc "$tab")" -eq 0 ] || fail "expected only problem lines, got: $output"

new_world porcelain_problem_of_no_module
uses_cucumber core 7.20.1
uses_cucumber janitor 7.18.0
run_bump --check --porcelain
[ "$status" -ne 0 ] || fail "expected a failure, it passed: $output"
[ "$output" = "${tab}Modules use different Cucumber versions: core 7.20.1, janitor 7.18.0" ] \
    || fail "expected the Cucumber problem with an empty module, got: $output"

new_world align_dry_run
module_pom core 1.3.0-SNAPSHOT > "$world/core/pom.xml"
before=$(cat "$world/janitor/pom.xml")
run_bump --align --dry-run
expect_success
printf '%s' "$output" | grep -qF "janitor: core 1.2.0-SNAPSHOT -> 1.3.0-SNAPSHOT" \
    || fail "expected the dry run to list the change, got: $output"
[ "$(cat "$world/janitor/pom.xml")" = "$before" ] || fail "the dry run changed janitor/pom.xml"

new_world align
module_pom core 1.3.0-SNAPSHOT > "$world/core/pom.xml"
run_bump --align
expect_success
grep -qF "<version>1.3.0-SNAPSHOT</version>" "$world/janitor/pom.xml" \
    || fail "janitor/pom.xml does not depend on core 1.3.0-SNAPSHOT: $(cat "$world/janitor/pom.xml")"
grep -qF "<version>1.2.0-SNAPSHOT</version>" "$world/janitor/pom.xml" \
    || fail "janitor lost its own version: $(cat "$world/janitor/pom.xml")"
run_bump --check
expect_success

if [ "$failures" -eq 0 ]; then
    echo "bump.sh --check and --align: all checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
