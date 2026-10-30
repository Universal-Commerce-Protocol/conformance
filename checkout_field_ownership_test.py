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

"""Structural conformance tests for checkout server-owned fields.

``checkout.json`` marks ``continue_url``, ``expires_at``, ``messages``, and
``order`` with ``"ucp_request": "omit"``, the schema-authoring vocabulary
that excludes a field from every request variant (create and update) while
keeping it on the response. These four members exist only so the business
can communicate state back to the caller; a caller has no schema-legal way
to set them, and a compliant create handler that builds the response by
merging or spreading the raw request body must still not let a value the
caller supplied for one of these members reach either the response or the
stored checkout.

This module sends a caller payload that stuffs values into all four members
anyway (bypassing the SDK request model, which has no fields for them, by
posting a raw JSON body, which is what an uncooperative or buggy client can
do) and asserts the values the business itself set come back instead, both
in the create response and when the session is read back afterward.
"""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping.payment import Payment

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class CheckoutFieldOwnershipTest(integration_test_utils.IntegrationTestBase):
  """Caller-supplied values for omit-only checkout members must not stick.

  Validated Paths:
  - POST /checkout-sessions
  - GET /checkout-sessions/{id}
  """

  # Deliberately implausible values: if any of these come back unchanged,
  # the business adopted a member it does not own.
  CALLER_SUPPLIED = {
    "continue_url": "https://caller.example/attacker-chosen",
    "expires_at": "2099-01-01T00:00:00Z",
    "messages": [
      {"type": "info", "code": "custom", "content": "caller supplied text"}
    ],
    "order": {
      "id": "order_caller_chosen",
      "checkout_session_id": "not-a-real-session",
      "permalink_url": "https://caller.example/order",
    },
  }

  def _post_checkout_with_caller_supplied_fields(self) -> dict:
    """POST a checkout create request carrying all four omit-only members.

    Builds the payload the same way ``create_checkout_payload`` does, then
    layers the caller-supplied members onto the raw JSON dict (the SDK
    ``CheckoutCreateRequest`` has no fields for them, so this bypasses the
    model rather than fighting it, matching what a non-SDK client would
    send on the wire).
    """
    payload = self.create_checkout_payload().model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    payload.update(self.CALLER_SUPPLIED)

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, [200, 201])
    return response.json()

  def _assert_caller_values_absent(self, data: dict, where: str) -> None:
    """Assert none of the caller-supplied omit values are present in data."""
    self.assertNotEqual(
      data.get("continue_url"),
      self.CALLER_SUPPLIED["continue_url"],
      f"continue_url is business owned (checkout.json ucp_request: omit); "
      f"the caller-supplied value must not appear in {where}",
    )
    self.assertNotEqual(
      data.get("expires_at"),
      self.CALLER_SUPPLIED["expires_at"],
      f"expires_at is business owned (checkout.json ucp_request: omit); "
      f"the caller-supplied value must not appear in {where}",
    )
    message_contents = [m.get("content") for m in (data.get("messages") or [])]
    self.assertNotIn(
      "caller supplied text",
      message_contents,
      f"messages is business owned (checkout.json ucp_request: omit); "
      f"the caller-supplied entry must not appear in {where}",
    )
    self.assertNotEqual(
      (data.get("order") or {}).get("id"),
      self.CALLER_SUPPLIED["order"]["id"],
      f"order is business owned (checkout.json ucp_request: omit); "
      f"the caller-supplied value must not appear in {where}",
    )

  def test_create_does_not_adopt_caller_supplied_omit_members(self) -> None:
    """The create response must not echo back caller-owned-field values.

    Given a checkout creation payload that also sets continue_url,
    expires_at, messages, and order,
    When a POST request is sent to /checkout-sessions,
    Then the response must not contain any of the caller-supplied values
    for those four members.
    """
    data = self._post_checkout_with_caller_supplied_fields()
    self._assert_caller_values_absent(data, "the create response")

  def test_create_does_not_persist_caller_supplied_omit_members(self) -> None:
    """Caller-owned-field values must not be persisted, only reflected.

    Given a checkout created with caller-supplied values for the four
    omit-only members,
    When the session is read back with GET /checkout-sessions/{id},
    Then the stored checkout must not contain any of the caller-supplied
    values either, ruling out a handler that merely omits them from the
    immediate response while still writing them to storage.
    """
    created = self._post_checkout_with_caller_supplied_fields()
    checkout_id = created["id"]

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    stored = response.json()
    self._assert_caller_values_absent(stored, "the stored checkout")

  def test_create_without_omit_members_is_unaffected(self) -> None:
    """A create that never sends these members must still succeed.

    Given a checkout creation payload that omits continue_url, expires_at,
    messages, and order entirely (the ordinary, spec-conformant case),
    When a POST request is sent to /checkout-sessions,
    Then the response should have a 200/201 status and include a checkout
    ID. This test guards against a fix that starts rejecting the absence
    of these fields while chasing their presence.
    """
    data = self.create_checkout_session(select_fulfillment=False)
    self.assertTrue(data.get("id"), "Created checkout missing ID")


if __name__ == "__main__":
  absltest.main()
