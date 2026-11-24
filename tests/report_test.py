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

"""Unit tests for the coverage report in framework.report.

Most use a small catalog built in memory; the last classes read this
repository's catalog and drive the runner with `--dry-run`.
"""

import contextlib
import dataclasses
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest
from unittest import mock

import runner
from framework import report
from framework.decorators import SpecMarker, requires_capability, spec_assert
from framework.discovery import UcpProfile
from framework.marker_check import LoadedTest
from framework.outcomes import (
  ERROR,
  FAILED,
  NOT_APPLICABLE,
  NOT_RUN,
  PASSED,
  SKIPPED,
  Outcome,
)
from framework.outcomes import OTHER as OTHER_REASON
from framework.requirements import (
  DEFAULT_CATALOG_DIR,
  INDEX_FILE,
  Capability,
  Catalog,
  Rule,
  Statement,
  load_catalog,
)

CART = "dev.ucp.shopping.cart"
CHECKOUT = "dev.ucp.shopping.checkout"
DISCOUNT = "dev.ucp.shopping.discount"
LOOKUP = "dev.ucp.shopping.catalog.lookup"
SEARCH = "dev.ucp.shopping.catalog.search"
OTHER = "dev.ucp.shopping.other"


def rule(
  rule_id: str,
  capabilities: tuple[str, ...],
  level: str = "MUST",
  testable: bool = True,
  is_rule: bool = True,
) -> Rule:
  """Return a rule."""
  return Rule(
    id=rule_id,
    level=level,
    clause=f"The business {level} do {rule_id}.",
    capabilities=capabilities,
    source_file="docs/specification/shopping/checkout/index.md",
    line=1,
    link="specification/shopping/checkout/#a",
    testable=testable,
    testable_reason="aimed at the business" if testable else "MCP page",
    is_rule=is_rule,
  )


RULES = (
  rule("REQ-SHOPPING-CART-0001", (CART,)),
  rule("REQ-SHOPPING-CATALOG-0001", (LOOKUP, SEARCH)),
  rule("REQ-SHOPPING-CHECKOUT-0001", (CHECKOUT,)),
  rule("REQ-SHOPPING-CHECKOUT-0002", (CHECKOUT,), level="MUST NOT"),
  rule("REQ-SHOPPING-CHECKOUT-0003", (CHECKOUT,), level="SHOULD"),
  rule("REQ-SHOPPING-CHECKOUT-0004", (CHECKOUT,), testable=False),
  rule("REQ-SHOPPING-CHECKOUT-0005", (CHECKOUT,), is_rule=False),
  rule("REQ-SHOPPING-OTHER-0001", (OTHER,), level="MAY"),
)
CATALOG = Catalog(
  rules=MappingProxyType({r.id: r for r in RULES}),
  retired=frozenset(),
  extends=MappingProxyType({DISCOUNT: (CART, CHECKOUT)}),
  capabilities=(
    Capability(CART),
    Capability(CHECKOUT),
    Capability(DISCOUNT, "extension", (CART, CHECKOUT)),
    Capability(LOOKUP),
    Capability(SEARCH),
  ),
  spec_commit="abc123def456",
)


def loaded(
  name: str,
  marker: SpecMarker | None,
  module: str = "shopping.checkout.example_test",
) -> LoadedTest:
  """Return a loaded test that requires checkout."""
  return LoadedTest(
    f"{module}.ExampleTest.{name}", module, marker, frozenset({CHECKOUT})
  )


def cites(*rule_ids: str) -> SpecMarker:
  """Return a spec_assert marker."""
  return SpecMarker("spec_assert", rule_ids=rule_ids)


TESTS = (
  loaded(
    "test_b",
    cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CATALOG-0001"),
  ),
  loaded(
    "test_a", cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CHECKOUT-0003")
  ),
  loaded("test_c", cites("REQ-SHOPPING-CHECKOUT-0004")),
  loaded(
    "test_d", cites("REQ-SHOPPING-CHECKOUT-0005", "REQ-SHOPPING-CHECKOUT-0099")
  ),
  loaded("test_e", SpecMarker("no_rule", reason="no keyword")),
  loaded("test_f", None),
  loaded("test_core", None, module="core.protocol_test"),
)


def full_id(name: str) -> str:
  """Return the ID `loaded` gives a test."""
  return f"shopping.checkout.example_test.ExampleTest.{name}"


# A run: two more tests, and an outcome for every test but test_f.
RUN_TESTS = (
  *TESTS,
  loaded("test_g", cites("REQ-SHOPPING-CART-0001")),
  loaded("test_h", cites("REQ-SHOPPING-CHECKOUT-0002")),
)
OUTCOMES = {
  full_id("test_a"): Outcome(PASSED),
  full_id("test_b"): Outcome(FAILED, "total 5 | expected *6* <b>"),
  full_id("test_c"): Outcome(SKIPPED, "no fixture"),
  full_id("test_d"): Outcome(ERROR, "KeyError: 'id'"),
  full_id("test_e"): Outcome(PASSED),
  full_id("test_g"): Outcome(SKIPPED, "no cart capability"),
  full_id("test_h"): Outcome(PASSED),
  "core.protocol_test.ExampleTest.test_core": Outcome(PASSED),
}
TARGET = "http://localhost:8182"


def fake_shopping_suite() -> unittest.TestSuite:
  """Return shopping tests that pass, fail and skip, citing real rules.

  The test case class is built here, so that nothing collects it on its own.
  """

  @requires_capability(CHECKOUT)
  class FakeTest(unittest.TestCase):
    @spec_assert("REQ-SHOPPING-CHECKOUT-0010")
    def test_pass(self):
      """Pass."""

    @spec_assert("REQ-SHOPPING-CHECKOUT-0011")
    def test_fail(self):
      """Fail."""
      self.fail("total was 5, expected 6")

    @spec_assert("REQ-SHOPPING-CHECKOUT-0105")
    def test_skip(self):
      """Skip."""
      self.skipTest("no second discount code configured")

  FakeTest.__module__ = "shopping.fake_test"
  FakeTest.__qualname__ = "FakeTest"
  return unittest.defaultTestLoader.loadTestsFromTestCase(FakeTest)


class BuildTest(unittest.TestCase):
  """Tests for build."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_tests_and_markers(self):
    """Test that shopping tests are listed by ID, and markers counted."""
    self.assertEqual(
      [row.test_id for row in self.report.tests],
      [full_id(f"test_{letter}") for letter in "abcdef"],
    )
    self.assertEqual(
      self.report.markers(),
      {
        "spec_assert": 4,
        "no_rule": 1,
        "scenario": 0,
        "outside_catalog": 0,
        "older_version": 0,
        "unmarked": 1,
      },
    )
    unmarked = self.report.tests[-1]
    self.assertEqual((unmarked.kind, unmarked.rule_ids), ("unmarked", ()))
    self.assertEqual(self.report.tests[4].reason, "no keyword")

  def test_cited_by(self):
    """Test that only IDs naming rules are cited, each with its tests."""
    self.assertEqual(
      dict(self.report.cited_by),
      {
        "REQ-SHOPPING-CATALOG-0001": (full_id("test_b"),),
        "REQ-SHOPPING-CHECKOUT-0001": (full_id("test_a"), full_id("test_b")),
        "REQ-SHOPPING-CHECKOUT-0003": (full_id("test_a"),),
        "REQ-SHOPPING-CHECKOUT-0004": (full_id("test_c"),),
      },
    )

  def test_totals(self):
    """Test the counts over all rules, each rule counted once."""
    total = self.report.total
    self.assertEqual(
      total,
      report.Tally(
        rules=7, testable=6, testable_must=4, covered=3, covered_must=2
      ),
    )
    self.assertEqual(total.uncovered_must, 2)
    self.assertEqual(total.must_coverage, 0.5)
    self.assertEqual(self.report.not_rules, 1)
    self.assertEqual(len(self.report.rules), 7)
    self.assertEqual(self.report.spec_commit, "abc123def456")

  def test_capabilities(self):
    """Test each capability's counts, in the index's order."""
    rows = {row.capability.name: row.tally for row in self.report.capabilities}
    self.assertEqual(
      [row.capability.name for row in self.report.capabilities],
      [CART, CHECKOUT, DISCOUNT, LOOKUP, SEARCH, OTHER],
    )
    self.assertEqual(rows[CHECKOUT], report.Tally(4, 3, 2, 2, 1))
    self.assertEqual(rows[CART], report.Tally(1, 1, 1, 0, 0))
    self.assertEqual(rows[LOOKUP], report.Tally(1, 1, 1, 1, 1))
    self.assertEqual(rows[SEARCH], rows[LOOKUP])
    self.assertEqual(rows[DISCOUNT], report.Tally(0, 0, 0, 0, 0))
    self.assertIsNone(rows[DISCOUNT].must_coverage)
    self.assertEqual(rows[OTHER], report.Tally(1, 1, 0, 0, 0))
    self.assertEqual(self.report.capabilities[2].capability.kind, "extension")


class OutputTest(unittest.TestCase):
  """Tests for to_json, write, summary and generate."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_json(self):
    """Test the shape of matrix.json, with null run fields after no run."""
    data = report.to_json(self.report)
    self.assertEqual(
      list(data),
      [
        "format",
        "spec_commit",
        "totals",
        "versions",
        "not_rules",
        "markers",
        "run",
        "capabilities",
        "rules",
        "tests",
      ],
    )
    self.assertEqual(data["format"], report.FORMAT_VERSION)
    self.assertEqual(
      data["totals"],
      {
        "rules": 7,
        "testable": 6,
        "testable_must": 4,
        "covered": 3,
        "covered_must": 2,
        "partial": 0,
        "partial_must": 0,
        "uncovered_must": 2,
      },
    )
    # Without a history, the only version is the catalog's own.
    self.assertEqual(
      data["versions"],
      [
        {
          "version": "draft",
          "testable_must": 4,
          "covered_must": 2,
          "partial_must": 0,
          "uncovered_must": 2,
        }
      ],
    )
    discount = data["capabilities"][2]
    self.assertEqual(
      (discount["name"], discount["kind"], discount["extends"]),
      (DISCOUNT, "extension", [CART, CHECKOUT]),
    )
    by_id = {entry["id"]: entry for entry in data["rules"]}
    self.assertNotIn("REQ-SHOPPING-CHECKOUT-0005", by_id)
    self.assertEqual(
      by_id["REQ-SHOPPING-CHECKOUT-0001"]["tests"],
      [full_id("test_a"), full_id("test_b")],
    )
    self.assertEqual(by_id["REQ-SHOPPING-CART-0001"]["tests"], [])
    self.assertEqual(
      data["tests"][3]["rules"],
      ["REQ-SHOPPING-CHECKOUT-0005", "REQ-SHOPPING-CHECKOUT-0099"],
    )
    self.assertEqual(data["tests"][0]["capabilities"], [CHECKOUT])
    self.assertIsNone(data["run"])
    self.assertIsNone(discount["results"])
    self.assertIsNone(by_id["REQ-SHOPPING-CHECKOUT-0001"]["result"])
    self.assertIsNone(data["tests"][0]["outcome"])

  def test_write(self):
    """Test that write creates the directory, and writes the same bytes."""
    with tempfile.TemporaryDirectory() as directory:
      out = Path(directory) / "new" / "reports"
      paths = report.write(self.report, out)
      self.assertEqual(
        paths,
        [
          out / report.JSON_FILE,
          out / report.MARKDOWN_FILE,
          out / report.HTML_FILE,
        ],
      )
      first = [path.read_bytes() for path in paths]
      self.assertEqual(json.loads(first[0]), report.to_json(self.report))
      self.assertEqual(first[1].decode(), report.to_markdown(self.report))
      self.assertEqual(first[2].decode(), report.to_html(self.report))
      report.write(self.report, out)
      self.assertEqual([path.read_bytes() for path in paths], first)

  def test_write_links_the_findings(self):
    """Test that write links the findings document relative to the output."""
    with tempfile.TemporaryDirectory() as directory:
      findings = Path(directory) / "findings.md"
      paths = report.write(self.report, Path(directory) / "out", findings)
      markdown = paths[1].read_text()
    self.assertIn("- **Findings:** [findings.md](../findings.md)", markdown)

  def test_summary(self):
    """Test the console lines."""
    text = report.summary(self.report, [Path("out/matrix.json")])
    self.assertIn("Requirements report: out (matrix.json)", text)
    self.assertIn(
      "catalog at abc123de: 7 rules, 6 testable, 4 of them MUST or MUST NOT",
      text,
    )
    self.assertIn(
      "6 tests cite 4 rules; 2 of the 4 testable MUST and MUST NOT rules "
      "are fully checked by a test (50%), 2 have no test",
      text,
    )

  def test_summary_without_musts(self):
    """Test the console lines when no rule is a testable MUST."""
    empty = Catalog(rules=MappingProxyType({}), retired=frozenset())
    text = report.summary(report.build(empty, ()), [])
    self.assertIn("catalog at an unknown commit: 0 rules", text)
    self.assertIn("fully checked by a test, 0 have no test", text)


class TagBalance(HTMLParser):
  """Track open elements, to check that a page closes what it opens."""

  VOID = frozenset({"br", "hr", "img", "input", "link", "meta"})

  def __init__(self):
    """Start with no open elements."""
    super().__init__()
    self.open: list[str] = []
    self.mismatched: list[str] = []

  def handle_starttag(self, tag, attrs):
    """Open an element, unless it has no end tag."""
    del attrs
    if tag not in self.VOID:
      self.open.append(tag)

  def handle_endtag(self, tag):
    """Close the innermost element, noting a mismatch."""
    if not self.open or self.open.pop() != tag:
      self.mismatched.append(tag)


TRICKY = Catalog(
  rules=MappingProxyType(
    {
      "REQ-SHOPPING-CART-0009": dataclasses.replace(
        rule("REQ-SHOPPING-CART-0009", (CART,)),
        clause="Use `a|b` or c | d, per [RFC 9535](https://example.com/rfc)"
        '{ target="_blank" }, never <b>bold</b>; **MUST** apply.',
      )
    }
  ),
  retired=frozenset(),
)


class RenderTest(unittest.TestCase):
  """Tests for to_markdown, to_html and rule_groups."""

  def setUp(self):
    """Build the report for the small catalog."""
    super().setUp()
    self.report = report.build(CATALOG, TESTS)

  def test_markdown_summary(self):
    """Test the headline and the summary table, extensions last."""
    text = report.to_markdown(self.report)
    self.assertTrue(text.startswith("# Shopping requirements coverage\n"))
    self.assertIn(
      "**2 of the 4 testable MUST and MUST NOT rules are fully checked by a "
      "test (50%), and 3 of the 6 testable rules.** 6 shopping tests: 4 cite "
      "rules, 1 `no_rule`, 0 `scenario`, 0 `outside_catalog`, 0 "
      "`older_version`, 1 unmarked.",
      text,
    )
    rows = [
      "| cart | – | 1 | 1 | 0 | 1 | 0 | 0 | 1 | 0% |",
      "| checkout | – | 4 | 3 | 2 | 2 | 1 | 0 | 1 | 50% |",
      "| catalog.lookup | – | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 100% |",
      "| catalog.search | – | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 100% |",
      "| other | – | 1 | 1 | 0 | 0 | 0 | 0 | 0 | – |",
      "| discount | cart, checkout | 0 | 0 | 0 | 0 | 0 | 0 | 0 | – |",
      "| **All rules, each counted once** |  | 7 | 6 | 3 | 4 | 2 | 0 | 2 "
      "| 50% |",
    ]
    positions = [text.index(row) for row in rows]
    self.assertEqual(positions, sorted(positions))

  def test_markdown_sections(self):
    """Test a capability's section, the tests and the notes."""
    text = report.to_markdown(self.report)
    source = (
      "[checkout/index.md:1](https://github.com/Universal-Commerce-Protocol/"
      "ucp/blob/abc123def456/docs/specification/shopping/checkout/index.md"
      "?plain=1#L1)"
    )
    for expected in (
      "## checkout\n\n`dev.ucp.shopping.checkout`, a capability: 4 rules, 3 "
      "of them testable. 1 of its 2 testable MUST and MUST NOT rules are "
      "fully checked by a test (50%).",
      "### Rules fully checked by a test: 2",
      "### Rules checked only in part: 0\n\nNone.",
      "| CHECKOUT-0001 | MUST | The business MUST do "
      "REQ-SHOPPING-CHECKOUT-0001. | `ExampleTest.test_a`<br>"
      f"`ExampleTest.test_b` | {source} |",
      "### Testable MUST and MUST NOT rules without a test: 1",
      "<summary>Other testable rules without a test: 0</summary>\n\nNone.",
      "<summary>Not testable by this suite: 1</summary>",
      "| CHECKOUT-0004 | MUST | The business MUST do "
      f"REQ-SHOPPING-CHECKOUT-0004. | MCP page | {source} |",
      "`dev.ucp.shopping.discount`, an extension of cart, checkout: 0 rules",
      "### Tests that cite rules (`spec_assert`): 4",
      "| `ExampleTest.test_d` | `shopping/checkout/example_test.py` | "
      "CHECKOUT-0005, CHECKOUT-0099 |",
      "| `ExampleTest.test_e` | `shopping/checkout/example_test.py` | "
      "no keyword |",
      "### Checks of one store's data or features (`scenario`): 0\n\nNone.",
      "### Tests without a marker: 1",
      "- **IDs:** Rule IDs drop the `REQ-SHOPPING-` prefix.",
      "and the sentence the review found isn't a rule is left out.",
      "- **Shared rules:** 1 rule belongs to more than one capability.",
    ):
      self.assertIn(expected, text)
    self.assertNotIn("**Findings:**", text)
    self.assertIn(
      "- **Findings:** [findings.md](../findings.md) reviews each claim",
      report.to_markdown(self.report, "../findings.md"),
    )

  def test_html(self):
    """Test the page: headline cards, anchors, and only its own resources."""
    page = report.to_html(self.report, "../findings.md")
    self.assertTrue(page.startswith("<!DOCTYPE html>\n"))
    for expected in (
      '<div class="big">2 / 4</div>',
      '<a href="#cap-catalog-lookup">catalog.lookup</a>',
      '<section id="cap-catalog-lookup">',
      '<span style="width: 50%"></span>',
      '<tr class="total"><td>All rules, each counted once</td>',
      "<details open><summary>Rules fully checked by a test (2)</summary>",
      "<details><summary>Not testable by this suite (1)</summary>",
      "<code>ExampleTest.test_a</code><br><code>ExampleTest.test_b</code>",
      '<a href="../findings.md">findings.md</a>',
    ):
      self.assertIn(expected, page)
    for external in ("<script", "<link", " src="):
      self.assertNotIn(external, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_rule_text_is_escaped(self):
    """Test pipes, links and markup in rule text, in both outputs."""
    built = report.build(TRICKY, ())
    markdown = report.to_markdown(built)
    page = report.to_html(built)
    self.assertIn(
      "| CART-0009 | MUST | Use `a\\|b` or c \\| d, per RFC 9535, never "
      "&lt;b>bold&lt;/b>; **MUST** apply. | checkout/index.md:1 |",
      markdown,
    )
    self.assertIn(
      "<td>Use <code>a|b</code> or c | d, per RFC 9535, never "
      "&lt;b&gt;bold&lt;/b&gt;; <strong>MUST</strong> apply.</td>",
      page,
    )
    for text in (markdown, page):
      self.assertNotIn("example.com", text)
      self.assertNotIn("_blank", text)

  def test_rule_groups_match_the_counts(self):
    """Test that each capability's groups add up to its counts."""
    for row in self.report.capabilities:
      groups = report.rule_groups(self.report, row.capability.name)
      tally = row.tally
      with self.subTest(row.capability.name):
        self.assertEqual(len(groups.covered), tally.covered)
        self.assertEqual(len(groups.partial), tally.partial)
        self.assertEqual(len(groups.uncovered_must), tally.uncovered_must)
        self.assertEqual(
          len(groups.covered)
          + len(groups.partial)
          + len(groups.uncovered_must)
          + len(groups.other),
          tally.testable,
        )
        self.assertEqual(len(groups.untestable), tally.rules - tally.testable)
    checkout = report.rule_groups(self.report, CHECKOUT)
    self.assertEqual(
      [rule.id for rule in checkout.untestable], ["REQ-SHOPPING-CHECKOUT-0004"]
    )


# Partial claims: test_p checks CHECKOUT-0001 only in part and CHECKOUT-0002
# fully; test_q checks CHECKOUT-0002 and CART-0001 only in part.
PARTIAL_TESTS = (
  loaded(
    "test_p",
    SpecMarker(
      "spec_assert",
      rule_ids=("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CHECKOUT-0002"),
      gaps=(("REQ-SHOPPING-CHECKOUT-0001", "whole numbers only"),),
    ),
  ),
  loaded(
    "test_q",
    SpecMarker(
      "spec_assert",
      rule_ids=("REQ-SHOPPING-CHECKOUT-0002", "REQ-SHOPPING-CART-0001"),
      gaps=(
        ("REQ-SHOPPING-CHECKOUT-0002", "headers only"),
        ("REQ-SHOPPING-CART-0001", "create only"),
      ),
    ),
  ),
)


class PartialClaimTest(unittest.TestCase):
  """Tests for rules that tests check only in part."""

  def setUp(self):
    """Build the report for the partial claims."""
    super().setUp()
    self.report = report.build(CATALOG, PARTIAL_TESTS)

  def test_coverage_and_gaps(self):
    """Test that only a full claim covers a rule, and that gaps are kept."""
    self.assertEqual(
      {rule.id: self.report.coverage(rule.id) for rule in self.report.rules},
      {
        "REQ-SHOPPING-CART-0001": report.PARTIAL,
        "REQ-SHOPPING-CATALOG-0001": report.NONE,
        "REQ-SHOPPING-CHECKOUT-0001": report.PARTIAL,
        "REQ-SHOPPING-CHECKOUT-0002": report.FULL,
        "REQ-SHOPPING-CHECKOUT-0003": report.NONE,
        "REQ-SHOPPING-CHECKOUT-0004": report.NONE,
        "REQ-SHOPPING-OTHER-0001": report.NONE,
      },
    )
    self.assertEqual(
      self.report.gap("REQ-SHOPPING-CHECKOUT-0001", full_id("test_p")),
      "whole numbers only",
    )
    self.assertIsNone(
      self.report.gap("REQ-SHOPPING-CHECKOUT-0002", full_id("test_p"))
    )

  def test_counts_and_groups(self):
    """Test that partly checked rules are counted apart from covered ones."""
    total = self.report.total
    self.assertEqual(
      total,
      report.Tally(
        rules=7,
        testable=6,
        testable_must=4,
        covered=1,
        covered_must=1,
        partial=2,
        partial_must=2,
      ),
    )
    self.assertEqual(total.uncovered_must, 1)
    self.assertEqual(total.must_coverage, 0.25)
    checkout = report.rule_groups(self.report, CHECKOUT)
    self.assertEqual(
      [rule.id for rule in checkout.covered], ["REQ-SHOPPING-CHECKOUT-0002"]
    )
    self.assertEqual(
      [rule.id for rule in checkout.partial], ["REQ-SHOPPING-CHECKOUT-0001"]
    )
    self.assertEqual(checkout.uncovered_must, ())

  def test_json(self):
    """Test each rule's coverage and gaps, and each test's partial claims."""
    data = report.to_json(self.report)
    self.assertEqual(
      (data["totals"]["partial"], data["totals"]["partial_must"]), (2, 2)
    )
    by_id = {entry["id"]: entry for entry in data["rules"]}
    full = by_id["REQ-SHOPPING-CHECKOUT-0002"]
    self.assertEqual(full["coverage"], "full")
    self.assertEqual(full["gaps"], {full_id("test_q"): "headers only"})
    self.assertEqual(by_id["REQ-SHOPPING-CART-0001"]["coverage"], "partial")
    self.assertEqual(by_id["REQ-SHOPPING-CATALOG-0001"]["coverage"], "none")
    self.assertEqual(by_id["REQ-SHOPPING-CATALOG-0001"]["gaps"], {})
    tests = {entry["id"]: entry for entry in data["tests"]}
    self.assertEqual(
      tests[full_id("test_p")]["partial"],
      {"REQ-SHOPPING-CHECKOUT-0001": "whole numbers only"},
    )

  def test_markdown_and_html(self):
    """Test the headline, the extra column, the section and the gaps."""
    text = report.to_markdown(self.report)
    for expected in (
      "**1 of the 4 testable MUST and MUST NOT rules are fully checked by a "
      "test (25%), and 1 of the 6 testable rules. 2 more MUST and MUST NOT "
      "rules are checked only in part.**",
      "| checkout | – | 4 | 3 | 1 | 2 | 1 | 1 | 0 | 50% |",
      "1 of its 2 testable MUST and MUST NOT rules are fully checked by a "
      "test (50%), and 1 more in part.",
      "### Rules checked only in part: 1",
      "`ExampleTest.test_p` (in part: whole numbers only)",
      "`ExampleTest.test_p`<br>`ExampleTest.test_q` (in part: headers only)",
      "| `ExampleTest.test_p` | `shopping/checkout/example_test.py` | "
      "CHECKOUT-0001 (in part), CHECKOUT-0002 |",
      "- **Partial claims:** A test cites a rule with `partly`",
    ):
      self.assertIn(expected, text)
    page = report.to_html(self.report)
    for expected in (
      '<div class="big">2</div>more testable MUST and MUST NOT rules are '
      "checked only in part",
      "<details open><summary>Rules checked only in part (1)</summary>",
      "<code>ExampleTest.test_q</code> (in part: headers only)",
    ):
      self.assertIn(expected, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_summary_and_results(self):
    """Test the console lines, and that results count partly checked rules."""
    text = report.summary(self.report, [Path("out/matrix.json")])
    self.assertIn(
      "1 of the 4 testable MUST and MUST NOT rules are fully checked by a "
      "test (25%), 2 more only in part, 1 have no test\n",
      text,
    )
    run = report.build(
      CATALOG,
      PARTIAL_TESTS,
      {
        full_id("test_p"): Outcome(PASSED),
        full_id("test_q"): Outcome(FAILED, "missing header"),
      },
      TARGET,
    )
    self.assertEqual(
      report.results(run, run.rules),
      {
        "passed": 1,
        "failed": 2,
        "error": 0,
        "not_run": 0,
        "not_applicable": 0,
        "must_passed": 1,
        "must_failed": 2,
        "must_error": 0,
        "must_not_run": 0,
        "must_not_applicable": 0,
      },
    )
    self.assertIn(
      "results against http://localhost:8182: 1 of the 3 testable MUST and "
      "MUST NOT rules with a test passed, 2 failed, 0 weren't run\n",
      report.summary(run, []),
    )
    self.assertIn(
      "**Of the 3 testable MUST and MUST NOT rules with a test, 1 passed, 2 "
      "failed and 0 weren't run.**",
      report.to_markdown(run),
    )


def stated(version: str, level: str = "MUST") -> Statement:
  """Return how a release states a rule."""
  return Statement(
    version, level, "The business MUST do it.", "docs/specification/a.md", 1
  )


# A catalog with a history: a rule that was a SHOULD in its first release, a
# rule new in the draft, an untestable rule, and a rule only 2026-01-11
# states.
HISTORY_VERSIONS = ("2026-01-11", "2026-04-08", "draft")
REMOVED_RULE = dataclasses.replace(
  rule("REQ-SHOPPING-CHECKOUT-0090", (CHECKOUT,)),
  since="2026-01-11",
  until="2026-04-08",
  statements=(stated("2026-01-11"),),
)
HISTORY_CATALOG = Catalog(
  rules=MappingProxyType(
    {
      r.id: r
      for r in (
        dataclasses.replace(
          rule("REQ-SHOPPING-CHECKOUT-0001", (CHECKOUT,)),
          since="2026-01-11",
          statements=(stated("2026-01-11", "SHOULD"), stated("2026-04-08")),
        ),
        rule("REQ-SHOPPING-CHECKOUT-0002", (CHECKOUT,), level="MUST NOT"),
        dataclasses.replace(
          rule("REQ-SHOPPING-CHECKOUT-0004", (CHECKOUT,), testable=False),
          since="2026-04-08",
          statements=(stated("2026-04-08"),),
        ),
      )
    }
  ),
  retired=frozenset({REMOVED_RULE.id}),
  capabilities=(Capability(CHECKOUT),),
  spec_commit="abc123def456",
  removed=MappingProxyType({REMOVED_RULE.id: REMOVED_RULE}),
  versions=HISTORY_VERSIONS,
)
HISTORY_TESTS = (
  loaded("test_a", cites("REQ-SHOPPING-CHECKOUT-0001")),
  loaded(
    "test_b",
    SpecMarker(
      "spec_assert",
      rule_ids=(REMOVED_RULE.id,),
      gaps=((REMOVED_RULE.id, "one case only"),),
    ),
  ),
)


class VersionsTest(unittest.TestCase):
  """Tests for the view by release."""

  def setUp(self):
    """Build the report for the catalog with a history."""
    super().setUp()
    self.report = report.build(HISTORY_CATALOG, HISTORY_TESTS)

  def test_rows(self):
    """Test each version's counts, at the level that version states."""
    self.assertEqual(
      self.report.versions,
      (
        # Only the removed rule is a MUST then, and its test checks part.
        report.VersionRow("2026-01-11", 1, 0, 1),
        report.VersionRow("2026-04-08", 1, 1, 0),
        report.VersionRow("draft", 2, 1, 0),
      ),
    )
    draft = self.report.versions[-1]
    self.assertEqual(
      (draft.testable_must, draft.covered_must, draft.uncovered_must),
      (
        self.report.total.testable_must,
        self.report.total.covered_must,
        self.report.total.uncovered_must,
      ),
    )
    self.assertEqual(draft.must_coverage, 0.5)
    # The removed rule counts only in the view by release.
    self.assertNotIn(REMOVED_RULE.id, self.report.cited_by)
    self.assertNotIn(REMOVED_RULE, self.report.rules)

  def test_outputs(self):
    """Test the table in Markdown and HTML, and the fields in JSON."""
    markdown = report.to_markdown(self.report)
    self.assertIn(
      f"### By release\n\n{report.VERSIONS_INTRO}\n\n"
      "| Version | Testable MUST stated | MUST fully checked | MUST checked "
      "in part | MUST without a test | MUST coverage |\n"
      "| --- | ---: | ---: | ---: | ---: | ---: |\n"
      "| 2026-01-11 | 1 | 0 | 1 | 0 | 0% |\n"
      "| 2026-04-08 | 1 | 1 | 0 | 0 | 100% |\n"
      "| draft (abc123de) | 2 | 1 | 0 | 1 | 50% |\n",
      markdown,
    )
    self.assertIn("- **Releases:** The view by release counts", markdown)
    page = report.to_html(self.report)
    self.assertIn("<h3>By release</h3>", page)
    self.assertIn("<td>draft (abc123de)</td>", page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))
    data = report.to_json(self.report)
    self.assertEqual(
      [row["version"] for row in data["versions"]], list(HISTORY_VERSIONS)
    )
    by_id = {entry["id"]: entry for entry in data["rules"]}
    self.assertEqual(
      (
        by_id["REQ-SHOPPING-CHECKOUT-0001"]["since"],
        by_id["REQ-SHOPPING-CHECKOUT-0001"]["versions"],
      ),
      ("2026-01-11", list(HISTORY_VERSIONS)),
    )

  def test_no_history(self):
    """Test that a catalog without a history has no view by release."""
    plain = report.build(CATALOG, TESTS)
    self.assertEqual([row.version for row in plain.versions], ["draft"])
    self.assertNotIn("By release", report.to_markdown(plain))
    self.assertNotIn("By release", report.to_html(plain))


class ResultsTest(unittest.TestCase):
  """Tests for the report after a run, with each test's outcome."""

  def setUp(self):
    """Build the report for the small catalog, with outcomes."""
    super().setUp()
    self.report = report.build(CATALOG, RUN_TESTS, OUTCOMES, TARGET)

  def test_rule_results(self):
    """Test each rule's result, from its tests' outcomes."""
    self.assertEqual(
      {rule.id: self.report.result(rule.id) for rule in self.report.rules},
      {
        "REQ-SHOPPING-CART-0001": NOT_RUN,
        "REQ-SHOPPING-CATALOG-0001": FAILED,
        "REQ-SHOPPING-CHECKOUT-0001": FAILED,
        "REQ-SHOPPING-CHECKOUT-0002": PASSED,
        "REQ-SHOPPING-CHECKOUT-0003": PASSED,
        "REQ-SHOPPING-CHECKOUT-0004": NOT_RUN,
        "REQ-SHOPPING-OTHER-0001": report.NO_TEST,
      },
    )
    dry_run = report.build(CATALOG, RUN_TESTS)
    self.assertIsNone(dry_run.result("REQ-SHOPPING-CHECKOUT-0001"))
    self.assertIsNone(dry_run.test_counts())
    self.assertIsNone(report.results(dry_run, dry_run.rules))

  def test_outcomes_and_counts(self):
    """Test the shopping tests' outcomes, and the counts of results."""
    outcomes = self.report.outcomes
    self.assertEqual(len(outcomes), 8)
    self.assertNotIn("core.protocol_test.ExampleTest.test_core", outcomes)
    self.assertEqual(
      outcomes[full_id("test_f")],
      Outcome(NOT_RUN, "no outcome was recorded", OTHER_REASON),
    )
    self.assertEqual(
      self.report.test_counts(),
      {
        PASSED: 3,
        FAILED: 1,
        ERROR: 1,
        SKIPPED: 2,
        NOT_APPLICABLE: 0,
        NOT_RUN: 1,
      },
    )
    self.assertEqual(
      report.results(self.report, self.report.rules),
      {
        "passed": 2,
        "failed": 2,
        "error": 0,
        "not_run": 1,
        "not_applicable": 0,
        "must_passed": 1,
        "must_failed": 2,
        "must_error": 0,
        "must_not_run": 1,
        "must_not_applicable": 0,
      },
    )
    checkout = [r for r in self.report.rules if CHECKOUT in r.capabilities]
    self.assertEqual(
      report.results(self.report, checkout),
      {
        "passed": 2,
        "failed": 1,
        "error": 0,
        "not_run": 0,
        "not_applicable": 0,
        "must_passed": 1,
        "must_failed": 1,
        "must_error": 0,
        "must_not_run": 0,
        "must_not_applicable": 0,
      },
    )

  def test_json(self):
    """Test the run's fields in matrix.json."""
    data = report.to_json(self.report)
    self.assertEqual(
      data["run"],
      {
        "target": TARGET,
        "tests": {
          "passed": 3,
          "failed": 1,
          "error": 1,
          "skipped": 2,
          "not_applicable": 0,
          "not_run": 1,
        },
        "results": {
          "passed": 2,
          "failed": 2,
          "error": 0,
          "not_run": 1,
          "not_applicable": 0,
          "must_passed": 1,
          "must_failed": 2,
          "must_error": 0,
          "must_not_run": 1,
          "must_not_applicable": 0,
        },
        "not_run_by_category": {
          "all": {
            "no_trigger": 0,
            "environment": 0,
            "not_exercised": 0,
            "capability": 0,
            "other": 1,
          },
          "must": {
            "no_trigger": 0,
            "environment": 0,
            "not_exercised": 0,
            "capability": 0,
            "other": 1,
          },
        },
        "upgrade_list": [
          "REQ-SHOPPING-CATALOG-0001",
          "REQ-SHOPPING-CHECKOUT-0001",
        ],
      },
    )
    self.assertEqual(
      data["capabilities"][0]["not_run_by_category"]["must"],
      {
        "no_trigger": 0,
        "environment": 0,
        "not_exercised": 0,
        "capability": 0,
        "other": 1,
      },
    )
    self.assertEqual(
      data["capabilities"][0]["results"],
      {
        "passed": 0,
        "failed": 0,
        "error": 0,
        "not_run": 1,
        "not_applicable": 0,
        "must_passed": 0,
        "must_failed": 0,
        "must_error": 0,
        "must_not_run": 1,
        "must_not_applicable": 0,
      },
    )
    rules = {entry["id"]: entry["result"] for entry in data["rules"]}
    self.assertEqual(rules["REQ-SHOPPING-CHECKOUT-0001"], "failed")
    self.assertEqual(rules["REQ-SHOPPING-OTHER-0001"], "no_test")
    tests = {entry["id"]: entry["outcome"] for entry in data["tests"]}
    self.assertEqual(
      tests[full_id("test_b")],
      {
        "status": "failed",
        "detail": "total 5 | expected *6* <b>",
        "category": None,
      },
    )
    self.assertEqual(
      tests[full_id("test_g")],
      {
        "status": "skipped",
        "detail": "no cart capability",
        "category": "other",
      },
    )
    not_run = {
      entry["id"]: entry["not_run_category"] for entry in data["rules"]
    }
    self.assertEqual(not_run["REQ-SHOPPING-CART-0001"], "other")
    self.assertIsNone(not_run["REQ-SHOPPING-CHECKOUT-0001"])

  def test_markdown(self):
    """Test the results in the Markdown, with failure messages escaped."""
    text = report.to_markdown(self.report)
    for expected in (
      "the shopping tests' markers and a run against `http://localhost:8182`.",
      "**Of the 4 testable MUST and MUST NOT rules with a test, 1 passed, 2 "
      "failed and 1 wasn't run. The 1 MUST and MUST NOT rule that wasn't "
      "run, by reason: other reason 1.** Test outcomes: 3 passed, 1 failed, "
      "1 error, 2 skipped, 0 not applicable, 1 not run.",
      "## Results",
      "| cart | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 0 |",
      "| checkout | 3 | 2 | 1 | 0 | 0 | 0 | 2 | 1 | 1 | 0 | 0 | 0 |",
      "| **All rules, each counted once** | 5 | 2 | 2 | 0 | 1 | 0 | 4 | 1 "
      "| 2 | 0 | 1 | 0 |",
      "### Rules that failed: 2",
      "### Rules whose tests hit an error: 0",
      "`ExampleTest.test_b` **failed**: total 5 \\| expected \\*6\\* \\<b\\>",
      "### Rules whose tests didn't run: 1",
      "`ExampleTest.test_g` skipped: no cart capability",
      "Of those 2, 1 passed, 1 failed and 0 weren't run.",
      "| Rule | Level | Result | Text | Tests | Source |",
      "| CHECKOUT-0001 | MUST | **failed** | The business MUST do "
      "REQ-SHOPPING-CHECKOUT-0001. | `ExampleTest.test_a` passed<br>"
      "`ExampleTest.test_b` **failed** |",
      "| Test | File | Rules | Result |",
      "| `ExampleTest.test_d` | `shopping/checkout/example_test.py` | "
      "CHECKOUT-0005, CHECKOUT-0099 | **error**: KeyError: 'id' |",
      "| `ExampleTest.test_f` | `shopping/checkout/example_test.py` | "
      "not run: no outcome was recorded |",
      "- **Results:** A passing test shows",
    ):
      self.assertIn(expected, text)

  def test_html(self):
    """Test the results in the page, with failure messages escaped."""
    page = report.to_html(self.report)
    for expected in (
      '<div class="big">1 / 4</div>testable MUST and MUST NOT rules with a '
      "test passed: 2 failed, 1 not run",
      '<a href="#results">Results</a>',
      '<h2 id="results">Results</h2>',
      '<span class="stack"><span class="pass" style="width: 25%"></span>'
      '<span class="fail" style="width: 50%"></span>'
      '<span class="skip" style="width: 25%"></span></span>',
      '<td class="result"><span class="r-failed">failed</span></td>',
      '<span class="r-failed">failed</span>: total 5 | expected *6* &lt;b&gt;',
      '<span class="r-skipped">skipped</span>: no cart capability',
    ):
      self.assertIn(expected, page)
    for external in ("<script", "<link", " src="):
      self.assertNotIn(external, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_dry_run_has_no_results(self):
    """Test that a report without a run shows no results."""
    dry_run = report.build(CATALOG, RUN_TESTS)
    self.assertNotIn("## Results", report.to_markdown(dry_run))
    self.assertNotIn('href="#results"', report.to_html(dry_run))
    self.assertNotIn("  results", report.summary(dry_run, []))

  def test_summary(self):
    """Test the console line with the results."""
    text = report.summary(self.report, [Path("out/matrix.json")])
    self.assertIn(
      "  results against http://localhost:8182: 1 of the 4 testable MUST and "
      "MUST NOT rules with a test passed, 2 failed, 1 wasn't run\n",
      text,
    )


# A run against a server on 2026-04-08, with the catalog with a history:
# CHECKOUT-0001 is stated, CHECKOUT-0002 only the draft states.
NA_TESTS = (
  loaded("test_a", cites("REQ-SHOPPING-CHECKOUT-0001")),
  loaded("test_n", cites("REQ-SHOPPING-CHECKOUT-0002")),
  loaded(
    "test_both",
    cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CHECKOUT-0002"),
  ),
)
NA_OUTCOMES = {
  full_id("test_a"): Outcome(PASSED),
  full_id("test_n"): Outcome(
    NOT_APPLICABLE,
    "the server implements 2026-04-08, which doesn't state "
    "REQ-SHOPPING-CHECKOUT-0002 (not yet in a release)",
  ),
  full_id("test_both"): Outcome(PASSED),
}
SERVER_2026_04_08 = UcpProfile(
  {
    "ucp": {
      "version": "2026-04-08",
      "capabilities": [{"name": CHECKOUT, "version": "2026-04-08"}],
    }
  }
)


class NotApplicableTest(unittest.TestCase):
  """Tests for rules the version the server implements doesn't state."""

  def setUp(self):
    """Build the report for the run, with the server's profile."""
    super().setUp()
    self.report = report.build(
      HISTORY_CATALOG, NA_TESTS, NA_OUTCOMES, TARGET, SERVER_2026_04_08
    )

  def test_rule_results(self):
    """Test that an unstated rule is not applicable, whatever its tests did."""
    self.assertEqual(self.report.inapplicable, {"REQ-SHOPPING-CHECKOUT-0002"})
    self.assertEqual(self.report.result("REQ-SHOPPING-CHECKOUT-0001"), PASSED)
    # test_both passed, but the server's version doesn't state the rule.
    self.assertEqual(
      self.report.result("REQ-SHOPPING-CHECKOUT-0002"), NOT_APPLICABLE
    )
    self.assertEqual(
      report.results(self.report, self.report.rules),
      {
        "passed": 1,
        "failed": 0,
        "error": 0,
        "not_run": 0,
        "not_applicable": 1,
        "must_passed": 1,
        "must_failed": 0,
        "must_error": 0,
        "must_not_run": 0,
        "must_not_applicable": 1,
      },
    )
    self.assertEqual(
      self.report.test_counts(),
      {
        PASSED: 2,
        FAILED: 0,
        ERROR: 0,
        SKIPPED: 0,
        NOT_APPLICABLE: 1,
        NOT_RUN: 0,
      },
    )

  def test_without_the_profile(self):
    """Test that without the profile, not-applicable tests name the rules."""
    built = report.build(HISTORY_CATALOG, NA_TESTS, NA_OUTCOMES, TARGET)
    self.assertEqual(built.inapplicable, {"REQ-SHOPPING-CHECKOUT-0002"})
    # With only the test that also cites a stated rule, nothing says the
    # rule is unstated: the profile is what settles it.
    both_only = {full_id("test_both"): Outcome(PASSED)}
    built = report.build(HISTORY_CATALOG, NA_TESTS[2:], both_only, TARGET)
    self.assertEqual(built.result("REQ-SHOPPING-CHECKOUT-0002"), PASSED)
    built = report.build(
      HISTORY_CATALOG, NA_TESTS[2:], both_only, TARGET, SERVER_2026_04_08
    )
    self.assertEqual(built.result("REQ-SHOPPING-CHECKOUT-0002"), NOT_APPLICABLE)

  def test_summary(self):
    """Test the console line: unstated rules leave the denominator."""
    self.assertIn(
      "  results against http://localhost:8182: 1 of the 1 testable MUST and "
      "MUST NOT rules with a test passed, 0 failed, 0 weren't run, 1 doesn't "
      "apply to the version the server implements\n",
      report.summary(self.report, []),
    )

  def test_markdown(self):
    """Test the wording and the list in the Markdown."""
    text = report.to_markdown(self.report)
    for expected in (
      "A rule the version the server implements doesn't state is not "
      "applicable, and neither passes nor fails.",
      "**Of the 1 testable MUST and MUST NOT rules with a test, 1 passed, 0 "
      "failed and 0 weren't run. 1 more rule with a test doesn't apply to "
      "the version the server implements.** Test outcomes: 2 passed, 0 "
      "failed, 0 errors, 0 skipped, 1 not applicable, 0 not run.",
      "| checkout | 2 | 1 | 0 | 0 | 0 | 1 | 2 | 1 | 0 | 0 | 0 | 1 |",
      "### Rules the version the server implements doesn't state: 1",
      "| CHECKOUT-0002 | MUST NOT | The business MUST NOT do "
      "REQ-SHOPPING-CHECKOUT-0002. | `ExampleTest.test_both` passed<br>"
      "`ExampleTest.test_n` not applicable: the server implements "
      "2026-04-08, which doesn't state REQ-SHOPPING-CHECKOUT-0002 (not yet "
      "in a release) |",
      "| CHECKOUT-0002 | MUST NOT | not applicable | The business MUST NOT do "
      "REQ-SHOPPING-CHECKOUT-0002. | `ExampleTest.test_both` passed<br>"
      "`ExampleTest.test_n` not applicable |",
      "Of the 1 with a test, 1 passed, 0 failed and 0 weren't run. 1 more "
      "rule with a test doesn't apply to the version the server implements.",
      "neither counts for or against the server.",
    ):
      self.assertIn(expected, text)

  def test_html(self):
    """Test the card, the result and the bar in the page."""
    page = report.to_html(self.report)
    for expected in (
      '<div class="big">1 / 1</div>testable MUST and MUST NOT rules with a '
      "test passed: 0 failed, 0 not run, 1 more not applicable",
      '<span class="r-not_applicable">not applicable</span>',
      '<span class="pass" style="width: 50%"></span>'
      '<span class="na" style="width: 50%"></span>',
      "<h3>Rules the version the server implements doesn't state: 1</h3>",
    ):
      self.assertIn(expected, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_json(self):
    """Test the result and the counts in matrix.json."""
    data = report.to_json(self.report)
    self.assertEqual(data["format"], 5)
    rules = {entry["id"]: entry["result"] for entry in data["rules"]}
    self.assertEqual(rules["REQ-SHOPPING-CHECKOUT-0002"], "not_applicable")
    self.assertEqual(data["run"]["tests"]["not_applicable"], 1)
    self.assertEqual(data["run"]["results"]["must_not_applicable"], 1)


# A run exercising rule-level `error` vs `failed`:
# - CART-0001 has only an errored test -> `error`.
# - CATALOG-0001 has both a failed test and an errored test -> `failed`.
# - CHECKOUT-0001 has both a passing test and an errored test -> `error`.
# - CHECKOUT-0002 has a passing test -> `passed`.
# - CHECKOUT-0003 (SHOULD) has a skipped test -> `not_run`.
ERROR_TESTS = (
  loaded("test_err_only", cites("REQ-SHOPPING-CART-0001")),
  loaded("test_fail_and_err_1", cites("REQ-SHOPPING-CATALOG-0001")),
  loaded("test_fail_and_err_2", cites("REQ-SHOPPING-CATALOG-0001")),
  loaded(
    "test_pass_and_err_1",
    cites("REQ-SHOPPING-CHECKOUT-0001", "REQ-SHOPPING-CHECKOUT-0002"),
  ),
  loaded("test_pass_and_err_2", cites("REQ-SHOPPING-CHECKOUT-0001")),
  loaded("test_skip_should", cites("REQ-SHOPPING-CHECKOUT-0003")),
)
ERROR_OUTCOMES = {
  full_id("test_err_only"): Outcome(ERROR, "KeyError: 'buyer'"),
  full_id("test_fail_and_err_1"): Outcome(FAILED, "status 500"),
  full_id("test_fail_and_err_2"): Outcome(ERROR, "TimeoutError"),
  full_id("test_pass_and_err_1"): Outcome(PASSED),
  full_id("test_pass_and_err_2"): Outcome(ERROR, "ValueError: bad json"),
  full_id("test_skip_should"): Outcome(
    SKIPPED, "the server emits no allocations[]", "not_exercised"
  ),
}


class ErrorResultTest(unittest.TestCase):
  """Tests for rule-level `error` results, separate from `failed`."""

  def setUp(self):
    """Build the report for the run with errored tests."""
    super().setUp()
    self.report = report.build(CATALOG, ERROR_TESTS, ERROR_OUTCOMES, TARGET)

  def test_precedence_and_counts(self):
    """Test `FAILED` > `ERROR` > `PASSED` > `NOT_RUN`, and upgrade_list."""
    self.assertEqual(
      {rule.id: self.report.result(rule.id) for rule in self.report.rules},
      {
        "REQ-SHOPPING-CART-0001": ERROR,
        "REQ-SHOPPING-CATALOG-0001": FAILED,
        "REQ-SHOPPING-CHECKOUT-0001": ERROR,
        "REQ-SHOPPING-CHECKOUT-0002": PASSED,
        "REQ-SHOPPING-CHECKOUT-0003": NOT_RUN,
        "REQ-SHOPPING-CHECKOUT-0004": report.NO_TEST,
        "REQ-SHOPPING-OTHER-0001": report.NO_TEST,
      },
    )
    self.assertEqual(
      report.results(self.report, self.report.rules),
      {
        "passed": 1,
        "failed": 1,
        "error": 2,
        "not_run": 1,
        "not_applicable": 0,
        "must_passed": 1,
        "must_failed": 1,
        "must_error": 2,
        "must_not_run": 0,
        "must_not_applicable": 0,
      },
    )
    # Rules whose tests crashed without an assertion failure stay off the
    # server's upgrade list; they are listed in their own section instead.
    self.assertEqual(
      [rule.id for rule in report.upgrade_list(self.report)],
      ["REQ-SHOPPING-CATALOG-0001"],
    )

  def test_summary_markdown_html_and_json(self):
    """Test non-zero `must_error` rendering across all report formats."""
    self.assertIn(
      "  results against http://localhost:8182: 1 of the 4 testable MUST and "
      "MUST NOT rules with a test passed, 1 failed, 2 hit an error, "
      "0 weren't run\n",
      report.summary(self.report, []),
    )

    text = report.to_markdown(self.report)
    for expected in (
      "**Of the 4 testable MUST and MUST NOT rules with a test, 1 passed, 1 "
      "failed, 2 hit an error and 0 weren't run.**",
      "| checkout | 3 | 1 | 0 | 1 | 1 | 0 | 2 | 1 | 0 | 1 | 0 | 0 |",
      "### Rules whose tests hit an error: 2",
      "| CART-0001 | MUST | The business MUST do REQ-SHOPPING-CART-0001. | "
      "`ExampleTest.test_err_only` **error**: KeyError: 'buyer' |",
      "| CHECKOUT-0001 | MUST | **error** | The business MUST do "
      "REQ-SHOPPING-CHECKOUT-0001. | `ExampleTest.test_pass_and_err_1` "
      "passed<br>`ExampleTest.test_pass_and_err_2` **error** |",
      "Of those 2, 1 passed, 0 failed, 1 hit an error and 0 weren't run.",
      "a failure or error in a test that cites more than one rule applies to "
      "every rule it cites",
    ):
      self.assertIn(expected, text)

    page = report.to_html(self.report)
    for expected in (
      '<div class="big">1 / 4</div>testable MUST and MUST NOT rules with a '
      "test passed: 1 failed, 2 error, 0 not run",
      '<span class="stack"><span class="pass" style="width: 25%"></span>'
      '<span class="fail" style="width: 25%"></span>'
      '<span class="err" style="width: 50%"></span></span>',
      '<td class="result"><span class="r-error">error</span></td>',
      "<h3>Rules whose tests hit an error: 2</h3>",
    ):
      self.assertIn(expected, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

    data = report.to_json(self.report)
    self.assertEqual(data["format"], 5)
    self.assertEqual(data["run"]["results"]["error"], 2)
    self.assertEqual(data["run"]["results"]["must_error"], 2)
    rules = {entry["id"]: entry["result"] for entry in data["rules"]}
    self.assertEqual(rules["REQ-SHOPPING-CART-0001"], "error")
    self.assertEqual(rules["REQ-SHOPPING-CATALOG-0001"], "failed")
    self.assertEqual(rules["REQ-SHOPPING-CHECKOUT-0001"], "error")


# A run where every rule but one wasn't run, each for a different reason.
# CHECKOUT-0001's two tests give two reasons; a trigger wins. CHECKOUT-0002
# has one test skipped for the environment and one that never started.
NOT_RUN_TESTS = (
  loaded("test_t1", cites("REQ-SHOPPING-CHECKOUT-0001")),
  loaded("test_t2", cites("REQ-SHOPPING-CHECKOUT-0001")),
  loaded("test_e", cites("REQ-SHOPPING-CHECKOUT-0002")),
  loaded("test_m", cites("REQ-SHOPPING-CHECKOUT-0002")),
  loaded("test_x", cites("REQ-SHOPPING-CHECKOUT-0003")),
  loaded("test_c", cites("REQ-SHOPPING-CART-0001")),
  loaded("test_o", cites("REQ-SHOPPING-OTHER-0001")),
  loaded("test_f", cites("REQ-SHOPPING-CATALOG-0001")),
)
NOT_RUN_OUTCOMES = {
  full_id("test_t1"): Outcome(SKIPPED, "a known customer", "no_trigger"),
  full_id("test_t2"): Outcome(
    SKIPPED, "'dev.ucp.shopping.cart' (min_version: any)", "capability"
  ),
  full_id("test_e"): Outcome(
    SKIPPED, "the simulation secret was rejected", "environment"
  ),
  full_id("test_x"): Outcome(
    SKIPPED, "the server emits no discount allocations[]", "not_exercised"
  ),
  full_id("test_c"): Outcome(
    SKIPPED, "'dev.ucp.shopping.cart' (min_version: any)", "capability"
  ),
  full_id("test_o"): Outcome(SKIPPED, "not graded until the spec is fixed"),
  full_id("test_f"): Outcome(FAILED, "missing header"),
}


class NotRunTest(unittest.TestCase):
  """Tests for why rules weren't run, and for the upgrade list."""

  def setUp(self):
    """Build the report for the run."""
    super().setUp()
    self.report = report.build(CATALOG, NOT_RUN_TESTS, NOT_RUN_OUTCOMES, TARGET)

  def test_rule_categories(self):
    """Test each not-run rule's reason, with a trigger winning."""
    self.assertEqual(
      {
        rule.id: self.report.not_run_category(rule.id)
        for rule in self.report.rules
      },
      {
        "REQ-SHOPPING-CART-0001": "capability",
        "REQ-SHOPPING-CATALOG-0001": None,
        "REQ-SHOPPING-CHECKOUT-0001": "no_trigger",
        "REQ-SHOPPING-CHECKOUT-0002": "environment",
        "REQ-SHOPPING-CHECKOUT-0003": "not_exercised",
        "REQ-SHOPPING-CHECKOUT-0004": None,
        "REQ-SHOPPING-OTHER-0001": "other",
      },
    )
    # The test that never started reads as not run for another reason.
    self.assertEqual(
      self.report.outcomes[full_id("test_m")],
      Outcome(NOT_RUN, "no outcome was recorded", OTHER_REASON),
    )
    dry_run = report.build(CATALOG, NOT_RUN_TESTS)
    self.assertIsNone(dry_run.not_run_category("REQ-SHOPPING-CHECKOUT-0001"))
    self.assertIsNone(report.not_run_by_category(dry_run, dry_run.rules))
    self.assertEqual(report.upgrade_list(dry_run), [])

  def test_counts_and_upgrade_list(self):
    """Test the breakdown by reason and the rules to act on."""
    self.assertEqual(
      report.not_run_by_category(self.report, self.report.rules),
      {
        "all": {
          "no_trigger": 1,
          "environment": 1,
          "not_exercised": 1,
          "capability": 1,
          "other": 1,
        },
        "must": {
          "no_trigger": 1,
          "environment": 1,
          "not_exercised": 0,
          "capability": 1,
          "other": 0,
        },
      },
    )
    self.assertEqual(
      [rule.id for rule in report.upgrade_list(self.report)],
      ["REQ-SHOPPING-CATALOG-0001", "REQ-SHOPPING-CHECKOUT-0001"],
    )

  def test_summary(self):
    """Test the console lines with the breakdown and the upgrade list."""
    text = report.summary(self.report, [])
    self.assertIn(
      "  results against http://localhost:8182: 0 of the 4 testable MUST and "
      "MUST NOT rules with a test passed, 1 failed, 3 weren't run\n"
      "  the 3 MUST and MUST NOT rules that weren't run, by reason: no "
      "trigger declared 1, environment 1, capability not advertised 1\n"
      "  upgrade list: 2 rules (2 MUST or MUST NOT) failed or lack a trigger\n",
      text,
    )

  def test_markdown(self):
    """Test the headline, the upgrade list and the not-run breakdown."""
    text = report.to_markdown(self.report)
    for expected in (
      "**Of the 4 testable MUST and MUST NOT rules with a test, 0 passed, 1 "
      "failed and 3 weren't run. The 3 MUST and MUST NOT rules that weren't "
      "run, by reason: no trigger declared 1, environment 1, capability not "
      "advertised 1.**",
      "### Upgrade list: 2\n\nThe rules that failed, and the rules not run "
      "because the fixtures declare no trigger for their tests.\n\n"
      "| Rule | Level | Text | Tests | Source |",
      "### Rules whose tests didn't run: 5\n\nThe 5 rules that weren't run, "
      "by reason: no trigger declared 1, environment 1, not exercised 1, "
      "capability not advertised 1, other reason 1.\n",
      "`ExampleTest.test_t1` skipped (no trigger declared): a known customer"
      "<br>`ExampleTest.test_t2` skipped (capability not advertised): "
      "'dev.ucp.shopping.cart' (min\\_version: any)",
      "`ExampleTest.test_e` skipped (environment): the simulation secret was "
      "rejected<br>`ExampleTest.test_m` not run: no outcome was recorded",
      "`ExampleTest.test_o` skipped: not graded until the spec is fixed",
      "- **Skips:** A skipped test says why: no trigger declared",
    ):
      self.assertIn(expected, text)
    # The upgrade list holds the failed rule and the one lacking a trigger.
    upgrade = text.split("### Upgrade list: 2")[1].split("### Rules that")[0]
    self.assertIn("| CATALOG-0001 |", upgrade)
    self.assertIn("| CHECKOUT-0001 |", upgrade)
    self.assertNotIn("| CART-0001 |", upgrade)

  def test_html(self):
    """Test the labels and sections in the page."""
    page = report.to_html(self.report)
    for expected in (
      '<span class="r-skipped">skipped (no trigger declared)</span>: a known '
      "customer",
      '<span class="r-skipped">skipped</span>: not graded until the spec is '
      "fixed",
      "<h3>Upgrade list: 2</h3>",
      '<p class="note">The rules that failed, and the rules not run because '
      "the fixtures declare no trigger for their tests.</p>",
      '<p class="note">The 5 rules that weren\'t run, by reason: no trigger '
      "declared 1, environment 1, not exercised 1, capability not advertised "
      "1, other reason 1.</p>",
    ):
      self.assertIn(expected, page)
    balance = TagBalance()
    balance.feed(page)
    self.assertEqual((balance.open, balance.mismatched), ([], []))

  def test_json(self):
    """Test the categories, the breakdown and the list in matrix.json."""
    data = report.to_json(self.report)
    self.assertEqual(
      data["run"]["upgrade_list"],
      ["REQ-SHOPPING-CATALOG-0001", "REQ-SHOPPING-CHECKOUT-0001"],
    )
    self.assertEqual(
      data["run"]["not_run_by_category"]["must"],
      {
        "no_trigger": 1,
        "environment": 1,
        "not_exercised": 0,
        "capability": 1,
        "other": 0,
      },
    )
    rules = {entry["id"]: entry["not_run_category"] for entry in data["rules"]}
    self.assertEqual(rules["REQ-SHOPPING-CHECKOUT-0001"], "no_trigger")
    self.assertIsNone(rules["REQ-SHOPPING-CATALOG-0001"])
    tests = {
      entry["id"]: entry["outcome"]["category"] for entry in data["tests"]
    }
    self.assertEqual(tests[full_id("test_t1")], "no_trigger")
    self.assertEqual(tests[full_id("test_m")], "other")
    self.assertIsNone(tests[full_id("test_f")])


class RepositoryReportTest(unittest.TestCase):
  """Tests against this repository's catalog."""

  def test_counts_reconcile_with_the_index(self):
    """Test that the report's counts match capabilities.json."""
    built = report.build(load_catalog(), ())
    index = json.loads((DEFAULT_CATALOG_DIR / INDEX_FILE).read_text("utf-8"))
    expected = {
      entry["name"]: (
        entry["rules"],
        entry["testable_rules"],
        entry["testable_must"],
      )
      for entry in index["capabilities"]
    }
    self.assertEqual(
      {
        row.capability.name: (
          row.tally.rules,
          row.tally.testable,
          row.tally.testable_must,
        )
        for row in built.capabilities
      },
      expected,
    )
    self.assertEqual(
      (built.total.rules, built.total.testable, built.total.testable_must),
      (507, 211, 98),
    )
    self.assertEqual(built.not_rules, 3)
    self.assertTrue(built.spec_commit.startswith("f2626c28"))

  def test_generate(self):
    """Test that generate writes the three files and describes them."""
    with tempfile.TemporaryDirectory() as directory:
      text = report.generate((), directory)
      out = Path(directory)
      data = json.loads((out / report.JSON_FILE).read_text())
      markdown = (out / report.MARKDOWN_FILE).read_text()
      page = (out / report.HTML_FILE).read_text()
    self.assertIn("507 rules, 211 testable", text)
    self.assertIn("(matrix.json, matrix.md, matrix.html)", text)
    self.assertEqual(data["tests"], [])
    self.assertIn(f"# {report.TITLE}", markdown)
    self.assertIn('<div class="big">0 / 98</div>', page)

  def test_by_release(self):
    """Test the testable MUST rules each version states."""
    built = report.build(load_catalog(), ())
    self.assertEqual(
      [(row.version, row.testable_must) for row in built.versions],
      [
        ("2026-01-11", 18),
        ("2026-01-23", 19),
        ("2026-04-08", 60),
        ("2026-08-25", 96),
        ("draft", 98),
      ],
    )
    self.assertEqual(
      built.versions[-1].testable_must, built.total.testable_must
    )


class RunnerTest(unittest.TestCase):
  """Tests for the runner's --requirements_report option and its summary."""

  def run_main(self, *argv: str) -> tuple[int, str]:
    """Run the runner offline; return its exit code and its output."""
    out, err = io.StringIO(), io.StringIO()
    with (
      mock.patch.object(
        runner, "fetch_server_profile", side_effect=OSError("offline")
      ),
      contextlib.redirect_stdout(out),
      contextlib.redirect_stderr(err),
    ):
      code = runner.main(list(argv))
    return code, out.getvalue() + err.getvalue()

  def test_dry_run_writes_the_report(self):
    """Test that a dry run writes the report for the selected tests."""
    with tempfile.TemporaryDirectory() as directory:
      code, output = self.run_main(
        "--suite=shopping.checkout.totals_test",
        "--dry-run",
        f"--requirements_report={directory}",
      )
      out = Path(directory)
      data = json.loads((out / report.JSON_FILE).read_text())
      written = sorted(path.name for path in out.iterdir())
    self.assertEqual(code, 0, output)
    self.assertIn("Requirements report: ", output)
    self.assertEqual(written, ["matrix.html", "matrix.json", "matrix.md"])
    self.assertEqual(len(data["tests"]), 5)
    self.assertEqual(data["markers"]["unmarked"], 0)

  def test_non_writable_directory_stops_the_run(self):
    """Test that a report that cannot be written fails the run."""
    with tempfile.NamedTemporaryFile() as not_a_directory:
      code, output = self.run_main(
        "--suite=shopping.checkout.totals_test",
        "--dry-run",
        f"--requirements_report={not_a_directory.name}",
      )
    self.assertEqual(code, 1)
    self.assertIn("Requirements report failed:", output)

  def test_run_writes_the_results(self):
    """Test that a run writes each test's outcome and each rule's result."""
    with (
      tempfile.TemporaryDirectory() as directory,
      mock.patch.object(
        runner, "_load_tests", return_value=(fake_shopping_suite(), [])
      ),
    ):
      code, output = self.run_main(
        "--server_url=http://127.0.0.1:9",
        f"--requirements_report={directory}",
      )
      data = json.loads((Path(directory) / report.JSON_FILE).read_text())
    self.assertEqual(code, 1, output)
    self.assertIn("CONFORMANCE TEST SUMMARY", output)
    self.assertIn("results against http://127.0.0.1:9: ", output)
    self.assertEqual(data["run"]["target"], "http://127.0.0.1:9")
    self.assertEqual(
      data["run"]["tests"],
      {
        "passed": 1,
        "failed": 1,
        "error": 0,
        "skipped": 1,
        "not_applicable": 0,
        "not_run": 0,
      },
    )
    self.assertEqual(
      {
        entry["id"]: entry["result"]
        for entry in data["rules"]
        if entry["tests"]
      },
      {
        "REQ-SHOPPING-CHECKOUT-0010": "passed",
        "REQ-SHOPPING-CHECKOUT-0011": "failed",
        "REQ-SHOPPING-CHECKOUT-0105": "not_run",
      },
    )
    outcomes = {entry["id"]: entry["outcome"] for entry in data["tests"]}
    self.assertEqual(
      outcomes["shopping.fake_test.FakeTest.test_fail"],
      {
        "status": "failed",
        "detail": "total was 5, expected 6",
        "category": None,
      },
    )
    self.assertEqual(
      outcomes["shopping.fake_test.FakeTest.test_skip"],
      {
        "status": "skipped",
        "detail": "no second discount code configured",
        "category": "other",
      },
    )
    # The failed rule is on the upgrade list; the one skipped without a
    # category isn't.
    self.assertEqual(
      data["run"]["upgrade_list"], ["REQ-SHOPPING-CHECKOUT-0011"]
    )
    self.assertIn("Skipped: 1\n  by reason: other reason 1\n", output)
    self.assertIn("upgrade list: 1 rules (1 MUST or MUST NOT)", output)

  def test_non_writable_directory_stops_a_run_before_the_tests(self):
    """Test that a run checks the report can be written before it starts."""
    with (
      tempfile.NamedTemporaryFile() as not_a_directory,
      mock.patch.object(
        runner, "_load_tests", return_value=(fake_shopping_suite(), [])
      ),
    ):
      code, output = self.run_main(
        f"--requirements_report={not_a_directory.name}"
      )
    self.assertEqual(code, 1)
    self.assertIn("Requirements report failed:", output)
    self.assertNotIn("CONFORMANCE TEST SUMMARY", output)

  def test_summary_does_not_count_skipped_tests_as_passed(self):
    """Test the summary's counts for a pass, a failure and a skip."""
    with mock.patch.object(
      runner, "_load_tests", return_value=(fake_shopping_suite(), [])
    ):
      code, output = self.run_main("--server_url=http://127.0.0.1:9")
    self.assertEqual(code, 1, output)
    summary = output[output.index("CONFORMANCE TEST SUMMARY") :]
    for line in (
      "Total Executed: 3\n",
      "Passed: 1\n",
      "Skipped: 1\n",
      "Failures: 1\n",
      "Errors: 0\n",
    ):
      self.assertIn(line, summary)


if __name__ == "__main__":
  unittest.main()
