"""Light static checks on the packaging metadata.

We do NOT run ``pip install`` here (slow, mutates the environment, and not
something a unit suite should do). Instead we parse ``pyproject.toml`` with
the stdlib and assert the fields that downstream tooling (PyPI, pip,
type-checkers) rely on. This catches the easy mistakes -- missing entry
point, forgotten license, bundled models -- without leaving the repo.
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

# Make repo root importable for path checks below.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yang_xml_gen.cli import main  # noqa: E402,F401  (importability check)
from yang_xml_gen.rpc_worker import main as rpc_main  # noqa: E402,F401

PYPROJECT = ROOT / "pyproject.toml"
LICENSE = ROOT / "LICENSE"
PY_TYPED = ROOT / "src" / "yang_xml_gen" / "py.typed"


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


class PyprojectTests(unittest.TestCase):
    """The fields a downstream user / pip relies on must be present."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_pyproject()

    def test_build_system_uses_setuptools(self):
        # Anything else would be surprising for this project; pinning the
        # backend keeps the build reproducible.
        self.assertEqual(
            self.data["build-system"]["build-backend"],
            "setuptools.build_meta",
        )

    def test_project_name_and_version_present(self):
        proj = self.data["project"]
        self.assertEqual(proj["name"], "yang-xml-gen")
        # Version must be a valid PEP 440-ish string (we just check it's
        # non-empty and starts with a digit -- pyproject validation itself
        # is setuptools' job).
        self.assertRegex(proj["version"], r"^\d+\.\d+\.\d+")

    def test_requires_python_floor(self):
        # The code uses PEP 604 unions (X | Y) and `match`-free 3.10+
        # syntax; declare the floor honestly.
        self.assertEqual(self.data["project"]["requires-python"], ">=3.10")

    def test_runtime_dependencies_pin_floors(self):
        deps = self.data["project"]["dependencies"]
        # pyang is the schema engine; PyYAML parses spec files. Both must
        # be declared with a floor so a fresh install doesn't pull ancient
        # releases.
        self.assertIn("pyang>=2.5", deps)
        self.assertIn("PyYAML>=6.0", deps)

    def test_console_script_entry_point(self):
        scripts = self.data["project"]["scripts"]
        self.assertEqual(scripts["yang-xml-gen"], "yang_xml_gen.cli:main")
        # The long-lived RPC worker entry point (used by external hosts like
        # the netconfSub Node backend to embed us as a plugin).
        self.assertEqual(
            scripts["yang-xml-gen-rpc"], "yang_xml_gen.rpc_worker:main"
        )

    def test_src_layout_package_discovery(self):
        # package-dir "" -> "src" is what makes `from yang_xml_gen import ...`
        # work from the installed wheel without a top-level yang_xml_gen/
        # directory at repo root.
        setuptools = self.data["tool"]["setuptools"]
        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertEqual(setuptools["packages"]["find"]["where"], ["src"])

    def test_py_typed_marker_shipped(self):
        # PEP 561: without py.typed in package-data, type-checkers won't
        # see our inline annotations after install.
        pkg_data = self.data["tool"]["setuptools"]["package-data"]
        self.assertIn("py.typed", pkg_data["yang_xml_gen"])

    def test_license_points_at_file(self):
        # `license = { file = "LICENSE" }` so the MIT text lands in the
        # wheel metadata (classifiers alone don't ship the text).
        self.assertEqual(self.data["project"]["license"]["file"], "LICENSE")


class FileSystemTests(unittest.TestCase):
    """The files pyproject references must actually exist on disk."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_pyproject()

    def test_license_file_exists_and_contains_mit(self):
        self.assertTrue(LICENSE.is_file(), f"{LICENSE} missing")
        text = LICENSE.read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        # The copyright line is filled in (not a TODO placeholder).
        self.assertIn("Copyright (c)", text)

    def test_py_typed_marker_exists(self):
        self.assertTrue(PY_TYPED.is_file(), f"{PY_TYPED} missing")

    def test_readme_referenced_exists(self):
        readme = ROOT / self.data["project"]["readme"]
        self.assertTrue(readme.is_file(), f"{readme} missing")

    def test_models_directory_not_in_package_data(self):
        # The whole point of step 7's packaging decision: models/ is large
        # upstream artefact and must NOT be bundled. We assert that no
        # package-data entry references it.
        pkg_data = self.data["tool"]["setuptools"]["package-data"]
        for entries in pkg_data.values():
            for e in entries:
                self.assertNotIn("models", e)


class ImportabilityTests(unittest.TestCase):
    """Smoke-test that the entry-point target is actually callable."""

    def test_cli_main_is_callable(self):
        # If `yang-xml-gen` is installed, pip wires this up; we just make
        # sure the symbol referenced by the entry point exists and is a
        # callable, so the entry point won't blow up at launch.
        self.assertTrue(callable(main))

    def test_rpc_worker_main_is_callable(self):
        # Same check for the `yang-xml-gen-rpc` entry point.
        self.assertTrue(callable(rpc_main))


if __name__ == "__main__":
    unittest.main()
