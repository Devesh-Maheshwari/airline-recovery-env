"""Every file that states the release version must state the same one, and CI runs what exists."""
import importlib.util
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _match(path, pattern):
    found = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    if found is None:
        raise AssertionError(f"{path} does not declare a version")
    return found.group(1)


class ReleaseConsistencyTests(unittest.TestCase):
    def test_version_strings_agree(self):
        versions = {
            "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
            "airline_recovery/__init__.py": _match("airline_recovery/__init__.py", r"""^__version__\s*=\s*["']([^"']+)["']"""),
            "openenv.yaml": _match("openenv.yaml", r"""^version:\s*["']?([^"'\s#]+)"""),
            "CITATION.cff": _match("CITATION.cff", r"""^version:\s*["']?([^"'\s#]+)"""),
        }
        self.assertEqual(len(set(versions.values())), 1, versions)
        self.assertRegex(versions["pyproject.toml"], r"^\d+\.\d+\.\d+")


WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


@unittest.skipUnless(WORKFLOW.is_file(), "the CI workflow is not part of this checkout")
class WorkflowTests(unittest.TestCase):
    def test_project_modules_run_by_ci_are_importable(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        modules = sorted(set(re.findall(r"python\"?\s+-m\s+(airline[\w.-]*)", text)))
        self.assertIn("airline_recovery", modules)
        for name in modules:
            with self.subTest(module=name):
                # A console-script name such as airline-recovery is not a module: `python -m` cannot run it.
                self.assertIsNotNone(importlib.util.find_spec(name), name)

    def test_core_job_timeout_leaves_room_for_the_full_suite(self):
        core = re.search(r"^  core:\n((?:    .*\n|\s*\n)+)", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
        minutes = re.search(r"^    timeout-minutes:\s*(\d+)", core.group(1), re.MULTILINE)
        self.assertGreaterEqual(int(minutes.group(1)), 30)


if __name__ == "__main__":
    unittest.main()
