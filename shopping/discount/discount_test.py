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

"""Discount application tests for the UCP Shopping vertical."""

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import discount as discount
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)

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
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


@requires_capability("dev.ucp.shopping.discount")
class DiscountTest(ShoppingIntegrationTestBase):
  """Tests for applying discount codes to a checkout session.

  Validated Paths:
  - PUT /checkout-sessions/{id}
  """

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
      headers=self.get_headers(),
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
    applied_codes = [d.code for d in discounts_obj.applied]
    self.assertIn(
      valid_code,
      applied_codes,
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

    # Verify both submitted codes are present in the applied list. The applied
    # list also carries any automatic discounts (discount.md: applied contains
    # code-based + automatic), so assert membership rather than an exact count.
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and discounts_obj.applied)
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

    # Verify the valid code is present in the applied list. The applied list
    # also carries any automatic discounts, so assert membership rather than an
    # exact count or a fixed position (automatic discounts have no code).
    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and discounts_obj.applied)
    applied_codes = [d.code for d in discounts_obj.applied]
    self.assertIn(valid_code, applied_codes)
    self.assertNotIn("INVALID_CODE", applied_codes)

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
    applied_discounts = {d.code: d for d in discounts_obj.applied if d.code}
    self.assertIn(fixed_code, applied_discounts)
    self.assertEqual(
      applied_discounts[fixed_code].amount,
      expected_discount,
      "applied discount amount must match the configured fixed reduction",
    )

  def test_unknown_discount_code(self):
    """Test that unknown discount codes are ignored."""
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)

    resp_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["INVALID_CODE_123"]}
    )
    updated_checkout = checkout.Checkout(**resp_json)
    discount_total = next(
      (t for t in updated_checkout.totals if t.type == "discount"), None
    )
    self.assertIsNone(
      discount_total, "Unknown discount code should not apply discount"
    )


if __name__ == "__main__":
  absltest.main()
