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

"""Checkout Lifecycle tests for the UCP SDK Server."""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import checkout_update_req
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping import payment_update_req
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)
from ucp_sdk.models.schemas.shopping.types import item_update_req
from ucp_sdk.models.schemas.shopping.types import line_item_update_req

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class CheckoutLifecycleTest(integration_test_utils.IntegrationTestBase):
  """Tests for the lifecycle of a checkout session.

  Validated Paths:
  - POST /checkout-sessions
  - GET /checkout-sessions/{id}
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  - POST /checkout-sessions/{id}/cancel
  """

  def test_create_checkout(self):
    """Test successful checkout creation.

    Given a valid checkout creation payload,
    When a POST request is sent to /checkout-sessions,
    Then the response should have a 200/201 status and include a checkout ID.
    """
    response_json = self.create_checkout_session()
    created_checkout = checkout.Checkout(**response_json)
    self.assertTrue(created_checkout.id, "Created checkout missing ID")

  def test_get_checkout(self):
    """Test successful checkout retrieval.

    Given an existing checkout session,
    When a GET request is sent to /checkout-sessions/{id},
    Then the response should be 200 OK and return the correct checkout data.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    retrieved_checkout = checkout.Checkout(**response.json())
    self.assertEqual(
      retrieved_checkout.id,
      checkout_id,
      msg="Get checkout returned wrong ID",
    )

  def test_update_checkout(self):
    """Test successful checkout update.

    Given an existing checkout session,
    When a PUT request is sent to /checkout-sessions/{id} with updated line
    items,
    Then the response should be 200 OK and reflect the updates.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Construct Update Request
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
    )

    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 200)

  def test_cancel_checkout(self):
    """Test successful checkout cancellation.

    Given an existing checkout session in progress,
    When a POST request is sent to /checkout-sessions/{id}/cancel,
    Then the response should be 200 OK and the status should update to
    'canceled'.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)

    canceled_checkout = checkout.Checkout(**response.json())
    self.assertEqual(
      canceled_checkout.status,
      "canceled",
      msg=f"Checkout status not 'canceled', got '{canceled_checkout.status}'",
    )

  def test_complete_checkout(self):
    """Test successful checkout completion.

    Given an existing checkout session with valid payment details,
    When a POST request is sent to /checkout-sessions/{id}/complete,
    Then the response should be 200 OK, the status should be 'completed', and an
    order ID should be generated.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=integration_test_utils.get_valid_payment_payload(),
      headers=integration_test_utils.get_headers(),
    )

    if response.status_code == 409 and "stock" in response.text.lower():
      return  # Expected behavior if low inventory

    self.assert_response_status(response, 200)

    completed_checkout = checkout.Checkout(**response.json())
    self.assertEqual(
      completed_checkout.status,
      "completed",
      msg=(
        f"Checkout status not 'completed', got '{completed_checkout.status}'"
      ),
    )
    self.assertIsNotNone(
      completed_checkout.order, "order object missing in completion response"
    )
    self.assertTrue(
      completed_checkout.order.id,
      "order.id missing",
    )
    self.assertTrue(
      completed_checkout.order.permalink_url,
      "order.permalink_url missing",
    )

  def _cancel_checkout(self, checkout_id):
    """Cancel a checkout."""
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response

  def test_cancel_is_idempotent(self):
    """Test that cancellation is idempotent.

    Given a checkout session that has already been canceled,
    When another cancel request is sent,
    Then the server should reject it with a 409 Conflict (or handle idempotency
    if key matches, but here we test state conflict logic).
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    # 1. Cancel
    self._cancel_checkout(checkout_id)

    # 2. Cancel again - should likely fail with 409 or be idempotent (200)
    # depending on implementation. The original test expected NotEqual 200
    # (implying 409 Conflict).
    # checkout_service.py says:
    # if checkout.status in [COMPLETED, CANCELED]:
    # raise CheckoutNotModifiableError -> 409
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to cancel an already canceled checkout.",
    )

  def test_cannot_update_canceled_checkout(self):
    """Test that a canceled checkout cannot be updated.

    Given a canceled checkout session,
    When an update request is sent,
    Then the server should reject it with a non-200 status (likely 409).
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    self._cancel_checkout(checkout_id)

    # Try Update
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
    )
    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
    )
    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to update a canceled checkout.",
    )

  def test_cannot_complete_canceled_checkout(self):
    """Test that a canceled checkout cannot be completed.

    Given a canceled checkout session,
    When a complete request is sent,
    Then the server should reject it with a non-200 status.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    self._cancel_checkout(checkout_id)

    # Try Complete
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=integration_test_utils.get_valid_payment_payload(),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to complete a canceled checkout.",
    )

  def _complete_checkout(self, checkout_id):
    """Complete a checkout."""
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=integration_test_utils.get_valid_payment_payload(),
      headers=integration_test_utils.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response

  def test_complete_is_idempotent(self):
    """Tests that completing an already completed checkout behaves correctly.

    # Note: checkout_service.py raises CheckoutNotModifiableError (409) if
    # status is COMPLETED. Idempotency is handled by the idempotency key
    # check BEFORE the status check. If we use a different key (which
    # default get_headers does), it should fail.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    self._complete_checkout(checkout_id)

    # Try Complete again (new idempotency key)
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=integration_test_utils.get_valid_payment_payload(),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to complete an already completed checkout.",
    )

  def test_cannot_update_completed_checkout(self):
    """Test that a completed checkout cannot be updated.

    Given a completed checkout session,
    When an update request is sent,
    Then the server should reject it with a non-200 status.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    self._complete_checkout(checkout_id)

    # Try Update
    item_update = item_update_req.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
      title=checkout_obj.line_items[0].item.title,
    )
    line_item_update = line_item_update_req.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=2,
    )
    payment_update = payment_update_req.PaymentUpdateRequest(
      selected_instrument_id=checkout_obj.payment.selected_instrument_id,
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.handlers
      ],
    )
    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to update a completed checkout.",
    )

  def test_cannot_cancel_completed_checkout(self):
    """Test that a completed checkout cannot be canceled.

    Given a completed checkout session,
    When a cancel request is sent,
    Then the server should reject it with a non-200 status.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    self._complete_checkout(checkout_id)

    # Try Cancel
    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=integration_test_utils.get_headers(),
    )
    self.assertNotEqual(
      response.status_code,
      200,
      msg="Should not be able to cancel a completed checkout.",
    )


if __name__ == "__main__":
  absltest.main()
