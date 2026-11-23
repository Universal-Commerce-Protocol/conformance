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

"""Tests for the MCP suites themselves, run against a stand-in server."""

import json
from pathlib import Path
from typing import Any
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework.agent_profile_server import AgentProfileServer
from framework.base_test import BaseIntegrationTest
from framework.discovery import UcpProfile
import httpx
from shopping.base import DEFAULT_CONFORMANCE_INPUT
from shopping.base import mcp_entry
from shopping.cart import mcp_test as cart_suite
from shopping.catalog import mcp_test as catalog_suite
from shopping.checkout import mcp_test as checkout_suite
from shopping import mcp

# The suites are reached through their modules: a test class imported by name
# would be collected by pytest as a test of this module.

CHECKOUT = "dev.ucp.shopping.checkout"
CART = "dev.ucp.shopping.cart"
SEARCH = "dev.ucp.shopping.catalog.search"
LOOKUP = "dev.ucp.shopping.catalog.lookup"

MCP_URL = "http://shop.example/mcp"


def profile(capabilities: list[str], transports: list[str]) -> UcpProfile:
  """Build the discovery profile of a server with these capabilities."""
  return UcpProfile(
    {
      "ucp": {
        "version": "2026-04-08",
        "capabilities": {
          name: [{"version": "2026-04-08"}] for name in capabilities
        },
        "services": {
          "dev.ucp.shopping": [
            {
              "transport": transport,
              "endpoint": f"http://shop.example/{transport}",
            }
            for transport in transports
          ]
        },
      }
    }
  )


class FakeMcpServer:
  """An MCP endpoint that lists tools on two pages and records tool calls."""

  def __init__(self, tools: list[str], event_stream: bool = False) -> None:
    """Serve `tools`, as JSON or as an event stream."""
    self.tools = tools
    self.event_stream = event_stream
    self.calls: list[dict[str, Any]] = []

  def __call__(self, request: httpx.Request) -> httpx.Response:
    """Answer one JSON-RPC message."""
    message = json.loads(request.content)
    if "id" not in message:
      return httpx.Response(202)
    method, params = message["method"], message.get("params") or {}
    if method == "initialize":
      result: dict[str, Any] = {"protocolVersion": params["protocolVersion"]}
    elif method == "tools/list":
      # One tool on the first page, so a client has to follow the cursor.
      first = not params.get("cursor")
      names = self.tools[:1] if first else self.tools[1:]
      result = {"tools": [{"name": name} for name in names]}
      if first and len(self.tools) > 1:
        result["nextCursor"] = "page-2"
    else:
      self.calls.append(params)
      result = {"structuredContent": {"ucp": {"version": "2026-04-08"}}}
    body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
    if self.event_stream:
      return httpx.Response(
        200,
        headers={"Content-Type": "Text/Event-Stream; charset=utf-8"},
        text=f"event: message\ndata: {body}\n\n",
      )
    return httpx.Response(
      200, headers={"Content-Type": "application/json"}, text=body
    )


def run_test(
  test: unittest.TestCase,
  server_profile: UcpProfile,
  server: FakeMcpServer | None = None,
) -> tuple[unittest.TestResult, mock.Mock]:
  """Run one conformance test against a stand-in server.

  Returns:
    The test's result and the mock that stood in for starting the agent
    profile server.

  """
  real_client = httpx.Client

  def client(**kwargs: Any) -> httpx.Client:
    return real_client(transport=httpx.MockTransport(server), **kwargs)

  result = unittest.TestResult()
  with (
    flagsaver.flagsaver(server_url="http://shop.example"),
    mock.patch.object(BaseIntegrationTest, "server_profile", server_profile),
    mock.patch.object(
      BaseIntegrationTest, "_start_agent_server"
    ) as start_agent_server,
    mock.patch.object(httpx, "Client", client),
  ):
    test.run(result)
  return result, start_agent_server


def problems(result: unittest.TestResult) -> list[str]:
  """Return the tracebacks of a result's failures and errors."""
  return [text for _, text in result.failures + result.errors]


class CapabilityGatingTest(unittest.TestCase):
  """Tests that each suite needs only the capability of its own tools."""

  def test_base_class_requires_no_capability(self):
    """Test that the MCP base class does not need checkout."""
    self.assertFalse(
      getattr(mcp.McpIntegrationTestBase, "_required_capabilities", ())
    )

  def test_each_suite_requires_its_own_capability(self):
    """Test the capabilities the three suites declare."""
    self.assertEqual(
      checkout_suite.McpCheckoutTest._required_capabilities, {(CHECKOUT, None)}
    )
    self.assertEqual(
      cart_suite.McpCartTest._required_capabilities, {(CART, None)}
    )
    self.assertFalse(
      getattr(catalog_suite.McpCatalogTest, "_required_capabilities", ())
    )

  def test_cart_only_server_is_tested(self):
    """Test that the cart suite runs on a server without checkout."""
    server = FakeMcpServer(
      ["create_cart", "get_cart", "update_cart", "cancel_cart"]
    )
    result, _ = run_test(
      cart_suite.McpCartTest("test_lists_cart_tools"),
      profile([CART], ["mcp"]),
      server,
    )
    self.assertEqual(problems(result), [])
    self.assertEqual(result.skipped, [])

  def test_catalog_only_server_is_tested(self):
    """Test that the catalog suite runs on a server without checkout."""
    server = FakeMcpServer(["lookup_catalog", "get_product"])
    result, _ = run_test(
      catalog_suite.McpCatalogTest("test_lists_lookup_tools"),
      profile([LOOKUP], ["mcp"]),
      server,
    )
    self.assertEqual(problems(result), [])
    self.assertEqual(result.skipped, [])

  def test_checkout_suite_skips_without_checkout(self):
    """Test that the checkout suite still needs the checkout capability."""
    result, _ = run_test(
      checkout_suite.McpCheckoutTest("test_lists_checkout_tools"),
      profile([CART], ["mcp"]),
      FakeMcpServer([]),
    )
    self.assertEqual(len(result.skipped), 1)
    self.assertIn(CHECKOUT, result.skipped[0][1])


class RestOnlyServerTest(unittest.TestCase):
  """Tests for a server that does not offer the MCP transport."""

  def test_skips_before_starting_the_agent_server(self):
    """Test that nothing is started for a test that is going to skip."""
    for test in (
      checkout_suite.McpCheckoutTest("test_lists_checkout_tools"),
      cart_suite.McpCartTest("test_lists_cart_tools"),
      catalog_suite.McpCatalogTest("test_lists_search_tool"),
    ):
      with self.subTest(suite=type(test).__name__):
        requests: list[httpx.Request] = []
        result, start_agent_server = run_test(
          test, profile([CHECKOUT, CART, SEARCH], ["rest"]), requests.append
        )
        self.assertEqual(problems(result), [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("MCP", result.skipped[0][1])
        start_agent_server.assert_not_called()
        self.assertEqual(requests, [])


class TransportTest(unittest.TestCase):
  """Tests for reading the server's answers."""

  def test_tools_list_follows_cursor(self):
    """Test that tools on a later page count, and missing ones fail."""
    server = FakeMcpServer(["search_catalog", "lookup_catalog"])
    result, _ = run_test(
      catalog_suite.McpCatalogTest("test_lists_lookup_tools"),
      profile([LOOKUP], ["mcp"]),
      server,
    )
    self.assertEqual(len(result.failures), 1)
    self.assertIn("get_product", result.failures[0][1])

  def test_event_stream_media_type_is_case_insensitive(self):
    """Test that `Text/Event-Stream` is read as an event stream."""
    server = FakeMcpServer(["search_catalog"], event_stream=True)
    result, _ = run_test(
      catalog_suite.McpCatalogTest("test_lists_search_tool"),
      profile([SEARCH], ["mcp"]),
      server,
    )
    self.assertEqual(problems(result), [])
    self.assertEqual(result.skipped, [])


class McpEntryTest(unittest.TestCase):
  """Tests for finding the MCP entry of the shopping service."""

  def test_finds_the_mcp_entry_among_transports(self):
    """Test that the entry with transport `mcp` is returned."""
    rest = {"transport": "rest", "endpoint": "http://shop.example/rest"}
    over_mcp = {"transport": "mcp", "endpoint": MCP_URL}
    self.assertEqual(mcp_entry([rest, over_mcp]), over_mcp)
    self.assertEqual(mcp_entry([over_mcp, rest]), over_mcp)

  def test_accepts_a_single_entry(self):
    """Test that a service given as one object, not a list, is read."""
    over_mcp = {"transport": "mcp", "endpoint": MCP_URL}
    self.assertEqual(mcp_entry(over_mcp), over_mcp)

  def test_returns_none_without_an_mcp_entry(self):
    """Test the answers for services that do not offer MCP."""
    rest = {"transport": "rest", "endpoint": "http://shop.example/rest"}
    for entries in ([], [rest], rest, None, ["mcp"], [None]):
      with self.subTest(entries=entries):
        self.assertIsNone(mcp_entry(entries))


class EventStreamTest(unittest.TestCase):
  """Tests for reading JSON-RPC messages out of an event stream."""

  def setUp(self):
    """Build a test instance to call the parser on."""
    super().setUp()
    self.test = cart_suite.McpCartTest("test_lists_cart_tools")

  def response(self, text: str) -> httpx.Response:
    """Build an event-stream response with this body."""
    return httpx.Response(
      200, headers={"Content-Type": "text/event-stream"}, text=text
    )

  def test_picks_the_message_with_the_request_id(self):
    """Test that notifications and other responses are passed over."""
    text = (
      ": keep-alive\n\n"
      'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
      'event: message\ndata: {"jsonrpc":"2.0","id":6,"result":{}}\n\n'
      'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":1}}\n\n'
    )
    body = self.test._rpc_body(self.response(text), 7)
    self.assertEqual(body["result"], {"ok": 1})

  def test_reads_crlf_and_multi_line_data(self):
    """Test CRLF line ends and a payload split over two data lines."""
    text = 'data: {"jsonrpc":"2.0",\r\ndata: "id":3,"result":{}}\r\n\r\n'
    self.assertEqual(self.test._rpc_body(self.response(text), 3)["id"], 3)

  def test_fails_without_an_answer(self):
    """Test that a stream with no response for the request fails."""
    for text in ("", "data: not json\n\n", 'data: {"id":1,"result":{}}\n\n'):
      with self.subTest(text=text), self.assertRaises(AssertionError):
        self.test._rpc_body(self.response(text), 2)

  def test_reads_a_plain_json_answer(self):
    """Test that a JSON document is read when it is not an event stream."""
    response = httpx.Response(
      200,
      headers={"Content-Type": "application/json"},
      text='{"jsonrpc":"2.0","id":4,"result":{}}',
    )
    self.assertEqual(self.test._rpc_body(response, 4)["id"], 4)


class CheckoutArgumentTest(unittest.TestCase):
  """Tests for the `checkout` argument of create_checkout."""

  def setUp(self):
    """Give a checkout test the configuration setUp would load."""
    super().setUp()
    self.test = checkout_suite.McpCheckoutTest("test_create_returns_checkout")
    self.test.conformance_config = json.loads(
      Path(DEFAULT_CONFORMANCE_INPUT).read_text(encoding="utf-8")
    )

  def test_has_no_id_or_business_fields(self):
    """Test that the argument leaves out what the business assigns."""
    argument = self.test.create_argument()
    for field in ("id", "status", "ucp", "totals", "links"):
      self.assertNotIn(field, argument)
    self.assertEqual(argument["line_items"][0]["quantity"], 1)
    self.assertIn("payment", argument)
    self.assertIn("fulfillment", argument)

  def test_carries_buyer(self):
    """Test that a buyer given to the helper reaches the argument."""
    buyer = checkout_suite.McpCheckoutTest.DEFAULT_BUYER
    self.assertEqual(self.test.create_argument(buyer=buyer)["buyer"], buyer)


class MissingAgentTest(unittest.TestCase):
  """Tests for the answers accepted when meta has no `ucp-agent`."""

  def setUp(self):
    """Build a test instance to call the assertion on."""
    super().setUp()
    self.test = cart_suite.McpCartTest("test_ucp_agent_is_required")

  def check(self, code: int, http_status: int) -> None:
    """Run the assertion on a JSON-RPC error with this code and status."""
    self.test.last_response = httpx.Response(http_status)
    self.test.assert_missing_agent_rejected(
      {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "no"}}
    )

  def test_accepts_discovery_error(self):
    """Test that the discovery error with HTTP 400 is accepted."""
    self.check(mcp.DISCOVERY_ERROR, 400)

  def test_accepts_invalid_params(self):
    """Test that "invalid params" is accepted, whatever the HTTP status."""
    self.check(mcp.INVALID_PARAMS, 200)
    self.check(mcp.INVALID_PARAMS, 400)

  def test_rejects_discovery_error_with_wrong_status(self):
    """Test that the discovery error must still come with HTTP 400."""
    with self.assertRaises(AssertionError):
      self.check(mcp.DISCOVERY_ERROR, 200)

  def test_rejects_a_result(self):
    """Test that a processed call is not accepted."""
    self.test.last_response = httpx.Response(200)
    with self.assertRaises(AssertionError):
      self.test.assert_missing_agent_rejected(
        {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {}}}
      )


class AgentProfileTest(unittest.TestCase):
  """Tests for the capabilities the test agent advertises."""

  def test_advertises_every_capability_the_mcp_suites_call(self):
    """Test that negotiation can succeed for cart and catalog tools."""
    capabilities = AgentProfileServer(
      port=0, webhook_port=8284
    ).render_profile()["ucp"]["capabilities"]
    for name in (CHECKOUT, CART, SEARCH, LOOKUP):
      self.assertIn(name, capabilities)
      self.assertEqual(capabilities[name][0]["name"], name)


if __name__ == "__main__":
  unittest.main()
