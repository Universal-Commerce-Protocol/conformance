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

"""Order tests for the UCP SDK Server."""

import datetime
import uuid
from absl import flags
from absl.testing import absltest
import integration_test_utils
from pydantic import AnyUrl
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping import order
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import adjustment
from ucp_sdk.models.schemas.shopping.types import fulfillment_event

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})

FLAGS = flags.FLAGS


class OrderTest(integration_test_utils.IntegrationTestBase):
  """Tests for order management.

  Validated Paths:
  - GET /orders/{id}
  - PUT /orders/{id}
  """

  def test_order_retrieval(self) -> None:
    """Test successful order retrieval.

    Given a completed checkout/order,
    When a GET request is sent to /orders/{id},
    Then the response should be 200 OK and return the correct order data with
    matching IDs.
    """
    checkout_data = self.create_checkout_session()
    checkout_id = checkout_data["id"]
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    # Get Order
    response = self.client.get(
      f"/orders/{order_id}", headers=self.get_headers()
    )
    self.assert_response_status(response, 200)

    order_data = order.Order(**response.json())
    self.assertEqual(order_data.id, order_id, "Order ID mismatch")
    self.assertEqual(
      order_data.checkout_id, checkout_id, "Checkout ID mismatch"
    )

  def test_order_fulfillment_retrieval(self) -> None:
    """Test that fulfillment expectations are persisted in the order.

    Given a checkout session where a fulfillment option is selected,
    When the checkout is completed,
    Then the resulting order should contain fulfillment expectations
    matching the selected option.
    """
    checkout_data = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # Update with Address to get options
    # Use helper to get a valid address from CSV
    address_data = integration_test_utils.test_data.addresses[0]
    fulfillment_address = {
      "id": "dest_manual",
      "full_name": "Jane Doe",
      "street_address": address_data["street_address"],
      "address_locality": address_data["city"],
      "address_region": address_data["state"],
      "postal_code": address_data["postal_code"],
      "address_country": address_data["country"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [fulfillment_address],
          "selected_destination_id": "dest_manual",
        }
      ]
    }

    update_payload = {
      "id": checkout_id,
      "currency": checkout_obj.currency,
      "line_items": [
        {
          "item": {"id": li.item.id, "title": li.item.title},
          "quantity": li.quantity,
          "id": li.id,
        }
        for li in checkout_obj.line_items
      ],
      "payment": integration_test_utils.get_valid_payment_payload(),
      "fulfillment": fulfillment_payload,
    }

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout_with_options = checkout.Checkout(**response.json())

    # Check options in hierarchical structure
    options = []
    if (
      checkout_with_options.fulfillment
      and checkout_with_options.fulfillment.root.methods
      and checkout_with_options.fulfillment.root.methods[0].groups
    ):
      options = (
        checkout_with_options.fulfillment.root.methods[0].groups[0].options
      )

    self.assertTrue(options, "No options returned")

    # Select Option
    option_id = options[0].id

    # Update payload to select option
    # Need to preserve the method structure
    update_payload["fulfillment"]["methods"][0]["groups"] = [
      {"selected_option_id": option_id}
    ]

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    # Complete
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    # Get Order and verify fulfillment details
    response = self.client.get(
      f"/orders/{order_id}", headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    order_obj = order.Order(**response.json())

    self.assertTrue(
      order_obj.fulfillment.expectations, "No expectations in order"
    )
    # Verify the expectation description matches the selected option title
    self.assertEqual(
      order_obj.fulfillment.expectations[0].description,
      options[0].title,
      "Expectation description mismatch",
    )

  def test_order_update(self) -> None:
    """Test updating an existing order.

    Given a completed order,
    When a PUT request is sent to /orders/{id} with updated fulfillment events
    (e.g., adding shipment info),
    Then the response should be 200 OK and the retrieved order should reflect
    the new event.
    """
    checkout_data = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # Update with Address to get options
    # Use helper to get a valid address from CSV
    address_data = integration_test_utils.test_data.addresses[0]
    addr = {
      "id": "dest_manual_2",
      "full_name": "Jane Doe",
      "street_address": address_data["street_address"],
      "address_locality": address_data["city"],
      "address_region": address_data["state"],
      "postal_code": address_data["postal_code"],
      "address_country": address_data["country"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [addr],
          "selected_destination_id": "dest_manual_2",
        }
      ]
    }

    update_payload = {
      "id": checkout_id,
      "currency": checkout_obj.currency,
      "line_items": [
        {
          "item": {"id": li.item.id, "title": li.item.title},
          "quantity": li.quantity,
          "id": li.id,
        }
        for li in checkout_obj.line_items
      ],
      "payment": integration_test_utils.get_valid_payment_payload(),
      "fulfillment": fulfillment_payload,
    }

    resp = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    checkout_resp = resp.json()
    options = []
    if (
      checkout_resp.get("fulfillment")
      and checkout_resp["fulfillment"].get("root")  # RootModel serialized?
      and checkout_resp["fulfillment"]["root"].get("methods")
      and checkout_resp["fulfillment"]["root"]["methods"][0].get("groups")
    ):
      options = checkout_resp["fulfillment"]["root"]["methods"][0]["groups"][0][
        "options"
      ]
    elif (
      checkout_resp.get("fulfillment")
      and checkout_resp["fulfillment"].get("methods")
      and checkout_resp["fulfillment"]["methods"][0].get("groups")
    ):
      options = checkout_resp["fulfillment"]["methods"][0]["groups"][0][
        "options"
      ]

    self.assertTrue(options)

    # Select option
    update_payload["fulfillment"]["methods"][0]["groups"] = [
      {"selected_option_id": options[0]["id"]}
    ]

    self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )

    # Complete
    complete_data = self.complete_checkout_session(checkout_id)
    order_id = complete_data["order"]["id"]

    # Get Order
    resp = self.client.get(f"/orders/{order_id}", headers=self.get_headers())
    self.assert_response_status(resp, 200)
    order_obj = order.Order(**resp.json())

    # Update Order (Add Shipment Event)
    new_event = fulfillment_event.FulfillmentEvent(
      id=f"evt_{uuid.uuid4()}",
      occurred_at=datetime.datetime.now(datetime.timezone.utc),
      type="shipped",
      line_items=[
        fulfillment_event.LineItem(id=li.id, quantity=li.quantity.total)
        for li in order_obj.line_items
      ],
      tracking_number="TRACK123",
      tracking_url=AnyUrl("http://track.me/123"),
      description="Shipped via FedEx",
    )

    if order_obj.fulfillment.events is None:
      order_obj.fulfillment.events = []
    order_obj.fulfillment.events.append(new_event)

    resp = self.client.put(
      f"/orders/{order_id}",
      json=order_obj.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    updated_order = order.Order(**resp.json())
    self.assertTrue(updated_order.fulfillment.events, "No events returned")
    self.assertEqual(
      updated_order.fulfillment.events[0].tracking_number,
      "TRACK123",
      msg="Order event not persisted",
    )

  def test_order_adjustments(self) -> None:
    """Test that order adjustments are persisted.

    Given a completed order,
    When the order is updated to include an adjustment (e.g., refund),
    Then the retrieved order should correctly retain the adjustment.
    """
    order_id = self.create_completed_order()

    # Get Order
    resp = self.client.get(f"/orders/{order_id}", headers=self.get_headers())
    self.assert_response_status(resp, 200)
    order_obj = order.Order(**resp.json())

    # Add Adjustment
    adj = adjustment.Adjustment(
      id=f"adj_{uuid.uuid4()}",
      type="refund",
      occurred_at=datetime.datetime.now(datetime.timezone.utc),
      status="pending",
      amount=500,
      description="Customer refund request",
    )

    if order_obj.adjustments is None:
      order_obj.adjustments = []
    order_obj.adjustments.append(adj)

    # Update Order
    resp = self.client.put(
      f"/orders/{order_id}",
      json=order_obj.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)

    updated_order = order.Order(**resp.json())
    self.assertTrue(updated_order.adjustments, "No adjustments returned")
    self.assertEqual(updated_order.adjustments[0].amount, 500)
    self.assertEqual(updated_order.adjustments[0].type, "refund")


if __name__ == "__main__":
  absltest.main()
