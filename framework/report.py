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

"""Build the requirements coverage report from the catalog and the markers.

The runner writes the report with `--requirements_report=<dir>`.
For each capability, the report counts the rules, the testable rules and the
testable MUST and MUST NOT rules, and how many of those a test checks fully,
or only in part (`partly` in a `spec_assert` marker). It also lists every rule
with the tests that cite it, and every shopping test with its marker.

With `--dry-run`, the report reads only the catalog and the tests' markers,
so it needs no server, and it says which rules have a test. After a run, it
also gives each test's outcome and each rule's result: passed, failed, not
run, or no test.

The report is written three ways: `matrix.json` for tools, `matrix.md` to
read on GitHub, and `matrix.html`, one page with no external resources.
"""

import collections
from collections.abc import Callable, Iterable, Mapping, Sequence
import dataclasses
import html
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from framework.decorators import MARKER_KINDS
from framework.discovery import UcpProfile
from framework.marker_check import LoadedTest, in_scope
from framework.outcomes import (
  CAPABILITY,
  ENVIRONMENT,
  ERROR,
  FAILED,
  NO_TRIGGER,
  NOT_APPLICABLE,
  NOT_EXERCISED,
  NOT_RUN,
  OTHER,
  PASSED,
  SKIP_CATEGORIES,
  SKIP_LABELS,
  SKIPPED,
  STATUSES,
  Outcome,
)
from framework.requirements import (
  DEFAULT_CATALOG_DIR,
  DRAFT,
  Capability,
  Catalog,
  Rule,
  load_catalog,
)
from framework.versions import rule_applies

# 2: "covered" counts rules a test checks fully; rules checked only in part
# are counted and listed apart. Version 2 also has the view by release
# (`versions`) and each rule's `since` and `versions`.
# 3: the `not_applicable` test outcome and rule result, for rules the version
# the server implements doesn't state.
# 4: each skipped test's `category`, each not-run rule's `not_run_category`,
# the `not_run_by_category` counts and the `upgrade_list`.
# 5: `error` as a rule result of its own, apart from `failed`, when a test
# raised an unexpected exception and none failed an assertion; `error` and
# `must_error` in `results`.
FORMAT_VERSION = 5
JSON_FILE = "matrix.json"
MARKDOWN_FILE = "matrix.md"
HTML_FILE = "matrix.html"
# The findings document, next to the catalog directory.
FINDINGS_FILE = "findings.md"
TITLE = "Shopping requirements coverage"
# The levels of an absolute requirement or prohibition.
MUST_LEVELS = frozenset({"MUST", "MUST NOT", "SHALL", "SHALL NOT"})
UNMARKED = "unmarked"
ID_PREFIX = "REQ-SHOPPING-"
CAPABILITY_PREFIX = "dev.ucp.shopping."
SPEC_PAGES = "docs/specification/shopping/"
SPEC_REPO = "https://github.com/Universal-Commerce-Protocol/ucp"
KIND_TITLES = {
  "spec_assert": "Tests that cite rules",
  "no_rule": "Behavior the spec states without a keyword",
  "scenario": "Checks of one store's data or features",
  "outside_catalog": "Rules on pages outside the catalog",
  "older_version": "Checks of an older release",
  UNMARKED: "Tests without a marker",
}
# The result of a rule no test cites.
NO_TEST = "no_test"
# The results of a rule with a test, in the order the report lists them.
RULE_RESULTS = (PASSED, FAILED, ERROR, NOT_RUN, NOT_APPLICABLE)
# The lists under the results table, with their titles.
RESULT_LISTS = (
  ("Rules that failed", FAILED),
  ("Rules whose tests hit an error", ERROR),
  ("Rules whose tests didn't run", NOT_RUN),
  ("Rules the version the server implements doesn't state", NOT_APPLICABLE),
)
# Why a rule wasn't run, when its tests give different reasons: the first of
# these that any of them gives. A trigger the merchant can declare comes
# first, then the run's environment, then a reason without a category, which
# wants a look; a server that didn't exercise the rule or doesn't advertise
# a capability leaves nothing to do.
NOT_RUN_PRECEDENCE = (NO_TRIGGER, ENVIRONMENT, OTHER, NOT_EXERCISED, CAPABILITY)
# The rules a server should act on: those that failed, and those not run
# because the fixtures declare no trigger for them.
UPGRADE_TITLE = "Upgrade list"
UPGRADE_INTRO = (
  "The rules that failed, and the rules not run because the fixtures "
  "declare no trigger for their tests."
)
# How well a rule is covered: some test checks it fully; its tests all check
# it only in part (`partly`); or no test cites it.
FULL = "full"
PARTIAL = "partial"
NONE = "none"
# The view by release, under the summary.
VERSIONS_TITLE = "By release"
VERSIONS_INTRO = (
  "The testable MUST and MUST NOT rules each release states, rules removed "
  "since included, and how many a test checks."
)
VERSIONS_HEADER = (
  "Version",
  "Testable MUST stated",
  "MUST fully checked",
  "MUST checked in part",
  "MUST without a test",
  "MUST coverage",
)
# How the report writes test outcomes and rule results.
LABELS = {
  PASSED: "passed",
  FAILED: "failed",
  ERROR: "error",
  SKIPPED: "skipped",
  NOT_APPLICABLE: "not applicable",
  NOT_RUN: "not run",
  NO_TEST: "no test",
}
# A Markdown link, with the attribute list that may follow it, as in
# `[RFC 9535](https://www.rfc-editor.org/rfc/rfc9535){ target="_blank" }`.
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)(?:\{[^}]*\})?")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
# Characters that Markdown could read as formatting in a table cell.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|&~])")
_CSS = """
body { font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial,
  sans-serif; color: #1f2328; max-width: 1200px; margin: 0 auto;
  padding: 24px; }
h1 { font-size: 26px; margin: 0 0 8px; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 4px; margin-top: 36px; }
h3 { margin-top: 28px; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }
a { color: #0969da; }
.note { color: #59636e; }
nav a { margin-right: 16px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0; }
.card { flex: 1 1 0; min-width: 180px; border: 1px solid #d0d7de;
  border-radius: 8px; padding: 12px 16px; }
.card .big { font-size: 26px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
th, td { border: 1px solid #d0d7de; padding: 4px 8px; text-align: left;
  vertical-align: top; }
th { background: #f6f8fa; }
td.num { text-align: right; white-space: nowrap; }
td.id, td.level, td.result, td.cov { white-space: nowrap; }
tr.total td { font-weight: 600; background: #f6f8fa; }
.bar { display: inline-block; width: 90px; height: 10px; border-radius: 5px;
  background: #eaeef2; vertical-align: middle; overflow: hidden; }
.bar span { display: block; height: 100%; background: #1a7f37; }
.stack { display: inline-flex; width: 90px; height: 10px; border-radius: 5px;
  background: #eaeef2; vertical-align: middle; overflow: hidden; }
.stack .pass { background: #1a7f37; }
.stack .fail { background: #cf222e; }
.stack .err { background: #bc4c00; }
.stack .skip { background: #bf8700; }
.stack .na { background: #8c959f; }
.r-passed { color: #1a7f37; }
.r-failed, .r-error { color: #cf222e; font-weight: 600; }
.r-skipped, .r-not_run { color: #9a6700; }
.r-not_applicable { color: #59636e; }
details { margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; }
"""


@dataclasses.dataclass(frozen=True)
class Tally:
  """Counts for a set of rules.

  Attributes:
    rules: How many rules there are.
    testable: How many of them this suite can test.
    testable_must: How many of the testable rules are MUST or MUST NOT.
    covered: How many testable rules some test checks fully.
    covered_must: How many testable MUST and MUST NOT rules some test checks
      fully.
    partial: How many testable rules are cited only by tests that check them
      in part.
    partial_must: How many testable MUST and MUST NOT rules are cited only
      by tests that check them in part.

  """

  rules: int
  testable: int
  testable_must: int
  covered: int
  covered_must: int
  partial: int = 0
  partial_must: int = 0

  @property
  def uncovered_must(self) -> int:
    """Return how many testable MUST and MUST NOT rules no test cites."""
    return self.testable_must - self.covered_must - self.partial_must

  @property
  def must_coverage(self) -> float | None:
    """Return the share of testable MUST and MUST NOT rules checked fully."""
    if not self.testable_must:
      return None
    return self.covered_must / self.testable_must


@dataclasses.dataclass(frozen=True)
class CapabilityRow:
  """One capability's line in the report.

  Attributes:
    capability: The capability.
    tally: The counts for its rules.

  """

  capability: Capability
  tally: Tally


@dataclasses.dataclass(frozen=True)
class VersionRow:
  """One version's line in the report's view by release.

  Attributes:
    version: A release, or `DRAFT` for the catalog's own version.
    testable_must: How many testable rules that version states as MUST or
      MUST NOT, rules removed since included.
    covered_must: How many of them some test checks fully.
    partial_must: How many of them are cited only by tests that check them
      in part.

  """

  version: str
  testable_must: int
  covered_must: int
  partial_must: int = 0

  @property
  def uncovered_must(self) -> int:
    """Return how many of the version's testable MUST rules no test cites."""
    return self.testable_must - self.covered_must - self.partial_must

  @property
  def must_coverage(self) -> float | None:
    """Return the share of the version's testable MUST rules checked fully."""
    if not self.testable_must:
      return None
    return self.covered_must / self.testable_must


@dataclasses.dataclass(frozen=True)
class TestRow:
  """One shopping test in the report.

  Attributes:
    test_id: The test's ID, as the runner reports it.
    module: The module the test is defined in.
    kind: Its marker's kind, or "unmarked".
    rule_ids: The rules its `spec_assert` marker cites.
    reason: For the other kinds, its marker's reason.
    version: For `older_version`, the release the test checks.
    capabilities: The capabilities the test requires.
    gaps: The rules it checks only in part, each with what it leaves out.

  """

  test_id: str
  module: str
  kind: str
  rule_ids: tuple[str, ...]
  reason: str
  version: str
  capabilities: tuple[str, ...]
  gaps: tuple[tuple[str, str], ...] = ()


@dataclasses.dataclass(frozen=True)
class Report:
  """The coverage report.

  Attributes:
    spec_commit: The specification commit the catalog was built from.
    capabilities: One row per capability, in the catalog's order.
    total: The counts for all rules, each rule counted once.
    rules: Every rule, by ID. Sentences the review found are not rules are
      left out.
    cited_by: For each rule at least one test cites, those tests' IDs.
    tests: Every shopping test, by ID.
    not_rules: How many catalog sentences the review found are not rules.
    outcomes: After a run, each shopping test's outcome, by test ID; None
      when the tests weren't run.
    target: The server the tests ran against, if known.
    gaps: For each rule a test cites only in part, keyed by the rule's ID
      and the test's ID, what that test leaves unchecked.
    versions: For each version the catalog's history covers, oldest first,
      the testable MUST and MUST NOT rules it states and their coverage.
    inapplicable: After a run, the IDs of the rules the version the server
      implements doesn't state; see `framework.versions`.

  """

  spec_commit: str
  capabilities: tuple[CapabilityRow, ...]
  total: Tally
  rules: tuple[Rule, ...]
  cited_by: Mapping[str, tuple[str, ...]]
  tests: tuple[TestRow, ...]
  not_rules: int = 0
  outcomes: Mapping[str, Outcome] | None = None
  target: str = ""
  gaps: Mapping[tuple[str, str], str] = MappingProxyType({})
  versions: tuple[VersionRow, ...] = ()
  inapplicable: frozenset[str] = frozenset()

  def markers(self) -> dict[str, int]:
    """Return how many tests carry each kind of marker, or none."""
    counts = collections.Counter(test.kind for test in self.tests)
    return {kind: counts[kind] for kind in (*MARKER_KINDS, UNMARKED)}

  def gap(self, rule_id: str, test_id: str) -> str | None:
    """Return what a test leaves unchecked of a rule it cites in part.

    Args:
      rule_id: The rule's ID.
      test_id: The test's ID.

    Returns:
      The gap, or None when the test checks the rule fully or doesn't cite
      it.

    """
    return self.gaps.get((rule_id, test_id))

  def coverage(self, rule_id: str) -> str:
    """Return how well tests cover a rule: `FULL`, `PARTIAL` or `NONE`."""
    return _coverage(rule_id, self.cited_by.get(rule_id, ()), self.gaps)

  def result(self, rule_id: str) -> str | None:
    """Return a rule's result, from the outcomes of the tests that cite it.

    A rule the version the server implements doesn't state is not
    applicable: it neither passes nor fails, whatever its tests did. Any
    other rule passed when at least one of its tests passed and none failed
    or hit an error. It failed when any of them failed an assertion, hit an
    error when none failed and one raised an unexpected exception, and
    wasn't run when none of them ran. Tests that check the rule only in part
    count too.

    Args:
      rule_id: The rule's ID.

    Returns:
      `PASSED`, `FAILED`, `ERROR`, `NOT_RUN` or `NOT_APPLICABLE`, or
      `NO_TEST` when no test cites the rule; None when the tests weren't
      run.

    """
    if self.outcomes is None:
      return None
    tests = self.cited_by.get(rule_id, ())
    if not tests:
      return NO_TEST
    if rule_id in self.inapplicable:
      return NOT_APPLICABLE
    statuses = {self.outcomes[test_id].status for test_id in tests}
    if FAILED in statuses:
      return FAILED
    if ERROR in statuses:
      return ERROR
    if PASSED in statuses:
      return PASSED
    return NOT_RUN

  def not_run_category(self, rule_id: str) -> str | None:
    """Return why a rule wasn't run, from its tests' skip categories.

    The first of `NOT_RUN_PRECEDENCE` that any of the rule's tests gives: a
    rule one test could run with a trigger is `NO_TRIGGER`, whatever the
    others say.

    Args:
      rule_id: The rule's ID.

    Returns:
      One of `SKIP_CATEGORIES`, or None when the rule's result isn't
      `NOT_RUN`.

    """
    if self.result(rule_id) != NOT_RUN:
      return None
    categories = {
      self.outcomes[test_id].category or OTHER
      for test_id in self.cited_by.get(rule_id, ())
      if self.outcomes[test_id].status in (SKIPPED, NOT_RUN)
    }
    return next(
      (category for category in NOT_RUN_PRECEDENCE if category in categories),
      OTHER,
    )

  def test_counts(self) -> dict[str, int] | None:
    """Return how many tests had each outcome; None if they weren't run."""
    if self.outcomes is None:
      return None
    counts = collections.Counter(
      outcome.status for outcome in self.outcomes.values()
    )
    return {status: counts[status] for status in STATUSES}


@dataclasses.dataclass(frozen=True)
class RuleGroups:
  """A capability's rules, grouped as the report shows them.

  Attributes:
    covered: The testable rules some test checks fully.
    uncovered_must: The testable MUST and MUST NOT rules no test cites.
    other: The other testable rules no test cites.
    untestable: The rules this suite cannot test.
    partial: The testable rules cited only by tests that check them in part.

  """

  covered: tuple[Rule, ...]
  uncovered_must: tuple[Rule, ...]
  other: tuple[Rule, ...]
  untestable: tuple[Rule, ...]
  partial: tuple[Rule, ...] = ()


def build(
  catalog: Catalog,
  tests: Iterable[LoadedTest],
  outcomes: Mapping[str, Outcome] | None = None,
  target: str = "",
  profile: UcpProfile | None = None,
) -> Report:
  """Build the report for the shopping tests among the loaded tests.

  Args:
    catalog: The requirements catalog.
    tests: The loaded tests; those outside the shopping suite are ignored.
    outcomes: After a run, the tests' outcomes by test ID. A shopping test
      without one didn't run.
    target: The server the tests ran against.
    profile: The server's discovery profile, which says which version it
      implements. Without it, the rules a not-applicable test cites are the
      ones taken as not stated.

  Returns:
    The report.

  """
  rows = sorted(
    (_test_row(test) for test in tests if in_scope(test.module)),
    key=lambda row: row.test_id,
  )
  cited_by: dict[str, list[str]] = collections.defaultdict(list)
  gaps: dict[tuple[str, str], str] = {}
  # Removed rules count only in the view by release.
  removed_cited: dict[str, list[str]] = collections.defaultdict(list)
  removed_gaps: dict[tuple[str, str], str] = {}
  for row in rows:
    for rule_id in dict.fromkeys(row.rule_ids):
      status = catalog.status(rule_id)
      if status == "rule":
        cited_by[rule_id].append(row.test_id)
      elif status == "removed":
        removed_cited[rule_id].append(row.test_id)
    for rule_id, gap in row.gaps:
      status = catalog.status(rule_id)
      if status == "rule":
        gaps[(rule_id, row.test_id)] = gap
      elif status == "removed":
        removed_gaps[(rule_id, row.test_id)] = gap
  coverage = {
    rule_id: _coverage(rule_id, tests, gaps)
    for rule_id, tests in cited_by.items()
  }
  removed_coverage = {
    rule_id: _coverage(rule_id, tests, removed_gaps)
    for rule_id, tests in removed_cited.items()
  }
  rules = tuple(rule for rule in catalog.rules.values() if rule.is_rule)
  removed = tuple(rule for rule in catalog.removed.values() if rule.is_rule)
  inapplicable: frozenset[str] = frozenset()
  if outcomes is not None:
    missing = Outcome(NOT_RUN, "no outcome was recorded", OTHER)
    outcomes = MappingProxyType(
      {row.test_id: outcomes.get(row.test_id, missing) for row in rows}
    )
    inapplicable = _inapplicable(catalog, rules, cited_by, outcomes, profile)
  return Report(
    spec_commit=catalog.spec_commit,
    capabilities=tuple(
      CapabilityRow(
        capability,
        _tally(
          [rule for rule in rules if capability.name in rule.capabilities],
          coverage,
        ),
      )
      for capability in _capabilities(catalog, rules)
    ),
    total=_tally(rules, coverage),
    rules=rules,
    cited_by=MappingProxyType(
      {rule_id: tuple(ids) for rule_id, ids in sorted(cited_by.items())}
    ),
    tests=tuple(rows),
    not_rules=len(catalog.rules) - len(rules),
    outcomes=outcomes,
    target=target,
    gaps=MappingProxyType(dict(sorted(gaps.items()))),
    versions=tuple(
      _version_row(
        version, (*rules, *removed), {**coverage, **removed_coverage}
      )
      for version in catalog.versions
    ),
    inapplicable=inapplicable,
  )


def rule_groups(report: Report, capability: str) -> RuleGroups:
  """Group one capability's rules for the report's drill-down.

  Args:
    report: The report.
    capability: The capability's name.

  Returns:
    Its rules, grouped.

  """
  rules = _rules_of(report, capability)
  testable = [rule for rule in rules if rule.testable]
  not_cited = [rule for rule in testable if rule.id not in report.cited_by]
  return RuleGroups(
    covered=tuple(r for r in testable if report.coverage(r.id) == FULL),
    uncovered_must=tuple(r for r in not_cited if r.level in MUST_LEVELS),
    other=tuple(r for r in not_cited if r.level not in MUST_LEVELS),
    untestable=tuple(rule for rule in rules if not rule.testable),
    partial=tuple(r for r in testable if report.coverage(r.id) == PARTIAL),
  )


def results(report: Report, rules: Iterable[Rule]) -> dict[str, int] | None:
  """Count how the testable rules with a test fared, among some rules.

  Args:
    report: The report.
    rules: The rules to look at; only the testable ones with a test count.

  Returns:
    How many passed, failed or weren't run, then the same for the MUST and
    MUST NOT rules alone, keyed `must_passed` and so on; None when the tests
    weren't run.

  """
  if report.outcomes is None:
    return None
  keys = [*RULE_RESULTS, *(f"must_{result}" for result in RULE_RESULTS)]
  counts = dict.fromkeys(keys, 0)
  for rule in rules:
    if not rule.testable or rule.id not in report.cited_by:
      continue
    result = report.result(rule.id)
    counts[result] += 1
    if rule.level in MUST_LEVELS:
      counts[f"must_{result}"] += 1
  return counts


def not_run_by_category(
  report: Report, rules: Iterable[Rule]
) -> dict[str, dict[str, int]] | None:
  """Count why the testable rules with a test weren't run, among some rules.

  Args:
    report: The report.
    rules: The rules to look at; only the testable ones with a test whose
      result is `NOT_RUN` count.

  Returns:
    For `"all"` of them and for the `"must"` ones, how many weren't run for
    each of `SKIP_CATEGORIES`; None when the tests weren't run.

  """
  if report.outcomes is None:
    return None
  counts = {
    "all": dict.fromkeys(SKIP_CATEGORIES, 0),
    "must": dict.fromkeys(SKIP_CATEGORIES, 0),
  }
  for rule in rules:
    if not rule.testable or rule.id not in report.cited_by:
      continue
    category = report.not_run_category(rule.id)
    if category is None:
      continue
    counts["all"][category] += 1
    if rule.level in MUST_LEVELS:
      counts["must"][category] += 1
  return counts


def upgrade_list(report: Report) -> list[Rule]:
  """Return the rules the server should act on, in the catalog's order.

  The testable rules with a test that failed, and those not run because the
  fixtures declare no trigger for their tests: declaring one makes the test
  run. Rules not run for other reasons, and rules the server's version
  doesn't state, are left out.

  Args:
    report: The report, after a run.

  Returns:
    The rules; none when the tests weren't run.

  """
  if report.outcomes is None:
    return []
  return [
    rule
    for rule in report.rules
    if rule.testable
    and rule.id in report.cited_by
    and (
      report.result(rule.id) == FAILED
      or report.not_run_category(rule.id) == NO_TRIGGER
    )
  ]


def to_json(report: Report) -> dict[str, Any]:
  """Return the report as the object written to `matrix.json`.

  The fields about the run are null when the tests weren't run.
  """
  return {
    "format": FORMAT_VERSION,
    "spec_commit": report.spec_commit,
    "totals": _tally_json(report.total),
    "versions": [_version_json(row) for row in report.versions],
    "not_rules": report.not_rules,
    "markers": report.markers(),
    "run": _run_json(report),
    "capabilities": [
      {
        "name": row.capability.name,
        "kind": row.capability.kind,
        "extends": list(row.capability.extends),
        **_tally_json(row.tally),
        "results": results(report, _rules_of(report, row.capability.name)),
        "not_run_by_category": not_run_by_category(
          report, _rules_of(report, row.capability.name)
        ),
      }
      for row in report.capabilities
    ],
    "rules": [
      {
        "id": rule.id,
        "level": rule.level,
        "clause": rule.clause,
        "capabilities": list(rule.capabilities),
        "source_file": rule.source_file,
        "line": rule.line,
        "link": rule.link,
        "since": rule.since,
        "versions": list(rule.versions),
        "testable": rule.testable,
        "testable_reason": rule.testable_reason,
        "tests": list(report.cited_by.get(rule.id, ())),
        "coverage": report.coverage(rule.id),
        "gaps": {
          test_id: gap
          for test_id in report.cited_by.get(rule.id, ())
          if (gap := report.gap(rule.id, test_id)) is not None
        },
        "result": report.result(rule.id),
        "not_run_category": report.not_run_category(rule.id)
        if report.outcomes is not None
        else None,
      }
      for rule in report.rules
    ],
    "tests": [
      {
        "id": test.test_id,
        "module": test.module,
        "marker": test.kind,
        "rules": list(test.rule_ids),
        "partial": dict(test.gaps),
        "reason": test.reason,
        "version": test.version,
        "capabilities": list(test.capabilities),
        "outcome": _outcome_json(report, test.test_id),
      }
      for test in report.tests
    ],
  }


def to_markdown(report: Report, findings: str | None = None) -> str:
  """Return the report as Markdown that renders on GitHub.

  Args:
    report: The report.
    findings: A link to the findings document, if there is one.

  Returns:
    The Markdown.

  """
  ran = report.outcomes is not None
  lines = [
    f"# {TITLE}",
    "",
    f"> {_intro(report, _md_code)}",
    "",
    f"**{_headline(report)}** {_marker_counts(report, _md_code)}",
  ]
  if ran:
    lines += ["", f"**{_results_headline(report)}** {_outcome_counts(report)}"]
  lines += [
    "",
    "## Summary",
    "",
    "| Capability | Extends | Rules | Testable | Fully checked | Testable MUST "
    "| MUST fully checked | MUST checked in part | MUST without a test "
    "| MUST coverage |",
    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
  ]
  for row in _ordered(report):
    extends = ", ".join(_short_cap(name) for name in row.capability.extends)
    lines.append(
      _md_tally_row(_short_cap(row.capability.name), extends or "–", row.tally)
    )
  lines.append(
    _md_tally_row("**All rules, each counted once**", "", report.total)
  )
  if len(report.versions) > 1:
    lines += ["", *_md_versions(report)]
  if ran:
    lines += ["", *_md_results(report)]
  for row in _ordered(report):
    lines += ["", *_md_capability(report, row)]
  lines += ["", *_md_tests(report), "", "## Notes", ""]
  lines += [
    f"- **{label}:** {text}"
    for label, text in _notes(report, _md_code, _md_link, findings)
  ]
  return "\n".join(lines) + "\n"


def to_html(report: Report, findings: str | None = None) -> str:
  """Return the report as one HTML page with no external resources.

  Args:
    report: The report.
    findings: A link to the findings document, if there is one.

  Returns:
    The HTML.

  """
  total = report.total
  ran = report.outcomes is not None
  sections = [
    ("summary", "Summary"),
    *([("results", "Results")] if ran else []),
    ("capabilities", "Capabilities"),
    ("tests", "Tests"),
    ("notes", "Notes"),
  ]
  nav = "".join(
    f'<a href="#{anchor}">{title}</a>' for anchor, title in sections
  )
  head = [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    f"<title>{TITLE}</title>",
    f"<style>{_CSS}</style>",
    "</head>",
    "<body>",
    f"<h1>{TITLE}</h1>",
    f'<p class="note">{_intro(report, _html_code)}</p>',
    '<div class="cards">',
    _card(
      f"{total.covered_must} / {total.testable_must}",
      "testable MUST and MUST NOT rules are fully checked by a test "
      f"({_percent(total.must_coverage)})",
    ),
    *(
      [
        _card(
          str(total.partial_must),
          "more testable MUST and MUST NOT rules are checked only in part",
        )
      ]
      if total.partial_must
      else []
    ),
    _card(
      f"{total.covered} / {total.testable}",
      "testable rules are fully checked by a test",
    ),
    _card(str(total.rules), "rules in the catalog"),
    _card(
      str(len(report.tests)),
      "shopping tests: " + ", ".join(_marker_parts(report, _html_code)),
    ),
    *([_results_card(report)] if ran else []),
    "</div>",
    f"<nav>{nav}</nav>",
    '<h2 id="summary">Summary</h2>',
    _html_summary(report),
    *([_html_versions(report)] if len(report.versions) > 1 else []),
    *(['<h2 id="results">Results</h2>', _html_results(report)] if ran else []),
    '<h2 id="capabilities">Capabilities</h2>',
  ]
  body = [_html_capability(report, row) for row in _ordered(report)]
  notes = "".join(
    f"<li><strong>{label}:</strong> {text}</li>"
    for label, text in _notes(report, _html_code, _html_link, findings)
  )
  tail = [
    _html_tests(report),
    '<h2 id="notes">Notes</h2>',
    f"<ul>{notes}</ul>",
    "</body>",
    "</html>",
  ]
  return "\n".join([*head, *body, *tail]) + "\n"


def write(
  report: Report,
  directory: str | Path,
  findings: str | Path | None = None,
) -> list[Path]:
  """Write the report's files to a directory, creating it if need be.

  Args:
    report: The report.
    directory: Where to write it.
    findings: The findings document to link to, if there is one.

  Returns:
    The paths written: `matrix.json`, `matrix.md` and `matrix.html`.

  """
  out = Path(directory)
  out.mkdir(parents=True, exist_ok=True)
  href = None
  if findings:
    relative = os.path.relpath(Path(findings).resolve(), out.resolve())
    href = Path(relative).as_posix()
  texts = {
    JSON_FILE: json.dumps(to_json(report), indent=2, ensure_ascii=False) + "\n",
    MARKDOWN_FILE: to_markdown(report, href),
    HTML_FILE: to_html(report, href),
  }
  paths = []
  for name, text in texts.items():
    path = out / name
    path.write_text(text, encoding="utf-8")
    paths.append(path)
  return paths


def summary(report: Report, paths: Sequence[Path]) -> str:
  """Return a few lines for the console about a written report."""
  total = report.total
  share = total.must_coverage
  commit = report.spec_commit[:8] or "an unknown commit"
  if len({path.parent for path in paths}) == 1:
    names = ", ".join(path.name for path in paths)
    where = f"{paths[0].parent} ({names})"
  else:
    where = ", ".join(str(path) for path in paths)
  text = (
    f"Requirements report: {where}\n"
    f"  catalog at {commit}: {total.rules} rules, {total.testable} "
    f"testable, {total.testable_must} of them MUST or MUST NOT\n"
    f"  {len(report.tests)} tests cite {len(report.cited_by)} rules; "
    f"{total.covered_must} of the {total.testable_must} testable MUST and "
    f"MUST NOT rules are fully checked by a test"
    + (f" ({share:.0%})" if share is not None else "")
    + (
      f", {total.partial_must} more only in part" if total.partial_must else ""
    )
    + f", {total.uncovered_must} have no test\n"
  )
  counts = results(report, report.rules)
  if counts is not None:
    against = f" against {report.target}" if report.target else ""
    not_applicable = counts["must_not_applicable"]
    text += (
      f"  results{against}: {counts['must_passed']} of the "
      f"{_must_applicable(total, counts)} testable MUST and MUST NOT rules "
      f"with a test passed, {counts['must_failed']} failed"
      f"{_hit_error(counts['must_error'])}, "
      f"{_not_run(counts['must_not_run'])}"
      + (f", {_not_applicable(not_applicable)}" if not_applicable else "")
      + "\n"
    )
    by_category = not_run_by_category(report, report.rules)
    if by_category and counts["must_not_run"]:
      why = _why_not_run(by_category["must"], "MUST and MUST NOT ")
      text += f"  {why[0].lower()}{why[1:].rstrip('.')}\n"
    upgrades = upgrade_list(report)
    must = sum(rule.level in MUST_LEVELS for rule in upgrades)
    text += (
      f"  {UPGRADE_TITLE.lower()}: {len(upgrades)} rules ({must} MUST or MUST "
      "NOT) failed or lack a trigger\n"
    )
  return text


def generate(
  tests: Iterable[LoadedTest],
  directory: str | Path,
  catalog_dir: str | Path | None = None,
  outcomes: Mapping[str, Outcome] | None = None,
  target: str = "",
  profile: UcpProfile | None = None,
) -> str:
  """Build the report for the loaded tests, write it, and describe it.

  Args:
    tests: The loaded tests.
    directory: Where to write the report.
    catalog_dir: The catalog directory; by default this repository's. The
      findings document beside it, if any, is linked from the report.
    outcomes: After a run, the tests' outcomes by test ID.
    target: The server the tests ran against.
    profile: The server's discovery profile, if it was fetched.

  Returns:
    A few lines for the console.

  Raises:
    CatalogError: If the catalog cannot be loaded.
    OSError: If the report cannot be written.

  """
  catalog_root = Path(catalog_dir or DEFAULT_CATALOG_DIR).resolve()
  report = build(load_catalog(catalog_root), tests, outcomes, target, profile)
  findings = catalog_root.parent / FINDINGS_FILE
  paths = write(report, directory, findings if findings.is_file() else None)
  return summary(report, paths)


def prepare(
  directory: str | Path, catalog_dir: str | Path | None = None
) -> None:
  """Check, before the tests run, that the report can be built and written.

  Args:
    directory: Where the report will be written; it is created if need be.
    catalog_dir: The catalog directory; by default this repository's.

  Raises:
    CatalogError: If the catalog cannot be loaded.
    OSError: If the directory cannot be created.

  """
  load_catalog(Path(catalog_dir or DEFAULT_CATALOG_DIR).resolve())
  Path(directory).mkdir(parents=True, exist_ok=True)


# Building.


def _capabilities(catalog: Catalog, rules: Sequence[Rule]) -> list[Capability]:
  """Return the index's capabilities, then any other a rule belongs to."""
  listed = list(catalog.capabilities)
  names = {capability.name for capability in listed}
  others = {name for rule in rules for name in rule.capabilities} - names
  return listed + [Capability(name) for name in sorted(others)]


def _inapplicable(
  catalog: Catalog,
  rules: Sequence[Rule],
  cited_by: Mapping[str, Sequence[str]],
  outcomes: Mapping[str, Outcome],
  profile: UcpProfile | None,
) -> frozenset[str]:
  """Return the IDs of the rules the server's version doesn't state.

  With the server's profile, each rule is compared with the version the
  server advertises for its capability. Without it, a rule is taken as not
  stated when a test that cites it was skipped as not applicable: such a
  test cites no rule the server's version states.
  """
  if profile is not None:
    return frozenset(
      rule.id for rule in rules if not rule_applies(rule, catalog, profile)
    )
  return frozenset(
    rule_id
    for rule_id, tests in cited_by.items()
    if any(outcomes[test_id].status == NOT_APPLICABLE for test_id in tests)
  )


def _coverage(
  rule_id: str,
  tests: Iterable[str],
  gaps: Mapping[tuple[str, str], str],
) -> str:
  """Return a rule's coverage from the tests that cite it and their gaps."""
  tests = list(tests)
  if not tests:
    return NONE
  if any((rule_id, test_id) not in gaps for test_id in tests):
    return FULL
  return PARTIAL


def _tally(rules: Sequence[Rule], coverage: Mapping[str, str]) -> Tally:
  """Count rules, testable rules and testable MUSTs, and how they're covered.

  Args:
    rules: The rules to count.
    coverage: The coverage of each rule a test cites, by rule ID.

  Returns:
    The counts.

  """
  testable = [rule for rule in rules if rule.testable]
  must = [rule for rule in testable if rule.level in MUST_LEVELS]

  def count(group: Sequence[Rule], level: str) -> int:
    return sum(coverage.get(rule.id) == level for rule in group)

  return Tally(
    rules=len(rules),
    testable=len(testable),
    testable_must=len(must),
    covered=count(testable, FULL),
    covered_must=count(must, FULL),
    partial=count(testable, PARTIAL),
    partial_must=count(must, PARTIAL),
  )


def _must_with_a_test(tally: Tally) -> int:
  """Return how many testable MUSTs some test cites, fully or in part."""
  return tally.covered_must + tally.partial_must


def _must_applicable(tally: Tally, counts: Mapping[str, int]) -> int:
  """Return how many testable MUSTs with a test the server's version states."""
  return _must_with_a_test(tally) - counts["must_not_applicable"]


def _tally_json(tally: Tally) -> dict[str, int]:
  """Return a tally's counts as JSON, with the uncovered MUSTs."""
  return {
    "rules": tally.rules,
    "testable": tally.testable,
    "testable_must": tally.testable_must,
    "covered": tally.covered,
    "covered_must": tally.covered_must,
    "partial": tally.partial,
    "partial_must": tally.partial_must,
    "uncovered_must": tally.uncovered_must,
  }


def _version_row(
  version: str, rules: Sequence[Rule], coverage: Mapping[str, str]
) -> VersionRow:
  """Count the testable MUST rules a version states, and their coverage.

  Args:
    version: A release, or `DRAFT`.
    rules: The rules to look at: the catalog's and the removed ones.
    coverage: The coverage of each rule a test cites, by rule ID.

  Returns:
    The version's row. A rule counts at the level the version states it.

  """
  stated = [
    rule
    for rule in rules
    if rule.testable and rule.level_in(version) in MUST_LEVELS
  ]
  return VersionRow(
    version=version,
    testable_must=len(stated),
    covered_must=sum(coverage.get(rule.id) == FULL for rule in stated),
    partial_must=sum(coverage.get(rule.id) == PARTIAL for rule in stated),
  )


def _version_json(row: VersionRow) -> dict[str, Any]:
  """Return a version's row as JSON, with the uncovered MUSTs."""
  return {
    "version": row.version,
    "testable_must": row.testable_must,
    "covered_must": row.covered_must,
    "partial_must": row.partial_must,
    "uncovered_must": row.uncovered_must,
  }


def _test_row(test: LoadedTest) -> TestRow:
  """Describe a loaded test for the report."""
  marker = test.marker
  return TestRow(
    test_id=test.test_id,
    module=test.module,
    kind=marker.kind if marker else UNMARKED,
    rule_ids=marker.rule_ids if marker else (),
    reason=marker.reason if marker else "",
    version=marker.version if marker else "",
    capabilities=tuple(sorted(test.capabilities)),
    gaps=marker.gaps if marker else (),
  )


def _run_json(report: Report) -> dict[str, Any] | None:
  """Return the run's target and counts as JSON, or None without a run."""
  if report.outcomes is None:
    return None
  return {
    "target": report.target,
    "tests": report.test_counts(),
    "results": results(report, report.rules),
    "not_run_by_category": not_run_by_category(report, report.rules),
    "upgrade_list": [rule.id for rule in upgrade_list(report)],
  }


def _outcome_json(report: Report, test_id: str) -> dict[str, Any] | None:
  """Return a test's outcome as JSON, or None without a run.

  A skipped test, or one that didn't run, has a category; the others have
  null.
  """
  if report.outcomes is None:
    return None
  outcome = report.outcomes[test_id]
  category = None
  if outcome.status in (SKIPPED, NOT_RUN):
    category = outcome.category or OTHER
  return {
    "status": outcome.status,
    "detail": outcome.detail,
    "category": category,
  }


def _rules_of(report: Report, capability: str) -> list[Rule]:
  """Return the rules that belong to a capability."""
  return [rule for rule in report.rules if capability in rule.capabilities]


def _rules_with_result(report: Report, result: str) -> list[Rule]:
  """Return the testable rules with a test that have the given result."""
  return [
    rule
    for rule in report.rules
    if rule.testable
    and rule.id in report.cited_by
    and report.result(rule.id) == result
  ]


# Wording shared by the Markdown and the HTML. `code` and `link` format
# code and links for the output; the rest of the wording needs no escaping.


def _intro(report: Report, code: Callable[[str], str]) -> str:
  """Return the sentence that says what the report is."""
  commit = report.spec_commit[:8] or "an unknown commit"
  covered = (
    f"A rule counts as covered when a test's {code('spec_assert')} marker "
    f"cites it as fully checked; one cited only with {code('partly')} "
    "counts as checked in part."
  )
  if report.outcomes is None:
    return (
      f"Built from the requirements catalog at {code(commit)} and the "
      f"shopping tests' markers. {covered} The report says which rules have "
      "a test, not whether the tests pass."
    )
  run = f"a run against {code(report.target)}" if report.target else "a run"
  text = (
    f"Built from the requirements catalog at {code(commit)}, the shopping "
    f"tests' markers and {run}. {covered} A rule passed when at least one "
    "of its tests passed and none failed or hit an error, failed when any "
    "of them failed an assertion, hit an error when none failed and one "
    "raised an unexpected exception before checking it, and wasn't run when "
    "none of them ran."
  )
  if report.inapplicable:
    text += (
      " A rule the version the server implements doesn't state is not "
      "applicable, and neither passes nor fails."
    )
  return text


def _headline(report: Report) -> str:
  """Return the sentence with the coverage numbers."""
  total = report.total
  text = (
    f"{total.covered_must} of the {total.testable_must} testable MUST and "
    "MUST NOT rules are fully checked by a test "
    f"({_percent(total.must_coverage)}), and {total.covered} of the "
    f"{total.testable} testable rules."
  )
  if total.partial_must:
    rules = "rule is" if total.partial_must == 1 else "rules are"
    text += (
      f" {total.partial_must} more MUST and MUST NOT {rules} checked only in "
      "part."
    )
  return text


def _marker_parts(report: Report, code: Callable[[str], str]) -> list[str]:
  """Return how many tests carry each kind of marker, as phrases."""
  markers = report.markers()
  return [
    f"{markers['spec_assert']} cite rules",
    *(f"{markers[kind]} {code(kind)}" for kind in MARKER_KINDS[1:]),
    f"{markers[UNMARKED]} unmarked",
  ]


def _marker_counts(report: Report, code: Callable[[str], str]) -> str:
  """Return the sentence with the marker counts."""
  parts = ", ".join(_marker_parts(report, code))
  return f"{len(report.tests)} shopping tests: {parts}."


def _results_headline(report: Report) -> str:
  """Return the sentence with the results for the testable MUSTs."""
  counts = results(report, report.rules) or dict.fromkeys(
    (*RULE_RESULTS, *(f"must_{result}" for result in RULE_RESULTS)), 0
  )
  text = (
    f"Of the {_must_applicable(report.total, counts)} testable MUST and MUST "
    f"NOT rules with a test, {counts['must_passed']} passed, "
    f"{counts['must_failed']} failed{_hit_error(counts['must_error'])} and "
    f"{_not_run(counts['must_not_run'])}."
    + _not_applicable_sentence(counts["must_not_applicable"])
  )
  by_category = not_run_by_category(report, report.rules)
  if by_category and counts["must_not_run"]:
    text += f" {_why_not_run(by_category['must'], 'MUST and MUST NOT ')}"
  return text


def _outcome_counts(report: Report) -> str:
  """Return the sentence with how many tests had each outcome."""
  counts = report.test_counts() or dict.fromkeys(STATUSES, 0)
  errors = "error" if counts[ERROR] == 1 else "errors"
  return (
    f"Test outcomes: {counts[PASSED]} passed, {counts[FAILED]} failed, "
    f"{counts[ERROR]} {errors}, {counts[SKIPPED]} skipped, "
    f"{counts[NOT_APPLICABLE]} not applicable, {counts[NOT_RUN]} not run."
  )


def _hit_error(count: int) -> str:
  """Return how many rules hit an error, or '' when none did."""
  return f", {count} hit an error" if count else ""


def _not_run(count: int) -> str:
  """Return how many rules weren't run, as a phrase."""
  return f"{count} wasn't run" if count == 1 else f"{count} weren't run"


def _why_not_run(counts: Mapping[str, int], levels: str = "") -> str:
  """Return the sentence that breaks down why rules weren't run.

  Args:
    counts: How many rules weren't run for each of `SKIP_CATEGORIES`.
    levels: What the rules are, such as "MUST and MUST NOT ", or "" for
      rules of every level.

  Returns:
    The sentence, such as "The 3 MUST and MUST NOT rules that weren't run,
    by reason: no trigger declared 2, environment 1."; "" when none.

  """
  total = sum(counts.values())
  if not total:
    return ""
  parts = ", ".join(
    f"{SKIP_LABELS[category]} {counts[category]}"
    for category in SKIP_CATEGORIES
    if counts[category]
  )
  rules = f"{levels}rule" if total == 1 else f"{levels}rules"
  verb = "wasn't" if total == 1 else "weren't"
  return f"The {total} {rules} that {verb} run, by reason: {parts}."


def _outcome_label(outcome: Outcome) -> str:
  """Return an outcome's label, with a skip's category in brackets."""
  label = LABELS[outcome.status]
  if outcome.status == SKIPPED and outcome.category not in ("", OTHER):
    return f"{label} ({SKIP_LABELS[outcome.category]})"
  return label


_APPLIES_TAIL = "apply to the version the server implements"


def _not_applicable(count: int) -> str:
  """Return how many rules the server's version doesn't state, as a phrase."""
  verb = "doesn't" if count == 1 else "don't"
  return f"{count} {verb} {_APPLIES_TAIL}"


def _not_applicable_sentence(count: int) -> str:
  """Return the sentence about rules with a test that don't apply, or ""."""
  if not count:
    return ""
  if count == 1:
    return f" 1 more rule with a test doesn't {_APPLIES_TAIL}."
  return f" {count} more rules with a test don't {_APPLIES_TAIL}."


def _capability_sentence(
  report: Report, row: CapabilityRow, code: Callable[[str], str]
) -> str:
  """Return the sentence that introduces a capability's section."""
  capability, tally = row.capability, row.tally
  if capability.kind == "extension" and capability.extends:
    parents = ", ".join(_short_cap(name) for name in capability.extends)
    kind = f"an extension of {parents}"
  elif capability.kind == "extension":
    kind = "an extension"
  else:
    kind = "a capability"
  sentence = (
    f"{code(capability.name)}, {kind}: {tally.rules} rules, "
    f"{tally.testable} of them testable. {tally.covered_must} of its "
    f"{tally.testable_must} testable MUST and MUST NOT rules are fully "
    f"checked by a test ({_percent(tally.must_coverage)})"
    + (f", and {tally.partial_must} more in part" if tally.partial_must else "")
    + "."
  )
  counts = results(report, _rules_of(report, capability.name))
  with_a_test = _must_with_a_test(tally)
  if counts is not None and with_a_test:
    applicable = _must_applicable(tally, counts)
    not_applicable = counts["must_not_applicable"]
    if applicable:
      lead = (
        f"Of the {applicable} with a test"
        if tally.partial_must or not_applicable
        else f"Of those {applicable}"
      )
      sentence += (
        f" {lead}, {counts['must_passed']} passed, "
        f"{counts['must_failed']} failed{_hit_error(counts['must_error'])} "
        f"and {_not_run(counts['must_not_run'])}."
      )
    sentence += _not_applicable_sentence(not_applicable)
  return sentence


def _notes(
  report: Report,
  code: Callable[[str], str],
  link: Callable[[str, str], str],
  findings: str | None,
) -> list[tuple[str, str]]:
  """Return the notes at the end of the report, as labels and text."""
  commit = report.spec_commit[:8] or "an unknown commit"
  notes = []
  if findings:
    notes.append(
      (
        "Findings",
        f"{link(FINDINGS_FILE, findings)} reviews each claim and lists the "
        "spec and test findings from marking the tests.",
      )
    )
  if report.not_rules == 1:
    left_out = ", and the sentence the review found isn't a rule is left out"
  elif report.not_rules:
    left_out = (
      f", and the {report.not_rules} sentences the review found aren't rules "
      "are left out"
    )
  else:
    left_out = ""
  notes += [
    (
      "IDs",
      f"Rule IDs drop the {code(ID_PREFIX)} prefix. They are stable within "
      "this suite until official IDs are issued in the specification.",
    ),
    (
      "Catalog",
      f"Rules stated in the specification pages under {code(SPEC_PAGES)} at "
      f"{code(commit)}. Keyword sentences in schema field descriptions "
      f"aren't included{left_out}.",
    ),
    (
      "Testable",
      "This suite plays the platform and calls a business over REST, so a "
      "rule is testable when it is on a transport-neutral or REST page and "
      "aimed at the business, as corrected by the review.",
    ),
    ("MUST", "Counts MUST and MUST NOT."),
  ]
  if len(report.versions) > 1:
    notes.append(
      (
        "Releases",
        "The view by release counts the testable MUST and MUST NOT rules "
        "each release states, at the level it states them. A rule only "
        "older releases state is a removed rule: it counts in those "
        f"releases, never in {code(DRAFT)}, the catalog's own version.",
      )
    )
  if report.gaps:
    notes.append(
      (
        "Partial claims",
        f"A test cites a rule with {code('partly')} when its assertions "
        "check only part of it, and says what they leave out. A rule only "
        "such tests cite counts as checked in part, not covered.",
      )
    )
  if report.outcomes is not None:
    notes.append(
      (
        "Results",
        "A passing test shows the server did what the test's assertions "
        "check, which may be only part of the rule it cites. A skipped test "
        "counts as not run. An unexpected exception in a test gives the rule "
        f"result {code('error')}, separate from {code('failed')}, unless "
        "another test for the rule failed an assertion: the crash prevented "
        "the test from checking the rule. Because outcomes are recorded per "
        "test method, a failure or error in a test that cites more than one "
        "rule applies to every rule it cites, even if the test stopped at an "
        "earlier assertion. A test is not applicable when the version the "
        "server implements states none of the rules it cites, and a rule is "
        "not applicable when that version doesn't state it: neither counts "
        "for or against the server.",
      )
    )
    notes.append(
      (
        "Skips",
        "A skipped test says why: no trigger declared (the fixtures lack "
        "what it needs), environment (the run's own settings or "
        "surroundings), not exercised (the server's responses gave it "
        "nothing to check), capability not advertised, or another reason. A "
        "rule that wasn't run takes the first of those its tests give, in "
        f"that order. The {UPGRADE_TITLE.lower()} is the rules that failed "
        "plus those not run for want of a trigger: the ones a server can act "
        "on.",
      )
    )
  shared = sum(len(rule.capabilities) > 1 for rule in report.rules)
  if shared:
    rules = "1 rule belongs" if shared == 1 else f"{shared} rules belong"
    notes.append(
      (
        "Shared rules",
        f"{rules} to more than one capability. Each capability counts "
        "them; the total counts each rule once.",
      )
    )
  return notes


def _ordered(report: Report) -> list[CapabilityRow]:
  """Return the capabilities, then the extensions, in the index's order."""
  rows = list(report.capabilities)
  return [row for row in rows if row.capability.kind != "extension"] + [
    row for row in rows if row.capability.kind == "extension"
  ]


def _tests_by_kind(report: Report) -> list[tuple[str, list[TestRow]]]:
  """Return the tests grouped by marker kind, in the kinds' order."""
  return [
    (kind, [test for test in report.tests if test.kind == kind])
    for kind in (*MARKER_KINDS, UNMARKED)
  ]


def _test_header(kind: str, with_result: bool = False) -> list[str]:
  """Return the column titles for tests of one kind."""
  if kind == "spec_assert":
    header = ["Test", "File", "Rules"]
  elif kind == "older_version":
    header = ["Test", "File", "Release", "What changed"]
  elif kind == UNMARKED:
    header = ["Test", "File"]
  else:
    header = ["Test", "File", "Reason"]
  return [*header, "Result"] if with_result else header


def _test_values(test: TestRow) -> list[str]:
  """Return what the report shows for a test, in its columns' order."""
  values = [_short_test(test.test_id), test.module.replace(".", "/") + ".py"]
  if test.kind == "spec_assert":
    partial = {rule_id for rule_id, _ in test.gaps}
    values.append(
      ", ".join(
        _short_id(rule_id) + (" (in part)" if rule_id in partial else "")
        for rule_id in test.rule_ids
      )
    )
  elif test.kind == "older_version":
    values += [test.version, test.reason]
  elif test.kind != UNMARKED:
    values.append(test.reason)
  return values


def _rule_header(extra: str, with_result: bool = False) -> list[str]:
  """Return the column titles for a table of rules."""
  title = {"tests": "Tests", "outcomes": "Tests", "why": "Why not testable"}
  return [
    "Rule",
    "Level",
    *(["Result"] if with_result else []),
    "Text",
    *([title[extra]] if extra in title else []),
    "Source",
  ]


def _short_id(rule_id: str) -> str:
  """Return a rule ID without its common prefix."""
  return rule_id.removeprefix(ID_PREFIX)


def _short_cap(name: str) -> str:
  """Return a capability's name without its common prefix."""
  return name.removeprefix(CAPABILITY_PREFIX)


def _short_test(test_id: str) -> str:
  """Return a test's class and method from its ID."""
  return ".".join(test_id.rsplit(".", 2)[-2:])


def _percent(share: float | None) -> str:
  """Return a share as a percentage, or a dash when there is none."""
  return "–" if share is None else f"{share:.0%}"


def _version_label(report: Report, version: str) -> str:
  """Return a version's label: a release, or the draft with its commit."""
  if version == DRAFT and report.spec_commit:
    return f"{DRAFT} ({report.spec_commit[:8]})"
  return version


def _source_label(rule: Rule) -> str:
  """Return the page and line a rule is on."""
  return f"{rule.source_file.removeprefix(SPEC_PAGES)}:{rule.line}"


def _source_url(report: Report, rule: Rule) -> str:
  """Return a link to a rule's line at the catalog's commit, if known."""
  if not report.spec_commit:
    return ""
  return (
    f"{SPEC_REPO}/blob/{report.spec_commit}/{rule.source_file}"
    f"?plain=1#L{rule.line}"
  )


def _plain(text: str) -> str:
  """Return text on one line, with Markdown links reduced to their text."""
  return " ".join(_LINK.sub(r"\1", text).split())


# Markdown.


def _md_code(text: str) -> str:
  """Return text as Markdown code."""
  return f"`{text}`"


def _md_link(text: str, href: str) -> str:
  """Return a Markdown link."""
  return f"[{text}]({href})"


def _md_cell(text: str) -> str:
  """Return text for a Markdown table cell."""
  parts = _plain(text).split("`")
  parts[::2] = [part.replace("<", "&lt;") for part in parts[::2]]
  return "`".join(parts).replace("|", "\\|")


def _md_table(
  header: Sequence[str], rows: Iterable[Sequence[str]]
) -> list[str]:
  """Return a Markdown table."""
  lines = [
    "| " + " | ".join(header) + " |",
    "|" + " --- |" * len(header),
  ]
  lines += ["| " + " | ".join(row) + " |" for row in rows]
  return lines


def _md_tally_row(label: str, extends: str, tally: Tally) -> str:
  """Return a summary row."""
  cells = [
    label,
    extends,
    tally.rules,
    tally.testable,
    tally.covered,
    tally.testable_must,
    tally.covered_must,
    tally.partial_must,
    tally.uncovered_must,
    _percent(tally.must_coverage),
  ]
  return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _md_versions(report: Report) -> list[str]:
  """Return the view by release."""
  lines = [
    f"### {VERSIONS_TITLE}",
    "",
    VERSIONS_INTRO,
    "",
    "| " + " | ".join(VERSIONS_HEADER) + " |",
    "| --- |" + " ---: |" * (len(VERSIONS_HEADER) - 1),
  ]
  for row in report.versions:
    cells = [
      _version_label(report, row.version),
      row.testable_must,
      row.covered_must,
      row.partial_must,
      row.uncovered_must,
      _percent(row.must_coverage),
    ]
    lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
  return lines


def _md_plain(text: str) -> str:
  """Return text, such as a failure message, as Markdown with no formatting."""
  return _MD_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def _md_result(status: str) -> str:
  """Return a result or outcome; failures and errors are bold."""
  label = LABELS[status]
  return f"**{label}**" if status in (FAILED, ERROR) else label


def _md_outcome(outcome: Outcome) -> str:
  """Return a test's outcome, with its category and detail."""
  result = _md_result(outcome.status)
  if outcome.status == SKIPPED:
    result = _outcome_label(outcome)
  return f"{result}: {_md_plain(outcome.detail)}" if outcome.detail else result


def _md_test(
  report: Report, test_id: str, detail: bool = False, gap: str | None = None
) -> str:
  """Return a test's name, its gap for a partly checked rule, and outcome."""
  name = f"`{_short_test(test_id)}`"
  if gap is not None:
    name += f" (in part: {_md_plain(gap)})"
  if report.outcomes is None:
    return name
  outcome = report.outcomes[test_id]
  if detail:
    return f"{name} {_md_outcome(outcome)}"
  return f"{name} {_md_result(outcome.status)}"


def _md_results(report: Report) -> list[str]:
  """Return the results section."""
  lines = [
    "## Results",
    "",
    "| Capability | With a test | Passed | Failed | Error | Not run "
    "| Not applicable | MUST with a test | MUST passed | MUST failed "
    "| MUST error | MUST not run | MUST not applicable |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
    "| ---: | ---: | ---: |",
  ]
  for row in _ordered(report):
    counts = results(report, _rules_of(report, row.capability.name))
    lines.append(
      _md_results_row(_short_cap(row.capability.name), row.tally, counts)
    )
  lines.append(
    _md_results_row(
      "**All rules, each counted once**",
      report.total,
      results(report, report.rules),
    )
  )
  upgrades = upgrade_list(report)
  lines += ["", f"### {UPGRADE_TITLE}: {len(upgrades)}", "", UPGRADE_INTRO, ""]
  lines += _md_rules(report, upgrades, "outcomes")
  by_category = not_run_by_category(report, report.rules) or {}
  for title, result in RESULT_LISTS:
    rules = _rules_with_result(report, result)
    lines += ["", f"### {title}: {len(rules)}", ""]
    if result == NOT_RUN and rules:
      lines += [_why_not_run(by_category["all"]), ""]
    lines += _md_rules(report, rules, "outcomes")
  return lines


def _md_results_row(label: str, tally: Tally, counts: Mapping[str, int]) -> str:
  """Return a row of the results table."""
  cells = [
    label,
    tally.covered + tally.partial,
    counts[PASSED],
    counts[FAILED],
    counts[ERROR],
    counts[NOT_RUN],
    counts[NOT_APPLICABLE],
    _must_with_a_test(tally),
    counts["must_passed"],
    counts["must_failed"],
    counts["must_error"],
    counts["must_not_run"],
    counts["must_not_applicable"],
  ]
  return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _md_capability(report: Report, row: CapabilityRow) -> list[str]:
  """Return a capability's section."""
  groups = rule_groups(report, row.capability.name)
  return [
    f"## {_short_cap(row.capability.name)}",
    "",
    _capability_sentence(report, row, _md_code),
    "",
    f"### Rules fully checked by a test: {len(groups.covered)}",
    "",
    *_md_rules(report, groups.covered, "tests"),
    "",
    f"### Rules checked only in part: {len(groups.partial)}",
    "",
    *_md_rules(report, groups.partial, "tests"),
    "",
    "### Testable MUST and MUST NOT rules without a test: "
    f"{len(groups.uncovered_must)}",
    "",
    *_md_rules(report, groups.uncovered_must, ""),
    "",
    "<details>",
    "<summary>Other testable rules without a test: "
    f"{len(groups.other)}</summary>",
    "",
    *_md_rules(report, groups.other, ""),
    "",
    "</details>",
    "",
    "<details>",
    f"<summary>Not testable by this suite: {len(groups.untestable)}</summary>",
    "",
    *_md_rules(report, groups.untestable, "why"),
    "",
    "</details>",
  ]


def _md_rules(report: Report, rules: Sequence[Rule], extra: str) -> list[str]:
  """Return a table of rules, or "None."; `extra` adds a column.

  Args:
    report: The report.
    rules: The rules.
    extra: "tests" for the tests that cite each rule, with their outcomes
      and the rule's result after a run; "outcomes" for those tests with
      their outcomes' details; "why" for why a rule isn't testable; or ""
      for no extra column. A test that checks the rule only in part shows
      what it leaves out.

  Returns:
    The table's lines.

  """
  if not rules:
    return ["None."]
  with_result = extra == "tests" and report.outcomes is not None
  rows = []
  for rule in rules:
    cells = [_short_id(rule.id), rule.level]
    if with_result:
      cells.append(_md_result(report.result(rule.id)))
    cells.append(_md_cell(rule.clause))
    if extra in ("tests", "outcomes"):
      tests = report.cited_by.get(rule.id, ())
      detail = extra == "outcomes"
      cells.append(
        "<br>".join(
          _md_test(report, t, detail, report.gap(rule.id, t)) for t in tests
        )
      )
    elif extra == "why":
      cells.append(_md_cell(rule.testable_reason))
    url = _source_url(report, rule)
    label = _source_label(rule)
    cells.append(f"[{label}]({url})" if url else label)
    rows.append(cells)
  return _md_table(_rule_header(extra, with_result), rows)


def _md_tests(report: Report) -> list[str]:
  """Return the tests section."""
  with_result = report.outcomes is not None
  lines = ["## Tests"]
  for kind, tests in _tests_by_kind(report):
    if kind == UNMARKED and not tests:
      continue
    marker = "" if kind == UNMARKED else f" (`{kind}`)"
    lines += ["", f"### {KIND_TITLES[kind]}{marker}: {len(tests)}", ""]
    if not tests:
      lines.append("None.")
      continue
    rows = []
    for test in tests:
      values = _test_values(test)
      row = [f"`{values[0]}`", f"`{values[1]}`", *map(_md_cell, values[2:])]
      if with_result:
        row.append(_md_outcome(report.outcomes[test.test_id]))
      rows.append(row)
    lines += _md_table(_test_header(kind, with_result), rows)
  return lines


# HTML.


def _html_code(text: str) -> str:
  """Return text as HTML code."""
  return f"<code>{html.escape(text)}</code>"


def _html_link(text: str, href: str) -> str:
  """Return an HTML link."""
  return f'<a href="{html.escape(href)}">{html.escape(text)}</a>'


def _html_text(text: str) -> str:
  """Return text as HTML, keeping its code spans and bold."""
  escaped = html.escape(_plain(text), quote=False)
  return _BOLD.sub(
    r"<strong>\1</strong>", _CODE.sub(r"<code>\1</code>", escaped)
  )


def _html_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
  """Return an HTML table; the cells are HTML already."""
  lines = [
    "<table>",
    "<tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr>",
  ]
  lines += ["<tr>" + "".join(row) + "</tr>" for row in rows]
  lines.append("</table>")
  return "\n".join(lines)


def _card(big: str, label: str) -> str:
  """Return a headline number with its label."""
  return f'<div class="card"><div class="big">{big}</div>{label}</div>'


def _anchor(name: str) -> str:
  """Return the HTML ID of a capability's section."""
  return "cap-" + _short_cap(name).replace(".", "-")


def _html_summary(report: Report) -> str:
  """Return the summary table."""
  header = [
    "Capability",
    "Extends",
    "Rules",
    "Testable",
    "Fully checked",
    "Testable MUST",
    "MUST fully checked",
    "MUST checked in part",
    "MUST without a test",
    "MUST coverage",
  ]
  rows = []
  for row in _ordered(report):
    name = row.capability.name
    label = _html_link(_short_cap(name), f"#{_anchor(name)}")
    extends = ", ".join(_short_cap(parent) for parent in row.capability.extends)
    rows.append(
      _html_tally_cells(label, html.escape(extends or "–"), row.tally)
    )
  total = _html_tally_cells("All rules, each counted once", "", report.total)
  table = _html_table(header, rows)
  return table.replace("</table>", f'<tr class="total">{total}</tr>\n</table>')


def _html_tally_cells(label: str, extends: str, tally: Tally) -> str:
  """Return a summary row's cells."""
  share = tally.must_coverage
  width = 0 if share is None else round(100 * share)
  numbers = "".join(
    f'<td class="num">{value}</td>'
    for value in (
      tally.rules,
      tally.testable,
      tally.covered,
      tally.testable_must,
      tally.covered_must,
      tally.partial_must,
      tally.uncovered_must,
    )
  )
  bar = f'<span class="bar"><span style="width: {width}%"></span></span>'
  return (
    f"<td>{label}</td><td>{extends}</td>{numbers}"
    f'<td class="cov">{bar} {_percent(share)}</td>'
  )


def _html_versions(report: Report) -> str:
  """Return the view by release."""
  rows = []
  for row in report.versions:
    share = row.must_coverage
    width = 0 if share is None else round(100 * share)
    numbers = "".join(
      f'<td class="num">{value}</td>'
      for value in (
        row.testable_must,
        row.covered_must,
        row.partial_must,
        row.uncovered_must,
      )
    )
    bar = f'<span class="bar"><span style="width: {width}%"></span></span>'
    rows.append(
      f"<td>{html.escape(_version_label(report, row.version))}</td>"
      f'{numbers}<td class="cov">{bar} {_percent(share)}</td>'
    )
  return "\n".join(
    [
      f"<h3>{VERSIONS_TITLE}</h3>",
      f"<p>{VERSIONS_INTRO}</p>",
      _html_table(VERSIONS_HEADER, rows),
    ]
  )


def _html_result(status: str) -> str:
  """Return a result or outcome, colored by the stylesheet."""
  return f'<span class="r-{status}">{LABELS[status]}</span>'


def _html_outcome(outcome: Outcome, detail: bool = True) -> str:
  """Return a test's outcome, with its detail unless asked not to.

  A skipped test's category follows its label.
  """
  label = _outcome_label(outcome) if outcome.status == SKIPPED else None
  result = (
    f'<span class="r-{outcome.status}">{html.escape(label)}</span>'
    if label
    else _html_result(outcome.status)
  )
  if detail and outcome.detail:
    return f"{result}: {html.escape(outcome.detail)}"
  return result


def _html_test(
  report: Report, test_id: str, detail: bool = False, gap: str | None = None
) -> str:
  """Return a test's name, its gap for a partly checked rule, and outcome."""
  name = _html_code(_short_test(test_id))
  if gap is not None:
    name += f" (in part: {html.escape(gap)})"
  if report.outcomes is None:
    return name
  return f"{name} {_html_outcome(report.outcomes[test_id], detail)}"


def _results_card(report: Report) -> str:
  """Return the headline card for the results."""
  counts = results(report, report.rules) or dict.fromkeys(
    (*RULE_RESULTS, *(f"must_{result}" for result in RULE_RESULTS)), 0
  )
  not_applicable = counts["must_not_applicable"]
  return _card(
    f"{counts['must_passed']} / {_must_applicable(report.total, counts)}",
    "testable MUST and MUST NOT rules with a test passed: "
    f"{counts['must_failed']} failed"
    + (f", {counts['must_error']} error" if counts["must_error"] else "")
    + f", {counts['must_not_run']} not run"
    + (f", {not_applicable} more not applicable" if not_applicable else ""),
  )


def _html_results(report: Report) -> str:
  """Return the results section, after its heading."""
  header = [
    "Capability",
    "With a test",
    "Passed",
    "Failed",
    "Error",
    "Not run",
    "Not applicable",
    "MUST with a test",
    "MUST passed",
    "MUST failed",
    "MUST error",
    "MUST not run",
    "MUST not applicable",
    "MUST results",
  ]
  rows = []
  for row in _ordered(report):
    name = row.capability.name
    label = _html_link(_short_cap(name), f"#{_anchor(name)}")
    counts = results(report, _rules_of(report, name))
    rows.append(_html_results_cells(label, row.tally, counts))
  total = _html_results_cells(
    "All rules, each counted once",
    report.total,
    results(report, report.rules),
  )
  table = _html_table(header, rows).replace(
    "</table>", f'<tr class="total">{total}</tr>\n</table>'
  )
  upgrades = upgrade_list(report)
  parts = [
    f"<p><strong>{_results_headline(report)}</strong> "
    f"{_outcome_counts(report)}</p>",
    table,
    f"<h3>{UPGRADE_TITLE}: {len(upgrades)}</h3>",
    f'<p class="note">{UPGRADE_INTRO}</p>',
    _html_rules(report, upgrades, "outcomes"),
  ]
  by_category = not_run_by_category(report, report.rules) or {}
  for title, result in RESULT_LISTS:
    rules = _rules_with_result(report, result)
    parts.append(f"<h3>{title}: {len(rules)}</h3>")
    if result == NOT_RUN and rules:
      parts.append(f'<p class="note">{_why_not_run(by_category["all"])}</p>')
    parts.append(_html_rules(report, rules, "outcomes"))
  return "\n".join(parts)


def _html_results_cells(
  label: str, tally: Tally, counts: Mapping[str, int]
) -> str:
  """Return a results row's cells, ending with a bar of the MUST results."""
  with_a_test = _must_with_a_test(tally)
  numbers = "".join(
    f'<td class="num">{value}</td>'
    for value in (
      tally.covered + tally.partial,
      counts[PASSED],
      counts[FAILED],
      counts[ERROR],
      counts[NOT_RUN],
      counts[NOT_APPLICABLE],
      with_a_test,
      counts["must_passed"],
      counts["must_failed"],
      counts["must_error"],
      counts["must_not_run"],
      counts["must_not_applicable"],
    )
  )
  spans = "".join(
    f'<span class="{css}" style="width: '
    f'{round(100 * counts[key] / with_a_test)}%"></span>'
    for key, css in (
      ("must_passed", "pass"),
      ("must_failed", "fail"),
      ("must_error", "err"),
      ("must_not_run", "skip"),
      ("must_not_applicable", "na"),
    )
    if with_a_test and counts[key]
  )
  return (
    f"<td>{label}</td>{numbers}"
    f'<td class="cov"><span class="stack">{spans}</span></td>'
  )


def _html_capability(report: Report, row: CapabilityRow) -> str:
  """Return a capability's section."""
  groups = rule_groups(report, row.capability.name)
  sections = [
    ("Rules fully checked by a test", groups.covered, "tests", True),
    ("Rules checked only in part", groups.partial, "tests", True),
    (
      "Testable MUST and MUST NOT rules without a test",
      groups.uncovered_must,
      "",
      True,
    ),
    ("Other testable rules without a test", groups.other, "", False),
    ("Not testable by this suite", groups.untestable, "why", False),
  ]
  parts = [
    f'<section id="{_anchor(row.capability.name)}">',
    f"<h3>{html.escape(_short_cap(row.capability.name))}</h3>",
    f"<p>{_capability_sentence(report, row, _html_code)}</p>",
  ]
  for title, rules, extra, expanded in sections:
    is_open = " open" if expanded else ""
    parts += [
      f"<details{is_open}><summary>{title} ({len(rules)})</summary>",
      _html_rules(report, rules, extra),
      "</details>",
    ]
  parts.append("</section>")
  return "\n".join(parts)


def _html_rules(report: Report, rules: Sequence[Rule], extra: str) -> str:
  """Return a table of rules, or "None."; `extra` adds a column.

  Args:
    report: The report.
    rules: The rules.
    extra: As for `_md_rules`.

  Returns:
    The table.

  """
  if not rules:
    return "<p>None.</p>"
  with_result = extra == "tests" and report.outcomes is not None
  rows = []
  for rule in rules:
    cells = [
      f'<td class="id">{html.escape(_short_id(rule.id))}</td>',
      f'<td class="level">{html.escape(rule.level)}</td>',
    ]
    if with_result:
      result = _html_result(report.result(rule.id))
      cells.append(f'<td class="result">{result}</td>')
    cells.append(f"<td>{_html_text(rule.clause)}</td>")
    if extra in ("tests", "outcomes"):
      tests = report.cited_by.get(rule.id, ())
      detail = extra == "outcomes"
      names = "<br>".join(
        _html_test(report, t, detail, report.gap(rule.id, t)) for t in tests
      )
      cells.append(f"<td>{names}</td>")
    elif extra == "why":
      cells.append(f"<td>{_html_text(rule.testable_reason)}</td>")
    url = _source_url(report, rule)
    label = _source_label(rule)
    source = _html_link(label, url) if url else html.escape(label)
    cells.append(f"<td>{source}</td>")
    rows.append(cells)
  return _html_table(_rule_header(extra, with_result), rows)


def _html_tests(report: Report) -> str:
  """Return the tests section."""
  with_result = report.outcomes is not None
  parts = ['<h2 id="tests">Tests</h2>']
  for kind, tests in _tests_by_kind(report):
    if kind == UNMARKED and not tests:
      continue
    marker = "" if kind == UNMARKED else f" ({_html_code(kind)})"
    parts.append(f"<h3>{KIND_TITLES[kind]}{marker}: {len(tests)}</h3>")
    if not tests:
      parts.append("<p>None.</p>")
      continue
    rows = []
    for test in tests:
      values = _test_values(test)
      cells = [_html_code(values[0]), _html_code(values[1])]
      cells += [_html_text(value) for value in values[2:]]
      if with_result:
        cells.append(_html_outcome(report.outcomes[test.test_id]))
      rows.append([f"<td>{cell}</td>" for cell in cells])
    parts.append(_html_table(_test_header(kind, with_result), rows))
  return "\n".join(parts)
