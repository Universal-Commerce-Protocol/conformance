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

"""Generic protocol and discovery compliance tests for UCP servers.

Vertical-agnostic: everything asserted here is required of any UCP server
regardless of the business verticals it implements. Vertical-specific
discovery assertions (for example that a shopping service is advertised)
live with their vertical's suite.
"""

import unittest
from absl.testing import absltest
from framework.base_test import BaseIntegrationTest, DEFAULT_UCP_VERSION
import httpx
from pydantic import TypeAdapter, ValidationError
from ucp_sdk.models.schemas.ucp import BusinessSchema

try:
  from ucp_sdk.models.schemas.common.types.reverse_domain_name import (
    ReverseDomainName,
  )
except ImportError:
  from ucp_sdk.models.schemas.shopping.types.reverse_domain_name import (
    ReverseDomainName,
  )


class ProtocolTest(BaseIntegrationTest):
  """Tests for generic UCP protocol and discovery compliance.

  Validated Paths:
  - GET /.well-known/ucp
  """

  def _extract_document_urls(
    self, profile: BusinessSchema
  ) -> list[tuple[str, str]]:
    """Extract all spec and schema URLs from the discovery profile.

    Returns:
      A list of (JSON path, URL) tuples.

    """
    if isinstance(profile, dict):
      profile = profile.get("ucp", profile)
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
    Then the response should be 200 OK and include a well-formed version,
    capabilities, and payment handlers.
    """
    response = self.client.get("/.well-known/ucp")
    self.assert_response_status(response, 200)
    data = response.json()

    # profile.json requires the top-level `ucp` object.
    self.assertIn("ucp", data, "Discovery profile must be wrapped in 'ucp' key")
    ucp_data = data["ucp"]
    self.assertIn(
      "version", ucp_data, "Discovery profile must contain 'version'"
    )

    # Validate schema using SDK model
    BusinessSchema(**ucp_data)

    # Universal structural validation: UCP versions are date-based
    # (YYYY-MM-DD) regardless of which release the server implements.
    declared_version = ucp_data.get("version")
    version_str = (
      declared_version.get("root", "")
      if isinstance(declared_version, dict)
      else str(declared_version)
    )
    self.assertRegex(
      version_str,
      r"^\d{4}-\d{2}-\d{2}$",
      msg="UCP version in discovery doc must be date-based (YYYY-MM-DD)",
    )
    # Exact-version assertion is driven by conformance_input.json
    # ("ucp_version"), so the suite tests the release the merchant targets
    # instead of a hardcoded literal.
    expected_version = self.conformance_config.get(
      "ucp_version", DEFAULT_UCP_VERSION
    )
    self.assertEqual(
      version_str,
      expected_version,
      msg="Unexpected UCP version in discovery doc",
    )

    # Verify Capabilities — every capability group name must follow the
    # reverse-DNS convention (universal). Which capabilities a merchant
    # declares is negotiated per merchant (server-selects model), so any
    # expected roster comes from conformance_input.json
    # ("required_capabilities") rather than being fixed here.
    capabilities = set(ucp_data.get("capabilities", {}))
    for cap_name in capabilities:
      try:
        TypeAdapter(ReverseDomainName).validate_python(str(cap_name))
      except ValidationError as e:
        self.fail(
          f"Capability name '{cap_name}' does not follow reverse-DNS "
          f"convention: {e}"
        )
    expected_capabilities = set(
      self.conformance_config.get("required_capabilities", [])
    )
    missing_caps = expected_capabilities - capabilities
    self.assertFalse(
      missing_caps,
      f"Missing expected capabilities in discovery: {missing_caps}",
    )

    # Verify Payment Handlers - structural validation (server-agnostic)
    if ucp_data.get("payment_handlers"):
      handler_count = 0
      for handler_name, handler_list in ucp_data.get(
        "payment_handlers", {}
      ).items():
        # Validate handler group name using the SDK's ReverseDomainName
        # model, which enforces the pattern defined in the UCP spec.
        try:
          TypeAdapter(ReverseDomainName).validate_python(str(handler_name))
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

  # Version negotiation can only be observed on a request the server
  # validates; discovery is a public GET that returns 200 whatever UCP-Agent
  # version is sent. The check lives with the shopping checkout suite
  # (ShoppingProtocolTest.test_version_negotiation), which asserts the 422.


if __name__ == "__main__":
  absltest.main()
