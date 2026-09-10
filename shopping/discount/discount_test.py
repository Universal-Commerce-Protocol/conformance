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

"""Discount capability tests for UCP Shopping vertical."""

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import discount


@requires_capability("dev.ucp.shopping.discount")
class DiscountTest(ShoppingIntegrationTestBase):
  """Tests for promotion and discount capabilities in retail checkout.

  Validated Paths:
  - PUT /checkout-sessions/{id}
  """

  def test_discount_flow(self):
    """Test that valid discount codes decrease the total amount."""
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    response = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["10OFF"]}
    )
    discounted_checkout = checkout.Checkout(**response)
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
    """Test that multiple valid discount codes are both applied."""
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["10OFF", "WELCOME20"]}
    )
    discounted_checkout = checkout.Checkout(**response_json)
    expected_total = int(int(expected_price * 0.9) * 0.8)

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertEqual(
      total_obj.amount,
      expected_total,
      f"Multiple discounts failed. Exp {expected_total}, got"
      f" {total_obj.amount}",
    )

    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 2)
    applied_codes = [d.code for d in discounts_obj.applied]
    self.assertIn("10OFF", applied_codes)
    self.assertIn("WELCOME20", applied_codes)

  def test_multiple_discounts_one_rejected(self):
    """Test requesting multiple discounts where one is valid and one is not."""
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["10OFF", "INVALID_CODE"]}
    )
    discounted_checkout = checkout.Checkout(**response_json)
    expected_total = int(expected_price * 0.9)

    total_obj = next(
      (t for t in discounted_checkout.totals if t.type == "total"), None
    )
    self.assertEqual(total_obj.amount, expected_total)

    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(discounts_obj and len(discounts_obj.applied) == 1)
    self.assertEqual(discounts_obj.applied[0].code, "10OFF")

  def test_fixed_amount_discount(self):
    """Test that a fixed-amount discount code decreases the total correctly."""
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    expected_price = (
      self.conformance_config.get("items", [{}])[0].get("price", 3500)
      if self.conformance_config
      else 3500
    )
    expected_price = int(expected_price)

    response_json = self.update_checkout_session(
      checkout_obj, discounts={"codes": ["FIXED500"]}
    )
    discounted_checkout = checkout.Checkout(**response_json)
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

    discounts_data = getattr(discounted_checkout, "discounts", {})
    discounts_obj = (
      discount.DiscountsObject(**discounts_data) if discounts_data else None
    )
    self.assertTrue(
      discounts_obj and discounts_obj.applied,
      "Applied discounts field missing",
    )
    self.assertEqual(discounts_obj.applied[0].code, "FIXED500")
    self.assertEqual(discounts_obj.applied[0].amount, 500)

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
