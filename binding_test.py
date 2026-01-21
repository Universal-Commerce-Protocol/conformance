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

"""Tests for Token Binding in UCP SDK Server."""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import binding
from ucp_sdk.models.schemas.shopping.types import card_payment_instrument
from ucp_sdk.models.schemas.shopping.types import payment_identity
from ucp_sdk.models.schemas.shopping.types import payment_instrument
from ucp_sdk.models.schemas.shopping.types import token_credential_resp


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class TokenBindingTest(integration_test_utils.IntegrationTestBase):
  """Tests for Token Binding.

  Validated Paths:
  - POST /checkout-sessions/{id}/complete
  """

  def test_token_binding_completion(self) -> None:
    """Test successful checkout completion with bound token.

    Given a ready-to-complete checkout session,
    When a completion request is made using a token with binding data,
    Then the request should succeed with status 200.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    identity = payment_identity.PaymentIdentity(
      access_token="user_access_token"
    )
    token_binding = binding.Binding(checkout_id=checkout_id, identity=identity)

    # TokenCredentialResponse allows extra fields
    credential = token_credential_resp.TokenCredentialResponse(
      type="stripe_token", token="success_token", binding=token_binding
    )

    instr = payment_instrument.PaymentInstrument(
      root=card_payment_instrument.CardPaymentInstrument(
        id="instr_1",
        brand="visa",
        last_digits="4242",
        handler_id="mock_payment_handler",
        handler_name="mock_payment_handler",
        type="card",
        credential=credential,
      )
    )
    payment_data = instr.root.model_dump(mode="json", exclude_none=True)
    payment_payload = {
      "payment_data": payment_data,
      "risk_signals": {},
    }

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 200)
    self.assertEqual(
      response.json().get("status"),
      "completed",
      msg="Checkout status not 'completed'",
    )


if __name__ == "__main__":
  absltest.main()
