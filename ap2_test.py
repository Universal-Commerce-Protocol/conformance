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

"""Tests for AP2 Mandate in UCP SDK Server."""

from absl.testing import absltest
import httpx
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class Ap2MandateTest(integration_test_utils.IntegrationTestBase):
  """Tests for AP2 Mandate.

  Every request here uses the mock agent profile that declares the AP2
  Mandates extension. The tests skip unless AP2 is in the capability
  intersection of that profile and the Business profile, computed with the
  UCP intersection algorithm.

  Validated Paths:
  - POST /checkout-sessions/{id}/complete
  """

  def setUp(self) -> None:
    """Skip unless AP2 is in the negotiated capability intersection."""
    super().setUp()
    version = self.conformance_config.get(
      "ucp_version", integration_test_utils.DEFAULT_UCP_VERSION
    )
    ap2 = integration_test_utils.ap2_release(version)
    if ap2 is None:
      self.skipTest(f"No AP2 Mandates definition for UCP release {version}")
    discovery = self.client.get("/.well-known/ucp")
    self.assert_response_status(discovery, 200)
    platform = httpx.get(
      f"http://localhost:{integration_test_utils.FLAGS.mock_agent_port}"
      f"{integration_test_utils.AgentProfileServer.AP2_PROFILE_PATH}"
    )
    self.assert_response_status(platform, 200)
    active = integration_test_utils.capability_intersection(
      discovery.json(), platform.json()
    )
    if ap2["capability"] not in active:
      self.skipTest(
        f"{ap2['capability']} is not in the capability intersection, so "
        "AP2 is not negotiated"
      )

  def get_headers(
    self, idempotency_key: str | None = None, request_id: str | None = None
  ) -> dict[str, str]:
    """Use the agent profile that declares the AP2 Mandates extension."""
    return integration_test_utils.get_headers(
      idempotency_key,
      request_id,
      profile_path=integration_test_utils.AgentProfileServer.AP2_PROFILE_PATH,
    )

  def _payment_instrument(self) -> dict:
    return {
      "id": "instr_1",
      "handler_id": "mock_payment_handler",
      "type": "card",
      "display": {
        "brand": "visa",
        "last_digits": "4242",
      },
      "credential": {"type": "token", "token": "success_token"},
    }

  def test_ap2_mandate_completion(self) -> None:
    """Test successful checkout completion with a valid AP2 mandate.

    Needs a signed SD-JWT+kb checkout mandate fixture, which the suite does
    not have yet. With AP2 negotiated, a Business that verifies the mandate
    must reject the placeholder mandate this test used to send.
    """
    self.skipTest(
      "Needs a signed SD-JWT+kb checkout mandate fixture. With AP2 "
      "negotiated, a Business that verifies the mandate must reject the "
      "placeholder mandate this test used to send."
    )

  def test_complete_without_mandate_is_rejected(self) -> None:
    """Test that completion without a checkout mandate is refused.

    Given AP2 is negotiated and the checkout is ready to complete,
    When a completion request omits `ap2.checkout_mandate`,
    Then the checkout must not complete, and the refusal must carry the
    `mandate_required` error code.

    The spec does not pin whether this is a protocol error or a business
    outcome, so either form is accepted, each in its own shape: HTTP 200
    requires the UCP error envelope with a `messages` entry, and any other
    status requires a top-level `code`.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json={
        "payment": {"instruments": [self._payment_instrument()]},
        "risk_signals": {},
      },
      headers=self.get_headers(),
    )

    body = response.json() if response.content else {}
    self.assertNotEqual(
      body.get("status"),
      "completed",
      msg=f"Checkout completed without an AP2 mandate. Resp: {response.text}",
    )
    if response.status_code == 200:
      # Business outcome: UCP envelope with an error message.
      self.assertEqual(
        body.get("ucp", {}).get("status"),
        "error",
        msg=f"Expected a UCP error envelope. Resp: {response.text}",
      )
      codes = {
        m.get("code")
        for m in body.get("messages", [])
        if isinstance(m, dict) and m.get("type") == "error"
      }
      self.assertIn(
        "mandate_required",
        codes,
        msg=f"Expected error message 'mandate_required'. Resp: {response.text}",
      )
    else:
      # Protocol error: non-200 with a top-level code.
      self.assertEqual(
        body.get("code"),
        "mandate_required",
        msg=f"Expected protocol error mandate_required. Resp: {response.text}",
      )


if __name__ == "__main__":
  absltest.main()
