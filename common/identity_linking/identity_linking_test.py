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

"""Identity linking tests: authorization discovery and token challenges.

Each test checks a MUST of the identity linking specification
(specification/common/identity-linking in the 2026-08-25 release,
specification/identity-linking in 2026-04-08) or of the RFC it cites. The
tests read public metadata and send one request with a made-up token. They
never run an authorization flow or need an account.

The rules are the same in both releases, except the `oauth2` provider rule,
which is new in 2026-08-25 and applies only to a profile that lists such a
provider.
"""

import re
import uuid
from urllib.parse import urlsplit
from absl.testing import absltest
from framework.base_test import BaseIntegrationTest
from framework.decorators import requires_capability
from framework.services import (
  IDENTITY_LINKING,
  capability_config,
  gated_scopes,
  rest_endpoint,
)
import httpx

CATALOG_SEARCH = "dev.ucp.shopping.catalog.search"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# One auth-param of a challenge (RFC 9110 §11.2), with an optional comma.
AUTH_PARAM = re.compile(
  r'\s*,?\s*([A-Za-z0-9_.-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]+)'
)


def well_known_url(identifier: str, suffix: str) -> str:
  """Insert a well-known suffix between host and path (RFC 8414 §3.1).

  RFC 9728 §3.1 builds protected-resource metadata URLs the same way. A
  terminating "/" on the path is removed first.
  """
  parts = urlsplit(identifier)
  path = parts.path.rstrip("/")
  return f"{parts.scheme}://{parts.netloc}/.well-known/{suffix}{path}"


def bearer_params(header: str) -> dict[str, str] | None:
  """Return the parameters of the Bearer challenge in a header, if any.

  Parsing stops at the first token that is not a parameter, which starts
  the next challenge.
  """
  match = re.search(r"(?:^|,)\s*Bearer\b", header, re.IGNORECASE)
  if match is None:
    return None
  params, position = {}, match.end()
  while param := AUTH_PARAM.match(header, position):
    name, value = param.groups()
    if value.startswith('"'):
      value = re.sub(r"\\(.)", r"\1", value[1:-1])
    params.setdefault(name.lower(), value)
    position = param.end()
  return params


class MetadataUnavailableError(Exception):
  """A metadata URL answered with a server error or not at all."""


@requires_capability(IDENTITY_LINKING)
class IdentityLinkingTest(BaseIntegrationTest):
  """Authorization discovery and the challenge on a rejected token."""

  def setUp(self) -> None:
    """Read the business origin and the identity linking settings."""
    super().setUp()
    if self.server_profile is None:
      self.skipTest("Could not fetch the discovery profile")
    parts = urlsplit(self.base_url)
    self.business_origin = f"{parts.scheme}://{parts.netloc}"
    self.config = self.conformance_config.get("identity_linking") or {}

  def get_json(self, url: str) -> tuple[int, dict | None]:
    """GET a metadata document; return the status and the JSON object.

    Raises:
      MetadataUnavailableError: on a 5xx answer or a network error, so a broken
        metadata URL fails a test instead of reading as "not published".

    """
    try:
      response = self.client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
      raise MetadataUnavailableError(f"{url}: {exc}") from exc
    if response.status_code >= 500:
      raise MetadataUnavailableError(f"{url}: HTTP {response.status_code}")
    try:
      body = response.json()
    except ValueError:
      body = None
    return response.status_code, body if isinstance(body, dict) else None

  def fetch_json(self, url: str) -> tuple[int, dict | None]:
    """Like get_json, but fail the test when the URL is unavailable."""
    try:
      return self.get_json(url)
    except MetadataUnavailableError as exc:
      self.fail(f"Metadata URL unavailable: {exc}")

  def origin_forms(self) -> set[str]:
    """Return both spellings of the business domain as an identifier.

    The specification names "the business domain" without fixing whether
    the identifier ends in "/", so both spellings are accepted for it.
    """
    return {self.business_origin, self.business_origin + "/"}

  def protected_resource(self) -> tuple[set[str], dict] | None:
    """Find the business's protected-resource metadata (RFC 9728).

    RFC 9728 builds the metadata URL from the resource identifier. The
    test tries each REST endpoint the profile lists as the resource, then
    the business origin. Returns the accepted spellings of the resource
    identifier that answered, and its metadata.
    """
    candidates = [
      rest_endpoint(self.server_profile, service)
      for service in self.server_profile.services
    ]
    for resource in dict.fromkeys(c for c in candidates if c):
      status, body = self.fetch_json(
        well_known_url(resource, "oauth-protected-resource")
      )
      if 200 <= status < 300 and body is not None:
        return {resource}, body
    status, body = self.fetch_json(
      well_known_url(self.business_origin, "oauth-protected-resource")
    )
    if 200 <= status < 300 and body is not None:
      return self.origin_forms(), body
    return None

  def issuer_candidates(self) -> list[tuple[str, set[str]]]:
    """Return each issuer a platform would try, with its accepted spellings.

    Discovery, Step 1: the entries of `authorization_servers`, compared
    byte for byte, or the business domain when no protected-resource
    metadata is published. Skips when the metadata lists no server.
    """
    found = self.protected_resource()
    if found is None:
      return [(self.business_origin, self.origin_forms())]
    servers = found[1].get("authorization_servers")
    entries = [s for s in servers if isinstance(s, str)] if servers else []
    if not entries:
      self.skipTest("The protected-resource metadata lists no issuer")
    return [(s, {s}) for s in entries]

  def authorization_server_metadata(self, issuer: str) -> dict:
    """Fetch an issuer's metadata the way Discovery, Step 2 describes.

    RFC 8414 first. OpenID Connect discovery only when that answers 404.
    """
    url = well_known_url(issuer, "oauth-authorization-server")
    status, body = self.fetch_json(url)
    if status == 404:
      url = issuer.rstrip("/") + "/.well-known/openid-configuration"
      status, body = self.fetch_json(url)
    self.assertTrue(
      200 <= status < 300 and body is not None,
      msg=f"No authorization server metadata for {issuer}: {url} {status}",
    )
    return body

  def test_protected_resource_metadata_matches_its_resource(self) -> None:
    """Protected-resource metadata names the resource it was fetched for.

    RFC 9728 §3.3: the `resource` value MUST be identical to the resource
    identifier the metadata URL was built from. RFC 9728 §2:
    `authorization_servers`, when present, is an array of issuer
    identifiers.
    """
    found = self.protected_resource()
    if found is None:
      self.skipTest("The business publishes no protected-resource metadata")
    resources, metadata = found
    self.assertIn(metadata.get("resource"), resources, msg="`resource`")
    servers = metadata.get("authorization_servers")
    if servers is not None:
      self.assertIsInstance(servers, list, msg="`authorization_servers`")
      for server in servers:
        self.assertIsInstance(server, str, msg="`authorization_servers` entry")

  def test_authorization_server_metadata_issuer_matches(self) -> None:
    """Each issuer's metadata states that same issuer.

    RFC 8414 §3.3: the `issuer` value MUST be identical to the issuer
    identifier the metadata URL was built from. Identity linking repeats
    the rule in its Discovery section: no normalization, byte for byte.
    """
    for issuer, accepted in self.issuer_candidates():
      with self.subTest(issuer=issuer):
        metadata = self.authorization_server_metadata(issuer)
        self.assertIn(metadata.get("issuer"), accepted, msg="`issuer`")

  def test_authorization_server_metadata_declares_scopes_and_auth(
    self,
  ) -> None:
    """The metadata lists scopes and client authentication methods.

    For Businesses: the business MUST populate `scopes_supported`, and MUST
    declare its client authentication methods in
    `token_endpoint_auth_methods_supported` and enforce one of them. When it
    lists an `oauth2` identity provider, it MUST include the JWT bearer
    grant in `grant_types_supported` in its RFC 8414 metadata.
    """
    providers = capability_config(self.server_profile, IDENTITY_LINKING).get(
      "providers"
    )
    # `config.providers` maps each key to an array of provider entries.
    groups = providers.values() if isinstance(providers, dict) else [providers]
    needs_jwt_bearer = any(
      isinstance(p, dict) and p.get("type") == "oauth2"
      for group in groups
      for p in (group if isinstance(group, list) else [group])
    )
    grant_types = set()
    for issuer, _ in self.issuer_candidates():
      with self.subTest(issuer=issuer):
        metadata = self.authorization_server_metadata(issuer)
        for field in (
          "scopes_supported",
          "token_endpoint_auth_methods_supported",
        ):
          values = metadata.get(field)
          self.assertIsInstance(values, list, msg=f"`{field}`")
          self.assertTrue(values, msg=f"`{field}` is empty")
        grant_types.update(metadata.get("grant_types_supported") or [])
    if needs_jwt_bearer:
      self.assertIn(
        JWT_BEARER,
        grant_types,
        msg="No authorization server lists the JWT bearer grant",
      )

  def test_rejected_token_on_gated_operation_gets_bearer_challenge(
    self,
  ) -> None:
    """A gated operation answers a made-up token with the full challenge.

    Error Handling, `identity_required`: when an operation is gated by a
    scope in `config.scopes` and the token is invalid, the business MUST
    return 401, a `WWW-Authenticate: Bearer` challenge, and a message with
    code `identity_required`. The header MUST include `realm` set to the
    issuer declared in the RFC 8414 metadata, and MUST include
    `error="invalid_token"` because a token was sent.

    The gated operation is `identity_linking.request` from the conformance
    input (`path` on the shopping REST endpoint, and `body`), or a catalog
    search for `catalog.search_query` when `config.scopes` lists a
    `dev.ucp.shopping.catalog.search` scope. The test skips otherwise: the
    specification leaves to the business how an operation that no scope
    gates answers an invalid token.
    """
    endpoint = rest_endpoint(self.server_profile, "dev.ucp.shopping")
    if endpoint is None:
      self.skipTest("The profile lists no REST endpoint for dev.ucp.shopping")
    request = self.config.get("request")
    query = (self.conformance_config.get("catalog") or {}).get("search_query")
    search_gated = any(
      scope.startswith(f"{CATALOG_SEARCH}:")
      for scope in gated_scopes(self.server_profile)
    )
    if isinstance(request, dict) and request.get("path"):
      path, body = request["path"], request.get("body", {})
    elif query and search_gated:
      path, body = "/catalog/search", {"query": query}
    else:
      self.skipTest(
        "No gated operation: set identity_linking.request in the"
        " conformance input"
      )

    headers = self.get_headers()
    headers["Authorization"] = f"Bearer ucp-conformance-{uuid.uuid4().hex}"
    response = self.client.post(
      endpoint.rstrip("/") + path, json=body, headers=headers
    )
    self.assert_response_status(response, 401)
    try:
      reply = response.json()
    except ValueError:
      reply = None
    messages = reply.get("messages") if isinstance(reply, dict) else None
    codes = [m.get("code") for m in messages or [] if isinstance(m, dict)]
    self.assertIn("identity_required", codes, msg="No `identity_required`")

    params = bearer_params(response.headers.get("WWW-Authenticate", ""))
    self.assertIsNotNone(params, msg="401 without a Bearer challenge")
    self.assertIsNotNone(params.get("realm"), msg="Challenge has no `realm`")
    issuers = {
      self.authorization_server_metadata(issuer).get("issuer")
      for issuer, _ in self.issuer_candidates()
    } - {None}
    self.assertIn(params.get("realm"), issuers, msg="`realm`")
    self.assertEqual(params.get("error"), "invalid_token", msg="`error`")


if __name__ == "__main__":
  absltest.main()
