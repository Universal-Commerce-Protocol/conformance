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

"""Record per-test outcomes and attach HTTP exchanges to failure reports."""

import dataclasses
import unittest

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"

# The longest detail kept, in characters.
MAX_DETAIL = 200


@dataclasses.dataclass(frozen=True)
class Outcome:
  """What happened to one test.

  Attributes:
    status: One of `PASSED`, `FAILED`, `ERROR`, or `SKIPPED`.
    detail: Why the test was skipped, or its failure or error message, on one
      line and shortened.

  """

  status: str
  detail: str = ""


class RecordingResult(unittest.TextTestResult):
  """A text test result that records per-test outcomes and HTTP context.

  The `add...` methods keep unittest's names, print what
  `unittest.TextTestResult` prints, attach the test's last HTTP exchange to
  failures and errors, and record one consolidated outcome per test ID so
  tests with multiple failing subtests are counted once in the summary.

  Attributes:
    outcomes: The outcome of each test that started, by test ID.
    fixture_errors: Count of class- or module-level fixture errors (such as a
      failing `setUpClass` or `tearDownClass`).

  """

  def __init__(self, *args, **kwargs):
    """Start with no outcomes; the arguments are TextTestResult's."""
    super().__init__(*args, **kwargs)
    self.outcomes: dict[str, Outcome] = {}
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
    """Record a skip."""
    super().addSkip(test, reason)
    self._record(test, Outcome(SKIPPED, _shorten(reason)))

  def addExpectedFailure(self, test, err):  # noqa: N802
    """Record an expected failure as a skip."""
    super().addExpectedFailure(test, err)
    self._record(test, Outcome(SKIPPED, f"expected failure: {_message(err)}"))

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

  def _record(self, test: object, outcome: Outcome) -> None:
    """Keep a test's first outcome, unless a failure or error follows."""
    if not isinstance(test, unittest.TestCase):
      if outcome.status in (FAILED, ERROR):
        self.fixture_errors += 1
      return
    parent = getattr(test, "test_case", None)
    if isinstance(parent, unittest.TestCase):
      test = parent
    test_id = test.id()
    earlier = self.outcomes.get(test_id)
    if earlier is None or (
      earlier.status in (PASSED, SKIPPED) and outcome.status in (FAILED, ERROR)
    ):
      self.outcomes[test_id] = outcome


def _message(err: tuple, with_type: bool = False) -> str:
  """Return an exception's message on one line, shortened."""
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
