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

"""Destination selection tests for the fulfillment extension (fulfillment.md).

Both tests drive `selected_destination_id` on a shipping method and read the
wire JSON of the response, so they hold for any business that quotes
fulfillment for a platform-authored shipping address:

- a `selected_destination_id` the business does not recognize is refused and
  the stored checkout is left as it was (Selection and Location Identity);
- an id the business recognized earlier carries no stale terms: when the
  platform re-submits it with the current content of the destination, the
  business quotes that content (Selection and Location Identity, revalidation).

The other business MUSTs of the page that no test cites are not checked here
on purpose: the reference server advertises no catalog, no pickup location and
no `options[].description`, so a test of `filters.fulfills_to`, of the
`business_location` destination shape, of the catalog `location` id, or of
`description` repeating `title` would skip on every run.
"""

import re
import uuid

from absl.testing import absltest
from framework.decorators import (
  partly,
  requires_capability,
  spec_assert,
)
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout

_FULFILLMENT_CAPABILITY = "dev.ucp.shopping.fulfillment"


# A message for a rejected selection selects the attempted method and its
# selected_destination_id, e.g. $.fulfillment.methods[0].selected_destination_id
def _dotted(path: str) -> str:
  """Rewrite RFC 9535 bracketed member names to the dot form.

  `path` MUST be an RFC 9535 JSONPath expression (checkout.md, The `path`
  Field), and the specification exemplifies the bracket form, so both
  spellings of the same location have to match.
  """
  return re.sub(r"\[(['\"])([^'\"\]]+)\1\]", r".\2", path)


_SELECTED_DESTINATION_PATH = re.compile(
  r"^\$\.fulfillment\.methods\[\d+\]\.selected_destination_id$"
)

# Fields a rejected update must not have changed. `messages` is left out
# because the rejection itself is reported there, and `expires_at` because
# it is session timing, not checkout state.
_UNCHANGED_FIELDS = (
  "status",
  "currency",
  "line_items",
  "totals",
  "fulfillment",
  "buyer",
  "payment",
  "discounts",
)


def _methods(response_json: dict) -> list[dict]:
  """Return the fulfillment ``methods[]`` from a raw checkout response."""
  fulfillment = response_json.get("fulfillment") or {}
  if "methods" not in fulfillment and isinstance(fulfillment.get("root"), dict):
    fulfillment = fulfillment["root"]
  return fulfillment.get("methods") or []


def _option_ids(response_json: dict) -> set[str]:
  """Return every option id quoted anywhere in the response."""
  ids = set()
  for method in _methods(response_json):
    for group in method.get("groups") or []:
      for option in group.get("options") or []:
        if option.get("id"):
          ids.add(option["id"])
  return ids


def _error_messages(response_json: dict) -> list[dict]:
  """Return the ``type: "error"`` entries in the messages of a response."""
  return [
    m
    for m in response_json.get("messages") or []
    if isinstance(m, dict) and m.get("type") == "error"
  ]


@requires_capability(_FULFILLMENT_CAPABILITY)
class FulfillmentSelectionTest(ShoppingIntegrationTestBase):
  """Selecting a shipping destination by id.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - GET /checkout-sessions/{id}
  """

  def _shipping_destination(self, dest: dict, dest_id: str) -> dict:
    """Build a platform-authored shipping destination from fixture content."""
    return {
      "id": dest_id,
      "type": "shipping_address",
      "street_address": dest["street_address"],
      "address_locality": dest["address_locality"],
      "address_region": dest.get("address_region"),
      "postal_code": dest["postal_code"],
      "address_country": dest["address_country"],
    }

  def _shipping_method(
    self, line_item_id: str, destination: dict, selected_id: str
  ) -> dict:
    return {
      "methods": [
        {
          "id": "method_1",
          "type": "shipping",
          "line_item_ids": [line_item_id],
          "destinations": [destination],
          "selected_destination_id": selected_id,
        }
      ]
    }

  def _put_raw(self, current: dict, fulfillment: dict):
    """PUT a full-replacement update and return the raw response.

    Unlike `update_checkout_session`, this does not assert the status, so a
    rejected update can be examined whatever shape the business gives it.
    """
    body = {
      "id": current["id"],
      "currency": current["currency"],
      "line_items": [
        {
          "id": li["id"],
          "quantity": li["quantity"],
          "item": {"id": li["item"]["id"]},
        }
        for li in current["line_items"]
      ],
      "payment": {"instruments": []},
      "fulfillment": fulfillment,
    }
    if current.get("buyer"):
      body["buyer"] = current["buyer"]
    return self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{current['id']}"),
      json=body,
      headers=self.get_headers(),
    )

  def _get_raw(self, checkout_id: str) -> dict:
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response.json()

  @spec_assert(
    "REQ-SHOPPING-FULFILLMENT-0016",  # a rejected selection leaves it unchanged
    partly(
      "REQ-SHOPPING-FULFILLMENT-0017",  # ...returned, recoverable, with path
      "an error response is accepted as the rejection: no 2026-04-08 rule "
      "governs a rejected selection and the flower shop reference answers "
      "HTTP 400, so on that branch nothing of this rule is asserted. The "
      "returned Checkout, the recoverable severity and the path to "
      "selected_destination_id are checked only when the business answers "
      "200",
    ),
  )
  def test_unrecognized_selected_destination_is_rejected_unchanged(
    self,
  ) -> None:
    """An unrecognized selected_destination_id is refused, not absorbed.

    Given a checkout with a selected shipping destination,
    When the platform submits a selected_destination_id the business cannot
    recognize for that method,
    Then the business reports an error rather than accepting the id, and a
    subsequent read returns the checkout exactly as it was.
    """
    created = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**created)
    line_item_id = created["line_items"][0]["id"]

    known = self._shipping_destination(
      self.fixture_ctx.get_test_destination(), "dest_1"
    )
    accepted = self.update_checkout_session(
      checkout_obj,
      fulfillment=self._shipping_method(line_item_id, known, "dest_1"),
    )
    self.assertEqual(
      _methods(accepted)[0].get("selected_destination_id"),
      "dest_1",
      f"The known destination was not selected: {accepted}",
    )

    before = self._get_raw(created["id"])

    unknown_id = f"dest_unknown_{uuid.uuid4().hex[:12]}"
    response = self._put_raw(
      before, self._shipping_method(line_item_id, known, unknown_id)
    )
    if 400 <= response.status_code < 500:
      # The reference answers an error response. No 2026-04-08 rule governs
      # a rejected selection, so the status is taken as the rejection and the
      # shape of the error body is left to the REST binding tests.
      pass
    elif response.status_code == 200:
      body = response.json()
      errors = _error_messages(body)
      # The 2026-08-25 shape: the unchanged checkout with a recoverable error
      # whose path selects the selected_destination_id of the attempted method.
      self.assertTrue(
        errors,
        "An unrecognized selected_destination_id must not be accepted "
        f"silently; the response carries no error Message: {body}",
      )
      selecting = [
        m
        for m in errors
        if _SELECTED_DESTINATION_PATH.match(_dotted(str(m.get("path", ""))))
      ]
      self.assertTrue(
        selecting,
        "No error Message selects the selected_destination_id of the "
        f"attempted method; messages: {errors}",
      )
      self.assertTrue(
        any(m.get("severity") == "recoverable" for m in selecting),
        "No error Message selecting selected_destination_id is "
        f"recoverable: {selecting}",
      )
      self.assertEqual(
        body.get("fulfillment"),
        before.get("fulfillment"),
        "The returned Checkout must be the current one, not the rejected "
        f"update; got {body.get('fulfillment')}",
      )
    else:
      self.fail(
        f"Unexpected status {response.status_code} for a rejected update:"
        f" {response.text}"
      )

    after = self._get_raw(created["id"])
    for field in _UNCHANGED_FIELDS:
      self.assertEqual(
        after.get(field),
        before.get(field),
        f"A rejected update must leave the Checkout unchanged; {field!r} "
        f"changed from {before.get(field)} to {after.get(field)}",
      )

  @spec_assert(
    partly(
      "REQ-SHOPPING-FULFILLMENT-0014",  # revalidate the terms of a selection
      "terms only: the options quoted must follow the current content of "
      "the destination; availability and inventory are not checked, a "
      "destination the business no longer serves is not exercised, and an "
      "option quoted in addition to the expected one is not judged",
    ),
  )
  def test_reselected_destination_is_quoted_on_its_current_content(
    self,
  ) -> None:
    """A recognized id carries no stale terms.

    Given a checkout whose shipping destination dest_1 holds the domestic
    address from the fixture and has been quoted,
    When the platform re-submits dest_1 as the selection with the
    international address as its content,
    Then the options quoted are those the fixture expects for the
    international destination, not the ones quoted earlier for the id.
    """
    dynamic = self.fixture_ctx.get_dynamic_fulfillment()
    if dynamic is None:
      self.skipTest(
        "No dynamic fulfillment cases configured in fixtures "
        "(test_fixtures.dynamic_fulfillment)."
      )
    domestic = dynamic["domestic"]
    international = dynamic["international"]

    created = self.create_checkout_session(select_fulfillment=False)
    checkout_obj = checkout.Checkout(**created)
    line_item_id = created["line_items"][0]["id"]

    first = self.update_checkout_session(
      checkout_obj,
      fulfillment=self._shipping_method(
        line_item_id,
        self._shipping_destination(domestic["destination"], "dest_1"),
        "dest_1",
      ),
    )
    self.assertIn(
      domestic["expected_option_id"],
      _option_ids(first),
      "The domestic destination was not quoted with its expected option; "
      f"got {_option_ids(first)}",
    )

    # A business is allowed to refuse the re-bind instead of re-quoting: the
    # text says one that cannot honor the submitted selection rejects it
    # rather than replacing it, and an id may name an address the business
    # resolves from its own records. Refusal is therefore not a failure here,
    # it only means stale terms cannot be exercised against this business.
    response = self._put_raw(
      first,
      self._shipping_method(
        line_item_id,
        self._shipping_destination(international["destination"], "dest_1"),
        "dest_1",
      ),
    )
    if 400 <= response.status_code < 500:
      self.skipTest(
        "The business refused to re-bind dest_1 to new content, which the "
        "text allows; stale terms cannot be exercised here"
      )
    self.assertEqual(
      response.status_code,
      200,
      f"Unexpected status re-submitting dest_1: {response.status_code}",
    )
    second = response.json()
    refused = [
      m
      for m in _error_messages(second)
      if _SELECTED_DESTINATION_PATH.match(_dotted(str(m.get("path", ""))))
    ]
    if refused:
      self.skipTest(
        "The business refused to re-bind dest_1 to new content, which the "
        f"text allows; it reported {refused}"
      )
    self.assertIn(
      international["expected_option_id"],
      _option_ids(second),
      "Re-submitting dest_1 with the international address must quote the "
      f"international option {international['expected_option_id']!r}; the "
      f"business returned {_option_ids(second)}, the terms quoted for the "
      "earlier content of that id",
    )


if __name__ == "__main__":
  absltest.main()
