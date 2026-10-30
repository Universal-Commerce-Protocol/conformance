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

    Accept `mandate_required` in error messages on HTTP 200 or 4xx, or as
    a top-level code on 4xx. A 200 response without a checkout resource
    must carry a UCP error envelope. Completion must be neither accepted
    for processing nor completed.
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
    self.assertIsInstance(
      body, dict, msg=f"Expected a JSON object. Resp: {response.text}"
    )
    self.assertNotIn(
      body.get("status"),
      ("complete_in_progress", "completed"),
      msg=(
        "Checkout accepted completion without an AP2 mandate. "
        f"Resp: {response.text}"
      ),
    )
    self.assertTrue(
      response.status_code == 200 or 400 <= response.status_code < 500,
      msg=(
        "Expected HTTP 200 or 4xx for a missing mandate, got "
        f"{response.status_code}. Resp: {response.text}"
      ),
    )
    # mandate_required may arrive as an error message on 200 or 4xx, or as
    # a top-level code on 4xx. A resourceless 200 must be a UCP error
    # envelope.
    messages = body.get("messages")
    if not isinstance(messages, list):
      messages = []
    has_message_error = any(
      isinstance(m, dict)
      and m.get("type") == "error"
      and m.get("code") == "mandate_required"
      for m in messages
    )
    has_top_level_code = (
      400 <= response.status_code < 500
      and body.get("code") == "mandate_required"
    )
    if response.status_code == 200:
      if "id" in body:
        self.assertIsInstance(
          body["id"], str, msg=f"Expected a checkout id. Resp: {response.text}"
        )
        self.assertIn(
          body.get("status"),
          (
            "incomplete",
            "requires_escalation",
            "ready_for_complete",
            "canceled",
          ),
          msg=f"Expected a non-completing status. Resp: {response.text}",
        )
      else:
        ucp = body.get("ucp")
        self.assertIsInstance(
          ucp, dict, msg=f"Expected a UCP error envelope. Resp: {response.text}"
        )
        self.assertEqual(
          ucp.get("status"),
          "error",
          msg=f"Expected a UCP error envelope. Resp: {response.text}",
        )
    self.assertTrue(
      has_message_error or has_top_level_code,
      msg=(
        "Expected error code 'mandate_required' in messages or body. "
        f"Resp: {response.text}"
      ),
    )


if __name__ == "__main__":
  absltest.main()
