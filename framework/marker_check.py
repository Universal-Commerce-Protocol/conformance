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

"""Check the specification markers of loaded tests against the catalog.

Demo only. The check runs once tests are loaded and before any of them
runs: from `runner.py`, including with `--dry-run`, and from pytest through
`conftest.py`. Only the shopping suite has a catalog, so only its tests are
checked.

Problems stop the run in strict mode:
- a test with no marker;
- a cited ID that is not in the catalog, or that the catalog retired without
  a history;
- a cited ID that the review found states no requirement.

Warnings never stop it:
- a cited rule that only older releases state, which the catalog keeps as a
  removed rule;
- a cited rule that this suite cannot test;
- a cited rule outside the capabilities the test requires.
"""

import collections
from collections.abc import Iterable
import dataclasses
import unittest

from framework.decorators import MARKER_KINDS, SpecMarker, spec_marker
from framework.requirements import Catalog, CatalogError, load_catalog

# strict: problems stop the run; warn: report only; off: no check.
MODES = ("strict", "warn", "off")
CHECKED_PACKAGE = "shopping"
# What a marker may cite: a rule of the catalog's specification, or a rule
# that only older releases state.
CITABLE = ("rule", "removed")
STATUS_PROBLEMS = {
  "unknown": "is not in the catalog",
  "retired": "is retired: its rule was removed or rewritten",
  "not a rule": "is a sentence the review found states no requirement",
}


@dataclasses.dataclass(frozen=True)
class LoadedTest:
  """What the check needs to know about one loaded test.

  Attributes:
    test_id: The test's ID, as the runner reports it.
    module: The module the test is defined in.
    marker: The test's marker, if it has one.
    capabilities: The capabilities the test requires.

  """

  test_id: str
  module: str
  marker: SpecMarker | None
  capabilities: frozenset[str]


@dataclasses.dataclass
class CheckResult:
  """What the check found.

  Attributes:
    checked: How many tests were checked.
    kinds: How many tests carry each kind of marker; "unmarked" counts the
      tests with none.
    rules: The rules cited, by ID.
    problems: Findings that stop the run in strict mode.
    warnings: Findings that never stop it.

  """

  checked: int = 0
  kinds: collections.Counter[str] = dataclasses.field(
    default_factory=collections.Counter
  )
  rules: set[str] = dataclasses.field(default_factory=set)
  problems: list[str] = dataclasses.field(default_factory=list)
  warnings: list[str] = dataclasses.field(default_factory=list)

  def report(self) -> str:
    """Return the findings as text for the console."""
    counts = ", ".join(
      f"{self.kinds[kind]} {kind}" for kind in (*MARKER_KINDS[1:], "unmarked")
    )
    lines = [
      f"Requirements check: {self.checked} tests; "
      f"{self.kinds['spec_assert']} cite rules ({len(self.rules)} distinct "
      f"rules), {counts}"
    ]
    for title, findings in (
      ("problems", self.problems),
      ("warnings", self.warnings),
    ):
      if findings:
        lines.append(f"  {title} ({len(findings)}):")
        lines.extend(f"    - {finding}" for finding in findings)
    return "\n".join(lines) + "\n"


def in_scope(module: str) -> bool:
  """Return whether tests in a module are checked."""
  return module == CHECKED_PACKAGE or module.startswith(f"{CHECKED_PACKAGE}.")


def required_capabilities(test_class: object, method: object) -> frozenset[str]:
  """Return the capability names a test requires.

  As when the test runs, these are the names given to `requires_capability`
  on the test's class, or a class it inherits from, and on the test method.

  Args:
    test_class: The test's class.
    method: The test method.

  Returns:
    The capability names.

  """
  return frozenset(
    name
    for target in (test_class, method)
    for name, _ in getattr(target, "_required_capabilities", ())
  )


def from_unittest(test: unittest.TestCase) -> LoadedTest:
  """Describe a test case as unittest loads it."""
  method = getattr(test, test._testMethodName, None)
  return LoadedTest(
    test_id=test.id(),
    module=type(test).__module__,
    marker=spec_marker(test),
    capabilities=required_capabilities(type(test), method),
  )


def from_pytest_item(item: object) -> LoadedTest:
  """Describe a test as pytest collects it."""
  method = getattr(item, "obj", None)
  return LoadedTest(
    test_id=getattr(item, "nodeid", repr(item)),
    module=getattr(getattr(item, "module", None), "__name__", ""),
    marker=spec_marker(method) if method is not None else None,
    capabilities=required_capabilities(getattr(item, "cls", None), method),
  )


def check(tests: Iterable[LoadedTest], catalog: Catalog) -> CheckResult:
  """Check the marker of every shopping test against the catalog.

  Args:
    tests: The loaded tests; those outside the shopping suite are ignored.
    catalog: The requirements catalog.

  Returns:
    The counts, problems and warnings.

  """
  result = CheckResult()
  for test in tests:
    if not in_scope(test.module):
      continue
    result.checked += 1
    if test.marker is None:
      result.kinds["unmarked"] += 1
      result.problems.append(
        f"{test.test_id}: no marker ({', '.join(MARKER_KINDS)})"
      )
      continue
    result.kinds[test.marker.kind] += 1
    covered = catalog.with_parents(test.capabilities)
    for rule_id in test.marker.rule_ids:
      status = catalog.status(rule_id)
      if status not in CITABLE:
        result.problems.append(
          f"{test.test_id}: {rule_id} {STATUS_PROBLEMS[status]}"
        )
        continue
      result.rules.add(rule_id)
      rule = catalog.rule(rule_id)
      if status == "removed":
        result.warnings.append(
          f"{test.test_id}: {rule_id} was removed in {rule.until}; only "
          f"{', '.join(rule.versions)} state it"
        )
      if not rule.testable:
        result.warnings.append(
          f"{test.test_id}: {rule_id} is not testable by this suite "
          f"({rule.testable_reason})"
        )
      if test.capabilities and not covered.intersection(rule.capabilities):
        result.warnings.append(
          f"{test.test_id}: {rule_id} belongs to "
          f"{', '.join(rule.capabilities)}, which the test does not require"
        )
  return result


def evaluate(
  tests: list[LoadedTest], mode: str, catalog_dir: str | None = None
) -> tuple[str, bool]:
  """Run the check in a mode.

  Args:
    tests: The loaded tests.
    mode: One of `MODES`.
    catalog_dir: The catalog directory; by default this repository's.

  Returns:
    The report to show, empty when nothing was checked, and whether the run
    must stop.

  """
  if mode == "off" or not any(in_scope(test.module) for test in tests):
    return "", False
  try:
    catalog = load_catalog(catalog_dir)
  except CatalogError as error:
    return f"Requirements check: {error}\n", mode == "strict"
  result = check(tests, catalog)
  return result.report(), mode == "strict" and bool(result.problems)
