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
import integration_test_utils
from ucp_sdk.models.schemas.shopping import buyer_consent_resp as buyer_consent
from ucp_sdk.models.schemas.shopping import checkout_update_req
from ucp_sdk.models.schemas.shopping import discount_resp as discount
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping import payment_update_req
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import buyer
from ucp_sdk.models.schemas.shopping.types import item_update_req
from ucp_sdk.models.schemas.shopping.types import line_item_update_req

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class BusinessLogicTest(integration_test_utils.IntegrationTestBase):
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

    # Create with wrong title in payload. The helper sends a default price
    # if not specified, but the server should ignore client-provided values
    # and use the authoritative price from its DB (which matches our config).
    response_json = self.create_checkout_session(
      title="Wrong Title", select_fulfillment=False
    )
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
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
    )
    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
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
      headers=integration_test_utils.get_headers(),
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

  def test_discount_flow(self):
    """Test that valid discount codes decrease the total amount.

    Given an existing checkout session with a total amount,
    When the valid discount code '10OFF' is applied,
    Then the total amount should be reduced by 10%, and the
    applied discount details should be present.
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

    # Apply Discount
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=1,
    )
    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    update_dict = update_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    update_dict["discounts"] = {"codes": ["10OFF"]}

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_dict,
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    discounted_checkout = checkout.Checkout(**response.json())
    expected_total = int(expected_price * 0.9)

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertIsNotNone(total_obj, "Total object missing")
    self.assertEqual(
      total_obj.amount,
      expected_total,
      msg=(
        f"Discount not applied correctly. Expected {expected_total}, got"
        f" {total_obj.amount}"
      ),
    )

    # Parse discounts from extra fields
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )

    self.assertTrue(
      discounts_obj and discounts_obj.applied,
      "Applied discounts field missing",
    )
    self.assertEqual(
      discounts_obj.applied[0].code,
      "10OFF",
      "Applied discounts field incorrect",
    )

  def test_multiple_discounts_accepted(self):
    """Test that multiple valid discount codes are both applied.

    Given an existing checkout session,
    When two valid discount codes ('10OFF' and 'WELCOME20') are applied,
    Then the total amount should be reduced by both discounts sequentially,
    and both should be present in the applied list.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Get expected price from config
    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    # Apply both discounts using helper to ensure all required fields are
    # present
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["10OFF", "WELCOME20"]}
    )

    discounted_checkout = checkout.Checkout(**response_json)
    # 3500 -> 3500 * 0.9 = 3150 -> 3150 * 0.8 = 2520
    expected_total = int(int(expected_price * 0.9) * 0.8)

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertEqual(
      total_obj.amount,
      expected_total,
      f"Multiple discounts failed. Exp {expected_total}, "
      f"got {total_obj.amount}",
    )

    # Verify both applied discounts are present
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 2)
    applied_codes = [d.code for d in discounts_obj.applied]
    self.assertIn("10OFF", applied_codes)
    self.assertIn("WELCOME20", applied_codes)

  def test_multiple_discounts_one_rejected(self):
    """Test requesting multiple discounts where one is valid and one is not.

    Given an existing checkout session,
    When one valid ('10OFF') and one invalid ('INVALID_CODE') are applied,
    Then only the valid discount should be applied, and the invalid one
    should be omitted from the applied list.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Get expected price
    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    # Apply one valid and one invalid discount using helper
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["10OFF", "INVALID_CODE"]}
    )

    discounted_checkout = checkout.Checkout(**response_json)
    # Only 10% off
    expected_total = int(expected_price * 0.9)

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertEqual(total_obj.amount, expected_total)

    # Verify only one applied discount is present
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 1)
    self.assertEqual(discounts_obj.applied[0].code, "10OFF")

  def test_fixed_amount_discount(self):
    """Test that a fixed-amount discount code decreases the total correctly.

    Given an existing checkout session with a total amount,
    When the valid fixed-amount discount code 'FIXED500' is applied,
    Then the total amount should be reduced by 500 cents, and the
    applied discount details should be present.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Get expected price from config
    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    # Apply Fixed-amount Discount
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["FIXED500"]}
    )

    discounted_checkout = checkout.Checkout(**response_json)
    # 3500 - 500 = 3000
    expected_total = expected_price - 500

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertIsNotNone(total_obj, "Total object missing")
    self.assertEqual(
      total_obj.amount,
      expected_total,
      msg=(
        f"Fixed discount failed. Exp {expected_total}, got {total_obj.amount}"
      ),
    )

    # Parse discounts from extra fields
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )

    self.assertTrue(
      discounts_obj and discounts_obj.applied,
      "Applied discounts field missing",
    )
    self.assertEqual(
      discounts_obj.applied[0].code,
      "FIXED500",
    )
    self.assertEqual(
      discounts_obj.applied[0].amount,
      500,
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
    consent_obj = buyer_consent.Consent(
      marketing=True,
      analytics=False,
      sale_of_data=False,
    )

    create_payload_dict = create_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    create_payload_dict["buyer"] = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
      "consent": consent_obj.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
    }

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload_dict,
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 201)
    checkout_id = checkout.Checkout(**response.json()).id

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")

    # buyer is types.buyer.Buyer, consent is in extra fields
    consent_data = getattr(checkout_obj.buyer, "consent", None)
    self.assertTrue(consent_data, "Consent info missing")

    # Parse to model for easy access
    consent_model = buyer_consent.Consent(**consent_data)

    self.assertTrue(
      consent_model.marketing,
      f"Marketing consent not persisted. Resp: {consent_model}",
    )
    self.assertFalse(
      consent_model.analytics,
      f"Analytics consent not persisted. Resp: {consent_model}",
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
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=1,
    )
    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
      buyer=buyer.Buyer(
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
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    # GET and verify
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=integration_test_utils.get_headers(),
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
