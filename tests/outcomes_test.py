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

"""Unit tests for recording test outcomes in framework.outcomes.

Each test builds its example test cases inside a function, so that neither
pytest nor unittest collects them on their own.
"""

import io
import unittest

from framework import outcomes
from framework.outcomes import (
  CAPABILITY,
  ENVIRONMENT,
  ERROR,
  FAILED,
  MAX_DETAIL,
  NO_TRIGGER,
  NOT_APPLICABLE,
  NOT_APPLICABLE_PREFIX,
  NOT_EXERCISED,
  NOT_RUN,
  OTHER,
  PASSED,
  SKIP_CATEGORIES,
  SKIP_LABELS,
  SKIP_PREFIXES,
  SKIPPED,
  Outcome,
  RecordingResult,
  skip_category,
  skip_reason,
)


def run(*cases: type[unittest.TestCase]) -> RecordingResult:
  """Run test case classes quietly; return the recording result."""
  suite = unittest.TestSuite(
    unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases
  )
  runner = unittest.TextTestRunner(
    stream=io.StringIO(), resultclass=RecordingResult
  )
  return runner.run(suite)


def name(case: type[unittest.TestCase], method: str) -> str:
  """Return the ID unittest gives a test."""
  return f"{case.__module__}.{case.__qualname__}.{method}"


class RecordingResultTest(unittest.TestCase):
  """Tests for RecordingResult."""

  def test_each_outcome(self):
    """Test passes, failures, errors and skips, with their details."""

    class Example(unittest.TestCase):
      def test_pass(self):
        """Pass."""

      def test_fail(self):
        """Fail an assertion with a message over several lines."""
        self.assertEqual(404, 200, "\n\ncheckout not created\nbody: {}")

      def test_error(self):
        """Raise an error."""
        raise KeyError("totals")

      def test_bare_error(self):
        """Raise an error with no message."""
        raise RuntimeError

      def test_skip(self):
        """Skip."""
        self.skipTest("no second discount code configured")

      @unittest.expectedFailure
      def test_expected_failure(self):
        """Fail as expected."""
        self.fail("known gap")

      @unittest.expectedFailure
      def test_unexpected_success(self):
        """Pass unexpectedly."""

      def test_not_applicable(self):
        """Skip because the server's version doesn't state the rule."""
        self.skipTest(
          f"{NOT_APPLICABLE_PREFIX} the server implements 2026-04-08, which "
          "doesn't state REQ-SHOPPING-CHECKOUT-0016 (introduced in 2026-08-25)"
        )

    result = run(Example)
    self.assertEqual(
      result.outcomes,
      {
        name(Example, "test_pass"): Outcome(PASSED),
        name(Example, "test_fail"): Outcome(
          FAILED, "404 != 200 : checkout not created body: {}"
        ),
        name(Example, "test_error"): Outcome(ERROR, "KeyError: 'totals'"),
        name(Example, "test_bare_error"): Outcome(ERROR, "RuntimeError"),
        name(Example, "test_skip"): Outcome(
          SKIPPED, "no second discount code configured", OTHER
        ),
        name(Example, "test_expected_failure"): Outcome(
          SKIPPED, "expected failure: known gap", OTHER
        ),
        name(Example, "test_unexpected_success"): Outcome(
          FAILED, "unexpected success"
        ),
        name(Example, "test_not_applicable"): Outcome(
          NOT_APPLICABLE,
          "the server implements 2026-04-08, which doesn't state "
          "REQ-SHOPPING-CHECKOUT-0016 (introduced in 2026-08-25)",
        ),
      },
    )
    self.assertEqual((len(result.failures), len(result.errors)), (1, 2))
    # unittest counts a not-applicable test among the skipped.
    self.assertEqual(len(result.skipped), 2)
    self.assertEqual(result.testsRun, 8)

  def test_subtests(self):
    """Test that a failing or skipped subtest counts for its test."""

    class Example(unittest.TestCase):
      def test_fails_once(self):
        """Fail the second of three subtests."""
        for value in range(3):
          with self.subTest(value=value):
            self.assertNotEqual(value, 1)

      def test_errs_then_fails(self):
        """Raise in one subtest, then fail in another."""
        with self.subTest("first"):
          raise ValueError("bad total")
        with self.subTest("second"):
          self.fail("never kept")

      def test_skips_one(self):
        """Skip one subtest and pass the other."""
        with self.subTest("skipped"):
          self.skipTest("no fixture")
        with self.subTest("passed"):
          pass

    result = run(Example)
    self.assertEqual(
      result.outcomes[name(Example, "test_fails_once")],
      Outcome(FAILED, "(value=1): 1 == 1"),
    )
    self.assertEqual(
      result.outcomes[name(Example, "test_errs_then_fails")],
      Outcome(ERROR, "[first]: ValueError: bad total"),
    )
    self.assertEqual(
      result.outcomes[name(Example, "test_skips_one")],
      Outcome(SKIPPED, "no fixture", OTHER),
    )

  def test_class_fixtures(self):
    """Test the tests of a class whose setUpClass fails or skips."""

    class Broken(unittest.TestCase):
      @classmethod
      def setUpClass(cls):
        """Fail before any test runs."""
        raise ConnectionError("server unreachable")

      def test_one(self):
        """Never run."""

    class Skipped(unittest.TestCase):
      @classmethod
      def setUpClass(cls):
        """Skip the whole class."""
        raise unittest.SkipTest("no order capability")

      def test_one(self):
        """Never run."""

    result = run(Broken, Skipped)
    self.assertEqual(result.outcomes, {})
    found = result.outcomes_for(
      [name(Broken, "test_one"), name(Skipped, "test_one"), "other.Test.test"]
    )
    self.assertEqual(
      list(found.values()),
      [
        Outcome(
          NOT_RUN,
          "setUpClass failed: ConnectionError: server unreachable",
          OTHER,
        ),
        Outcome(SKIPPED, "no order capability", OTHER),
        Outcome(NOT_RUN, "no outcome was recorded", OTHER),
      ],
    )

  def test_outcomes_for_keeps_recorded_outcomes(self):
    """Test that outcomes_for returns what the run recorded, in order."""

    class Example(unittest.TestCase):
      def test_pass(self):
        """Pass."""

    result = run(Example)
    found = result.outcomes_for(
      ["missing.Test.test", name(Example, "test_pass")]
    )
    self.assertEqual(
      list(found.items()),
      [
        (
          "missing.Test.test",
          Outcome(NOT_RUN, "no outcome was recorded", OTHER),
        ),
        (name(Example, "test_pass"), Outcome(PASSED)),
      ],
    )

  def test_long_details_are_shortened(self):
    """Test that a detail is cut to MAX_DETAIL characters."""

    class Example(unittest.TestCase):
      def test_long(self):
        """Fail with a long message."""
        self.fail("x" * 500)

    detail = run(Example).outcomes[name(Example, "test_long")].detail
    self.assertEqual(len(detail), MAX_DETAIL)
    self.assertTrue(detail.endswith("…"))
    self.assertEqual(outcomes.STATUSES[-1], NOT_RUN)

  def test_skip_categories(self):
    """Test that a skip's category comes from the start of its reason."""

    class Example(unittest.TestCase):
      def test_no_trigger(self):
        """Skip for a trigger the fixtures don't declare."""
        self.skipTest(skip_reason(NO_TRIGGER, "a known customer"))

      def test_environment(self):
        """Skip for the run's surroundings."""
        self.skipTest(skip_reason(ENVIRONMENT, "the server is remote"))

      def test_not_exercised(self):
        """Skip for a response that carried nothing to check."""
        self.skipTest(skip_reason(NOT_EXERCISED, "no allocations[]"))

      def test_capability(self):
        """Skip for a capability the server doesn't advertise."""
        self.skipTest(skip_reason(CAPABILITY, "'dev.ucp.shopping.cart'"))

      def test_other(self):
        """Skip with a reason that has no category."""
        self.skipTest("not graded until the spec is self-consistent")

    result = run(Example)
    self.assertEqual(
      {
        method: result.outcomes[name(Example, method)]
        for method in (
          "test_no_trigger",
          "test_environment",
          "test_not_exercised",
          "test_capability",
          "test_other",
        )
      },
      {
        "test_no_trigger": Outcome(SKIPPED, "a known customer", NO_TRIGGER),
        "test_environment": Outcome(
          SKIPPED, "the server is remote", ENVIRONMENT
        ),
        "test_not_exercised": Outcome(
          SKIPPED, "no allocations[]", NOT_EXERCISED
        ),
        "test_capability": Outcome(
          SKIPPED, "'dev.ucp.shopping.cart'", CAPABILITY
        ),
        "test_other": Outcome(
          SKIPPED, "not graded until the spec is self-consistent", OTHER
        ),
      },
    )


class LastExchangeTest(unittest.TestCase):
  """A failure or error ends with the test's last HTTP exchange."""

  def test_attached_to_failures_errors_and_subtests(self):
    """The exchange follows the traceback in failures and errors."""

    class Case(unittest.TestCase):
      def describe_last_exchange(self):
        return '> POST /x\n< 200 for POST /x\n{"status": "incomplete"}'

      def test_fails(self):
        self.fail("no order")

      def test_errors(self):
        raise KeyError("order")

      def test_subtest_fails(self):
        with self.subTest(part=1):
          self.fail("part one")

    result = run(Case)

    self.assertEqual(len(result.failures), 2)
    self.assertEqual(len(result.errors), 1)
    for _, formatted in result.failures + result.errors:
      self.assertIn("Last HTTP exchange:", formatted)
      self.assertIn('{"status": "incomplete"}', formatted)
    # The traceback itself is still there, before the exchange.
    _, failure_text = result.failures[0]
    self.assertLess(
      failure_text.index("AssertionError"),
      failure_text.index("Last HTTP exchange:"),
    )

  def test_left_alone_without_an_exchange(self):
    """No method, or nothing recorded, leaves the traceback as it is."""

    class Plain(unittest.TestCase):
      def test_fails(self):
        self.fail("plain")

    class Empty(unittest.TestCase):
      def describe_last_exchange(self):
        return ""

      def test_fails(self):
        self.fail("empty")

    result = run(Plain, Empty)

    self.assertEqual(len(result.failures), 2)
    for _, formatted in result.failures:
      self.assertNotIn("Last HTTP exchange:", formatted)


class SkipReasonTest(unittest.TestCase):
  """Tests for building and reading categorized skip reasons."""

  def test_round_trip(self):
    """Test that every prefixed category is read back."""
    for category in SKIP_PREFIXES:
      with self.subTest(category=category):
        reason = skip_reason(category, "what the test needed")
        self.assertEqual(
          skip_category(reason), (category, "what the test needed")
        )

  def test_wording(self):
    """Test the prefixes, which the tests' skip messages start with."""
    self.assertEqual(
      skip_reason(NO_TRIGGER, "a known customer"),
      "no trigger declared: a known customer",
    )
    self.assertEqual(
      skip_reason(CAPABILITY, "'dev.ucp.shopping.cart' (min_version: any)"),
      "capability not advertised: 'dev.ucp.shopping.cart' (min_version: any)",
    )

  def test_other(self):
    """Test that a reason with no prefix is OTHER, kept whole."""
    self.assertEqual(skip_category("no fixture"), (OTHER, "no fixture"))
    self.assertEqual(skip_category(""), (OTHER, ""))

  def test_every_category_has_a_label(self):
    """Test that the categories, the labels and the prefixes agree."""
    self.assertEqual(set(SKIP_LABELS), set(SKIP_CATEGORIES))
    self.assertEqual(set(SKIP_PREFIXES), set(SKIP_CATEGORIES) - {OTHER})


if __name__ == "__main__":
  unittest.main()
