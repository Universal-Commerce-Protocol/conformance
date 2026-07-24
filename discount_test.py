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

"""Conformance tests for the discount capability (discount.md).

Complements business_logic_test.py (discount flow, multiple-code accept/reject,
fixed-amount) by covering discount MUSTs the existing suite does not assert:

  DSC-003  a new codes[] set replaces the previous one
  DSC-004  an empty codes[] removes all discounts
  DSC-005  codes are matched case-insensitively
  DSC-021  a discount's totals[] entry is negative (total.json exclusiveMax 0)
  DSC-022  an applied discount's allocations[] sum to its amount
  DSC-027  client-supplied applied[] (response-only) must not change pricing

Server-agnostic: assertions are relative (a discount reduced the total; the
applied code is echoed; the discount total is negative; an empty code set
removes it; allocations reconcile) rather than tied to a specific discount
value, so any conformant business passes regardless of the discount amount.
Configure the codes via conformance_input.json under test_fixtures
(valid_discount_code / valid_discount_code_2); the tests skip honestly when no
code is supplied or when the business does not advertise the capability.
"""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout

_DISCOUNT_CAPABILITY = "dev.ucp.shopping.discount"


class DiscountTest(integration_test_utils.IntegrationTestBase):
  """Discount capability conformance (discount.md)."""

  def setUp(self) -> None:
    """Skip unless the business advertises discount + a code is configured."""
    super().setUp()
    if not self._advertises_discount():
      self.skipTest(
        f"business does not advertise {_DISCOUNT_CAPABILITY}; skipping"
      )
    self._code = self.fixture_ctx.get_test_discount_code()

  def _advertises_discount(self) -> bool:
    """Return True if discovery advertises the discount capability."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    names = (
      list(caps.keys())
      if isinstance(caps, dict)
      else [c.get("name") for c in caps if isinstance(c, dict)]
    )
    return _DISCOUNT_CAPABILITY in names

  # ── shared drivers (operate on the raw response dict, as discount.md
  #    describes the wire shape and the sample suite asserts) ──────────────
  def _new_checkout(self):
    """Create a checkout and return (checkout_model, raw_dict)."""
    raw = self.create_checkout_session(select_fulfillment=False)
    return checkout.Checkout(**raw), raw

  def _apply_codes(self, checkout_obj, codes):
    """Apply a discount code set; return the raw updated-checkout dict."""
    return self.update_checkout_session(
      checkout_obj,
      discounts={"codes": codes},
      headers=integration_test_utils.get_headers(),
    )

  @staticmethod
  def _discount_total(raw):
    return next(
      (t for t in (raw.get("totals") or []) if t.get("type") == "discount"),
      None,
    )

  @staticmethod
  def _total_amount(raw):
    t = next(
      (t for t in (raw.get("totals") or []) if t.get("type") == "total"), None
    )
    return t.get("amount") if t else None

  @staticmethod
  def _applied_codes(raw):
    applied = (raw.get("discounts") or {}).get("applied") or []
    return [
      a.get("code") for a in applied if isinstance(a, dict) and a.get("code")
    ]

  # ── DSC-005: case-insensitive matching (discount.md) ────────────────────
  def test_code_matches_case_insensitively(self):
    """A code submitted in a different case still matches and applies."""
    base, _ = self._new_checkout()
    swapped = self._code.swapcase()
    self.assertNotEqual(
      swapped, self._code, "test code has no letters to change case on"
    )
    raw = self._apply_codes(base, [swapped])
    applied = [c.upper() for c in self._applied_codes(raw)]
    self.assertIn(
      self._code.upper(),
      applied,
      f"case-variant '{swapped}' of '{self._code}' should match and appear in "
      f"discounts.applied (got {applied})",
    )
    self.assertIsNotNone(
      self._discount_total(raw),
      "a matched discount must produce a discount total",
    )

  # ── discount total is negative (discount.md / total.json) ───────────────
  def test_discount_total_is_negative(self):
    """A discount's totals[] entry is negative (total.json exclusiveMax 0)."""
    base, base_raw = self._new_checkout()
    total_before = self._total_amount(base_raw)
    raw = self._apply_codes(base, [self._code])
    dt = self._discount_total(raw)
    self.assertIsNotNone(dt, "applying a valid code must add a discount total")
    self.assertLess(
      dt.get("amount"),
      0,
      f"discount totals[] entry must be negative, got {dt.get('amount')}",
    )
    if total_before is not None:
      self.assertLess(
        self._total_amount(raw),
        total_before,
        "the order total must decrease when a discount applies",
      )

  # ── DSC-022: allocations sum to the applied discount amount ─────────────
  def test_allocations_sum_to_applied_amount(self):
    """Each applied discount's allocations sum to its amount (discount.md).

    A cross-field invariant the JSON schema cannot express: when a discount
    carries allocations[], their amounts must sum to applied_discount.amount.
    Skips honestly for a server that does not emit allocations (they are
    optional per the schema).
    """
    base, _ = self._new_checkout()
    raw = self._apply_codes(base, [self._code])
    applied = (raw.get("discounts") or {}).get("applied") or []
    self.assertTrue(applied, "a valid code must produce an applied discount")
    checked = 0
    for a in applied:
      allocs = a.get("allocations") or []
      if not allocs:
        continue
      checked += 1
      self.assertEqual(
        sum(al.get("amount", 0) for al in allocs),
        a.get("amount"),
        f"allocations must sum to applied_discount.amount for "
        f"'{a.get('code')}' (got {[al.get('amount') for al in allocs]} vs "
        f"{a.get('amount')})",
      )
    if checked == 0:
      self.skipTest("server does not emit discount allocations[]")

  # ── DSC-027: client-supplied applied[] must not change pricing ──────────
  def test_client_applied_does_not_change_price(self):
    """Injected discounts.applied[] must not lower the priced total.

    discounts.applied is response-only (discount.json marks it
    ucp_request: omit). A server prices discounts from codes[], so a client
    that injects a large applied[] amount must not obtain a cheaper order.
    Asserts the total equals applying the same code without the injection,
    rather than asserting the server rejects the field (the spec constrains
    the request sender, not the server's leniency).
    """
    base_a, _ = self._new_checkout()
    clean = self._apply_codes(base_a, [self._code])
    clean_total = self._total_amount(clean)
    self.assertIsNotNone(clean_total, "need a computed total to compare")
    base_b, _ = self._new_checkout()
    injected = self.update_checkout_session(
      base_b,
      discounts={
        "codes": [self._code],
        "applied": [
          {
            "code": "__INJECT__",
            "title": "injected",
            "amount": 10_000_000,
            "allocations": [{"path": "subtotal", "amount": 10_000_000}],
          }
        ],
      },
      headers=integration_test_utils.get_headers(),
    )
    self.assertEqual(
      self._total_amount(injected),
      clean_total,
      "client-supplied discounts.applied must not change the priced total "
      "(applied is response-only; the server prices from codes[])",
    )

  # ── DSC-004: empty codes[] removes all discounts ────────────────────────
  def test_empty_codes_removes_discount(self):
    """Sending codes:[] removes previously applied discounts."""
    base, _ = self._new_checkout()
    applied_raw = self._apply_codes(base, [self._code])
    self.assertIsNotNone(
      self._discount_total(applied_raw), "precondition: code applied"
    )
    cleared = self._apply_codes(checkout.Checkout(**applied_raw), [])
    self.assertIsNone(
      self._discount_total(cleared),
      "codes:[] must remove the discount (no discount total should remain)",
    )
    self.assertEqual(
      self._applied_codes(cleared), [], "applied[] must be empty after clearing"
    )

  # ── DSC-003: a new codes[] set replaces the previous one ────────────────
  def test_codes_replace_previous_set(self):
    """Submitting discounts.codes replaces any previously submitted codes."""
    second = self.fixture_ctx.get_test_discount_code_2()
    if not second:
      self.skipTest("no valid_discount_code_2 configured for the replace test")
    base, _ = self._new_checkout()
    applied_raw = self._apply_codes(base, [self._code])
    applied_obj = checkout.Checkout(**applied_raw)
    replaced = self._apply_codes(applied_obj, [second])
    applied = [c.upper() for c in self._applied_codes(replaced)]
    self.assertIn(
      second.upper(), applied, "the replacement code must be applied"
    )
    self.assertNotIn(
      self._code.upper(),
      applied,
      f"the first code must be replaced, not accumulated (got {applied})",
    )


if __name__ == "__main__":
  absltest.main()
