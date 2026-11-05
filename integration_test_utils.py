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

"""Compatibility facade for tests written before the tiered layout.

Deprecated:
  The shopping helpers live in `shopping.base`; the vertical-agnostic pieces
  live in `framework`. This module re-exports both under the names tests used
  to import from here and will be removed in a future release.
"""

import uuid
import warnings
from framework.agent_profile_server import AgentProfileServer
from framework.base_test import (
  BaseIntegrationTest,
  DEFAULT_UCP_VERSION,
  flag_value,
  FLAGS,
)
from framework.mock_webhook_server import MockWebhookServer
from shopping.base import (
  AddressFixture,
  ConfiguredFixtureContext,
  CustomerFixture,
  DEFAULT_CONFORMANCE_INPUT,
  DEFAULT_FIXTURE_CONFIG,
  DEFAULT_REQUIRED_CAPABILITIES,
  DEFAULT_SHOPPING_FIXTURES,
  DynamicFixtureContext,
  DynamicFulfillmentCaseFixture,
  DynamicFulfillmentFixture,
  get_valid_payment_payload,
  shopping_test_data,
  ShoppingIntegrationTestBase,
  ShoppingTestData,
  UnifiedUpdate,
)

warnings.warn(
  "integration_test_utils is deprecated and will be removed in a future"
  " release. Use framework.base_test and shopping.base instead.",
  DeprecationWarning,
  stacklevel=2,
)

__all__ = [
  "AddressFixture",
  "AgentProfileServer",
  "BaseIntegrationTest",
  "ConfiguredFixtureContext",
  "CustomerFixture",
  "DEFAULT_CONFORMANCE_INPUT",
  "DEFAULT_FIXTURE_CONFIG",
  "DEFAULT_REQUIRED_CAPABILITIES",
  "DEFAULT_SHOPPING_FIXTURES",
  "DEFAULT_UCP_VERSION",
  "DynamicFixtureContext",
  "DynamicFulfillmentCaseFixture",
  "DynamicFulfillmentFixture",
  "FLAGS",
  "IntegrationTestBase",
  "MockWebhookServer",
  "ShoppingIntegrationTestBase",
  "ShoppingTestData",
  "TestData",
  "UnifiedUpdate",
  "flag_value",
  "get_headers",
  "get_valid_payment_payload",
  "shopping_test_data",
  "test_data",
]

# Names the test modules use from before the framework split.
IntegrationTestBase = ShoppingIntegrationTestBase
TestData = ShoppingTestData
test_data = shopping_test_data


def get_headers(
  idempotency_key: str | None = None, request_id: str | None = None
) -> dict[str, str]:
  """Generate headers for UCP requests (same set as the base class)."""
  port = FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285
  profile_url = f"http://localhost:{port}{AgentProfileServer.PROFILE_PATH}"
  return {
    "UCP-Agent": f'profile="{profile_url}"',
    "request-signature": "test",
    "idempotency-key": idempotency_key or str(uuid.uuid4()),
    "request-id": request_id or str(uuid.uuid4()),
    "Content-Type": "application/json",
  }
