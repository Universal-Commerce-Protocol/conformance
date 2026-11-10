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

"""Tests for Webhook notifications in UCP SDK Server."""

import time
from typing import Any

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class WebhookTest(integration_test_utils.IntegrationTestBase):
  """Tests for Webhook notifications."""

  def setUp(self) -> None:
    """Set up the webhook server and configuration."""
    super().setUp()
    port = integration_test_utils.FLAGS.mock_webhook_port
    self.webhook_server = integration_test_utils.MockWebhookServer(port=port)
    self.webhook_server.start()
    self.webhook_url = (
      f"http://localhost:{port}/webhooks/partners/test_partner/events/order"
    )

  def tearDown(self) -> None:
    """Stop the webhook server and clean up."""
    self.webhook_server.stop()
    super().tearDown()

  def _deliveries_for(self, order_id: str) -> list[dict[str, Any]]:
    """All captured deliveries whose payload is the given order.

    order.md defines only ``Webhook-Id`` and ``Webhook-Timestamp`` as required
    delivery headers, so a conformant Business need not label the event type in
    a header; the order entity in the body is what identifies the delivery.

    A delivery whose body carries no ``id`` (a delta rather than the full
    entity) is invisible here and reads as "not delivered". That deviation is
    diagnosed by webhook_structure_test, which asserts the full-entity contract
    directly.
    """
    return [
      e
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict)
      and e["payload"].get("id") == order_id
    ]

  def test_webhook_event_stream(self) -> None:
    """Test that completing and then shipping an order each notify.

    Given a mock webhook server is running,
    When a checkout is completed with a webhook_url (via Agent Profile),
    Then the server should deliver the "Order created" event.
    When the order is subsequently shipped,
    Then the server should deliver an update whose order snapshot reflects
    the shipment.
    """
    # 1. Create checkout (webhook URL passed via UCP-Agent header)
    checkout_data = self.create_checkout_session(headers=self.get_headers())

    checkout_obj = checkout.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # 2. Complete Checkout
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]

    # 3. Observe the "Order created" delivery before shipping. Checking it
    # here, rather than counting deliveries at the end, is what stops a
    # Business that only announces the shipment from satisfying the count
    # (order.md: MUST send the "Order created" event on completion).
    for _ in range(50):
      if self._deliveries_for(order_id):
        break
      time.sleep(0.1)
    self.assertTrue(
      self._deliveries_for(order_id),
      "no order-event webhook delivered for the completed order",
    )

    # 4. Trigger Shipping
    headers = self.get_headers()
    headers["Simulation-Secret"] = (
      integration_test_utils.FLAGS.simulation_secret
    )
    ship_response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    self.assert_response_status(ship_response, 200)

    # 5. Verify Webhook Events
    # Poll for the update delivery to arrive (up to 5 seconds)
    for _ in range(50):
      if len(self._deliveries_for(order_id)) >= 2:
        break
      time.sleep(0.1)

    deliveries = self._deliveries_for(order_id)
    self.assertGreaterEqual(
      len(deliveries),
      2,
      f"Expected at least 2 deliveries for order {order_id}, "
      f"got {len(deliveries)}",
    )

    # Every delivery belongs to this checkout.
    for delivery in deliveries:
      self.assertEqual(delivery["payload"].get("checkout_id"), checkout_id)

    # The shipment is observable in the order snapshot itself, which is what
    # order.md requires the Business to send — not a header label.
    shipped_delivery = next(
      (
        d
        for d in deliveries
        if any(
          fe.get("type") == "shipped"
          for fe in (d["payload"].get("fulfillment") or {}).get("events") or []
        )
      ),
      None,
    )
    self.assertIsNotNone(
      shipped_delivery,
      "no delivery reflected the shipped state in the order data",
    )

  def test_webhook_order_address_known_customer(self) -> None:
    """Test that webhook contains correct address for known customer/address."""
    customer = self.fixture_ctx.get_known_customer()
    if not customer or not customer.get("addresses"):
      self.skipTest(
        "No known customer with stored addresses configured in fixtures."
      )
    buyer_info = {
      "fullName": customer.get("full_name", ""),
      "email": customer["email"],
    }
    checkout_data = self.create_checkout_session(buyer=buyer_info)
    checkout_obj = checkout.Checkout(**checkout_data)

    # Update to trigger address injection and selection
    self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "line_item_ids": [checkout_obj.line_items[0].id],
            "type": "shipping",
          }
        ]
      },
    )

    # Fetch to get injected destinations
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      headers=self.get_headers(),
    )
    checkout_data = response.json()
    checkout_obj = checkout.Checkout(**checkout_data)

    self.assertTrue(
      getattr(checkout_obj, "model_extra", None)
      and checkout_obj.model_extra.get("fulfillment")
      and checkout_obj.model_extra["fulfillment"].get("methods")
    )
    selected_destination = None
    if checkout_obj.model_extra["fulfillment"]["methods"][0].get(
      "destinations"
    ):
      method = checkout_obj.model_extra["fulfillment"]["methods"][0]
      selected_destination = method["destinations"][0]
      dest_id = selected_destination["id"]
      # Select destination first to calculate options
      self.update_checkout_session(
        checkout_obj,
        fulfillment={
          "methods": [
            {
              "id": "method_1",
              "line_item_ids": [checkout_obj.line_items[0].id],
              "type": "shipping",
              "selected_destination_id": dest_id,
            }
          ]
        },
      )

      # Fetch again to get options
      response = self.client.get(
        self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
        headers=self.get_headers(),
      )
      checkout_obj = checkout.Checkout(**response.json())
      method = checkout_obj.model_extra["fulfillment"]["methods"][0]
      if method.get("groups", []) and method.get("groups", [])[0].get(
        "options", []
      ):
        option_id = method.get("groups", [])[0].get("options", [])[0].get("id")
        self.update_checkout_session(
          checkout_obj,
          fulfillment={
            "methods": [
              {
                "id": "method_1",
                "line_item_ids": [checkout_obj.line_items[0].id],
                "type": "shipping",
                "selected_destination_id": dest_id,
                "groups": [
                  {
                    "id": "group_1",
                    "line_item_ids": [checkout_obj.line_items[0].id],
                    "selected_option_id": option_id,
                  }
                ],
              }
            ]
          },
        )

    complete_response = self.complete_checkout_session(checkout_obj.id)
    order_id = complete_response["order"]["id"]

    for _ in range(20):
      if len(self.webhook_server.events) >= 1:
        break
      time.sleep(0.1)

    event = next(
      (e for e in self.webhook_server.events if e["payload"]["id"] == order_id),
      None,
    )
    self.assertIsNotNone(event)
    expectations = event["payload"]["fulfillment"]["expectations"]
    self.assertTrue(expectations)
    destination = expectations[0]["destination"]
    self.assertIsNotNone(selected_destination)
    for field in (
      "street_address",
      "address_locality",
      "address_region",
      "postal_code",
      "address_country",
    ):
      if field in selected_destination:
        self.assertEqual(destination.get(field), selected_destination[field])

  def test_webhook_order_address_new_address(self) -> None:
    """Test that webhook contains correct address when a new one is provided."""
    customer = self.fixture_ctx.get_known_customer()
    if not customer:
      self.skipTest("No known customer configured in fixtures.")
    buyer_info = {
      "fullName": customer.get("full_name", ""),
      "email": customer["email"],
    }
    checkout_data = self.create_checkout_session(buyer=buyer_info)
    checkout_obj = checkout.Checkout(**checkout_data)

    new_address = {
      "id": "dest_new_webhook",
      "address_country": "CA",
      "postal_code": "M5V 2H1",
      "street_address": "Webhook St",
    }
    # Send address to get options
    fulfillment_payload = {
      "methods": [
        {
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "type": "shipping",
          "destinations": [new_address],
          "selected_destination_id": "dest_new_webhook",
        }
      ]
    }
    self.update_checkout_session(checkout_obj, fulfillment=fulfillment_payload)

    # Fetch to get options
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      headers=self.get_headers(),
    )
    checkout_obj = checkout.Checkout(**response.json())
    method = checkout_obj.model_extra["fulfillment"]["methods"][0]

    if method.get("groups", []) and method.get("groups", [])[0].get(
      "options", []
    ):
      option_id = method.get("groups", [])[0].get("options", [])[0].get("id")
      # Select option
      fulfillment_payload["methods"][0]["groups"] = [
        {
          "id": "group_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "selected_option_id": option_id,
        }
      ]
      fulfillment_payload["methods"][0]["type"] = "shipping"
      fulfillment_payload["methods"][0]["id"] = "method_1"
      fulfillment_payload["methods"][0]["line_item_ids"] = [
        checkout_obj.line_items[0].id
      ]
      self.update_checkout_session(
        checkout_obj, fulfillment=fulfillment_payload
      )

    complete_response = self.complete_checkout_session(checkout_obj.id)
    order_id = complete_response["order"]["id"]

    for _ in range(20):
      if len(self.webhook_server.events) >= 1:
        break
      time.sleep(0.1)

    event = next(
      (e for e in self.webhook_server.events if e["payload"]["id"] == order_id),
      None,
    )
    self.assertIsNotNone(event)
    expectations = event["payload"]["fulfillment"]["expectations"]
    self.assertTrue(expectations)
    self.assertEqual(expectations[0]["destination"]["address_country"], "CA")
    self.assertEqual(
      expectations[0]["destination"]["street_address"], "Webhook St"
    )


if __name__ == "__main__":
  absltest.main()
