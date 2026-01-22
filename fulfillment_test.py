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

"""Fulfillment tests for the UCP SDK Server."""

import uuid
from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import postal_address

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class FulfillmentTest(integration_test_utils.IntegrationTestBase):
  """Tests for fulfillment logic.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  """

  def test_fulfillment_flow(self) -> None:
    """Test the complete fulfillment selection flow.

    Given a newly created checkout session,
    When a fulfillment address is added and a fulfillment option is selected,
    Then the checkout totals should update to include the selected fulfillment
    cost.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # 1. Update with Fulfillment Address
    # We explicitly construct the extended fulfillment payload here because
    # the helper method assumes a flatter, older structure.
    # Use helper to get a valid address from CSV
    addr_data = integration_test_utils.test_data.addresses[0]
    address = postal_address.PostalAddress(
      full_name="John Doe",
      street_address=addr_data["street_address"],
      address_locality=addr_data["city"],
      address_region=addr_data["state"],
      postal_code=addr_data["postal_code"],
      address_country=addr_data["country"],
    )

    # Construct fulfillment payload
    address_data = address.model_dump(exclude_none=True)
    address_data["id"] = "dest_1"  # Mock ID matching the injected address

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [address_data],
          "selected_destination_id": "dest_1",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    checkout_with_options = checkout.Checkout(**response_json)

    # Verify options are generated in the nested structure
    # checkout.fulfillment.root.methods[0].groups[0].options
    self.assertIsNotNone(checkout_with_options.fulfillment)
    self.assertNotEmpty(checkout_with_options.fulfillment.root.methods)
    method = checkout_with_options.fulfillment.root.methods[0]
    self.assertNotEmpty(method.groups)
    group = method.groups[0]
    options = group.options

    self.assertTrue(
      options,
      f"Fulfillment options not generated. Resp: {response_json}",
    )

    # 2. Select Option
    option_id = options[0].id
    option_cost = next(
      (t.amount for t in options[0].totals if t.type == "total"), 0
    )

    # Update payload to select the option
    # We must preserve the destination to keep options available
    fulfillment_payload["methods"][0]["groups"] = [
      {"selected_option_id": option_id}
    ]

    response_json = self.update_checkout_session(
      checkout_with_options, fulfillment=fulfillment_payload
    )
    final_checkout = checkout.Checkout(**response_json)

    expected_total = 3500 + option_cost  # Base 3500 + shipping
    total_obj = next(
      (t for t in final_checkout.totals if t.type == "total"), None
    )
    self.assertIsNotNone(total_obj, "Total object missing")
    self.assertEqual(
      total_obj.amount,
      expected_total,
      msg=(
        f"Total not updated correctly. Expected {expected_total}, got"
        f" {total_obj.amount}"
      ),
    )

  def test_dynamic_fulfillment(self) -> None:
    """Test that fulfillment options are dynamically generated based on address.

    Given a checkout session,
    When the fulfillment address is updated to a US address, then a US-specific
    option is available.
    When the fulfillment address is updated to a CA address, then a CA-specific
    option is available.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # 1. Update with US Address
    # addr_1 is US in CSV
    addr_data = integration_test_utils.test_data.addresses[0]
    us_address = {
      "id": "dest_us",
      "address_country": addr_data["country"],
      "postal_code": addr_data["postal_code"],
    }

    fulfillment_us = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [us_address],
          "selected_destination_id": "dest_us",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_us
    )
    us_checkout = checkout.Checkout(**response_json)

    # Check for US options
    options = us_checkout.fulfillment.root.methods[0].groups[0].options
    self.assertTrue(
      options and any(o.id == "exp-ship-us" for o in options),
      f"Expected US express option, got {options}",
    )

    # 2. Update with CA Address
    ca_address = {
      "id": "dest_ca",
      "address_country": "CA",
      "postal_code": "M5V 2H1",
    }

    fulfillment_ca = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [ca_address],
          "selected_destination_id": "dest_ca",
        }
      ]
    }

    response_json = self.update_checkout_session(
      us_checkout, fulfillment=fulfillment_ca
    )
    ca_checkout = checkout.Checkout(**response_json)

    # Check for International options
    options = ca_checkout.fulfillment.root.methods[0].groups[0].options
    self.assertTrue(
      options and any(o.id == "exp-ship-intl" for o in options),
      f"Expected Intl express option, got {options}",
    )

  def test_unknown_customer_no_address(self) -> None:
    """Test that an unknown customer gets no automatic address injection."""
    # Create checkout with unknown buyer
    response_json = self.create_checkout_session(
      buyer={"fullName": "Unknown Person", "email": "unknown@example.com"},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Trigger fulfillment update (empty payload to trigger sync)
    response_json = self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )
    updated_checkout = checkout.Checkout(**response_json)

    # Verify no destinations injected
    method = updated_checkout.fulfillment.root.methods[0]
    self.assertIsNone(method.destinations)

  def test_known_customer_no_address(self) -> None:
    """Test that a known customer with no stored address gets no injection."""
    # Jane Doe (cust_3) has no address in CSV
    response_json = self.create_checkout_session(
      buyer={"fullName": "Jane Doe", "email": "jane.doe@example.com"},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]
    self.assertIsNone(method.destinations)

  def test_known_customer_one_address(self) -> None:
    """Test that a known customer with an address gets it injected."""
    # John Doe (cust_1) has an address
    response_json = self.create_checkout_session(
      buyer={"fullName": "John Doe", "email": "john.doe@example.com"},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]
    self.assertIsNotNone(method.destinations)
    # He has at least 2 addresses
    self.assertGreaterEqual(len(method.destinations), 2)
    self.assertEqual(method.destinations[0].root.address_country, "US")

  def test_known_customer_multiple_addresses_selection(self) -> None:
    """Test selecting between multiple addresses for a known customer."""
    # John Doe has 2 addresses: addr_1 and addr_2
    response_json = self.create_checkout_session(
      buyer={"fullName": "John Doe", "email": "john.doe@example.com"},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Trigger injection
    response_json = self.update_checkout_session(
      checkout_obj, fulfillment={"methods": [{"type": "shipping"}]}
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]
    destinations = method.destinations
    self.assertGreaterEqual(len(destinations), 2)

    # Verify IDs (assuming deterministic order or check existence)
    dest_ids = [d.root.id for d in destinations]
    self.assertIn("addr_1", dest_ids)
    self.assertIn("addr_2", dest_ids)

    # Select addr_2
    fulfillment_payload = {
      "methods": [{"type": "shipping", "selected_destination_id": "addr_2"}]
    }
    response_json = self.update_checkout_session(
      updated_checkout, fulfillment=fulfillment_payload
    )
    final_checkout = checkout.Checkout(**response_json)

    # Verify selection in hierarchical model
    self.assertEqual(
      final_checkout.fulfillment.root.methods[0].selected_destination_id,
      "addr_2",
    )

    # Verify selection details from the selected destination
    method = final_checkout.fulfillment.root.methods[0]
    selected_dest = next(
      d for d in method.destinations if d.root.id == "addr_2"
    )
    self.assertEqual(
      selected_dest.root.street_address,
      "456 Oak Ave",
    )
    self.assertEqual(selected_dest.root.postal_code, "10012")

  def test_known_customer_new_address(self) -> None:
    """Test that providing a new address works for a known customer."""
    response_json = self.create_checkout_session(
      buyer={"fullName": "John Doe", "email": "john.doe@example.com"}
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Provide a new explicit destination
    new_address = {
      "id": "dest_new",
      "address_country": "CA",
      "postal_code": "M5V 2H1",
      "street_address": "123 New St",
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [new_address],
          "selected_destination_id": "dest_new",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]

    # Should see the new address (and potentially the injected ones if the
    # server merges them). The server returns a union of known + provided.
    self.assertGreaterEqual(len(method.destinations), 1)
    dest_ids = [d.root.id for d in method.destinations]
    self.assertIn("dest_new", dest_ids)

    # And we should get options calculated for CA
    group = method.groups[0]
    self.assertTrue(any(o.id == "exp-ship-intl" for o in group.options))

  def test_new_user_new_address_persistence(self) -> None:
    """Test that a new address for a new user is persisted and ID generated.

    Given a checkout for a new (unknown) user,
    When a new fulfillment address is provided in an update,
    Then the address should be saved, assigned an ID, and reused for subsequent
    checkouts by the same user.
    """
    email = f"new.user.{uuid.uuid4()}@example.com"
    response_json = self.create_checkout_session(
      buyer={"fullName": "New User", "email": email},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # New address without ID
    new_address = {
      "street_address": "789 Pine St",
      "address_locality": "Villagetown",
      "address_region": "NY",
      "postal_code": "10001",
      "address_country": "US",
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [new_address],
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]
    self.assertIsNotNone(method.destinations)
    self.assertLen(method.destinations, 1)

    # ID should be generated
    generated_id = method.destinations[0].root.id
    self.assertTrue(generated_id, "ID should be generated for new address")

    # Verify persistence by creating another checkout for same user
    # and checking if address is injected
    response_json_2 = self.create_checkout_session(
      buyer={"fullName": "New User", "email": email},
      select_fulfillment=False,
    )
    checkout_obj_2 = checkout.Checkout(**response_json_2)
    response_json_2 = self.update_checkout_session(
      checkout_obj_2, fulfillment={"methods": [{"type": "shipping"}]}
    )
    updated_checkout_2 = checkout.Checkout(**response_json_2)
    method_2 = updated_checkout_2.fulfillment.root.methods[0]

    self.assertIsNotNone(method_2.destinations)
    # Could be more if tests re-run, but should contain our ID
    dest_ids = [d.root.id for d in method_2.destinations]
    self.assertIn(generated_id, dest_ids)

  def test_known_user_existing_address_reuse(self) -> None:
    """Test that an existing address is reused (same ID returned).

    Given a known user with existing addresses in the database,
    When an update provides an address matching an existing one (content-wise),
    Then the server should reuse the existing address ID.
    """
    # John Doe has addr_1 (123 Main St, Springfield, IL, 62704, US)
    response_json = self.create_checkout_session(
      buyer={"fullName": "John Doe", "email": "john.doe@example.com"},
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Send address matching addr_1 but without ID
    matching_address = {
      "street_address": "123 Main St",
      "address_locality": "Springfield",
      "address_region": "IL",
      "postal_code": "62704",
      "address_country": "US",
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [matching_address],
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.fulfillment.root.methods[0]
    self.assertIsNotNone(method.destinations)
    self.assertGreaterEqual(len(method.destinations), 1)

    # Should reuse addr_1
    dest_ids = [d.root.id for d in method.destinations]
    self.assertIn("addr_1", dest_ids)

  def test_free_shipping_on_expensive_order(self) -> None:
    """Test that free shipping is offered for orders over $100."""
    # Base price is 3500. Quantity 3 = 10500 > 10000 threshold.
    response_json = self.create_checkout_session(quantity=3)
    checkout_obj = checkout.Checkout(**response_json)

    # addr_1 is US in CSV
    addr_data = integration_test_utils.test_data.addresses[0]
    address = {
      "id": "dest_us",
      "address_country": addr_data["country"],
      "postal_code": addr_data["postal_code"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [address],
          "selected_destination_id": "dest_us",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    options = updated_checkout.fulfillment.root.methods[0].groups[0].options
    free_shipping_option = next(
      (o for o in options if o.id == "std-ship"), None
    )

    self.assertIsNotNone(free_shipping_option)
    opt_total = next(
      (t.amount for t in free_shipping_option.totals if t.type == "total"),
      None,
    )
    self.assertEqual(opt_total, 0)
    self.assertIn("Free", free_shipping_option.title)

  def test_free_shipping_for_specific_item(self) -> None:
    """Test that free shipping is offered for eligible items."""
    # 'bouquet_roses' is eligible for free shipping
    response_json = self.create_checkout_session(item_id="bouquet_roses")
    checkout_obj = checkout.Checkout(**response_json)

    # addr_1 is US in CSV
    addr_data = integration_test_utils.test_data.addresses[0]
    address = {
      "id": "dest_us",
      "address_country": addr_data["country"],
      "postal_code": addr_data["postal_code"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "destinations": [address],
          "selected_destination_id": "dest_us",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    options = updated_checkout.fulfillment.root.methods[0].groups[0].options
    free_shipping_option = next(
      (o for o in options if o.id == "std-ship"), None
    )

    self.assertIsNotNone(free_shipping_option)
    opt_total = next(
      (t.amount for t in free_shipping_option.totals if t.type == "total"),
      None,
    )
    self.assertEqual(opt_total, 0)
    self.assertIn("Free", free_shipping_option.title)


if __name__ == "__main__":
  absltest.main()
