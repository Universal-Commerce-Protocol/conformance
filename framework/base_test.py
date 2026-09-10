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

"""Base integration test class decoupled from specific business verticals."""

import logging
import os
import uuid
from absl import flags
from absl.testing import absltest
from framework.agent_profile_server import AgentProfileServer
from framework.discovery import UcpProfile, fetch_server_profile
import httpx

FLAGS = flags.FLAGS
try:
  flags.DEFINE_string("server_url", None, "Base URL of the target UCP server")
  flags.DEFINE_string(
    "simulation_secret", str(uuid.uuid4()), "Secret for simulation endpoints"
  )
  flags.DEFINE_integer(
    "mock_webhook_port", 8284, "Port for the mock webhook server"
  )
  flags.DEFINE_integer(
    "mock_agent_port", 8285, "Port for the mock agent profile server"
  )
  flags.DEFINE_bool(
    "verbose_http", False, "Whether to log HTTP requests and responses"
  )
except flags.DuplicateFlagError:
  pass


class BaseIntegrationTest(absltest.TestCase):
  """Agnostic foundation for all UCP protocol and capability tests."""

  server_profile: UcpProfile | None = None

  def setUp(self) -> None:
    """Set up test environment, HTTP client, and mock agent server."""
    super().setUp()
    server_url = (
      FLAGS.server_url
      if FLAGS.is_parsed() and FLAGS.server_url
      else os.environ.get("FLAGS_server_url")  # noqa: SIM112
    )
    if not server_url:
      self.skipTest("Missing --server_url flag")

    self.base_url = server_url.rstrip("/")
    self.client = httpx.Client(base_url=self.base_url, timeout=15.0)

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(
      logging.INFO
      if (FLAGS.is_parsed() and FLAGS.verbose_http)
      else logging.WARNING
    )

    # Lazily cache server discovery profile across tests
    if BaseIntegrationTest.server_profile is None:
      try:
        BaseIntegrationTest.server_profile = fetch_server_profile(self.base_url)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning("Could not pre-fetch /.well-known/ucp: %s", e)

    # Capability filtering check from @requires_capability decorator
    self._verify_required_capabilities()

    self._start_agent_server()

  def _verify_required_capabilities(self) -> None:
    """Verify that the server supports capabilities required by class/method."""
    reqs = set()
    if hasattr(self, "_required_capabilities"):
      reqs.update(self._required_capabilities)
    test_method = getattr(self, self._testMethodName, None)
    if test_method and hasattr(test_method, "_required_capabilities"):
      reqs.update(test_method._required_capabilities)

    if self.server_profile is not None:
      for cap_name, min_ver in reqs:
        if not self.server_profile.supports_capability(cap_name, min_ver):
          self.skipTest(
            f"Server does not support required capability: '{cap_name}' "
            f"(min_version: {min_ver or 'any'})"
          )

  def _start_agent_server(self) -> None:
    agent_port = FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285
    webhook_port = FLAGS.mock_webhook_port if FLAGS.is_parsed() else 8284
    self.agent_server = AgentProfileServer(
      port=agent_port,
      webhook_port=webhook_port,
    )
    self.agent_server.start()

  def tearDown(self) -> None:
    """Tear down HTTP client and mock agent server."""
    self.client.close()
    if hasattr(self, "agent_server") and self.agent_server is not None:
      self.agent_server.stop()
    super().tearDown()

  def get_headers(
    self,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    custom_profile_url: str | None = None,
  ) -> dict[str, str]:
    """Generate standard UCP transport headers."""
    agent_port = FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285
    profile_url = (
      custom_profile_url
      or f"http://localhost:{agent_port}{AgentProfileServer.PROFILE_PATH}"
    )
    return {
      "UCP-Agent": f'profile="{profile_url}"',
      "request-signature": "test",
      "idempotency-key": idempotency_key or str(uuid.uuid4()),
      "request-id": request_id or str(uuid.uuid4()),
      "Content-Type": "application/json",
    }

  def assert_response_status(
    self, response: httpx.Response, expected_code: int | list[int]
  ) -> None:
    """Verify that an HTTP response has the expected status code."""
    expected = (
      [expected_code] if isinstance(expected_code, int) else expected_code
    )
    self.assertIn(
      response.status_code,
      expected,
      msg=(
        f"Expected status {expected}, got {response.status_code} for"
        f" {response.request.method} {response.request.url}. Response:"
        f" {response.text}"
      ),
    )
