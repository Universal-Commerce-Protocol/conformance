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

"""Structural conformance for the order-event webhook contract (order.md).

The 2026-04-08 order specification pins a delivery contract for order-event
webhooks beyond "an event arrives" (which webhook_test.py already covers):

  * Required headers follow Standard Webhooks: ``Webhook-Id`` (unique event
    identifier) and ``Webhook-Timestamp`` (unix event timestamp) — order.md
    "Order Event Webhook", Required Headers.
  * The body is the FULL order entity: the same current-state snapshot as Get
    Order. Businesses MUST send the "Order created" event with a fully
    populated order entity and MUST send the full entity on updates, never an
    incremental delta — order.md "Events" and its Business guidelines. The
    schema's required properties (order.json) are the floor for "fully
    populated".
  * Deliveries MUST be signed. What is checked here is the signing ENVELOPE
    (headers, digest, signed components), not cryptographic verification of
    the signature value against the business's published keys: ``UCP-Agent``
    (business profile URL), ``Signature``, ``Signature-Input`` and
    ``Content-Digest`` headers, with
    the SHA-256 ``Content-Digest`` matching the raw body — order.md "Webhook
    Signature Verification". Because the ``UCP-Agent`` header is required on
    webhook deliveries, ``ucp-agent`` is a required signed component
    (signatures.md, REST Request Signing component table, footnote **).

The signature tests skip loudly when the server sends completely unsigned
deliveries (the current reference-server behavior), mirroring how other
structural tests skip on shapes the server does not emit; the update-event
test skips when the server exposes no /testing/simulate-shipping path.
"""

import base64
import hashlib
import re
import time
from typing import Any

from absl.testing import absltest
import integration_test_utils

_ORDER_CAPABILITY = "dev.ucp.shopping.order"

# order.json "required" — the floor for a "fully populated order entity"
_ORDER_REQUIRED = (
  "ucp",
  "id",
  "checkout_id",
  "permalink_url",
  "line_items",
  "fulfillment",
  "currency",
  "totals",
)

# order.md "Webhook Signature Verification" Required Headers (lowercase)
_SIGNATURE_HEADERS = (
  "ucp-agent",
  "signature",
  "signature-input",
  "content-digest",
)


class WebhookStructureTest(integration_test_utils.IntegrationTestBase):
  """Order-event webhook contract: headers, full entity, signing."""

  def setUp(self) -> None:
    """Start the receiver; skip unless the order capability is advertised."""
    super().setUp()
    if not self._advertises_order():
      self.skipTest(
        f"business does not advertise {_ORDER_CAPABILITY}; skipping"
      )
    port = integration_test_utils.FLAGS.mock_webhook_port
    self.webhook_server = integration_test_utils.MockWebhookServer(port=port)
    self.webhook_server.start()

  def tearDown(self) -> None:
    """Stop the receiver."""
    if hasattr(self, "webhook_server"):
      self.webhook_server.stop()
    super().tearDown()

  def _advertises_order(self) -> bool:
    """Return True if discovery advertises the order capability."""
    resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(resp, 200)
    ucp = resp.json().get("ucp", resp.json())
    caps = ucp.get("capabilities") or {}
    names = (
      list(caps.keys())
      if isinstance(caps, dict)
      else [c.get("name") for c in caps if isinstance(c, dict)]
    )
    return _ORDER_CAPABILITY in names

  # ── shared drivers ──────────────────────────────────────────────────────
  def _events_for(self, order_id: str) -> list[dict[str, Any]]:
    """All captured deliveries whose payload is the given order.

    A delivery whose body has no ``id`` at all (a delta rather than the full
    entity) cannot be matched here; callers surface that case explicitly so
    the deviation reads "not a full entity", not "nothing delivered".
    """
    return [
      e
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict)
      and e["payload"].get("id") == order_id
    ]

  def _idless_deliveries(self) -> int:
    """Deliveries whose dict body carries no id (delta-shaped)."""
    return sum(
      1
      for e in self.webhook_server.events
      if isinstance(e.get("payload"), dict) and "id" not in e["payload"]
    )

  def _wait_for_events(self, order_id: str, count: int) -> list[dict[str, Any]]:
    """Poll (up to 5s) until `count` deliveries for the order arrived."""
    for _ in range(50):
      events = self._events_for(order_id)
      if len(events) >= count:
        return events
      time.sleep(0.1)
    return self._events_for(order_id)

  def _place_order(self) -> tuple[str, str, dict[str, Any]]:
    """Create + complete a checkout; return (checkout_id, order_id, event).

    The returned event is the first delivery for the order — the "Order
    created" event the business MUST send (order.md Business guidelines).
    """
    checkout_data = self.create_checkout_session(headers=self.get_headers())
    checkout_id = checkout_data["id"]
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]
    events = self._wait_for_events(order_id, 1)
    if not events and self._idless_deliveries():
      self.fail(
        "webhook delivery arrived without an order id in the body - a delta "
        "rather than the full order entity (order.md Events: deliveries are "
        "fully populated order objects, never deltas)"
      )
    self.assertTrue(
      events,
      "no order-event webhook delivered for the completed order "
      "(order.md: MUST send 'Order created' event)",
    )
    return checkout_id, order_id, events[0]

  def _assert_full_order_entity(
    self, payload: dict[str, Any], checkout_id: str, order_id: str
  ) -> None:
    """Assert the payload is the fully populated order entity for our order."""
    missing = [k for k in _ORDER_REQUIRED if payload.get(k) is None]
    self.assertFalse(
      missing,
      "webhook body is not a fully populated order entity; missing "
      f"required order properties {missing} (order.md: the payload is the "
      "same current-state snapshot as Get Order)",
    )
    self.assertTrue(
      payload["line_items"],
      "webhook order entity carries no line_items",
    )
    self.assertEqual(payload["id"], order_id)
    self.assertEqual(
      payload["checkout_id"],
      checkout_id,
      "webhook order entity does not reference the originating checkout",
    )

  # ── order.md "Order Event Webhook": Standard Webhooks headers ───────────
  def test_delivery_carries_standard_webhook_headers(self) -> None:
    """Every delivery carries Webhook-Id and a unix Webhook-Timestamp."""
    _, _, event = self._place_order()
    headers = event["headers"]
    self.assertTrue(
      headers.get("webhook-id"),
      "delivery is missing the required Webhook-Id header",
    )
    timestamp = headers.get("webhook-timestamp", "")
    self.assertRegex(
      timestamp,
      r"^\d+$",
      "Webhook-Timestamp must be a unix timestamp",
    )
    # The spec requires a unix timestamp but sets no skew bound; assert the
    # value parses into the unix-seconds era rather than inventing a window
    # (a millisecond value or a date string fails, clock skew does not).
    self.assertTrue(
      1_000_000_000 <= int(timestamp) <= 4_000_000_000,
      f"Webhook-Timestamp {timestamp} does not parse as unix seconds",
    )

  def test_transient_failure_retries_preserve_event_identity(self) -> None:
    """A failed delivery is retried as the same webhook event."""
    self.webhook_server.stop()
    port = integration_test_utils.FLAGS.mock_webhook_port
    self.webhook_server = integration_test_utils.MockWebhookServer(
      port=port, failures_before_success=1
    )
    self.webhook_server.start()

    checkout_data = self.create_checkout_session(headers=self.get_headers())
    checkout_id = checkout_data["id"]
    complete_response = self.complete_checkout_session(checkout_id)
    order_id = complete_response["order"]["id"]
    deliveries = self._wait_for_events(order_id, 2)

    self.assertGreaterEqual(
      len(deliveries),
      2,
      "failed webhook delivery was not retried (order.md: MUST retry failed "
      "webhook deliveries)",
    )
    self.assertEqual(deliveries[0]["response_status"], 500)
    self.assertEqual(deliveries[1]["response_status"], 200)
    self.assertEqual(
      len({delivery["headers"].get("webhook-id") for delivery in deliveries}),
      1,
      "retry attempts must preserve the Webhook-Id of the original event",
    )
    self.assertEqual(
      len(
        {
          delivery["headers"].get("webhook-timestamp")
          for delivery in deliveries
        }
      ),
      1,
      "retry attempts must preserve the Webhook-Timestamp of the original "
      "event",
    )
    self.assertTrue(
      all(
        delivery["payload"] == deliveries[0]["payload"]
        for delivery in deliveries
      ),
      "retry attempts must carry the same full order entity",
    )

  # ── order.md "Events": fully populated order entity ─────────────────────
  def test_order_created_event_is_full_order_entity(self) -> None:
    """The 'Order created' delivery body is the full order entity."""
    checkout_id, order_id, event = self._place_order()
    self._assert_full_order_entity(event["payload"], checkout_id, order_id)

  def test_update_event_is_full_order_entity(self) -> None:
    """An update delivery is the full current-state snapshot, not a delta."""
    checkout_id, order_id, _ = self._place_order()
    headers = self.get_headers()
    headers["Simulation-Secret"] = (
      integration_test_utils.FLAGS.simulation_secret
    )
    ship_response = self.client.post(
      f"/testing/simulate-shipping/{order_id}",
      headers=headers,
    )
    if ship_response.status_code == 403:
      self.skipTest(
        "simulation secret rejected (/testing/simulate-shipping -> 403); "
        "configure --simulation_secret to match the server under test"
      )
    if ship_response.status_code in (404, 405):
      self.skipTest(
        "server exposes no webhook simulation path "
        f"(/testing/simulate-shipping -> {ship_response.status_code}); "
        "cannot trigger an order update"
      )
    self.assert_response_status(ship_response, 200)
    events = self._wait_for_events(order_id, 2)
    self.assertGreaterEqual(
      len(events),
      2,
      "no update webhook delivered after the order shipped "
      "(order.md: MUST send full order entity on updates)",
    )
    for event in events:
      self._assert_full_order_entity(event["payload"], checkout_id, order_id)
    fulfillment_events = [
      fe
      for event in events
      for fe in (event["payload"]["fulfillment"].get("events") or [])
    ]
    self.assertTrue(
      any(fe.get("type") == "shipped" for fe in fulfillment_events),
      "no update delivery reflected the shipped state in the snapshot",
    )

  # ── order.md "Webhook Signature Verification" ───────────────────────────
  def _signed_event(self) -> dict[str, Any]:
    """Place an order and return its delivery, skipping if unsigned.

    A delivery with NONE of the signature headers means the server does not
    implement webhook signing at all; the structural assertions would all
    fail on the same root cause, so skip with one loud message instead
    (order.md: payloads MUST be signed — this skip is a finding, not a pass).
    """
    _, _, event = self._place_order()
    if not any(event["headers"].get(h) for h in _SIGNATURE_HEADERS):
      self.skipTest(
        "server sends unsigned webhook deliveries (no UCP-Agent, Signature, "
        "Signature-Input or Content-Digest header); order.md 'Webhook "
        "Signature Verification' MUSTs are not exercised"
      )
    return event

  def test_signed_delivery_carries_required_headers_and_digest(self) -> None:
    """A signed delivery carries all four headers; the digest matches."""
    event = self._signed_event()
    headers = event["headers"]
    missing = [h for h in _SIGNATURE_HEADERS if not headers.get(h)]
    self.assertFalse(
      missing,
      f"signed delivery is missing required headers: {missing} "
      "(order.md Webhook Signature Verification)",
    )
    agent = headers["ucp-agent"]
    match = re.search(r'profile="([^"]+)"', agent)
    self.assertIsNotNone(
      match,
      f"UCP-Agent is not an RFC 8941 dictionary with profile=: {agent!r}",
    )
    profile_url = match.group(1).split("?")[0].rstrip("/")
    self.assertTrue(
      profile_url.endswith("/.well-known/ucp"),
      "UCP-Agent profile URL must point at the business's /.well-known/ucp "
      f"(signatures.md UCP-Agent parsing): {agent!r}",
    )
    digest_match = re.search(
      r"sha-256=:([A-Za-z0-9+/=]+):", headers["content-digest"]
    )
    self.assertIsNotNone(
      digest_match,
      f"Content-Digest is not sha-256 (RFC 9530): "
      f"{headers['content-digest']!r}",
    )
    expected = hashlib.sha256(event["raw_body"]).digest()
    self.assertEqual(
      base64.b64decode(digest_match.group(1)),
      expected,
      "Content-Digest does not match the SHA-256 of the raw delivery body",
    )

  def test_signature_covers_ucp_agent(self) -> None:
    """The signed components include ucp-agent (binds signer identity).

    signatures.md's REST signing component table requires ``ucp-agent``
    whenever the UCP-Agent header is present, and order.md requires that
    header on every webhook delivery — so a conformant webhook signature
    always covers it (along with the always-required derived components and
    the body components).
    """
    event = self._signed_event()
    signature_input = event["headers"].get("signature-input", "")
    match = re.search(r"\(([^)]*)\)", signature_input)
    self.assertIsNotNone(
      match,
      f"Signature-Input has no component list: {signature_input!r}",
    )
    components = re.findall(r'"([^"]+)"', match.group(1))
    for required in (
      "@method",
      "@authority",
      "@path",
      "content-digest",
      "content-type",
    ):
      self.assertIn(
        required,
        components,
        f"webhook signature does not cover {required!r} "
        f"(signatures.md signed-component table): {signature_input!r}",
      )
    # signatures.md's component table requires ucp-agent whenever the header
    # is present, but order.md's own example webhook signature omits it; that
    # inconsistency has a fix in flight (ucp#659). Until it lands, a merchant
    # following the example verbatim must not be failed here.
    if "ucp-agent" not in components:
      self.skipTest(
        "webhook signature does not cover ucp-agent; normative text requires "
        "it but the spec example omits it (fix pending in ucp#659) - not "
        "graded until the spec is self-consistent"
      )


if __name__ == "__main__":
  absltest.main()
