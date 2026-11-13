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

"""Unit tests for the catalog loader in framework.requirements.

Most use a small catalog written to a temporary directory; the last class
reads this repository's catalog.
"""

import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from framework import requirements
from framework.requirements import (
  DEFAULT_CATALOG_DIR,
  HISTORY_FILE,
  ID_RE,
  INDEX_FILE,
  LOCK_FILE,
  Capability,
  CatalogError,
  Rule,
  Statement,
  load_catalog,
)

PAGE = "docs/specification/shopping/cart/index.md"
# The page as a release before the shopping folder named it.
OLD_PAGE = "docs/specification/cart.md"
TESTABLE = {"testable": True, "testable_reason": "aimed at the business"}
VERSIONS = ["2026-01-11", "2026-04-08", "draft"]


def record(
  rule_id: str, level: str = "MUST", demo: dict[str, Any] | None = None
) -> dict[str, Any]:
  """Return a catalog record with the fields the loader reads."""
  return {
    "id": rule_id,
    "level": level,
    "clause": f"The business **{level}** keep {rule_id}.",
    "capabilities": ["dev.ucp.shopping.cart"],
    "source": {
      "file": PAGE,
      "start_line": 12,
      "page": "specification/shopping/cart/",
      "anchor": "cart-lifecycle",
    },
    "demo": TESTABLE if demo is None else demo,
  }


class SmallCatalogTest(unittest.TestCase):
  """Tests against a small catalog in a temporary directory."""

  def setUp(self):
    """Create an empty catalog directory, with nothing cached."""
    super().setUp()
    requirements._load.cache_clear()
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.catalog_dir = Path(temporary.name)

  def write(
    self,
    records: list[dict[str, Any]],
    lock: dict[str, str],
    page: str = "cart/index.json",
  ) -> None:
    """Write one page of records and a lock of IDs and their statuses."""
    path = self.catalog_dir / "shopping" / page
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requirements": records}), encoding="utf-8")
    entries = [{"id": i, "status": status} for i, status in lock.items()]
    (self.catalog_dir / LOCK_FILE).write_text(
      json.dumps({"lock_version": 1, "entries": entries}), encoding="utf-8"
    )

  def test_looks_up_a_rule(self):
    """Test that a rule's fields come from its catalog record."""
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    self.assertEqual(
      load_catalog(self.catalog_dir).rules["REQ-SHOPPING-CART-0001"],
      Rule(
        id="REQ-SHOPPING-CART-0001",
        level="MUST",
        clause="The business **MUST** keep REQ-SHOPPING-CART-0001.",
        capabilities=("dev.ucp.shopping.cart",),
        source_file=PAGE,
        line=12,
        link="specification/shopping/cart/#cart-lifecycle",
        testable=True,
        testable_reason="aimed at the business",
        is_rule=True,
      ),
    )

  def test_level_follows_the_review(self):
    """Test that a level corrected by the review replaces the extracted one."""
    demo = {**TESTABLE, "review": {"level": "MUST"}}
    self.write(
      [record("REQ-SHOPPING-CART-0001", level="MAY", demo=demo)],
      {"REQ-SHOPPING-CART-0001": "active"},
    )
    rule = load_catalog(self.catalog_dir).rules["REQ-SHOPPING-CART-0001"]
    self.assertEqual(rule.level, "MUST")

  def test_status_of_each_kind_of_id(self):
    """Test the four statuses: rule, not a rule, retired and unknown."""
    excluded = {"testable": False, "excluded": True}
    self.write(
      [
        record("REQ-SHOPPING-CART-0001"),
        record("REQ-SHOPPING-CART-0002", demo=excluded),
      ],
      {
        "REQ-SHOPPING-CART-0001": "active",
        "REQ-SHOPPING-CART-0002": "active",
        "REQ-SHOPPING-CART-0003": "retired",
      },
    )
    catalog = load_catalog(self.catalog_dir)
    self.assertEqual(
      [
        catalog.status(rule_id)
        for rule_id in (
          "REQ-SHOPPING-CART-0001",
          "REQ-SHOPPING-CART-0002",
          "REQ-SHOPPING-CART-0003",
          "REQ-SHOPPING-CART-0004",
        )
      ],
      ["rule", "not a rule", "retired", "unknown"],
    )

  def test_extension_parents(self):
    """Test that extensions' parents come from the capability index."""
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    index = {
      "capabilities": [
        {
          "name": "dev.ucp.shopping.discount",
          "extends": ["dev.ucp.shopping.cart"],
        },
        {"name": "dev.ucp.shopping.cart", "extends": []},
      ]
    }
    (self.catalog_dir / INDEX_FILE).write_text(
      json.dumps(index), encoding="utf-8"
    )
    catalog = load_catalog(self.catalog_dir)
    self.assertEqual(
      catalog.with_parents({"dev.ucp.shopping.discount"}),
      {"dev.ucp.shopping.discount", "dev.ucp.shopping.cart"},
    )
    self.assertEqual(
      catalog.with_parents({"dev.ucp.shopping.order"}),
      {"dev.ucp.shopping.order"},
    )

  def test_capability_index(self):
    """Test that capabilities and the spec commit come from the index."""
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    self.assertEqual(load_catalog(self.catalog_dir).capabilities, ())
    self.assertEqual(load_catalog(self.catalog_dir).spec_commit, "")
    requirements._load.cache_clear()
    index = {
      "demo": {"spec_commit": "f2626c28"},
      "capabilities": [
        {"name": "dev.ucp.shopping.cart", "kind": "capability"},
        {
          "name": "dev.ucp.shopping.discount",
          "kind": "extension",
          "extends": ["dev.ucp.shopping.cart"],
        },
      ],
    }
    (self.catalog_dir / INDEX_FILE).write_text(
      json.dumps(index), encoding="utf-8"
    )
    catalog = load_catalog(self.catalog_dir)
    self.assertEqual(
      catalog.capabilities,
      (
        Capability("dev.ucp.shopping.cart"),
        Capability(
          "dev.ucp.shopping.discount", "extension", ("dev.ucp.shopping.cart",)
        ),
      ),
    )
    self.assertEqual(catalog.spec_commit, "f2626c28")

  def test_reads_the_disk_once(self):
    """Test that later loads of the same directory reuse the first read."""
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    first = load_catalog(self.catalog_dir)
    (self.catalog_dir / LOCK_FILE).unlink()
    self.assertIs(load_catalog(str(self.catalog_dir)), first)

  def test_rejects_a_bad_catalog(self):
    """Test that a missing, inconsistent or malformed catalog is an error."""
    cases = {
      "missing": lambda: None,
      "rule missing from the lock": lambda: self.write(
        [record("REQ-SHOPPING-CART-0001")], {}
      ),
      "active ID missing from the pages": lambda: self.write(
        [], {"REQ-SHOPPING-CART-0001": "active"}
      ),
      "malformed record": lambda: self.write(
        [{"id": "REQ-SHOPPING-CART-0001"}], {"REQ-SHOPPING-CART-0001": "active"}
      ),
      "malformed ID": lambda: self.write(
        [record("REQ-SHOPPING-CART")], {"REQ-SHOPPING-CART": "active"}
      ),
    }
    for name, write in cases.items():
      with self.subTest(name), tempfile.TemporaryDirectory() as directory:
        self.catalog_dir = Path(directory)
        write()
        with self.assertRaises(CatalogError):
          load_catalog(self.catalog_dir)

  def test_rejects_an_id_on_two_pages(self):
    """Test that an ID repeated across pages is an error."""
    self.write([record("REQ-SHOPPING-CART-0001")], {}, page="cart/rest.json")
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    with self.assertRaises(CatalogError):
      load_catalog(self.catalog_dir)

  def write_history(self, removed: list[dict[str, Any]]) -> None:
    """Write a history of three versions with some removed rules."""
    (self.catalog_dir / HISTORY_FILE).write_text(
      json.dumps({"versions": VERSIONS, "removed": removed}), encoding="utf-8"
    )

  def test_reads_the_history(self):
    """Test the releases a rule records, and a rule only older ones state."""
    stated = {
      **TESTABLE,
      "since": "2026-01-11",
      "releases": [
        {
          "version": "2026-01-11",
          "file": OLD_PAGE,
          "start_line": 3,
          "level": "SHOULD",
          "text_digest": "aaaaaaaaaa",
          "clause": "The business **SHOULD** keep it.",
        },
        {
          "version": "2026-04-08",
          "file": OLD_PAGE,
          "start_line": 9,
          "level": "MUST",
          "text_digest": "bbbbbbbbbb",
        },
      ],
    }
    self.write(
      [record("REQ-SHOPPING-CART-0001", demo=stated)],
      {"REQ-SHOPPING-CART-0001": "active", "REQ-SHOPPING-CART-0002": "retired"},
    )
    removed = {
      **TESTABLE,
      "since": "2026-01-11",
      "until": "2026-04-08",
      "releases": [
        {
          "version": "2026-01-11",
          "file": OLD_PAGE,
          "start_line": 5,
          "level": "MUST",
          "text_digest": "cccccccccc",
        }
      ],
    }
    self.write_history([record("REQ-SHOPPING-CART-0002", demo=removed)])
    catalog = load_catalog(self.catalog_dir)
    self.assertEqual(catalog.versions, tuple(VERSIONS))
    rule = catalog.rules["REQ-SHOPPING-CART-0001"]
    self.assertEqual((rule.since, rule.until), ("2026-01-11", None))
    self.assertEqual(rule.versions, tuple(VERSIONS))
    self.assertEqual(
      [rule.level_in(v) for v in ("2026-01-11", "2026-01-23", *VERSIONS[1:])],
      ["SHOULD", None, "MUST", "MUST"],
    )
    self.assertEqual(
      rule.statements[0],
      Statement(
        "2026-01-11", "SHOULD", "The business **SHOULD** keep it.", OLD_PAGE, 3
      ),
    )
    # A statement in the rule's own wording takes the record's clause.
    self.assertEqual(rule.statements[1].clause, rule.clause)
    self.assertEqual(catalog.status("REQ-SHOPPING-CART-0002"), "removed")
    gone = catalog.rule("REQ-SHOPPING-CART-0002")
    self.assertEqual(
      (gone.since, gone.until, gone.versions),
      ("2026-01-11", "2026-04-08", ("2026-01-11",)),
    )
    self.assertIsNone(gone.level_in("draft"))
    self.assertNotIn(gone.id, catalog.rules)

  def test_rejects_a_removed_rule_that_is_not_retired(self):
    """Test that a removed rule must have a retired ID in the lock."""
    self.write(
      [record("REQ-SHOPPING-CART-0001")], {"REQ-SHOPPING-CART-0001": "active"}
    )
    for rule_id in ("REQ-SHOPPING-CART-0001", "REQ-SHOPPING-CART-0002"):
      with self.subTest(rule_id):
        requirements._load.cache_clear()
        self.write_history([record(rule_id)])
        with self.assertRaises(CatalogError):
          load_catalog(self.catalog_dir)


class RepositoryCatalogTest(unittest.TestCase):
  """Tests against this repository's catalog."""

  def setUp(self):
    """Load the repository's catalog."""
    super().setUp()
    self.catalog = load_catalog()

  def test_matches_its_lock(self):
    """Test that every rule fits the ID pattern and has an active lock ID."""
    lock = json.loads((DEFAULT_CATALOG_DIR / LOCK_FILE).read_text("utf-8"))
    active = {e["id"] for e in lock["entries"] if e["status"] == "active"}
    self.assertEqual(set(self.catalog.rules), active)
    self.assertTrue(all(ID_RE.match(rule_id) for rule_id in active))

  def test_known_rules(self):
    """Test what a few stable IDs name, and an extension's parents."""
    self.assertIn(
      "dev.ucp.shopping.checkout",
      self.catalog.extends["dev.ucp.shopping.fulfillment"],
    )
    subtotal = self.catalog.rules["REQ-SHOPPING-CHECKOUT-0133"]
    self.assertEqual(subtotal.level, "MUST")
    self.assertIn("subtotal", subtotal.clause)
    self.assertEqual(subtotal.capabilities, ("dev.ucp.shopping.checkout",))
    self.assertTrue(subtotal.testable)
    # Extracted as MAY; the review corrected it to MUST.
    self.assertEqual(
      self.catalog.rules["REQ-SHOPPING-CATALOG-0046"].level, "MUST"
    )
    # The review found this sentence states no requirement.
    self.assertEqual(
      self.catalog.status("REQ-SHOPPING-CATALOG-0044"), "not a rule"
    )
    # Positional IDs from before the lock are no longer valid citations.
    self.assertEqual(
      self.catalog.status("REQ-CHECKOUT-RENDERING-CONTRACT-05"), "unknown"
    )

  def test_history(self):
    """Test the releases that state a few rules, and a removed rule."""
    self.assertEqual(
      self.catalog.versions,
      ("2026-01-11", "2026-01-23", "2026-04-08", "2026-08-25", "draft"),
    )
    # Stated in every release; 2026-01-11 had no list stem for it.
    titles = self.catalog.rules["REQ-SHOPPING-FULFILLMENT-0018"]
    self.assertEqual(titles.since, "2026-01-11")
    self.assertEqual(titles.versions, self.catalog.versions)
    # The selection and location identity contract came in 2026-08-25.
    self.assertEqual(
      self.catalog.rules["REQ-SHOPPING-FULFILLMENT-0015"].since, "2026-08-25"
    )
    # Webhook signatures were detached JWTs until RFC 9421 replaced them.
    jwt = self.catalog.rule("REQ-SHOPPING-ORDER-0043")
    self.assertEqual(self.catalog.status(jwt.id), "removed")
    self.assertEqual((jwt.since, jwt.until), ("2026-01-11", "2026-04-08"))
    self.assertIn("Request-Signature", jwt.clause)
    self.assertTrue(all(rule.until for rule in self.catalog.removed.values()))


if __name__ == "__main__":
  unittest.main()
