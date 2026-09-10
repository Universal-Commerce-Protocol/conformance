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

"""Platform requirement profile evaluator."""

from dataclasses import dataclass
from pathlib import Path
from framework.discovery import UcpProfile
import yaml


@dataclass
class PlatformRequirement:
  """Requirement specification for a single capability."""

  name: str
  min_version: str | None = None
  required: bool = True


@dataclass
class PlatformProfile:
  """Certification profile containing mandated capabilities and handlers."""

  platform_name: str
  required_capabilities: list[PlatformRequirement]
  required_payment_handlers: list[str]

  @classmethod
  def from_yaml(cls, path: str | Path) -> "PlatformProfile":
    """Load a platform requirement profile from a YAML file."""
    with Path(path).open(encoding="utf-8") as f:
      data = yaml.safe_load(f) or {}

    caps = [
      PlatformRequirement(
        name=c["name"],
        min_version=c.get("min_version"),
        required=c.get("required", True),
      )
      for c in data.get("capabilities", [])
      if isinstance(c, dict) and "name" in c
    ]
    handlers = [str(h) for h in data.get("payment_handlers", [])]
    return cls(
      platform_name=data.get("platform", "unknown"),
      required_capabilities=caps,
      required_payment_handlers=handlers,
    )

  def validate(self, server_profile: UcpProfile) -> list[str]:
    """Validate server profile against platform requirements.

    Args:
      server_profile: The discovered server profile from /.well-known/ucp.

    Returns:
      A list of error strings describing any unmet requirements.

    """
    errors = []
    for req in self.required_capabilities:
      if req.required and not server_profile.supports_capability(
        req.name, req.min_version
      ):
        errors.append(
          f"Platform '{self.platform_name}' requires capability"
          f" '{req.name}' (min_version: {req.min_version or 'any'}), but it"
          " is not supported."
        )
    for handler in self.required_payment_handlers:
      if not server_profile.supports_payment_handler(handler):
        errors.append(
          f"Platform '{self.platform_name}' requires payment handler"
          f" '{handler}', but it is missing from /.well-known/ucp."
        )
    return errors
