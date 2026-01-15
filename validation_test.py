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

"""Validation tests for the UCP SDK Server."""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout_update_req
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping import payment_update_req
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import item_update_req
from ucp_sdk.models.schemas.shopping.types import line_item_update_req


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class ValidationTest(integration_test_utils.IntegrationTestBase):
  """Tests for input validation and error handling.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  """

  def test_out_of_stock(self) -> None:
    """Test validation for out-of-stock items.

    Given a product with 0 inventory,
    When a checkout creation request is made for this item,
    Then the server should return a 400 Bad Request error indicating
    insufficient stock.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
      title=out_of_stock_item["title"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)
    self.assertIn(
      "Insufficient stock",
      response.text,
      msg="Expected 'Insufficient stock' message",
    )

  def test_update_inventory_validation(self) -> None:
    """Test that inventory validation is enforced on update.

    Given an existing checkout session with a valid quantity,
    When the line item quantity is updated to exceed available stock,
    Then the server should return a 400 Bad Request error indicating
    insufficient stock.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update to excessive quantity (e.g. 10000)
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=10001,
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

    self.assert_response_status(response, 400)
    self.assertIn(
      "stock", response.text.lower(), msg="Expected 'stock' message"
    )

  def test_product_not_found(self) -> None:
    """Test validation for non-existent products.

    Given a request for a product ID that does not exist in the catalog,
    When a checkout creation request is made,
    Then the server should return a 400 Bad Request error indicating the product
    was not found.
    """
    non_existent_item = self.conformance_config.get(
      "non_existent_item",
      {"id": "non_existent_item_1", "title": "Non-existent Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=non_existent_item["id"],
      title=non_existent_item["title"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)
    self.assertIn(
      "not found", response.text.lower(), msg="Expected 'not found' message"
    )

  def test_payment_failure(self) -> None:
    """Test handling of payment failures.

    Given a checkout session ready for completion,
    When a payment instrument with a known failing token ('fail_token') is
    submitted,
    Then the server should return a 402 Payment Required error.
    """
    response_json = self.create_checkout_session(handlers=[])
    checkout_id = checkout.Checkout(**response_json).id

    # Use the helper to get valid structure, but request the failing instrument
    # 'instr_fail' is loaded from payment_instruments.csv
    payment_payload = integration_test_utils.get_valid_payment_payload(
      instrument_id="instr_fail"
    )

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 402)

  def test_complete_without_fulfillment(self) -> None:
    """Test completion rejection when fulfillment is missing.

    Given a newly created checkout session without fulfillment details,
    When a completion request is submitted,
    Then the server should return a 400 Bad Request error.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_id = response_json["id"]

    payment_payload = integration_test_utils.get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)
    self.assertIn(
      "Fulfillment address and option must be selected",
      response.text,
      msg="Expected error message for missing fulfillment",
    )

  def test_structured_error_messages(self) -> None:
    """Test that error responses conform to the Message schema.

    Given a request that triggers an error (e.g., out of stock),
    When the server responds with an error code (400),
    Then the response body should contain a structured 'detail' field describing
    the error.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)

    # Check for structured error
    data = response.json()
    self.assertTrue(data.get("detail"), "Error response missing 'detail' field")
    self.assertIn("Insufficient stock", data["detail"])


if __name__ == "__main__":
  absltest.main()
