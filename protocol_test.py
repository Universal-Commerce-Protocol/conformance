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
from ucp_sdk.models.schemas.ucp import BusinessSchema, ReverseDomainName
from ucp_sdk.models.schemas.shopping import checkout
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)

# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class ProtocolTest(integration_test_utils.IntegrationTestBase):
  """Tests for UCP protocol compliance.

  Validated Paths:
  - GET /.well-known/ucp
  - POST /checkout-sessions
  """

  def _extract_document_urls(
    self, profile: BusinessSchema
  ) -> list[tuple[str, str]]:
    """Extract all spec and schema URLs from the discovery profile.

    Returns:
      A list of (JSON path, URL) tuples.

    """
    urls = set()

    # 1. Services (dict[ReverseDomainName, list[ServiceBinding]])
    if profile.services:
      for service_name, service_list in profile.services.items():
        for idx, service_wrapper in enumerate(service_list):
          svc = service_wrapper.root
          base_path = f"ucp.services['{service_name.root}'][{idx}]"
          if svc.spec:
            urls.add((f"{base_path}.spec", str(svc.spec)))
          if svc.schema_:
            urls.add((f"{base_path}.schema", str(svc.schema_)))

    # 2. Capabilities (dict[ReverseDomainName, list[CapabilityBinding]])
    if profile.capabilities:
      for cap_name, cap_list in profile.capabilities.items():
        for idx, cap in enumerate(cap_list):
          base_path = f"ucp.capabilities['{cap_name.root}'][{idx}]"
          if cap.spec:
            urls.add((f"{base_path}.spec", str(cap.spec)))
          if cap.schema_:
            urls.add((f"{base_path}.schema", str(cap.schema_)))

    # 3. Payment Handlers (dict[ReverseDomainName, list[HandlerBinding]])
    if profile.payment_handlers:
      for handler_name, handler_list in profile.payment_handlers.items():
        for idx, handler in enumerate(handler_list):
          base_path = f"ucp.payment_handlers['{handler_name.root}'][{idx}]"
          if handler.spec:
            urls.add((f"{base_path}.spec", str(handler.spec)))
          if handler.schema_:
            urls.add((f"{base_path}.schema", str(handler.schema_)))

    return sorted(urls, key=lambda x: x[0])

  def test_discovery_urls(self):
    """Verify all spec and schema URLs in discovery profile are valid.

    Fetches each URL and verifies it returns 200 OK and valid HTML/JSON.
    """
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    data = response.json()
    profile = BusinessSchema(**data["ucp"])

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
          # Skip known-missing external mock handler specs
          if "mock_payment_handler" in url and "ucp.dev" in url:
            continue
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
    profile = BusinessSchema(**data["ucp"])

    self.assertEqual(
      profile.version.root,
      "2026-01-23",
      msg="Unexpected UCP version in discovery doc",
    )

    # Verify Capabilities (dict[ReverseDomainName, list[...]])
    capabilities = set()
    if profile.capabilities:
      for cap_name in profile.capabilities.keys():
        capabilities.add(cap_name.root)
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

    # Verify Payment Handlers (dict[ReverseDomainName, list[...]])
    handlers = set()
    if profile.payment_handlers:
      for handler_name, handler_list in profile.payment_handlers.items():
        for h in handler_list:
          if h.id:
            handlers.add(h.id)
    expected_handlers = {"google_pay", "mock_payment_handler", "shop_pay"}
    missing_handlers = expected_handlers - handlers
    self.assertFalse(
      missing_handlers,
      f"Missing expected payment handlers: {missing_handlers}",
    )

    # Specific check for Shop Pay config
    shop_pay = None
    if profile.payment_handlers:
      for handler_name, handler_list in profile.payment_handlers.items():
        for h in handler_list:
          if h.id == "shop_pay":
            shop_pay = h
            break
    self.assertIsNotNone(shop_pay, "Shop Pay handler not found")
    self.assertIn("shop_id", shop_pay.config)

    # Verify shopping service
    rdn = ReverseDomainName(root="dev.ucp.shopping")
    self.assertIn(rdn, profile.services)
    shopping_services = profile.services[rdn]
    rest_binding = next(
      (s for s in shopping_services if s.root.transport == "rest"), None
    )
    self.assertIsNotNone(rest_binding, "REST transport not found for shopping")
    self.assertEqual(rest_binding.root.version.root, "2026-01-23")
    self.assertIsNotNone(rest_binding.root.endpoint)

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
    discovery_data = discovery_resp.json()
    profile = BusinessSchema(**discovery_data["ucp"])
    rdn = ReverseDomainName(root="dev.ucp.shopping")
    shopping_services = profile.services[rdn]
    rest_binding = next(
      (s for s in shopping_services if s.root.transport == "rest"), None
    )
    self.assertIsNotNone(
      rest_binding, "REST transport not found for shopping service"
    )
    self.assertIsNotNone(
      rest_binding.root.endpoint,
      "Endpoint not found for shopping service",
    )
    checkout_sessions_url = (
      f"{str(rest_binding.root.endpoint).rstrip('/')}/checkout-sessions"
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
