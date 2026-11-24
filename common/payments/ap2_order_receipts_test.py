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

"""AP2 Order receipts issued by the server under test verify.

Order receipts (ap2-mandates.md "Order Receipts", ucp#1109) are optional:
they are required only when both profiles declare `order_receipts` in the
`dev.ucp.common.payment.ap2_mandate` config. These tests skip unless the
business does, and the platform profile they serve declares it too.

The receipts are checked with the same platform-side verifier as the fixed
vectors in tests/ap2_receipt_vectors_test.py, against the `keys` the business
publishes at its discovery profile and the Payment Mandate the test sent.

Like ap2_test.py:test_ap2_mandate_completion, the tests skip until the suite
has a signed SD-JWT+kb checkout mandate fixture. With AP2 negotiated, a
business that verifies mandates rejects a placeholder, so a completion cannot
be placed without one.
"""

import logging
from typing import Any

from absl.testing import absltest
from common.payments import ap2_receipt_verifier as v
from common.payments.ap2_support import (
  ap2_release,
  Ap2AgentProfileServer,
  capability_intersection,
)
from framework.base_test import FLAGS, flag_value
from framework.decorators import requires_capability
from framework.discovery import FORMER_NAMES
import httpx
from shopping.base import get_valid_payment_payload, ShoppingIntegrationTestBase

_AP2_CAPABILITY = "dev.ucp.common.payment.ap2_mandate"

# order_receipts_config.events in payment_ap2_mandate.json.
_RECEIPT_EVENTS = [
  "authorization",
  "authorization_adjustment",
  "capture",
  "capture_reversal",
  "release",
  "refund",
  "refund_reversal",
]

_MANDATE_SKIP = (
  "Needs a signed SD-JWT+kb checkout mandate fixture. With AP2 negotiated, a "
  "Business that verifies the mandate must reject a placeholder mandate."
)


class _ReceiptsAgentProfileServer(Ap2AgentProfileServer):
  """The AP2 agent profile server, declaring `order_receipts` too."""

  AP2_CAPABILITY_CONFIG = {"order_receipts": {"events": _RECEIPT_EVENTS}}


def _order_receipts_config(profile_raw: dict[str, Any]) -> dict | None:
  """Return the business's `order_receipts` config, if it declares one."""
  ucp = profile_raw.get("ucp", profile_raw)
  capabilities = ucp.get("capabilities", {})
  if not isinstance(capabilities, dict):
    return None
  for name in (_AP2_CAPABILITY, *FORMER_NAMES.get(_AP2_CAPABILITY, ())):
    entries = capabilities.get(name) or []
    for entry in entries if isinstance(entries, list) else [entries]:
      config = (entry or {}).get("config") or {}
      if isinstance(config.get("order_receipts"), dict):
        return config["order_receipts"]
  return None


@requires_capability(_AP2_CAPABILITY)
@requires_capability("dev.ucp.shopping.order")
class Ap2OrderReceiptsTest(ShoppingIntegrationTestBase):
  """Receipts a business issues on an AP2 Order verify.

  Every request here uses the mock agent profile that declares the AP2
  Mandates extension with `order_receipts`.

  Validated Paths:
  - POST /checkout-sessions/{id}/complete
  - GET /orders/{id}
  """

  DEFAULT_BUYER = {
    "email": "conformance-test-buyer@example.com",
    "name": "Conformance Test Buyer",
  }

  def _verify_required_capabilities(self) -> None:
    """Also skip unless the business declares `order_receipts`."""
    super()._verify_required_capabilities()
    if self.server_profile is None:
      self.skipTest("discovery profile unavailable")
    if _order_receipts_config(self.server_profile.raw) is None:
      self.skipTest(
        f"server does not declare order_receipts in its {_AP2_CAPABILITY} "
        "config (ap2-mandates.md, Order Receipts: Negotiation)"
      )

  def setUp(self) -> None:
    """Skip unless AP2 is in the negotiated capability intersection."""
    super().setUp()
    ap2 = ap2_release(self.ucp_version)
    if ap2 is None:
      self.skipTest(
        f"No AP2 Mandates definition for UCP release {self.ucp_version}"
      )
    self._keys_field = ap2["keys_field"]
    discovery = self.client.get(self.discovery_url)
    self.assert_response_status(discovery, 200)
    platform = httpx.get(self._ap2_profile_url())
    self.assert_response_status(platform, 200)
    if ap2["capability"] not in capability_intersection(
      discovery.json(), platform.json()
    ):
      self.skipTest(
        f"{ap2['capability']} is not in the capability intersection, so "
        "AP2 is not negotiated"
      )

  def _start_agent_server(self) -> None:
    """Serve the agent profiles, including the one that declares AP2.

    Receipts are required only when both profiles declare `order_receipts`
    (ap2-mandates.md, Order Receipts: Negotiation).
    """
    self.agent_server = _ReceiptsAgentProfileServer(
      port=FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285,
      webhook_port=FLAGS.mock_webhook_port if FLAGS.is_parsed() else 8284,
      ucp_version=self.ucp_version,
      webhook_base_url=flag_value("webhook_base_url"),
    )
    self.agent_server.start()
    self.addCleanup(self.agent_server.stop)

  def _ap2_profile_url(self) -> str:
    port = FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285
    return f"http://localhost:{port}{Ap2AgentProfileServer.AP2_PROFILE_PATH}"

  def get_headers(
    self,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    custom_profile_url: str | None = None,
  ) -> dict[str, str]:
    """Use the agent profile that declares the AP2 Mandates extension."""
    return super().get_headers(
      idempotency_key,
      request_id,
      custom_profile_url=custom_profile_url or self._ap2_profile_url(),
    )

  @property
  def business_profile_uri(self) -> str:
    """The profile URI the business's receipts carry as `iss`."""
    return self.discovery_url

  def _signed_checkout_mandate(self) -> str:
    """Return a signed SD-JWT+kb checkout mandate for the checkout.

    There is no fixture for one yet, so this skips; see ap2_test.py.
    """
    self.skipTest(_MANDATE_SKIP)

  def _place_order(self) -> tuple[dict[str, Any], v.Mandate]:
    """Complete an AP2 checkout; return the Order and the mandate sent."""
    checkout_data = self.create_checkout_session(buyer=self.DEFAULT_BUYER)
    checkout_mandate = self._signed_checkout_mandate()
    payment_payload = get_valid_payment_payload()
    # The signed mandate goes in `ap2.checkout_mandate` of the completion
    # request (ap2_with_checkout_mandate). The Payment Mandate travels in
    # payment.instruments[*].credential.token, which mandate_ref hashes
    # (ap2-mandates.md, Hashes and Chaining).
    payment_payload["ap2"] = {"checkout_mandate": checkout_mandate}
    token = payment_payload["payment"]["instruments"][0]["credential"]["token"]
    complete_data = self.complete_checkout_session(
      checkout_data["id"], payment_payload
    )
    order_id = complete_data["order"]["id"]
    # Fulfillment selection during completion can change the totals, so read
    # the amount from the completed checkout, not the one created.
    amount = next(
      (
        t["amount"]
        for t in complete_data.get("totals", [])
        if t.get("type") == "total"
      ),
      None,
    )
    return self._get_order(order_id), v.Mandate(
      token, self.business_profile_uri, amount
    )

  def _get_order(self, order_id: str) -> dict[str, Any]:
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    self.assert_response_status(response, 200)
    return response.json()

  def _receipts(self, order: dict[str, Any]) -> list[str]:
    ap2 = order.get("ap2")
    self.assertTrue(
      ap2 is None or isinstance(ap2, dict), "ap2 must be an object"
    )
    receipts = (ap2 or {}).get("receipts")
    if receipts is None:
      return []
    self.assertIsInstance(receipts, list, "ap2.receipts must be an array")
    return receipts

  def _assert_receipts_verify(
    self, order: dict[str, Any], mandate: v.Mandate
  ) -> v.VerificationResult:
    """Verify the Order's receipts and the business requirements on them."""
    keys = self.server_profile.raw.get(self._keys_field) or []
    result = v.verify_order_receipts(
      order, {self.business_profile_uri: keys}, [mandate]
    )
    # receipt_payload: the members the schema requires.
    self.assertEqual(
      result.malformed, [], "signed receipts that are not Receipt Payloads"
    )

    # Platform Verification 1-6 are MUSTs.
    self.assertEqual(
      result.outcomes,
      [],
      "receipts failed verification (ap2-mandates.md, Platform "
      "Verification 1-6): "
      + "; ".join(
        f"{f.receipt_index}: {f.code} ({f.detail})" for f in result.outcomes
      ),
    )
    # Platform Verification 7 and 8 are SHOULDs: reported, not failed.
    for finding in result.warnings + result.reconciliation:
      logging.warning(
        "receipt %d: %s (%s)",
        finding.receipt_index,
        finding.code,
        finding.detail,
      )

    previous: dict[v.ChainKey, dict[str, int]] = {}
    for receipt in result.verified:
      payload = receipt.payload
      where = f"receipt {receipt.index}"
      self.assertEqual(payload["order_id"], order["id"], f"{where}: order_id")
      # receipt_event.currency: "MUST match the Order currency."
      self.assertEqual(
        payload["event"]["currency"], order.get("currency"), where
      )
      # receipt_event.status: "Only `succeeded` events change the running
      # totals."
      totals = payload["totals"]
      derived = receipt.derived_totals
      self.assertEqual(
        (totals["authorized"], totals["captured"], totals["refunded"]),
        (derived.authorized, derived.captured, derived.refunded),
        f"{where}: totals do not follow from the chain's succeeded events",
      )
      if not receipt.counted:
        opening = {
          "mandated": totals["mandated"],
          "authorized": 0,
          "captured": 0,
          "refunded": 0,
        }
        self.assertEqual(
          totals,
          previous.get(receipt.chain, opening),
          f"{where}: a receipt that is not counted changed totals",
        )
      previous[receipt.chain] = totals
    return result

  def test_issued_receipts_verify(self) -> None:
    """Test that the receipts on a new AP2 Order verify.

    Given a business that declares order_receipts,
    When an AP2 checkout is completed and the Order is read,
    Then every receipt in ap2.receipts verifies against the business's keys
    and the Payment Mandate sent, with no sequence gaps and both total
    invariants holding.
    """
    order, mandate = self._place_order()
    if not self._receipts(order):
      self.skipTest("the business issued no receipts for the new Order")
    self._assert_receipts_verify(order, mandate)

  def test_receipts_extend_after_shipment(self) -> None:
    """Test that a later Order update keeps earlier receipts.

    Given an AP2 Order with receipts,
    When the order ships and is read again,
    Then ap2.receipts still holds every earlier receipt, each chain in seq
    order (ap2-mandates.md, Placement and Delivery), and all of them
    verify.
    """
    order, mandate = self._place_order()
    before = self._receipts(order)
    headers = self.get_headers()
    headers["Simulation-Secret"] = FLAGS.simulation_secret
    ship_response = self.client.post(
      f"/testing/simulate-shipping/{order['id']}", headers=headers
    )
    if ship_response.status_code == 403:
      self.skipTest(
        "simulation secret rejected (/testing/simulate-shipping -> 403); "
        "configure --simulation_secret to match the server under test"
      )
    if ship_response.status_code in (404, 405):
      self.skipTest(
        "server exposes no simulation path "
        f"(/testing/simulate-shipping -> {ship_response.status_code}); "
        "cannot trigger an order update"
      )
    self.assert_response_status(ship_response, 200)

    shipped = self._get_order(order["id"])
    after = self._receipts(shipped)
    if not after:
      self.skipTest("the business issued no receipts for the Order")
    for receipt in before:
      self.assertIn(receipt, after, "an earlier receipt was dropped")
    self.assertEqual(
      [r for r in after if r in before],
      before,
      "earlier receipts changed order",
    )
    self._assert_receipts_verify(shipped, mandate)


if __name__ == "__main__":
  absltest.main()
