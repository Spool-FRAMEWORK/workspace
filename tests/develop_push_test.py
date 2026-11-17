"""Tests for tools/develop_push.py. Run them with: python3 -m unittest discover -s tests -p "*_test.py" """
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import develop_push  # noqa: E402
import release  # noqa: E402

PUBLISHED = release.PublishState("completed", "success", "https://runs/ok")
FAILED = release.PublishState("completed", "failure", "https://runs/failed")
RUNNING = release.PublishState("in_progress", "", "https://runs/running")


def clone(module, *dependencies, **state):
    return develop_push.Clone(module=module, version="1.0.0-SNAPSHOT", dependencies=list(dependencies), **state)


def chain(**states):
    """core <- validator <- ingester <- infrastructure, plus watchdog on core, which has no publish workflow."""
    clones = [
        clone("core"),
        clone("validator", "core"),
        clone("ingester", "core", "validator"),
        clone("infrastructure", "core", "ingester"),
        clone("watchdog", "core", publishes=False),
    ]
    for each in clones:
        for field, value in states.get(each.module, {}).items():
            setattr(each, field, value)
    return clones


def plan_of(clones, bump=None, publishes=None):
    latest = publishes or {}
    return develop_push.plan_push(clones, bump or {}, lambda module: latest.get(module, PUBLISHED))


def actions(plan):
    return {step.module: step.action for step in plan.steps}


class PlanPushTest(unittest.TestCase):
    def test_modules_with_new_commits_are_pushed_each_after_the_ones_it_depends_on(self):
        clones = list(reversed(chain(core={"ahead": 1}, ingester={"ahead": 2}, infrastructure={"ahead": 1})))

        plan = plan_of(clones)

        self.assertEqual([step.module for step in plan.uploads], ["core", "ingester", "infrastructure"])
        self.assertEqual(plan.problems, [])

    def test_a_module_without_new_commits_whose_last_publish_succeeded_is_up_to_date(self):
        plan = plan_of(chain(core={"ahead": 1}))

        self.assertEqual(actions(plan)["validator"], develop_push.UP_TO_DATE)

    def test_a_module_without_new_commits_whose_last_publish_failed_is_published_again(self):
        plan = plan_of(chain(), publishes={"ingester": FAILED})

        self.assertEqual(actions(plan)["ingester"], develop_push.PUBLISH_AGAIN)

    def test_a_module_never_published_from_develop_is_published_again(self):
        plan = develop_push.plan_push(chain(), {}, lambda module: None if module == "validator" else PUBLISHED)

        self.assertEqual(actions(plan)["validator"], develop_push.PUBLISH_AGAIN)

    def test_a_publish_in_progress_is_waited_for_instead_of_started_again(self):
        plan = plan_of(chain(), publishes={"core": RUNNING})

        self.assertEqual(actions(plan)["core"], develop_push.WAIT)

    def test_a_module_without_a_publish_workflow_is_pushed_and_never_asked_about_its_publish(self):
        asked = []
        plan = develop_push.plan_push(chain(watchdog={"ahead": 1}), {},
                                      lambda module: asked.append(module) or PUBLISHED)

        self.assertEqual(actions(plan)["watchdog"], develop_push.PUSH)
        self.assertNotIn("watchdog", asked)

    def test_each_reason_a_clone_cannot_be_pushed_is_given(self):
        cases = {
            "branch": ({"branch": "feature/dev-1-x", "ahead": 1}, "its clone is on feature/dev-1-x, not on develop"),
            "uncommitted": ({"uncommitted": True, "ahead": 1}, "it has uncommitted changes"),
            "behind": ({"behind": 2}, "its develop is 2 commit(s) behind origin/develop: pull first"),
            "diverged": ({"ahead": 1, "behind": 3},
                         "its develop and origin/develop have diverged (1 and 3 commit(s) apart)"),
        }
        for name, (state, reason) in cases.items():
            with self.subTest(name):
                plan = plan_of(chain(watchdog=state), publishes={"watchdog": FAILED})

                watchdog = next(step for step in plan.steps if step.module == "watchdog")
                self.assertEqual(watchdog.action, develop_push.SKIP)
                self.assertIn(reason, watchdog.reason)

    def test_a_problem_bump_reports_for_a_module_makes_it_unfit(self):
        problem = "infrastructure depends on core 1.2.0-SNAPSHOT, but core is 1.3.0-SNAPSHOT"

        plan = plan_of(chain(infrastructure={"ahead": 1}), bump={"infrastructure": [problem]})

        infrastructure = next(step for step in plan.steps if step.module == "infrastructure")
        self.assertEqual(infrastructure.action, develop_push.SKIP)
        self.assertIn(problem, infrastructure.reason)

    def test_an_unfit_module_that_nothing_pushed_depends_on_is_skipped_and_the_rest_goes_on(self):
        plan = plan_of(chain(core={"ahead": 1}, infrastructure={"ahead": 1, "uncommitted": True}))

        self.assertEqual(plan.problems, [])
        self.assertEqual(actions(plan)["infrastructure"], develop_push.SKIP)
        self.assertEqual([step.module for step in plan.uploads], ["core"])

    def test_an_unfit_module_that_a_pushed_one_depends_on_stops_everything_saying_which_and_why(self):
        plan = plan_of(chain(validator={"ahead": 1, "branch": "feature/dev-1-x"}, infrastructure={"ahead": 1}))

        self.assertEqual(plan.problems, [
            "validator cannot be pushed (its clone is on feature/dev-1-x, not on develop), "
            "and infrastructure depends on it"])

    def test_the_problem_names_every_pushed_module_that_depends_on_the_unfit_one(self):
        plan = plan_of(chain(core={"ahead": 1, "uncommitted": True}, validator={"ahead": 1},
                             infrastructure={"ahead": 1}))

        self.assertEqual(plan.problems, [
            "core cannot be pushed (it has uncommitted changes), and validator, infrastructure depend on it"])

    def test_an_unfit_module_with_nothing_to_publish_never_stops_its_dependents(self):
        plan = plan_of(chain(core={"uncommitted": True}, validator={"ahead": 1}))

        self.assertEqual(plan.problems, [])
        self.assertEqual(actions(plan)["core"], develop_push.SKIP)
        self.assertEqual([step.module for step in plan.uploads], ["validator"])

    def test_a_problem_bump_reports_for_no_module_in_particular_stops_everything(self):
        plan = plan_of(chain(core={"ahead": 1}), bump={"": ["Modules use different Cucumber versions: a, b"]})

        self.assertEqual(plan.problems, ["Modules use different Cucumber versions: a, b"])

    def test_the_plan_numbers_only_what_is_uploaded(self):
        text = develop_push.render_plan(plan_of(chain(validator={"ahead": 1})))

        self.assertRegex(text, r"(?m)^1\s+validator\s+1\.0\.0-SNAPSHOT\s+push")
        self.assertRegex(text, r"(?m)^-\s+core\s+1\.0\.0-SNAPSHOT\s+up to date")


class FakeOps:
    """Stands in for GitHub: every publish succeeds unless the module is in fail, and busy modules have a
    publish running for as many questions as their number says."""

    def __init__(self, fail=(), busy=None, never_ends=()):
        self.fail, self.busy, self.never_ends = set(fail), dict(busy or {}), set(never_ends)
        self.calls = []

    def running(self, module):
        left = self.busy.get(module, 0)
        self.busy[module] = left - 1
        return left > 0

    def dispatch(self, module, tag=None):
        self.calls.append(("dispatch", module))

    def wait_for_run(self, module, timeout, on_wait=None, commit=None):
        self.calls.append(("wait", module, commit))
        if module in self.never_ends:
            return None
        if module in self.fail:
            return release.Run("failure", f"https://runs/{module}", "[ERROR] Could not find artifact")
        return release.Run("success", f"https://runs/{module}")


class FakeGit:
    def __init__(self, rejects=()):
        self.rejects, self.calls = set(rejects), []

    def push(self, module):
        self.calls.append(("push", module))
        if module in self.rejects:
            raise release.ReleaseFailed("git push was rejected: hook declined")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def push(plan, ops=None, git=None):
    ops, git, clock = ops or FakeOps(), git or FakeGit(), FakeClock()
    results = develop_push.push_in_order(plan, ops, git, run_timeout=600, log=lambda _: None,
                                         clock=clock, sleep=clock.sleep)
    return results, ops, git


def outcomes(results):
    return {result.module: result.outcome for result in results}


class PushInOrderTest(unittest.TestCase):
    def test_a_module_is_pushed_only_after_the_publish_of_the_one_before_succeeded(self):
        plan = plan_of(chain(core={"ahead": 1, "head": "c1"}, validator={"ahead": 1, "head": "v1"}))

        _, ops, git = push(plan)

        self.assertEqual(ops.calls, [("wait", "core", "c1"), ("wait", "validator", "v1")])
        self.assertEqual(git.calls, [("push", "core"), ("push", "validator")])

    def test_a_failed_publish_stops_everything_after_it_with_its_error_lines(self):
        plan = plan_of(chain(core={"ahead": 1}, validator={"ahead": 1}, ingester={"ahead": 1}))

        results, _, git = push(plan, FakeOps(fail={"validator"}))

        self.assertEqual(git.calls, [("push", "core"), ("push", "validator")])
        self.assertEqual(outcomes(results)["validator"], "failed")
        self.assertEqual(outcomes(results)["ingester"], "not attempted")
        validator = next(result for result in results if result.module == "validator")
        self.assertIn("Could not find artifact", validator.excerpt)

    def test_a_publish_that_never_ends_stops_everything_after_it(self):
        plan = plan_of(chain(core={"ahead": 1}, validator={"ahead": 1}))

        results, _, _ = push(plan, FakeOps(never_ends={"core"}))

        self.assertEqual(outcomes(results)["core"], "failed")
        self.assertEqual(outcomes(results)["validator"], "not attempted")

    def test_a_rejected_push_stops_everything_after_it(self):
        plan = plan_of(chain(core={"ahead": 1}, validator={"ahead": 1}))

        results, _, _ = push(plan, git=FakeGit(rejects={"core"}))

        self.assertEqual(outcomes(results)["core"], "failed")
        self.assertEqual(outcomes(results)["validator"], "not attempted")

    def test_a_module_whose_last_publish_failed_is_published_again_without_a_push(self):
        plan = plan_of(chain(), publishes={"ingester": FAILED})

        results, ops, git = push(plan)

        self.assertEqual(git.calls, [])
        self.assertEqual(ops.calls, [("dispatch", "ingester"), ("wait", "ingester", None)])
        self.assertEqual(outcomes(results)["ingester"], "published")

    def test_a_publish_in_progress_is_followed_until_it_ends(self):
        plan = plan_of(chain(core={"head": "c1"}), publishes={"core": RUNNING})

        _, ops, git = push(plan)

        self.assertEqual(git.calls, [])
        self.assertEqual(ops.calls, [("wait", "core", "c1")])

    def test_nothing_is_pushed_while_a_publish_of_the_same_module_is_running(self):
        plan = plan_of(chain(core={"ahead": 1}))
        ops = FakeOps(busy={"core": 3})

        push(plan, ops)

        self.assertEqual(ops.busy["core"], -1)

    def test_a_module_without_a_publish_workflow_is_only_pushed(self):
        plan = plan_of(chain(watchdog={"ahead": 1}))

        results, ops, git = push(plan)

        self.assertEqual(git.calls, [("push", "watchdog")])
        self.assertEqual(ops.calls, [])
        self.assertEqual(outcomes(results)["watchdog"], "pushed")

    def test_modules_with_nothing_to_upload_appear_in_the_result_with_why(self):
        plan = plan_of(chain(core={"ahead": 1}, infrastructure={"ahead": 1, "uncommitted": True}))

        results, _, _ = push(plan)

        self.assertEqual(outcomes(results)["validator"], develop_push.UP_TO_DATE)
        infrastructure = next(result for result in results if result.module == "infrastructure")
        self.assertEqual(infrastructure.outcome, develop_push.SKIP)
        self.assertIn("it has uncommitted changes", infrastructure.detail)


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class FakeRun:
    """Answers commands from a list of (prefix, reply) and remembers them."""

    def __init__(self, *replies):
        self.replies, self.commands = list(replies), []

    def __call__(self, command, **options):
        self.commands.append(command)
        for prefix, reply in self.replies:
            if command[: len(prefix)] == prefix:
                return reply
        raise AssertionError(f"unexpected command {command}")


class LocalGitTest(unittest.TestCase):
    def git(self, *replies, root=Path("/spool")):
        run = FakeRun(*replies)
        return develop_push.LocalGit(root, run=run), run

    def test_reads_the_branch_the_changes_and_how_far_develop_is_from_origin(self):
        folder = str(Path("/spool") / "core")
        git, run = self.git(
            (["git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD"], Completed("develop\n")),
            (["git", "-C", folder, "status"], Completed(" M pom.xml\n")),
            (["git", "-C", folder, "rev-list"], Completed("2\t1\n")),
            (["git", "-C", folder, "rev-parse", "develop"], Completed("abc123\n")))

        state = git.state("core")

        self.assertEqual(state, develop_push.GitState("develop", True, 2, 1, "abc123"))
        status = next(command for command in run.commands if "status" in command)
        self.assertIn("--untracked-files=no", status)
        compared = next(command for command in run.commands if "rev-list" in command)
        self.assertIn("develop...origin/develop", compared)

    def test_a_clean_clone_has_no_uncommitted_changes(self):
        git, _ = self.git(
            (["git", "-C", str(Path("/spool") / "core"), "status"], Completed("")),
            (["git", "-C", str(Path("/spool") / "core"), "rev-list"], Completed("0\t0\n")),
            (["git"], Completed("develop\n")))

        self.assertFalse(git.state("core").uncommitted)

    def test_pushes_develop_to_origin(self):
        folder = str(Path("/spool") / "core")
        git, run = self.git((["git", "-C", folder, "push"], Completed()))

        git.push("core")

        self.assertEqual(run.commands[0], ["git", "-C", folder, "push", "origin", "develop"])

    def test_a_rejected_push_says_why(self):
        git, _ = self.git((["git"], Completed(stderr="! [rejected] develop -> develop (fetch first)", returncode=1)))

        with self.assertRaises(release.ReleaseFailed) as raised:
            git.push("core")
        self.assertIn("fetch first", str(raised.exception))

    def test_a_fetch_that_fails_says_why(self):
        git, _ = self.git((["git"], Completed(stderr="Could not resolve host: github.com", returncode=128)))

        with self.assertRaises(release.ReleaseFailed) as raised:
            git.fetch("core")
        self.assertIn("Could not resolve host", str(raised.exception))


class BumpProblemsTest(unittest.TestCase):
    def test_each_line_is_a_problem_of_the_module_it_names(self):
        output = ("infrastructure\tinfrastructure depends on core 1.2.0, but core is 1.3.0\n"
                  "janitor\tjanitor has 1 commit(s) after v1.2.0\n"
                  "\tModules use different Cucumber versions: core 7.20.1, janitor 7.18.0\n")

        problems = develop_push.parse_bump_problems(output)

        self.assertEqual(problems, {
            "infrastructure": ["infrastructure depends on core 1.2.0, but core is 1.3.0"],
            "janitor": ["janitor has 1 commit(s) after v1.2.0"],
            "": ["Modules use different Cucumber versions: core 7.20.1, janitor 7.18.0"],
        })

    def test_bump_runs_its_check_in_the_workspace_and_a_failure_without_lines_stops_everything(self):
        run = FakeRun((["sh"], Completed(stderr="Module 'dsl' is not cloned next to the workspace.", returncode=1)))

        problems = develop_push.bump_problems(Path("/spool/workspace"), run=run)

        self.assertEqual(run.commands[0], ["sh", str(Path("/spool/workspace") / "bump.sh"), "--check", "--porcelain"])
        self.assertEqual(problems, {"": ["bump.sh --check failed: Module 'dsl' is not cloned next to the workspace."]})


class GitHubOpsForDevelopTest(unittest.TestCase):
    def ops(self, *replies, now=1_000_000.0):
        clock = FakeClock()
        clock.now = now
        run = FakeRun(*replies)
        return release.GitHubOps(run=run, clock=clock, sleep=clock.sleep, workflow=release.PUBLISH_WORKFLOW), run

    def test_publishing_again_runs_publish_develop_on_develop_without_inputs(self):
        ops, run = self.ops((["gh", "workflow"], Completed()))

        ops.dispatch("ingester")

        self.assertEqual(run.commands[0], ["gh", "workflow", "run", "publish-develop.yml",
                                           "-R", "Spool-FRAMEWORK/ingester", "--ref", "develop"])

    def test_the_run_of_a_push_is_found_by_its_commit(self):
        listing = json.dumps([{"databaseId": 9, "createdAt": "2026-11-17T19:00:00Z"}])
        view = json.dumps({"status": "completed", "conclusion": "success", "url": "https://runs/9"})
        ops, run = self.ops((["gh", "run", "list"], Completed(listing)), (["gh", "run", "view"], Completed(view)))

        result = ops.wait_for_run("core", 600, commit="abc123")

        self.assertEqual(result, release.Run("success", "https://runs/9"))
        listed = run.commands[0]
        self.assertEqual(listed[listed.index("--workflow") + 1], "publish-develop.yml")
        self.assertEqual(listed[listed.index("--commit") + 1], "abc123")
        self.assertNotIn("--event", listed)

    def test_a_publish_is_running_when_one_of_publish_develop_is_queued_or_in_progress(self):
        ops, run = self.ops((["gh", "run", "list"], Completed('[{"databaseId": 1}]')))

        self.assertTrue(ops.running("core"))
        self.assertIn("publish-develop.yml", run.commands[0])

    def test_the_latest_publish_from_develop_is_read_with_its_state(self):
        reply = json.dumps([{"status": "completed", "conclusion": "failure", "url": "https://runs/3"}])
        ops, _ = self.ops((["gh", "run", "list"], Completed(reply)))

        self.assertEqual(ops.latest_publish("core"), release.PublishState("completed", "failure", "https://runs/3"))

    def test_a_module_never_published_from_develop_has_no_latest_publish(self):
        ops, _ = self.ops((["gh", "run", "list"], Completed("[]")))

        self.assertIsNone(ops.latest_publish("core"))


if __name__ == "__main__":
    unittest.main()
