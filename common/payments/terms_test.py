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
  PT-006  the accepted term travels unchanged to the order, and when
          order creation is observed directly (an unadjusted embedded
          order) its schedule amounts sum to the order total
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
from common.payments.terms_support import TermsAgentProfileServer
from framework.base_test import FLAGS
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from shopping.base import UnifiedUpdate
from shopping.base import get_valid_payment_payload
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


@requires_capability(_PT_CAPABILITY)
class PaymentTermsTest(ShoppingIntegrationTestBase):
  """Payment-terms capability conformance (terms.md).

  Every request uses the mock agent profile that declares the payment-terms
  extension, so a Business computing the capability intersection negotiates it.
  `@requires_capability` skips the suite when the Business does not advertise
  Payment Terms, before the mock agent server starts, and accumulates with
  whatever the base class requires.
  """

  def _start_agent_server(self) -> None:
    """Serve the agent profiles, including the one that declares terms."""
    flags = FLAGS
    self.agent_server = TermsAgentProfileServer(
      port=flags.mock_agent_port if flags.is_parsed() else 8285,
      webhook_port=flags.mock_webhook_port if flags.is_parsed() else 8284,
      ucp_version=self.ucp_version,
    )
    self.agent_server.start()
    self.addCleanup(self.agent_server.stop)

  def _terms_profile_url(self) -> str:
    """Return the URL of the agent profile that declares terms."""
    flags = FLAGS
    port = flags.mock_agent_port if flags.is_parsed() else 8285
    return (
      f"http://localhost:{port}{TermsAgentProfileServer.TERMS_PROFILE_PATH}"
    )

  def get_headers(
    self,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    custom_profile_url: str | None = None,
  ) -> dict[str, str]:
    """Use the agent profile that declares the payment-terms extension."""
    return super().get_headers(
      idempotency_key,
      request_id,
      custom_profile_url=custom_profile_url or self._terms_profile_url(),
    )

  # ── shared drivers (raw wire dicts throughout) ──────────────────────────
  def _new_terms_checkout(self, select_fulfillment: bool = False):
    """Create a checkout and return its raw dict; skip if it has no terms.

    Offering terms on a given checkout is a business decision
    (terms.md: payment terms are optional), so a business that
    advertises the capability but returns no terms for the default test
    item leaves nothing to assert.

    Fulfillment is left unselected by default, so the create response is
    read before any fulfillment update recomputes it. A case that needs a
    checkout it can drive to readiness passes select_fulfillment=True, so
    the create carries a fulfillment method: a business that honors the
    selection on create is then ready without any update, and one that
    does not leaves the readiness helper a method to update instead of
    none to build from. That build-from-nothing path omits a field the
    update requires, which is reported upstream in #102.
    """
    raw = self.create_checkout_session(select_fulfillment=select_fulfillment)
    terms = self._terms(raw)
    if terms is None:
      self.skipTest(
        "business advertises payment terms but returned none on the "
        "driven checkout; nothing to assert"
      )
    # An absent terms[] is a business decision; a present but empty one
    # is not allowed, so it is asserted rather than skipped.
    self.assertTrue(
      terms,
      "payment.terms is present but empty (terms.md: where terms is "
      "present it MUST contain at least one term)",
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

  @staticmethod
  def _canonical_due_at(value):
    """Normalize a valid RFC 3339 date-time to a UTC instant for comparison.

    Unparseable values and leap seconds are left verbatim so PT-007 still owns
    syntax validation and a leap second never collapses into :59.
    """
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
      return value
    if value[17:19] == "60":
      return value
    probe = value[:10] + "T" + value[11:]
    if probe[-1] in "zZ":
      probe = probe[:-1] + "+00:00"
    try:
      dt = datetime.datetime.fromisoformat(probe)
    except ValueError:
      return value
    return (
      dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    )

  @classmethod
  def _order_term_shape(cls, term):
    """Return the normative schedule shape for checkout-to-order projection.

    Compares ordered (amount, normalized due_at, type) tuples. Schedule id is
    excluded across checkout-to-order projection because payment_schedule.json
    marks id stability as SHOULD rather than MUST. Prose descriptions are
    excluded to avoid false failures on re-wording; timing stated only in
    description when due_at is omitted is not enforced here.
    """
    return [
      (s.get("amount"), cls._canonical_due_at(s.get("due_at")), s.get("type"))
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
      self.assertIsInstance(s, dict, f"schedule entry must be an object: {s!r}")
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
    """PUT a minimal selection update; return the response unchecked.

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
    body = UnifiedUpdate(
      id=checkout_obj.id,
      currency=checkout_obj.currency,
      line_items=line_items,
      payment={"selected_term_id": term_id},
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    return self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=body,
      headers=self.get_headers(),
    )

  @staticmethod
  def _has_embedded_order(order_ref):
    """Return whether a completion order reference embeds the order data."""
    payment = order_ref.get("payment")
    return bool(
      isinstance(payment, dict)
      and payment.get("accepted_term")
      and order_ref.get("totals")
    )

  def _complete_ready_checkout(self, checkout_id):
    """Complete a ready checkout without a hidden second readiness pass.

    Posting directly to /complete avoids complete_checkout_session running
    ensure_fulfillment_ready again and discarding its result, which could
    trigger a recomputing update after `ready` was captured.
    """
    resp = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=get_valid_payment_payload(),
      headers=self.get_headers(),
    )
    self.assert_response_status(resp, 200)
    return resp.json()

  def _resolve_accepted_term_baseline(self, ready, complete_data):
    """Return (expected_term, expected_id, expected_sum) for PT-006.

    Uses the completed checkout's payment.terms when present, requiring a
    payment_term_changed warning if the selection or normative schedule shape
    changed from `ready`. When completion omits payment.terms,
    selected_term_id must also be absent (payment_terms.json), and `ready`'s
    selected term anchors the baseline unless completion signals recomputation
    (payment_term_changed or a changed checkout total), in which case exact
    term continuity is not observable and the fallback narrows to the
    completed checkout total.
    """
    ready_term = self._selected_term(ready)
    ready_id = ready_term.get("id")
    ready_sum = self._schedule_sum(ready_term)
    ready_total = self._total_amount(ready)
    if ready_total is not None:
      self.assertEqual(
        ready_sum,
        ready_total,
        f"precondition: ready checkout selected term '{ready_id}' "
        f"schedule amounts must sum to the checkout total {ready_total}",
      )

    complete_terms = self._terms(complete_data)
    complete_selected_id = self._selected_id(complete_data)
    complete_total = self._total_amount(complete_data)
    complete_warnings = self._warning_codes(complete_data)
    self.assertIsNotNone(
      complete_total,
      "completed checkout must carry a 'total' totals entry",
    )
    if complete_terms is not None:
      self.assertTrue(
        complete_terms,
        "completed checkout payment.terms is present but empty "
        "(terms.md: where terms is present it MUST contain at least one term)",
      )
      complete_term = self._selected_term(complete_data)
      complete_sum = self._schedule_sum(complete_term)
      self.assertEqual(
        complete_sum,
        complete_total,
        f"completed checkout selected term '{complete_term.get('id')}' "
        f"schedule amounts must sum to the checkout total {complete_total}",
      )
      id_changed = complete_term.get("id") != ready_id
      complete_shape = self._order_term_shape(complete_term)
      ready_shape = self._order_term_shape(ready_term)
      if id_changed or complete_shape != ready_shape:
        self.assertIn(
          _PT_WARNING_CODE,
          complete_warnings,
          "completion changed the selected term from "
          f"'{ready_id}' ({ready_shape}) to "
          f"'{complete_term.get('id')}' ({complete_shape}) without "
          f"reporting a '{_PT_WARNING_CODE}' warning (messages[] codes: "
          f"{complete_warnings})",
        )
      return complete_term, complete_term.get("id"), complete_sum

    self.assertIsNone(
      complete_selected_id,
      "completed checkout must not carry payment.selected_term_id "
      f"('{complete_selected_id}') when payment.terms is absent "
      "(payment_terms.json: present whenever terms is, absent otherwise)",
    )
    recomputed = (
      _PT_WARNING_CODE in complete_warnings or complete_total != ready_sum
    )
    if recomputed:
      return None, None, complete_total

    return ready_term, ready_id, ready_sum

  def _fetch_order(self, order_ref):
    """Return the full order dict, following the id if the embed is thin.

    The suite's own precedent (order_test.py) reads the order id from
    the completion response and GETs the order for content assertions;
    a business may embed only a reference. Assert on the embedded order
    only when it already carries the accepted term and totals, otherwise
    on the GET, so a thin or partial embed is never mistaken for the
    authoritative order record.
    """
    if self._has_embedded_order(order_ref):
      return order_ref
    order_id = order_ref.get("id")
    self.assertIsNotNone(
      order_id, f"completion order reference carries no id: {order_ref}"
    )
    resp = self.client.get(
      self.get_order_url(order_id),
      headers=self.get_headers(),
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
      # Visit the term in effect first. The in-place rule only applies to
      # that term, so leaving it to the end of the walk would make the rule
      # unreachable for a business that lists its default anywhere but
      # first.
      selected_now = self._selected_id(raw)
      candidates.sort(key=lambda tid: tid != selected_now)
      if not candidates:
        break
      term_id = candidates[0]
      visited.add(term_id)
      before = next(
        (t for t in self._terms(raw) if t.get("id") == term_id), None
      )
      # Only the term already in effect is covered by the in-place rule.
      # An unselected term is indicative, and the spec directs a platform
      # not to assume its amounts survive selection unchanged (terms.md),
      # so recomputing one on selection needs no warning.
      was_selected = self._selected_id(raw) == term_id
      raw = self._select_term(raw, term_id)
      if self._terms(raw) is None:
        # The spec does not forbid a recomputed response from no longer
        # carrying terms; with none offered there is nothing left to select
        # or assert. ABSENT is that case. A present but empty terms[] is a
        # defect, so it falls through to the assertions instead of ending the
        # walk, which is the distinction _new_terms_checkout draws.
        break
      after = next(
        (t for t in self._terms(raw) if t.get("id") == term_id), None
      )
      if (
        was_selected
        and self._selected_id(raw) == term_id
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
    selected_before = self._selected_id(raw)
    control_messages = [
      m for m in (raw.get("messages") or []) if isinstance(m, dict)
    ]
    control_term = next(
      (t for t in (self._terms(raw) or []) if t.get("id") == selected_before),
      None,
    )
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
    if self._terms(updated) is not None:
      self._selected_term(updated)
    # A business may refuse the update through the message channel rather
    # than an HTTP 4xx, returning the checkout with a recoverable error for
    # the platform to correct in band (checkout.md "Error Handling"). Only
    # an error this probe produced counts: an incomplete checkout already
    # lists what it still needs as recoverable errors, and treating those as
    # a refusal would excuse a business that ignored the selection outright.
    # The refusal must also have changed nothing, in the selection and in
    # the schedules of the term in effect, since a rewrite of the term in
    # effect is the very thing the warning exists to report.
    refused = [
      m
      for m in (updated.get("messages") or [])
      if isinstance(m, dict)
      and m.get("type") == "error"
      and m not in control_messages
    ]
    kept = next(
      (
        t
        for t in (self._terms(updated) or [])
        if t.get("id") == self._selected_id(updated)
      ),
      None,
    )
    # `kept is None` is not a pass: a business that dropped terms[] while
    # keeping selected_term_id changed the term in effect to nothing, which is
    # exactly the rewrite this warning exists to report. When a control term
    # existed, the term in effect must still be present and still match.
    untouched = self._selected_id(updated) == selected_before and (
      control_term is None
      or (
        kept is not None
        and self._term_shape(kept) == self._term_shape(control_term)
      )
    )
    if refused and untouched:
      return
    self.assertIn(
      _PT_WARNING_CODE,
      self._warning_codes(updated),
      "accepting an unresolvable selection without a "
      f"'{_PT_WARNING_CODE}' warning is a silent substitution "
      f"(messages[] codes: {self._warning_codes(updated)})",
    )

  # ── PT-006: the accepted term travels to the order ──────────────────────
  def test_accepted_term_travels_to_the_order(self):
    """The order preserves the accepted term, and matches total at creation.

    Given a checkout that offers payment terms and is driven to
    fulfillment readiness and completed without extra recomputing updates,
    When the resulting order is examined,
    Then:
      - payment.accepted_term is present and preserves the accepted term's
        id and normative schedule shape (ordered amounts, normalized due_at
        instants, types), even when post-purchase adjustments change or
        preserve the displayed order total (terms.md: "A Business MUST NOT
        modify the accepted term after the Order is created; post-purchase
        changes are recorded in adjustments[]"); and
      - when order creation is directly observed via an unadjusted order
        embedded synchronously in the completion response, its schedule
        amounts sum to the order total (terms.md: "When the Order is
        created, a Business MUST ensure its schedule amounts sum to the
        Order total").

    A subsequent GET /orders/{id} — even the first GET, and even when
    adjustments[] is absent or empty — is not treated as proof of observing
    order creation. On runs returning a thin order_confirmation reference,
    creation-time equality against order.total is therefore not asserted,
    while accepted-term presence, id, and schedule preservation against the
    completed checkout remain enforced (narrowing to schedule sum against
    completed checkout total only when completion recomputed without
    echoing payment.terms). Orders carrying adjustments are never
    blanket-skipped, and credits are never required to be netted into
    order.totals.
    """
    # create_checkout_session(select_fulfillment=True) already calls
    # ensure_fulfillment_ready and returns the readied checkout
    # (shopping/base.py). Completing directly avoids a hidden second
    # ensure_fulfillment_ready pass that could recompute after `ready`.
    ready = self._new_terms_checkout(select_fulfillment=True)
    if ready.get("status") != "ready_for_complete":
      # The term is already agreed before a checkout can reach
      # ready_for_complete, so only a capture made at readiness is
      # anchored; completing from an earlier state may involve further
      # recomputing updates that change the selection, as the spec allows.
      self.skipTest(
        "the driven checkout did not reach ready_for_complete; the "
        "accepted-term capture point cannot be anchored"
      )
    complete_data = self._complete_ready_checkout(ready["id"])
    complete_status = complete_data.get("status")
    if complete_status in ("complete_in_progress", "requires_escalation"):
      self.skipTest(
        f"checkout completion returned status '{complete_status}' rather "
        "than 'completed'; no created order is synchronously available"
      )
    self.assertEqual(
      complete_status,
      "completed",
      "expected completed checkout after completing ready session, "
      f"got status {complete_status!r}",
    )
    expected_term, expected_id, expected_sum = (
      self._resolve_accepted_term_baseline(ready, complete_data)
    )
    order_ref = complete_data.get("order")
    self.assertIsInstance(
      order_ref, dict, "the completion response must reference the order"
    )
    embedded = self._has_embedded_order(order_ref)
    order = order_ref if embedded else self._fetch_order(order_ref)
    order_payment = order.get("payment")
    accepted = (
      order_payment.get("accepted_term")
      if isinstance(order_payment, dict)
      else None
    )
    self.assertIsNotNone(
      accepted,
      "a checkout that carried payment terms must project the accepted "
      "term onto the order as payment.accepted_term",
    )
    self.assertIsInstance(
      accepted,
      dict,
      f"order payment.accepted_term must be an object, got {accepted!r}",
    )
    accepted_sum = self._schedule_sum(accepted)
    if expected_id is not None:
      self.assertEqual(
        accepted.get("id"),
        expected_id,
        "the accepted term must carry the id agreed at checkout completion",
      )
    if expected_term is not None:
      self.assertEqual(
        self._order_term_shape(accepted),
        self._order_term_shape(expected_term),
        "the accepted term carried onto the order must preserve the "
        "agreed schedules (amounts, due dates, types) unchanged "
        "(terms.md: a business MUST NOT modify the accepted term after "
        "the order is created)",
      )
    else:
      self.assertEqual(
        accepted_sum,
        expected_sum,
        "the accepted term's schedule amounts must match the completed "
        f"checkout total {expected_sum} even if later order totals change",
      )
    if embedded and not order.get("adjustments"):
      order_total = self._total_amount(order)
      self.assertIsNotNone(order_total, "order must carry a 'total' entry")
      self.assertEqual(
        accepted_sum,
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
