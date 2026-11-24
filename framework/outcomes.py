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

"""Record per-test outcomes, skip categories, and HTTP failure context.

The runner records each test's outcome so that the run summary and the
requirements coverage report share one per-test count and can say, for each
rule, whether the tests that cite it passed. A test passes, fails an
assertion, stops with an error, is skipped, doesn't apply to the server's
version, or never starts (for example when its class's `setUpClass` fails).

When a test uses `subTest`, unittest calls `addSubTest` for each subtest
and still calls `addSuccess` if the test finishes. One test is recorded
once: the first failure or error wins over passes and skips, and the first
failure or error's message is kept.
"""

from collections.abc import Iterable
import dataclasses
import re
import unittest

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"
NOT_RUN = "not_run"
# Every status, in the order the report lists them.
STATUSES = (PASSED, FAILED, ERROR, SKIPPED, NOT_APPLICABLE, NOT_RUN)
# How a skip reason begins when the test doesn't apply to the server's version.
NOT_APPLICABLE_PREFIX = "not applicable:"
# Why a test was skipped, or didn't run. The fixtures declare no trigger for
# what the test needs; the run's own settings or surroundings stop it; the
# server's responses gave it nothing to check; the server doesn't advertise a
# capability it requires; or something else, such as a check on hold for a
# specification fix, or a fixture that failed before the test started.
NO_TRIGGER = "no_trigger"
ENVIRONMENT = "environment"
NOT_EXERCISED = "not_exercised"
CAPABILITY = "capability"
OTHER = "other"
# Every category, in the order the report lists them.
SKIP_CATEGORIES = (NO_TRIGGER, ENVIRONMENT, NOT_EXERCISED, CAPABILITY, OTHER)
# How a skip reason begins for each category that has a prefix.
SKIP_PREFIXES = {
  NO_TRIGGER: "no trigger declared:",
  ENVIRONMENT: "environment:",
  NOT_EXERCISED: "not exercised:",
  CAPABILITY: "capability not advertised:",
}
# How the runner and the report name each category.
SKIP_LABELS = {
  NO_TRIGGER: "no trigger declared",
  ENVIRONMENT: "environment",
  NOT_EXERCISED: "not exercised",
  CAPABILITY: "capability not advertised",
  OTHER: "other reason",
}
# The longest detail kept, in characters.
MAX_DETAIL = 200
# How unittest names a class or module fixture that failed or skipped, as in
# "setUpClass (shopping.checkout.example_test.ExampleTest)".
_FIXTURE = re.compile(r"(\w+) \((.+)\)")


def is_not_applicable(reason: str) -> bool:
  """Return whether a skip reason says the test doesn't apply to the server."""
  return reason.startswith(NOT_APPLICABLE_PREFIX)


def skip_reason(category: str, text: str) -> str:
  """Return a skip reason that starts with a category's prefix.

  Args:
    category: One of `SKIP_PREFIXES`' keys.
    text: What the test needed, or what stopped it.

  Returns:
    The reason, such as "no trigger declared: a known customer".

  """
  return f"{SKIP_PREFIXES[category]} {text}"


def skip_category(reason: str) -> tuple[str, str]:
  """Return a skip reason's category, and its text without the prefix.

  Args:
    reason: The reason a test was skipped with.

  Returns:
    One of `SKIP_CATEGORIES`, and the reason's text after the category's
    prefix: the whole reason when it has none, which makes it `OTHER`.

  """
  for category, prefix in SKIP_PREFIXES.items():
    if reason.startswith(prefix):
      return category, reason.removeprefix(prefix).strip()
  return OTHER, reason


@dataclasses.dataclass(frozen=True)
class Outcome:
  """What happened to one test.

  Attributes:
    status: One of `STATUSES`.
    detail: Why the test was skipped or didn't run, or its failure or error
      message, on one line and shortened.
    category: For a skipped test or one that didn't run, why, as one of
      `SKIP_CATEGORIES`; empty for other outcomes, and for a skip recorded
      without one, which counts as `OTHER`.

  """

  status: str
  detail: str = ""
  category: str = ""


class RecordingResult(unittest.TextTestResult):
  """A text test result that records per-test outcomes and HTTP context.

  The `add...` methods keep unittest's names, print what
  `unittest.TextTestResult` prints, attach the test's last HTTP exchange to
  failures and errors, and record one consolidated outcome per test ID so
  tests with multiple failing subtests are counted once in the summary.

  Attributes:
    outcomes: The outcome of each test that started, by test ID.
    fixtures: For each class or module whose fixture failed or skipped, by
      its dotted name, the outcome its tests take.
    fixture_errors: Count of class- or module-level fixture errors (such as a
      failing `setUpClass` or `tearDownClass`).

  """

  def __init__(self, *args, **kwargs):
    """Start with no outcomes; the arguments are TextTestResult's."""
    super().__init__(*args, **kwargs)
    self.outcomes: dict[str, Outcome] = {}
    self.fixtures: dict[str, Outcome] = {}
    self.fixture_errors: int = 0

  def addSuccess(self, test):  # noqa: N802
    """Record a pass."""
    super().addSuccess(test)
    self._record(test, Outcome(PASSED))

  def addFailure(self, test, err):  # noqa: N802
    """Record a failed assertion, with the test's last HTTP exchange."""
    super().addFailure(test, err)
    self._attach_exchange(test, self.failures)
    self._record(test, Outcome(FAILED, _message(err)))

  def addError(self, test, err):  # noqa: N802
    """Record an error, with the test's last HTTP exchange."""
    super().addError(test, err)
    self._attach_exchange(test, self.errors)
    self._record(test, Outcome(ERROR, _message(err, with_type=True)))

  def addSkip(self, test, reason):  # noqa: N802
    """Record a skip with its category; a not-applicable one on its own."""
    super().addSkip(test, reason)
    if is_not_applicable(reason):
      detail = reason.removeprefix(NOT_APPLICABLE_PREFIX)
      self._record(test, Outcome(NOT_APPLICABLE, _shorten(detail)))
    else:
      category, detail = skip_category(reason)
      self._record(test, Outcome(SKIPPED, _shorten(detail), category))

  def addExpectedFailure(self, test, err):  # noqa: N802
    """Record an expected failure as a skip."""
    super().addExpectedFailure(test, err)
    detail = _shorten(f"expected failure: {_message(err)}")
    self._record(test, Outcome(SKIPPED, detail, OTHER))

  def addUnexpectedSuccess(self, test):  # noqa: N802
    """Record an unexpected success as a failure, matching wasSuccessful()."""
    super().addUnexpectedSuccess(test)
    self._record(test, Outcome(FAILED, "unexpected success"))

  def addSubTest(self, test, subtest, err):  # noqa: N802
    """Record a failing subtest as its test's failure or error."""
    super().addSubTest(test, subtest, err)
    if err is None:
      return
    where = subtest.id().removeprefix(test.id()).strip()
    if issubclass(err[0], test.failureException):
      self._attach_exchange(test, self.failures)
      outcome = Outcome(FAILED, _shorten(f"{where}: {_message(err)}"))
    else:
      self._attach_exchange(test, self.errors)
      detail = _message(err, with_type=True)
      outcome = Outcome(ERROR, _shorten(f"{where}: {detail}"))
    self._record(test, outcome)

  @staticmethod
  def _attach_exchange(test: object, entries: list) -> None:
    """Append the test's last HTTP exchange to the entry just recorded.

    `entries` is unittest's `failures` or `errors` list of
    `(test, formatted traceback)` pairs; the text is what `printErrors`
    shows under FAIL: or ERROR:. A test without `describe_last_exchange`
    (a fixture holder, a unit test) is left alone.
    """
    describe = getattr(test, "describe_last_exchange", None)
    if not callable(describe) or not entries:
      return
    exchange = describe()
    if not exchange:
      return
    holder, formatted = entries[-1]
    entries[-1] = (holder, f"{formatted}\nLast HTTP exchange:\n{exchange}\n")

  def outcomes_for(self, test_ids: Iterable[str]) -> dict[str, Outcome]:
    """Return the outcome of each of the given tests.

    A test that never started didn't run. If its class's or module's fixture
    failed, the fixture's error is the reason; if the fixture skipped, the
    test was skipped too.

    Args:
      test_ids: The IDs of the tests the run selected.

    Returns:
      Each test's outcome, by ID.

    """
    return {
      test_id: self.outcomes.get(test_id) or self._not_started(test_id)
      for test_id in test_ids
    }

  def _not_started(self, test_id: str) -> Outcome:
    """Return the outcome of a test that never started."""
    class_name = test_id.rpartition(".")[0]
    module = class_name.rpartition(".")[0]
    for parent in (class_name, module):
      if parent in self.fixtures:
        return self.fixtures[parent]
    return Outcome(NOT_RUN, "no outcome was recorded", OTHER)

  def _record(self, test: object, outcome: Outcome) -> None:
    """Keep a test's first outcome, unless a failure or error follows."""
    if not isinstance(test, unittest.TestCase):
      if outcome.status in (FAILED, ERROR):
        self.fixture_errors += 1
      self._record_fixture(test, outcome)
      return
    parent = getattr(test, "test_case", None)
    if isinstance(parent, unittest.TestCase):
      # A subtest's skip counts for the test it belongs to.
      test = parent
    test_id = test.id()
    earlier = self.outcomes.get(test_id)
    if earlier is None or (
      earlier.status in (PASSED, SKIPPED, NOT_APPLICABLE)
      and outcome.status in (FAILED, ERROR)
    ):
      self.outcomes[test_id] = outcome

  def _record_fixture(self, holder: object, outcome: Outcome) -> None:
    """Note a class or module fixture's error or skip, for its tests."""
    describe = getattr(holder, "id", None)
    match = _FIXTURE.fullmatch(describe()) if callable(describe) else None
    if not match:
      return
    fixture, parent = match.groups()
    if outcome.status not in (SKIPPED, NOT_APPLICABLE):
      outcome = Outcome(
        NOT_RUN, _shorten(f"{fixture} failed: {outcome.detail}"), OTHER
      )
    self.fixtures.setdefault(parent, outcome)


def _message(err: tuple, with_type: bool = False) -> str:
  """Return an exception's message on one line, shortened.

  Args:
    err: The exception, as `sys.exc_info()` gives it.
    with_type: Whether to start with the exception's type.

  Returns:
    The message.

  """
  exc_type, value = err[0], err[1]
  text = " ".join(str(value).split())
  if with_type:
    text = f"{exc_type.__name__}: {text}" if text else exc_type.__name__
  return _shorten(text)


def _shorten(text: str) -> str:
  """Return text on one line, cut to `MAX_DETAIL` characters."""
  text = " ".join(text.split())
  if len(text) <= MAX_DETAIL:
    return text
  return text[: MAX_DETAIL - 1].rstrip() + "…"
