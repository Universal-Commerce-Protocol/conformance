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

"""Conformance test for the order Authorization rule (order.md).

order.md "Authorization" states the obligation twice, once in the section and
once in the Business list: a business MUST authenticate requests to order data
before returning a response, using any supported UCP mechanism. No test cites
either statement.

What is asserted here is the observable consequence rather than a mechanism: a
request that identifies its caller in no way does not come back carrying order
data. Of the four headers the suite sends, exactly one speaks for the caller,
UCP-Agent, which order-rest.md calls platform identification. It is omitted
here while the correlation headers request-id and idempotency-key are kept, so
that a refusal for a missing correlation id cannot be mistaken for a refusal to
authenticate.

Deliberately not asserted: the status code, the error shape, and which
mechanism the business uses. A business is free to answer 401, 403 or a 200
envelope carrying only messages, and free to authenticate by API key, OAuth,
mutual TLS or message signatures. The only failure is order data served to a
caller that identified itself in no way.

Out of scope, and the reason both citations are partly. Of the five options
checkout-rest.md lists under Authentication, four are credentials the suite
never sends, X-API-Key, OAuth 2.0 bearer, mutual TLS and RFC 9421 message
signatures, and the first, Open API, is the absence of one, which is the
posture the suite already has. UCP-Agent is none of them, so removing it
withholds identification rather than a credential, and this test cannot tell a
business that verifies who sent UCP-Agent from one that only requires the
header to be present. The reference, run as the suite runs it, is the second
kind, because its order route discards the header it demands and verifies no
signature by default, and this test passes against it. A business that
authenticates with something the suite does not send, mutual TLS, an API key or
an enforced message signature, fails or errors at the first authenticated
request, which is the checkout creation rather than the order read, so it is
not misjudged here, it is not judged at all.

One stated limit, and it is the accepted price of the narrow condition. The
failure condition is the top level id, so a business that served every field of
the order anonymously except id would pass here. test_order_retrieval does not
rescue that case: it only ever reads with the full header set, so it sees such
a business only if the identified read is shaped the same way.

A second limit, in the other direction, and this one can fail a business that
conforms. checkout-rest.md introduces its five options with MAY, so the list is
permissive rather than exhaustive, and a business may authenticate by a means
that leaves no trace in the request headers: mutual TLS terminated at a
perimeter, or a mesh identity, with the business itself lenient about
UCP-Agent. Such a business has authenticated and would still serve this probe,
so this test fails it. It is out of reach rather than out of scope: the suite
can present no credential of any kind, it sends no X-API-Key, no Authorization
and no client certificate, so there is no posture it could adopt that would let
this test tell that business apart from one that authenticates nothing. Anyone
running the suite behind such a perimeter should read a failure here as that,
not as a finding.
"""

from absl.testing import absltest
from framework.decorators import partly
from framework.decorators import requires_capability
from framework.decorators import spec_assert
from shopping.base import ShoppingIntegrationTestBase

_ORDER_CAPABILITY = "dev.ucp.shopping.order"

# The only header the suite sends that speaks for the caller. Everything else
# it sends is correlation or content typing.
_IDENTITY_HEADERS = ("ucp-agent",)

_PARTLY_REASON = (
  "a caller that sends no identifying header at all; the suite presents none "
  "of the mechanisms the specification lists, so a business that only "
  "requires UCP-Agent to be present is not told apart from one that "
  "authenticates"
)


def _without_identity(headers: dict) -> dict:
  """Return the headers with the identifying ones removed, correlation kept."""
  return {
    name: value
    for name, value in headers.items()
    if name.lower() not in _IDENTITY_HEADERS
  }


@requires_capability(_ORDER_CAPABILITY)
class OrderAuthorizationTest(ShoppingIntegrationTestBase):
  """The order read path refuses a caller that identifies itself in no way."""

  @spec_assert(
    partly(
      "REQ-SHOPPING-ORDER-0009",  # authenticate before returning order data
      _PARTLY_REASON,
    ),
    partly(
      "REQ-SHOPPING-ORDER-0015",  # the same obligation in the Business list
      _PARTLY_REASON,
    ),
  )
  def test_order_data_is_not_served_to_an_unidentified_caller(self) -> None:
    """Order data needs a caller that identifies itself.

    Given a completed order the platform can read with its own headers,
    When the same order is requested with every identifying header removed and
    only the correlation headers kept,
    Then the response does not carry order data.
    """
    created = self.create_checkout_session()
    completed = self.complete_checkout_session(created["id"])
    order_id = (completed.get("order") or {}).get("id")
    self.assertIsNotNone(
      order_id,
      f"Completion returned no order to read: {completed}",
    )

    url = self.get_shopping_url(f"/orders/{order_id}")
    authenticated = self.client.get(url, headers=self.get_headers())
    self.assert_response_status(authenticated, 200)
    self.assertEqual(
      authenticated.json().get("id"),
      order_id,
      "The authenticated read did not return the order under test",
    )

    anonymous = self.client.get(
      url, headers=_without_identity(self.get_headers())
    )
    try:
      body = anonymous.json()
    except ValueError:
      body = anonymous.text
    served_id = body.get("id") if isinstance(body, dict) else None
    self.assertNotEqual(
      served_id,
      order_id,
      "Order data was served to a caller that presented no identifying "
      f"header; status {anonymous.status_code}, body {body}",
    )


if __name__ == "__main__":
  absltest.main()
