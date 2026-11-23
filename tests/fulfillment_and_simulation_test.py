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

"""Unit tests for fulfillment totals and simulate-shipping skips."""

import unittest

from absl.testing import flagsaver
import httpx
from shopping import base
from shopping.order import simulation_url_security_test
from ucp_sdk.models.schemas.shopping import checkout as checkout_model


def _make_checkout_with_totals(totals: list[dict]) -> checkout_model.Checkout:
  """Construct a Checkout model carrying `totals`."""
  return checkout_model.Checkout(
    ucp={
      "version": "2026-04-08",
      "capabilities": {},
      "payment_handlers": {},
    },
    id="chk_1",
    status="ready_for_complete",
    currency="USD",
    line_items=[
      {
        "id": "li_1",
        "quantity": 1,
        "item": {"id": "bouquet_roses", "title": "Roses", "price": 3500},
        "totals": [
          {"type": "subtotal", "amount": 3500},
          {"type": "total", "amount": 3500},
        ],
      }
    ],
    totals=totals,
    links=[],
  )


def _make_security_probe(handler) -> simulation_url_security_test.SecurityTest:
  """Return a SecurityTest wired to `handler` without starting servers."""

  class Probe(simulation_url_security_test.SecurityTest):
    """A SecurityTest bound to a mock transport."""

    def runTest(self):  # noqa: N802
      """Do nothing; individual test methods are invoked directly."""

    def create_completed_order(self) -> str:
      return "ord_1"

  probe = Probe()
  probe.client = httpx.Client(
    base_url="https://store.example",
    transport=httpx.MockTransport(handler),
  )
  return probe


class FulfillmentTotalsFormulaTest(unittest.TestCase):
  """Fulfillment totals obey the spec's verification formula with tax."""

  def _make_base_probe(self) -> base.ShoppingIntegrationTestBase:
    class Probe(base.ShoppingIntegrationTestBase):
      def runTest(self):  # noqa: N802
        """Do nothing."""

    return Probe()

  def test_tax_and_fee_entries_pass_consistency_check(self):
    """A checkout that adds tax on top of subtotal + shipping passes."""
    probe = self._make_base_probe()
    chk = _make_checkout_with_totals(
      [
        {"type": "subtotal", "amount": 3500},
        {"type": "fulfillment", "amount": 599},
        {"type": "tax", "amount": 410},
        {"type": "total", "amount": 4509},
      ]
    )
    fulfillment_total = sum(
      t.amount for t in chk.totals if t.type == "fulfillment"
    )
    self.assertEqual(fulfillment_total, 599)
    probe.assert_totals_consistent(chk, 3500)

  def test_mismatched_grand_total_fails_consistency_check(self):
    """A grand total that disagrees with the non-total entries fails."""
    probe = self._make_base_probe()
    chk = _make_checkout_with_totals(
      [
        {"type": "subtotal", "amount": 3500},
        {"type": "fulfillment", "amount": 599},
        {"type": "tax", "amount": 410},
        {"type": "total", "amount": 4099},
      ]
    )
    with self.assertRaises(AssertionError):
      probe.assert_totals_consistent(chk, 3500)


class SimulationSecuritySkipTest(unittest.TestCase):
  """SecurityTest skips when the server has no simulation hook."""

  def test_absent_hook_skips_all_three_tests(self):
    """A 404 or 405 from the hook skips every SecurityTest method."""
    for status in (404, 405):
      probe = _make_security_probe(lambda _req, s=status: httpx.Response(s))
      with flagsaver.flagsaver(simulation_secret="right-secret"):
        for method_name in (
          "test_simulation_endpoint_missing_header",
          "test_simulation_endpoint_incorrect_secret",
          "test_simulation_endpoint_correct_secret",
        ):
          with self.assertRaises(unittest.SkipTest) as caught:
            getattr(probe, method_name)()
          self.assertIn("no webhook simulation path", str(caught.exception))

  def test_rejected_configured_secret_skips_with_guidance(self):
    """A 403 to the configured secret skips and names --simulation_secret."""
    probe = _make_security_probe(lambda _req: httpx.Response(403))
    with (
      flagsaver.flagsaver(simulation_secret="wrong-secret"),
      self.assertRaises(unittest.SkipTest) as caught,
    ):
      probe.test_simulation_endpoint_correct_secret()
    self.assertIn("--simulation_secret", str(caught.exception))

  def test_stealth_404_on_bad_secret_passes_when_hook_exists(self):
    """A hook that answers 404 without the secret and 200 with it passes."""

    def handler(request: httpx.Request) -> httpx.Response:
      if request.headers.get("Simulation-Secret") == "right-secret":
        return httpx.Response(200, json={"ok": True})
      return httpx.Response(404)

    probe = _make_security_probe(handler)
    with flagsaver.flagsaver(simulation_secret="right-secret"):
      probe.test_simulation_endpoint_missing_header()
      probe.test_simulation_endpoint_incorrect_secret()
      probe.test_simulation_endpoint_correct_secret()

  def test_unprotected_hook_still_fails(self):
    """A hook that answers 200 without a secret fails the security check."""
    probe = _make_security_probe(lambda _req: httpx.Response(200))
    with (
      flagsaver.flagsaver(simulation_secret="right-secret"),
      self.assertRaises(AssertionError),
    ):
      probe.test_simulation_endpoint_missing_header()


if __name__ == "__main__":
  unittest.main()
