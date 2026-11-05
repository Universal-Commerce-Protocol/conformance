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

"""Unit tests for the marker check in framework.marker_check."""

import dataclasses
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest

from framework import marker_check
from framework.decorators import (
  SpecMarker,
  requires_capability,
  spec_assert,
)
from framework.marker_check import LoadedTest
from framework.requirements import Catalog, Rule, Statement

CART = "dev.ucp.shopping.cart"
CHECKOUT = "dev.ucp.shopping.checkout"
DISCOUNT = "dev.ucp.shopping.discount"
ORDER = "dev.ucp.shopping.order"


def rule(
  rule_id: str, capability: str, testable: bool = True, is_rule: bool = True
) -> Rule:
  """Return a rule of one capability."""
  return Rule(
    id=rule_id,
    level="MUST",
    clause="The business MUST do it.",
    capabilities=(capability,),
    source_file="docs/specification/shopping/checkout/index.md",
    line=1,
    link="specification/shopping/checkout/#a",
    testable=testable,
    testable_reason="aimed at the business" if testable else "MCP page",
    is_rule=is_rule,
  )


CATALOG = Catalog(
  rules=MappingProxyType(
    {
      "REQ-SHOPPING-CHECKOUT-0001": rule(
        "REQ-SHOPPING-CHECKOUT-0001", CHECKOUT
      ),
      "REQ-SHOPPING-CHECKOUT-0002": rule(
        "REQ-SHOPPING-CHECKOUT-0002", CHECKOUT, testable=False
      ),
      "REQ-SHOPPING-CHECKOUT-0003": rule(
        "REQ-SHOPPING-CHECKOUT-0003", CHECKOUT, is_rule=False
      ),
      "REQ-SHOPPING-CART-0001": rule("REQ-SHOPPING-CART-0001", CART),
    }
  ),
  retired=frozenset({"REQ-SHOPPING-CHECKOUT-0004"}),
  extends=MappingProxyType({DISCOUNT: (CART, CHECKOUT)}),
)


def loaded(
  name: str,
  marker: SpecMarker | None,
  capabilities: frozenset[str] = frozenset({CHECKOUT}),
  module: str = "shopping.checkout.example_test",
) -> LoadedTest:
  """Return a loaded test."""
  return LoadedTest(
    f"{module}.ExampleTest.{name}", module, marker, capabilities
  )


def cites(*rule_ids: str) -> SpecMarker:
  """Return a spec_assert marker."""
  return SpecMarker("spec_assert", rule_ids=rule_ids)


class CheckTest(unittest.TestCase):
  """Tests for check."""

  def test_counts_markers_and_cited_rules(self):
    """Test the counts, the rules cited, and that other suites are skipped."""
    result = marker_check.check(
      [
        loaded("test_cites", cites("REQ-SHOPPING-CHECKOUT-0001")),
        loaded("test_no_rule", SpecMarker("no_rule", reason="r")),
        loaded("test_scenario", SpecMarker("scenario", reason="r")),
        loaded("test_core_rule", SpecMarker("outside_catalog", reason="r")),
        loaded(
          "test_old",
          SpecMarker("older_version", reason="r", version="2026-04-08"),
        ),
        loaded("test_unmarked", None),
        loaded("test_core", None, module="core.protocol_test"),
      ],
      CATALOG,
    )
    self.assertEqual(result.checked, 6)
    self.assertEqual(
      dict(result.kinds),
      {
        "spec_assert": 1,
        "no_rule": 1,
        "scenario": 1,
        "outside_catalog": 1,
        "older_version": 1,
        "unmarked": 1,
      },
    )
    self.assertEqual(result.rules, {"REQ-SHOPPING-CHECKOUT-0001"})
    self.assertEqual(len(result.problems), 1)
    self.assertIn("test_unmarked: no marker", result.problems[0])
    self.assertEqual(result.warnings, [])
    self.assertIn(
      "1 cite rules (1 distinct rules), 1 no_rule, 1 scenario, "
      "1 outside_catalog, 1 older_version, 1 unmarked",
      result.report(),
    )

  def test_problems_for_ids_a_test_must_not_cite(self):
    """Test that unknown, retired and not-a-rule IDs are problems."""
    result = marker_check.check(
      [
        loaded(
          "test_cites",
          cites(
            "REQ-SHOPPING-CHECKOUT-0099",
            "REQ-SHOPPING-CHECKOUT-0004",
            "REQ-SHOPPING-CHECKOUT-0003",
          ),
        )
      ],
      CATALOG,
    )
    self.assertEqual(
      [problem.split(": ", 1)[1] for problem in result.problems],
      [
        "REQ-SHOPPING-CHECKOUT-0099 is not in the catalog",
        "REQ-SHOPPING-CHECKOUT-0004 is retired: its rule was removed or "
        "rewritten",
        "REQ-SHOPPING-CHECKOUT-0003 is a sentence the review found states no "
        "requirement",
      ],
    )
    self.assertEqual(result.rules, set())

  def test_warnings(self):
    """Test the warnings for untestable rules and other capabilities."""
    result = marker_check.check(
      [
        loaded("test_untestable", cites("REQ-SHOPPING-CHECKOUT-0002")),
        loaded(
          "test_other_capability",
          cites("REQ-SHOPPING-CART-0001"),
          capabilities=frozenset({ORDER}),
        ),
        # A discount test exercises checkout, which discount extends.
        loaded(
          "test_parent",
          cites("REQ-SHOPPING-CHECKOUT-0001"),
          capabilities=frozenset({DISCOUNT}),
        ),
        # A test that requires no capability is not compared.
        loaded(
          "test_no_capability",
          cites("REQ-SHOPPING-CART-0001"),
          capabilities=frozenset(),
        ),
      ],
      CATALOG,
    )
    self.assertEqual(result.problems, [])
    self.assertEqual(len(result.warnings), 2)
    self.assertIn(
      "test_untestable: REQ-SHOPPING-CHECKOUT-0002 is not", result.warnings[0]
    )
    self.assertIn(
      f"test_other_capability: REQ-SHOPPING-CART-0001 belongs to {CART}",
      result.warnings[1],
    )

  def test_removed_rule(self):
    """Test that a removed rule may be cited, with a warning."""
    removed = dataclasses.replace(
      rule("REQ-SHOPPING-CHECKOUT-0005", CHECKOUT),
      since="2026-01-11",
      until="2026-04-08",
      statements=(
        Statement("2026-01-11", "MUST", "The business MUST do it.", "a.md", 1),
        Statement("2026-01-23", "MUST", "The business MUST do it.", "a.md", 2),
      ),
    )
    catalog = dataclasses.replace(
      CATALOG,
      retired=CATALOG.retired | {removed.id},
      removed=MappingProxyType({removed.id: removed}),
    )
    result = marker_check.check(
      [
        loaded(
          "test_old",
          cites("REQ-SHOPPING-CHECKOUT-0005", "REQ-SHOPPING-CHECKOUT-0004"),
        )
      ],
      catalog,
    )
    self.assertEqual(result.rules, {"REQ-SHOPPING-CHECKOUT-0005"})
    self.assertEqual(
      result.warnings,
      [
        f"{loaded('test_old', None).test_id}: REQ-SHOPPING-CHECKOUT-0005 was "
        "removed in 2026-04-08; only 2026-01-11, 2026-01-23 state it"
      ],
    )
    # A retired ID without a history still names nothing to cite.
    self.assertEqual(len(result.problems), 1)
    self.assertIn("REQ-SHOPPING-CHECKOUT-0004 is retired", result.problems[0])


class EvaluateTest(unittest.TestCase):
  """Tests for evaluate, against this repository's catalog."""

  def test_modes(self):
    """Test that only strict mode stops the run, and off checks nothing."""
    tests = [loaded("test_unmarked", None)]
    report, stop = marker_check.evaluate(tests, "strict")
    self.assertTrue(stop)
    self.assertIn("1 unmarked", report)
    self.assertEqual(marker_check.evaluate(tests, "warn"), (report, False))
    self.assertEqual(marker_check.evaluate(tests, "off"), ("", False))

  def test_nothing_to_check(self):
    """Test that tests outside the shopping suite need no catalog."""
    tests = [loaded("test_core", None, module="core.protocol_test")]
    self.assertEqual(
      marker_check.evaluate(tests, "strict", catalog_dir="/nonexistent"),
      ("", False),
    )

  def test_missing_catalog(self):
    """Test that a missing catalog stops a strict run only."""
    tests = [loaded("test_cites", cites("REQ-SHOPPING-CHECKOUT-0001"))]
    with tempfile.TemporaryDirectory() as empty:
      report, stop = marker_check.evaluate(tests, "strict", catalog_dir=empty)
      self.assertTrue(stop)
      self.assertIn("no catalog", report)
      self.assertFalse(
        marker_check.evaluate(tests, "warn", catalog_dir=empty)[1]
      )


class DescribeTest(unittest.TestCase):
  """Tests for describing tests from unittest and from pytest."""

  def test_from_unittest(self):
    """Test the marker and capabilities read from a loaded test case."""

    @requires_capability(CHECKOUT)
    class Example(unittest.TestCase):
      @requires_capability(DISCOUNT)
      @spec_assert("REQ-SHOPPING-CHECKOUT-0001")
      def test_one(self):
        """Do nothing."""

    (test,) = unittest.defaultTestLoader.loadTestsFromTestCase(Example)
    described = marker_check.from_unittest(test)
    self.assertEqual(described.module, __name__)
    self.assertEqual(described.marker, cites("REQ-SHOPPING-CHECKOUT-0001"))
    self.assertEqual(described.capabilities, {CHECKOUT, DISCOUNT})

  def test_from_pytest_item(self):
    """Test the fields read from a collected pytest item."""

    @spec_assert("REQ-SHOPPING-CHECKOUT-0001")
    def test_one():
      pass

    item = SimpleNamespace(
      nodeid="shopping/checkout/x_test.py::X::test_one",
      module=SimpleNamespace(__name__="shopping.checkout.x_test"),
      cls=SimpleNamespace(_required_capabilities={(CHECKOUT, None)}),
      obj=test_one,
    )
    self.assertEqual(
      marker_check.from_pytest_item(item),
      LoadedTest(
        "shopping/checkout/x_test.py::X::test_one",
        "shopping.checkout.x_test",
        cites("REQ-SHOPPING-CHECKOUT-0001"),
        frozenset({CHECKOUT}),
      ),
    )


if __name__ == "__main__":
  unittest.main()
