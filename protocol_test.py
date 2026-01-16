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

"""Protocol tests for the UCP SDK Server."""

from absl.testing import absltest
import integration_test_utils
import httpx
from ucp_sdk.models.discovery.profile_schema import UcpDiscoveryProfile
from ucp_sdk.models.schemas.shopping import fulfillment_resp as checkout
from ucp_sdk.models.schemas.shopping.payment_resp import (
  PaymentResponse as Payment,
)

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"PaymentResponse": Payment})


class ProtocolTest(integration_test_utils.IntegrationTestBase):
  """Tests for UCP protocol compliance.

  Validated Paths:
  - GET /.well-known/ucp
  - POST /checkout-sessions
  """

  def _extract_document_urls(
    self, profile: UcpDiscoveryProfile
  ) -> list[tuple[str, str]]:
    """Extract all spec and schema URLs from the discovery profile.

    Returns:
      A list of (JSON path, URL) tuples.

    """
    urls = set()

    # 1. Services
    for service_name, service in profile.ucp.services.root.items():
      base_path = f"ucp.services['{service_name}']"
      if service.spec:
        urls.add((f"{base_path}.spec", str(service.spec)))
      if service.rest and service.rest.schema_:
        urls.add((f"{base_path}.rest.schema", str(service.rest.schema_)))
      if service.mcp and service.mcp.schema_:
        urls.add((f"{base_path}.mcp.schema", str(service.mcp.schema_)))
      if service.embedded and service.embedded.schema_:
        urls.add(
          (f"{base_path}.embedded.schema", str(service.embedded.schema_))
        )

    # 2. Capabilities
    for i, cap in enumerate(profile.ucp.capabilities):
      cap_name = cap.name or f"index_{i}"
      base_path = f"ucp.capabilities['{cap_name}']"
      if cap.spec:
        urls.add((f"{base_path}.spec", str(cap.spec)))
      if cap.schema_:
        urls.add((f"{base_path}.schema", str(cap.schema_)))

    # 3. Payment Handlers
    if profile.payment and profile.payment.handlers:
      for i, handler in enumerate(profile.payment.handlers):
        handler_id = handler.id or f"index_{i}"
        base_path = f"payment.handlers['{handler_id}']"
        if handler.spec:
          urls.add((f"{base_path}.spec", str(handler.spec)))
        if handler.config_schema:
          urls.add((f"{base_path}.config_schema", str(handler.config_schema)))
        if handler.instrument_schemas:
          for j, s in enumerate(handler.instrument_schemas):
            urls.add((f"{base_path}.instrument_schemas[{j}]", str(s)))

    return sorted(urls, key=lambda x: x[0])

  def test_discovery_urls(self):
    """Verify all spec and schema URLs in discovery profile are valid.

    Fetches each URL and verifies it returns 200 OK and valid HTML/JSON.
    """
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    profile = UcpDiscoveryProfile(**response.json())

    url_entries = self._extract_document_urls(profile)
    failures = []

    with httpx.Client(follow_redirects=True, timeout=10.0) as external_client:
      # Sort by path for consistent output
      for path, url in sorted(url_entries, key=lambda x: x[0]):
        # Use internal client for local URLs, external client otherwise
        client = (
          self.client if url.startswith(self.base_url) else external_client
        )

        try:
          # Handle relative URLs if any (AnyUrl should be absolute though)
          res = client.get(url)
          if res.status_code != 200:
            failures.append(f"[{path}] {url} returned status {res.status_code}")
            continue

          content_type = res.headers.get("content-type", "").lower()
          if "json" in content_type:
            try:
              res.json()
            except Exception as e:
              failures.append(f"[{path}] {url} (JSON) failed to parse: {e}")
          elif "html" in content_type:
            is_valid_html = (
              "<html" in res.text.lower() or "<!doctype" in res.text.lower()
            )
            if not is_valid_html:
              failures.append(
                f"[{path}] {url} (HTML) does not appear to be valid HTML"
              )
          elif not res.text.strip():
            failures.append(f"[{path}] {url} returned empty content")

        except Exception as e:
          failures.append(f"[{path}] {url} fetch failed: {e}")

    if failures:
      self.fail("\n".join(["Discovery URL validation failed:"] + failures))

  def test_discovery(self):
    """Test the UCP discovery endpoint.

    Given the UCP server is running,
    When a GET request is sent to /.well-known/ucp,
    Then the response should be 200 OK and include the expected version,
    capabilities, and payment handlers.
    """
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    data = response.json()

    # Validate schema using SDK model
    profile = UcpDiscoveryProfile(**data)

    self.assertEqual(
      profile.ucp.version.root,
      "2026-01-11",
      msg="Unexpected UCP version in discovery doc",
    )

    # Verify Capabilities
    capabilities = {c.name for c in profile.ucp.capabilities}
    expected_capabilities = {
      "dev.ucp.shopping.checkout",
      "dev.ucp.shopping.order",
      "dev.ucp.shopping.discount",
      "dev.ucp.shopping.fulfillment",
      "dev.ucp.shopping.buyer_consent",
    }
    missing_caps = expected_capabilities - capabilities
    self.assertFalse(
      missing_caps,
      f"Missing expected capabilities in discovery: {missing_caps}",
    )

    # Verify Payment Handlers
    handlers = {h.id for h in profile.payment.handlers}
    expected_handlers = {"google_pay", "mock_payment_handler", "shop_pay"}
    missing_handlers = expected_handlers - handlers
    self.assertFalse(
      missing_handlers,
      f"Missing expected payment handlers: {missing_handlers}",
    )

    # Specific check for Shop Pay config
    shop_pay = next(
      (h for h in profile.payment.handlers if h.id == "shop_pay"),
      None,
    )
    self.assertIsNotNone(shop_pay, "Shop Pay handler not found")
    self.assertEqual(shop_pay.name, "com.shopify.shop_pay")
    self.assertIn("shop_id", shop_pay.config)

    # Verify shopping capability
    self.assertIn("dev.ucp.shopping", profile.ucp.services.root)
    shopping_service = profile.ucp.services.root["dev.ucp.shopping"]
    self.assertEqual(shopping_service.version.root, "2026-01-11")
    self.assertIsNotNone(shopping_service.rest)
    self.assertIsNotNone(shopping_service.rest.endpoint)

  def test_version_negotiation(self):
    """Test protocol version negotiation via headers.

    Given a checkout creation request,
    When the request includes a 'UCP-Agent' header with a compatible version,
    then the request succeeds (200/201).
    When the request includes a 'UCP-Agent' header with an incompatible version,
    then the request fails with 400 Bad Request.
    """
    # Discover shopping service endpoint
    discovery_resp = self.client.get("/.well-known/ucp")
    self.assert_response_status(discovery_resp, 200)
    profile = UcpDiscoveryProfile(**discovery_resp.json())
    shopping_service = profile.ucp.services.root["dev.ucp.shopping"]
    self.assertIsNotNone(
      shopping_service, "Shopping service not found in discovery"
    )
    self.assertIsNotNone(
      shopping_service.rest, "REST config not found for shopping service"
    )
    self.assertIsNotNone(
      shopping_service.rest.endpoint,
      "Endpoint not found for shopping service",
    )
    checkout_sessions_url = (
      f"{str(shopping_service.rest.endpoint).rstrip('/')}/checkout-sessions"
    )

    create_payload = self.create_checkout_payload()

    # 1. Compatible Version
    headers = integration_test_utils.get_headers()
    headers["UCP-Agent"] = 'profile="..."; version="2026-01-11"'
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
    self.assert_response_status(response, 400)


if __name__ == "__main__":
  absltest.main()
