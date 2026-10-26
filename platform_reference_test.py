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

"""Conformance for the checkout ``platform_reference`` contract (ucp#413).

ucp#839 adds ``platform_reference``: an opaque, platform-supplied correlation
value that lets a platform map later checkout responses, orders and order
events back to its own records without storing business-assigned ids. The
contract agreed on ucp#413 and specified in ucp#839:

  * The platform MAY set it on Create Checkout; it is immutable afterwards
    (``ucp_request``: create optional, update omit, complete omit).
  * The business MUST echo it verbatim on every checkout response, MUST
    carry it onto the order created from that checkout, and MUST include it
    in every order event webhook for that order. It is opaque: the business
    MUST NOT interpret, parse or modify it.
  * A value received on Update or Complete MUST be ignored; the value set on
    Create is retained.
  * ``maxLength`` is 512.

Scope: a single business (ucp#413 agreed single-Business first). The
multi-Business fan-out (one platform reference across checkouts at several
businesses, whose ``checkout_id`` values may collide) needs a second server
under test and is left for a follow-up.

Gating: the field is new in the release that ships ucp#839, so the module
skips unless discovery advertises ``dev.ucp.shopping.checkout`` at or after
``_MIN_VERSION``. Set ``UCP_CONFORMANCE_PREVIEW=platform_reference`` to run it
against a server that implements the field ahead of that release.

Not covered here, pending clarification in ucp#839 review: behavior for a
value over 512 characters (reject vs. otherwise), and whether a strict
business may reject a resent value instead of ignoring it.
"""

import os
import time
from typing import Any

from absl.testing import absltest
import integration_test_utils

_CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"
_ORDER_CAPABILITY = "dev.ucp.shopping.order"

# First release expected to carry ucp#839. Update when the release ships.
_MIN_VERSION = "2026-12-08"
_PREVIEW_ENV = "UCP_CONFORMANCE_PREVIEW"
_PREVIEW_TOKEN = "platform_reference"

_FIELD = "platform_reference"
# Mixed case and punctuation catch lowercasing, trimming and normalization.
_REF = "pr_01J9Z6R7NQ2K4T8V0X3Y5B7C9D-Mx.~_:"
_REF_ALT = "pr_01J9Z6R8ALT0000000000000000-Zz.~_:"
_REF_MAX = "a" * 512


class PlatformReferenceTest(integration_test_utils.IntegrationTestBase):
  """Checkout ``platform_reference``: create, immutability, order, events."""

  def setUp(self) -> None:
    """Gate on the advertised version and start the webhook receiver."""
    super().setUp()
    versions = self._advertised_versions(_CHECKOUT_CAPABILITY)
    if not versions:
      self.skipTest(f"business does not advertise {_CHECKOUT_CAPABILITY}")
    preview = _PREVIEW_TOKEN in os.getenv(_PREVIEW_ENV, "").split(",")
    if max(versions) < _MIN_VERSION and not preview:
      self.skipTest(
        f"{_CHECKOUT_CAPABILITY} advertised at {max(versions)}; "
        f"{_FIELD} (ucp#839) applies from {_MIN_VERSION}. Set "
        f"{_PREVIEW_ENV}={_PREVIEW_TOKEN} to run it anyway."
      )
    port = integration_test_utils.FLAGS.mock_webhook_port
    self.webhook_server = integration_test_utils.MockWebhookServer(port=port)
    self.webhook_server.start()

  def tearDown(self) -> None:
    """Stop the webhook receiver."""
    if hasattr(self, "webhook_server"):
      self.webhook_server.stop()
    super().tearDown()

  # ── discovery ───────────────────────────────────────────────────────────
  def _advertised_versions(self, capability: str) -> list[str]:
    """Return the versions discovery advertises for a capability."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    if isinstance(caps, dict):
      entries = caps.get(capability) or []
    else:
      entries = [c for c in caps if c.get("name") == capability]
    if isinstance(entries, dict):
      entries = [entries]
    return [str(e.get("version")) for e in entries if e.get("version")]

  def _require_order(self) -> None:
    """Skip unless discovery advertises the order capability."""
    if not self._advertised_versions(_ORDER_CAPABILITY):
      self.skipTest(f"business does not advertise {_ORDER_CAPABILITY}")

  # ── raw-wire drivers ────────────────────────────────────────────────────
  def _create(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a checkout from raw JSON, adding ``extra`` members.

    The shared helper builds the request from SDK models that do not declare
    the field, so the body is dumped to a dict and extended here.
    """
    payload = self.create_checkout_payload().model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    payload.update(extra or {})
    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    return response.json()

  def _get_checkout(self, checkout_id: str) -> dict[str, Any]:
    """Get Checkout."""
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response.json()

  def _update(
    self, checkout: dict[str, Any], extra: dict[str, Any] | None = None
  ) -> Any:
    """Send a full-replacement Update built from a checkout response."""
    payload: dict[str, Any] = {
      "id": checkout["id"],
      "line_items": [
        {
          "id": li["id"],
          "item": {"id": li["item"]["id"]},
          "quantity": li["quantity"],
        }
        for li in checkout["line_items"]
      ],
    }
    for member in ("payment", "fulfillment", "buyer"):
      if checkout.get(member) is not None:
        payload[member] = checkout[member]
    payload.update(extra or {})
    return self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout['id']}"),
      json=payload,
      headers=self.get_headers(),
    )

  def _complete(
    self, checkout_id: str, extra: dict[str, Any] | None = None
  ) -> dict[str, Any]:
    """Complete a checkout, adding ``extra`` members to the request."""
    self.ensure_fulfillment_ready(checkout_id)
    payload = integration_test_utils.get_valid_payment_payload()
    payload.update(extra or {})
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response.json()

  def _get_order(self, order_id: str) -> dict[str, Any]:
    """Get Order."""
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    return response.json()

  def _place_order(self, extra: dict[str, Any] | None) -> tuple[str, str]:
    """Create and complete a checkout; return (checkout_id, order_id)."""
    checkout = self._create(extra)
    completed = self._complete(checkout["id"])
    return checkout["id"], completed["order"]["id"]

  def _events_for(self, order_id: str) -> list[dict[str, Any]]:
    """Return captured deliveries whose body is the given order."""
    return [
      e
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict)
      and e["payload"].get("id") == order_id
    ]

  def _wait_for_events(self, order_id: str, count: int) -> list[dict[str, Any]]:
    """Poll (up to 5s) until ``count`` deliveries for the order arrived."""
    for _ in range(50):
      events = self._events_for(order_id)
      if len(events) >= count:
        return events
      time.sleep(0.1)
    return self._events_for(order_id)

  def _assert_reference(
    self, body: dict[str, Any], expected: str, where: str
  ) -> None:
    """Assert ``body`` carries the reference verbatim."""
    self.assertIn(
      _FIELD,
      body,
      f"{where} does not carry {_FIELD} (ucp#839: the business MUST echo "
      "it on every checkout response and carry it onto the order and every "
      "order event)",
    )
    self.assertEqual(
      body[_FIELD],
      expected,
      f"{where} altered {_FIELD} (ucp#839: echoed verbatim; the business "
      "MUST NOT interpret, parse or modify it)",
    )

  # ── Create and echo ─────────────────────────────────────────────────────
  def test_create_echoes_reference(self) -> None:
    """Create Checkout echoes the reference verbatim."""
    checkout = self._create({_FIELD: _REF})
    self._assert_reference(checkout, _REF, "Create Checkout response")

  def test_get_checkout_returns_reference(self) -> None:
    """Get Checkout returns the reference set on Create."""
    checkout = self._create({_FIELD: _REF})
    fetched = self._get_checkout(checkout["id"])
    self._assert_reference(fetched, _REF, "Get Checkout response")

  def test_max_length_reference_round_trips(self) -> None:
    """A 512-character reference is kept intact through to the order."""
    self._require_order()
    checkout = self._create({_FIELD: _REF_MAX})
    self._assert_reference(checkout, _REF_MAX, "Create Checkout response")
    completed = self._complete(checkout["id"])
    order = self._get_order(completed["order"]["id"])
    self._assert_reference(order, _REF_MAX, "Get Order response")

  # ── Immutability on Update ──────────────────────────────────────────────
  def test_update_without_field_retains_reference(self) -> None:
    """A full-replacement Update that omits the field does not clear it."""
    checkout = self._create({_FIELD: _REF})
    response = self._update(checkout)
    self.assert_response_status(response, 200)
    self._assert_reference(response.json(), _REF, "Update Checkout response")
    self._assert_reference(
      self._get_checkout(checkout["id"]), _REF, "Get Checkout after Update"
    )

  def test_update_cannot_change_reference(self) -> None:
    """A different value sent on Update is ignored; the original is kept."""
    checkout = self._create({_FIELD: _REF})
    response = self._update(checkout, {_FIELD: _REF_ALT})
    # ucp#839: the business MUST ignore the value. Whether a strict business
    # may reject the request instead is pending review, so only the stored
    # value is graded here.
    if response.status_code == 200:
      self._assert_reference(response.json(), _REF, "Update Checkout response")
    self._assert_reference(
      self._get_checkout(checkout["id"]), _REF, "Get Checkout after Update"
    )

  def test_update_resending_same_reference_keeps_it(self) -> None:
    """Resending the same value on Update leaves it unchanged."""
    checkout = self._create({_FIELD: _REF})
    response = self._update(checkout, {_FIELD: _REF})
    if response.status_code == 200:
      self._assert_reference(response.json(), _REF, "Update Checkout response")
    self._assert_reference(
      self._get_checkout(checkout["id"]), _REF, "Get Checkout after Update"
    )

  def test_update_cannot_add_reference(self) -> None:
    """A reference first sent on Update is ignored (it is Create-only)."""
    checkout = self._create()
    self._update(checkout, {_FIELD: _REF})
    self.assertNotIn(
      _FIELD,
      self._get_checkout(checkout["id"]),
      f"{_FIELD} sent only on Update was applied (ucp#839: Create-only; a "
      "value received on Update MUST be ignored)",
    )

  # ── Complete and Cancel ─────────────────────────────────────────────────
  def test_complete_keeps_reference_and_ignores_new_value(self) -> None:
    """Complete echoes the original reference and ignores a new one."""
    checkout = self._create({_FIELD: _REF})
    completed = self._complete(checkout["id"], {_FIELD: _REF_ALT})
    self._assert_reference(completed, _REF, "Complete Checkout response")
    self._assert_reference(
      self._get_checkout(checkout["id"]), _REF, "Get Checkout after Complete"
    )

  def test_cancel_echoes_reference(self) -> None:
    """Cancel Checkout echoes the reference on the canceled checkout."""
    checkout = self._create({_FIELD: _REF})
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout['id']}/cancel"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    self._assert_reference(response.json(), _REF, "Cancel Checkout response")
    self._assert_reference(
      self._get_checkout(checkout["id"]), _REF, "Get Checkout after Cancel"
    )

  # ── Order and order events ──────────────────────────────────────────────
  def test_order_carries_reference(self) -> None:
    """Get Order returns the reference from the originating checkout."""
    self._require_order()
    _, order_id = self._place_order({_FIELD: _REF})
    self._assert_reference(self._get_order(order_id), _REF, "Get Order")

  def test_order_created_event_carries_reference(self) -> None:
    """The 'Order created' webhook body carries the reference."""
    self._require_order()
    _, order_id = self._place_order({_FIELD: _REF})
    events = self._wait_for_events(order_id, 1)
    self.assertTrue(
      events, "no order event webhook delivered for the completed order"
    )
    self._assert_reference(events[0]["payload"], _REF, "Order created event")

  def test_order_update_events_carry_reference(self) -> None:
    """Every order event, including updates, carries the reference."""
    self._require_order()
    _, order_id = self._place_order({_FIELD: _REF})
    headers = self.get_headers()
    headers["Simulation-Secret"] = (
      integration_test_utils.FLAGS.simulation_secret
    )
    ship = self.client.post(
      f"/testing/simulate-shipping/{order_id}", headers=headers
    )
    if ship.status_code == 403:
      self.skipTest(
        "simulation secret rejected (/testing/simulate-shipping -> 403); "
        "configure --simulation_secret to match the server under test"
      )
    if ship.status_code in (404, 405):
      self.skipTest(
        "server exposes no webhook simulation path "
        f"(/testing/simulate-shipping -> {ship.status_code})"
      )
    self.assert_response_status(ship, 200)
    events = self._wait_for_events(order_id, 2)
    self.assertGreaterEqual(
      len(events), 2, "no update webhook delivered after the order shipped"
    )
    for index, event in enumerate(events):
      self._assert_reference(
        event["payload"], _REF, f"order event #{index + 1}"
      )

  def test_distinct_references_stay_separate(self) -> None:
    """Two orders keep their own references on Get Order and events."""
    self._require_order()
    _, order_a = self._place_order({_FIELD: _REF})
    _, order_b = self._place_order({_FIELD: _REF_ALT})
    for order_id, expected in ((order_a, _REF), (order_b, _REF_ALT)):
      self._assert_reference(
        self._get_order(order_id), expected, f"Get Order {order_id}"
      )
      events = self._wait_for_events(order_id, 1)
      self.assertTrue(events, f"no order event delivered for {order_id}")
      self._assert_reference(
        events[0]["payload"], expected, f"order event for {order_id}"
      )

  # ── Absence ─────────────────────────────────────────────────────────────
  def test_absent_reference_is_not_synthesized(self) -> None:
    """Without a reference on Create, none appears on checkout or order."""
    checkout = self._create()
    self.assertNotIn(
      _FIELD, checkout, f"Create Checkout response invented a {_FIELD}"
    )
    completed = self._complete(checkout["id"])
    self.assertNotIn(
      _FIELD, completed, f"Complete Checkout response invented a {_FIELD}"
    )
    if self._advertised_versions(_ORDER_CAPABILITY):
      order = self._get_order(completed["order"]["id"])
      self.assertNotIn(
        _FIELD,
        order,
        f"order carries a {_FIELD} although the checkout had none "
        "(order.json: present when the originating checkout carried one)",
      )


if __name__ == "__main__":
  absltest.main()
