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
from framework.decorators import (
  requires_capability,
  spec_assert,
)
from shopping.base import get_valid_payment_payload
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout

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


@requires_capability("dev.ucp.shopping.checkout")
class ValidationTest(ShoppingIntegrationTestBase):
  """Tests for input validation and error handling.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  """

  def assert_4xx_error(
    self,
    response,
    expected_status: int,
    substring: str,
  ) -> None:
    """Assert a 4xx rejection with a specific status and error message."""
    self.assert_response_status(response, expected_status)
    self._assert_structured_4xx_error(
      response,
      about=substring,
      paths=(),
      codes=frozenset(),
      words=(substring,),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_out_of_stock(self) -> None:
    """Test validation for out-of-stock items.

    Given a product with 0 inventory,
    When a checkout creation request is made for this item,
    Then the server either rejects with a 4xx describing the stock problem,
    or answers in-band per the spec's error model with a typed
    'out_of_stock' error message.
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
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      about="the item being out of stock",
      paths=("$.line_items",),
      codes={"out_of_stock", "item_unavailable"},
      words=("stock", "unavailable"),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_update_inventory_validation(self) -> None:
    """Test that inventory validation is enforced on update.

    Given an existing checkout session with a valid quantity,
    When the line item quantity is updated to exceed available stock,
    Then the server either rejects with a 4xx describing the stock
    problem, answers in-band per the spec's error model with a typed
    'out_of_stock'/'item_unavailable' error message, or clamps the
    quantity and reports a 'quantity_adjusted' warning (checkout-rest.md
    "Business Outcomes" — the spec's canonical answer to requesting more
    units than are in stock).
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update to excessive quantity (e.g. 10000)
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=10001,
    )
    update_payload = self.checkout_update_payload(
      checkout_obj, line_items=[line_item_update]
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )

    if response.status_code in (200, 201):
      data = response.json()
      messages = data.get("messages", [])
      adjusted = [
        m
        for m in messages
        if m.get("type") == "warning" and m.get("code") == "quantity_adjusted"
      ]
      if adjusted and not any(m.get("type") == "error" for m in messages):
        # Clamp-and-warn posture (checkout-rest.md "Business Outcomes"):
        # the server fulfills what it can and reports the adjustment. The
        # returned quantity must actually be clamped below the requested
        # amount — silently accepting the excess would be a real failure.
        quantities = [li.get("quantity") for li in data.get("line_items", [])]
        self.assertTrue(
          quantities
          and all(isinstance(q, int) and q < 10001 for q in quantities),
          "A quantity_adjusted warning must come with the line-item "
          "quantity actually clamped below the requested amount, got "
          f"{quantities}",
        )
        return

    self.assert_business_error(
      response,
      about="the item being out of stock",
      paths=("$.line_items",),
      codes={"out_of_stock", "item_unavailable"},
      words=("stock", "unavailable"),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_product_not_found(self) -> None:
    """Test validation for non-existent products.

    Given a request for a product ID that does not exist in the catalog,
    When a checkout creation request is made,
    Then the server either rejects with a 4xx indicating the product was not
    found, or answers in-band per the spec's error model with a typed error
    message.
    """
    non_existent_item = self.conformance_config.get(
      "non_existent_item",
      {"id": "non_existent_item_1", "title": "Non-existent Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=non_existent_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      about="the unknown item",
      paths=("$.line_items",),
      codes={"not_found", "item_unavailable"},
      words=("not found", "unknown", "unavailable", "does not exist"),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_payment_failure(self) -> None:
    """Test handling of payment failures.

    Given a checkout session ready for completion,
    When a payment instrument with a known failing token ('fail_token') is
    submitted,
    Then the server either rejects with a 4xx describing the payment
    problem, or answers in-band per the spec's error model with a typed
    'payment_failed' error message (checkout.md standard error codes /
    error_code.json; checkout-rest.md "Error Responses": business
    outcomes return HTTP 200 with the UCP envelope and messages[]).
    """
    # With a buyer, so that a business which needs one to place an order
    # gets as far as the payment and reports that, not the missing buyer.
    response_json = self.create_checkout_session(
      handlers=[], buyer=self.DEFAULT_BUYER
    )
    checkout_id = checkout.Checkout(**response_json).id

    # Use the helper to get valid structure, but request the failing instrument
    # 'instr_fail' is loaded from payment_instruments.csv
    payment_payload = get_valid_payment_payload(instrument_id="instr_fail")

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      about="the failed payment",
      paths=("$.payment",),
      codes={"payment_failed", "payment_declined"},
      words=("payment", "declined", "card"),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  @requires_capability("dev.ucp.shopping.fulfillment")
  def test_complete_without_fulfillment(self) -> None:
    """Test completion rejection when fulfillment is missing.

    Given a newly created checkout session without fulfillment details,
    When a completion request is submitted,
    Then the server either rejects with a 4xx describing the fulfillment
    problem, or returns an in-band error message with code 'missing' or
    'field_required'.
    """
    response_json = self.create_checkout_session(
      select_fulfillment=False, buyer=self.DEFAULT_BUYER
    )
    checkout_id = response_json["id"]

    payment_payload = get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      about="the missing fulfillment",
      paths=("$.fulfillment",),
      codes={"missing", "field_required"},
      words=("fulfillment", "shipping", "address", "delivery"),
    )

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0105",  # SHOULD provide accurate error messages
  )
  def test_structured_error_messages(self) -> None:
    """Test that error responses carry structured, machine-readable detail.

    Given a request that triggers an error (e.g., out of stock),
    Then a 4xx rejection must carry a structured 'detail' field describing
    the error, and an in-band answer must carry the full message envelope
    (type, code, content, severity) — the structural requirement behind
    both postures.
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
      headers=self.get_headers(),
    )

    self.assert_business_error(
      response,
      about="the item being out of stock",
      paths=("$.line_items",),
      codes={"out_of_stock", "item_unavailable"},
      words=("stock", "unavailable"),
    )


if __name__ == "__main__":
  absltest.main()
