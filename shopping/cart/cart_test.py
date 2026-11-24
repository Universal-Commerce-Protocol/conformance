# Copyright 2026 UCP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Conformance tests for the cart capability (cart.md).

The suite has no cart coverage today, while the capability defines normative
business obligations that are observable on the wire. This module asserts the
ones that are stated as MUST and are not covered elsewhere:

  cart.md "Cart-to-Checkout Conversion"
                   the business uses the cart contents and ignores overlapping
                   fields in the checkout payload
  cart.md "Idempotent conversion"
                   a second conversion of the same cart returns the existing
                   incomplete checkout rather than creating a new one
  cart.md "Cancel Cart"
                   cancel returns the cart state before deletion
  discount.md "Cart-to-checkout continuity"
                   discount codes applied to a cart carry forward to the
                   checkout the cart converts into

Sections are named rather than cited by line, so the references survive
edits to the specification.

Server-agnostic by construction. Line items are compared by quantity summed
per item id rather than by server-assigned identifiers; the conversion
identity check compares checkout ids rather than totals arithmetic; both 200 and
201 are accepted where the specification does not pin a status; and the discount
assertion accepts any of the three wire signals a conformant business may use.

Not asserted here, deliberately. The full replacement rule in cart.md
"Update Cart" binds the platform rather than the business, so it is not a
business obligation this suite can hold a merchant to. The sentence in
"Cancel Cart" about subsequent operations returning not_found is a SHOULD, so
the post-cancel status is not asserted.
"""

import uuid

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase

_CART_CAPABILITY = "dev.ucp.shopping.cart"
_DISCOUNT_CAPABILITY = "dev.ucp.shopping.discount"


@requires_capability(_CART_CAPABILITY)
class CartTest(ShoppingIntegrationTestBase):
  """Cart capability conformance (cart.md).

  `@requires_capability` skips the suite when the business does not advertise
  Cart, before the mock agent server starts, and accumulates with whatever the
  base class requires.
  """

  def setUp(self) -> None:
    """Resolve the fixture SKU; capability gating belongs to the decorator."""
    super().setUp()
    self._sku = self.fixture_ctx.get_test_sku()

  def _advertised_capabilities(self) -> dict:
    """Return the advertised capability map, keyed by capability name.

    Prefers the profile the framework already fetched, so no second request is
    made, and falls back to `self.discovery_url` rather than a hardcoded
    `/.well-known/ucp` so `--discovery_url` is respected.
    """
    raw = None
    if self.server_profile is not None:
      raw = self.server_profile.raw
    if raw is None:
      resp = self.client.get(self.discovery_url)
      self.assert_response_status(resp, 200)
      raw = resp.json()
    ucp = raw.get("ucp", raw)
    caps = ucp.get("capabilities") or {}
    if isinstance(caps, dict):
      return caps
    return {c.get("name"): c for c in caps if isinstance(c, dict)}

  def _discount_extends_cart(self) -> bool:
    """Return True if the discount capability declares it extends cart."""
    entry = self._advertised_capabilities().get(_DISCOUNT_CAPABILITY)
    if entry is None:
      return False
    entries = entry if isinstance(entry, list) else [entry]
    for item in entries:
      if not isinstance(item, dict):
        continue
      extends = item.get("extends")
      if extends is None:
        continue
      if isinstance(extends, str):
        extends = [extends]
      if _CART_CAPABILITY in extends:
        return True
    return False

  # ── drivers ────────────────────────────────────────────────────────────
  def _create_cart(self, quantity: int = 2, **extra) -> dict:
    """Create a cart holding the configured test item; return the raw dict."""
    payload = {
      "line_items": [{"item": {"id": self._sku}, "quantity": quantity}]
    }
    payload.update(extra)
    resp = self.client.post(
      self.get_shopping_url("/carts"),
      json=payload,
      headers=self.get_headers(),
    )
    self.assertIn(
      resp.status_code,
      (200, 201),
      f"cart creation failed: {resp.status_code} {resp.text}",
    )
    return resp.json()

  def _convert(self, cart_id: str, **extra) -> dict:
    """Convert a cart into a checkout; return the raw checkout dict."""
    payload = {"cart_id": cart_id}
    payload.update(extra)
    resp = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=payload,
      headers=self.get_headers(idempotency_key=str(uuid.uuid4())),
    )
    self.assertIn(
      resp.status_code,
      (200, 201),
      f"cart conversion failed: {resp.status_code} {resp.text}",
    )
    return resp.json()

  @staticmethod
  def _item_bag(raw: dict) -> list:
    """Return the quantity per item id, summed across however many lines.

    The same contents may be expressed as one line of quantity two or as two
    lines of quantity one, and a business may expand bundle children only at
    checkout, so quantities are summed per item id and lines that name a
    parent are left out. Comparing contents this way still catches a
    business that takes its quantities from the request payload.
    """
    totals: dict = {}
    for li in raw.get("line_items") or []:
      if li.get("parent_id"):
        continue
      item_id = (li.get("item") or {}).get("id")
      totals[item_id] = totals.get(item_id, 0) + (li.get("quantity") or 0)
    return sorted(totals.items())

  # ── cart.md "Cart-to-Checkout Conversion" ──────────────────────────────
  def test_conversion_uses_cart_contents_over_payload(self) -> None:
    """Cart contents win over overlapping fields in the checkout payload."""
    cart = self._create_cart(quantity=2)
    conflicting = [{"item": {"id": self._sku}, "quantity": 9}]

    checkout = self._convert(cart["id"], line_items=conflicting)

    self.assertEqual(
      self._item_bag(checkout),
      self._item_bag(cart),
      "checkout line items must come from the cart, not the request payload",
    )

  # ── cart.md "Idempotent conversion" ────────────────────────────────────
  def test_second_conversion_returns_the_existing_checkout(self) -> None:
    """A cart with an incomplete checkout converts to that same checkout."""
    cart = self._create_cart()

    first = self._convert(cart["id"])
    second = self._convert(cart["id"])

    self.assertEqual(
      second.get("id"),
      first.get("id"),
      "a second conversion must return the existing incomplete checkout",
    )

  # ── cart.md "Cancel Cart" ──────────────────────────────────────────────
  def test_cancel_returns_the_cart_state_before_deletion(self) -> None:
    """Cancelling a cart returns the cart as it was before deletion."""
    cart = self._create_cart(quantity=3)

    resp = self.client.post(
      self.get_shopping_url(f"/carts/{cart['id']}/cancel"),
      headers=self.get_headers(),
    )
    self.assertIn(
      resp.status_code,
      (200, 201),
      f"cart cancel failed: {resp.status_code} {resp.text}",
    )
    cancelled = resp.json()

    self.assertEqual(
      cancelled.get("id"),
      cart.get("id"),
      "cancel must return the cancelled cart, identified by its own id",
    )
    self.assertEqual(
      self._item_bag(cancelled),
      self._item_bag(cart),
      "cancel must return the cart state as it was before deletion",
    )

  # ── discount.md "Cart-to-checkout continuity" ──────────────────────────
  def test_cart_discount_codes_carry_forward_to_checkout(self) -> None:
    """Discount codes applied to a cart survive conversion to a checkout."""
    if not self._discount_extends_cart():
      self.skipTest(
        f"{_DISCOUNT_CAPABILITY} does not declare it extends "
        f"{_CART_CAPABILITY}; skipping"
      )
    code = self.fixture_ctx.get_test_discount_code()
    cart = self._create_cart(discounts={"codes": [code]})
    if not self._discount_applied(cart, code):
      self.skipTest(f"discount code {code} did not apply to the cart; skipping")

    checkout = self._convert(cart["id"])

    # The obligation is that the code carries forward. A business that can no
    # longer apply it SHOULD report that in messages[], so naming the code is
    # enough on its own; otherwise the discount it caused must still show, by
    # at least as much as on the cart, so that an unrelated discount cannot
    # stand in for the one under test.
    carried = self._discount_named(checkout, code) or (
      self._discount_total(cart) < 0
      and self._discount_total(checkout) <= self._discount_total(cart)
    )
    self.assertTrue(
      carried,
      "a discount applied to the cart must carry forward to the checkout",
    )

  @staticmethod
  def _discount_total(raw: dict) -> int:
    """Return the sum of the negative discount entries in totals[]."""
    return sum(
      t.get("amount") or 0
      for t in (raw.get("totals") or [])
      # discount.md "Impact on Line Items and Totals": a code allocated to
      # line items appears as `items_discount`, not `discount`, so a suite
      # that only counted `discount` missed the line-item allocation case.
      if t.get("type") in ("discount", "items_discount")
      and (t.get("amount") or 0) < 0
    )

  @staticmethod
  def _discount_named(raw: dict, code: str) -> bool:
    """Return True if the response names the code, in codes or in applied."""
    discounts = raw.get("discounts") or {}
    codes = [c.casefold() for c in (discounts.get("codes") or [])]
    if code.casefold() in codes:
      return True
    return any(
      (a.get("code") or "").casefold() == code.casefold()
      for a in (discounts.get("applied") or [])
      if isinstance(a, dict)
    )

  def _discount_applied(self, raw: dict, code: str) -> bool:
    """Return True if the code visibly took effect, not merely came back.

    A business may return a code it received without applying it, so the
    echo alone proves only that the code was carried. Application needs an
    applied entry naming the code, or a discount in totals[]. The guard above
    depends on this: a configured code that does not apply at the business
    under test must skip the case rather than drive it.
    """
    applied = (raw.get("discounts") or {}).get("applied") or []
    named = any(
      (a.get("code") or "").casefold() == code.casefold()
      for a in applied
      if isinstance(a, dict)
    )
    return named or self._discount_total(raw) < 0


if __name__ == "__main__":
  absltest.main()
