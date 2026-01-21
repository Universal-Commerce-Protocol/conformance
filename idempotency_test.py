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

"""Idempotency tests for the UCP SDK Server."""

import uuid

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class IdempotencyTest(integration_test_utils.IntegrationTestBase):
  """Tests for API idempotency.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  - POST /checkout-sessions/{id}/cancel
  """

  def test_idempotency_create(self) -> None:
    """Test that checkout creation is idempotent.

    Given a checkout creation request with a specific idempotency key,
    When the same request is sent multiple times,
    Then the subsequent responses should match the first response exactly,
    and a modified request with the same key should result in a 409 Conflict.
    """
    idem_key = str(uuid.uuid4())
    create_payload = self.create_checkout_payload()

    # 1. Initial Request
    headers = integration_test_utils.get_headers(idempotency_key=idem_key)
    response1 = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response1, [200, 201])

    # 2. Duplicate Request
    response2 = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assertEqual(
      response2.status_code,
      response1.status_code,
      msg="Idempotency Failed: Status code mismatch.",
    )
    self.assertEqual(
      response2.json(),
      response1.json(),
      msg="Idempotency Failed: Response body mismatch.",
    )

    # 3. Conflict Request
    conflict_payload = create_payload.model_copy(deep=True)
    conflict_payload.currency = "EUR"
    response3 = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=conflict_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response3, 409)

  def test_idempotency_update(self) -> None:
    """Test that checkout update is idempotent.

    Given a checkout update request with a specific idempotency key,
    When the same update request is sent multiple times,
    Then the subsequent responses should match the first response exactly,
    and a modified update request with the same key should result in a 409
    Conflict.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)

    # 1. Initial Update
    idem_key = str(uuid.uuid4())
    headers = integration_test_utils.get_headers(idempotency_key=idem_key)

    # Use the helper logic but manually call to control headers/idempotency key
    # We construct the update payload same as helper does
    line_items_req = []
    for li in checkout_obj.line_items:
      line_items_req.append(
        {
          "item": {"id": li.item.id, "title": li.item.title},
          "quantity": 2,  # Change quantity
          "id": li.id,
        }
      )

    payment_req = {
      "selected_instrument_id": checkout_obj.payment.selected_instrument_id,
      "instruments": [
        i.model_dump(mode="json", exclude_none=True)
        for i in checkout_obj.payment.instruments
      ],
    }

    update_payload = {
      "id": checkout_obj.id,
      "currency": checkout_obj.currency,
      "line_items": line_items_req,
      "payment": payment_req,
    }

    response1 = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=update_payload,
      headers=headers,
    )
    self.assert_response_status(response1, 200)

    # 2. Duplicate Request
    response2 = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=update_payload,
      headers=headers,
    )
    self.assertEqual(
      response2.status_code,
      200,
      msg="Idempotency Update Failed: Status code mismatch.",
    )
    self.assertEqual(
      response2.json(),
      response1.json(),
      msg="Idempotency Update Failed: Response body mismatch.",
    )

    # 3. Conflict Request
    conflict_payload = update_payload.copy()
    conflict_payload["line_items"][0]["quantity"] = 3

    response3 = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=conflict_payload,
      headers=headers,
    )
    self.assert_response_status(response3, 409)

  def test_idempotency_complete(self) -> None:
    """Test that checkout completion is idempotent.

    Given a checkout completion request with a specific idempotency key,
    When the same completion request is sent multiple times,
    Then the subsequent responses should match the first response exactly,
    and a modified completion request with the same key should result in a 409
    Conflict.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    # 1. Initial Complete
    idem_key = str(uuid.uuid4())
    headers = integration_test_utils.get_headers(idempotency_key=idem_key)
    complete_payload = integration_test_utils.get_valid_payment_payload()

    response1 = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=complete_payload,
      headers=headers,
    )
    self.assert_response_status(response1, 200)

    # 2. Duplicate Request
    response2 = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=complete_payload,
      headers=headers,
    )
    self.assertEqual(
      response2.status_code,
      200,
      msg="Idempotency Complete Failed: Status code mismatch.",
    )
    self.assertEqual(
      response2.json(),
      response1.json(),
      msg="Idempotency Complete Failed: Response body mismatch.",
    )

    # 3. Conflict Request
    complete_payload_diff = integration_test_utils.get_valid_payment_payload()
    complete_payload_diff["payment_data"]["credential"]["token"] = (
      "different_token"
    )
    response3 = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=complete_payload_diff,
      headers=headers,
    )
    self.assert_response_status(response3, 409)

  def test_idempotency_cancel(self) -> None:
    """Test that checkout cancellation is idempotent.

    Given a checkout cancellation request with a specific idempotency key,
    When the same cancellation request is sent multiple times,
    Then the subsequent responses should match the first response exactly.
    """
    response_json = self.create_checkout_session()
    checkout_id = checkout.Checkout(**response_json).id

    # 1. Initial Cancel
    idem_key = str(uuid.uuid4())
    headers = integration_test_utils.get_headers(idempotency_key=idem_key)

    response1 = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=headers,
    )
    self.assert_response_status(response1, 200)

    # 2. Duplicate Request
    response2 = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/cancel"),
      headers=headers,
    )
    self.assertEqual(
      response2.status_code,
      200,
      msg="Idempotency Cancel Failed: Status code mismatch.",
    )
    self.assertEqual(
      response2.json(),
      response1.json(),
      msg="Idempotency Cancel Failed: Response body mismatch.",
    )


if __name__ == "__main__":
  absltest.main()
