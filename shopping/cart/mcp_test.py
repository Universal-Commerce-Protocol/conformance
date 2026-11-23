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
"""Tests for the cart tools of the MCP binding."""

from typing import Any
import uuid

from absl.testing import absltest
from framework.decorators import requires_capability
from shopping.base import DynamicFixtureContext
from shopping.mcp import McpIntegrationTestBase
from ucp_sdk.models.schemas.shopping import cart as cart

CART_TOOLS = ("create_cart", "get_cart", "update_cart", "cancel_cart")


@requires_capability("dev.ucp.shopping.cart")
class McpCartTest(McpIntegrationTestBase):
  """Tests for the cart tools served over the MCP transport.

  Validated Tools:
  - create_cart
  - get_cart
  - update_cart
  - cancel_cart
  """

  def cart_payload(
    self, quantity: int = 1, item_id: str | None = None
  ) -> dict[str, Any]:
    """Build the `cart` argument for one line item."""
    if item_id is None:
      item_id = DynamicFixtureContext(self.conformance_config).get_test_sku()
    return {"line_items": [{"item": {"id": item_id}, "quantity": quantity}]}

  def create_cart(self, **kwargs: Any) -> dict[str, Any]:
    """Create a cart through create_cart and return it."""
    return self.tool_payload(
      self.call_tool("create_cart", cart=self.cart_payload(**kwargs))
    )

  def assert_error_outcome(self, outcome: dict[str, Any]) -> None:
    """Assert a UCP payload reports an error in its messages."""
    errors = [
      m for m in outcome.get("messages", []) if m.get("type") == "error"
    ]
    self.assertTrue(
      errors, msg=f"Expected an error message in the envelope: {outcome}"
    )

  def test_lists_cart_tools(self) -> None:
    """Test that every core cart tool is provided.

    Given an MCP session,
    When tools/list is called,
    Then all four cart tools are listed.
    """
    self.assert_tools_listed(CART_TOOLS)

  def test_create_returns_cart(self) -> None:
    """Test that create_cart returns the cart in the UCP envelope.

    Given a valid cart payload,
    When create_cart is called,
    Then the result's structuredContent is a cart that carries its new id
    and the ucp metadata.
    """
    created = self.create_cart()

    cart.Cart(**created)
    self.assertTrue(created.get("id"))
    self.assertIn("version", created.get("ucp", {}))

  def test_get_uses_top_level_id(self) -> None:
    """Test that get_cart finds a cart by the top-level id.

    Given two carts created over MCP,
    When get_cart is called with the id of the first,
    Then the first cart is returned.
    """
    created = self.create_cart()
    self.create_cart()

    fetched = self.tool_payload(self.call_tool("get_cart", id=created["id"]))

    self.assertEqual(fetched.get("id"), created["id"])

  def test_update_changes_cart(self) -> None:
    """Test that update_cart applies a change.

    Given a cart with one unit of an item,
    When update_cart sets the quantity to two,
    Then the returned cart holds two units.
    """
    created = self.create_cart()

    # The binding names the cart by the top-level id, never in the body.
    updated = self.tool_payload(
      self.call_tool(
        "update_cart", id=created["id"], cart=self.cart_payload(quantity=2)
      )
    )

    self.assertEqual(updated["id"], created["id"])
    self.assertEqual(updated["line_items"][0]["quantity"], 2)

  def test_cancel_returns_cart(self) -> None:
    """Test that cancel_cart returns the cart it canceled.

    Given a cart created over MCP,
    When cancel_cart is called,
    Then the cart is returned as it was before deletion.
    """
    created = self.create_cart()

    canceled = self.tool_payload(
      self.call_tool("cancel_cart", id=created["id"])
    )

    self.assertEqual(canceled["id"], created["id"])
    self.assertEqual(
      [(li["item"]["id"], li["quantity"]) for li in canceled["line_items"]],
      [(li["item"]["id"], li["quantity"]) for li in created["line_items"]],
    )

  def test_ucp_agent_is_required(self) -> None:
    """Test that a call without meta["ucp-agent"] fails discovery.

    Given a create_cart call whose meta names no platform profile,
    When it is sent,
    Then the server answers with the discovery error, as REST does for a
    missing profile URL, or rejects the arguments as invalid.
    """
    body = self.call_tool(
      "create_cart",
      meta={"idempotency-key": str(uuid.uuid4())},
      cart=self.cart_payload(),
    )

    self.assert_missing_agent_rejected(body)

  def test_cancel_requires_idempotency_key(self) -> None:
    """Test that cancel_cart needs meta["idempotency-key"].

    Given a cart created over MCP,
    When cancel_cart is called without the key,
    Then it is not processed and the cart can still be read.
    """
    created = self.create_cart()

    self.assert_not_processed(
      self.call_tool(
        "cancel_cart",
        meta=self.request_meta(include_idempotency_key=False),
        id=created["id"],
      )
    )

    fetched = self.tool_payload(self.call_tool("get_cart", id=created["id"]))
    self.assertEqual(fetched.get("id"), created["id"])

  def test_cancel_is_idempotent(self) -> None:
    """Test that meta["idempotency-key"] makes cancel_cart retry safe.

    Given a cancel_cart call with an idempotency key,
    When the same call is repeated,
    Then the same cart is returned again, as the REST binding requires of
    a repeated request.
    """
    created = self.create_cart()
    meta = self.request_meta()

    first = self.tool_payload(
      self.call_tool("cancel_cart", meta=meta, id=created["id"])
    )
    replay = self.tool_payload(
      self.call_tool("cancel_cart", meta=meta, id=created["id"])
    )

    self.assertEqual(replay.get("id"), first["id"])

  def test_unknown_cart_is_a_result(self) -> None:
    """Test that a cart that does not exist is not a JSON-RPC error.

    Given an id no cart has,
    When get_cart is called with it,
    Then the response is a JSON-RPC result whose UCP envelope carries an
    error message.
    """
    outcome = self.tool_payload(
      self.call_tool("get_cart", id=f"no_such_cart_{uuid.uuid4()}")
    )

    self.assert_error_outcome(outcome)
    self.assertNotIn("line_items", outcome)

  def test_business_outcome_is_a_result(self) -> None:
    """Test that a cart with no available item is not a JSON-RPC error.

    Given a product that does not exist,
    When create_cart is called for it alone,
    Then the response is a JSON-RPC result that reports the problem in its
    messages, whether or not a cart was created.
    """
    missing_item = self.conformance_config.get(
      "non_existent_item", {"id": "non_existent_item_1"}
    )

    outcome = self.create_cart(item_id=missing_item["id"])

    self.assertTrue(
      outcome.get("messages"),
      msg=f"Expected a message about the unavailable item: {outcome}",
    )


if __name__ == "__main__":
  absltest.main()
