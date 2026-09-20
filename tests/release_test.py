"""Tests for tools/release.py. Run them with: python3 -m unittest discover -s tests -p "*_test.py" """
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release  # noqa: E402

MODULES = ["core", "validator", "crawler", "janitor", "mounter", "ingester",
           "infrastructure", "dsl", "runtime", "watchdog"]
RELEASED = ["1.1.0", "1.1.2", "1.2.0"]


def pom(artifact, version, *dependencies):
    body = "".join(
        f"<dependency><groupId>io.github.spool-framework</groupId><artifactId>{a}</artifactId>"
        f"<version>{v}</version></dependency>" for a, v in dependencies)
    return (f"<project><groupId>io.github.spool-framework</groupId><artifactId>{artifact}</artifactId>"
            f"<version>{version}</version><dependencies>{body}</dependencies></project>")


def metadata(*versions):
    return "<metadata><versioning><versions>" + "".join(f"<version>{v}</version>" for v in versions) \
           + "</versions></versioning></metadata>"


def after_the_bump():
    """main after merging the bumped develop: four modules have a version that is not on Central yet."""
    return {
        "core": pom("core", "1.2.0-SNAPSHOT"),
        "validator": pom("validator", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT")),
        "crawler": pom("crawler", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT")),
        "janitor": pom("janitor", "1.2.1-SNAPSHOT", ("core", "1.2.0-SNAPSHOT")),
        "mounter": pom("mounter", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT")),
        "ingester": pom("ingester", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT"), ("validator", "1.2.0-SNAPSHOT")),
        "infrastructure": pom("infrastructure", "1.2.1-SNAPSHOT", ("core", "1.2.0-SNAPSHOT"),
                              ("janitor", "1.2.1-SNAPSHOT"), ("crawler", "1.2.0-SNAPSHOT"),
                              ("ingester", "1.2.0-SNAPSHOT"), ("mounter", "1.2.0-SNAPSHOT")),
        "dsl": pom("dsl", "1.3.0-SNAPSHOT", ("infrastructure", "1.2.1-SNAPSHOT")),
        "runtime": pom("runtime", "1.3.0-SNAPSHOT", ("dsl", "1.3.0-SNAPSHOT")),
        "watchdog": pom("watchdog", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT")),
    }


def world(main, develop=None, central=None):
    """A fetch function over fake poms on main and develop and fake Central metadata."""
    pages = {}
    for module, xml in main.items():
        pages[release.pom_url(module, release.RELEASE_BRANCH)] = xml
    for module, xml in (develop or main).items():
        pages[release.pom_url(module, release.DEVELOPMENT_BRANCH)] = xml
    for module in main:
        versions = (central or {}).get(module, RELEASED)
        pages[release.metadata_url(module)] = metadata(*versions)
    return pages.get


class ParsePomTest(unittest.TestCase):
    def test_reads_the_module_and_only_its_spool_dependencies(self):
        xml = ("<project><groupId>io.github.spool-framework</groupId><artifactId>dsl</artifactId>"
               "<version>1.3.0-SNAPSHOT</version><dependencies>"
               "<dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>"
               "<version>5.11.4</version></dependency>"
               "<dependency><groupId>io.github.spool-framework</groupId><artifactId>infrastructure</artifactId>"
               "<version>1.2.1-SNAPSHOT</version></dependency></dependencies></project>")

        self.assertEqual(release.parse_pom(xml), ("dsl", "1.3.0-SNAPSHOT", [("infrastructure", "1.2.1-SNAPSHOT")]))

    def test_a_pom_without_spool_modules_is_rejected(self):
        with self.assertRaises(ValueError):
            release.parse_pom("<project></project>")

    def test_the_workspace_pom_lists_its_modules(self):
        text = "<modules><module>../core</module><module>../dsl</module></modules>"

        self.assertEqual(release.modules_of(text), ["core", "dsl"])


class BuildPlanTest(unittest.TestCase):
    def test_after_the_bump_four_modules_are_pending_in_dependency_order(self):
        plan = release.build_plan(MODULES, world(after_the_bump()))

        self.assertEqual([e.module for e in plan.pending], ["janitor", "infrastructure", "dsl", "runtime"])
        self.assertEqual([e.tag for e in plan.pending], ["v1.2.1", "v1.2.1", "v1.3.0", "v1.3.0"])
        self.assertEqual(plan.problems, [])

    def test_the_order_does_not_depend_on_the_order_of_the_module_list(self):
        plan = release.build_plan(list(reversed(MODULES)), world(after_the_bump()))

        self.assertEqual([e.module for e in plan.pending], ["janitor", "infrastructure", "dsl", "runtime"])

    def test_a_module_comes_after_everything_it_depends_on(self):
        order = [e.module for e in release.build_plan(list(reversed(MODULES)), world(after_the_bump())).entries]

        self.assertLess(order.index("core"), order.index("janitor"))
        self.assertLess(order.index("janitor"), order.index("infrastructure"))
        self.assertLess(order.index("infrastructure"), order.index("dsl"))
        self.assertLess(order.index("dsl"), order.index("runtime"))

    def test_a_snapshot_whose_base_is_already_on_central_is_not_pending(self):
        plan = release.build_plan(MODULES, world(after_the_bump()))

        self.assertFalse(next(e for e in plan.entries if e.module == "core").pending)

    def test_watchdog_is_never_released_to_central(self):
        plan = release.build_plan(MODULES, world(after_the_bump(), central={"watchdog": []}))

        self.assertNotIn("watchdog", [e.module for e in plan.pending])

    def test_a_module_that_was_never_published_is_pending(self):
        plan = release.build_plan(MODULES, world(after_the_bump(), central={"mounter": []}))

        self.assertIn("mounter", [e.module for e in plan.pending])

    def test_nothing_is_pending_when_everything_is_on_central(self):
        main = after_the_bump()
        for module in ("janitor", "infrastructure"):
            main[module] = main[module].replace("1.2.1", "1.2.0")
        main["dsl"] = main["dsl"].replace("1.3.0", "1.2.0").replace("1.2.1", "1.2.0")
        main["runtime"] = main["runtime"].replace("1.3.0", "1.2.0")
        main["infrastructure"] = main["infrastructure"].replace("1.2.1", "1.2.0")

        plan = release.build_plan(MODULES, world(main))

        self.assertEqual(plan.pending, [])
        self.assertIn("Nothing to release", release.render(plan))

    def test_a_dependency_on_a_version_that_will_never_exist_is_a_problem(self):
        main = after_the_bump()
        main["janitor"] = pom("janitor", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT"))   # forgot to bump it

        plan = release.build_plan(MODULES, world(main))

        self.assertEqual(plan.problems, [
            "infrastructure: depends on janitor 1.2.1, which is neither on Central nor released before it in this plan"])

    def test_a_dependency_released_earlier_in_the_same_plan_is_fine(self):
        plan = release.build_plan(MODULES, world(after_the_bump()))

        self.assertEqual(plan.problems, [])

    def test_a_version_that_is_not_major_minor_patch_is_a_problem(self):
        main = after_the_bump()
        main["runtime"] = pom("runtime", "1.3-SNAPSHOT", ("dsl", "1.3.0-SNAPSHOT"))

        self.assertEqual(release.build_plan(MODULES, world(main)).problems,
                         ["runtime: version 1.3-SNAPSHOT is not major.minor.patch"])

    def test_develop_ahead_of_main_is_reported_as_a_note(self):
        develop = after_the_bump()
        main = dict(develop)
        main["janitor"] = pom("janitor", "1.2.0-SNAPSHOT", ("core", "1.2.0-SNAPSHOT"))

        plan = release.build_plan(MODULES, world(main, develop=develop))

        note = next(e.notes for e in plan.entries if e.module == "janitor")[0]
        self.assertIn("develop is at 1.2.1-SNAPSHOT but main is at 1.2.0-SNAPSHOT", note)
        self.assertIn("merge develop into main", note)

    def test_a_circular_dependency_is_rejected(self):
        main = {"a": pom("a", "1.0.0-SNAPSHOT", ("b", "1.0.0-SNAPSHOT")),
                "b": pom("b", "1.0.0-SNAPSHOT", ("a", "1.0.0-SNAPSHOT"))}

        with self.assertRaises(ValueError):
            release.build_plan(["a", "b"], world(main, central={"a": [], "b": []}))

    def test_a_module_whose_pom_cannot_be_read_stops_the_plan(self):
        with self.assertRaises(LookupError):
            release.build_plan(["core"], lambda url: None)


class RenderTest(unittest.TestCase):
    def test_the_table_numbers_only_the_pending_modules(self):
        text = release.render(release.build_plan(MODULES, world(after_the_bump())))

        pending_lines = [line for line in text.splitlines() if "PENDING" in line]
        self.assertEqual([line.split()[0] for line in pending_lines], ["1", "2", "3", "4"])
        self.assertIn("v1.3.0", text)

    def test_the_markdown_version_is_a_table(self):
        text = release.render(release.build_plan(MODULES, world(after_the_bump())), markdown=True)

        self.assertTrue(text.startswith("| Order | Module | Version on main | Tag | State |"))


class FakeOps:
    """Stands in for GitHub and Central. Each module can be told to fail at one step."""

    def __init__(self, fail_at=None, existing_tags=(), central_after=0):
        self.fail_at = fail_at or {}
        self.existing_tags = set(existing_tags)
        self.central_after = central_after     # how many times Central says no before it says yes
        self.calls = []
        self._asked = {}

    def tag_exists(self, module, tag):
        return tag in self.existing_tags

    def dispatch(self, module, tag):
        self.calls.append(("dispatch", module, tag))

    def wait_for_run(self, module, timeout):
        self.calls.append(("wait", module))
        outcome = self.fail_at.get(module)
        if outcome == "timeout":
            return None
        if outcome == "workflow":
            return release.Run("failure", f"https://runs/{module}", "[ERROR] Failed to execute goal deploy")
        return release.Run("success", f"https://runs/{module}")

    def on_central(self, module, version):
        self._asked[module] = self._asked.get(module, 0) + 1
        self.calls.append(("central", module))
        return self._asked[module] > self.central_after

    def resolves(self, module, version):
        self.calls.append(("resolves", module))
        return self.fail_at.get(module) != "resolve"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run_release(ops, main=None, **options):
    clock = FakeClock()
    plan = release.build_plan(MODULES, world(main or after_the_bump()))
    results = release.release(plan, ops, log=lambda _: None, clock=clock, sleep=clock.sleep, **options)
    return results, clock


class WaitUntilTest(unittest.TestCase):
    def test_returns_as_soon_as_the_condition_holds(self):
        clock = FakeClock()

        self.assertTrue(release.wait_until(lambda: True, 100, clock, clock.sleep))
        self.assertEqual(clock.slept, [])

    def test_waits_a_little_longer_each_time_up_to_a_cap(self):
        clock, answers = FakeClock(), iter([False] * 6 + [True])

        self.assertTrue(release.wait_until(lambda: next(answers), 10_000, clock, clock.sleep,
                                           first=20, factor=2, cap=100))
        self.assertEqual(clock.slept, [20, 40, 80, 100, 100, 100])

    def test_gives_up_when_the_time_is_over(self):
        clock = FakeClock()

        self.assertFalse(release.wait_until(lambda: False, 100, clock, clock.sleep, first=60))
        self.assertLessEqual(clock.now, 100)


class ReleaseTest(unittest.TestCase):
    def test_releases_every_pending_module_in_order(self):
        ops = FakeOps()

        results, _ = run_release(ops)

        self.assertEqual([r.module for r in results], ["janitor", "infrastructure", "dsl", "runtime"])
        self.assertTrue(all(r.outcome == "released" for r in results))
        self.assertEqual([c[1] for c in ops.calls if c[0] == "dispatch"],
                         ["janitor", "infrastructure", "dsl", "runtime"])

    def test_each_module_is_checked_before_the_next_one_starts(self):
        ops = FakeOps()

        run_release(ops)

        janitor_done = ops.calls.index(("resolves", "janitor"))
        infrastructure_start = ops.calls.index(("dispatch", "infrastructure", "v1.2.1"))
        self.assertLess(janitor_done, infrastructure_start)

    def test_a_failed_workflow_stops_everything_after_it(self):
        ops = FakeOps(fail_at={"infrastructure": "workflow"})

        results, _ = run_release(ops)

        self.assertEqual([r.outcome for r in results], ["released", "failed", "not attempted", "not attempted"])
        self.assertIn("https://runs/infrastructure", results[1].detail)
        self.assertNotIn("dsl", [c[1] for c in ops.calls if c[0] == "dispatch"])

    def test_a_workflow_that_never_finishes_stops_the_release(self):
        results, _ = run_release(FakeOps(fail_at={"janitor": "timeout"}))

        self.assertEqual(results[0].outcome, "failed")
        self.assertIn("did not finish", results[0].detail)
        self.assertEqual([r.outcome for r in results[1:]], ["not attempted"] * 3)

    def test_a_version_that_never_reaches_central_stops_the_release(self):
        results, _ = run_release(FakeOps(central_after=10 ** 6), central_timeout=600)

        self.assertEqual(results[0].outcome, "failed")
        self.assertIn("Central does not show 1.2.1", results[0].detail)

    def test_it_waits_for_central_instead_of_giving_up_at_the_first_no(self):
        ops = FakeOps(central_after=3)

        results, clock = run_release(ops)

        self.assertEqual(results[0].outcome, "released")
        self.assertGreater(clock.now, 0)

    def test_a_version_that_cannot_be_resolved_stops_the_release(self):
        results, _ = run_release(FakeOps(fail_at={"dsl": "resolve"}))

        self.assertEqual([r.outcome for r in results], ["released", "released", "failed", "not attempted"])
        self.assertIn("cannot be resolved", results[2].detail)

    def test_a_tag_that_already_exists_is_never_dispatched_over(self):
        ops = FakeOps(existing_tags={"v1.2.1"})

        results, _ = run_release(ops)

        self.assertEqual(results[0].outcome, "failed")
        self.assertIn("already exists", results[0].detail)
        self.assertEqual([c for c in ops.calls if c[0] == "dispatch"], [])

    def test_the_time_of_each_module_is_recorded(self):
        results, _ = run_release(FakeOps(central_after=2))

        self.assertGreater(results[0].seconds, 0)

    def test_the_result_table_shows_what_was_not_attempted(self):
        results, _ = run_release(FakeOps(fail_at={"janitor": "workflow"}))

        text = release.render_results(results)

        self.assertIn("failed", text)
        self.assertEqual(text.count("not attempted"), 3)


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class FakeGh:
    """Answers the gh and mvn commands GitHubOps runs, from a list of (prefix, reply)."""

    def __init__(self, *replies):
        self.replies, self.commands = list(replies), []

    def __call__(self, command, **_):
        self.commands.append(command)
        for prefix, reply in self.replies:
            if command[: len(prefix)] == prefix:
                return reply
        raise AssertionError(f"unexpected command {command}")


def github_ops(gh, exists=lambda url: True, clock=None):
    clock = clock or FakeClock()
    return release.GitHubOps(run=gh, exists=exists, clock=clock, sleep=clock.sleep), clock


class GitHubOpsTest(unittest.TestCase):
    def test_a_missing_tag_is_told_apart_from_a_failed_call(self):
        ops, _ = github_ops(FakeGh((["gh", "api"], Completed(stderr="gh: Not Found (HTTP 404)", returncode=1))))

        self.assertFalse(ops.tag_exists("janitor", "v1.2.1"))

    def test_an_existing_tag_is_found(self):
        ops, _ = github_ops(FakeGh((["gh", "api"], Completed(stdout="{}"))))

        self.assertTrue(ops.tag_exists("janitor", "v1.2.1"))

    def test_a_failed_call_is_not_taken_for_a_missing_tag(self):
        ops, _ = github_ops(FakeGh((["gh", "api"], Completed(stderr="HTTP 401: Bad credentials", returncode=1))))

        with self.assertRaises(release.ReleaseFailed):
            ops.tag_exists("janitor", "v1.2.1")

    def test_dispatch_runs_the_release_workflow_of_the_module_on_main_with_the_tag(self):
        gh = FakeGh((["gh", "workflow"], Completed()))
        ops, _ = github_ops(gh)

        ops.dispatch("janitor", "v1.2.1")

        self.assertEqual(gh.commands[0], ["gh", "workflow", "run", "release.yml", "-R", "Spool-FRAMEWORK/janitor",
                                          "--ref", "main", "-f", "tag=v1.2.1"])

    def test_a_dispatch_that_gh_rejects_stops_the_release(self):
        ops, _ = github_ops(FakeGh((["gh", "workflow"], Completed(stderr="no workflow", returncode=1))))

        with self.assertRaises(release.ReleaseFailed):
            ops.dispatch("janitor", "v1.2.1")

    def test_it_follows_the_run_started_by_its_own_dispatch_and_not_an_older_one(self):
        clock = FakeClock()
        clock.now = 1_000_000.0
        old = "1970-01-01T00:00:00Z"
        new = datetime_of(1_000_005.0)
        listing = json.dumps([{"databaseId": 1, "createdAt": old}, {"databaseId": 2, "createdAt": new}])
        view = json.dumps({"status": "completed", "conclusion": "success", "url": "https://runs/2"})
        gh = FakeGh((["gh", "workflow"], Completed()), (["gh", "run", "list"], Completed(listing)),
                    (["gh", "run", "view"], Completed(view)))
        ops, _ = github_ops(gh, clock=clock)

        ops.dispatch("janitor", "v1.2.1")
        run = ops.wait_for_run("janitor", 3600)

        self.assertEqual(run, release.Run("success", "https://runs/2"))
        self.assertEqual(gh.commands[-1][:4], ["gh", "run", "view", "2"])

    def test_it_keeps_asking_while_the_run_is_in_progress(self):
        clock = FakeClock()
        clock.now = 1_000_000.0
        listing = json.dumps([{"databaseId": 7, "createdAt": datetime_of(1_000_001.0)}])
        states = iter([{"status": "queued"}, {"status": "in_progress"},
                       {"status": "completed", "conclusion": "failure", "url": "u"}])

        class Gh(FakeGh):
            def __call__(self, command, **kwargs):
                if command[:3] == ["gh", "run", "view"] and "--json" in command:
                    return Completed(json.dumps(next(states)))
                if command[:3] == ["gh", "run", "view"]:
                    return Completed(returncode=1)         # the log of the failed run
                return super().__call__(command, **kwargs)

        ops, _ = github_ops(Gh((["gh", "workflow"], Completed()), (["gh", "run", "list"], Completed(listing))),
                            clock=clock)
        ops.dispatch("janitor", "v1.2.1")

        self.assertEqual(ops.wait_for_run("janitor", 3600), release.Run("failure", "u"))

    def test_a_version_is_on_central_only_when_its_pom_and_its_jar_are(self):
        seen = []
        ops, _ = github_ops(FakeGh(), exists=lambda url: seen.append(url) or url.endswith(".pom"))

        self.assertFalse(ops.on_central("janitor", "1.2.1"))
        self.assertEqual(seen[0], "https://repo1.maven.org/maven2/io/github/spool-framework/janitor/1.2.1/janitor-1.2.1.pom")

    def test_resolving_uses_a_settings_file_that_sends_everything_to_central(self):
        gh = FakeGh((["mvn"], Completed()))
        ops, _ = github_ops(gh)

        self.assertTrue(ops.resolves("janitor", "1.2.1"))
        command = gh.commands[0]
        self.assertIn("-Dartifact=io.github.spool-framework:janitor:1.2.1", command)
        self.assertIn("-s", command)


def datetime_of(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutputsTest(unittest.TestCase):
    def test_a_workflow_is_told_how_many_modules_are_pending_and_how_many_problems_there_are(self):
        import tempfile
        plan = release.build_plan(MODULES, world(after_the_bump()))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "output"

            release.write_outputs(str(path), plan)

            self.assertEqual(path.read_text(), "pending=4\nproblems=0\n")

    def test_nothing_is_written_when_no_file_is_given(self):
        release.write_outputs(None, release.build_plan(MODULES, world(after_the_bump())))


# Lines as gh prints them for a failed step: job, step and time in front of each one.
FAILED_LOG = "\n".join([
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:19:59.8111103Z [WARNING] public Janitor(JanitorStrategy strategy) {",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3471477Z [ERROR] Unable to upload bundle for deployment: Deployment",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3473968Z java.lang.RuntimeException: Invalid request. Status: 401",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3551966Z [INFO] BUILD FAILURE",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3559854Z [ERROR] Failed to execute goal central-publishing-maven-plugin:0.7.0:publish",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3562895Z [ERROR] ",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3564032Z [ERROR] To see the full stack trace, re-run Maven with the -e switch.",
    "release / publish\tUNKNOWN STEP\t2026-09-20T17:20:11.3566311Z ##[error]Process completed with exit code 1.",
])


class ErrorLinesTest(unittest.TestCase):
    def test_keeps_only_the_error_lines_without_what_gh_puts_in_front(self):
        self.assertEqual(release.error_lines(FAILED_LOG).splitlines(), [
            "[ERROR] Unable to upload bundle for deployment: Deployment",
            "java.lang.RuntimeException: Invalid request. Status: 401",
            "[ERROR] Failed to execute goal central-publishing-maven-plugin:0.7.0:publish",
            "[ERROR] To see the full stack trace, re-run Maven with the -e switch.",
            "[ERROR] Process completed with exit code 1.",
        ])

    def test_only_the_last_lines_are_kept_when_there_are_many(self):
        log = "\n".join(f"job\tstep\t2026-09-20T17:00:00.0000000Z [ERROR] line {n}" for n in range(30))

        self.assertEqual(release.error_lines(log, limit=3).splitlines(), ["[ERROR] line 27", "[ERROR] line 28", "[ERROR] line 29"])

    def test_a_log_without_errors_gives_nothing(self):
        self.assertEqual(release.error_lines("job\tstep\t2026-09-20T17:00:00.0000000Z all fine"), "")


class FailureExcerptTest(unittest.TestCase):
    def failed_run(self, log_reply):
        clock = FakeClock()
        clock.now = 1_000_000.0
        listing = json.dumps([{"databaseId": 9, "createdAt": datetime_of(1_000_001.0)}])
        view = json.dumps({"status": "completed", "conclusion": "failure", "url": "https://runs/9"})
        gh = FakeGh((["gh", "workflow"], Completed()), (["gh", "run", "list"], Completed(listing)),
                    (["gh", "run", "view", "9", "-R", "Spool-FRAMEWORK/janitor", "--json"], Completed(view)),
                    (["gh", "run", "view", "9", "-R", "Spool-FRAMEWORK/janitor", "--log-failed"], log_reply))
        ops, _ = github_ops(gh, clock=clock)
        ops.dispatch("janitor", "v1.2.1")
        return ops.wait_for_run("janitor", 3600)

    def test_a_failed_run_comes_with_its_error_lines(self):
        run = self.failed_run(Completed(FAILED_LOG))

        self.assertEqual(run.conclusion, "failure")
        self.assertIn("[ERROR] Unable to upload bundle for deployment: Deployment", run.excerpt)

    def test_a_log_that_cannot_be_read_does_not_hide_the_failure(self):
        run = self.failed_run(Completed(stderr="log expired", returncode=1))

        self.assertEqual((run.conclusion, run.excerpt), ("failure", ""))

    def test_a_successful_run_does_not_ask_for_the_log(self):
        clock = FakeClock()
        clock.now = 1_000_000.0
        listing = json.dumps([{"databaseId": 9, "createdAt": datetime_of(1_000_001.0)}])
        view = json.dumps({"status": "completed", "conclusion": "success", "url": "u"})
        gh = FakeGh((["gh", "workflow"], Completed()), (["gh", "run", "list"], Completed(listing)),
                    (["gh", "run", "view"], Completed(view)))
        ops, _ = github_ops(gh, clock=clock)
        ops.dispatch("janitor", "v1.2.1")

        ops.wait_for_run("janitor", 3600)

        self.assertNotIn("--log-failed", [arg for command in gh.commands for arg in command])


class ExcerptReportTest(unittest.TestCase):
    def test_the_error_lines_of_a_failed_module_end_up_in_its_result_and_in_the_report(self):
        results, _ = run_release(FakeOps(fail_at={"janitor": "workflow"}))

        self.assertEqual(results[0].excerpt, "[ERROR] Failed to execute goal deploy")
        self.assertIn("janitor v1.2.1\n[ERROR] Failed to execute goal deploy", release.render_excerpts(results))
        self.assertIn("```", release.render_excerpts(results, markdown=True))

    def test_the_error_lines_are_logged_when_the_release_stops(self):
        lines = []
        plan = release.build_plan(MODULES, world(after_the_bump()))
        clock = FakeClock()

        release.release(plan, FakeOps(fail_at={"janitor": "workflow"}), log=lines.append, clock=clock,
                        sleep=clock.sleep)

        self.assertIn("      [ERROR] Failed to execute goal deploy", lines)

    def test_there_is_no_report_when_nothing_failed(self):
        results, _ = run_release(FakeOps())

        self.assertEqual(release.render_excerpts(results), "")


if __name__ == "__main__":
    unittest.main()
