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

"""Tests for simulation URL security and authorization headers."""

from absl import flags
from absl.testing import absltest
from framework.base_test import BaseIntegrationTest

FLAGS = flags.FLAGS


class SecurityTest(BaseIntegrationTest):
  """Tests for simulation endpoint security controls."""

  def test_simulation_endpoint_missing_header(self):
    """Test access without the secret header returns 403."""
    response = self.client.post(
      "/testing/simulate-shipping/test-order-security",
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 403)

  def test_simulation_endpoint_incorrect_secret(self):
    """Test access with an incorrect secret returns 403."""
    headers = self.get_headers()
    headers["Simulation-Secret"] = "for-sure-incorrect-secret"
    response = self.client.post(
      "/testing/simulate-shipping/test-order-security",
      headers=headers,
    )
    self.assert_response_status(response, 403)

  def test_simulation_endpoint_correct_secret(self):
    """Test access with the correct secret bypasses 403 security block."""
    headers = self.get_headers()
    headers["Simulation-Secret"] = FLAGS.simulation_secret
    response = self.client.post(
      "/testing/simulate-shipping/test-order-security",
      headers=headers,
    )
    # Valid secret bypasses security check (returns 404 for test ID or 200)
    self.assertIn(response.status_code, [200, 404])


if __name__ == "__main__":
  absltest.main()
