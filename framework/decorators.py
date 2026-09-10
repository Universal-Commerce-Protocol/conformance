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

"""Decorators for capability filtering and specification traceability."""

from collections.abc import Callable
import functools
from typing import TypeVar

T = TypeVar("T")


def requires_capability(
  capability_name: str, min_version: str | None = None
) -> Callable[[T], T]:
  """Mark a test class or test method as requiring a specific UCP capability.

  If the server under test does not advertise the capability, skip the test.

  Args:
    capability_name: The name of the capability.
    min_version: Optional minimum version string.

  Returns:
    The decorated class or method.

  """

  def decorator(obj: T) -> T:
    if not hasattr(obj, "_required_capabilities"):
      obj._required_capabilities = set()
    obj._required_capabilities.add((capability_name, min_version))
    return obj

  return decorator


def spec_assert(section: str, requirement_id: str) -> Callable:
  """Tag a test method with a specific clause from the UCP specification.

  Used for automated compliance matrix generation and test auditing.

  Args:
    section: The specification section identifier.
    requirement_id: The specific requirement ID.

  Returns:
    The decorated test method.

  """

  def decorator(func: Callable) -> Callable:
    func._spec_section = section
    func._spec_requirement_id = requirement_id

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
      return func(*args, **kwargs)

    return wrapper

  return decorator
