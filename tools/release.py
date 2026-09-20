#!/usr/bin/env python3
"""Plans, and runs, the release of the Spool modules to Maven Central.

    python3 tools/release.py plan       what would be released, in which order, and what is wrong

A module is pending when the base version of its pom on main, without -SNAPSHOT, is not on Central yet.
Modules are ordered so that each one comes after the ones it depends on: a module published against a
version that does not exist yet would be broken for good, because Central does not let a version be
published again.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ORG = "Spool-FRAMEWORK"
CENTRAL = "https://repo1.maven.org/maven2"
GROUP_PATH = "io/github/spool-framework"
RELEASE_BRANCH = "main"
DEVELOPMENT_BRANCH = "develop"

# Modules that are not published to Maven Central, and why.
NOT_ON_CENTRAL = {"watchdog": "it is published as a Docker image"}

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


def central_versions(module: str, fetch: Fetch) -> set[str]:
    metadata = fetch(metadata_url(module))
    return set(re.findall(r"<version>([^<]+)</version>", metadata)) if metadata else set()


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


def render(plan: Plan, markdown: bool = False) -> str:
    rows, position = [], 0
    for entry in plan.entries:
        if entry.skipped:
            state = f"not released to Central ({entry.skipped})"
            order = "-"
        elif entry.pending:
            position += 1
            state, order = "PENDING", str(position)
        else:
            state, order = "already on Central", "-"
        rows.append((order, entry.module, entry.version, entry.tag if entry.pending else "", state))

    header = ("Order", "Module", "Version on main", "Tag", "State")
    lines = []
    if markdown:
        lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
    else:
        widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
        for row in [header] + rows:
            lines.append("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())

    if not plan.pending:
        lines += ["", "Nothing to release: every version on main is already on Central."]
    if plan.problems:
        lines += ["", "Problems that stop the release:"] + [f"- {problem}" for problem in plan.problems]
    notes = [f"{entry.module}: {note}" for entry in plan.entries for note in entry.notes]
    if notes:
        lines += ["", "Notes:"] + [f"- {note}" for note in notes]
    return "\n".join(lines)


def default_workspace() -> Path:
    return Path(__file__).resolve().parent.parent


def command_plan(args: argparse.Namespace) -> int:
    modules = modules_of((Path(args.workspace) / "pom.xml").read_text(encoding="utf-8"))
    try:
        plan = build_plan(modules, http_fetch)
    except (LookupError, ValueError, urllib.error.URLError) as error:
        print(f"Cannot build the plan: {error}", file=sys.stderr)
        return 2
    print(render(plan))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write("## Release plan\n\n" + render(plan, markdown=True) + "\n")
    return 1 if plan.problems else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Plans and runs the release of the Spool modules.")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="show what would be released, in which order, and what is wrong")
    plan.add_argument("--workspace", default=str(default_workspace()), help="folder with the workspace pom.xml")
    plan.add_argument("--summary", help="file to append a markdown version of the plan to")
    plan.set_defaults(handler=command_plan)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
