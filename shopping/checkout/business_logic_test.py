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

"""Business logic and totals tests for the UCP Shopping vertical."""

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout
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
from ucp_sdk.models.schemas.shopping.types import buyer_update_request
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})

# The release (ucp#451) that made buyer consent a map of reverse-DNS purposes,
# each confirmed with `granted` and `source`. Earlier releases send booleans.
CONSENT_PURPOSE_MAP_VERSION = "2026-08-25"


@requires_capability("dev.ucp.shopping.checkout")
class BusinessLogicTest(ShoppingIntegrationTestBase):
  """Tests for business logic and calculations.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - GET /checkout-sessions/{id}
  """

  def test_totals_calculation_on_create(self):
    """Test that totals are calculated correctly upon checkout creation.

    Given a request to create a checkout session with a specific item,
    When the checkout is created with an incorrect title/price in the request,
    Then the server should return a checkout where line item totals, subtotal,
    and grand total correctly reflect the database price, ignoring client
    input.
    """
    expected_price = self.fixture_ctx.get_test_price()

    # Create checkout (client cannot send title/price per schema). The server
    # should use the authoritative price from its DB (which matches our config).
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    # Verify Line Item Calculations
    line_item = checkout_obj.line_items[0]
    li_subtotal = next(
      (t.amount for t in line_item.totals if t.type == "subtotal"), 0
    )
    li_total = next(
      (t.amount for t in line_item.totals if t.type == "total"), 0
    )

    self.assertEqual(
      li_subtotal,
      expected_price,
      f"Line item subtotal should match DB price {expected_price}",
    )
    self.assertEqual(
      li_total,
      expected_price,
      f"Line item total should match DB price {expected_price}",
    )

    # Verify Totals Breakdown
    self.assert_totals_consistent(checkout_obj, expected_price)

  def test_totals_recalculation_on_update(self):
    """Test that totals are recalculated correctly upon checkout update.

    Given an existing checkout session with 1 item,
    When the line item quantity is updated to 2,
    Then the server should return the updated checkout with a total amount of
    2 * price.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    expected_price = self.fixture_ctx.get_test_price()

    # Update quantity to 2.
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
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

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    updated_checkout = checkout.Checkout(**response.json())
    expected_total = expected_price * 2
    self.assert_totals_consistent(updated_checkout, expected_total)

  @requires_capability("dev.ucp.shopping.buyer_consent")
  def test_buyer_consent(self):
    """Test that buyer consent is exchanged in the shape of the target release.

    From 2026-08-25 (ucp#451) consent is a map of reverse-DNS purposes that the
    business advertises and the platform confirms; see
    `_check_consent_advertise_and_confirm`.

    For 2026-04-08:
    Given a checkout creation payload including buyer consent preferences
    (marketing=True, analytics=False),
    When the checkout session is created,
    Then the returned checkout object should correctly reflect these consent
    values.
    """
    if self.ucp_version >= CONSENT_PURPOSE_MAP_VERSION:
      self._check_consent_advertise_and_confirm()
      return

    create_payload = self.create_checkout_payload()

    # Consent is sent as a plain mapping: in newer SDK releases
    # `buyer_consent.Consent` is a `TypeAliasType`
    # (dict[ReverseDomainName, ConsentPurpose]) and cannot be instantiated
    # with keyword arguments, so a dict keeps this test working across
    # SDK versions.
    consent_dict = {
      "marketing": True,
      "analytics": False,
      "sale_of_data": False,
    }

    create_payload_dict = create_payload.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    create_payload_dict["buyer"] = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
      "consent": consent_dict,
    }

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload_dict,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    checkout_id = checkout.Checkout(**response.json()).id

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")

    # buyer is types.buyer.Buyer, consent is in extra fields
    consent_data = getattr(checkout_obj.buyer, "consent", None)
    self.assertTrue(consent_data, "Consent info missing")

    if isinstance(consent_data, dict):
      self.assertTrue(
        consent_data.get("marketing"),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        consent_data.get("analytics"),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )
    else:
      self.assertTrue(
        getattr(consent_data, "marketing", False),
        f"Marketing consent not persisted. Resp: {consent_data}",
      )
      self.assertFalse(
        getattr(consent_data, "analytics", True),
        f"Analytics consent not persisted. Resp: {consent_data}",
      )

  def _check_consent_advertise_and_confirm(self):
    """Check the 2026-08-25 advertise and confirm flow for buyer consent.

    Given a checkout created with a buyer and no consent (requirement 3: a
    business ignores purposes it has not advertised, so a platform has nothing
    to send yet),
    When the platform confirms every advertised purpose and segment with
    `granted` and `source` (requirement 2), changing one purpose to a
    platform-sourced choice,
    Then the checkout still advertises the same purposes and segments in the
    response shape, and the changed purpose holds either that choice with
    `source: "platform"` or a business-sourced value.

    Whether a business must record a platform-sourced confirm is not settled
    by the extension (ucp#1113), so recording is not required here.
    """
    buyer = {
      "first_name": "Consent",
      "last_name": "Tester",
      "email": "consent@example.com",
    }
    created = self.create_checkout_session(
      buyer=buyer, select_fulfillment=False
    )
    advertised = (created.get("buyer") or {}).get("consent")
    if not advertised:
      self.skipTest(
        "The business advertises dev.ucp.shopping.buyer_consent but the"
        " checkout response carries no buyer.consent options to confirm."
      )
    self._assert_consent_advertised(advertised, "create response")

    # Echo every advertised key; the platform states a buyer choice for one.
    purpose = sorted(advertised)[0]
    choice = not advertised[purpose]["granted"]
    confirm = {
      key: self._consent_confirm_entry(value)
      for key, value in advertised.items()
    }
    confirm[purpose]["granted"] = choice
    confirm[purpose]["source"] = "platform"

    # checkout.json marks `id` and `currency` as `ucp_request: "omit"`, so
    # the update carries only fields a platform may send.
    checkout_id = created["id"]
    update_payload = {
      "line_items": [
        {
          "id": line_item["id"],
          "item": {"id": line_item["item"]["id"]},
          "quantity": line_item["quantity"],
        }
        for line_item in created["line_items"]
      ],
      "buyer": {**buyer, "consent": confirm},
    }
    if created.get("payment"):
      update_payload["payment"] = {
        "instruments": created["payment"].get("instruments") or []
      }
    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    current = (response.json().get("buyer") or {}).get("consent")
    self.assertTrue(
      current, "buyer.consent is missing after the platform confirmed it"
    )
    self._assert_consent_advertised(current, "read after confirm")

    self.assertEqual(
      set(current),
      set(advertised),
      "The advertised purposes changed after a confirm that echoed them",
    )
    for key, value in advertised.items():
      self.assertEqual(
        set(current[key].get("segments") or {}),
        set(value.get("segments") or {}),
        f"The advertised segments of {key} changed after a confirm that"
        " echoed them",
      )

    recorded = current[purpose]
    if recorded["source"] == "platform":
      self.assertEqual(
        recorded["granted"],
        choice,
        f"{purpose} reports source 'platform' but not the granted value the"
        f" platform confirmed. Resp: {recorded}",
      )

  def _assert_consent_advertised(self, consent, where):
    """Assert a consent map has the response shape of buyer_consent.json."""
    self.assertIsInstance(consent, dict, f"buyer.consent in {where}")
    for key, purpose in consent.items():
      self._assert_consent_value(purpose, f"{key} in {where}")
      if "segments" not in purpose:
        continue
      segments = purpose["segments"]
      self.assertIsInstance(
        segments, dict, f"{key}.segments in {where} is not an object"
      )
      for segment_key, segment in segments.items():
        self._assert_consent_value(segment, f"{segment_key} in {where}")

  def _assert_consent_value(self, value, label):
    """Assert one advertised purpose or segment carries its required fields."""
    self.assertIsInstance(value, dict, f"{label} is not an object")
    self.assertIsInstance(
      value.get("granted"), bool, f"{label} has no boolean granted"
    )
    self.assertIn(
      value.get("source"),
      ("business", "platform"),
      f"{label} has no valid source",
    )
    self.assertIsInstance(
      value.get("description"), str, f"{label} has no description"
    )

  @staticmethod
  def _consent_confirm_entry(advertised):
    """Echo an advertised purpose as a confirm: granted, source, segments."""
    entry = {"granted": advertised["granted"], "source": advertised["source"]}
    if advertised.get("segments"):
      entry["segments"] = {
        key: {"granted": segment["granted"], "source": segment["source"]}
        for key, segment in advertised["segments"].items()
      }
    return entry

  def test_buyer_info_persistence(self):
    """Test that buyer information is persisted on update.

    Given an existing checkout session,
    When the session is updated with new buyer details (email, name),
    Then the retrieved checkout session should reflect these updated buyer
    details.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update with buyer info
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
      buyer=buyer_update_request.BuyerUpdateRequest(
        email="test@example.com",
        first_name="Test",
        last_name="User",
      ),
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)

    # GET and verify
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_obj = checkout.Checkout(**response.json())
    self.assertTrue(checkout_obj.buyer, "Buyer info missing")
    self.assertEqual(
      checkout_obj.buyer.email, "test@example.com", "Email mismatch"
    )
    self.assertEqual(
      checkout_obj.buyer.first_name, "Test", "First name mismatch"
    )


if __name__ == "__main__":
  absltest.main()
