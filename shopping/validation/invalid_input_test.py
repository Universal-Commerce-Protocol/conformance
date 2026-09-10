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

"""Tests for invalid inputs to the UCP SDK Server."""

import datetime
import uuid
from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import order

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


@requires_capability("dev.ucp.shopping.order")
class InvalidInputTest(ShoppingIntegrationTestBase):
  """Tests for invalid inputs and schema validation.

  Validated Paths:
  - GET /orders/{id}
  - PUT /orders/{id}
  """

  def test_invalid_adjustment_status(self):
    """Test that an invalid adjustment status is rejected.

    Given a completed order,
    When an update is requested with an invalid adjustment status,
    Then the server should return a 422 Unprocessable Entity error due to
    validation failure.
    """
    order_id = self.create_completed_order()

    # Get Order
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    order_obj = order.Order(**response.json())
    order_dict = order_obj.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

    # Add Adjustment with invalid status
    adj = {
      "id": f"adj_{uuid.uuid4()}",
      "type": "refund",
      "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "status": "INVALID_STATUS",  # Invalid literal
      "amount": 500,
    }

    if order_dict.get("adjustments") is None:
      order_dict["adjustments"] = []
    order_dict["adjustments"].append(adj)

    # Update Order
    resp = self.client.put(
      self.get_order_url(order_id),
      json=order_dict,
      headers=self.get_headers(),
    )
    # Pydantic validation error should result in 422
    self.assert_response_status(resp, 422)

  def test_malformed_adjustment_payload(self):
    """Test that malformed adjustment payloads are rejected.

    Given a completed order,
    When an update is requested with a malformed adjustments field (e.g., a dict
    instead of a list),
    Then the server should return a 422 Unprocessable Entity error.
    """
    order_id = self.create_completed_order()

    # Get Order
    response = self.client.get(
      self.get_order_url(order_id), headers=self.get_headers()
    )
    order_obj = order.Order(**response.json())
    order_dict = order_obj.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

    # Corrupt the adjustments field (dict instead of list)
    order_dict["adjustments"] = {"id": "adj_1", "amount": 100}

    # Update Order
    resp = self.client.put(
      self.get_order_url(order_id),
      json=order_dict,
      headers=self.get_headers(),
    )

    self.assert_response_status(resp, 422)


if __name__ == "__main__":
  absltest.main()
