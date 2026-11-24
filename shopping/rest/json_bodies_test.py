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

"""Conformance tests for the REST rule that response bodies are valid JSON.

Each REST binding states it, and the catalog carries all three as MUST and
testable: cart-rest.md for REQ-SHOPPING-CART-0062, checkout-rest.md for
REQ-SHOPPING-CHECKOUT-0241 and order-rest.md for REQ-SHOPPING-ORDER-0034. None
is cited by any test.

The path these tests take is a GET for a resource that does not exist, and the
encoding of a refusal is unchecked everywhere. A success body is parsed by the
test that receives it, so a success body that is not JSON already breaks that
test. No refusal has that protection: every one of the seven assertions in the
suite that expects a 4xx checks the status alone and never reads the body, and
404 is treated as a signal to skip rather than a body to inspect. So a business
can answer an unknown resource with HTML and pass every check. Measured against
the flower shop reference with only the 404 branch of its error handler
returning `<html>Not found</html>`, and every other error left as JSON: the
whole suite runs 95 tests, 0 failures, and exits 0.

What is asserted is only what the rule says. The body must parse as JSON. The
status code is not asserted, because the rule governs the encoding and not the
outcome, and a business may answer an unknown resource with 404, with 403 if
existence is itself privileged, or with a 200 envelope carrying messages.

An empty body is treated as no body rather than an invalid one. RFC 8259 needs
a value, so a strict reading would fail a bodiless refusal, but the rule
constrains the bodies a business sends rather than requiring it to send one.

A redirect is judged on its body like anything else, and the client does not
follow it. A 307 carrying nothing passes, and a 302 carrying the conventional
HTML stub fails, which is what the rule says about any response body.

The two `partly` citations use the reasoning the catalog itself records. Cart
and checkout
state the rule over requests and responses both, and the catalog notes for each
read "binds both sides; the response half is visible". Only the response half
is a business obligation that a suite checking a business can observe, so the
request half is out of reach here. The order binding states the rule over
response bodies alone, so that citation is whole.
"""

import json
from typing import Any

from absl.testing import absltest
from framework.decorators import partly
from framework.decorators import requires_capability
from framework.decorators import spec_assert
from shopping.base import ShoppingIntegrationTestBase

_CART_CAPABILITY = "dev.ucp.shopping.cart"
_CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"
_ORDER_CAPABILITY = "dev.ucp.shopping.order"

# An id no business can have issued.
_MISSING_ID = "ucp-conformance-no-such-resource"

_BOTH_SIDES = (
  "the response half only; the rule binds request bodies too, but those are "
  "sent by the platform rather than the business, so a suite that checks a "
  "business cannot observe that half"
)


class RestJsonBodiesTest(ShoppingIntegrationTestBase):
  """A refusal carries a JSON body, on every REST surface."""

  def _assert_body_is_json(self, path: str, surface: str) -> None:
    """GET a resource that does not exist and parse whatever comes back."""
    response = self.client.get(
      self.get_shopping_url(path), headers=self.get_headers()
    )
    raw = response.content
    if not raw.strip():
      # No body at all is outside the rule, which constrains the bodies a
      # business sends rather than obliging it to send one.
      return
    try:
      parsed: Any = json.loads(raw)
    except ValueError as exc:
      self.fail(
        f"the {surface} refusal body is not valid JSON ({exc}); status "
        f"{response.status_code}, content-type "
        f"{response.headers.get('content-type')!r}, first 200 bytes "
        f"{raw[:200]!r}"
      )
    self.assertIsNotNone(
      parsed,
      f"the {surface} refusal body parsed to JSON null, which carries no "
      "error information",
    )

  @requires_capability(_CART_CAPABILITY)
  @spec_assert(
    partly("REQ-SHOPPING-CART-0062", _BOTH_SIDES),
  )
  def test_cart_refusal_body_is_json(self) -> None:
    """A GET for a cart that does not exist answers with JSON."""
    self._assert_body_is_json(f"/carts/{_MISSING_ID}", "cart")

  @requires_capability(_CHECKOUT_CAPABILITY)
  @spec_assert(
    partly("REQ-SHOPPING-CHECKOUT-0241", _BOTH_SIDES),
  )
  def test_checkout_refusal_body_is_json(self) -> None:
    """A GET for a checkout session that does not exist answers with JSON."""
    self._assert_body_is_json(
      f"/checkout-sessions/{_MISSING_ID}", "checkout session"
    )

  @requires_capability(_ORDER_CAPABILITY)
  @spec_assert(
    "REQ-SHOPPING-ORDER-0034",  # response bodies MUST be valid JSON
  )
  def test_order_refusal_body_is_json(self) -> None:
    """A GET for an order that does not exist answers with JSON."""
    self._assert_body_is_json(f"/orders/{_MISSING_ID}", "order")


if __name__ == "__main__":
  absltest.main()
