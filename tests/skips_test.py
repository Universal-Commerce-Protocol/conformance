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

"""Unit tests for the categorized skips of the base test class.

The example tests are built inside the test methods, so nothing collects
them on their own, and no client or agent server is started.
"""

import io
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework.base_test import BaseIntegrationTest
from framework.outcomes import (
  ENVIRONMENT,
  NO_TRIGGER,
  NOT_EXERCISED,
  SKIPPED,
  Outcome,
  RecordingResult,
)


def run(case: type[unittest.TestCase], **flags) -> dict[str, Outcome]:
  """Run a base-test subclass with the given flags; return outcomes by name.

  The server profile is left unset, so no capability or version check
  applies, and the agent server isn't started.
  """
  suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
  with (
    flagsaver.flagsaver(**flags),
    mock.patch.dict("os.environ", {"SERVER_URL": ""}),
    mock.patch.object(BaseIntegrationTest, "server_profile", None),
    mock.patch.object(BaseIntegrationTest, "_start_agent_server"),
  ):
    runner = unittest.TextTestRunner(
      stream=io.StringIO(), resultclass=RecordingResult
    )
    result = runner.run(suite)
  prefix = f"{case.__module__}.{case.__qualname__}."
  return {
    test_id.removeprefix(prefix): outcome
    for test_id, outcome in result.outcomes.items()
  }


class SkipHelperTest(unittest.TestCase):
  """Tests for skip_no_trigger, skip_environment and skip_not_exercised."""

  def test_each_helper_sets_its_category(self):
    """Test the category and detail each helper records."""

    class Example(BaseIntegrationTest):
      def test_no_trigger(self):
        """Skip for a trigger the fixtures don't declare."""
        self.skip_no_trigger("a known customer with two stored addresses")

      def test_environment(self):
        """Skip for the run's surroundings."""
        self.skip_environment("the simulation secret was rejected")

      def test_not_exercised(self):
        """Skip for a response that carried nothing to check."""
        self.skip_not_exercised("the server emits no discount allocations[]")

    self.assertEqual(
      run(Example, server_url="http://127.0.0.1:9"),
      {
        "test_no_trigger": Outcome(
          SKIPPED, "a known customer with two stored addresses", NO_TRIGGER
        ),
        "test_environment": Outcome(
          SKIPPED, "the simulation secret was rejected", ENVIRONMENT
        ),
        "test_not_exercised": Outcome(
          SKIPPED, "the server emits no discount allocations[]", NOT_EXERCISED
        ),
      },
    )

  def test_missing_server_url_is_the_environment(self):
    """Test that a run with no server URL skips for the environment."""

    class Example(BaseIntegrationTest):
      def test_one(self):
        """Never reached."""

    outcome = run(Example, server_url=None)["test_one"]
    self.assertEqual(outcome.status, SKIPPED)
    self.assertEqual(outcome.category, ENVIRONMENT)
    self.assertIn("--server_url", outcome.detail)

  def test_unreachable_webhooks_are_the_environment(self):
    """Test that a remote server without --webhook_base_url skips so."""

    class Example(BaseIntegrationTest):
      def test_one(self):
        """Skip unless the server can reach the webhook server."""
        self.skip_unless_webhooks_reachable()

    outcome = run(Example, server_url="https://shop.example")["test_one"]
    self.assertEqual(outcome.status, SKIPPED)
    self.assertEqual(outcome.category, ENVIRONMENT)
    self.assertIn("--webhook_base_url", outcome.detail)


if __name__ == "__main__":
  unittest.main()
