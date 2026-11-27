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

"""Regression tests for payment requests using merchant-declared fixtures."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.payments import card_credential_test, token_binding_test
import httpx
from shopping import base


class PaymentFixtureTest(unittest.TestCase):
  """Exercise the actual payment tests without a networked merchant."""

  def setUp(self):
    """Load the bundled fixture; each regression changes only one value."""
    self.data = base.ShoppingTestData()
    self.data.load(base.DEFAULT_SHOPPING_FIXTURES)
    self.patch = patch.object(base, "shopping_test_data", self.data)
    self.patch.start()
    self.addCleanup(self.patch.stop)

  def exercise(self, test_class, method, expected_handler, expected_token=None):
    """Invoke a real payment test against an independently checked request."""
    case = test_class(method)
    requests = []

    def handler(request):
      payload = json.loads(request.content)
      instrument = payload["payment"]["instruments"][0]
      self.assertEqual(instrument["handler_id"], expected_handler)
      if expected_token is not None:
        self.assertEqual(instrument["credential"]["token"], expected_token)
      requests.append(instrument)
      return httpx.Response(200, json={"status": "completed"})

    with (
      httpx.Client(transport=httpx.MockTransport(handler)) as client,
      patch.object(case, "client", client, create=True),
      patch.object(case, "create_checkout_session", return_value={"id": "chk"}),
      patch.object(
        case, "get_shopping_url", return_value="https://store.test/complete"
      ),
      patch.object(case, "get_headers", return_value={}),
      patch("common.payments.card_credential_test.checkout.Checkout") as model,
    ):
      model.return_value.id = "chk"
      getattr(case, method)()
    self.assertEqual(len(requests), 1)
    return requests[0]

  def test_card_uses_configured_handler(self):
    """Changing only the handler never leaves a reference-store literal."""
    self.data.payment_instruments[0]["handler_id"] = "merchant_card_handler"
    instrument = self.exercise(
      card_credential_test.CardCredentialTest,
      "test_card_credential_payment",
      "merchant_card_handler",
    )
    self.assertEqual(instrument["credential"]["type"], "card")
    self.assertEqual(instrument["credential"]["number"], "4242424242424242")

  def test_token_uses_configured_handler(self):
    """The token-binding test reads the same handler as other payments."""
    self.data.payment_instruments[0]["handler_id"] = "merchant_token_handler"
    self.exercise(
      token_binding_test.TokenBindingTest,
      "test_token_binding_completion",
      "merchant_token_handler",
    )

  def test_token_uses_configured_credential(self):
    """Changing only the token preserves it when adding binding data."""
    self.data.payment_instruments[0]["token"] = "merchant_success_token"
    instrument = self.exercise(
      token_binding_test.TokenBindingTest,
      "test_token_binding_completion",
      "mock_payment_handler",
      "merchant_success_token",
    )
    self.assertEqual(instrument["credential"]["binding"]["checkout_id"], "chk")

  def test_default_fixture_still_works(self):
    """Both payment tests retain the bundled flower-shop configuration."""
    self.exercise(
      card_credential_test.CardCredentialTest,
      "test_card_credential_payment",
      "mock_payment_handler",
    )
    self.exercise(
      token_binding_test.TokenBindingTest,
      "test_token_binding_completion",
      "mock_payment_handler",
      "success_token",
    )

  def test_unknown_instrument_is_a_configuration_error(self):
    """An unknown ID cannot silently use the reference-store credential."""
    with self.assertRaisesRegex(ValueError, "missing_instrument"):
      base.get_valid_payment_payload("missing_instrument")

  def test_loading_without_payment_file_clears_previous_credentials(self):
    """A second merchant cannot inherit payment data from the first."""
    with tempfile.TemporaryDirectory() as directory:
      self.data.load(Path(directory))
    self.assertEqual(self.data.payment_instruments, [])
    with self.assertRaisesRegex(ValueError, "instr_1"):
      base.get_valid_payment_payload()


if __name__ == "__main__":
  unittest.main()
