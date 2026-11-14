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

"""AP2 Mandates helpers for the conformance tests.

Holds the per-release AP2 names, the UCP capability intersection, and a mock
agent profile server that declares the AP2 Mandates extension.
"""

import json
from typing import Any
from fastapi.responses import JSONResponse
from framework.agent_profile_server import AgentProfileServer


def ap2_release(ucp_version: str) -> dict[str, str] | None:
  """Return the AP2 Mandates names and URLs for a UCP release.

  The capability name, spec and schema paths, and the profile key field
  differ between releases, so only releases listed here are supported:
  2026-04-08 and 2026-08-25. Returns None for any other release.
  """
  base = f"https://ucp.dev/{ucp_version}"
  if ucp_version == "2026-04-08":
    return {
      "capability": "dev.ucp.shopping.ap2_mandate",
      "spec": f"{base}/specification/ap2-mandates",
      "schema": f"{base}/schemas/shopping/ap2_mandate.json",
      "checkout_spec": f"{base}/specification/checkout",
      "checkout_schema": f"{base}/schemas/shopping/checkout.json",
      "keys_field": "signing_keys",
    }
  if ucp_version == "2026-08-25":
    return {
      "capability": "dev.ucp.common.payment.ap2_mandate",
      "spec": f"{base}/specification/payment/extensions/ap2-mandates",
      "schema": f"{base}/schemas/common/payment_ap2_mandate.json",
      "checkout_spec": f"{base}/specification/shopping/checkout",
      "checkout_schema": f"{base}/schemas/shopping/checkout.json",
      "keys_field": "keys",
    }
  return None


def _capability_entries(profile: dict[str, Any]) -> dict[str, list[dict]]:
  """Return capability name to entries from a profile document."""
  ucp = profile.get("ucp", profile)
  capabilities = ucp.get("capabilities", {})
  if isinstance(capabilities, dict):
    return {
      name: [e for e in entries if isinstance(e, dict)]
      for name, entries in capabilities.items()
      if isinstance(entries, list)
    }
  result: dict[str, list[dict]] = {}
  for entry in capabilities if isinstance(capabilities, list) else []:
    if isinstance(entry, dict) and entry.get("name"):
      result.setdefault(entry["name"], []).append(entry)
  return result


def capability_intersection(
  business_profile: dict[str, Any], platform_profile: dict[str, Any]
) -> set[str]:
  """Compute the active capability set per the UCP intersection algorithm.

  Keeps business capabilities the platform also declares with at least one
  mutual version, then repeatedly prunes extensions none of whose parents
  remain.
  """
  business = _capability_entries(business_profile)
  platform = _capability_entries(platform_profile)
  parents: dict[str, list[str]] = {}
  for name, entries in business.items():
    if name not in platform:
      continue
    mutual = {e.get("version") for e in entries} & {
      e.get("version") for e in platform[name]
    }
    mutual.discard(None)
    if not mutual:
      continue
    selected = max(mutual)
    entry = next(e for e in entries if e.get("version") == selected)
    extends = entry.get("extends")
    if isinstance(extends, str):
      parents[name] = [extends]
    elif isinstance(extends, list):
      parents[name] = [p for p in extends if isinstance(p, str)]
    else:
      parents[name] = []
  changed = True
  while changed:
    changed = False
    for name in list(parents):
      if parents[name] and not any(p in parents for p in parents[name]):
        del parents[name]
        changed = True
  return set(parents)


class Ap2AgentProfileServer(AgentProfileServer):
  """The framework agent profile server plus an AP2 Mandates profile.

  AP2 Mandates is a shopping extension, so its profile route lives in the
  shopping layer rather than in the vertical-agnostic framework server.
  """

  # Same profile plus the AP2 Mandates extension. Served on its own path so
  # only tests that opt in negotiate AP2. A Business that supports AP2 must
  # reject every completion without a mandate once AP2 is negotiated, so
  # advertising it in the default profile would affect every other test.
  AP2_PROFILE_PATH = "/profiles/shopping-agent-ap2.json"
  # Public test key for the mock AP2 platform, which uses the trusted
  # platform provider flow. Only the public half exists: no test signs a
  # mandate with it yet.
  AP2_PLATFORM_KEY = {
    "kid": "conformance_ap2_platform",
    "kty": "EC",
    "crv": "P-256",
    "x": "oM8xPJdamTFt-HCkoA9pRul2eRsQ8OUY6eZE5L1ehXo",
    "y": "NnRLhGusN7MNYCrEGA9Hyh20Rda9SWDdzA-4yQg6ZWI",
    "use": "sig",
    "alg": "ES256",
  }

  def _setup_routes(self) -> None:
    super()._setup_routes()

    @self.app.get(self.AP2_PROFILE_PATH, response_model=None)
    async def get_ap2_profile() -> JSONResponse:
      """Return an agent profile that negotiates the AP2 Mandates extension.

      AP2 extends checkout, and the intersection algorithm prunes an
      extension whose parent is absent, so checkout is declared as well. In
      the trusted platform provider flow the platform also publishes at
      least one top-level signing key. Names and URLs follow the declared
      UCP release.
      """
      content = self._profile_template.replace(
        "{webhook_port}", str(self.webhook_port)
      ).replace("{ucp_version}", self.ucp_version)
      content_dict = json.loads(content)
      ap2 = ap2_release(self.ucp_version)
      if ap2 is None:
        return JSONResponse(content=content_dict)
      capabilities = content_dict["ucp"]["capabilities"]
      capabilities.setdefault(
        "dev.ucp.shopping.checkout",
        [
          {
            "version": self.ucp_version,
            "spec": ap2["checkout_spec"],
            "schema": ap2["checkout_schema"],
          }
        ],
      )
      capabilities[ap2["capability"]] = [
        {
          "version": self.ucp_version,
          "spec": ap2["spec"],
          "schema": ap2["schema"],
          "extends": "dev.ucp.shopping.checkout",
        }
      ]
      keys = content_dict.setdefault(ap2["keys_field"], [])
      if not any(
        k.get("kid") == self.AP2_PLATFORM_KEY["kid"]
        for k in keys
        if isinstance(k, dict)
      ):
        keys.append(self.AP2_PLATFORM_KEY)
      return JSONResponse(content=content_dict)
