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

"""A scripted checkout server with the postures the sample server lacks.

The sample server completes a checkout without a buyer, answers business
outcomes with a 4xx and always returns a ``payment`` member, so it cannot
show whether the suite copes with a business that does otherwise, within
what the spec allows. This store does otherwise:

* it needs a buyer email to place an order, and answers a completion
  without one the way the spec's own Create Checkout example answers a
  create without one: HTTP 200, ``status: incomplete`` and a recoverable
  error at ``$.buyer.email``;
* it returns no ``payment`` member (the schema leaves it optional);
* it refuses calls on a terminal session in-band: HTTP 200, an Error
  Response with ``ucp.status: "error"`` and no resource;
* it builds the fulfillment options itself from the destination the
  platform sends, and reports an unknown ``selected_option_id`` as an error.

It serves ``httpx`` through a ``MockTransport``, so a test drives the suite's
own helpers against it without a socket. ``StrictStore.requests`` keeps
every request, for assertions on what the suite sent.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

ENDPOINT = "https://store.example/ucp"

_OPTIONS = [
  {
    "id": "ground",
    "title": "Ground",
    "totals": [{"type": "total", "amount": 599}],
  },
  {
    "id": "express",
    "title": "Express",
    "totals": [{"type": "total", "amount": 1599}],
  },
]


def _error(code: str, path: str, content: str) -> dict[str, Any]:
  return {
    "type": "error",
    "code": code,
    "path": path,
    "content": content,
    "severity": "recoverable",
  }


class StrictStore:
  """The scripted store; see the module docstring for its postures."""

  def __init__(
    self,
    *,
    needs_buyer: bool = True,
    tax: int = 0,
    async_completion: bool = False,
  ) -> None:
    """Configure the store.

    Args:
      needs_buyer: Refuse to place an order until the checkout has a buyer
        email, with a recoverable error at `$.buyer.email`.
      tax: A tax amount (minor units) added to the totals when non-zero.
      async_completion: Answer Complete Checkout with
        `complete_in_progress` and place the order on the next Get.

    """
    self.needs_buyer = needs_buyer
    self.tax = tax
    self.async_completion = async_completion
    self.checkouts: dict[str, dict[str, Any]] = {}
    self.requests: list[httpx.Request] = []
    self._pending: set[str] = set()
    self._counter = 0

  # -- httpx plumbing -------------------------------------------------------

  def client(self) -> httpx.Client:
    """Return a client whose every request lands in this store."""
    return httpx.Client(transport=httpx.MockTransport(self.handle))

  def handle(self, request: httpx.Request) -> httpx.Response:
    """Route one request."""
    self.requests.append(request)
    path = request.url.path
    body = json.loads(request.content) if request.content else {}
    prefix = httpx.URL(ENDPOINT).path
    assert path.startswith(prefix), path
    route = path[len(prefix) :].strip("/").split("/")

    if route == ["checkout-sessions"] and request.method == "POST":
      return self._create(body)
    if len(route) == 2 and route[0] == "checkout-sessions":
      checkout = self.checkouts.get(route[1])
      if checkout is None:
        return self._not_found(route[1])
      if request.method == "GET":
        return self._get(checkout)
      if request.method == "PUT":
        return self._update(checkout, body)
    if len(route) == 3 and route[0] == "checkout-sessions":
      checkout = self.checkouts.get(route[1])
      if checkout is None:
        return self._not_found(route[1])
      if route[2] == "complete" and request.method == "POST":
        return self._complete(checkout, body)
      if route[2] == "cancel" and request.method == "POST":
        return self._cancel(checkout)
    return httpx.Response(404, json={"detail": f"no route {path}"})

    # -- what the store does ------------------------------------------------

  def _new_id(self, prefix: str) -> str:
    self._counter += 1
    return f"{prefix}_{self._counter}"

  def _create(self, body: dict[str, Any]) -> httpx.Response:
    checkout: dict[str, Any] = {
      "ucp": {"version": "2026-08-25"},
      "id": self._new_id("chk"),
      "status": "incomplete",
      "currency": "USD",
      "line_items": [
        {
          "id": self._new_id("li"),
          "item": {"id": li["item"]["id"], "title": "Thing", "price": 105000},
          "quantity": li["quantity"],
          "totals": [
            {"type": "subtotal", "amount": 105000 * li["quantity"]},
            {"type": "total", "amount": 105000 * li["quantity"]},
          ],
        }
        for li in body.get("line_items", [])
      ],
      "links": [{"type": "privacy_policy", "url": "https://store.example/p"}],
      "messages": [],
    }
    if body.get("buyer"):
      checkout["buyer"] = dict(body["buyer"])
    self._apply_fulfillment(checkout, body.get("fulfillment"))
    self._settle(checkout)
    self.checkouts[checkout["id"]] = checkout
    return httpx.Response(201, json=self._view(checkout))

  def _get(self, checkout: dict[str, Any]) -> httpx.Response:
    if checkout["id"] in self._pending:
      self._pending.discard(checkout["id"])
      self._place(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _update(
    self, checkout: dict[str, Any], body: dict[str, Any]
  ) -> httpx.Response:
    if checkout["status"] in ("completed", "canceled"):
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot update {checkout['status']} checkout: {checkout['id']}",
      )
    if "buyer" in body:
      checkout["buyer"] = dict(body["buyer"]) if body["buyer"] else None
    if body.get("line_items"):
      for li in checkout["line_items"]:
        for wanted in body["line_items"]:
          if wanted.get("id") == li["id"]:
            li["quantity"] = wanted.get("quantity", li["quantity"])
            li["totals"] = [
              {"type": "subtotal", "amount": 105000 * li["quantity"]},
              {"type": "total", "amount": 105000 * li["quantity"]},
            ]
    if "fulfillment" in body:
      self._apply_fulfillment(checkout, body["fulfillment"])
    if body.get("discounts"):
      checkout["discounts"] = {"codes": body["discounts"].get("codes", [])}
    self._settle(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _complete(
    self, checkout: dict[str, Any], body: dict[str, Any]
  ) -> httpx.Response:
    if checkout["status"] in ("completed", "canceled"):
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot complete already {checkout['status']} checkout:"
        f" {checkout['id']}",
      )
    self._settle(checkout)
    if checkout["status"] != "ready_for_complete":
      # The spec's posture for a blocked completion: the current checkout,
      # still incomplete, with the recoverable errors that block it.
      return httpx.Response(200, json=self._view(checkout))
    token = (
      ((body.get("payment") or {}).get("instruments") or [{}])[0]
      .get("credential", {})
      .get("token")
    )
    if token == "fail_token":
      checkout["status"] = "incomplete"
      checkout["messages"] = [
        _error("payment_declined", "$.payment", "The card was declined.")
      ]
      return httpx.Response(200, json=self._view(checkout))
    if self.async_completion:
      checkout["status"] = "complete_in_progress"
      self._pending.add(checkout["id"])
      return httpx.Response(200, json=self._view(checkout))
    self._place(checkout)
    return httpx.Response(200, json=self._view(checkout))

  def _cancel(self, checkout: dict[str, Any]) -> httpx.Response:
    if checkout["status"] == "completed":
      return self._error_response(
        "invalid_parameter",
        "$.status",
        f"Cannot cancel completed checkout: {checkout['id']}",
      )
    if checkout["status"] == "canceled":
      return self._not_found(checkout["id"])
    checkout["status"] = "canceled"
    return httpx.Response(200, json=self._view(checkout))

  def _place(self, checkout: dict[str, Any]) -> None:
    checkout["status"] = "completed"
    checkout["messages"] = []
    checkout["order"] = {
      "id": self._new_id("ord"),
      "permalink_url": f"https://store.example/orders/{checkout['id']}",
    }

  def _apply_fulfillment(
    self, checkout: dict[str, Any], fulfillment: dict[str, Any] | None
  ) -> None:
    """Build the method, its groups and options from what the platform sent."""
    if not fulfillment or not fulfillment.get("methods"):
      checkout.pop("fulfillment", None)
      return
    sent = fulfillment["methods"][0]
    destinations = [
      {**d, "id": d.get("id") or self._new_id("dest")}
      for d in sent.get("destinations", [])
    ]
    selected_destination = sent.get("selected_destination_id")
    if selected_destination not in {d["id"] for d in destinations}:
      selected_destination = None
    sent_groups = sent.get("groups") or [{}]
    selected_option = sent_groups[0].get("selected_option_id")
    problems = []
    if selected_option and selected_option not in {o["id"] for o in _OPTIONS}:
      problems.append(
        _error(
          "option_unavailable",
          "$.fulfillment.methods[0].groups[0].selected_option_id",
          f"The shipping option {selected_option!r} is not available.",
        )
      )
      selected_option = None
    for d in destinations:
      if not d.get("address_locality"):
        problems.append(
          _error(
            "missing",
            "$.fulfillment.methods[0].destinations[0].address_locality",
            "The city is required.",
          )
        )
    checkout["fulfillment"] = {
      "methods": [
        {
          "id": "method_1",
          "type": "shipping",
          "line_item_ids": [li["id"] for li in checkout["line_items"]],
          "destinations": destinations,
          "selected_destination_id": selected_destination,
          "groups": [
            {
              "id": "group_1",
              "line_item_ids": [li["id"] for li in checkout["line_items"]],
              "options": copy.deepcopy(_OPTIONS),
              "selected_option_id": selected_option,
            }
          ],
        }
      ]
    }
    checkout["_fulfillment_problems"] = problems

  def _settle(self, checkout: dict[str, Any]) -> None:
    """Recompute messages, totals and status from the checkout's state."""
    if checkout["status"] in ("completed", "canceled", "complete_in_progress"):
      return
    problems: list[dict[str, Any]] = list(
      checkout.get("_fulfillment_problems") or []
    )
    method = (checkout.get("fulfillment") or {}).get("methods", [None])[0]
    option = None
    if method is None or not method.get("selected_destination_id"):
      problems.append(
        _error(
          "address_unverifiable",
          "$.fulfillment",
          "Shipping address is required to complete checkout.",
        )
      )
    else:
      selected = method["groups"][0].get("selected_option_id")
      option = next((o for o in _OPTIONS if o["id"] == selected), None)
      if option is None:
        problems.append(
          _error(
            "missing",
            "$.fulfillment.methods[0].groups[0].selected_option_id",
            "Choose a shipping option.",
          )
        )
    if self.needs_buyer and not (checkout.get("buyer") or {}).get("email"):
      problems.append(
        _error(
          "missing_buyer_info",
          "$.buyer.email",
          "Buyer email is required to complete checkout.",
        )
      )
    subtotal = sum(li["totals"][0]["amount"] for li in checkout["line_items"])
    shipping = option["totals"][0]["amount"] if option else 0
    totals = [
      {"type": "subtotal", "amount": subtotal},
      {"type": "fulfillment", "amount": shipping},
    ]
    if self.tax:
      totals.append({"type": "tax", "amount": self.tax})
    totals.append(
      {"type": "total", "amount": subtotal + shipping + (self.tax or 0)}
    )
    checkout["totals"] = totals
    checkout["messages"] = problems
    checkout["status"] = "incomplete" if problems else "ready_for_complete"

  # -- response shapes ------------------------------------------------------

  @staticmethod
  def _view(checkout: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in checkout.items() if not k.startswith("_")}
    if view.get("buyer") is None:
      view.pop("buyer", None)
    return view

  @staticmethod
  def _error_response(code: str, path: str, content: str) -> httpx.Response:
    return httpx.Response(
      200,
      json={
        "ucp": {"version": "2026-08-25", "status": "error"},
        "messages": [_error(code, path, content)],
      },
    )

  @staticmethod
  def _not_found(checkout_id: str) -> httpx.Response:
    return httpx.Response(
      200,
      json={
        "ucp": {"version": "2026-08-25", "status": "error"},
        "messages": [
          {
            "type": "error",
            "code": "not_found",
            "content": f"Checkout '{checkout_id}' not found.",
          }
        ],
      },
    )
