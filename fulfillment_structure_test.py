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

"""Structural conformance tests for the fulfillment extension (fulfillment.md).

These tests assert two normative invariants of the checkout ``fulfillment``
structure that hold independent of the specific rates or packaging a
business computes, so they are server-agnostic and operate on the raw wire
payload (``response.json()``) rather than the parsed SDK model:

- FUL-001 — Rendering / Business Responsibilities, ``options[].title``:
  "MUST distinguish this option from its siblings." Within a group, every
  option's ``title`` must be a non-empty string and must be distinct from
  its siblings' titles.

- FUL-008 — Business Response Behavior, default (``supports_multi_group``
  absent / ``false``): "Business MUST consolidate all items into a single
  group per method." A method covering several line items must return
  exactly one group, and that group must cover all of the method's items.

The existing ``fulfillment_test.py`` exercises address injection/selection,
geography-driven option generation, free-shipping pricing and totals
roll-up, but asserts none of these two structural invariants. The
requirements the reference does not exercise are recorded but not tested
here: ``options[].description`` (MUST NOT repeat title/total) and
``available_methods[].description`` (MUST be a standalone sentence) — the
reference server emits neither field, so there is nothing to assert; the
``options[].title`` "sufficient for buyer decision" clause is a semantic
judgement no machine check can make; and the platform/consumer MUSTs
(preserve grouping, use per-method availability) bind the client, not the
server under test.
"""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import checkout_create_request
from ucp_sdk.models.schemas.shopping import payment_create_request
from ucp_sdk.models.schemas import payment_handler
from ucp_sdk.models.schemas.shopping.types import item_create_request
from ucp_sdk.models.schemas.shopping.types import line_item_create_request
import uuid

_FULFILLMENT_CAPABILITY = "dev.ucp.shopping.fulfillment"


def _methods(response_json: dict) -> list[dict]:
  """Return the fulfillment ``methods[]`` from a raw checkout response.

  Tolerates the SDK's ``RootModel`` wrapping (``fulfillment.root``) that a
  serializer may or may not flatten.
  """
  fulfillment = response_json.get("fulfillment") or {}
  if "methods" not in fulfillment and isinstance(fulfillment.get("root"), dict):
    fulfillment = fulfillment["root"]
  return fulfillment.get("methods") or []


class FulfillmentStructureTest(integration_test_utils.IntegrationTestBase):
  """Structural invariants of the checkout fulfillment tree.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  """

  def setUp(self) -> None:
    """Skip unless the business advertises the fulfillment capability."""
    super().setUp()
    if not self._advertises_fulfillment():
      self.skipTest(
        f"business does not advertise {_FULFILLMENT_CAPABILITY}; skipping"
      )

  def _advertises_fulfillment(self) -> bool:
    """Return True if discovery advertises the fulfillment capability."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    names = (
      list(caps.keys())
      if isinstance(caps, dict)
      else [c.get("name") for c in caps if isinstance(c, dict)]
    )
    return _FULFILLMENT_CAPABILITY in names

  def _checkout_with_options(self) -> dict:
    """Create a checkout and drive it to a state that carries options.

    Creates a session, then updates it with the configured destination so
    the business generates fulfillment ``options[]``, and returns the raw
    update response JSON.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**response_json)

    dest = self.fixture_ctx.get_test_destination()
    destination = {
      "id": "dest_1",
      "address_country": dest.get("address_country", dest.get("country", "US")),
      "postal_code": dest.get("postal_code", "94105"),
    }
    fulfillment_payload = {
      "methods": [
        {
          "type": "shipping",
          "id": "method_1",
          "line_item_ids": [checkout_obj.line_items[0].id],
          "destinations": [destination],
          "selected_destination_id": "dest_1",
        }
      ]
    }
    return self.update_checkout_session(
      checkout_obj, fulfillment=fulfillment_payload
    )

  def test_option_titles_distinguish_siblings(self) -> None:
    """FUL-001: sibling ``options[].title`` are non-empty and distinct.

    Given a checkout whose business has generated fulfillment options,
    When a group carries options,
    Then each option's ``title`` is a non-empty string and no two siblings
    in the same group share a title (fulfillment.md: ``options[].title``
    "MUST distinguish this option from its siblings").
    """
    response_json = self._checkout_with_options()

    groups_with_options = 0
    for method in _methods(response_json):
      for group in method.get("groups") or []:
        options = group.get("options") or []
        if not options:
          continue
        groups_with_options += 1
        titles = []
        for opt in options:
          title = opt.get("title")
          self.assertIsInstance(
            title,
            str,
            msg=f"Option {opt.get('id')!r} title must be a string: {opt}",
          )
          self.assertTrue(
            title.strip(),
            msg=f"Option {opt.get('id')!r} title must be non-empty: {opt}",
          )
          titles.append(title)
        self.assertEqual(
          len(titles),
          len(set(titles)),
          msg=(
            "Sibling option titles must be distinct within a group; got"
            f" duplicates in {titles}"
          ),
        )

    if groups_with_options == 0:
      self.skipTest("server generated no fulfillment options to assert on")

  def test_default_config_single_group_per_method(self) -> None:
    """FUL-008: under default config a method has one consolidating group.

    Given a cart with several line items assigned to one shipping method,
    When the platform declares no ``supports_multi_group`` (the default),
    Then each returned method carries exactly one group, and that group
    covers all of the method's line items (fulfillment.md, Business
    Response Behavior: "Business MUST consolidate all items into a single
    group per method").
    """
    item_id = self.fixture_ctx.get_test_sku()
    price = self.fixture_ctx.get_test_price()
    currency = self.conformance_config.get("currency", "USD")
    version = self.conformance_config.get("ucp_version", "2026-04-08")

    handlers = [
      payment_handler.Base(
        id="google_pay",
        name="google.pay",
        version=version,
        spec="https://example.com/spec",
        config_schema="https://example.com/schema",
        instrument_schemas=["https://example.com/instrument_schema"],
        config={},
      )
    ]

    line_item_ids = ["li_1", "li_2"]
    line_items = []
    for li_id in line_item_ids:
      li = line_item_create_request.LineItemCreateRequest(
        quantity=1,
        item=item_create_request.ItemCreateRequest(id=item_id),
      )
      li.id = li_id
      li.item.price = price
      li.totals = []
      line_items.append(li)

    payment = payment_create_request.PaymentCreateRequest(
      instruments=[],
      handlers=[h.model_dump(mode="json", exclude_none=True) for h in handlers],
    )

    dest = self.fixture_ctx.get_test_destination()
    create_req = checkout_create_request.CheckoutCreateRequest(
      id=str(uuid.uuid4()),
      currency=currency,
      line_items=line_items,
      payment=payment,
      fulfillment={
        "methods": [
          {
            "id": "method_1",
            "type": "shipping",
            "line_item_ids": line_item_ids,
            "destinations": [
              {
                "id": "dest_1",
                "address_country": dest.get(
                  "address_country", dest.get("country", "US")
                ),
                "postal_code": dest.get("postal_code", "94105"),
              }
            ],
            "selected_destination_id": "dest_1",
          }
        ]
      },
    )
    create_req.status = "incomplete"
    create_req.ucp = {"version": version}
    create_req.totals = []
    create_req.links = []

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_req.model_dump(mode="json", by_alias=True, exclude_none=True),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    response_json = response.json()

    methods = _methods(response_json)
    self.assertTrue(
      methods, msg=f"No fulfillment methods returned: {response_json}"
    )

    methods_with_groups = 0
    for method in methods:
      groups = method.get("groups")
      if not groups:
        # A server MAY defer group generation; only assert when present.
        continue
      methods_with_groups += 1
      self.assertEqual(
        len(groups),
        1,
        msg=(
          "Default config (no supports_multi_group) requires exactly one"
          f" group per method; method {method.get('id')!r} returned"
          f" {len(groups)} groups: {groups}"
        ),
      )
      method_items = set(method.get("line_item_ids") or [])
      group_items = set(groups[0].get("line_item_ids") or [])
      self.assertEqual(
        group_items,
        method_items,
        msg=(
          "The single group must consolidate all of the method's line"
          f" items; method {method.get('id')!r} items {method_items} vs"
          f" group items {group_items}"
        ),
      )

    if methods_with_groups == 0:
      self.skipTest("server returned no fulfillment groups to assert on")


if __name__ == "__main__":
  absltest.main()
