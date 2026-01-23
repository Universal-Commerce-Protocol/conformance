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

    Reference: https://ucp.dev/specification/checkout/#error-handling

    Given a product with 0 inventory,
    When a checkout creation request is made for this item,
    Then the server should return a successful response (201) with status
    'incomplete' and an error message indicating insufficient stock.
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

    # Verify HTTP status and then using centralized utility with
    # exact path matching
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()
    li_id = checkout_data["line_items"][0]["id"]
    self.assert_checkout_status(
      checkout_data,
      expected_status="incomplete",
      valid_path_matchers=[
        "$.line_items[0]",
        f"$.line_items[?(@.id=='{li_id}')]",
      ],
      model_class=checkout.Checkout,
    )

  def test_update_inventory_validation(self) -> None:
    """Test that inventory validation is enforced on update.

    Reference: https://ucp.dev/specification/checkout/#error-handling

    Given an existing checkout session with a valid quantity,
    When the line item quantity is updated to exceed available stock,
    Then the server should return a successful response (200/201) with
    status 'incomplete' and an error message indicating insufficient stock.
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

    # Verify HTTP status and then using centralized utility with
    # exact path matching
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()
    li_id = checkout_data["line_items"][0]["id"]
    self.assert_checkout_status(
      checkout_data,
      expected_status="incomplete",
      valid_path_matchers=[
        "$.line_items[0]",
        f"$.line_items[?(@.id=='{li_id}')]",
      ],
      model_class=checkout.Checkout,
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

  def test_update_without_fulfillment(self) -> None:
    """Test Soft-Fail when fulfillment info is missing during update.

    Reference: https://ucp.dev/specification/checkout/#error-handling

    Given an existing checkout session,
    When an update request is sent with an empty fulfillment method,
    Then the server should return a 200 OK status with 'incomplete' status
    and an error message indicating missing fulfillment info.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Update with empty fulfillment using the helper to ensure valid structure
    response_json = self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )

    # Get the method ID from the response for precise path matching
    method_id = response_json["fulfillment"]["methods"][0]["id"]

    self.assert_checkout_status(
      response_json,
      expected_status="incomplete",
      valid_path_matchers=[
        "$.fulfillment",
        "$.fulfillment.methods",
        f"$.fulfillment.methods[?(@.id=='{method_id}')].destinations",
      ],
      model_class=checkout.Checkout,
    )

  def test_structured_error_messages(self) -> None:
    """Test that error responses conform to the UCP ErrorResponse schema.

    Reference: https://ucp.dev/specification/checkout-rest/#error-responses

    Given a request for a non-existent checkout ID,
    When the server responds with a 404 Not Found error,
    Then the response body should contain a structured UCP response with
    status 'requires_escalation' and a descriptive error message.
    """
    non_existent_id = "non-existent-session-id"

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{non_existent_id}"),
      headers=integration_test_utils.get_headers(),
    )

    # Verify HTTP status 404
    self.assert_response_status(response, 404)

    # Verify using centralized utility with 'requires_escalation' status
    self.assert_checkout_status(
      response.json(), expected_status="requires_escalation"
    )


if __name__ == "__main__":
  absltest.main()
