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

"""Decide whether a test's rules apply to the version a server implements.

A server advertises the release each of its capabilities implements, such as
`2026-04-08`. The catalog records which releases state each rule (see
`framework.requirements`). Before a `spec_assert` test runs, the rules it cites
are compared with the version the server advertises for their capability: if
that version states none of them, the test doesn't apply. It is skipped with a
reason that starts with `NOT_APPLICABLE_PREFIX`, which `framework.outcomes`
records as its own outcome, one that never counts against the server.

A version newer than every release the catalog knows is taken to implement
the catalog's own version, the draft, and so is a server that advertises no
version. A version between two releases is taken to implement the older one.
"""

from collections.abc import Iterable, Sequence
import dataclasses

from framework.decorators import VERSION_RE, SpecMarker
from framework.discovery import FORMER_NAMES, UcpProfile
from framework.outcomes import NOT_APPLICABLE_PREFIX
from framework.requirements import DRAFT, Catalog, Rule


@dataclasses.dataclass(frozen=True)
class Applicability:
  """Whether a test's rules apply to the server, and why not if they don't.

  Attributes:
    applies: Whether the server's version states at least one of the test's
      rules.
    reason: When the test doesn't apply, why: which version the server
      implements, and which rules it doesn't state, with when each came or
      went.

  """

  applies: bool
  reason: str = ""

  @property
  def skip_reason(self) -> str:
    """Return the reason to skip the test with, starting with the prefix."""
    return f"{NOT_APPLICABLE_PREFIX} {self.reason}"


@dataclasses.dataclass(frozen=True)
class _Unstated:
  """A rule the server's version doesn't state."""

  rule: Rule
  advertised: str | None
  version: str


def advertised_version(
  profile: UcpProfile, capabilities: Iterable[str]
) -> str | None:
  """Return the version a server advertises for the first capability it offers.

  Args:
    profile: The server's discovery profile.
    capabilities: Capability names, such as a rule's.

  Returns:
    The version of the first of the capabilities the server advertises, under
    its current or a former name; otherwise the profile's own version. None
    when the server advertises no version there either.

  """
  for name in capabilities:
    for alias in (name, *FORMER_NAMES.get(name, ())):
      capability = profile.capabilities.get(alias)
      if capability is not None:
        return capability.version or profile.ucp_version or None
  return profile.ucp_version or None


def catalog_version(advertised: str | None, versions: Sequence[str]) -> str:
  """Return the catalog version a server's advertised version maps to.

  Args:
    advertised: The version the server advertises, such as "2026-04-08";
      None or empty when it advertises none.
    versions: The versions the catalog knows, oldest first, ending with
      `DRAFT`.

  Returns:
    The advertised version if the catalog knows it. Otherwise the newest
    release that isn't newer than it. `DRAFT` when the version is newer than
    every release the catalog knows, or older than all of them, or isn't a
    release date, or when the server advertises none.

  """
  if not advertised or not VERSION_RE.match(advertised):
    return DRAFT
  releases = [version for version in versions if version != DRAFT]
  if not releases or advertised > releases[-1]:
    return DRAFT
  older = [version for version in releases if version <= advertised]
  return older[-1] if older else DRAFT


def server_version(
  rule: Rule, catalog: Catalog, profile: UcpProfile
) -> tuple[str | None, str]:
  """Return the version a server implements for a rule, as the catalog reads it.

  Args:
    rule: The rule.
    catalog: The requirements catalog.
    profile: The server's discovery profile.

  Returns:
    The version the server advertises for the rule's capability (None when
    it advertises none), and the catalog version that maps to.

  """
  advertised = advertised_version(profile, rule.capabilities)
  return advertised, catalog_version(advertised, catalog.versions)


def rule_applies(rule: Rule, catalog: Catalog, profile: UcpProfile) -> bool:
  """Return whether the version the server implements states a rule."""
  _, version = server_version(rule, catalog, profile)
  return rule.level_in(version) is not None


def applicability(
  marker: SpecMarker | None, catalog: Catalog, profile: UcpProfile
) -> Applicability:
  """Decide whether a test applies to the server.

  Only a `spec_assert` test can be inapplicable. It applies when the version
  the server advertises for a cited rule's capability states that rule. A
  rule the catalog doesn't know counts as stated, so that the marker check,
  not this, reports it.

  Args:
    marker: The test's marker, if it has one.
    catalog: The requirements catalog.
    profile: The server's discovery profile.

  Returns:
    Whether the test applies and, if not, why.

  """
  if marker is None or marker.kind != "spec_assert":
    return Applicability(True)
  unstated = []
  for rule_id in marker.rule_ids:
    rule = catalog.rule(rule_id)
    if rule is None:
      return Applicability(True)
    advertised, version = server_version(rule, catalog, profile)
    if rule.level_in(version) is not None:
      return Applicability(True)
    unstated.append(_Unstated(rule, advertised, version))
  return Applicability(False, _reason(unstated, catalog.versions))


def _reason(unstated: Sequence[_Unstated], versions: Sequence[str]) -> str:
  """Say which version the server implements and what it doesn't state.

  Args:
    unstated: The test's rules, none of which the server's version states,
      each with the version compared.
    versions: Every version the catalog knows, oldest first.

  Returns:
    The reason, such as "the server implements 2026-04-08, which doesn't
    state REQ-SHOPPING-CHECKOUT-0016 (introduced in 2026-08-25)".

  """
  by_version: dict[tuple[str | None, str], list[str]] = {}
  for entry in unstated:
    rules = by_version.setdefault((entry.advertised, entry.version), [])
    rules.append(
      f"{entry.rule.id} ({_when(entry.rule, entry.version, versions)})"
    )
  return "; ".join(
    f"{_implements(advertised, version, versions)}, which doesn't state "
    + " or ".join(rules)
    for (advertised, version), rules in by_version.items()
  )


def _implements(
  advertised: str | None, version: str, versions: Sequence[str]
) -> str:
  """Say which version the server implements, and how it was read."""
  if not advertised:
    return (
      "the server advertises no version, so is taken to implement the draft"
    )
  if advertised == version:
    return f"the server implements {advertised}"
  if version != DRAFT:
    return f"the server implements {advertised}, taken as {version}"
  releases = [known for known in versions if known != DRAFT]
  if not VERSION_RE.match(advertised):
    how = "not a release date"
  elif releases and advertised < releases[0]:
    how = "older than any release the catalog knows"
  else:
    how = "newer than any release the catalog knows"
  return (
    f"the server implements {advertised}, {how}, so is taken to implement "
    "the draft"
  )


def _when(rule: Rule, version: str, versions: Sequence[str]) -> str:
  """Say when a rule the version doesn't state came or went."""
  if versions.index(version) < versions.index(rule.since):
    return (
      "not yet in a release"
      if rule.since == DRAFT
      else f"introduced in {rule.since}"
    )
  if rule.until is not None and versions.index(version) >= versions.index(
    rule.until
  ):
    return (
      "removed in the draft"
      if rule.until == DRAFT
      else f"removed in {rule.until}"
    )
  return f"not stated in {version}"
