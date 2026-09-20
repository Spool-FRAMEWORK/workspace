#!/usr/bin/env python3
"""Plans, and runs, the release of the Spool modules to Maven Central.

    python3 tools/release.py plan       what would be released, in which order, and what is wrong
    python3 tools/release.py run --yes  release every pending module, one after the other

A module is pending when the base version of its pom on main, without -SNAPSHOT, is not on Central yet.
Modules are ordered so that each one comes after the ones it depends on: a module published against a
version that does not exist yet would be broken for good, because Central does not let a version be
published again.

Nothing is waited for by guessing a time. After starting the release workflow of a module it waits for
the workflow to end, then for the version to appear on Central, then checks that it can be resolved the
way a consumer would. If any of those fails, nothing that depends on the module is released.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ORG = "Spool-FRAMEWORK"
GROUP = "io.github.spool-framework"
CENTRAL = "https://repo1.maven.org/maven2"
GROUP_PATH = "io/github/spool-framework"
RELEASE_BRANCH = "main"
DEVELOPMENT_BRANCH = "develop"

# Modules that are not published to Maven Central, and why.
NOT_ON_CENTRAL = {"watchdog": "it is published as a Docker image"}

# Settings that send every repository to Central, so a version is resolved the way a consumer would.
_CENTRAL_ONLY = (
    "<settings><mirrors><mirror><id>central-only</id><mirrorOf>*</mirrorOf>"
    f"<url>{CENTRAL}</url></mirror></mirrors></settings>"
)

_TRIPLET = re.compile(
    r"<groupId>io\.github\.spool-framework</groupId>\s*<artifactId>([^<]+)</artifactId>\s*<version>([^<]+)</version>"
)
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Returns the text at a URL, or None when there is nothing there.
Fetch = Callable[[str], Optional[str]]


def base_version(version: str) -> str:
    return version[: -len("-SNAPSHOT")] if version.endswith("-SNAPSHOT") else version


def parse_pom(xml: str) -> tuple[str, str, list[tuple[str, str]]]:
    """The module itself and the Spool modules it depends on, each as (artifactId, version)."""
    found = _TRIPLET.findall(xml)
    if not found:
        raise ValueError("no io.github.spool-framework module in the pom")
    (artifact, version), dependencies = found[0], found[1:]
    return artifact, version, list(dependencies)


def modules_of(workspace_pom: str) -> list[str]:
    return re.findall(r"<module>\.\./([^<]+)</module>", workspace_pom)


def pom_url(module: str, branch: str) -> str:
    return f"https://raw.githubusercontent.com/{ORG}/{module}/{branch}/pom.xml"


def metadata_url(module: str) -> str:
    return f"{CENTRAL}/{GROUP_PATH}/{module}/maven-metadata.xml"


def http_fetch(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def http_exists(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30):
            return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def central_versions(module: str, fetch: Fetch) -> set[str]:
    metadata = fetch(metadata_url(module))
    return set(re.findall(r"<version>([^<]+)</version>", metadata)) if metadata else set()


# --------------------------------------------------------------------------------------------- plan

@dataclass
class Entry:
    module: str
    version: str                                  # in the pom on main
    published: set[str]                           # already on Central
    dependencies: list[tuple[str, str]]           # (module, base version) it asks for
    develop_version: Optional[str] = None
    skipped: Optional[str] = None                 # why it is not released to Central
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return base_version(self.version)

    @property
    def tag(self) -> str:
        return "v" + self.base

    @property
    def pending(self) -> bool:
        return self.skipped is None and self.base not in self.published


@dataclass
class Plan:
    entries: list[Entry]                          # every module, each after the ones it depends on

    @property
    def pending(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.pending]

    @property
    def problems(self) -> list[str]:
        return [f"{entry.module}: {problem}" for entry in self.entries for problem in entry.problems]


def dependency_order(entries: dict[str, Entry], modules: list[str]) -> list[str]:
    """Each module after the ones it depends on. Ties keep the order of the module list."""
    remaining, ordered = list(modules), []
    while remaining:
        ready = [m for m in remaining
                 if not any(dep in remaining for dep, _ in entries[m].dependencies if dep in entries)]
        if not ready:
            raise ValueError("circular dependency between: " + ", ".join(remaining))
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


def check(entry: Entry, entries: dict[str, Entry]) -> None:
    if entry.develop_version and base_version(entry.develop_version) != entry.base:
        entry.notes.append(
            f"{DEVELOPMENT_BRANCH} is at {entry.develop_version} but {RELEASE_BRANCH} is at {entry.version}: "
            f"merge {DEVELOPMENT_BRANCH} into {RELEASE_BRANCH} to release it")
    if not entry.pending:
        return
    if not _SEMVER.match(entry.base):
        entry.problems.append(f"version {entry.version} is not major.minor.patch")
    for dependency, wanted in entry.dependencies:
        target = entries.get(dependency)
        if target is None:
            continue
        if wanted in target.published or (target.pending and target.base == wanted):
            continue
        entry.problems.append(
            f"depends on {dependency} {wanted}, which is neither on Central nor released before it in this plan")


def build_plan(modules: list[str], fetch: Fetch) -> Plan:
    entries: dict[str, Entry] = {}
    for module in modules:
        xml = fetch(pom_url(module, RELEASE_BRANCH))
        if xml is None:
            raise LookupError(f"cannot read the pom of {module} on {RELEASE_BRANCH}")
        _, version, dependencies = parse_pom(xml)
        skipped = NOT_ON_CENTRAL.get(module)
        develop = fetch(pom_url(module, DEVELOPMENT_BRANCH))
        entries[module] = Entry(
            module=module,
            version=version,
            published=set() if skipped else central_versions(module, fetch),
            dependencies=[(name, base_version(v)) for name, v in dependencies],
            develop_version=parse_pom(develop)[1] if develop else None,
            skipped=skipped,
        )
    ordered = [entries[m] for m in dependency_order(entries, modules)]
    for entry in ordered:
        check(entry, entries)
    return Plan(ordered)


def _table(header: tuple, rows: list[tuple], markdown: bool) -> list[str]:
    if markdown:
        return (["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
                + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows])
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
    return ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()
            for row in [header] + rows]


def render(plan: Plan, markdown: bool = False) -> str:
    rows, position = [], 0
    for entry in plan.entries:
        if entry.skipped:
            state, order = f"not released to Central ({entry.skipped})", "-"
        elif entry.pending:
            position += 1
            state, order = "PENDING", str(position)
        else:
            state, order = "already on Central", "-"
        rows.append((order, entry.module, entry.version, entry.tag if entry.pending else "", state))

    lines = _table(("Order", "Module", "Version on main", "Tag", "State"), rows, markdown)
    if not plan.pending:
        lines += ["", "Nothing to release: every version on main is already on Central."]
    if plan.problems:
        lines += ["", "Problems that stop the release:"] + [f"- {problem}" for problem in plan.problems]
    notes = [f"{entry.module}: {note}" for entry in plan.entries for note in entry.notes]
    if notes:
        lines += ["", "Notes:"] + [f"- {note}" for note in notes]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- release

class ReleaseFailed(Exception):
    """The release of a module cannot go on. Nothing that depends on it is released after this."""

    def __init__(self, message: str, excerpt: str = ""):
        super().__init__(message)
        self.excerpt = excerpt


@dataclass
class Run:
    conclusion: str
    url: str
    excerpt: str = ""           # the error lines of the log, when the run did not succeed


@dataclass
class Result:
    module: str
    tag: str
    outcome: str                # released, failed or not attempted
    detail: str = ""
    seconds: float = 0.0
    excerpt: str = ""


_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t\S+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_EXCEPTION = re.compile(r"^[\w.$]+(Exception|Error):")


def error_lines(log: str, limit: int = 15) -> str:
    """The last error lines of the log of a failed run, without the job, step and time gh puts in front.

    Those are the lines Maven marks as errors and the ones that start with an exception, which is where
    the reason is when Maven only says that a goal failed."""
    kept = []
    for line in log.splitlines():
        line = _ANSI.sub("", _LOG_PREFIX.sub("", line)).replace("##[error]", "[ERROR] ").strip()
        wanted = (line.startswith("[ERROR]") and line != "[ERROR]") or _EXCEPTION.match(line)
        if wanted and (not kept or kept[-1] != line):
            kept.append(line)
    return "\n".join(kept[-limit:])


def wait_until(condition: Callable[[], bool], timeout: float, clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep, first: float = 20.0, factor: float = 1.5,
               cap: float = 120.0) -> bool:
    """Asks again and again, waiting a little longer each time, until it is true or the time is up."""
    deadline, pause = clock() + timeout, first
    while True:
        if condition():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(pause, remaining))
        pause = min(pause * factor, cap)


def unpublished_dependencies(plan: Plan, ops) -> dict[str, str]:
    """Modules that a pending one depends on whose SNAPSHOT did not get to GitHub Packages, and why.

    The release workflow of a module builds it against the SNAPSHOT of its dependencies, which
    publish-develop puts there. If that failed, the release would fail after creating its tag."""
    needed = {dependency for entry in plan.pending for dependency, _ in entry.dependencies}
    problems = {}
    for entry in plan.entries:
        if entry.module in needed:
            problem = ops.snapshot_problem(entry.module)
            if problem is not None:
                problems[entry.module] = problem
    return problems


def release(plan: Plan, ops, run_timeout: float = 45 * 60, central_timeout: float = 60 * 60,
            log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> list[Result]:
    """Releases the pending modules in order and stops at the first one that fails."""
    blocked = unpublished_dependencies(plan, ops)
    if blocked:
        for module, why in blocked.items():
            log(f"   {module} is not published to GitHub Packages: {why}")
        log("   Nothing was released: the release of a module builds against the SNAPSHOT of its dependencies.")
        return [Result(entry.module, entry.tag, "not attempted", "; ".join(
                    f"{dependency}: {blocked[dependency]}" for dependency, _ in entry.dependencies
                    if dependency in blocked)) for entry in plan.pending]
    results, stopped = [], False
    for entry in plan.pending:
        if stopped:
            results.append(Result(entry.module, entry.tag, "not attempted"))
            continue
        started = clock()
        try:
            log(f"== {entry.module} {entry.tag}")
            if ops.tag_exists(entry.module, entry.tag):
                # A release that failed after creating the tag, before reaching Central. The version is
                # not burned, so it can be released again as long as the tag holds what the plan expects.
                held = ops.tag_version(entry.module, entry.tag)
                if held != entry.base:
                    raise ReleaseFailed(f"the tag {entry.tag} exists but holds version {held}, not {entry.base}")
                if ops.running(entry.module):
                    raise ReleaseFailed("a release workflow of this module is already running")
                log(f"   the tag {entry.tag} exists but {entry.base} is not on Central: releasing it again")
            ops.dispatch(entry.module, entry.tag)
            log("   release workflow started, waiting for it to finish")
            run = ops.wait_for_run(entry.module, run_timeout)
            if run is None:
                raise ReleaseFailed(f"the release workflow did not finish in {int(run_timeout // 60)} minutes")
            if run.conclusion != "success":
                raise ReleaseFailed(f"the release workflow ended as {run.conclusion}: {run.url}", run.excerpt)
            log("   workflow finished, waiting for Central to show the version")
            if not wait_until(lambda: ops.on_central(entry.module, entry.base), central_timeout, clock, sleep):
                raise ReleaseFailed(f"Central does not show {entry.base} after {int(central_timeout // 60)} minutes")
            problem = ops.resolution_error(entry.module, entry.base)
            if problem is not None:
                raise ReleaseFailed("it is on Central but cannot be resolved the way a consumer would", problem)
            results.append(Result(entry.module, entry.tag, "released", run.url, clock() - started))
            log(f"   released in {(clock() - started) / 60:.1f} minutes")
        except ReleaseFailed as error:
            results.append(Result(entry.module, entry.tag, "failed", str(error), clock() - started, error.excerpt))
            log(f"   STOPPED: {error}")
            for line in error.excerpt.splitlines():
                log(f"      {line}")
            stopped = True
    return results


def render_results(results: list[Result], markdown: bool = False) -> str:
    rows = [(r.module, r.tag, r.outcome, f"{r.seconds / 60:.1f}" if r.seconds else "", r.detail) for r in results]
    return "\n".join(_table(("Module", "Tag", "Outcome", "Minutes", "Detail"), rows, markdown))


def render_excerpts(results: list[Result], markdown: bool = False) -> str:
    """The error lines of every module that failed, or nothing when none has any."""
    blocks = []
    for result in results:
        if result.excerpt:
            body = f"```\n{result.excerpt}\n```" if markdown else result.excerpt
            blocks.append(f"{result.module} {result.tag}\n{body}")
    return "\n\n".join(blocks)


def _epoch(timestamp: str) -> float:
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class GitHubOps:
    """What the release does to the world: GitHub through the gh CLI, and Maven Central over HTTPS."""

    def __init__(self, run=subprocess.run, exists=http_exists, clock=time.time, sleep=time.sleep):
        self._run, self._exists, self._clock, self._sleep = run, exists, clock, sleep
        self._dispatched_at = 0.0
        self._run_id = None

    def _gh(self, *args):
        return self._run(["gh", *args], capture_output=True, text=True)

    def _json(self, *args):
        result = self._gh(*args)
        if result.returncode != 0:
            raise ReleaseFailed(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def tag_exists(self, module: str, tag: str) -> bool:
        # matching-refs answers with a list, empty when there is nothing, so a failed call is always a
        # real error and the exit code is enough. It matches by prefix, hence the exact comparison.
        refs = self._json("api", f"repos/{ORG}/{module}/git/matching-refs/tags/{tag}")
        return any(ref["ref"] == f"refs/tags/{tag}" for ref in refs)

    def tag_version(self, module: str, tag: str) -> str:
        """The version, without -SNAPSHOT, in the pom.xml the tag points at."""
        result = self._gh("api", f"repos/{ORG}/{module}/contents/pom.xml?ref={tag}",
                          "-H", "Accept: application/vnd.github.raw")
        if result.returncode != 0:
            raise ReleaseFailed(f"cannot read the pom.xml at tag {tag}: {result.stderr.strip()}")
        try:
            return base_version(parse_pom(result.stdout)[1])
        except ValueError as error:
            raise ReleaseFailed(f"cannot read the version at tag {tag}: {error}")

    def running(self, module: str) -> bool:
        """Whether a release workflow of the module is queued or in progress."""
        for status in ("queued", "in_progress"):
            if self._json("run", "list", "-R", f"{ORG}/{module}", "--workflow", "release.yml",
                          "--status", status, "--json", "databaseId"):
                return True
        return False

    def dispatch(self, module: str, tag: str) -> None:
        self._dispatched_at = self._clock()
        result = self._gh("workflow", "run", "release.yml", "-R", f"{ORG}/{module}",
                          "--ref", RELEASE_BRANCH, "-f", f"tag={tag}")
        if result.returncode != 0:
            raise ReleaseFailed(f"cannot start the release workflow: {result.stderr.strip()}")

    def wait_for_run(self, module: str, timeout: float) -> Optional[Run]:
        repo = f"{ORG}/{module}"

        def started() -> bool:
            runs = self._json("run", "list", "-R", repo, "--workflow", "release.yml", "--event",
                              "workflow_dispatch", "--limit", "10", "--json", "databaseId,createdAt")
            recent = [r for r in runs if _epoch(r["createdAt"]) >= self._dispatched_at - 60]
            if recent:
                self._run_id = max(recent, key=lambda r: _epoch(r["createdAt"]))["databaseId"]
            return bool(recent)

        if not wait_until(started, 300, self._clock, self._sleep, first=10):
            raise ReleaseFailed("the release workflow did not start in 5 minutes")

        final: dict = {}

        def finished() -> bool:
            final.update(self._json("run", "view", str(self._run_id), "-R", repo, "--json", "status,conclusion,url"))
            return final["status"] == "completed"

        if not wait_until(finished, timeout, self._clock, self._sleep, first=30):
            return None
        excerpt = "" if final["conclusion"] == "success" else self._failure_excerpt(repo)
        return Run(final["conclusion"], final["url"], excerpt)

    def _failure_excerpt(self, repo: str) -> str:
        """Best effort: not being able to read the log must not hide that the run failed."""
        result = self._gh("run", "view", str(self._run_id), "-R", repo, "--log-failed")
        return error_lines(result.stdout) if result.returncode == 0 else ""

    def on_central(self, module: str, version: str) -> bool:
        base = f"{CENTRAL}/{GROUP_PATH}/{module}/{version}/{module}-{version}"
        return self._exists(base + ".pom") and self._exists(base + ".jar")

    def snapshot_problem(self, module: str) -> Optional[str]:
        """Why the last publish of the module from develop did not leave its SNAPSHOT in GitHub Packages."""
        runs = self._json("run", "list", "-R", f"{ORG}/{module}", "--workflow", "publish-develop.yml",
                          "--branch", DEVELOPMENT_BRANCH, "--limit", "1", "--json", "status,conclusion,url")
        if not runs:
            return f"it was never published from {DEVELOPMENT_BRANCH}"
        run = runs[0]
        if run["status"] != "completed":
            return f"its latest publish is still {run['status']}: {run['url']}"
        if run["conclusion"] != "success":
            return f"its latest publish ended as {run['conclusion']}: {run['url']}"
        return None

    def resolution_error(self, module: str, version: str) -> Optional[str]:
        """Why Maven cannot resolve the module from Central alone, or None when it can."""
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.xml"
            settings.write_text(_CENTRAL_ONLY, encoding="utf-8")
            # Run in an empty folder: in one with a pom.xml Maven would load that project first, and the
            # workspace pom lists modules that are not cloned on a runner.
            result = self._run(
                ["mvn", "-B", "-q", "-s", str(settings), f"-Dmaven.repo.local={folder}/repository",
                 "dependency:get", f"-Dartifact={GROUP}:{module}:{version}"],
                capture_output=True, text=True, cwd=folder)
        if result.returncode == 0:
            return None
        return error_lines(result.stdout + result.stderr) or f"mvn ended with code {result.returncode}"


# ------------------------------------------------------------------------------------------- command line

def default_workspace() -> Path:
    return Path(__file__).resolve().parent.parent


def load_plan(workspace: str) -> Optional[Plan]:
    modules = modules_of((Path(workspace) / "pom.xml").read_text(encoding="utf-8"))
    try:
        return build_plan(modules, http_fetch)
    except (LookupError, ValueError, urllib.error.URLError) as error:
        print(f"Cannot build the plan: {error}", file=sys.stderr)
        return None


def append_summary(path: Optional[str], title: str, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(f"## {title}\n\n{text}\n\n")


def write_outputs(path: Optional[str], plan: Plan) -> None:
    """Lets a workflow decide what to do next: how many modules are pending and how many problems there are."""
    if path:
        with open(path, "a", encoding="utf-8") as outputs:
            outputs.write(f"pending={len(plan.pending)}\nproblems={len(plan.problems)}\n")


def command_plan(args: argparse.Namespace) -> int:
    plan = load_plan(args.workspace)
    if plan is None:
        return 2
    print(render(plan))
    append_summary(args.summary, "Release plan", render(plan, markdown=True))
    write_outputs(args.github_output, plan)
    return 1 if plan.problems else 0


def command_run(args: argparse.Namespace) -> int:
    if not args.yes:
        print("This publishes to Maven Central and it cannot be undone. Run it again with --yes to go on.",
              file=sys.stderr)
        return 2
    if not os.environ.get("GH_TOKEN"):
        print("GH_TOKEN is not set: the release needs a token that can run workflows in the module repositories.",
              file=sys.stderr)
        return 2
    plan = load_plan(args.workspace)
    if plan is None:
        return 2
    print(render(plan))
    append_summary(args.summary, "Release plan", render(plan, markdown=True))
    if plan.problems:
        print("\nNothing is released until the problems above are solved.", file=sys.stderr)
        return 1
    if not plan.pending:
        return 0
    results = release(plan, GitHubOps(), args.run_timeout * 60, args.central_timeout * 60)
    print("\n" + render_results(results))
    append_summary(args.summary, "Release result", render_results(results, markdown=True))
    if render_excerpts(results):
        print("\n" + render_excerpts(results))
        append_summary(args.summary, "Errors", render_excerpts(results, markdown=True))
    return 0 if all(r.outcome == "released" for r in results) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Plans and runs the release of the Spool modules.")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--workspace", default=str(default_workspace()), help="folder with the workspace pom.xml")
        command.add_argument("--summary", help="file to append a markdown version of the output to")

    plan = commands.add_parser("plan", help="show what would be released, in which order, and what is wrong")
    common(plan)
    plan.add_argument("--github-output", help="file to write pending=N and problems=N to, for a workflow")
    plan.set_defaults(handler=command_plan)

    run = commands.add_parser("run", help="release every pending module, one after the other")
    common(run)
    run.add_argument("--yes", action="store_true", help="confirm that it may publish to Maven Central")
    run.add_argument("--run-timeout", type=int, default=45, help="minutes to wait for each release workflow")
    run.add_argument("--central-timeout", type=int, default=60, help="minutes to wait for Central to show a version")
    run.set_defaults(handler=command_run)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
