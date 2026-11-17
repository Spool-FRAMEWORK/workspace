"""Pushes develop of every module that has new commits, in dependency order, the way a release goes.

    python3 tools/release.py push --dry-run   what would be pushed, published again or skipped, and why
    python3 tools/release.py push             the same, then it asks before pushing anything

Each push starts the publish-develop workflow of the module, which puts its SNAPSHOT in GitHub Packages.
The next module is not pushed until that publish has ended well, so nothing is ever built against a
SNAPSHOT of a dependency that is not there yet. A module without new commits whose last publish failed is
published again without a push.

A module that cannot be pushed (not on develop, with uncommitted changes, behind or diverged from
origin/develop, or with a problem in bump.sh --check) is skipped when nothing that is pushed depends on
it. When something does, nothing is pushed at all.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import release

PUSH = "push"
PUBLISH_AGAIN = "publish again"
WAIT = "wait for its publish"
UP_TO_DATE = "up to date"
SKIP = "skipped"

UPLOADS = (PUSH, PUBLISH_AGAIN, WAIT)


@dataclass
class GitState:
    """Where the clone of a module is: its branch, whether it has uncommitted changes, how many commits its
    develop has that origin/develop has not (ahead) and the other way round (behind), and the commit of develop."""
    branch: str
    uncommitted: bool
    ahead: int
    behind: int
    head: str


@dataclass
class Clone:
    """A module as its local clone has it. publishes says whether it has a publish-develop workflow."""
    module: str
    version: str
    dependencies: list[str]
    branch: str = release.DEVELOPMENT_BRANCH
    uncommitted: bool = False
    ahead: int = 0
    behind: int = 0
    head: str = ""
    publishes: bool = True


@dataclass
class Step:
    """What is done with a module, and why when it is not uploaded."""
    module: str
    version: str
    action: str
    reason: str = ""
    head: str = ""
    publishes: bool = True


@dataclass
class PushPlan:
    """Every module, each one after the ones it depends on. Problems stop everything."""
    steps: list[Step]
    problems: list[str]

    @property
    def uploads(self) -> list[Step]:
        return [step for step in self.steps if step.action in UPLOADS]


def unfit_reasons(clone: Clone, bump_problems: list[str]) -> list[str]:
    reasons = []
    if clone.branch != release.DEVELOPMENT_BRANCH:
        reasons.append(f"its clone is on {clone.branch}, not on {release.DEVELOPMENT_BRANCH}")
    if clone.uncommitted:
        reasons.append("it has uncommitted changes")
    if clone.behind and clone.ahead:
        reasons.append(f"its {release.DEVELOPMENT_BRANCH} and origin/{release.DEVELOPMENT_BRANCH} have diverged "
                       f"({clone.ahead} and {clone.behind} commit(s) apart)")
    elif clone.behind:
        reasons.append(f"its {release.DEVELOPMENT_BRANCH} is {clone.behind} commit(s) behind "
                       f"origin/{release.DEVELOPMENT_BRANCH}: pull first")
    return reasons + bump_problems


def what_to_upload(clone: Clone, latest_publish: Callable[[str], Optional[release.PublishState]]) -> Optional[str]:
    """PUSH, PUBLISH_AGAIN or WAIT, or None when there is nothing to upload."""
    if clone.ahead:
        return PUSH
    if not clone.publishes:
        return None
    publish = latest_publish(clone.module)
    if publish is None or not (publish.running or publish.succeeded):
        return PUBLISH_AGAIN
    return WAIT if publish.running else None


def depends_on(module: str, dependency: str, dependencies: dict[str, list[str]]) -> bool:
    """Whether the module depends on the other one, directly or through the modules in between."""
    seen, pending = set(), list(dependencies.get(module, []))
    while pending:
        current = pending.pop()
        if current == dependency:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(dependencies.get(current, []))
    return False


def plan_push(clones: list[Clone], bump_problems: dict[str, list[str]],
              latest_publish: Callable[[str], Optional[release.PublishState]]) -> PushPlan:
    """What to do with each module. bump_problems maps a module to its problems, and "" to the ones about
    no module in particular, which stop everything."""
    by_module = {clone.module: clone for clone in clones}
    dependencies = {clone.module: [d for d in clone.dependencies if d in by_module] for clone in clones}
    ordered = [by_module[m] for m in release.dependency_order(dependencies, [clone.module for clone in clones])]

    upload = {clone.module: what_to_upload(clone, latest_publish) for clone in ordered}
    unfit = {clone.module: unfit_reasons(clone, bump_problems.get(clone.module, [])) for clone in ordered}
    uploaded = [m for m in upload if upload[m] and not unfit[m]]

    steps, problems = [], list(bump_problems.get("", []))
    for clone in ordered:
        reasons, action = unfit[clone.module], upload[clone.module]
        step = Step(clone.module, clone.version, action or UP_TO_DATE, head=clone.head, publishes=clone.publishes)
        if reasons and not action:
            step.action, step.reason = SKIP, "nothing new to publish, and " + "; ".join(reasons)
        elif reasons:
            step.action = SKIP
            dependents = [m for m in uploaded if depends_on(m, clone.module, dependencies)]
            if dependents:
                verb = "depends" if len(dependents) == 1 else "depend"
                problems.append(f"{clone.module} cannot be pushed ({'; '.join(reasons)}), "
                                f"and {', '.join(dependents)} {verb} on it")
                step.reason = "cannot be pushed, and modules that are pushed depend on it"
            else:
                step.reason = f"cannot be pushed ({'; '.join(reasons)}), and nothing that is pushed depends on it"
        steps.append(step)
    return PushPlan(steps, problems)


def render_plan(plan: PushPlan, markdown: bool = False) -> str:
    rows, position = [], 0
    for step in plan.steps:
        if step.action in UPLOADS:
            position += 1
        order = str(position) if step.action in UPLOADS else "-"
        rows.append((order, step.module, step.version, step.action, step.reason))
    lines = release._table(("Order", "Module", "Version", "Action", "Why"), rows, markdown)
    if not plan.uploads:
        lines += ["", f"Nothing to push: every module is up to date with origin/{release.DEVELOPMENT_BRANCH}."]
    if plan.problems:
        lines += ["", "Problems that stop the push:"] + [f"- {problem}" for problem in plan.problems]
    return "\n".join(lines)


def push_in_order(plan: PushPlan, ops, git, run_timeout: float, log: Callable[[str], None] = print,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> list[release.Result]:
    """Uploads the modules of the plan in order and stops at the first one that fails.

    Before a push or a new publish it waits for any publish of the module that is already running, so that
    the run it follows is its own."""
    uploads = plan.uploads
    states = {step.module: (" ", "") for step in uploads}
    results, stopped = [], False

    def show(title: str) -> None:
        log(f"== {title}")
        log(release.render_progress([(step.module, step.version) for step in uploads], states))

    show(f"Uploading {len(uploads)} module(s)")
    for step in plan.steps:
        if step.action not in UPLOADS:
            results.append(release.Result(step.module, step.version, step.action, step.reason))
            continue
        if stopped:
            results.append(release.Result(step.module, step.version, "not attempted"))
            continue
        started = clock()

        def waiting(what: str, step=step, started=started):
            def beat(_elapsed: float) -> None:
                log(f"   {step.module}: {what} ({release.format_duration(clock() - started)})")
            return beat

        try:
            states[step.module] = (">", step.action)
            show(f"{step.module} {step.version}: {step.action}")
            if step.action != WAIT and step.publishes:
                if not release.wait_until(lambda: not ops.running(step.module), run_timeout, clock, sleep,
                                          on_wait=waiting("waiting for the publish already running")):
                    raise release.ReleaseFailed(
                        f"a publish of this module is still running after {int(run_timeout // 60)} minutes")
            if step.action == PUSH:
                git.push(step.module)
                if not step.publishes:
                    results.append(release.Result(step.module, step.version, "pushed", "", clock() - started))
                    states[step.module] = ("x", "pushed")
                    continue
                run = ops.wait_for_run(step.module, run_timeout, waiting("publishing"), commit=step.head)
            elif step.action == PUBLISH_AGAIN:
                ops.dispatch(step.module)
                run = ops.wait_for_run(step.module, run_timeout, waiting("publishing again"))
            else:
                run = ops.wait_for_run(step.module, run_timeout, waiting("publishing"), commit=step.head)
            if run is None:
                raise release.ReleaseFailed(f"the publish did not finish in {int(run_timeout // 60)} minutes")
            if run.conclusion != "success":
                raise release.ReleaseFailed(f"the publish ended as {run.conclusion}: {run.url}", run.excerpt)
            results.append(release.Result(step.module, step.version, "published", run.url, clock() - started))
            states[step.module] = ("x", "published")
        except release.ReleaseFailed as error:
            results.append(release.Result(step.module, step.version, "failed", str(error), clock() - started,
                                          error.excerpt))
            states[step.module] = ("!", "failed")
            log(f"   STOPPED: {error}")
            stopped = True
    show("Done" if not stopped else "Stopped")
    return results


class LocalGit:
    """The clones of the modules, next to the workspace, through the git CLI."""

    def __init__(self, root: Path, run=subprocess.run):
        self._root, self._run = root, run

    def _git(self, module: str, *args):
        return self._run(["git", "-C", str(self._root / module), *args], capture_output=True, text=True)

    def _output(self, module: str, *args) -> str:
        result = self._git(module, *args)
        if result.returncode != 0:
            raise release.ReleaseFailed(f"git {args[0]} failed in {module}: {result.stderr.strip()}")
        return result.stdout.strip()

    def fetch(self, module: str) -> None:
        self._output(module, "fetch", "-q", "origin", release.DEVELOPMENT_BRANCH)

    def state(self, module: str) -> GitState:
        branch = self._output(module, "rev-parse", "--abbrev-ref", "HEAD")
        uncommitted = bool(self._output(module, "status", "--porcelain", "--untracked-files=no"))
        ahead, behind = self._output(module, "rev-list", "--left-right", "--count",
                                     f"{release.DEVELOPMENT_BRANCH}...origin/{release.DEVELOPMENT_BRANCH}").split()
        head = self._output(module, "rev-parse", release.DEVELOPMENT_BRANCH)
        return GitState(branch, uncommitted, int(ahead), int(behind), head)

    def clone(self, module: str) -> Clone:
        folder = self._root / module
        _, version, dependencies = release.parse_pom((folder / "pom.xml").read_text(encoding="utf-8"))
        state = self.state(module)
        publishes = (folder / ".github" / "workflows" / release.PUBLISH_WORKFLOW.file).is_file()
        return Clone(module, version, [name for name, _ in dependencies], state.branch, state.uncommitted,
                     state.ahead, state.behind, state.head, publishes)

    def push(self, module: str) -> None:
        result = self._git(module, "push", "origin", release.DEVELOPMENT_BRANCH)
        if result.returncode != 0:
            raise release.ReleaseFailed(f"git push of {release.DEVELOPMENT_BRANCH} failed: {result.stderr.strip()}")


def parse_bump_problems(output: str) -> dict[str, list[str]]:
    """The lines of bump.sh --check --porcelain, "<module><TAB><problem>", grouped by module."""
    problems: dict[str, list[str]] = {}
    for line in output.splitlines():
        if "\t" in line:
            module, problem = line.split("\t", 1)
            problems.setdefault(module, []).append(problem)
    return problems


def bump_problems(workspace: Path, run=subprocess.run) -> dict[str, list[str]]:
    result = run(["sh", str(workspace / "bump.sh"), "--check", "--porcelain"], capture_output=True, text=True)
    problems = parse_bump_problems(result.stdout)
    if result.returncode != 0 and not problems:
        problems[""] = [f"bump.sh --check failed: {result.stderr.strip() or result.stdout.strip()}"]
    return problems


def load_plan(workspace: Path, git: LocalGit, ops: release.GitHubOps) -> PushPlan:
    modules = release.modules_of((workspace / "pom.xml").read_text(encoding="utf-8"))
    for module in modules:
        git.fetch(module)
    clones = [git.clone(module) for module in modules]
    return plan_push(clones, bump_problems(workspace), ops.latest_publish)


def command_push(args: argparse.Namespace, ask: Callable[[str], str] = input) -> int:
    workspace = Path(args.workspace)
    git, ops = LocalGit(workspace.parent), release.GitHubOps(workflow=release.PUBLISH_WORKFLOW)
    try:
        plan = load_plan(workspace, git, ops)
    except (release.ReleaseFailed, OSError, ValueError) as error:
        print(f"Cannot build the plan: {error}", file=sys.stderr)
        return 2
    print(render_plan(plan))
    if plan.problems:
        print("\nNothing is pushed until the problems above are solved.", file=sys.stderr)
        return 1
    if not plan.uploads or args.dry_run:
        return 0
    if not args.yes and ask("\nPush and publish the modules above, in this order? [y/N] ").strip().lower() != "y":
        print("Nothing was pushed.")
        return 0
    results = push_in_order(plan, ops, git, args.run_timeout * 60)
    print("\n" + release.render_result_table(results, "Version"))
    if release.render_excerpts(results):
        print("\n" + release.render_excerpts(results))
    return 0 if all(r.outcome != "failed" for r in results) else 1


def add_command(commands) -> None:
    push = commands.add_parser("push", help=f"push {release.DEVELOPMENT_BRANCH} of every module in dependency "
                                            "order, waiting for each publish")
    push.add_argument("--workspace", default=str(release.default_workspace()),
                      help="folder with the workspace pom.xml; the modules are cloned next to it")
    push.add_argument("--dry-run", action="store_true", help="only show what would be done")
    push.add_argument("--yes", action="store_true", help="do not ask before pushing")
    push.add_argument("--run-timeout", type=int, default=45, help="minutes to wait for each publish")
    push.set_defaults(handler=command_push)
