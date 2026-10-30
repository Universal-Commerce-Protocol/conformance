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
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)
from ucp_sdk.models.schemas.shopping.types import postal_address

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class FulfillmentTest(integration_test_utils.IntegrationTestBase):
  """Tests for fulfillment logic.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  """

  def _build_buyer_payload(
    self, customer: integration_test_utils.CustomerFixture
  ) -> dict[str, str]:
    """Build a create-checkout buyer payload from a customer fixture."""
    return {
      "fullName": customer.get("full_name", "Known Customer"),
      "email": customer["email"],
    }

  def _address_matches(self, destination: dict, address: dict) -> bool:
    """Check whether a returned destination carries the address content.

    Only the fields declared in the fixture address are compared; the
    destination id is server-assigned and never part of the match.
    """
    return all(
      destination.get(field) == value
      for field, value in address.items()
      if field != "id" and value is not None
    )

  def _find_matching_destination(
    self, destinations: list[dict], address: dict
  ) -> dict | None:
    """Find the destination whose content matches the given address."""
    return next(
      (d for d in destinations if self._address_matches(d, address)), None
    )

  def _get_free_shipping_option(self, options: list[dict]) -> dict | None:
    """Find a fulfillment option offered at a total cost of zero."""
    for option in options:
      if any(
        t["type"] == "total" and t["amount"] == 0
        for t in option.get("totals", [])
      ):
        return option
    return None

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
    # Use dynamic destination address from fixture_ctx or CSV fallback
    ctx = getattr(self, "fixture_ctx", None)
    dest_data = ctx.get_test_destination() if ctx else None
    if not dest_data:
      addr_csv = integration_test_utils.test_data.addresses[0]
      dest_data = {
        "street": addr_csv["street_address"],
        "city": addr_csv["city"],
        "state": addr_csv["state"],
        "postal_code": addr_csv["postal_code"],
        "country": addr_csv["country"],
      }
    address = postal_address.PostalAddress(
      full_name="John Doe",
      street_address=dest_data.get(
        "street_address", dest_data.get("street", "123 Market St")
      ),
      address_locality=dest_data.get(
        "locality", dest_data.get("city", "San Francisco")
      ),
      address_region=dest_data.get("region", dest_data.get("state", "CA")),
      postal_code=dest_data.get("postal_code", "94105"),
      address_country=dest_data.get(
        "address_country", dest_data.get("country", "US")
      ),
    )

    # Construct fulfillment payload
    address_data = address.model_dump(exclude_none=True)
    address_data["id"] = "dest_1"  # Mock ID matching the injected address

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
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
    methods = (
      response_json.get("fulfillment", {})
      .get("root", response_json.get("fulfillment", {}))
      .get("methods", [])
    )
    self.assertTrue(methods)
    method = methods[0]
    self.assertTrue(method.get("groups"))
    group = method["groups"][0]
    options = group.get("options", [])

    self.assertTrue(
      options,
      f"Fulfillment options not generated. Resp: {response_json}",
    )

    # 2. Select Option
    option_id = options[0]["id"]
    option_cost = next(
      (t["amount"] for t in options[0]["totals"] if t["type"] == "total"), 0
    )

    # Update payload to select the option
    # We must preserve the destination to keep options available
    fulfillment_payload["methods"][0]["groups"] = [
      {
        "id": group.get("id", "group_1"),
        "line_item_ids": group.get("line_item_ids", []),
        "selected_option_id": option_id,
      }
    ]

    response_json = self.update_checkout_session(
      checkout_with_options, fulfillment=fulfillment_payload
    )
    final_checkout = checkout.Checkout(**response_json)

    expected_total = (
      self.fixture_ctx.get_test_price() + option_cost
    )  # base price + shipping
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
    When the fulfillment address is updated to the domestic destination, then
    the option id declared for it in the fixtures is available.
    When the fulfillment address is updated to the international destination,
    then the option id declared for it in the fixtures is available.
    """
    dynamic = self.fixture_ctx.get_dynamic_fulfillment()
    if dynamic is None:
      self.skipTest(
        "No dynamic fulfillment cases configured in fixtures "
        "(test_fixtures.dynamic_fulfillment)."
      )

    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # 1. Update with the domestic destination
    domestic = dynamic["domestic"]
    domestic_dest = {
      "id": "dest_domestic",
      **domestic["destination"],
    }
    fulfillment_domestic = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [domestic_dest],
          "selected_destination_id": "dest_domestic",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_domestic
    )
    domestic_checkout = checkout.Checkout(**response_json)

    # Check that the declared domestic option is available
    options = domestic_checkout.model_extra["fulfillment"]["methods"][0][
      "groups"
    ][0]["options"]
    self.assertTrue(
      options
      and any(o["id"] == domestic["expected_option_id"] for o in options),
      "Expected domestic option "
      f"{domestic['expected_option_id']}, got {options}",
    )

    # 2. Update with the international destination
    international = dynamic["international"]
    international_dest = {
      "id": "dest_international",
      **international["destination"],
    }
    fulfillment_international = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [international_dest],
          "selected_destination_id": "dest_international",
        }
      ]
    }

    response_json = self.update_checkout_session(
      domestic_checkout, fulfillment=fulfillment_international
    )
    international_checkout = checkout.Checkout(**response_json)

    # Check that the declared international option is available
    options = international_checkout.model_extra["fulfillment"]["methods"][0][
      "groups"
    ][0]["options"]
    self.assertTrue(
      options
      and any(o["id"] == international["expected_option_id"] for o in options),
      "Expected international option "
      f"{international['expected_option_id']}, got {options}",
    )

  def test_unknown_customer_no_address(self) -> None:
    """Test that an unknown customer gets no automatic address injection."""
    # Create checkout with a randomized buyer email so it cannot collide
    # with a customer that the server under test happens to know.
    response_json = self.create_checkout_session(
      buyer={
        "fullName": "Unknown Person",
        "email": f"unknown-{uuid.uuid4().hex}@example.com",
      },
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Trigger fulfillment update (empty payload to trigger sync)
    response_json = self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": [checkout_obj.line_items[0].id],
          }
        ]
      },
    )
    updated_checkout = checkout.Checkout(**response_json)

    # Verify no destinations injected
    method = updated_checkout.model_extra["fulfillment"]["methods"][0]
    destinations = method.get("destinations")
    self.assertTrue(destinations is None or len(destinations) == 0)

  def test_known_customer_no_address(self) -> None:
    """Test that a known customer with no stored address gets no injection."""
    customer = self.fixture_ctx.get_known_customer_without_address()
    if not customer:
      self.skipTest(
        "No known customer without stored addresses configured in fixtures."
      )
    response_json = self.create_checkout_session(
      buyer=self._build_buyer_payload(customer),
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    response_json = self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": [checkout_obj.line_items[0].id],
          }
        ]
      },
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.model_extra["fulfillment"]["methods"][0]
    destinations = method.get("destinations")
    self.assertTrue(destinations is None or len(destinations) == 0)

  def test_known_customer_one_address(self) -> None:
    """Test that a known customer's stored addresses get injected."""
    customer = self.fixture_ctx.get_known_customer()
    if not customer or not customer.get("addresses"):
      self.skipTest(
        "No known customer with stored addresses configured in fixtures."
      )
    stored_addresses = customer["addresses"]

    response_json = self.create_checkout_session(
      buyer=self._build_buyer_payload(customer),
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    response_json = self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": [checkout_obj.line_items[0].id],
          }
        ]
      },
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.model_extra["fulfillment"]["methods"][0]
    destinations = method.get("destinations")
    self.assertTrue(destinations, "Stored addresses were not injected")
    self.assertGreaterEqual(len(destinations), len(stored_addresses))
    for address in stored_addresses:
      self.assertIsNotNone(
        self._find_matching_destination(destinations, address),
        f"Stored address {address} not found in destinations {destinations}",
      )

  def test_known_customer_multiple_addresses_selection(self) -> None:
    """Test selecting between multiple addresses for a known customer."""
    customer = self.fixture_ctx.get_known_customer()
    stored_addresses = customer.get("addresses", []) if customer else []
    if len(stored_addresses) < 2:
      self.skipTest(
        "Known customer with at least two stored addresses not configured."
      )

    response_json = self.create_checkout_session(
      buyer=self._build_buyer_payload(customer),
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Trigger injection
    response_json = self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": [checkout_obj.line_items[0].id],
          }
        ]
      },
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.model_extra["fulfillment"]["methods"][0]
    destinations = method.get("destinations") or []
    self.assertGreaterEqual(len(destinations), len(stored_addresses))

    # Find each stored address by content; ids are server-assigned.
    matches = []
    for address in stored_addresses:
      match = self._find_matching_destination(destinations, address)
      self.assertIsNotNone(
        match,
        f"Stored address {address} not found in destinations {destinations}",
      )
      matches.append(match)

    # Select the second stored address by its server-assigned id.
    target_address = stored_addresses[1]
    target_id = matches[1].get("id")
    self.assertTrue(target_id, f"Destination has no id: {matches[1]}")

    fulfillment_payload = {
      "methods": [
        {
          "id": method.get("id", "method_1"),
          "type": "shipping",
          "selected_destination_id": target_id,
          "line_item_ids": [checkout_obj.line_items[0].id],
        }
      ]
    }
    response_json = self.update_checkout_session(
      updated_checkout, fulfillment=fulfillment_payload
    )
    final_checkout = checkout.Checkout(**response_json)

    # Verify selection in hierarchical model
    self.assertEqual(
      final_checkout.model_extra["fulfillment"]["methods"][0][
        "selected_destination_id"
      ],
      target_id,
    )

    # Verify the selected destination carries the expected address content
    method = final_checkout.model_extra["fulfillment"]["methods"][0]
    selected_dest = next(
      d for d in method["destinations"] if d["id"] == target_id
    )
    self.assertTrue(
      self._address_matches(selected_dest, target_address),
      f"Selected destination {selected_dest} does not match {target_address}",
    )

  def test_known_customer_new_address(self) -> None:
    """Test that providing a new address works for a known customer."""
    customer = self.fixture_ctx.get_known_customer()
    if not customer:
      self.skipTest("No known customer configured in fixtures.")

    response_json = self.create_checkout_session(
      buyer=self._build_buyer_payload(customer)
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Provide a new explicit destination, built from the configured
    # shippable test destination rather than a hardcoded locale. The
    # client-side id is randomized so reruns cannot collide with state
    # the server persisted for this customer earlier.
    dest_data = self.fixture_ctx.get_test_destination()
    new_dest_id = f"dest_new_{uuid.uuid4().hex[:12]}"
    new_address = {
      "id": new_dest_id,
      "address_country": dest_data["address_country"],
      "postal_code": dest_data["postal_code"],
      "street_address": dest_data["street_address"],
    }
    if any(
      self._address_matches(new_address, stored)
      for stored in customer.get("addresses", [])
    ):
      self.skipTest("Configured test destination duplicates a stored address.")

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [new_address],
          "selected_destination_id": new_dest_id,
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.model_extra["fulfillment"]["methods"][0]

    # Should see the new address (and potentially the injected ones if the
    # server merges them). The server returns a union of known + provided.
    # The server may keep the client-supplied id or canonicalize the
    # destination to an id of its own (e.g. when it already stores
    # identical content for this customer), so match on content.
    destinations = method.get("destinations") or []
    self.assertGreaterEqual(len(destinations), 1)
    provided_content = {k: v for k, v in new_address.items() if k != "id"}
    match = self._find_matching_destination(destinations, provided_content)
    self.assertIsNotNone(
      match,
      f"Provided address {provided_content} not found in destinations"
      f" {destinations}",
    )

    # The requested selection should resolve to that destination — either
    # under the client-supplied id or the server's canonical id for it.
    self.assertIn(
      method.get("selected_destination_id"),
      {new_dest_id, match.get("id")},
      "Selection does not resolve to the provided destination",
    )

    # And the server should compute options for the provided destination
    group = method["groups"][0]
    self.assertTrue(
      group.get("options"),
      f"No fulfillment options generated for new destination: {group}",
    )

  def test_known_user_existing_address_reuse(self) -> None:
    """Test that an existing address is reused (same ID returned).

    Given a known user with existing addresses in the database,
    When an update provides an address matching an existing one (content-wise),
    Then the server should reuse the existing address ID.
    """
    customer = self.fixture_ctx.get_known_customer()
    if not customer or not customer.get("addresses"):
      self.skipTest(
        "No known customer with stored addresses configured in fixtures."
      )
    stored_address = customer["addresses"][0]

    response_json = self.create_checkout_session(
      buyer=self._build_buyer_payload(customer),
      select_fulfillment=False,
    )
    checkout_obj = checkout.Checkout(**response_json)

    # Trigger injection to learn the server-assigned id of the stored address
    response_json = self.update_checkout_session(
      checkout_obj,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": [checkout_obj.line_items[0].id],
          }
        ]
      },
    )
    injected_checkout = checkout.Checkout(**response_json)
    method = injected_checkout.model_extra["fulfillment"]["methods"][0]
    injected = self._find_matching_destination(
      method.get("destinations") or [], stored_address
    )
    self.assertIsNotNone(
      injected, f"Stored address {stored_address} was not injected"
    )
    injected_id = injected.get("id")
    self.assertTrue(injected_id, f"Injected destination has no id: {injected}")

    # Send the same address content back without an ID
    matching_address = {k: v for k, v in stored_address.items() if k != "id"}
    matching_address["id"] = ""

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [matching_address],
        }
      ]
    }

    response_json = self.update_checkout_session(
      injected_checkout, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    method = updated_checkout.model_extra["fulfillment"]["methods"][0]
    self.assertIsNotNone(method["destinations"])
    self.assertGreaterEqual(len(method["destinations"]), 1)

    # The matching content should still be served under the existing id
    match_ids = [
      d.get("id")
      for d in method["destinations"]
      if self._address_matches(d, stored_address)
    ]
    self.assertIn(
      injected_id,
      match_ids,
      "Resubmitted stored address was not reused under its existing id",
    )

  def test_free_shipping_on_expensive_order(self) -> None:
    """Test that free shipping is offered above the configured threshold."""
    threshold = self.fixture_ctx.get_free_shipping_min_subtotal()
    if threshold is None:
      self.skipTest(
        "No free-shipping subtotal threshold configured in fixtures."
      )

    # Order enough items to push the subtotal strictly above the threshold.
    price = self.fixture_ctx.get_test_price()
    quantity = threshold // price + 1
    response_json = self.create_checkout_session(quantity=quantity)
    checkout_obj = checkout.Checkout(**response_json)

    dest_data = self.fixture_ctx.get_test_destination()
    address = {
      "id": "dest_ship",
      "address_country": dest_data["address_country"],
      "postal_code": dest_data["postal_code"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [address],
          "selected_destination_id": "dest_ship",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    options = updated_checkout.model_extra["fulfillment"]["methods"][0][
      "groups"
    ][0]["options"]

    # A zero-cost option must be offered; its id and title are the
    # server's own to choose.
    free_shipping_option = self._get_free_shipping_option(options)
    self.assertIsNotNone(
      free_shipping_option,
      "No zero-cost fulfillment option offered above the configured"
      f" free-shipping threshold. Options: {options}",
    )

  def test_free_shipping_for_specific_item(self) -> None:
    """Test that free shipping is offered for eligible items."""
    eligible_sku = self.fixture_ctx.get_free_shipping_item_sku()
    if eligible_sku is None:
      self.skipTest(
        "No free-shipping-eligible item SKU configured in fixtures."
      )

    response_json = self.create_checkout_session(item_id=eligible_sku)
    checkout_obj = checkout.Checkout(**response_json)

    dest_data = self.fixture_ctx.get_test_destination()
    address = {
      "id": "dest_ship",
      "address_country": dest_data["address_country"],
      "postal_code": dest_data["postal_code"],
    }

    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [address],
          "selected_destination_id": "dest_ship",
        }
      ]
    }

    response_json = self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )
    updated_checkout = checkout.Checkout(**response_json)

    options = updated_checkout.model_extra["fulfillment"]["methods"][0][
      "groups"
    ][0]["options"]

    # A zero-cost option must be offered; its id and title are the
    # server's own to choose.
    free_shipping_option = self._get_free_shipping_option(options)
    self.assertIsNotNone(
      free_shipping_option,
      "No zero-cost fulfillment option offered for the configured"
      f" free-shipping-eligible item. Options: {options}",
    )


if __name__ == "__main__":
  absltest.main()
