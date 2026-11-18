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

"""Tests for the access controls on the order simulation endpoint.

`/testing/simulate-shipping/{order_id}` is the hook a merchant exposes so the
suite can advance an order; it is not part of the UCP specification, which is
why these tests live with the shopping order suite rather than in `core/`.
"""

from absl.testing import absltest
from framework.base_test import FLAGS
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase


@requires_capability("dev.ucp.shopping.order")
class SecurityTest(ShoppingIntegrationTestBase):
  """Tests for security controls."""

  def test_simulation_endpoint_missing_header(self):
    """Test access without the secret header returns 403."""
    order_id = self.create_completed_order()
    response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=self.get_headers(),  # Standard headers only
    )
    self.assert_response_status(response, 403)

  def test_simulation_endpoint_incorrect_secret(self):
    """Test access with an incorrect secret returns 403."""
    order_id = self.create_completed_order()
    headers = self.get_headers()
    headers["Simulation-Secret"] = "for-sure-incorrect-secret"
    response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    self.assert_response_status(response, 403)

  def test_simulation_endpoint_correct_secret(self):
    """Test access with the correct secret returns 200."""
    order_id = self.create_completed_order()
    headers = self.get_headers()
    headers["Simulation-Secret"] = FLAGS.simulation_secret
    response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    self.assert_response_status(response, 200)


if __name__ == "__main__":
  absltest.main()
