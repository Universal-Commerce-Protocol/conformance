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
  """Tests for security controls.

  The hook is the sample server's own. A business without it has nothing
  to show here, so each test skips when the hook is absent (404 or 405 to
  the configured secret) or when the configured secret is rejected (403),
  instead of counting three failures against a store that never had the
  hook.
  """

  def _simulate_shipping(self, order_id: str, headers: dict) -> object:
    return self.client.post(
      f"/testing/simulate-shipping/{order_id}", headers=headers
    )

  def _with_secret(self, secret: str) -> dict:
    headers = self.get_headers()
    headers["Simulation-Secret"] = secret
    return headers

  def _skip_without_hook(self, order_id: str) -> None:
    """Skip when the hook is absent or the configured secret is rejected.

    Probes with the configured secret; a 200 also advances the order, so
    the tests that expect a refusal call this only after their own
    request came back as if the hook did not exist.
    """
    probe = self._simulate_shipping(
      order_id, self._with_secret(FLAGS.simulation_secret)
    )
    if probe.status_code in (404, 405):
      self.skipTest(
        "server exposes no webhook simulation path "
        f"(/testing/simulate-shipping -> {probe.status_code}); "
        "cannot exercise simulation security controls"
      )
    if probe.status_code == 403:
      self.skipTest(
        "simulation secret rejected (/testing/simulate-shipping -> 403); "
        "configure --simulation_secret to match the server under test"
      )

  def _assert_refused(self, order_id: str, response) -> None:
    """Assert the hook refused a request without the right secret."""
    if response.status_code in (404, 405):
      # Either no hook at all, or a hook that reveals nothing to an
      # unauthenticated caller; the probe tells which.
      self._skip_without_hook(order_id)
      return
    self.assert_response_status(response, 403)

  def test_simulation_endpoint_missing_header(self):
    """Test access without the secret header is refused (403)."""
    order_id = self.create_completed_order()
    response = self._simulate_shipping(
      order_id,
      self.get_headers(),  # Standard headers only
    )
    self._assert_refused(order_id, response)

  def test_simulation_endpoint_incorrect_secret(self):
    """Test access with an incorrect secret is refused (403)."""
    order_id = self.create_completed_order()
    response = self._simulate_shipping(
      order_id, self._with_secret("for-sure-incorrect-secret")
    )
    self._assert_refused(order_id, response)

  def test_simulation_endpoint_correct_secret(self):
    """Test access with the correct secret returns 200."""
    order_id = self.create_completed_order()
    response = self._simulate_shipping(
      order_id, self._with_secret(FLAGS.simulation_secret)
    )
    if response.status_code in (403, 404, 405):
      self._skip_without_hook(order_id)
    self.assert_response_status(response, 200)


if __name__ == "__main__":
  absltest.main()
