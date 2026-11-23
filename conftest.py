#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Configuration file for pytest."""

import pytest

from framework import marker_check
from framework.base_test import FLAGS


FLAGS(["pytest"])


def pytest_addoption(parser: pytest.Parser) -> None:
  """Add the option for the requirements check."""
  parser.addoption(
    "--requirements-check",
    choices=marker_check.MODES,
    default="strict",
    help="check the shopping tests' spec markers against the requirements "
    "catalog: strict stops on problems, warn only reports them, off skips the "
    "check (default: strict)",
  )


def pytest_collection_modifyitems(
  config: pytest.Config, items: list[pytest.Item]
) -> None:
  """Check the spec markers of the collected tests, as the runner does."""
  report, stop = marker_check.evaluate(
    [marker_check.from_pytest_item(item) for item in items],
    config.getoption("--requirements-check"),
  )
  reporter = config.pluginmanager.get_plugin("terminalreporter")
  if report and reporter is not None:
    reporter.ensure_newline()
    reporter.write(report)
  if stop:
    raise pytest.UsageError(
      "requirements check failed: fix the problems above, or pass "
      "--requirements-check=warn to run anyway"
    )
