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
"""Tests for the checkout tools of the MCP binding."""

from typing import Any
import uuid

from absl.testing import absltest
from framework.decorators import spec_assert
from framework.decorators import outside_catalog
from framework.decorators import no_rule
from framework.decorators import partly
from framework.decorators import requires_capability
from shopping.base import get_valid_payment_payload
from shopping.base import ShoppingIntegrationTestBase
from shopping.mcp import McpIntegrationTestBase
from shopping.mcp import METHOD_NOT_FOUND
from shopping.mcp import PROTOCOL_ERROR
from ucp_sdk.models.schemas.shopping import checkout as checkout

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

CHECKOUT_TOOLS = (
  "create_checkout",
  "get_checkout",
  "update_checkout",
  "complete_checkout",
  "cancel_checkout",
)

# Fields of the payload ShoppingIntegrationTestBase builds for REST that a
# create_checkout call must not carry: the checkout has no id before it is
# created, and the others are for the business to set.
NOT_IN_CREATE_ARGUMENT = frozenset({"id", "status", "ucp", "totals", "links"})


@requires_capability("dev.ucp.shopping.checkout")
class McpCheckoutTest(McpIntegrationTestBase, ShoppingIntegrationTestBase):
  """Tests for the checkout tools served over the MCP transport.

  Validated Tools:
  - create_checkout
  - get_checkout
  - update_checkout
  - complete_checkout
  - cancel_checkout
  """

  # Some servers need to know the buyer before they complete a checkout.
  DEFAULT_BUYER = {
    "email": "conformance-test-buyer@example.com",
    "name": "Conformance Test Buyer",
  }

  def fetch_checkout(self, checkout_id: str) -> Any:
    """Read a checkout through get_checkout."""
    return self.tool_payload(self.call_tool("get_checkout", id=checkout_id))

  def send_checkout_update(
    self, checkout_id: str, payload: dict[str, Any], headers: dict[str, str]
  ) -> Any:
    """Send a checkout update through update_checkout."""
    # The binding names the checkout by the top-level id, never in the body.
    checkout_body = {k: v for k, v in payload.items() if k != "id"}
    return self.tool_payload(
      self.call_tool(
        "update_checkout",
        meta=self.request_meta(headers.get("idempotency-key")),
        id=checkout_id,
        checkout=checkout_body,
      )
    )

  def create_argument(self, **kwargs: Any) -> dict[str, Any]:
    """Build the `checkout` argument of a create_checkout call."""
    payload = self.create_checkout_payload(**kwargs).model_dump(
      mode="json", by_alias=True, exclude_none=True
    )
    return {k: v for k, v in payload.items() if k not in NOT_IN_CREATE_ARGUMENT}

  def create_over_mcp(self, **kwargs: Any) -> dict[str, Any]:
    """Create a checkout through create_checkout and return it."""
    return self.tool_payload(
      self.call_tool("create_checkout", checkout=self.create_argument(**kwargs))
    )

  @spec_assert(
    partly(
      "REQ-SHOPPING-CHECKOUT-0237",  # a conforming MCP implementation MUST
      "item 1 only (implement JSON-RPC 2.0 correctly), and of that only "
      "the response envelope, initialize and the method-not-found error",
    ),
  )
  def test_speaks_json_rpc(self) -> None:
    """Test that the endpoint implements JSON-RPC 2.0.

    Given the MCP endpoint from the discovery profile,
    When a session is initialized and an unknown method is called,
    Then initialize answers with a protocol version and the unknown method
    answers with the JSON-RPC "method not found" error.
    """
    self.assertIn("protocolVersion", self.initialize_result)

    _, body = self.rpc("ucp/no_such_method")
    self.assertNotIn("result", body)
    self.assertEqual(body.get("error", {}).get("code"), METHOD_NOT_FOUND)

  @spec_assert(
    partly(
      "REQ-SHOPPING-CHECKOUT-0237",  # a conforming MCP implementation MUST
      "item 2 only (provide all core checkout tools): the five tools are "
      "listed by tools/list",
    ),
  )
  def test_lists_checkout_tools(self) -> None:
    """Test that every core checkout tool is provided.

    Given an MCP session,
    When tools/list is called,
    Then all five checkout tools are listed.
    """
    self.assert_tools_listed(CHECKOUT_TOOLS)

  @no_rule(
    "checkout/mcp.md, Identifier Pattern: the create_checkout response "
    "includes the newly assigned checkout.id, and the create_checkout "
    "output is the Checkout object; neither is stated with a keyword"
  )
  def test_create_returns_checkout(self) -> None:
    """Test that create_checkout returns the checkout in the UCP envelope.

    Given a valid checkout payload,
    When create_checkout is called,
    Then the result's structuredContent is a checkout that carries its new
    id and the ucp metadata.
    """
    checkout_data = self.create_over_mcp()

    checkout.Checkout(**checkout_data)
    self.assertTrue(checkout_data.get("id"))
    self.assertIn("version", checkout_data.get("ucp", {}))

  @no_rule(
    "checkout/mcp.md, Identifier Pattern: for operations on existing "
    "checkouts a top-level id parameter identifies the target resource; "
    "stated without a keyword"
  )
  def test_get_uses_top_level_id(self) -> None:
    """Test that get_checkout finds a checkout by the top-level id.

    Given two checkouts created over MCP,
    When get_checkout is called with the id of the first,
    Then the first checkout is returned.
    """
    created = self.create_over_mcp()
    self.create_over_mcp()

    fetched = self.fetch_checkout(created["id"])

    self.assertEqual(fetched.get("id"), created["id"])
    self.assertEqual(fetched["status"], created["status"])

  @no_rule(
    "checkout/mcp.md, update_checkout: the tool maps to the Update "
    "Checkout operation and returns the updated Checkout; stated without "
    "a keyword"
  )
  def test_update_changes_checkout(self) -> None:
    """Test that update_checkout applies a change.

    Given a checkout with one unit of an item,
    When update_checkout sets the quantity to two,
    Then the returned checkout holds two units.
    """
    created = self.create_over_mcp()
    checkout_obj = checkout.Checkout(**created)
    checkout_obj.line_items[0].quantity = 2

    updated = self.update_checkout_session(checkout_obj)

    self.assertEqual(updated["id"], created["id"])
    self.assertEqual(updated["line_items"][0]["quantity"], 2)

  @no_rule(
    "checkout/mcp.md, complete_checkout: the tool maps to the Complete "
    "Checkout operation, whose result is a completed checkout with its "
    "order; stated without a keyword"
  )
  def test_complete_places_order(self) -> None:
    """Test that complete_checkout places the order.

    Given a checkout that is ready for completion,
    When complete_checkout is called with a valid payment,
    Then the checkout is completed and carries the order id.
    """
    created = self.create_over_mcp(buyer=self.DEFAULT_BUYER)
    checkout_id = created["id"]
    self.ensure_fulfillment_ready(checkout_id)

    completed = self.tool_payload(
      self.call_tool(
        "complete_checkout",
        id=checkout_id,
        checkout=get_valid_payment_payload(),
      )
    )

    self.assertEqual(completed["status"], "completed")
    self.assertTrue(completed.get("order", {}).get("id"))

  @no_rule(
    "checkout/mcp.md, cancel_checkout: the output is the Checkout object "
    "with status canceled; stated without a keyword"
  )
  def test_cancel(self) -> None:
    """Test that cancel_checkout cancels the checkout.

    Given a checkout created over MCP,
    When cancel_checkout is called,
    Then the returned checkout has status canceled.
    """
    created = self.create_over_mcp()

    canceled = self.tool_payload(
      self.call_tool("cancel_checkout", id=created["id"])
    )

    self.assertEqual(canceled["status"], "canceled")

  @no_rule(
    'checkout/mcp.md, Request Metadata: meta["ucp-agent"] is required '
    "on all requests, in bold but without an RFC 2119 keyword; the "
    "invalid_profile_url discovery error (HTTP 400, -32001) is in the "
    "overview page's error registry"
  )
  def test_ucp_agent_is_required(self) -> None:
    """Test that a call without meta["ucp-agent"] fails discovery.

    Given a create_checkout call whose meta names no platform profile,
    When it is sent,
    Then the server answers with the discovery error, as REST does for a
    missing profile URL, or rejects the arguments as invalid.
    """
    body = self.call_tool(
      "create_checkout",
      meta={"idempotency-key": str(uuid.uuid4())},
      checkout=self.create_argument(),
    )

    self.assert_missing_agent_rejected(body)

  @no_rule(
    "checkout/mcp.md, Request Metadata: complete_checkout and "
    'cancel_checkout also require meta["idempotency-key"] for retry '
    "safety; stated without an RFC 2119 keyword"
  )
  def test_final_operations_require_idempotency_key(self) -> None:
    """Test that complete and cancel need meta["idempotency-key"].

    Given a checkout that is ready for completion,
    When complete_checkout and cancel_checkout are called without the key,
    Then neither is processed and the checkout is unchanged.
    """
    created = self.create_over_mcp(buyer=self.DEFAULT_BUYER)
    checkout_id = created["id"]
    ready = self.ensure_fulfillment_ready(checkout_id)

    self.assert_not_processed(
      self.call_tool(
        "complete_checkout",
        meta=self.request_meta(include_idempotency_key=False),
        id=checkout_id,
        checkout=get_valid_payment_payload(),
      )
    )
    self.assert_not_processed(
      self.call_tool(
        "cancel_checkout",
        meta=self.request_meta(include_idempotency_key=False),
        id=checkout_id,
      )
    )

    self.assertEqual(
      self.fetch_checkout(checkout_id)["status"], ready["status"]
    )

  @spec_assert(
    partly(
      "REQ-SHOPPING-CHECKOUT-0237",  # a conforming MCP implementation MUST
      "item 4 only (business outcomes as a JSON-RPC result with the UCP "
      "envelope and messages), for one outcome: an out-of-stock item on "
      "create_checkout",
    ),
  )
  def test_business_outcome_is_a_result(self) -> None:
    """Test that a business error is not a JSON-RPC error.

    Given a product with no inventory,
    When create_checkout is called for it,
    Then the response is a JSON-RPC result whose UCP envelope carries a
    stock error message.
    """
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    outcome = self.create_over_mcp(item_id=out_of_stock_item["id"])

    errors = [
      m for m in outcome.get("messages", []) if m.get("type") == "error"
    ]
    self.assertTrue(
      errors, msg=f"Expected an error message in the envelope: {outcome}"
    )
    codes = {str(m.get("code", "")).lower() for m in errors}
    self.assertTrue(
      codes & {"out_of_stock", "item_unavailable"},
      msg=f"Expected a stock error code, got {sorted(codes)}",
    )

  @outside_catalog(
    "overview/index.md, Error Handling: an idempotency key reused with a "
    "different payload is HTTP 409 with JSON-RPC -32000; the cached result "
    "for a duplicate key is a rule of checkout/rest.md's Specific Header "
    "Requirements (rendered by the header_requirements macro, so the "
    'catalog lacks it), which meta["idempotency-key"] maps to'
  )
  def test_complete_is_idempotent(self) -> None:
    """Test that meta["idempotency-key"] makes complete_checkout retry safe.

    Given a complete_checkout call with an idempotency key,
    When the same call is repeated,
    Then the same order is returned,
    and reusing the key with a different payment is a protocol error.
    """
    created = self.create_over_mcp(buyer=self.DEFAULT_BUYER)
    checkout_id = created["id"]
    self.ensure_fulfillment_ready(checkout_id)
    meta = self.request_meta()
    payment = get_valid_payment_payload()

    first = self.tool_payload(
      self.call_tool(
        "complete_checkout", meta=meta, id=checkout_id, checkout=payment
      )
    )
    replay = self.tool_payload(
      self.call_tool(
        "complete_checkout", meta=meta, id=checkout_id, checkout=payment
      )
    )
    self.assertEqual(replay, first)

    other_payment = get_valid_payment_payload()
    other_payment["payment"]["instruments"][0]["credential"]["token"] = (
      "different_token"
    )
    conflict = self.call_tool(
      "complete_checkout", meta=meta, id=checkout_id, checkout=other_payment
    )
    self.assert_rpc_error(conflict, PROTOCOL_ERROR, http_status=409)

  @outside_catalog(
    "The MCP specification, which checkout/mcp.md binds to: a tools/call "
    "request for an unknown tool is answered with an error"
  )
  def test_unknown_tool_is_an_error(self) -> None:
    """Test that calling a tool the server does not provide is an error.

    Given an MCP session,
    When tools/call names a tool that does not exist,
    Then the call is refused.
    """
    body = self.call_tool("no_such_checkout_tool")

    if "error" not in body:
      self.assertIs(
        self.rpc_result(body).get("isError"),
        True,
        msg=f"A call to an unknown tool must not succeed: {body}",
      )


if __name__ == "__main__":
  absltest.main()
