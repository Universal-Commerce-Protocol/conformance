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
"""Base class for tests of the MCP binding of the shopping service."""

import json
from typing import Any
import uuid

from framework.base_test import agent_profile_url
from framework.base_test import BaseIntegrationTest
import httpx
from shopping.base import DEFAULT_CONFORMANCE_INPUT
from shopping.base import mcp_entry

MCP_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC's own "method not found" and "invalid params", and the codes UCP
# assigns to protocol and discovery errors.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PROTOCOL_ERROR = -32000
DISCOVERY_ERROR = -32001


class McpIntegrationTestBase(BaseIntegrationTest):
  """Base class for tests that call the shopping tools over MCP.

  Tests are skipped when the discovery profile lists no MCP entry for the
  shopping service. Every test opens an MCP session with `initialize` first.

  The class requires no capability of its own, so a server that offers only
  the cart or the catalog over MCP is still tested: each suite declares the
  capability its tools belong to.
  """

  default_conformance_input = DEFAULT_CONFORMANCE_INPUT

  def _verify_required_capabilities(self) -> None:
    """Skip, before any resource is created, without an MCP endpoint.

    BaseIntegrationTest calls this before it creates the HTTP client and
    starts the agent profile server, so a server without the MCP transport
    skips these tests without starting either.
    """
    super()._verify_required_capabilities()
    services = self.server_profile.services if self.server_profile else {}
    self._mcp_service = mcp_entry(services.get("dev.ucp.shopping", []))
    if not self._mcp_service:
      self.skipTest("Server does not advertise an MCP shopping service")

  def setUp(self) -> None:
    """Find the MCP endpoint and open a session on it."""
    super().setUp()
    entry = self._mcp_service
    self.assertTrue(
      entry.get("endpoint"), msg="The MCP service entry has no endpoint."
    )
    self.mcp_url = str(entry["endpoint"])
    self._rpc_id = 0
    self._session_id: str | None = None
    self._protocol_version: str | None = None
    self.last_response: httpx.Response | None = None

    response, body = self.rpc(
      "initialize",
      {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "ucp-conformance", "version": "1"},
      },
    )
    self.assert_response_status(response, 200)
    self.initialize_result = self.rpc_result(body)
    # Later requests carry the negotiated version and, if the server handed
    # one out, the session.
    self._protocol_version = self.initialize_result.get("protocolVersion")
    self._session_id = response.headers.get("mcp-session-id")
    self.client.post(
      self.mcp_url,
      json={"jsonrpc": "2.0", "method": "notifications/initialized"},
      headers=self._mcp_headers(),
    )

  def _mcp_headers(self) -> dict[str, str]:
    headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
    }
    if self._protocol_version:
      headers["MCP-Protocol-Version"] = self._protocol_version
    if self._session_id:
      headers["Mcp-Session-Id"] = self._session_id
    return headers

  def rpc(
    self, method: str, params: dict[str, Any] | None = None
  ) -> tuple[httpx.Response, dict[str, Any]]:
    """Send one JSON-RPC request and return the HTTP response and its body."""
    self._rpc_id += 1
    request: dict[str, Any] = {
      "jsonrpc": "2.0",
      "id": self._rpc_id,
      "method": method,
    }
    if params is not None:
      request["params"] = params
    response = self.client.post(
      self.mcp_url, json=request, headers=self._mcp_headers()
    )
    self.last_response = response
    body = self._rpc_body(response, self._rpc_id)
    self.assertEqual(body.get("jsonrpc"), "2.0", msg=response.text)
    self.assertEqual(body.get("id"), self._rpc_id, msg=response.text)
    return response, body

  def _rpc_body(self, response: httpx.Response, rpc_id: int) -> dict[str, Any]:
    """Return the JSON-RPC message answering `rpc_id`.

    MCP over HTTP lets a server answer with a single JSON document or with an
    event stream that carries it.
    """
    # Media types are case-insensitive (RFC 9110, section 8.3.1).
    content_type = response.headers.get("content-type", "")
    if content_type.strip().lower().startswith("text/event-stream"):
      messages = self._event_stream_messages(response.text)
    else:
      messages = [self._parse_json(response.text)]
    for message in messages:
      if isinstance(message, dict) and message.get("id") == rpc_id:
        return message
    self.fail(f"No JSON-RPC response for id {rpc_id} in: {response.text!r}")

  def _event_stream_messages(self, text: str) -> list[Any]:
    """Return the JSON payloads of the events in a server-sent event stream."""
    messages = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
      data = "\n".join(
        line[len("data:") :].strip()
        for line in event.split("\n")
        if line.startswith("data:")
      )
      if data:
        messages.append(self._parse_json(data))
    return messages

  def _parse_json(self, text: str) -> Any:
    try:
      return json.loads(text)
    except json.JSONDecodeError:
      return None

  def rpc_result(self, body: dict[str, Any]) -> dict[str, Any]:
    """Return the `result` of a JSON-RPC response that must not be an error."""
    self.assertNotIn("error", body, msg=f"Unexpected JSON-RPC error: {body}")
    self.assertIn("result", body)
    return body["result"]

  def assert_rpc_error(
    self, body: dict[str, Any], code: int, http_status: int
  ) -> None:
    """Assert a JSON-RPC error with the given code and HTTP status.

    The HTTP status is the primary signal for the type of a protocol error,
    so it has to match what REST answers for the same error.
    """
    self.assertNotIn("result", body, msg=f"Expected a JSON-RPC error: {body}")
    self.assertEqual(body.get("error", {}).get("code"), code, msg=str(body))
    self.assertEqual(
      self.last_response.status_code,
      http_status,
      msg=f"JSON-RPC error {code} must come with HTTP {http_status}.",
    )

  def assert_missing_agent_rejected(self, body: dict[str, Any]) -> None:
    """Assert a call whose meta has no `ucp-agent` was rejected.

    A missing profile URL is the discovery error `invalid_profile_url`
    (HTTP 400). The tool schema also marks `ucp-agent` as required, so a
    server that validates the arguments first may answer with JSON-RPC's
    "invalid params" before discovery runs; that answer has no HTTP status
    of its own in UCP.
    """
    self.assertNotIn("result", body, msg=f"Expected a JSON-RPC error: {body}")
    if body.get("error", {}).get("code") == INVALID_PARAMS:
      return
    self.assert_rpc_error(body, DISCOVERY_ERROR, http_status=400)

  def assert_tools_listed(self, expected: tuple[str, ...]) -> None:
    """Assert tools/list, over all its pages, includes every expected tool."""
    names: set[str] = set()
    params: dict[str, Any] | None = None
    while True:
      _, body = self.rpc("tools/list", params)
      page = self.rpc_result(body)
      names.update(tool.get("name") for tool in page.get("tools", []))
      if not page.get("nextCursor"):
        break
      params = {"cursor": page["nextCursor"]}
    missing = sorted(set(expected) - names)
    self.assertEqual(missing, [], msg=f"Tools not provided: {missing}")

  def request_meta(
    self,
    idempotency_key: str | None = None,
    include_idempotency_key: bool = True,
  ) -> dict[str, Any]:
    """Build the `meta` argument of a tool call."""
    meta: dict[str, Any] = {"ucp-agent": {"profile": agent_profile_url()}}
    if include_idempotency_key:
      meta["idempotency-key"] = idempotency_key or str(uuid.uuid4())
    return meta

  def call_tool(self, name: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool and return the JSON-RPC response body."""
    arguments.setdefault("meta", self.request_meta())
    _, body = self.rpc("tools/call", {"name": name, "arguments": arguments})
    return body

  def tool_payload(self, body: dict[str, Any]) -> dict[str, Any]:
    """Return the UCP payload a tool call answered with."""
    result = self.rpc_result(body)
    self.assertIn(
      "structuredContent",
      result,
      msg="A tool result must carry the UCP envelope in structuredContent.",
    )
    return result["structuredContent"]

  def assert_not_processed(self, body: dict[str, Any]) -> None:
    """Assert a call was turned away.

    A server may refuse with a JSON-RPC error, a tool result flagged
    `isError`, or a UCP error envelope.
    """
    if "error" in body:
      return
    result = self.rpc_result(body)
    if result.get("isError") is True:
      return
    status = result.get("structuredContent", {}).get("ucp", {}).get("status")
    self.assertEqual(
      status, "error", msg=f"The call should not have been processed: {body}"
    )
