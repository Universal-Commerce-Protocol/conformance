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

"""Business Logic tests for the UCP SDK Server."""

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import buyer_consent as buyer_consent
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout

try:
  from ucp_sdk.models.schemas.shopping import payment_update_request
except ImportError:
  from ucp_sdk.models.schemas.common.types import payment_update_request

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )
from ucp_sdk.models.schemas.shopping.types import buyer_update_request
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


@requires_capability("dev.ucp.shopping.checkout")
class BusinessLogicTest(ShoppingIntegrationTestBase):
  """Tests for business logic and calculations.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - GET /checkout-sessions/{id}
  """

  def test_totals_calculation_on_create(self):
    """Test that totals are calculated correctly upon checkout creation.

    Given a request to create a checkout session with a specific item,
    When the checkout is created with an incorrect title/price in the request,
    Then the server should return a checkout where line item totals, subtotal,
    and grand total correctly reflect the database price, ignoring client
    input.
    """
    # Get expected item details from config
    default_item = (
      self.conformance_config.get("items", [{}])[0]
      if self.conformance_config
      else {}
    )
    expected_price = int(default_item.get("price", 3500))

    # Create checkout (client cannot send title/price per schema). The server
    # should use the authoritative price from its DB (which matches our config).
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Verify Line Item Calculations
    line_item = checkout_obj.line_items[0]
    li_subtotal = next(
      (t.amount for t in line_item.totals if t.type == "subtotal"), 0
    )
    li_total = next(
      (t.amount for t in line_item.totals if t.type == "total"), 0
    )

    self.assertEqual(
      li_subtotal,
      expected_price,
      f"Line item subtotal should match DB price {expected_price}",
    )
    self.assertEqual(
      li_total,
      expected_price,
      f"Line item total should match DB price {expected_price}",
    )

    # Verify Totals Breakdown
    subtotal = next(
      (t for t in checkout_obj.totals if t.type == "subtotal"), None
    )
    total_obj = next(
      (t for t in checkout_obj.totals if t.type == "total"), None
    )

    self.assertIsNotNone(subtotal, "Subtotal missing")
    self.assertEqual(
      subtotal.amount,
      expected_price,
      f"Subtotal amount should match DB price {expected_price}",
    )

    self.assertIsNotNone(total_obj, "Total missing")
    self.assertEqual(
      total_obj.amount,
      expected_price,
      f"Total amount should match DB price {expected_price}",
    )

  def test_totals_recalculation_on_update(self):
    """Test that totals are recalculated correctly upon checkout update.

    Given an existing checkout session with 1 item,
    When the line item quantity is updated to 2,
    Then the server should return the updated checkout with a total amount of
    2 * price.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Get expected price from config
    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    # Update quantity to 2. Total should be 2 * expected_price.
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
    )
    payment_update = payment_update_request.PaymentUpdateRequest(
      instruments=checkout_obj.payment.instruments,
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    updated_checkout = checkout.Checkout(**response.json())
    total_obj = next(
      (t for t in updated_checkout.totals if t.type == "total"), None
    )
    expected_total = expected_price * 2
    self.assertEqual(
      total_obj.amount,
      expected_total,
      msg=(
        "Server did not correct totals on update. Expected"
        f" {expected_total}, got {total_obj.amount}"
      ),
    )

  def test_buyer_consent(self):
    """Test that buyer consent preferences are persisted on creation.

    Given a checkout creation payload including buyer consent preferences
    (marketing=True, analytics=False),
    When the checkout session is created,
    Then the returned checkout object should correctly reflect these consent
    values.
    """
    create_payload = self.create_checkout_payload()

    # Add consent info
    consent_dict = {
      "marketing": True,
      "analytics": False,
      "sale_of_data": False,
    }

    create_payload_dict = create_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    create_payload_dict["buyer"] = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
      "consent": consent_dict,
    }

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload_dict,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 201)
    checkout_id = checkout.Checkout(**response.json()).id

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")

    # buyer is types.buyer.Buyer, consent is in extra fields
    consent_data = getattr(checkout_obj.buyer, "consent", None)
    self.assertTrue(consent_data, "Consent info missing")

    if isinstance(consent_data, dict):
      self.assertTrue(
        consent_data.get("marketing"),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        consent_data.get("analytics"),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )
    else:
      self.assertTrue(
        getattr(consent_data, "marketing", False),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        getattr(consent_data, "analytics", True),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )

  def test_buyer_info_persistence(self):
    """Test that buyer information is persisted on update.

    Given an existing checkout session,
    When the session is updated with new buyer details (email, name),
    Then the retrieved checkout session should reflect these updated buyer
    details.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update with buyer info
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=1,
    )
    payment_update = payment_update_request.PaymentUpdateRequest(
      instruments=checkout_obj.payment.instruments,
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
      buyer=buyer_update_request.BuyerUpdateRequest(
        email="test@example.com",
        first_name="Test",
        last_name="User",
      ),
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    # GET and verify
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")
    self.assertEqual(
      checkout_obj.buyer.email, "test@example.com", "Email mismatch"
    )
    self.assertEqual(
      checkout_obj.buyer.first_name, "Test", "First name mismatch"
    )


if __name__ == "__main__":
  absltest.main()
