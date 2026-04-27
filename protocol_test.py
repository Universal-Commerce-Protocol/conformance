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
from pydantic import ValidationError
from ucp_sdk.models.schemas.ucp import BusinessSchema, ReverseDomainName
from ucp_sdk.models.schemas.shopping import checkout as checkout
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
    profile = profile
    urls = set()

    # 1. Services
    for service_name, services_list in profile.get("services", {}).items():
      for svc_idx, service in enumerate(
        services_list if isinstance(services_list, list) else [services_list]
      ):
        base_path = f"services['{service_name}'][{svc_idx}]"
        if service.get("spec"):
          urls.add((f"{base_path}.spec", str(service.get("spec"))))
        if service.get("transport") == "rest" and service.get("schema"):
          urls.add((f"{base_path}.schema", str(service.get("schema"))))
        if service.get("transport") == "mcp" and service.get("schema"):
          urls.add((f"{base_path}.schema", str(service.get("schema"))))
        if service.get("transport") == "embedded" and service.get("schema"):
          urls.add((f"{base_path}.schema", str(service.get("schema"))))

    # 2. Capabilities
    for _cap_key, caps in profile.get("capabilities", {}).items():
      for i, cap in enumerate(caps if isinstance(caps, list) else [caps]):
        cap_name = cap.get("name") or f"index_{i}"
        base_path = f"ucp.capabilities['{cap_name}']"
        if cap.get("spec"):
          urls.add((f"{base_path}.spec", str(cap.get("spec"))))
        if cap.get("schema"):
          urls.add((f"{base_path}.schema", str(cap.get("schema"))))

    # 3. Payment Handlers
    for domain, handlers in profile.get("payment_handlers", {}).items():
      for i, handler in enumerate(
        handlers if isinstance(handlers, list) else [handlers]
      ):
        handler_id = handler.get("id") or f"{domain}_index_{i}"
        base_path = f"payment_handlers['{handler_id}']"
        if handler.get("spec"):
          urls.add((f"{base_path}.spec", str(handler.get("spec"))))
        if handler.get("config_schema"):
          urls.add(
            (f"{base_path}.config_schema", str(handler.get("config_schema")))
          )
        if handler.get("instrument_schemas"):
          for j, s in enumerate(handler.get("instrument_schemas", [])):
            urls.add((f"{base_path}.instrument_schemas[{j}]", str(s)))

    return sorted(urls, key=lambda x: x[0])

  import unittest

  @unittest.skip("Schemas not yet published on remote ucp.dev domain")
  def test_discovery_urls(self):
    """Verify all spec and schema URLs in discovery profile are valid.

    Fetches each URL and verifies it returns 200 OK and valid HTML/JSON.
    """
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    profile = response.json()

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
    BusinessSchema(**data)

    self.assertEqual(
      data.get("version"),
      "2026-01-23",
      msg="Unexpected UCP version in discovery doc",
    )

    # Verify Capabilities
    capabilities = set(data.get("capabilities", {}))
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

    # Verify Payment Handlers - structural validation (server-agnostic)
    if data.get("payment_handlers"):
      handler_count = 0
      for handler_name, handler_list in data.get(
        "payment_handlers", {}
      ).items():
        # Validate handler group name using the SDK's ReverseDomainName
        # model, which enforces the pattern defined in the UCP spec.
        try:
          ReverseDomainName(root=str(handler_name))
        except ValidationError as e:
          self.fail(
            f"Payment handler group name '{handler_name}' "
            f"does not follow reverse-DNS convention: {e}"
          )
        for h in (
          handler_list if isinstance(handler_list, list) else [handler_list]
        ):
          handler_count += 1
          # Validate required fields are present and non-empty
          self.assertTrue(
            h.get("id"),
            "Payment handler missing 'id'",
          )
          self.assertTrue(
            h.get("version"),
            f"Payment handler '{h.get('id')}' missing 'version'",
          )
      self.assertGreater(
        handler_count,
        0,
        "payment_handlers is present but contains no handlers",
      )

    # Verify shopping capability
    shopping_services = data.get("services", {}).get("dev.ucp.shopping")
    self.assertIsNotNone(shopping_services, "Shopping service missing")
    shopping_service = (
      shopping_services[0]
      if isinstance(shopping_services, list)
      else shopping_services
    )
    self.assertEqual(shopping_service.get("version"), "2026-01-23")
    self.assertIsNotNone(shopping_service.get("transport") == "rest")
    self.assertIsNotNone(shopping_service.get("endpoint"))

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
    profile_dict = discovery_resp.json()
    shopping_services = profile_dict.get("services", {}).get("dev.ucp.shopping")
    self.assertIsNotNone(
      shopping_services, "Shopping service not found in discovery"
    )
    shopping_service = (
      shopping_services[0]
      if isinstance(shopping_services, list)
      else shopping_services
    )
    self.assertIsNotNone(
      (shopping_service.get("transport") == "rest"),
      "REST config not found for shopping service",
    )
    self.assertIsNotNone(
      shopping_service.get("endpoint"),
      "Endpoint not found for shopping service",
    )
    checkout_sessions_url = (
      f"{str(shopping_service.get('endpoint')).rstrip('/')}/checkout-sessions"
    )

    create_payload = self.create_checkout_payload()

    # 1. Compatible Version
    headers = integration_test_utils.get_headers()
    headers["UCP-Agent"] = 'profile="..."; version="2026-01-23"'
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
