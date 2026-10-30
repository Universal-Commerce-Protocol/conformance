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

import contextlib
import json
import logging
import os
from pathlib import Path
import uuid
from absl import flags
from absl.testing import absltest
from framework.agent_profile_server import AgentProfileServer
from framework.constants import DEFAULT_UCP_VERSION
from framework.discovery import UcpProfile, fetch_server_profile
import httpx

# Re-exported so suites can keep importing the default release from the base
# test module they already depend on.
__all__ = [
  "DEFAULT_UCP_VERSION",
  "BaseIntegrationTest",
  "FLAGS",
  "flag_value",
]

FLAGS = flags.FLAGS


def _define_flag(definer, *args, **kwargs) -> None:
  """Define a flag, tolerating modules that already registered it.

  Each definition is guarded individually: sharing one try/except across a
  block would silently drop every flag after the first duplicate.
  """
  with contextlib.suppress(flags.DuplicateFlagError):
    definer(*args, **kwargs)


# `SERVER_URL` and `SIMULATION_SECRET` keep working as environment variables
# so `uv run pytest` can be configured without flags.
_define_flag(
  flags.DEFINE_string,
  "server_url",
  os.getenv("SERVER_URL"),
  "Base URL of the target UCP server",
)
_define_flag(
  flags.DEFINE_string,
  "simulation_secret",
  os.getenv("SIMULATION_SECRET", str(uuid.uuid4())),
  "Secret for simulation endpoints",
)
_define_flag(
  flags.DEFINE_integer,
  "mock_webhook_port",
  8284,
  "Port for the mock webhook server",
)
_define_flag(
  flags.DEFINE_integer,
  "mock_agent_port",
  8285,
  "Port for the mock agent profile server",
)
_define_flag(
  flags.DEFINE_bool,
  "verbose_http",
  False,
  "Whether to log HTTP requests and responses",
)
# Merchant-supplied inputs. They default to None so that a value given on the
# command line always wins; a vertical's base class supplies its bundled
# sample fixtures when nothing is given.
_define_flag(
  flags.DEFINE_string,
  "conformance_input",
  None,
  "Path to the conformance input configuration JSON describing the "
  "merchant under test.",
)
_define_flag(
  flags.DEFINE_string,
  "fixture_config",
  None,
  "Path to the test fixtures configuration JSON.",
)
_define_flag(
  flags.DEFINE_string,
  "test_data_dir",
  None,
  "Directory containing CSV test data.",
)


def flag_value(name: str):
  """Return a flag's value, or None when flags have not been parsed yet."""
  if not FLAGS.is_parsed():
    return None
  return getattr(FLAGS, name, None)


class BaseIntegrationTest(absltest.TestCase):
  """Agnostic foundation for all UCP protocol and capability tests."""

  server_profile: UcpProfile | None = None

  # Conformance input used when --conformance_input is not given. A vertical's
  # base class points this at its bundled sample fixture. It is resolved here,
  # before the agent profile server starts, so the agent advertises the
  # release the merchant declares.
  default_conformance_input: str | None = None

  def setUp(self) -> None:
    """Set up test environment, HTTP client, and mock agent server."""
    super().setUp()
    server_url = (
      FLAGS.server_url if FLAGS.is_parsed() else os.getenv("SERVER_URL")
    )
    if not server_url:
      self.skipTest("Missing --server_url flag (or SERVER_URL variable)")

    self.base_url = server_url.rstrip("/")
    self.client = httpx.Client(base_url=self.base_url, timeout=15.0)

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(
      logging.INFO if flag_value("verbose_http") else logging.WARNING
    )

    # Merchant-declared conformance input.
    self.conformance_config = self._load_conformance_config()

    # Lazily cache server discovery profile across tests
    if BaseIntegrationTest.server_profile is None:
      try:
        BaseIntegrationTest.server_profile = fetch_server_profile(self.base_url)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning("Could not pre-fetch /.well-known/ucp: %s", e)

    # Capability filtering check from @requires_capability decorator
    self._verify_required_capabilities()

    self._start_agent_server()

  @property
  def ucp_version(self) -> str:
    """The UCP release the merchant declares in its conformance input."""
    return self.conformance_config.get("ucp_version", DEFAULT_UCP_VERSION)

  def _load_conformance_config(self) -> dict:
    """Load the merchant's conformance input JSON, if one is configured."""
    config_path = (
      flag_value("conformance_input") or self.default_conformance_input
    )
    if not config_path:
      return {}
    path = Path(config_path)
    if not path.exists():
      logging.warning("Conformance input not found at %s", config_path)
      return {}
    with path.open(encoding="utf-8") as f:
      return json.load(f)

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
      ucp_version=self.ucp_version,
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
