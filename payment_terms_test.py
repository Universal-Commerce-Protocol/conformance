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

"""Conformance tests for the payment-terms capability (terms.md).

Covers the cross-field invariants of ``checkout.payment.terms`` that the
JSON schema cannot express:

  PT-001  where ``terms`` is present it is non-empty, ``selected_term_id``
          is present and names one of ``terms[].id``
  PT-002  ``terms[].id`` are unique within the checkout
  PT-003  the selected term's schedule amounts sum to the checkout total
  PT-004  selecting a term is an update; the response either honors the
          selection or reports the change with a ``payment_term_changed``
          warning, including when the selected term is rewritten in
          place, and always keeps the sum invariant
  PT-005  a selection that does not resolve is never silently
          substituted: the business either rejects the update or reports
          a ``payment_term_changed`` warning in ``messages[]``
  PT-006  the accepted term travels to the order and its schedule
          amounts sum to the order total at creation
  PT-007  ``due_at``, where a schedule carries one, is a parseable
          RFC 3339 date-time

Assertions operate on the raw wire payload (``response.json()``) rather
than parsed SDK models, so they validate the on-the-wire contract
directly and do not depend on the SDK carrying payment-terms models.

Server-agnostic: whether a checkout carries terms, how many, and their
amounts are business decisions, so every assertion is relative to the
terms the business returned, and a response that resolves a selection
conflict differently from the request is accepted wherever the spec
sanctions it (the ``payment_term_changed`` warning is the designed
detection channel). The tests skip honestly when the business does not
advertise the capability or when the driven checkout carries no terms.
"""

import datetime
import re
import uuid

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request

try:
  # ucp#741 moved the payment constructs from schemas/shopping/ to
  # schemas/common/types/ (ucp-sdk>=0.5.0).
  from ucp_sdk.models.schemas.common.types.payment import Payment
except ImportError:
  # ucp-sdk<=0.4.6, pre-#741.
  from ucp_sdk.models.schemas.shopping.payment import Payment

# Rebuild models to resolve forward references (needed to parse a full
# checkout response before driving a selection update).
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})

_PT_CAPABILITY = "dev.ucp.common.payment.terms"
_PT_WARNING_CODE = "payment_term_changed"

# RFC 3339 date-time shape (section 5.6): full date, T, partial time with
# optional fraction, and a mandatory Z or numeric UTC offset. Lowercase
# t/z are explicitly permitted by the RFC; a missing offset is not.
# Offset minutes are constrained to 00-59 here because Python's parser
# normalizes larger values into hours instead of rejecting them; offset
# hours are left to the parser, which bounds the total offset below 24h.
_RFC3339_RE = re.compile(
  r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:[0-5]\d)$"
)
_RFC3339_FRACTION_RE = re.compile(r"\.\d+")


class PaymentTermsTest(integration_test_utils.IntegrationTestBase):
  """Payment-terms capability conformance (terms.md)."""

  def setUp(self) -> None:
    """Skip unless the business advertises the payment-terms capability."""
    super().setUp()
    if not self._advertises_payment_terms():
      self.skipTest(f"business does not advertise {_PT_CAPABILITY}; skipping")

  def _advertises_payment_terms(self) -> bool:
    """Return True if discovery advertises the payment-terms capability."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    names = (
      list(caps.keys())
      if isinstance(caps, dict)
      else [c.get("name") for c in caps if isinstance(c, dict)]
    )
    return _PT_CAPABILITY in names

  # ── shared drivers (raw wire dicts throughout) ──────────────────────────
  def _new_terms_checkout(self):
    """Create a checkout and return its raw dict; skip if it has no terms.

    Offering terms on a given checkout is a business decision
    (terms.md: payment terms are optional), so a business that
    advertises the capability but returns no terms for the default test
    item leaves nothing to assert.
    """
    raw = self.create_checkout_session(select_fulfillment=False)
    if not self._terms(raw):
      self.skipTest(
        "business advertises payment terms but returned none on the "
        "driven checkout; nothing to assert"
      )
    return raw

  @staticmethod
  def _terms(raw):
    """Return payment.terms from a raw checkout response, or None."""
    return (raw.get("payment") or {}).get("terms")

  @staticmethod
  def _selected_id(raw):
    """Return payment.selected_term_id from a raw checkout response."""
    return (raw.get("payment") or {}).get("selected_term_id")

  @staticmethod
  def _total_amount(raw):
    """Return the amount of the single 'total' totals entry, or None."""
    t = next(
      (t for t in (raw.get("totals") or []) if t.get("type") == "total"), None
    )
    return t.get("amount") if t else None

  @staticmethod
  def _warning_codes(raw):
    """Return the message codes carried by a raw response."""
    return [
      m.get("code") for m in (raw.get("messages") or []) if isinstance(m, dict)
    ]

  @staticmethod
  def _term_shape(term):
    """Return the amounts-and-timing shape of a term, for change detection.

    Compares only what the spec names as an in-place rewrite of the term
    in effect (schedules, due dates, amounts), so a business that merely
    re-words a description is not accused of changing the term.
    """
    return [
      (s.get("id"), s.get("amount"), s.get("due_at"), s.get("type"))
      for s in (term.get("schedules") or [])
    ]

  def _schedule_sum(self, term):
    """Sum a term's schedule amounts, asserting they are wire integers."""
    schedules = term.get("schedules") or []
    self.assertTrue(
      schedules,
      f"term '{term.get('id')}' must carry at least one schedule "
      "(payment_term.json: schedules minItems 1)",
    )
    for s in schedules:
      self.assertIsInstance(
        s.get("amount"),
        int,
        f"schedule 'amount' must be an integer minor-unit value: {s}",
      )
      self.assertNotIsInstance(
        s.get("amount"),
        bool,
        f"schedule 'amount' must be an integer, not a boolean: {s}",
      )
    return sum(s["amount"] for s in schedules)

  def _selected_term(self, raw):
    """Return the term object named by selected_term_id, asserting it."""
    terms = self._terms(raw)
    selected_id = self._selected_id(raw)
    self.assertIsNotNone(
      selected_id,
      "a response carrying payment.terms must carry selected_term_id "
      "(terms.md: name one of the offered terms on every "
      "response that carries terms)",
    )
    matches = [t for t in terms if t.get("id") == selected_id]
    self.assertEqual(
      len(matches),
      1,
      f"selected_term_id '{selected_id}' must resolve to exactly one "
      f"offered term (offered ids: {[t.get('id') for t in terms]})",
    )
    return matches[0]

  def _select_term(self, raw, term_id):
    """Select a term via checkout update; return the raw updated dict."""
    return self.update_checkout_session(
      checkout.Checkout(**raw),
      payment={"selected_term_id": term_id},
    )

  def _raw_selection_put(self, raw, term_id):
    """PUT a minimal selection update; return the unasserted response.

    Sends a valid update body (id, currency, line items, payment) and
    returns the raw httpx response so a test can accept either spec
    posture (a 4xx rejection or a 200 carrying a warning) instead of
    requiring 200. Callers that rely on a 4xx being attributable to the
    payload's selected_term_id must first prove this body shape is
    otherwise acceptable with a control call using a valid id.
    """
    checkout_obj = checkout.Checkout(**raw)
    line_items = [
      line_item_update_request.LineItemUpdateRequest(
        id=li.id,
        item=item_update_request.ItemUpdateRequest(id=li.item.id),
        quantity=li.quantity,
        parent_id=li.parent_id,
      )
      for li in checkout_obj.line_items
    ]
    body = integration_test_utils.UnifiedUpdate(
      id=checkout_obj.id,
      currency=checkout_obj.currency,
      line_items=line_items,
      payment={"selected_term_id": term_id},
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    return self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=body,
      headers=integration_test_utils.get_headers(),
    )

  def _fetch_order(self, order_ref):
    """Return the full order dict, following the id if the embed is thin.

    The suite's own precedent (order_test.py) reads the order id from
    the completion response and GETs the order for content assertions;
    a business may embed only a reference. Assert on the embedded order
    only when it already carries the accepted term and totals, otherwise
    on the GET, so a thin or partial embed is never mistaken for the
    authoritative order record.
    """
    if (order_ref.get("payment") or {}).get("accepted_term") and order_ref.get(
      "totals"
    ):
      return order_ref
    order_id = order_ref.get("id")
    self.assertIsNotNone(
      order_id, f"completion order reference carries no id: {order_ref}"
    )
    resp = self.client.get(
      self.get_order_url(order_id),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(resp, 200)
    return resp.json()

  # ── PT-001: selected_term_id present and resolving ──────────────────────
  def test_selected_term_names_an_offered_term(self):
    """Every response carrying terms names a selected term among them.

    Given a checkout whose response carries payment.terms,
    When the response is examined,
    Then terms[] is non-empty and selected_term_id names exactly one of
    terms[].id (terms.md: "Where terms is present it MUST
    contain at least one term, and selected_term_id MUST name one of
    them").
    """
    raw = self._new_terms_checkout()
    self._selected_term(raw)

  # ── PT-002: terms[].id unique within the checkout ───────────────────────
  def test_term_ids_unique_within_checkout(self):
    """Term ids are unique, so a selection resolves to exactly one term.

    Given a checkout whose response carries payment.terms,
    When the offered term ids are collected,
    Then no id appears twice (terms.md: "A Business MUST make
    terms[].id unique within a Checkout").
    """
    raw = self._new_terms_checkout()
    ids = [t.get("id") for t in self._terms(raw)]
    self.assertEqual(
      len(ids),
      len(set(ids)),
      f"terms[].id must be unique within the checkout, got {ids}",
    )

  # ── PT-003: selected term's schedules sum to the checkout total ─────────
  def test_selected_term_schedules_sum_to_checkout_total(self):
    """The selected term's schedule amounts sum to the checkout total.

    Given a checkout whose response carries payment.terms,
    When the selected term's schedule amounts are summed,
    Then the sum equals the checkout totals[] 'total' amount
    (terms.md: "For the selected term, the Business MUST ensure
    that sum equals the checkout total").
    """
    raw = self._new_terms_checkout()
    selected = self._selected_term(raw)
    total = self._total_amount(raw)
    self.assertIsNotNone(total, "checkout must carry a 'total' totals entry")
    self.assertEqual(
      self._schedule_sum(selected),
      total,
      f"selected term '{selected.get('id')}' schedule amounts must sum "
      f"to the checkout total {total}",
    )

  # ── PT-004: selection is an update; response is authoritative ───────────
  def test_selecting_each_term_recomputes_the_checkout(self):
    """Each selection is honored or warned, and keeps the sum invariant.

    Given a checkout offering one or more payment terms,
    When each currently offered term is selected through Update Checkout,
    Then each response either names the requested term as selected, or
    reports the changed selection with a 'payment_term_changed' warning;
    and where the requested term stays selected but is rewritten in
    place, that warning is required too (terms.md: a business
    "MUST also report that warning when changing the selected term in
    place, by altering its schedules, due dates, or amounts, without
    naming a different term", so "A Platform can therefore detect a
    changed selection from the code alone"). In every case the
    response's selected term resolves and its schedule amounts sum to
    that response's total.

    Selections are always drawn from the latest response's terms[], as
    the spec directs, because a recomputed response may legitimately
    change which terms are offered.
    """
    raw = self._new_terms_checkout()
    bound = len(self._terms(raw))
    visited = set()
    for _ in range(bound):
      candidates = [
        t.get("id")
        for t in self._terms(raw) or []
        if t.get("id") not in visited
      ]
      if not candidates:
        break
      term_id = candidates[0]
      visited.add(term_id)
      before = next(
        (t for t in self._terms(raw) if t.get("id") == term_id), None
      )
      raw = self._select_term(raw, term_id)
      if not self._terms(raw):
        # The spec does not forbid a recomputed response from no longer
        # carrying terms; with none offered there is nothing left to
        # select or assert.
        break
      after = next(
        (t for t in self._terms(raw) if t.get("id") == term_id), None
      )
      if (
        self._selected_id(raw) == term_id
        and before is not None
        and after is not None
        and self._term_shape(before) != self._term_shape(after)
      ):
        self.assertIn(
          _PT_WARNING_CODE,
          self._warning_codes(raw),
          f"the response kept '{term_id}' selected but rewrote it in "
          f"place ({self._term_shape(before)} -> "
          f"{self._term_shape(after)}) without reporting a "
          f"'{_PT_WARNING_CODE}' warning; a change to the term in "
          "effect must be detectable from the code alone "
          f"(messages[] codes: {self._warning_codes(raw)})",
        )
      if self._selected_id(raw) != term_id:
        self.assertIn(
          _PT_WARNING_CODE,
          self._warning_codes(raw),
          f"the update response selected "
          f"'{self._selected_id(raw)}' instead of the requested "
          f"'{term_id}' without reporting a '{_PT_WARNING_CODE}' "
          "warning; a changed selection must be detectable from the "
          f"code alone (messages[] codes: {self._warning_codes(raw)})",
        )
      selected = self._selected_term(raw)
      total = self._total_amount(raw)
      self.assertIsNotNone(total, "checkout must carry a 'total' totals entry")
      self.assertEqual(
        self._schedule_sum(selected),
        total,
        f"after selecting '{term_id}' the selected term's schedule "
        f"amounts must sum to the recomputed checkout total {total}",
      )

  # ── PT-005: unresolvable selection is never silent ──────────────────────
  def test_unresolvable_selection_is_not_silently_substituted(self):
    """A selection that does not resolve is rejected or warned, never silent.

    Given a checkout offering payment terms,
    When an update names a selected_term_id that matches no offered term,
    Then the business either rejects the update, or returns a checkout
    that reports a 'payment_term_changed' warning in messages[] and
    whose selected term still resolves (terms.md: "MUST NOT
    silently substitute a term, and MUST report the change as a
    payment_term_changed warning in messages[]").

    A control update using the currently selected id must succeed
    first, so a later 4xx is attributable to the unresolvable id rather
    than to the update body's shape.
    """
    raw = self._new_terms_checkout()
    control_id = self._selected_term(raw).get("id")
    control = self._raw_selection_put(raw, control_id)
    self.assertEqual(
      control.status_code,
      200,
      "control: re-selecting the currently selected term "
      f"'{control_id}' must succeed for this probe to be meaningful "
      f"(got {control.status_code}: {control.text[:200]})",
    )
    raw = control.json()
    bogus = f"pt_unresolvable_{uuid.uuid4().hex[:12]}"
    resp = self._raw_selection_put(raw, bogus)
    if 400 <= resp.status_code < 500:
      return  # rejecting the update outright is not a silent substitution
    self.assertEqual(
      resp.status_code,
      200,
      "an unresolvable selection must be rejected (4xx) or answered "
      f"with a warned checkout (200), got {resp.status_code}",
    )
    updated = resp.json()
    if self._terms(updated):
      self._selected_term(updated)
    self.assertIn(
      _PT_WARNING_CODE,
      self._warning_codes(updated),
      "accepting an unresolvable selection without a "
      f"'{_PT_WARNING_CODE}' warning is a silent substitution "
      f"(messages[] codes: {self._warning_codes(updated)})",
    )

  # ── PT-006: the accepted term travels to the order ──────────────────────
  def test_accepted_term_travels_to_the_order(self):
    """The order carries the accepted term, summing to the order total.

    Given a checkout that offers payment terms and is driven to
    fulfillment readiness and completed,
    When the resulting order is examined,
    Then payment.accepted_term carries the term that was selected once
    the checkout was ready (the term is already agreed before a checkout
    can reach ready_for_complete) and its schedule amounts sum to the order
    total (terms.md: "Carry the accepted term onto the Order",
    and "When the Order is created, a Business MUST ensure its schedule
    amounts sum to the Order total").

    The selection is captured AFTER fulfillment is driven ready, since
    those updates recompute the checkout and may conformantly change
    the selected or default term (with a warning) before it settles.
    """
    raw = self._new_terms_checkout()
    ready = self.ensure_fulfillment_ready(raw["id"])
    if not self._terms(ready):
      self.skipTest(
        "the ready checkout no longer carries payment terms; the "
        "accepted-term projection cannot be asserted"
      )
    if ready.get("status") != "ready_for_complete":
      # The term is already agreed before a checkout can reach
      # ready_for_complete, so only a capture made at readiness is
      # anchored; completing from an earlier state may involve further
      # recomputing updates that conformantly change the selection.
      self.skipTest(
        "the driven checkout did not reach ready_for_complete; the "
        "accepted-term capture point cannot be anchored"
      )
    selected_id = self._selected_term(ready).get("id")
    complete_data = self.complete_checkout_session(raw["id"])
    order_ref = complete_data.get("order")
    self.assertIsInstance(
      order_ref, dict, "the completion response must reference the order"
    )
    order = self._fetch_order(order_ref)
    accepted = (order.get("payment") or {}).get("accepted_term")
    self.assertIsNotNone(
      accepted,
      "a checkout that carried payment terms must project the accepted "
      "term onto the order as payment.accepted_term",
    )
    self.assertEqual(
      accepted.get("id"),
      selected_id,
      "the accepted term must be the term selected once the checkout "
      "was ready for completion",
    )
    order_total = self._total_amount(order)
    self.assertIsNotNone(order_total, "order must carry a 'total' entry")
    self.assertEqual(
      self._schedule_sum(accepted),
      order_total,
      "the accepted term's schedule amounts must sum to the order "
      f"total {order_total} at order creation",
    )

  # ── PT-007: due_at, where present, is RFC 3339 ──────────────────────────
  def test_due_at_when_present_is_rfc3339(self):
    """Every schedule due_at present on the wire is an RFC 3339 date-time.

    Given a checkout whose terms carry schedules,
    When a schedule states a due_at,
    Then it is an absolute RFC 3339 date-time (payment_schedule.json:
    format date-time; terms.md: "due_at is an absolute RFC 3339
    date-time"). The shape check follows RFC 3339 itself rather than
    Python's parser: lowercase t/z and a leap second are conformant and
    accepted; a naive date-time without a UTC offset is not absolute and
    is rejected. Skips when no schedule states one, since due_at is
    required to be omitted when the date depends on a future event.
    """
    raw = self._new_terms_checkout()
    checked = 0
    for term in self._terms(raw):
      for s in term.get("schedules") or []:
        value = s.get("due_at")
        if value is None:
          continue
        checked += 1
        self.assertIsInstance(
          value, str, f"due_at must be a string when present: {s}"
        )
        where = f"schedule '{s.get('id')}' of term '{term.get('id')}'"
        if not _RFC3339_RE.match(value):
          self.fail(
            f"due_at '{value}' on {where} is not an absolute RFC 3339 "
            "date-time (date, time, and a Z or numeric UTC offset)"
          )
        # Range-validate the calendar fields. Fractional seconds carry
        # no range information and the RFC 3339 leap second is legal, so
        # both are normalized away before handing to the parser.
        probe = _RFC3339_FRACTION_RE.sub("", value)
        if probe[17:19] == "60":
          probe = probe[:17] + "59" + probe[19:]
        probe = probe[:10] + "T" + probe[11:]
        probe = (probe[:-1] + "+00:00") if probe[-1] in "zZ" else probe
        try:
          datetime.datetime.fromisoformat(probe)
        except ValueError:
          self.fail(
            f"due_at '{value}' on {where} has out-of-range date or time fields"
          )
    if checked == 0:
      self.skipTest("no schedule states a due_at; nothing to assert")


if __name__ == "__main__":
  absltest.main()
