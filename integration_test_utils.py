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

"""Backward compatibility facade for legacy integration test utilities.

Deprecated:
  Use `framework.base_test.BaseIntegrationTest` for core/generic protocol tests
  and `shopping.base.ShoppingIntegrationTestBase` for retail shopping tests.
"""

import uuid
import warnings
from framework.agent_profile_server import AgentProfileServer
from framework.base_test import BaseIntegrationTest, FLAGS
from framework.mock_webhook_server import MockWebhookServer
from shopping.base import (
  DEFAULT_SHOPPING_FIXTURES,
  DynamicFixtureContext,
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

# Re-export aliases for backward compatibility
IntegrationTestBase = ShoppingIntegrationTestBase
TestData = ShoppingTestData
test_data = shopping_test_data
fixture_ctx: DynamicFixtureContext | None = None

DEFAULT_CONFORMANCE_INPUT = (
  f"{DEFAULT_SHOPPING_FIXTURES}/conformance_input.json"
)
DEFAULT_FIXTURE_CONFIG = f"{DEFAULT_SHOPPING_FIXTURES}/test_fixtures.json"


def get_headers(
  idempotency_key: str | None = None, request_id: str | None = None
) -> dict[str, str]:
  """Generate headers for UCP requests."""
  port = FLAGS.mock_agent_port if FLAGS.is_parsed() else 8285
  profile_url = f"http://localhost:{port}{AgentProfileServer.PROFILE_PATH}"
  headers = {
    "UCP-Agent": f'profile="{profile_url}"',
    "request-signature": "test",
    "request-id": request_id or str(uuid.uuid4()),
    "Content-Type": "application/json",
  }
  if idempotency_key:
    headers["idempotency-key"] = idempotency_key
  return headers


__all__ = [
  "AgentProfileServer",
  "BaseIntegrationTest",
  "DEFAULT_CONFORMANCE_INPUT",
  "DEFAULT_FIXTURE_CONFIG",
  "DynamicFixtureContext",
  "FLAGS",
  "IntegrationTestBase",
  "MockWebhookServer",
  "ShoppingIntegrationTestBase",
  "ShoppingTestData",
  "TestData",
  "UnifiedUpdate",
  "fixture_ctx",
  "get_headers",
  "get_valid_payment_payload",
  "test_data",
]
