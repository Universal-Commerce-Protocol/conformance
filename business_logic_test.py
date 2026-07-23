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
from ucp_sdk.models.schemas.shopping import buyer_consent as buyer_consent
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)
from ucp_sdk.models.schemas.shopping import discount as discount
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import payment_update_request
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)
from ucp_sdk.models.schemas.shopping.types import buyer_update_request
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class BusinessLogicTest(integration_test_utils.IntegrationTestBase):
  """Tests for business logic and calculations.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - GET /checkout-sessions/{id}
  """

  def assert_totals_consistent(
    self, checkout_obj, expected_subtotal, expected_discount=0
  ):
    """Assert that the checkout totals are consistent and correct."""
    subtotal = next(
      (t.amount for t in checkout_obj.totals if t.type == "subtotal"), 0
    )
    fulfillment = next(
      (t.amount for t in checkout_obj.totals if t.type == "fulfillment"), 0
    )
    tax = next((t.amount for t in checkout_obj.totals if t.type == "tax"), 0)
    fee = next((t.amount for t in checkout_obj.totals if t.type == "fee"), 0)
    discount = sum(
      t.amount
      for t in checkout_obj.totals
      if t.type in ["items_discount", "discount"]
    )
    total = next(
      (t.amount for t in checkout_obj.totals if t.type == "total"), 0
    )

    self.assertEqual(
      subtotal,
      expected_subtotal,
      f"Subtotal mismatch: expected {expected_subtotal}, got {subtotal}",
    )
    if expected_discount:
      if isinstance(expected_discount, (list, set, tuple)):
        self.assertIn(
          abs(discount),
          expected_discount,
          (
            f"Discount mismatch: expected one of {expected_discount}, got"
            f" {discount}"
          ),
        )
      else:
        self.assertEqual(
          abs(discount),
          expected_discount,
          f"Discount mismatch: expected {expected_discount}, got {discount}",
        )
    calculated_total = subtotal + fulfillment + tax + fee - abs(discount)
    self.assertEqual(
      total,
      calculated_total,
      (
        f"Total math mismatch: calculated {calculated_total} (subtotal="
        f"{subtotal}, fulfillment={fulfillment}, tax={tax}, fee={fee}, "
        f"discount={discount}), got {total}"
      ),
    )

  def test_totals_calculation_on_create(self):
    """Test that totals are calculated correctly upon checkout creation.

    Given a request to create a checkout session with a specific item,
    When the checkout is created with an incorrect title/price in the request,
    Then the server should return a checkout where line item totals, subtotal,
    and grand total correctly reflect the database price, ignoring client
    input.
    """
    expected_price = self.fixture_ctx.get_test_price()

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
    self.assert_totals_consistent(checkout_obj, expected_price)

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

    expected_price = self.fixture_ctx.get_test_price()

    # Update quantity to 2.
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
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    updated_checkout = checkout.Checkout(**response.json())
    expected_total = expected_price * 2
    self.assert_totals_consistent(updated_checkout, expected_total)

  def test_discount_flow(self):
    """Test that valid discount codes decrease the total amount.

    Given an existing checkout session with a total amount,
    When a valid discount code is applied,
    Then the total amount should be reduced, and the
    applied discount details should be present.
    """
    valid_code = self.fixture_ctx.get_test_discount_code()
    expected_price = self.fixture_ctx.get_test_price()
    percentage = self.fixture_ctx.get_expected_discount_percentage()
    expected_discount = round(expected_price * (percentage / 100))

    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Apply Discount
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
    )

    update_dict = update_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    update_dict["discounts"] = {"codes": [valid_code]}

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_dict,
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    discounted_checkout = checkout.Checkout(**response.json())

    self.assert_totals_consistent(
      discounted_checkout, expected_price, expected_discount=expected_discount
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
      valid_code,
      "Applied discounts field incorrect",
    )

  def test_multiple_discounts_accepted(self):
    """Test that multiple valid discount codes are both applied.

    Given an existing checkout session,
    When two valid discount codes are applied,
    Then the total amount should be reduced by both discounts sequentially,
    and both should be present in the applied list.
    """
    valid_code_1 = self.fixture_ctx.get_test_discount_code()
    valid_code_2 = self.fixture_ctx.get_test_discount_code_2()
    if not valid_code_2:
      self.skipTest("Second valid discount code not configured in fixtures.")

    expected_price = self.fixture_ctx.get_test_price()
    percentage1 = self.fixture_ctx.get_expected_discount_percentage()
    percentage2 = self.fixture_ctx.get_expected_discount_percentage_2()

    # Cumulative discount: each calculated on the original price
    d1_cum = round(expected_price * (percentage1 / 100))
    d2_cum = round(expected_price * (percentage2 / 100))
    expected_discount_cumulative = d1_cum + d2_cum

    # Sequential discount: second calculated on the remaining balance
    d1_seq = round(expected_price * (percentage1 / 100))
    d2_seq = round((expected_price - d1_seq) * (percentage2 / 100))
    expected_discount_sequential = d1_seq + d2_seq

    # Accept either approach
    expected_discount = [
      expected_discount_sequential,
      expected_discount_cumulative,
    ]

    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Apply both discounts using helper
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": [valid_code_1, valid_code_2]}
    )

    discounted_checkout = checkout.Checkout(**response_json)

    self.assert_totals_consistent(
      discounted_checkout, expected_price, expected_discount=expected_discount
    )

    # Verify both applied discounts are present
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 2)
    applied_codes = [d.code for d in discounts_obj.applied]
    self.assertIn(valid_code_1, applied_codes)
    self.assertIn(valid_code_2, applied_codes)

  def test_multiple_discounts_one_rejected(self):
    """Test requesting multiple discounts where one is valid and one is not.

    Given an existing checkout session,
    When one valid and one invalid are applied,
    Then only the valid discount should be applied, and the invalid one
    should be omitted from the applied list.
    """
    valid_code = self.fixture_ctx.get_test_discount_code()
    expected_price = self.fixture_ctx.get_test_price()
    percentage = self.fixture_ctx.get_expected_discount_percentage()
    expected_discount = round(expected_price * (percentage / 100))

    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Apply one valid and one invalid discount using helper
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": [valid_code, "INVALID_CODE"]}
    )

    discounted_checkout = checkout.Checkout(**response_json)

    self.assert_totals_consistent(
      discounted_checkout, expected_price, expected_discount=expected_discount
    )

    # Verify only one applied discount is present
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 1)
    self.assertEqual(discounts_obj.applied[0].code, valid_code)

  def test_fixed_amount_discount(self):
    """Test that a fixed-amount discount code decreases the total correctly.

    Given an existing checkout session with a total amount,
    When a valid fixed-amount discount code is applied,
    Then the total amount should be reduced, and the
    applied discount details should be present.
    """
    fixed_code = self.fixture_ctx.get_test_fixed_discount_code()
    if not fixed_code:
      self.skipTest("Fixed discount code not configured in fixtures.")

    expected_price = self.fixture_ctx.get_test_price()
    expected_discount = self.fixture_ctx.get_expected_fixed_discount_reduction()

    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Apply Fixed-amount Discount
    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": [fixed_code]}
    )

    discounted_checkout = checkout.Checkout(**response_json)

    self.assert_totals_consistent(
      discounted_checkout, expected_price, expected_discount=expected_discount
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
      fixed_code,
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
