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
import dataclasses
import inspect
import re
from typing import TypeVar
import unittest

from framework.requirements import ID_RE

T = TypeVar("T")

# Where a marker is kept on the test method it labels.
MARKER_ATTRIBUTE = "_spec_marker"
MARKER_KINDS = (
  "spec_assert",
  "no_rule",
  "scenario",
  "outside_catalog",
  "older_version",
)
# UCP versions are release dates.
VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def requires_capability(
  capability_name: str, min_version: str | None = None
) -> Callable[[T], T]:
  """Mark a test class or test method as requiring a specific UCP capability.

  If the server under test does not advertise the capability, skip the test.
  Requirements accumulate down the class hierarchy: a decorated subclass
  needs its own capability and everything its base classes declared.

  Args:
    capability_name: The name of the capability.
    min_version: Optional minimum version string.

  Returns:
    The decorated class or method.

  """

  def decorator(obj: T) -> T:
    # `getattr` sees requirements inherited from base classes; copy them so
    # the base class's own set is never mutated through a subclass.
    inherited = getattr(obj, "_required_capabilities", set())
    obj._required_capabilities = set(inherited) | {
      (capability_name, min_version)
    }
    return obj

  return decorator


@dataclasses.dataclass(frozen=True)
class SpecMarker:
  """What a test says about the specification rules it checks.

  Attributes:
    kind: One of `MARKER_KINDS`.
    rule_ids: For "spec_assert", the IDs of every rule the test cites, those
      it checks fully and those it checks only in part, in the order given.
    reason: For the other kinds, why the test cites no rule in the catalog.
    version: For "older_version", the specification release the test checks.
    gaps: For "spec_assert", the rules the test checks only in part, each
      with what the test leaves unchecked, in the order given.

  """

  kind: str
  rule_ids: tuple[str, ...] = ()
  reason: str = ""
  version: str = ""
  gaps: tuple[tuple[str, str], ...] = ()

  @property
  def partial_ids(self) -> frozenset[str]:
    """Return the IDs of the rules the test checks only in part."""
    return frozenset(rule_id for rule_id, _ in self.gaps)


@dataclasses.dataclass(frozen=True)
class PartialClaim:
  """A rule a test checks only in part; see `partly`.

  Attributes:
    rule_id: The rule's ID.
    gap: What the test leaves unchecked.

  """

  rule_id: str
  gap: str


def partly(rule_id: str, gap: str) -> PartialClaim:
  """Cite a rule the test checks only in part, saying what it leaves out.

  Use it inside `spec_assert`, next to the rules the test checks fully:

    @spec_assert(
      "REQ-SHOPPING-CHECKOUT-0134",  # exactly one "total" entry
      partly(
        "REQ-SHOPPING-CHECKOUT-0016",  # line total is price x quantity
        "whole-number quantities only, so scale and rounding aren't checked",
      ),
    )

  The report counts a rule as covered only when some test checks it fully;
  a rule cited only this way counts as checked in part, with its gap.

  Args:
    rule_id: The rule's ID.
    gap: What the test's assertions leave unchecked.

  Returns:
    The claim, for `spec_assert`.

  Raises:
    ValueError: If the gap is empty.

  """
  return PartialClaim(rule_id, _reason("partly", gap))


def spec_assert(*claims: str | PartialClaim) -> Callable[[T], T]:
  """Mark a test as checking one or more specification rules.

  Rules are cited by their stable ID in the requirements catalog, such as
  `REQ-SHOPPING-CHECKOUT-0133`. Cite the rules the test's assertions check,
  not every rule the test passes through on the way, and wrap a rule the
  test checks only in part in `partly`. Like the other markers, this only
  labels the test: it runs exactly as before. A test takes one marker.

  Args:
    *claims: The IDs of the rules the test checks fully, and `partly` claims
      for those it checks only in part.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If no rule is given, or an ID is malformed or given twice.

  """
  if not claims:
    raise ValueError("spec_assert needs at least one rule ID")
  rule_ids = tuple(
    claim.rule_id if isinstance(claim, PartialClaim) else claim
    for claim in claims
  )
  malformed = [
    rule_id
    for rule_id in rule_ids
    if not isinstance(rule_id, str) or not ID_RE.match(rule_id)
  ]
  if malformed:
    raise ValueError(f"spec_assert: malformed rule IDs {malformed}")
  repeated = sorted(
    {rule_id for rule_id in rule_ids if rule_ids.count(rule_id) > 1}
  )
  if repeated:
    raise ValueError(f"spec_assert: rule IDs given twice {repeated}")
  gaps = tuple(
    (claim.rule_id, claim.gap)
    for claim in claims
    if isinstance(claim, PartialClaim)
  )
  return _label(SpecMarker("spec_assert", rule_ids=rule_ids, gaps=gaps))


def no_rule(reason: str) -> Callable[[T], T]:
  """Mark a test whose behavior the spec describes without a keyword.

  For a test of behavior the specification describes, but not with an
  RFC 2119 keyword, so the catalog has no rule for it.

  Args:
    reason: Where and how the specification describes the behavior.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the reason is empty.

  """
  return _label(SpecMarker("no_rule", reason=_reason("no_rule", reason)))


def scenario(reason: str) -> Callable[[T], T]:
  """Mark a test that checks a store's own data rather than a spec rule.

  For a test built on one store's products or settings, such as a
  free-shipping threshold, whose expected outcome no rule decides.

  Args:
    reason: What the test checks instead of a rule.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the reason is empty.

  """
  return _label(SpecMarker("scenario", reason=_reason("scenario", reason)))


def outside_catalog(reason: str) -> Callable[[T], T]:
  """Mark a test that checks a rule this catalog does not contain.

  For a test of a rule stated on a specification page the catalog doesn't
  cover, such as the core overview page. Once the catalog covers that page,
  the test can cite the rule with `spec_assert`.

  Args:
    reason: Which page states the rule, and what the rule is.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the reason is empty.

  """
  return _label(
    SpecMarker("outside_catalog", reason=_reason("outside_catalog", reason))
  )


def older_version(version: str, reason: str) -> Callable[[T], T]:
  """Mark a test that checks an older version of the specification.

  For a test of behavior that the catalog's version of the specification
  has changed or dropped. Each such test is one to update.

  Args:
    version: The release the test checks, such as "2026-04-08".
    reason: What changed since that release.

  Returns:
    A decorator for a test method.

  Raises:
    ValueError: If the version is not a release date, or the reason is empty.

  """
  if not isinstance(version, str) or not VERSION_RE.match(version):
    raise ValueError(f"older_version: {version!r} is not a release date")
  return _label(
    SpecMarker(
      "older_version",
      reason=_reason("older_version", reason),
      version=version,
    )
  )


def spec_marker(
  test: Callable[..., object] | unittest.TestCase,
) -> SpecMarker | None:
  """Return the marker a test carries, or None if it has none.

  Args:
    test: A test method, or a test case as unittest loads it.

  Returns:
    The test's marker, or None.

  """
  if isinstance(test, unittest.TestCase):
    test = getattr(test, test._testMethodName)
  return getattr(test, MARKER_ATTRIBUTE, None)


def _reason(kind: str, reason: str) -> str:
  """Return a marker's reason, which must not be empty."""
  if not isinstance(reason, str) or not reason.strip():
    raise ValueError(f"{kind} needs a reason")
  return reason.strip()


def _label(marker: SpecMarker) -> Callable[[T], T]:
  """Return a decorator that attaches a marker to a test method."""

  def decorator(func: T) -> T:
    if not inspect.isfunction(func):
      raise TypeError(f"{marker.kind} labels a test method, not {func!r}")
    earlier = getattr(func, MARKER_ATTRIBUTE, None)
    if earlier is not None:
      raise ValueError(
        f"{func.__qualname__} already has a {earlier.kind} marker; a test "
        "takes one marker"
      )
    setattr(func, MARKER_ATTRIBUTE, marker)
    return func

  return decorator
