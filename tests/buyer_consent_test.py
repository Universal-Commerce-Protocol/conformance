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

"""Tests for the 2026-08-25 buyer consent check, against a mock business.

CI runs the conformance suite against a 2026-04-08 sample server, so these
tests keep the advertise and confirm path of `test_buyer_consent` covered.
"""

import json
import unittest
from unittest import mock

import httpx
from shopping.base import DynamicFixtureContext

# Import the module, not the class, so pytest does not collect the
# conformance test as a test of this file.
from shopping.checkout import business_logic_test

BASE_URL = "http://business.test"
MARKETING = "dev.ucp.consent.marketing"
ANALYTICS = "dev.ucp.consent.analytics"
EMAIL = "dev.ucp.consent.marketing.email"


def advertised_consent() -> dict:
  """Return the consent options a business advertises on create."""
  return {
    MARKETING: {
      "granted": False,
      "source": "business",
      "description": "Promotional communications",
      "links": [{"type": "privacy_policy", "url": "https://b.test/p"}],
      "segments": {
        EMAIL: {
          "granted": False,
          "source": "business",
          "description": "Promotional emails",
        }
      },
    },
    ANALYTICS: {
      "granted": True,
      "source": "business",
      "description": "Site analytics",
    },
  }


class MockBusiness:
  """A business that advertises consent and applies platform confirms.

  Each attribute changes one behavior, so a test can make the business
  break the extension in one specific way.
  """

  def __init__(self) -> None:
    """Start with the advertised consent and conforming behavior."""
    self.consent = advertised_consent()
    self.instruments: list | None = []
    self.record_platform_choice = True
    self.recorded_granted_override: bool | None = None
    self.drop_purpose_on_update: str | None = None
    self.updates: list[dict] = []

  def checkout(self) -> dict:
    """Return the checkout as the business serves it."""
    return {
      "id": "chk_1",
      "currency": "USD",
      "status": "incomplete",
      "line_items": [
        {"id": "li_1", "item": {"id": "sku_1", "price": 3500}, "quantity": 1}
      ],
      "buyer": {"email": "consent@example.com", "consent": self.consent},
      "payment": {"instruments": self.instruments},
    }

  def handle(self, request: httpx.Request) -> httpx.Response:
    """Serve create, update, and read of one checkout session."""
    path = request.url.path
    if request.method == "POST" and path == "/ucp/checkout-sessions":
      return httpx.Response(201, json=self.checkout())
    if path == "/ucp/checkout-sessions/chk_1":
      if request.method == "PUT":
        body = json.loads(request.content)
        self.updates.append(body)
        self.apply(body["buyer"]["consent"])
        return httpx.Response(200, json=self.checkout())
      if request.method == "GET":
        return httpx.Response(200, json=self.checkout())
    return httpx.Response(404, json={"error": f"{request.method} {path}"})

  def apply(self, confirm: dict) -> None:
    """Record platform-sourced choices for purposes it advertised."""
    for key, value in confirm.items():
      if key not in self.consent or value["source"] != "platform":
        continue
      if self.record_platform_choice:
        self.consent[key]["granted"] = value["granted"]
        self.consent[key]["source"] = "platform"
        if self.recorded_granted_override is not None:
          self.consent[key]["granted"] = self.recorded_granted_override
    if self.drop_purpose_on_update:
      self.consent.pop(self.drop_purpose_on_update)


class BuyerConsentTest(unittest.TestCase):
  """Tests for `test_buyer_consent` on 2026-08-25 and later."""

  def run_check(
    self, business: MockBusiness, version: str = "2026-08-25"
  ) -> None:
    """Run `test_buyer_consent` against the mock business."""
    case = business_logic_test.BusinessLogicTest("test_buyer_consent")
    case.conformance_config = {"ucp_version": version, "test_sku": "sku_1"}
    case.fixture_ctx = DynamicFixtureContext({})
    case._shopping_service_endpoint = f"{BASE_URL}/ucp"
    case.client = httpx.Client(
      base_url=BASE_URL, transport=httpx.MockTransport(business.handle)
    )
    self.addCleanup(case.client.close)
    case.test_buyer_consent()

  def test_passes_for_a_conforming_business(self):
    """A business that records the platform choice passes."""
    business = MockBusiness()
    self.run_check(business)
    self.assertEqual(business.consent[ANALYTICS]["source"], "platform")
    self.assertFalse(business.consent[ANALYTICS]["granted"])

  def test_passes_when_the_business_keeps_its_default(self):
    """Recording a platform confirm is not required (ucp#1113)."""
    business = MockBusiness()
    business.record_platform_choice = False
    self.run_check(business)

  def test_update_sends_only_request_fields(self):
    """The update omits response-only fields of checkout and consent."""
    business = MockBusiness()
    self.run_check(business)
    (update,) = business.updates
    # checkout.json marks these `ucp_request: "omit"`.
    self.assertNotIn("id", update)
    self.assertNotIn("currency", update)
    confirm = update["buyer"]["consent"]
    self.assertEqual(set(confirm), {MARKETING, ANALYTICS})
    # buyer_consent.json marks description and links `ucp_request: "omit"`.
    self.assertEqual(set(confirm[MARKETING]), {"granted", "source", "segments"})
    self.assertEqual(
      set(confirm[MARKETING]["segments"][EMAIL]), {"granted", "source"}
    )
    self.assertEqual(
      confirm[ANALYTICS], {"granted": False, "source": "platform"}
    )

  def test_update_sends_an_empty_list_for_null_instruments(self):
    """Null payment instruments are sent back as an empty list."""
    business = MockBusiness()
    business.instruments = None
    self.run_check(business)
    self.assertEqual(business.updates[0]["payment"], {"instruments": []})

  def test_skips_when_no_consent_is_advertised(self):
    """A checkout with no consent options skips the check."""
    business = MockBusiness()
    business.consent = {}
    with self.assertRaises(unittest.SkipTest):
      self.run_check(business)

  def test_fails_for_segments_that_are_not_an_object(self):
    """Segments that are present but not an object fail."""
    for segments in ([], None):
      with self.subTest(segments=segments):
        business = MockBusiness()
        business.consent[MARKETING]["segments"] = segments
        with self.assertRaisesRegex(AssertionError, "segments"):
          self.run_check(business)

  def test_fails_for_a_purpose_without_a_description(self):
    """An advertised purpose without a description fails."""
    business = MockBusiness()
    del business.consent[ANALYTICS]["description"]
    with self.assertRaisesRegex(AssertionError, "description"):
      self.run_check(business)

  def test_fails_when_purposes_change_after_the_confirm(self):
    """A purpose dropped after an echoing confirm fails."""
    business = MockBusiness()
    business.drop_purpose_on_update = MARKETING
    with self.assertRaisesRegex(AssertionError, "advertised purposes changed"):
      self.run_check(business)

  def test_fails_for_a_platform_source_with_another_value(self):
    """A platform source with a value the platform did not send fails."""
    business = MockBusiness()
    business.recorded_granted_override = True
    with self.assertRaisesRegex(AssertionError, "source 'platform'"):
      self.run_check(business)

  def test_version_selects_the_consent_shape(self):
    """2026-08-25 and later use purposes; 2026-04-08 keeps booleans."""
    cls = business_logic_test.BusinessLogicTest
    for version, purpose_map in (
      ("2026-04-08", False),
      ("2026-08-25", True),
      ("2026-11-01", True),
    ):
      with (
        self.subTest(version=version),
        mock.patch.object(cls, "_check_consent_advertise_and_confirm") as new,
        mock.patch.object(
          cls, "create_checkout_payload", side_effect=RuntimeError("old")
        ),
      ):
        case = cls("test_buyer_consent")
        case.conformance_config = {"ucp_version": version}
        if purpose_map:
          case.test_buyer_consent()
          new.assert_called_once()
        else:
          with self.assertRaisesRegex(RuntimeError, "old"):
            case.test_buyer_consent()
          new.assert_not_called()


if __name__ == "__main__":
  unittest.main()
