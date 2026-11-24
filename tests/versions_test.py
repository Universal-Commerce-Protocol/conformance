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

"""Unit tests for version gating in framework.versions and the base test.

The catalog is a small one built in memory, with a history of two releases
and the draft. The server profiles are fakes.
"""

import dataclasses
import io
from types import MappingProxyType
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework import base_test, outcomes, versions
from framework.base_test import BaseIntegrationTest
from framework.decorators import SpecMarker, requires_capability, spec_assert
from framework.discovery import UcpProfile
from framework.outcomes import (
  CAPABILITY,
  NOT_APPLICABLE,
  PASSED,
  SKIPPED,
  RecordingResult,
  is_not_applicable,
)
from framework.requirements import DRAFT, Capability, Catalog, Rule, Statement
from framework.versions import (
  Applicability,
  advertised_version,
  applicability,
  catalog_version,
  rule_applies,
)

CHECKOUT = "dev.ucp.shopping.checkout"
ORDER = "dev.ucp.shopping.order"
AP2 = "dev.ucp.common.payment.ap2_mandate"
VERSIONS = ("2026-01-11", "2026-04-08", DRAFT)


def stated(version: str, level: str = "MUST") -> Statement:
  """Return how a release states a rule."""
  return Statement(version, level, "The business MUST.", "a.md", 1)


def rule(
  rule_id: str,
  capabilities: tuple[str, ...] = (CHECKOUT,),
  since: str = DRAFT,
  until: str | None = None,
  statements: tuple[Statement, ...] = (),
) -> Rule:
  """Return a testable MUST rule with the given history."""
  return Rule(
    id=rule_id,
    level="MUST",
    clause="The business MUST.",
    capabilities=capabilities,
    source_file="docs/specification/shopping/checkout/index.md",
    line=1,
    link="specification/shopping/checkout/#a",
    testable=True,
    testable_reason="aimed at the business",
    is_rule=True,
    since=since,
    until=until,
    statements=statements,
  )


# Stated by every version.
OLD = rule(
  "REQ-SHOPPING-CHECKOUT-0001",
  since="2026-01-11",
  statements=(stated("2026-01-11"), stated("2026-04-08")),
)
# Introduced in 2026-04-08.
NEWER = rule(
  "REQ-SHOPPING-CHECKOUT-0002",
  since="2026-04-08",
  statements=(stated("2026-04-08"),),
)
# Only in the draft.
DRAFT_ONLY = rule("REQ-SHOPPING-CHECKOUT-0003")
# Stated by 2026-01-11 only; removed in 2026-04-08.
REMOVED = rule(
  "REQ-SHOPPING-CHECKOUT-0090",
  since="2026-01-11",
  until="2026-04-08",
  statements=(stated("2026-01-11"),),
)
# Stated by both releases, removed in the draft.
REMOVED_IN_DRAFT = rule(
  "REQ-SHOPPING-CHECKOUT-0091",
  since="2026-01-11",
  until=DRAFT,
  statements=(stated("2026-01-11"), stated("2026-04-08")),
)
# An order rule introduced in 2026-04-08, for a server whose capabilities
# are at different versions.
ORDER_NEWER = rule(
  "REQ-SHOPPING-ORDER-0001",
  capabilities=(ORDER,),
  since="2026-04-08",
  statements=(stated("2026-04-08"),),
)
# A rule of a renamed capability.
AP2_RULE = rule(
  "REQ-COMMON-PAYMENT-AP2-0001",
  capabilities=(AP2,),
  since="2026-01-11",
  statements=(stated("2026-01-11"), stated("2026-04-08")),
)
CATALOG = Catalog(
  rules=MappingProxyType(
    {r.id: r for r in (OLD, NEWER, DRAFT_ONLY, ORDER_NEWER, AP2_RULE)}
  ),
  retired=frozenset({REMOVED.id, REMOVED_IN_DRAFT.id}),
  capabilities=(Capability(CHECKOUT), Capability(ORDER)),
  spec_commit="abc123def456",
  removed=MappingProxyType(
    {REMOVED.id: REMOVED, REMOVED_IN_DRAFT.id: REMOVED_IN_DRAFT}
  ),
  versions=VERSIONS,
)


def profile(version: str | None, **capabilities: str | None) -> UcpProfile:
  """Return a profile that advertises a root version and capabilities.

  Args:
    version: The profile's root version; None for none.
    capabilities: Capability names (with `_` for `.`) and their versions;
      None for a capability advertised without a version.

  Returns:
    The profile.

  """
  ucp: dict = {"capabilities": []}
  if version is not None:
    ucp["version"] = version
  for name, cap_version in capabilities.items():
    entry: dict = {"name": name.replace("_", ".")}
    if cap_version is not None:
      entry["version"] = cap_version
    ucp["capabilities"].append(entry)
  return UcpProfile({"ucp": ucp})


def cites(*rule_ids: str) -> SpecMarker:
  """Return a spec_assert marker."""
  return SpecMarker("spec_assert", rule_ids=rule_ids)


class CatalogVersionTest(unittest.TestCase):
  """Tests for reading a server's version as one the catalog knows."""

  def test_known_release(self):
    """Test that a release the catalog knows is used as it is."""
    self.assertEqual(catalog_version("2026-01-11", VERSIONS), "2026-01-11")
    self.assertEqual(catalog_version("2026-04-08", VERSIONS), "2026-04-08")

  def test_between_releases(self):
    """Test that a version between releases is taken as the older one."""
    self.assertEqual(catalog_version("2026-02-01", VERSIONS), "2026-01-11")

  def test_newer_than_every_release(self):
    """Test that a version newer than every release is the draft."""
    self.assertEqual(catalog_version("2026-12-31", VERSIONS), DRAFT)

  def test_older_than_every_release(self):
    """Test that a version older than every release is the draft."""
    self.assertEqual(catalog_version("2025-01-01", VERSIONS), DRAFT)

  def test_no_version_or_not_a_date(self):
    """Test that no version, or one that isn't a date, is the draft."""
    self.assertEqual(catalog_version(None, VERSIONS), DRAFT)
    self.assertEqual(catalog_version("", VERSIONS), DRAFT)
    self.assertEqual(catalog_version("v2", VERSIONS), DRAFT)


class AdvertisedVersionTest(unittest.TestCase):
  """Tests for finding the version a server advertises for a rule."""

  def test_capability_version(self):
    """Test that the capability's own version is used."""
    server = profile("2026-04-08", dev_ucp_shopping_checkout="2026-01-11")
    self.assertEqual(advertised_version(server, (CHECKOUT,)), "2026-01-11")

  def test_falls_back_to_the_profile_version(self):
    """Test the root version when the capability isn't advertised."""
    server = profile("2026-04-08")
    self.assertEqual(advertised_version(server, (CHECKOUT,)), "2026-04-08")
    server = profile("2026-04-08", dev_ucp_shopping_checkout=None)
    self.assertEqual(advertised_version(server, (CHECKOUT,)), "2026-04-08")

  def test_none_when_nothing_is_advertised(self):
    """Test None when neither the capability nor the root has a version."""
    self.assertIsNone(advertised_version(profile(None), (CHECKOUT,)))

  def test_former_name(self):
    """Test that a capability advertised under a former name counts."""
    server = profile("2026-01-11", dev_ucp_shopping_ap2_mandate="2026-01-11")
    self.assertEqual(advertised_version(server, (AP2,)), "2026-01-11")

  def test_first_advertised_capability_wins(self):
    """Test that the first of a rule's capabilities the server offers wins."""
    server = profile(
      "2026-04-08",
      dev_ucp_shopping_checkout="2026-01-11",
      dev_ucp_shopping_order="2026-04-08",
    )
    self.assertEqual(
      advertised_version(server, (ORDER, CHECKOUT)), "2026-04-08"
    )


class ApplicabilityTest(unittest.TestCase):
  """Tests for deciding whether a test applies to a server."""

  def test_only_spec_assert_tests_can_be_inapplicable(self):
    """Test that tests without rules always apply."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    for marker in (
      None,
      SpecMarker("no_rule", reason="no keyword"),
      SpecMarker("scenario", reason="store data"),
      SpecMarker("outside_catalog", reason="core page"),
      SpecMarker("older_version", reason="old", version="2026-01-11"),
    ):
      with self.subTest(marker=marker):
        self.assertEqual(
          applicability(marker, CATALOG, server), Applicability(True)
        )

  def test_applies_when_any_rule_is_stated(self):
    """Test that one stated rule is enough for the test to apply."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    self.assertTrue(applicability(cites(OLD.id), CATALOG, server).applies)
    self.assertTrue(
      applicability(cites(NEWER.id, OLD.id), CATALOG, server).applies
    )

  def test_unknown_rule_counts_as_stated(self):
    """Test that an ID the catalog doesn't know leaves the test to run."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    marker = cites(NEWER.id, "REQ-SHOPPING-CHECKOUT-0999")
    self.assertTrue(applicability(marker, CATALOG, server).applies)

  def test_introduced_later(self):
    """Test a rule the server's release doesn't state yet."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    result = applicability(cites(NEWER.id), CATALOG, server)
    self.assertEqual(
      result,
      Applicability(
        False,
        "the server implements 2026-01-11, which doesn't state "
        "REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08)",
      ),
    )
    self.assertTrue(is_not_applicable(result.skip_reason))
    self.assertEqual(
      result.skip_reason,
      "not applicable: the server implements 2026-01-11, which doesn't "
      "state REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08)",
    )

  def test_not_yet_in_a_release(self):
    """Test a rule only the draft states, against a release."""
    server = profile("2026-04-08", dev_ucp_shopping_checkout="2026-04-08")
    self.assertEqual(
      applicability(cites(DRAFT_ONLY.id), CATALOG, server).reason,
      "the server implements 2026-04-08, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0003 (not yet in a release)",
    )

  def test_removed(self):
    """Test a removed rule, against the release that removed it."""
    server = profile("2026-04-08", dev_ucp_shopping_checkout="2026-04-08")
    self.assertEqual(
      applicability(cites(REMOVED.id), CATALOG, server).reason,
      "the server implements 2026-04-08, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0090 (removed in 2026-04-08)",
    )
    # The release before still states it.
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    self.assertTrue(applicability(cites(REMOVED.id), CATALOG, server).applies)

  def test_removed_in_the_draft(self):
    """Test a rule the draft removed, against a server on the draft."""
    server = profile("2027-01-01", dev_ucp_shopping_checkout="2027-01-01")
    self.assertEqual(
      applicability(cites(REMOVED_IN_DRAFT.id), CATALOG, server).reason,
      "the server implements 2027-01-01, newer than any release the catalog "
      "knows, so is taken to implement the draft, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0091 (removed in the draft)",
    )
    server = profile("2026-04-08", dev_ucp_shopping_checkout="2026-04-08")
    self.assertTrue(
      applicability(cites(REMOVED_IN_DRAFT.id), CATALOG, server).applies
    )

  def test_no_version_means_the_draft(self):
    """Test that a server with no version is taken to be on the draft."""
    server = profile(None, dev_ucp_shopping_checkout=None)
    self.assertTrue(
      applicability(cites(DRAFT_ONLY.id), CATALOG, server).applies
    )
    self.assertEqual(
      applicability(cites(REMOVED.id), CATALOG, server).reason,
      "the server advertises no version, so is taken to implement the draft, "
      "which doesn't state REQ-SHOPPING-CHECKOUT-0090 (removed in 2026-04-08)",
    )

  def test_version_the_catalog_cannot_place(self):
    """Test versions older than every release, or not a date: the draft."""
    for version, how in (
      ("2025-06-01", "older than any release the catalog knows"),
      ("v2", "not a release date"),
    ):
      with self.subTest(version=version):
        server = profile(version, dev_ucp_shopping_checkout=version)
        self.assertTrue(
          applicability(cites(DRAFT_ONLY.id), CATALOG, server).applies
        )
        self.assertEqual(
          applicability(cites(REMOVED.id), CATALOG, server).reason,
          f"the server implements {version}, {how}, so is taken to "
          "implement the draft, which doesn't state "
          "REQ-SHOPPING-CHECKOUT-0090 (removed in 2026-04-08)",
        )

  def test_version_between_releases(self):
    """Test that a version between releases reads as the older release."""
    server = profile("2026-02-01", dev_ucp_shopping_checkout="2026-02-01")
    self.assertEqual(
      applicability(cites(NEWER.id), CATALOG, server).reason,
      "the server implements 2026-02-01, taken as 2026-01-11, which doesn't "
      "state REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08)",
    )

  def test_several_rules_and_capabilities(self):
    """Test the reason for rules of capabilities at different versions."""
    server = profile(
      "2026-01-11",
      dev_ucp_shopping_checkout="2026-01-11",
      dev_ucp_shopping_order="2026-01-11",
    )
    self.assertEqual(
      applicability(
        cites(NEWER.id, DRAFT_ONLY.id, ORDER_NEWER.id), CATALOG, server
      ).reason,
      "the server implements 2026-01-11, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08) or "
      "REQ-SHOPPING-CHECKOUT-0003 (not yet in a release) or "
      "REQ-SHOPPING-ORDER-0001 (introduced in 2026-04-08)",
    )
    # The order capability is ahead of checkout: the order rule applies.
    server = profile(
      "2026-01-11",
      dev_ucp_shopping_checkout="2026-01-11",
      dev_ucp_shopping_order="2026-04-08",
    )
    self.assertTrue(
      applicability(cites(NEWER.id, ORDER_NEWER.id), CATALOG, server).applies
    )
    self.assertEqual(
      applicability(cites(NEWER.id, DRAFT_ONLY.id), CATALOG, server).reason,
      "the server implements 2026-01-11, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08) or "
      "REQ-SHOPPING-CHECKOUT-0003 (not yet in a release)",
    )

  def test_rule_applies(self):
    """Test the per-rule check the report uses."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    self.assertTrue(rule_applies(OLD, CATALOG, server))
    self.assertFalse(rule_applies(NEWER, CATALOG, server))
    self.assertTrue(rule_applies(REMOVED, CATALOG, server))
    self.assertFalse(rule_applies(DRAFT_ONLY, CATALOG, server))


def run_gated(server: UcpProfile | None, catalog: Catalog | None):
  """Run two base-test subclasses' tests against a fake server profile.

  One test cites a rule the 2026-01-11 server states, one a rule it doesn't,
  and one cites nothing. No client or agent server is started.

  Returns:
    The recording result, and the IDs unittest gave the tests.

  """

  @requires_capability(CHECKOUT)
  class Gated(BaseIntegrationTest):
    @spec_assert(OLD.id)
    def test_stated(self):
      """Pass."""

    @spec_assert(NEWER.id)
    def test_unstated(self):
      """Pass, if run."""

    def test_unmarked(self):
      """Pass."""

  ids = {
    name: f"{Gated.__module__}.{Gated.__qualname__}.{name}"
    for name in ("test_stated", "test_unstated", "test_unmarked")
  }
  suite = unittest.defaultTestLoader.loadTestsFromTestCase(Gated)
  with (
    flagsaver.flagsaver(server_url="http://127.0.0.1:9"),
    mock.patch.object(BaseIntegrationTest, "server_profile", server),
    mock.patch.object(BaseIntegrationTest, "_start_agent_server"),
    mock.patch.object(base_test, "_catalog", return_value=catalog),
  ):
    runner = unittest.TextTestRunner(
      stream=io.StringIO(), resultclass=RecordingResult
    )
    result = runner.run(suite)
  return result, ids


class SetUpGateTest(unittest.TestCase):
  """Tests for the version gate in BaseIntegrationTest.setUp."""

  def test_skips_only_the_test_whose_rule_is_unstated(self):
    """Test that a test citing only an unstated rule is not applicable."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    result, ids = run_gated(server, CATALOG)
    outcomes = {name: result.outcomes[test_id] for name, test_id in ids.items()}
    self.assertEqual(outcomes["test_stated"].status, PASSED)
    self.assertEqual(outcomes["test_unmarked"].status, PASSED)
    self.assertEqual(outcomes["test_unstated"].status, NOT_APPLICABLE)
    self.assertEqual(
      outcomes["test_unstated"].detail,
      "the server implements 2026-01-11, which doesn't state "
      "REQ-SHOPPING-CHECKOUT-0002 (introduced in 2026-04-08)",
    )

  def test_everything_runs_on_a_newer_server(self):
    """Test that a server on the newer release runs every test."""
    server = profile("2026-04-08", dev_ucp_shopping_checkout="2026-04-08")
    result, ids = run_gated(server, CATALOG)
    self.assertEqual(
      {name: result.outcomes[test_id].status for name, test_id in ids.items()},
      dict.fromkeys(ids, PASSED),
    )

  def test_capability_check_comes_first(self):
    """Test that a missing capability skips the test as before."""
    server = profile("2026-01-11")
    result, ids = run_gated(server, CATALOG)
    outcome = result.outcomes[ids["test_unstated"]]
    self.assertEqual(outcome.status, SKIPPED)
    self.assertEqual(outcome.category, CAPABILITY)
    self.assertEqual(
      outcome.detail, "'dev.ucp.shopping.checkout' (min_version: any)"
    )

  def test_without_a_profile_or_catalog_nothing_is_gated(self):
    """Test that the gate stands aside without a profile or a catalog."""
    server = profile("2026-01-11", dev_ucp_shopping_checkout="2026-01-11")
    for case, catalog in ((None, CATALOG), (server, None)):
      with self.subTest(profile=case, catalog=catalog):
        result, ids = run_gated(case, catalog)
        self.assertEqual(result.outcomes[ids["test_unstated"]].status, PASSED)


class ModuleTest(unittest.TestCase):
  """Tests for what the module exports."""

  def test_prefix_is_shared_with_outcomes(self):
    """Test that the skip reason uses the prefix outcomes.py recognizes."""
    self.assertIs(
      versions.NOT_APPLICABLE_PREFIX, outcomes.NOT_APPLICABLE_PREFIX
    )
    self.assertTrue(is_not_applicable(Applicability(False, "why").skip_reason))
    self.assertEqual(
      dataclasses.asdict(Applicability(True)), {"applies": True, "reason": ""}
    )


if __name__ == "__main__":
  unittest.main()
