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

  cart.md:66       cart contents win; overlapping checkout payload fields are
                   ignored on conversion
  cart.md:72-74    a second conversion of the same cart returns the existing
                   incomplete checkout rather than creating a new one
  cart.md:185      cancel returns the cart state as it was before deletion
  discount.md:143  discount codes applied to a cart carry forward to the
                   checkout the cart converts into

Server-agnostic by construction. Line items are compared as a bag of
(item id, quantity) rather than by server-assigned identifiers; the conversion
identity check compares checkout ids rather than totals arithmetic; both 200 and
201 are accepted where the specification does not pin a status; and the discount
assertion accepts any of the three wire signals a conformant business may use.

Not asserted here, deliberately. cart.md:176 ("full replacement") binds the
platform rather than the business, so it is not a business obligation this suite
can hold a merchant to. cart.md:186 (subsequent operations return not_found) is
a SHOULD, so the post-cancel status is not asserted.
"""

import uuid

from absl.testing import absltest
import integration_test_utils

_CART_CAPABILITY = "dev.ucp.shopping.cart"
_DISCOUNT_CAPABILITY = "dev.ucp.shopping.discount"


class CartTest(integration_test_utils.IntegrationTestBase):
  """Cart capability conformance (cart.md)."""

  def setUp(self) -> None:
    """Skip unless the business advertises the cart capability."""
    super().setUp()
    self._capabilities = self._advertised_capabilities()
    if _CART_CAPABILITY not in self._capabilities:
      self.skipTest(f"business does not advertise {_CART_CAPABILITY}; skipping")
    self._sku = self.fixture_ctx.get_test_sku()

  def _advertised_capabilities(self) -> dict:
    """Return the advertised capability map, keyed by capability name."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    if isinstance(caps, dict):
      return caps
    return {c.get("name"): c for c in caps if isinstance(c, dict)}

  def _discount_extends_cart(self) -> bool:
    """Return True if the discount capability declares it extends cart."""
    entry = self._capabilities.get(_DISCOUNT_CAPABILITY)
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
      headers=integration_test_utils.get_headers(),
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
      headers=integration_test_utils.get_headers(
        idempotency_key=str(uuid.uuid4())
      ),
    )
    self.assertIn(
      resp.status_code,
      (200, 201),
      f"cart conversion failed: {resp.status_code} {resp.text}",
    )
    return resp.json()

  @staticmethod
  def _item_bag(raw: dict) -> list:
    """Return line items as a sorted bag of (item id, quantity) pairs."""
    return sorted(
      (li.get("item", {}).get("id"), li.get("quantity"))
      for li in (raw.get("line_items") or [])
    )

  # ── cart.md:66 ─────────────────────────────────────────────────────────
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

  # ── cart.md:72-74 ──────────────────────────────────────────────────────
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

  # ── cart.md:185 ────────────────────────────────────────────────────────
  def test_cancel_returns_the_cart_state_before_deletion(self) -> None:
    """Cancelling a cart returns the cart as it was before deletion."""
    cart = self._create_cart(quantity=3)

    resp = self.client.post(
      self.get_shopping_url(f"/carts/{cart['id']}/cancel"),
      headers=integration_test_utils.get_headers(),
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

  # ── discount.md:143 ────────────────────────────────────────────────────
  def test_cart_discount_codes_carry_forward_to_checkout(self) -> None:
    """Discount codes applied to a cart survive conversion to a checkout."""
    if not self._discount_extends_cart():
      self.skipTest(
        f"{_DISCOUNT_CAPABILITY} does not declare it extends "
        f"{_CART_CAPABILITY}; skipping"
      )
    code = self.fixture_ctx.get_test_discount_code()
    cart = self._create_cart(discounts={"codes": [code]})
    if not self._discount_is_visible(cart, code):
      self.skipTest(f"discount code {code} did not apply to the cart; skipping")

    checkout = self._convert(cart["id"])

    self.assertTrue(
      self._discount_is_visible(checkout, code),
      "a discount applied to the cart must carry forward to the checkout",
    )

  @staticmethod
  def _discount_is_visible(raw: dict, code: str) -> bool:
    """Return True if the response shows the code, or a discount it caused."""
    discounts = raw.get("discounts") or {}
    codes = [c.casefold() for c in (discounts.get("codes") or [])]
    if code.casefold() in codes:
      return True
    applied = discounts.get("applied") or []
    if any(
      (a.get("code") or "").casefold() == code.casefold()
      for a in applied
      if isinstance(a, dict)
    ):
      return True
    return any(
      t.get("type") == "discount" and (t.get("amount") or 0) < 0
      for t in (raw.get("totals") or [])
    )


if __name__ == "__main__":
  absltest.main()
