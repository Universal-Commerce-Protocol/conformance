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

"""Totals-integrity structural tests for the UCP SDK Server.

These tests assert structural invariants of the checkout ``totals`` cost
breakdown that the spec mandates for every checkout response, independent
of the specific amounts a business computes:

- Exactly one ``subtotal`` entry and exactly one ``total`` entry
  (Totals: "MUST contain exactly one subtotal and one total entry").
- Every entry carries both a ``type`` and an ``amount``.
- Additive charge entries (``subtotal``, ``fulfillment``, ``tax``, ``fee``)
  are non-negative; a ``discount`` entry is negative
  (SignedAmount: "the sign is intrinsic ... discounts are negative,
  charges are positive").

Assertions operate on the raw wire payload (``response.json()``) rather
than the parsed SDK model, so they validate the on-the-wire contract
directly and do not presume a client uses the SDK's coercing model.
"""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping.payment import Payment

# Rebuild models to resolve forward references (needed to parse a full
# checkout response before driving a discount update).
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})

# Entry types whose amount is an additive charge and MUST be non-negative
# (total.json: minimum 0 for these types).
_ADDITIVE_TYPES = frozenset({"subtotal", "fulfillment", "tax", "fee"})

# Entry types whose amount MUST be negative (total.json: exclusiveMaximum 0).
_NEGATIVE_TYPES = frozenset({"discount", "items_discount"})

# A percentage discount code recognized by the reference data set. If the
# server under test does not recognize it, the discount-sign test skips.
_DISCOUNT_CODE = "10OFF"


class TotalsTest(integration_test_utils.IntegrationTestBase):
  """Structural-integrity tests for the checkout ``totals`` breakdown.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  """

  def _create_checkout_totals(self) -> list[dict]:
    """Create a fulfillment-selected checkout and return its raw totals.

    Returns:
        The ``totals`` list from the raw checkout response JSON.

    """
    checkout_json = self.create_checkout_session(select_fulfillment=True)
    totals = checkout_json.get("totals")
    self.assertIsInstance(
      totals, list, "Checkout response must include a totals[] list."
    )
    self.assertTrue(totals, "Checkout totals[] must not be empty.")
    return totals

  def _discounted_checkout_totals(self) -> list[dict]:
    """Create a checkout, apply a discount, and return its raw totals.

    Returns:
        The ``totals`` list from the raw checkout response JSON after a
        discount code has been applied.

    """
    checkout_json = self.create_checkout_session(select_fulfillment=True)
    checkout_obj = checkout.Checkout(**checkout_json)
    updated = self.update_checkout_session(
      checkout_obj, discounts={"codes": [_DISCOUNT_CODE]}
    )
    totals = updated.get("totals")
    self.assertIsInstance(
      totals, list, "Checkout response must include a totals[] list."
    )
    return totals

  def test_single_subtotal_entry(self):
    """Test that totals contain exactly one subtotal entry.

    Given a checkout session,
    When its totals breakdown is returned,
    Then exactly one entry of type 'subtotal' must be present.
    """
    totals = self._create_checkout_totals()
    subtotals = [t for t in totals if t.get("type") == "subtotal"]
    self.assertEqual(
      len(subtotals),
      1,
      f"totals[] must contain exactly one 'subtotal' entry, found {totals}.",
    )

  def test_single_total_entry(self):
    """Test that totals contain exactly one total entry.

    Given a checkout session,
    When its totals breakdown is returned,
    Then exactly one entry of type 'total' must be present.
    """
    totals = self._create_checkout_totals()
    grand_totals = [t for t in totals if t.get("type") == "total"]
    self.assertEqual(
      len(grand_totals),
      1,
      f"totals[] must contain exactly one 'total' entry, found {totals}.",
    )

  def test_entries_have_type_and_amount(self):
    """Test that every totals entry carries a type and an amount.

    Given a checkout session,
    When its totals breakdown is returned,
    Then every entry must carry a non-empty string 'type' and an integer
    'amount' (minor units) on the wire.
    """
    totals = self._create_checkout_totals()
    for entry in totals:
      self.assertIn("type", entry, f"totals entry missing 'type': {entry}.")
      self.assertIn("amount", entry, f"totals entry missing 'amount': {entry}.")
      self.assertIsInstance(
        entry["type"], str, f"totals 'type' must be a string: {entry}."
      )
      self.assertTrue(
        entry["type"], f"totals 'type' must be non-empty: {entry}."
      )
      self.assertIsInstance(
        entry["amount"],
        int,
        f"totals 'amount' must be an integer minor-unit value: {entry}.",
      )
      self.assertNotIsInstance(
        entry["amount"],
        bool,
        f"totals 'amount' must be an integer, not a boolean: {entry}.",
      )

  def test_additive_entries_non_negative(self):
    """Test that additive charge entries have a non-negative amount.

    Given a checkout session,
    When its totals breakdown is returned,
    Then every additive charge entry (subtotal, fulfillment, tax, fee)
    that is present must have an amount >= 0.
    """
    totals = self._create_checkout_totals()
    additive = [t for t in totals if t.get("type") in _ADDITIVE_TYPES]
    self.assertTrue(
      additive,
      "Expected at least one additive charge entry (e.g. subtotal).",
    )
    for entry in additive:
      self.assertGreaterEqual(
        entry["amount"],
        0,
        f"Additive '{entry.get('type')}' entry must be >= 0: {entry}.",
      )

  def test_discount_entry_is_negative(self):
    """Test that a discount totals entry has a negative amount.

    Given a checkout session with a valid discount applied,
    When its totals breakdown is returned,
    Then every discount entry (type 'discount' or 'items_discount') must
    have an amount < 0 (the sign is intrinsic to the value). If the server
    emits no such entry, there is nothing to assert and the test is skipped.
    """
    totals = self._discounted_checkout_totals()
    discounts = [t for t in totals if t.get("type") in _NEGATIVE_TYPES]
    if not discounts:
      self.skipTest(
        "Server emitted no discount entry; sign invariant not applicable."
      )
    for entry in discounts:
      self.assertLess(
        entry["amount"],
        0,
        f"A '{entry.get('type')}' totals entry must be negative: {entry}.",
      )


if __name__ == "__main__":
  absltest.main()
