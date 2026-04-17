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
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout
from ucp_sdk.models.schemas.shopping.ap2_mandate import Ap2WithCheckoutMandate
from ucp_sdk.models.schemas.shopping.ap2_mandate import CheckoutMandate
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)
from ucp_sdk.models.schemas.shopping.types import card_payment_instrument
from ucp_sdk.models.schemas.shopping.types import payment_instrument
from ucp_sdk.models.schemas.shopping.types import token_credential as token_credential_resp


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class Ap2MandateTest(integration_test_utils.IntegrationTestBase):
  """Tests for AP2 Mandate.

  Validated Paths:
  - POST /checkout-sessions/{id}/complete
  """

  def test_ap2_mandate_completion(self) -> None:
    """Test successful checkout completion with AP2 mandate.

    Given a ready-to-complete checkout session,
    When a completion request is made including ap2 extension data,
    Then the request should succeed with status 200.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    # SD-JWT+kb pattern:
    # ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(~[A-Za-z0-9_-]+)*$
    mandate = CheckoutMandate(root="header.payload.signature~kb_signature")
    ap2_data = Ap2WithCheckoutMandate(checkout_mandate=mandate)

    # Use the standard valid payment payload and add AP2 data
    payment_payload = integration_test_utils.get_valid_payment_payload()
    payment_payload["ap2"] = ap2_data.model_dump(
      mode="json", exclude_none=True
    )

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
