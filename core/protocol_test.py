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

"""Generic protocol and discovery compliance tests for UCP servers."""

import re
from absl.testing import absltest
from framework.base_test import BaseIntegrationTest
from ucp_sdk.models.schemas.ucp import BusinessSchema

REVERSE_DNS_REGEX = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
VERSION_DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ProtocolTest(BaseIntegrationTest):
  """Tests for generic UCP protocol and discovery compliance.

  Validated Paths:
  - GET /.well-known/ucp
  """

  def test_discovery_schema_and_types(self):
    """Test GET /.well-known/ucp returns 200 and conforms to UCP schema."""
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    data = response.json()

    # Discovery profile may be at root or under 'ucp'
    ucp_data = data.get("ucp", data)
    self.assertIn(
      "version", ucp_data, "Discovery profile must contain 'version'"
    )

    # Validate against UCP SDK BusinessSchema model
    BusinessSchema(**ucp_data)

    # Validate UCP root version format
    version_val = ucp_data.get("version")
    version_str = (
      version_val.get("root", "")
      if isinstance(version_val, dict)
      else str(version_val)
    )
    self.assertRegex(
      version_str,
      VERSION_DATE_REGEX,
      f"UCP root version '{version_str}' is not in YYYY-MM-DD format.",
    )

    # Validate capability reverse-DNS naming and version formats
    caps_data = ucp_data.get("capabilities", {})
    if isinstance(caps_data, dict):
      for cap_name, cap_list in caps_data.items():
        self.assertRegex(
          cap_name,
          REVERSE_DNS_REGEX,
          f"Capability '{cap_name}' does not follow reverse-DNS naming.",
        )
        items = cap_list if isinstance(cap_list, list) else [cap_list]
        for cap in items:
          if isinstance(cap, dict) and "version" in cap:
            ver = cap["version"]
            ver_str = ver.get("root", "") if isinstance(ver, dict) else str(ver)
            self.assertRegex(
              ver_str,
              VERSION_DATE_REGEX,
              f"Capability '{cap_name}' version '{ver_str}' not YYYY-MM-DD.",
            )
    elif isinstance(caps_data, list):
      for cap in caps_data:
        if isinstance(cap, dict) and "name" in cap:
          self.assertRegex(
            cap["name"],
            REVERSE_DNS_REGEX,
            f"Capability '{cap['name']}' does not follow reverse-DNS naming.",
          )
          if "version" in cap:
            ver = cap["version"]
            ver_str = ver.get("root", "") if isinstance(ver, dict) else str(ver)
            self.assertRegex(
              ver_str,
              VERSION_DATE_REGEX,
              f"Capability '{cap['name']}' version '{ver_str}' not YYYY-MM-DD.",
            )

    # Validate payment handlers reverse-DNS naming if present
    payment_handlers = ucp_data.get("payment_handlers", {})
    if isinstance(payment_handlers, dict):
      for group_name, handlers in payment_handlers.items():
        self.assertRegex(
          group_name,
          REVERSE_DNS_REGEX,
          f"Payment handler group '{group_name}' not in reverse-DNS format.",
        )
        items = handlers if isinstance(handlers, list) else [handlers]
        for handler in items:
          if isinstance(handler, dict):
            self.assertTrue(handler.get("id"), "Payment handler missing 'id'")

  def test_version_negotiation(self):
    """Test protocol version negotiation via UCP-Agent header."""
    headers = self.get_headers()

    # 1. Compatible version request to discovery
    headers["UCP-Agent"] = 'profile="..."; version="2026-04-08"'
    resp = self.client.get("/.well-known/ucp", headers=headers)
    self.assert_response_status(resp, 200)

    # 2. Future / incompatible version request
    headers["UCP-Agent"] = 'profile="..."; version="2099-01-01"'
    resp = self.client.get("/.well-known/ucp", headers=headers)
    # Discovery must still succeed or respond with structured error
    self.assertIn(resp.status_code, [200, 400])


if __name__ == "__main__":
  absltest.main()
