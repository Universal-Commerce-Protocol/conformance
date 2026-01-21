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
from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import fulfillment_resp
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)

# Rebuild models to resolve forward references
fulfillment_resp.Checkout.model_rebuild(
  _types_namespace={"PaymentResponse": Payment}
)


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

  def test_webhook_event_stream(self) -> None:
    """Test that the server sends order_placed and order_shipped events.

    Given a mock webhook server is running,
    When a checkout is completed with a webhook_url (via Agent Profile),
    Then the server should send an 'order_placed' event.
    When the order is subsequently shipped,
    Then the server should send an 'order_shipped' event.
    """
    # 1. Create checkout (webhook URL passed via UCP-Agent header)
    checkout_data = self.create_checkout_session(headers=self.get_headers())

    checkout_obj = fulfillment_resp.Checkout(**checkout_data)
    checkout_id = checkout_obj.id

    # 2. Complete Checkout
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]

    # 3. Trigger Shipping
    headers = self.get_headers()
    headers["Simulation-Secret"] = (
      integration_test_utils.FLAGS.simulation_secret
    )
    ship_response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    self.assert_response_status(ship_response, 200)

    # 4. Verify Webhook Events
    # Poll for events to arrive (up to 2 seconds)
    for _ in range(20):
      if len(self.webhook_server.events) >= 2:
        break
      time.sleep(0.1)

    events = self.webhook_server.events
    self.assertGreaterEqual(
      len(events),
      2,
      f"Expected at least 2 events, got {len(events)}",
    )

    # Verify order_placed event
    placed_event = next(
      (e for e in events if e["payload"]["event_type"] == "order_placed"),
      None,
    )
    self.assertIsNotNone(placed_event, "Missing order_placed event")
    self.assertEqual(placed_event["payload"]["checkout_id"], checkout_id)
    self.assertEqual(placed_event["payload"]["order"]["id"], order_id)

    # Verify order_shipped event
    shipped_event = next(
      (e for e in events if e["payload"]["event_type"] == "order_shipped"),
      None,
    )
    self.assertIsNotNone(shipped_event, "Missing order_shipped event")
    self.assertEqual(shipped_event["payload"]["checkout_id"], checkout_id)
    self.assertEqual(shipped_event["payload"]["order"]["id"], order_id)

    fulfillment_events = shipped_event["payload"]["order"]["fulfillment"].get(
      "events", []
    )
    self.assertTrue(
      any(e["type"] == "shipped" for e in fulfillment_events),
      "order_shipped event did not contain shipment info in order data",
    )

  def test_webhook_order_address_known_customer(self) -> None:
    """Test that webhook contains correct address for known customer/address."""
    buyer_info = {"fullName": "John Doe", "email": "john.doe@example.com"}
    checkout_data = self.create_checkout_session(buyer=buyer_info)
    checkout_obj = fulfillment_resp.Checkout(**checkout_data)

    # Trigger fulfillment update to inject address
    self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )

    # Fetch to get injected destinations
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      headers=self.get_headers(),
    )
    checkout_data = response.json()
    checkout_obj = fulfillment_resp.Checkout(**checkout_data)

    if (
      checkout_obj.fulfillment
      and checkout_obj.fulfillment.root.methods
      and checkout_obj.fulfillment.root.methods[0].destinations
    ):
      method = checkout_obj.fulfillment.root.methods[0]
      dest_id = method.destinations[0].root.id
      # Select destination first to calculate options
      self.update_checkout_session(
        checkout_obj,
        fulfillment={
          "methods": [{"type": "shipping", "selected_destination_id": dest_id}]
        },
      )

      # Fetch again to get options
      response = self.client.get(
        self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
        headers=self.get_headers(),
      )
      checkout_obj = fulfillment_resp.Checkout(**response.json())
      method = checkout_obj.fulfillment.root.methods[0]
      if method.groups and method.groups[0].options:
        option_id = method.groups[0].options[0].id
        self.update_checkout_session(
          checkout_obj,
          fulfillment={
            "methods": [
              {
                "type": "shipping",
                "selected_destination_id": dest_id,
                "groups": [{"selected_option_id": option_id}],
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
      (
        e
        for e in self.webhook_server.events
        if e["payload"]["order"]["id"] == order_id
      ),
      None,
    )
    self.assertIsNotNone(event)
    expectations = event["payload"]["order"]["fulfillment"]["expectations"]
    self.assertTrue(expectations)
    self.assertEqual(expectations[0]["destination"]["address_country"], "US")

  def test_webhook_order_address_new_address(self) -> None:
    """Test that webhook contains correct address when a new one is provided."""
    buyer_info = {"fullName": "John Doe", "email": "john.doe@example.com"}
    checkout_data = self.create_checkout_session(buyer=buyer_info)
    checkout_obj = fulfillment_resp.Checkout(**checkout_data)

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
    checkout_obj = fulfillment_resp.Checkout(**response.json())
    method = checkout_obj.fulfillment.root.methods[0]

    if method.groups and method.groups[0].options:
      option_id = method.groups[0].options[0].id
      # Select option
      fulfillment_payload["methods"][0]["groups"] = [
        {"selected_option_id": option_id}
      ]
      fulfillment_payload["methods"][0]["type"] = "shipping"
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
      (
        e
        for e in self.webhook_server.events
        if e["payload"]["order"]["id"] == order_id
      ),
      None,
    )
    self.assertIsNotNone(event)
    expectations = event["payload"]["order"]["fulfillment"]["expectations"]
    self.assertTrue(expectations)
    self.assertEqual(expectations[0]["destination"]["address_country"], "CA")
    self.assertEqual(
      expectations[0]["destination"]["street_address"], "Webhook St"
    )


if __name__ == "__main__":
  absltest.main()
