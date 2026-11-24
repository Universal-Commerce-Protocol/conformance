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

"""Conformance test for eligibility claims that a business does not recognize.

checkout.md "Eligibility Verification at Completion" says platforms provide
`context.eligibility`, that these are claims rather than verified facts, and
that unrecognized or inapplicable claims MUST NOT block the checkout. No test
sends an eligibility claim at all today, so nothing exercises that.

The claim this test sends is a reverse domain name under `com.example` that
names no benefit any business could offer, so a business cannot recognize it
and the MUST NOT applies by construction rather than by assumption.

What "not blocked" is taken to mean is the observable thing: the create is not
refused, and the checkout still reaches a completed order. Both halves matter.
A business that refuses the create fails the first, and one that accepts the
create and then refuses completion fails the second, which is the posture a
create only assertion would miss.

Deliberately not asserted, because it would be stricter than the rule. The
specification gives `warning` with `eligibility_not_accepted` for a claim that
is not recognized, but only as a SHOULD, so its absence is not a failure. And
nothing here asserts that `eligibility_invalid` is absent: that code belongs to
accepted claims left unverified at completion, so a business using it for an
unrecognized claim would be misreading the table, but it would not thereby have
blocked anything. The only failure is a checkout that does not get through.
"""

from typing import Any

from absl.testing import absltest
from framework.decorators import requires_capability
from framework.decorators import spec_assert
from shopping.base import ShoppingIntegrationTestBase

_CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"

# A reverse domain name under the documentation domain, naming a benefit no
# business offers, so no business can recognize it.
_UNRECOGNIZED_CLAIM = "com.example.ucp_conformance_unknown_benefit"


@requires_capability(_CHECKOUT_CAPABILITY)
class CheckoutEligibilityTest(ShoppingIntegrationTestBase):
  """An unrecognized eligibility claim does not stop a checkout."""

  @spec_assert(
    "REQ-SHOPPING-CHECKOUT-0048",  # unrecognized claims MUST NOT block
  )
  def test_unrecognized_eligibility_claim_does_not_block_the_checkout(
    self,
  ) -> None:
    """An unrecognized claim still reaches a completed order.

    Given a checkout created with a context.eligibility claim that names a
    benefit no business offers,
    When the checkout is driven through fulfillment selection and completion,
    Then the create is not refused and the completion returns an order.
    """
    payload: dict[str, Any] = self.create_checkout_payload().model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    payload["context"] = {"eligibility": [_UNRECOGNIZED_CLAIM]}

    created = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(created, [200, 201])
    checkout_id = created.json()["id"]

    # complete_checkout_session selects fulfillment itself, so there is no
    # separate call for it here.
    completed = self.complete_checkout_session(checkout_id)

    order_id = (completed.get("order") or {}).get("id")
    self.assertIsNotNone(
      order_id,
      "a checkout carrying an unrecognized eligibility claim did not reach a "
      f"completed order, so the claim blocked it: {completed}",
    )


if __name__ == "__main__":
  absltest.main()
