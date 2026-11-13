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

"""Profile lookups for suites that call a REST binding."""

from typing import Any
from framework.discovery import UcpProfile

IDENTITY_LINKING = "dev.ucp.common.identity_linking"


def rest_endpoint(profile: UcpProfile, service: str) -> str | None:
  """Return the REST endpoint a profile lists for a service, if any.

  A profile lists a service once per transport, and the REST entry need not
  come first. The endpoint is returned exactly as listed, because some
  checks compare it byte for byte.
  """
  for entry in profile.services.get(service, []):
    if (
      isinstance(entry, dict)
      and entry.get("transport") == "rest"
      and isinstance(entry.get("endpoint"), str)
    ):
      return entry["endpoint"]
  return None


def capability_config(profile: UcpProfile, name: str) -> dict[str, Any]:
  """Return the `config` of an advertised capability, or an empty dict.

  Profiles list capabilities either as a map of name to entries or as a
  list of entries that carry a `name`; both shapes are read.
  """
  ucp = profile.raw.get("ucp", profile.raw)
  capabilities = ucp.get("capabilities") if isinstance(ucp, dict) else None
  if isinstance(capabilities, dict):
    entries = capabilities.get(name) or []
  elif isinstance(capabilities, list):
    entries = [
      c for c in capabilities if isinstance(c, dict) and c.get("name") == name
    ]
  else:
    entries = []
  for entry in entries if isinstance(entries, list) else [entries]:
    if isinstance(entry, dict) and isinstance(entry.get("config"), dict):
      return entry["config"]
  return {}


def gated_scopes(profile: UcpProfile) -> set[str]:
  """Return the scopes the business lists in identity linking `config.scopes`.

  Identity linking, Scopes: listing a scope declares that the corresponding
  operations require a user identity token.
  """
  scopes = capability_config(profile, IDENTITY_LINKING).get("scopes")
  return set(scopes) if isinstance(scopes, dict) else set()
