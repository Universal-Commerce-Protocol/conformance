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

"""Shopping service discovery and version negotiation tests.

The vertical-agnostic half of protocol conformance lives in
`core/protocol_test.py`. This module covers the assertions that only apply
to a server advertising the shopping vertical.
"""

from absl.testing import absltest
from framework.base_test import DEFAULT_UCP_VERSION
from framework.decorators import requires_capability
from shopping.base import ShoppingIntegrationTestBase
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


@requires_capability("dev.ucp.shopping.checkout")
class ShoppingProtocolTest(ShoppingIntegrationTestBase):
  """Shopping service advertisement and version negotiation.

  Validated Paths:
  - GET /.well-known/ucp
  - POST /checkout-sessions
  """

  def _discovery_profile(self) -> dict:
    """Fetch and unwrap the discovery profile."""
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    profile = response.json()
    return profile.get("ucp", profile)

  def _shopping_service(self, ucp_data: dict) -> dict:
    """Return the advertised shopping service entry."""
    shopping_services = ucp_data.get("services", {}).get("dev.ucp.shopping")
    self.assertIsNotNone(
      shopping_services, "Shopping service not found in discovery"
    )
    return (
      shopping_services[0]
      if isinstance(shopping_services, list)
      else shopping_services
    )

  def test_shopping_service_advertisement(self):
    """Test that the shopping service is advertised correctly.

    Given the UCP server implements the shopping vertical,
    When the discovery profile is fetched,
    Then a REST shopping service must be advertised with a date-based
    version matching the profile version and a resolvable endpoint.
    """
    ucp_data = self._discovery_profile()
    shopping_service = self._shopping_service(ucp_data)

    declared_version = ucp_data.get("version")
    expected_version = (
      declared_version.get("root", "")
      if isinstance(declared_version, dict)
      else str(declared_version)
    )
    if not expected_version:
      expected_version = self.conformance_config.get(
        "ucp_version", DEFAULT_UCP_VERSION
      )

    # The shopping service entry's version follows the same input-driven
    # rule as the profile version.
    self.assertRegex(
      str(shopping_service.get("version")),
      r"^\d{4}-\d{2}-\d{2}$",
      msg="Shopping service version must be date-based (YYYY-MM-DD)",
    )
    self.assertEqual(shopping_service.get("version"), expected_version)
    self.assertEqual(
      shopping_service.get("transport"),
      "rest",
      "Shopping service must advertise the REST transport",
    )
    self.assertIsNotNone(shopping_service.get("endpoint"))

  def test_version_negotiation(self):
    """Test protocol version negotiation via headers.

    Given a checkout creation request,
    When the request includes a 'UCP-Agent' header with a compatible version,
    then the request succeeds (200/201).
    When the request includes a 'UCP-Agent' header with an incompatible
    version, then the request fails with 422 Unprocessable Content.
    """
    ucp_data = self._discovery_profile()
    shopping_service = self._shopping_service(ucp_data)
    self.assertEqual(
      shopping_service.get("transport"),
      "rest",
      "Shopping service must advertise the REST transport",
    )
    self.assertIsNotNone(
      shopping_service.get("endpoint"),
      "Endpoint not found for shopping service",
    )
    checkout_sessions_url = (
      f"{str(shopping_service.get('endpoint')).rstrip('/')}/checkout-sessions"
    )

    create_payload = self.create_checkout_payload()

    # 1. Compatible Version — use the version the server itself advertises in
    # discovery, so the test stays correct across spec releases instead of
    # pinning a literal that goes stale.
    advertised = ucp_data.get("version")
    advertised_version = (
      advertised.get("root", "")
      if isinstance(advertised, dict)
      else str(advertised)
    )
    self.assertTrue(
      advertised_version, "Discovery profile must advertise a UCP version"
    )
    headers = self.get_headers()
    headers["UCP-Agent"] = f'profile="..."; version="{advertised_version}"'
    response = self.client.post(
      checkout_sessions_url,
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response, [200, 201])

    # 2. Incompatible Version
    headers["UCP-Agent"] = 'profile="..."; version="2099-01-01"'
    response = self.client.post(
      checkout_sessions_url,
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=headers,
    )
    self.assert_response_status(response, 422)


if __name__ == "__main__":
  absltest.main()
