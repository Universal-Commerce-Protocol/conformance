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

"""Offline regression tests for PT-006 (accepted payment term on order)."""

import copy
import unittest

from common.payments import terms_test
import httpx

_ORDER_REF = {
  "id": "ord_1",
  "permalink_url": "https://store.example/orders/ord_1",
}
_TERM_CHANGED_WARNING = {
  "type": "warning",
  "code": "payment_term_changed",
  "content": "Payment terms recomputed before completion.",
}
# Negative probes: info is valid as an info message (not a warning), untyped
# is schema-invalid, and PT-006 accepts neither as the required warning.
_TERM_CHANGED_INFO = {
  "type": "info",
  "code": "payment_term_changed",
  "content": "Payment terms recomputed before completion.",
}
_TERM_CHANGED_UNTYPED = {
  "code": "payment_term_changed",
  "content": "Payment terms recomputed before completion.",
}


def _split_term(
  term_id: str = "term_split_2",
  amounts: tuple[int, ...] = (6000, 4000),
  due_dates: tuple[str | None, ...] = (
    "2026-12-01T00:00:00Z",
    "2026-12-15T00:00:00Z",
  ),
  schedule_ids: tuple[str, ...] = ("sched_1", "sched_2"),
  schedule_types: tuple[str | None, ...] = ("deposit", "balance"),
  title: str = "Two installments",
  description: str = "Pay deposit now and balance later",
  schedule_descriptions: tuple[str, ...] = (
    "Initial deposit due at booking",
    "Remaining balance due 2026-12-15",
  ),
) -> dict:
  """Build a schema-valid synthetic multi-installment payment term."""
  schedules = []
  for idx, amount in enumerate(amounts):
    schedule_description = (
      schedule_descriptions[idx]
      if idx < len(schedule_descriptions)
      else f"Installment {idx + 1}"
    )
    entry = {
      "id": (
        schedule_ids[idx] if idx < len(schedule_ids) else f"sched_{idx + 1}"
      ),
      "amount": amount,
      "description": {"plain": schedule_description},
    }
    if idx < len(due_dates) and due_dates[idx] is not None:
      entry["due_at"] = due_dates[idx]
    if idx < len(schedule_types) and schedule_types[idx] is not None:
      entry["type"] = schedule_types[idx]
    schedules.append(entry)
  return {
    "id": term_id,
    "title": title,
    "description": {"plain": description},
    "schedules": schedules,
  }


def _full_term(term_id: str = "term_full", amount: int = 10000) -> dict:
  """Build a schema-valid synthetic single-installment payment term."""
  return _split_term(
    term_id=term_id,
    amounts=(amount,),
    due_dates=("2026-12-01T00:00:00Z",),
    schedule_ids=("sched_full",),
    schedule_types=("immediate",),
    title="Pay in full",
    description="Pay the entire balance upfront",
    schedule_descriptions=("Full amount due immediately",),
  )


def _checkout(
  *,
  status: str | None = "ready_for_complete",
  total: int | None = 10000,
  terms: list[dict] | None = None,
  selected_term_id: str | None = "term_split_2",
  include_terms: bool = True,
  messages: list[dict] | None = None,
  order: dict | None = None,
) -> dict:
  """Build a synthetic UCP checkout wire payload."""
  payment: dict = {"instruments": []}
  if include_terms:
    payment["terms"] = (
      [_split_term(), _full_term()] if terms is None else copy.deepcopy(terms)
    )
  if selected_term_id is not None:
    payment["selected_term_id"] = selected_term_id
  totals = (
    []
    if total is None
    else [
      {"type": "subtotal", "amount": total},
      {"type": "total", "amount": total},
    ]
  )
  payload: dict = {
    "ucp": {
      "version": "2026-08-25",
      "capabilities": {},
      "payment_handlers": {},
    },
    "id": "chk_1",
    "currency": "USD",
    "line_items": [
      {
        "id": "li_1",
        "quantity": 1,
        "item": {"id": "stay_1", "title": "Standard Item", "price": 10000},
        "totals": [
          {"type": "subtotal", "amount": 10000},
          {"type": "total", "amount": 10000},
        ],
      }
    ],
    "totals": totals,
    "links": [],
    "payment": payment,
    "fulfillment": {
      "methods": [
        {
          "id": "method_1",
          "type": "shipping",
          "line_item_ids": ["li_1"],
          "destinations": [
            {
              "id": "dest_1",
              "street_address": "123 Main St",
              "address_locality": "Anytown",
              "address_region": "CA",
              "postal_code": "12345",
              "address_country": "US",
            }
          ],
          "selected_destination_id": "dest_1",
          "groups": [
            {
              "id": "group_1",
              "line_item_ids": ["li_1"],
              "selected_option_id": "std-ship",
              "options": [{"id": "std-ship", "title": "Standard"}],
            }
          ],
        }
      ]
    },
  }
  if status is not None:
    payload["status"] = status
  if messages is not None:
    payload["messages"] = copy.deepcopy(messages)
  if order is not None:
    payload["order"] = copy.deepcopy(order)
  return payload


def _completed(**kwargs) -> dict:
  """Build a completed checkout payload with sensible PT-006 defaults."""
  defaults = {
    "status": "completed",
    "total": 10000,
    "include_terms": False,
    "selected_term_id": None,
    "order": _ORDER_REF,
  }
  defaults.update(kwargs)
  return _checkout(**defaults)


def _order(
  *,
  total: int = 10000,
  accepted_term: dict | None = None,
  include_accepted_term: bool = True,
  adjustments: list[dict] | None = None,
) -> dict:
  """Build a synthetic UCP order wire payload."""
  payment: dict = {}
  if include_accepted_term:
    payment["accepted_term"] = (
      _split_term() if accepted_term is None else copy.deepcopy(accepted_term)
    )
  payload: dict = {
    "ucp": {"version": "2026-08-25", "capabilities": {}},
    "id": "ord_1",
    "checkout_id": "chk_1",
    "permalink_url": _ORDER_REF["permalink_url"],
    "currency": "USD",
    "line_items": [
      {
        "id": "li_1",
        "item": {"id": "stay_1", "title": "Standard Item", "price": 10000},
        "quantity": {"total": 1, "fulfilled": 0},
        "totals": [
          {"type": "subtotal", "amount": 10000},
          {"type": "total", "amount": 10000},
        ],
        "status": "processing",
      }
    ],
    "fulfillment": {"expectations": [], "events": []},
    "totals": [
      {"type": "subtotal", "amount": total},
      {"type": "total", "amount": total},
    ],
    "payment": payment,
  }
  if adjustments is not None:
    payload["adjustments"] = copy.deepcopy(adjustments)
  return payload


def _credit(amount: int = -2000) -> dict:
  """Build a synthetic completed post-purchase credit adjustment."""
  return {
    "id": "adj_credit_1",
    "type": "refund",
    "occurred_at": "2026-11-25T00:00:00Z",
    "status": "completed",
    "totals": [{"type": "total", "amount": amount}],
    "description": "Partial post-purchase credit",
  }


def _run_pt006(
  *,
  ready_checkout: dict | None = None,
  complete_checkout: dict | None = None,
  order_response: dict | None = None,
  on_put=None,
  recorded: list[tuple[str, str]] | None = None,
) -> None:
  """Run PT-006 against an in-process httpx.MockTransport fake."""
  state = {
    "checkout": (
      _checkout() if ready_checkout is None else copy.deepcopy(ready_checkout)
    )
  }
  completed = (
    _completed()
    if complete_checkout is None
    else copy.deepcopy(complete_checkout)
  )
  order = _order() if order_response is None else copy.deepcopy(order_response)

  def handler(request: httpx.Request) -> httpx.Response:
    if recorded is not None:
      recorded.append((request.method, request.url.path))
    path = request.url.path
    if request.method == "POST" and path == "/checkout-sessions":
      return httpx.Response(201, json=state["checkout"])
    if request.method == "GET" and path == "/checkout-sessions/chk_1":
      return httpx.Response(200, json=state["checkout"])
    if request.method == "PUT" and path == "/checkout-sessions/chk_1":
      if on_put is not None:
        state["checkout"] = on_put(state["checkout"], request)
      return httpx.Response(200, json=state["checkout"])
    if request.method == "POST" and path == "/checkout-sessions/chk_1/complete":
      return httpx.Response(200, json=completed)
    if request.method == "GET" and path == "/orders/ord_1":
      return httpx.Response(200, json=order)
    return httpx.Response(404, json={"detail": f"unexpected {path}"})

  class Probe(terms_test.PaymentTermsTest):
    """A PaymentTermsTest bound to an httpx.MockTransport fake."""

    def runTest(self):  # noqa: N802
      """Do nothing; individual test methods are invoked directly."""

  probe = Probe()
  probe.base_url = "https://store.example"
  probe.discovery_url = "https://store.example/.well-known/ucp"
  probe.conformance_config = {"ucp_version": "2026-08-25", "currency": "USD"}
  probe._shopping_service_endpoint = "https://store.example"
  probe.client = httpx.Client(
    base_url="https://store.example",
    transport=httpx.MockTransport(handler),
  )
  probe.test_accepted_term_travels_to_the_order()


class PT006AcceptedTermOrderTest(unittest.TestCase):
  """Regression tests for PT-006 (terms.md order payment requirements)."""

  def test_ordinary_valid_flow_passes(self):
    """An ordinary checkout and unchanged order pass PT-006."""
    _run_pt006()

  def test_unchanged_schedule_passes_with_adjusted_or_later_order_total(self):
    """Unchanged accepted_term passes regardless of later Order.totals."""
    cases = [
      ("netted_credit", _order(total=8000, adjustments=[_credit(-2000)])),
      ("historical_total", _order(total=10000, adjustments=[_credit(-2000)])),
      ("first_get_no_adjustments", _order(total=8000, adjustments=None)),
    ]
    for label, order in cases:
      with self.subTest(case=label):
        _run_pt006(order_response=order)

  def test_revised_schedule_matching_adjusted_total_fails(self):
    """Rewriting accepted_term after a credit fails even if it sums to total."""
    order = _order(
      total=8000,
      accepted_term=_split_term(amounts=(6000, 2000)),
      adjustments=[_credit(-2000)],
    )
    with self.assertRaisesRegex(AssertionError, "preserve the agreed"):
      _run_pt006(order_response=order)

  def test_altered_schedule_with_same_id_and_total_fails(self):
    """Altering split, due_at, or type with same id and total sum fails."""
    cases = [
      ("altered_split", _split_term(amounts=(5000, 5000))),
      (
        "altered_due_at",
        _split_term(due_dates=("2026-12-01T00:00:00Z", "2026-12-30T00:00:00Z")),
      ),
      ("altered_type", _split_term(schedule_types=("deposit", "final"))),
    ]
    for label, bad_term in cases:
      with (
        self.subTest(case=label),
        self.assertRaisesRegex(AssertionError, "preserve the agreed"),
      ):
        _run_pt006(order_response=_order(total=10000, accepted_term=bad_term))

  def test_missing_wrong_id_or_malformed_accepted_term_fails(self):
    """Missing accepted_term, wrong term id, or non-dict schedule fails."""
    malformed = _split_term()
    malformed["schedules"] = ["not_a_dict"]
    cases = [
      (
        "missing",
        _order(include_accepted_term=False),
        "payment.accepted_term",
      ),
      (
        "wrong_id",
        _order(accepted_term=_split_term(term_id="term_full")),
        "the accepted term must",
      ),
      (
        "malformed_schedule",
        _order(accepted_term=malformed),
        "schedule entry must be an object",
      ),
    ]
    for label, order, pattern in cases:
      with (
        self.subTest(case=label),
        self.assertRaisesRegex(AssertionError, pattern),
      ):
        _run_pt006(order_response=order)

  def test_harmless_projection_differences_pass(self):
    """Equivalent timestamps, renumbered schedule ids, and prose pass."""
    cases = [
      (
        "clock_equivalent_due_at",
        _split_term(
          due_dates=(
            "2026-12-01t00:00:00+00:00",
            "2026-12-15T02:00:00.000+02:00",
          )
        ),
      ),
      (
        "renumbered_schedule_ids",
        _split_term(schedule_ids=("ord_sched_1", "ord_sched_2")),
      ),
      (
        "reworded_prose",
        _split_term(
          title="Agreed two-part plan",
          description="Agreed installment schedule",
          schedule_descriptions=(
            "Deposit collected at booking",
            "Final payment due on 2026-12-15",
          ),
        ),
      ),
    ]
    for label, projected in cases:
      with self.subTest(case=label):
        _run_pt006(order_response=_order(accepted_term=projected))

  def test_embedded_creation_order_preserves_creation_total_guard(self):
    """Unadjusted embedded orders enforce creation total; adjusted ones pass."""
    with self.subTest(case="unadjusted_matching"):
      _run_pt006(complete_checkout=_completed(order=_order(total=10000)))

    with (
      self.subTest(case="unadjusted_mismatched_total"),
      self.assertRaisesRegex(AssertionError, "sum to the order total 11000"),
    ):
      _run_pt006(complete_checkout=_completed(order=_order(total=11000)))

    with self.subTest(case="adjusted_embedded"):
      adjusted = _order(total=8000, adjustments=[_credit(-2000)])
      _run_pt006(complete_checkout=_completed(order=adjusted))

  def test_recomputation_on_complete_with_terms_uses_completed_agreement(self):
    """Warned recomputation carrying terms anchors PT-006 on the new term."""
    recomputed = _split_term(amounts=(6480, 4320))
    completed = _completed(
      total=10800,
      include_terms=True,
      terms=[recomputed, _full_term(amount=10800)],
      selected_term_id="term_split_2",
      messages=[_TERM_CHANGED_WARNING],
    )
    _run_pt006(
      complete_checkout=completed,
      order_response=_order(
        total=8800, accepted_term=recomputed, adjustments=[_credit(-2000)]
      ),
    )
    mutated = _order(
      total=8800,
      accepted_term=_split_term(amounts=(6480, 2320)),
      adjustments=[_credit(-2000)],
    )
    with self.assertRaisesRegex(AssertionError, "preserve the agreed"):
      _run_pt006(complete_checkout=completed, order_response=mutated)

  def test_recomputation_on_complete_without_terms_narrows_honestly(self):
    """Warned recomputation omitting terms narrows to completed total."""
    completed = _completed(total=10800, messages=[_TERM_CHANGED_WARNING])
    with self.subTest(case="changed_total_adjusted_order"):
      _run_pt006(
        complete_checkout=completed,
        order_response=_order(
          total=8800,
          accepted_term=_full_term(term_id="term_recomputed", amount=10800),
          adjustments=[_credit(-2000)],
        ),
      )
    with self.subTest(case="identical_total_warned"):
      _run_pt006(
        complete_checkout=_completed(
          total=10000, messages=[_TERM_CHANGED_WARNING]
        ),
        order_response=_order(
          total=10000,
          accepted_term=_full_term(term_id="term_recomputed", amount=10000),
        ),
      )
    non_warning_unchanged_cases = [
      (
        "identical_total_info_rejects_id_change",
        _completed(total=10000, messages=[_TERM_CHANGED_INFO]),
        _order(
          total=10000,
          accepted_term=_full_term(term_id="term_recomputed", amount=10000),
        ),
        "the accepted term must carry the id",
      ),
      (
        "identical_total_info_rejects_shape_change",
        _completed(total=10000, messages=[_TERM_CHANGED_INFO]),
        _order(
          total=10000,
          accepted_term=_split_term(amounts=(5000, 5000)),
        ),
        "preserve the agreed",
      ),
      (
        "identical_total_untyped_rejects_shape_change",
        _completed(total=10000, messages=[_TERM_CHANGED_UNTYPED]),
        _order(
          total=10000,
          accepted_term=_split_term(amounts=(5000, 5000)),
        ),
        "preserve the agreed",
      ),
    ]
    for label, non_warn_complete, order, pattern in non_warning_unchanged_cases:
      with (
        self.subTest(case=label),
        self.assertRaisesRegex(AssertionError, pattern),
      ):
        _run_pt006(complete_checkout=non_warn_complete, order_response=order)
    with (
      self.subTest(case="mismatched_recomputed_sum"),
      self.assertRaisesRegex(AssertionError, "completed checkout total 10800"),
    ):
      _run_pt006(
        complete_checkout=completed,
        order_response=_order(
          total=8800,
          accepted_term=_full_term(term_id="term_recomputed", amount=8800),
          adjustments=[_credit(-2000)],
        ),
      )

  def test_completion_validation_and_async_skips(self):
    """Silent term changes, stray selected_term_id, and bad status fail."""
    recomputed_order = _order(
      total=10800,
      accepted_term=_full_term(term_id="term_recomputed", amount=10800),
    )
    fail_cases = [
      (
        "silent_change",
        _completed(
          include_terms=True,
          selected_term_id="term_full",
          messages=[],
        ),
        _order(accepted_term=_full_term()),
        "changed the selected term.*payment_term_changed",
      ),
      (
        "info_change_with_terms",
        _completed(
          include_terms=True,
          selected_term_id="term_full",
          messages=[_TERM_CHANGED_INFO],
        ),
        _order(accepted_term=_full_term()),
        "changed the selected term.*payment_term_changed",
      ),
      (
        "untyped_change_with_terms",
        _completed(
          include_terms=True,
          selected_term_id="term_full",
          messages=[_TERM_CHANGED_UNTYPED],
        ),
        _order(accepted_term=_full_term()),
        "changed the selected term.*payment_term_changed",
      ),
      (
        "silent_total_change_without_terms",
        _completed(total=10800, include_terms=False, messages=[]),
        recomputed_order,
        "changed the checkout total.*payment_term_changed",
      ),
      (
        "info_total_change_without_terms",
        _completed(
          total=10800, include_terms=False, messages=[_TERM_CHANGED_INFO]
        ),
        recomputed_order,
        "changed the checkout total.*payment_term_changed",
      ),
      (
        "untyped_total_change_without_terms",
        _completed(
          total=10800, include_terms=False, messages=[_TERM_CHANGED_UNTYPED]
        ),
        recomputed_order,
        "changed the checkout total.*payment_term_changed",
      ),
      (
        "selected_id_without_terms",
        _completed(include_terms=False, selected_term_id="term_split_2"),
        _order(),
        "selected_term_id",
      ),
      (
        "missing_status",
        _completed(status=None),
        _order(),
        "expected completed checkout",
      ),
      (
        "missing_total",
        _completed(total=None, messages=[_TERM_CHANGED_WARNING]),
        _order(),
        "completed checkout must carry a 'total'",
      ),
    ]
    for label, completed, order, pattern in fail_cases:
      with (
        self.subTest(case=label),
        self.assertRaisesRegex(AssertionError, pattern),
      ):
        _run_pt006(complete_checkout=completed, order_response=order)

    with self.subTest(case="warned_change_with_terms"):
      _run_pt006(
        complete_checkout=_completed(
          include_terms=True,
          selected_term_id="term_full",
          messages=[_TERM_CHANGED_WARNING],
        ),
        order_response=_order(accepted_term=_full_term()),
      )

    with self.subTest(case="warned_total_change_without_terms"):
      _run_pt006(
        complete_checkout=_completed(
          total=10800,
          include_terms=False,
          messages=[_TERM_CHANGED_WARNING],
        ),
        order_response=recomputed_order,
      )

    for status in ("complete_in_progress", "requires_escalation"):
      with (
        self.subTest(async_status=status),
        self.assertRaises(unittest.SkipTest),
      ):
        _run_pt006(complete_checkout=_completed(status=status, order=None))

  def test_direct_complete_avoids_hidden_fulfillment_put(self):
    """Completing a ready checkout sends no hidden PUT after ready."""
    recorded: list[tuple[str, str]] = []
    puts = {"n": 0}
    ready = _checkout()
    ready["fulfillment"]["methods"][0]["groups"][0].pop(
      "selected_option_id", None
    )

    def on_put(current, _request):
      puts["n"] += 1
      updated = copy.deepcopy(current)
      if puts["n"] >= 2:
        updated["totals"] = [
          {"type": "subtotal", "amount": 10500},
          {"type": "total", "amount": 10500},
        ]
        updated["payment"]["terms"] = [
          _split_term(amounts=(6500, 4000)),
          _full_term(amount=10500),
        ]
      return updated

    _run_pt006(ready_checkout=ready, on_put=on_put, recorded=recorded)
    self.assertEqual(puts["n"], 1)
    put_idx = next(i for i, (m, _) in enumerate(recorded) if m == "PUT")
    self.assertEqual(
      recorded[put_idx + 1], ("POST", "/checkout-sessions/chk_1/complete")
    )


if __name__ == "__main__":
  unittest.main()
