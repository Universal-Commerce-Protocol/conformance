# Copyright 2026 UCP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the suite registry completeness check.

Lives in tests/ with the other server-free framework and runner unit tests, so
`pytest tests` runs it and the check under test skips the whole directory
rather than discovering these cases as conformance modules.
"""

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import runner


class UnregisteredModulesTest(unittest.TestCase):
  """Completeness of SUITE_REGISTRY against the modules in the tree."""

  def test_registry_in_this_tree_is_complete(self):
    """Every module shipped here is reachable from --suite=all."""
    self.assertEqual(runner.unregistered_modules(), [])

  def test_module_absent_from_the_registry_is_reported(self):
    """A module the registry does not name is returned."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      (root / "registered_test.py").touch()
      (root / "forgotten_test.py").touch()
      found = runner.unregistered_modules(
        root=root, registered={"registered_test"}
      )
    self.assertEqual(found, ["forgotten_test"])

  def test_a_registered_module_is_not_reported(self):
    """A module the registry names is not returned."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      (root / "registered_test.py").touch()
      found = runner.unregistered_modules(
        root=root, registered={"registered_test"}
      )
    self.assertEqual(found, [])

  def test_nested_modules_are_discovered_by_dotted_path(self):
    """A tiered layout reports a dotted path, not a bare file name."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      nested = root / "shopping" / "checkout"
      nested.mkdir(parents=True)
      (nested / "protocol_test.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, ["shopping.checkout.protocol_test"])

  def test_the_harness_package_is_not_a_suite_module(self):
    """framework/base_test.py is the shared base class, not a suite module."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      (root / "framework").mkdir()
      (root / "framework" / "base_test.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, [])

  def test_a_test_file_beside_the_runner_is_not_discovered(self):
    """test_*.py is not a conformance module and must not be reported."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      (root / "test_suite_registry.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, [])

  def test_a_hidden_directory_is_not_searched(self):
    """A virtualenv or similar can ship its own *_test.py files."""
    with tempfile.TemporaryDirectory() as directory:
      root = pathlib.Path(directory)
      venv = root / ".venv" / "lib"
      venv.mkdir(parents=True)
      (venv / "vendored_test.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, [])

  def test_the_framework_unit_test_directory_is_not_searched(self):
    """`tests/` holds server-free framework and runner unit tests, not suites.

    `pytest tests` runs them, so SUITE_REGISTRY does not name them and
    `--verify-registry` must not report them. Without `tests` in the skip set
    the first module in that directory fails CI immediately.
    """
    with tempfile.TemporaryDirectory() as d:
      root = pathlib.Path(d)
      (root / "tests").mkdir()
      (root / "tests" / "remote_target_test.py").touch()
      (root / "tests" / "suite_registry_test.py").touch()
      (root / "shopping").mkdir()
      (root / "shopping" / "cart_test.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, ["shopping.cart_test"])

  def test_local_build_directories_are_not_searched(self):
    """`build/` and `dist/` are local wheel builds, not source."""
    with tempfile.TemporaryDirectory() as d:
      root = pathlib.Path(d)
      for sub in ("build", "dist"):
        (root / sub).mkdir()
        (root / sub / "stale_test.py").touch()
      found = runner.unregistered_modules(root=root, registered=set())
    self.assertEqual(found, [])

  def test_verify_registry_exit_code_follows_the_report(self):
    """--verify-registry exits 1 when a module is unreachable, else 0."""
    err, out = io.StringIO(), io.StringIO()
    incomplete = mock.patch.object(
      runner, "unregistered_modules", return_value=["x_test"]
    )
    with (
      incomplete,
      contextlib.redirect_stderr(err),
      contextlib.redirect_stdout(out),
    ):
      self.assertEqual(runner.main(["--verify-registry"]), 1)
    self.assertIn("x_test", err.getvalue())
    self.assertEqual(out.getvalue(), "")
    complete = mock.patch.object(
      runner, "unregistered_modules", return_value=[]
    )
    with complete, contextlib.redirect_stdout(out):
      self.assertEqual(runner.main(["--verify-registry"]), 0)
    self.assertIn("OK", out.getvalue())


if __name__ == "__main__":
  unittest.main()
