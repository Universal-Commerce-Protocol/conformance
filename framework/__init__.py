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

"""UCP Conformance Framework: Core test runtime and SDK-agnostic base."""

from framework.agent_profile_server import AgentProfileServer
from framework.base_test import BaseIntegrationTest
from framework.decorators import requires_capability, spec_assert
from framework.discovery import Capability, UcpProfile, fetch_server_profile
from framework.mock_webhook_server import MockWebhookServer
from framework.platform import PlatformProfile, PlatformRequirement

__all__ = [
  "AgentProfileServer",
  "BaseIntegrationTest",
  "Capability",
  "MockWebhookServer",
  "PlatformProfile",
  "PlatformRequirement",
  "UcpProfile",
  "fetch_server_profile",
  "requires_capability",
  "spec_assert",
]
