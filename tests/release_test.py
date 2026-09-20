"""Tests for tools/release.py. Run them with: python3 -m unittest discover -s tests -p "*_test.py" """
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


if __name__ == "__main__":
    unittest.main()
