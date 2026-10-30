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

"""Discovery and capability inspection for UCP servers."""

from dataclasses import dataclass
from typing import Any
import httpx


@dataclass(frozen=True)
class Capability:
  """Represents a discovered capability."""

  name: str
  version: str
  spec: str | None = None
  schema_url: str | None = None


class UcpProfile:
  """Represents an advertised server profile from /.well-known/ucp."""

  def __init__(self, raw_data: dict[str, Any]):
    """Initialize UcpProfile from raw discovery JSON."""
    self.raw = raw_data
    ucp_section = (
      raw_data.get("ucp") if isinstance(raw_data.get("ucp"), dict) else raw_data
    )

    # Extract version
    version_val = ucp_section.get("version", "")
    if isinstance(version_val, dict):
      self.ucp_version: str = str(version_val.get("root", ""))
    else:
      self.ucp_version: str = str(version_val)

    # Extract capabilities
    self.capabilities: dict[str, Capability] = {}
    caps_data = ucp_section.get("capabilities", {})

    if isinstance(caps_data, list):
      for cap in caps_data:
        if isinstance(cap, dict):
          name = cap.get("name")
          if name:
            ver = cap.get("version", "")
            ver_str = ver.get("root", "") if isinstance(ver, dict) else str(ver)
            self.capabilities[name] = Capability(
              name=name,
              version=ver_str,
              spec=cap.get("spec"),
              schema_url=cap.get("schema"),
            )
    elif isinstance(caps_data, dict):
      for name, cap_list in caps_data.items():
        items = cap_list if isinstance(cap_list, list) else [cap_list]
        for cap in items:
          if isinstance(cap, dict):
            ver = cap.get("version", "")
            ver_str = ver.get("root", "") if isinstance(ver, dict) else str(ver)
            self.capabilities[name] = Capability(
              name=name,
              version=ver_str,
              spec=cap.get("spec"),
              schema_url=cap.get("schema"),
            )

    # Extract payment handlers
    self.payment_handlers: set[str] = set()

    handlers_data = (
      ucp_section.get("payment_handlers")
      or ucp_section.get("payment", {}).get("handlers")
      or raw_data.get("payment_handlers")
      or raw_data.get("payment", {}).get("handlers", {})
    )

    if isinstance(handlers_data, list):
      for h in handlers_data:
        if isinstance(h, dict):
          if h.get("id"):
            self.payment_handlers.add(str(h["id"]))
          if h.get("name"):
            self.payment_handlers.add(str(h["name"]))
    elif isinstance(handlers_data, dict):
      for key, h_list in handlers_data.items():
        self.payment_handlers.add(str(key))
        items = h_list if isinstance(h_list, list) else [h_list]
        for h in items:
          if isinstance(h, dict):
            if h.get("id"):
              self.payment_handlers.add(str(h["id"]))
            if h.get("name"):
              self.payment_handlers.add(str(h["name"]))

    # Extract services
    self.services: dict[str, list[dict[str, Any]]] = {}
    svcs = ucp_section.get("services", {})
    if isinstance(svcs, dict):
      for svc_name, svc_items in svcs.items():
        self.services[svc_name] = (
          svc_items if isinstance(svc_items, list) else [svc_items]
        )

  def supports_capability(
    self, capability_name: str, min_version: str | None = None
  ) -> bool:
    """Check if the server advertises a capability.

    Optionally checks version requirement.
    """
    if capability_name not in self.capabilities:
      return False
    return not (
      min_version and self.capabilities[capability_name].version < min_version
    )

  def supports_payment_handler(self, handler_id: str) -> bool:
    """Check if the server supports a payment handler by ID or name."""
    return handler_id in self.payment_handlers


def fetch_server_profile(base_url: str, timeout: float = 10.0) -> UcpProfile:
  """Fetch and parse the discovery profile from the target server."""
  endpoint = f"{base_url.rstrip('/')}/.well-known/ucp"
  with httpx.Client(timeout=timeout) as client:
    response = client.get(endpoint)
    response.raise_for_status()
    return UcpProfile(response.json())
