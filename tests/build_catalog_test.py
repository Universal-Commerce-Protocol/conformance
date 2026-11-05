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

"""Unit tests for the release history in requirements/tools/build_catalog.py.

The tool is a script, not a package, so it is loaded from its path. These
tests need neither the extractor nor a specification checkout; the one that
reads a release from git builds a small repository of its own.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_TOOL = (
  Path(__file__).resolve().parents[1]
  / "requirements"
  / "tools"
  / "build_catalog.py"
)
_SPEC = importlib.util.spec_from_file_location("build_catalog", _TOOL)
build_catalog = importlib.util.module_from_spec(_SPEC)
sys.modules["build_catalog"] = build_catalog
_SPEC.loader.exec_module(build_catalog)

CHECKOUT_PAGE = "docs/specification/shopping/checkout/index.md"
CHECKOUT_REST = "docs/specification/shopping/checkout/rest.md"
CART_PAGE = "docs/specification/shopping/cart/index.md"
VERSIONS = ("2026-01-11", "2026-04-08", "draft")
COMMIT = "f" * 40


def wording(
  text: str, file: str = CHECKOUT_PAGE, context: str | None = None
) -> "build_catalog.Wording":
  """Return a wording, with a digest of its context and text."""
  joined = f"{context or ''}\x1f{text}"
  digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:10]
  return build_catalog.Wording(file, digest, context, text)


def stated(
  version: str,
  text: str,
  file: str = CHECKOUT_PAGE,
  context: str | None = None,
  line: int = 1,
  level: str = "MUST",
) -> "build_catalog.Stated":
  """Return a rule as a release states it, on a page under its old name."""
  return build_catalog.Stated(
    version=version,
    wording=wording(text, file, context),
    position=(line, line),
    file=file.replace("shopping/checkout/index.md", "checkout.md"),
    level=level,
    prefix="REQ-SHOPPING-CHECKOUT",
    record={"clause": f"The business {text}."},
  )


def same_capability(page: str, other: str) -> bool:
  """Return whether two test pages belong to one capability."""
  return Path(page).parent == Path(other).parent


class MatchReleaseTest(unittest.TestCase):
  """Tests for match_release."""

  def test_same_wording(self):
    """Test a rule found word for word on its page."""
    text = "must return the checkout"
    self.assertEqual(
      build_catalog.match_release(
        [stated("2026-04-08", text)], {"REQ-1": wording(text)}
      ),
      [("REQ-1", build_catalog.SAME_WORDING)],
    )

  def test_moved_to_a_related_page(self):
    """Test a rule that moved, but only between related pages."""
    text = "must return the checkout"
    candidates = {"REQ-1": wording(text, CHECKOUT_REST)}
    rules = [stated("2026-04-08", text)]
    self.assertEqual(
      build_catalog.match_release(rules, candidates, same_capability),
      [("REQ-1", build_catalog.OTHER_PAGE)],
    )
    elsewhere = {"REQ-1": wording(text, CART_PAGE)}
    self.assertEqual(
      build_catalog.match_release(rules, elsewhere, same_capability), [None]
    )

  def test_other_list_stem(self):
    """Test the same wording under another stem, if it is long enough."""
    long_text = "must distinguish this option from its siblings"
    self.assertEqual(
      build_catalog.match_release(
        [stated("2026-01-11", long_text)],
        {"REQ-1": wording(long_text, context="for options[].title")},
      ),
      [("REQ-1", build_catalog.OTHER_STEM)],
    )
    # A matrix cell reads just "must": its stem tells it apart.
    self.assertEqual(
      build_catalog.match_release(
        [stated("2026-01-11", "must", context="create")],
        {"REQ-1": wording("must", context="cancel")},
      ),
      [None],
    )

  def test_reworded(self):
    """Test a reworded rule, at least 85% alike, and one that isn't."""
    self.assertEqual(
      build_catalog.match_release(
        [stated("2026-08-25", "business must use cart contents")],
        {"REQ-1": wording("the business must use the cart contents")},
      ),
      [("REQ-1", build_catalog.REWORDED)],
    )
    self.assertEqual(
      build_catalog.match_release(
        [stated("2026-08-25", "must sign the payload with a JWT")],
        {"REQ-1": wording("must sign the payload per RFC 9421")},
      ),
      [None],
    )

  def test_one_rule_per_candidate(self):
    """Test that two copies of a wording take one candidate each."""
    text = "must return the checkout"
    rules = [
      stated("2026-04-08", text, line=1),
      stated("2026-04-08", text, line=2),
    ]
    self.assertEqual(
      build_catalog.match_release(rules, {"REQ-1": wording(text)}),
      [("REQ-1", build_catalog.SAME_WORDING), None],
    )
    self.assertEqual(
      build_catalog.match_release(
        rules, {"REQ-2": wording(text), "REQ-1": wording(text)}
      ),
      [
        ("REQ-1", build_catalog.SAME_WORDING),
        ("REQ-2", build_catalog.SAME_WORDING),
      ],
    )


class TraceHistoryTest(unittest.TestCase):
  """Tests for trace_history."""

  def setUp(self):
    """Set up one rule of the pinned spec, and two releases."""
    super().setUp()
    self.kept = "must return the full checkout on every response"
    self.reworded = "must return the checkout on every response"
    self.gone = "must sign webhooks with a detached jws signature"
    self.moved = "must include the ucp-agent header on every request"
    self.current = {"REQ-SHOPPING-CHECKOUT-0001": wording(self.kept)}
    self.outside = [wording(self.moved, "docs/specification/overview.md")]
    self.stated = {
      "2026-04-08": [
        stated("2026-04-08", self.kept),
        stated("2026-04-08", self.gone, line=2),
      ],
      "2026-01-11": [
        stated("2026-01-11", self.reworded),
        stated("2026-01-11", self.gone, line=2),
        stated("2026-01-11", self.moved, line=3),
      ],
    }

  def test_trace(self):
    """Test kept, reworded, removed and moved rules, newest release first."""
    history = build_catalog.trace_history(
      VERSIONS, self.stated, self.current, self.outside
    )
    kept = history.stated["REQ-SHOPPING-CHECKOUT-0001"]
    self.assertEqual(
      [(s.version, s.wording.text) for s in kept],
      [("2026-01-11", self.reworded), ("2026-04-08", self.kept)],
    )
    self.assertEqual(history.removed, ["~00000"])
    self.assertEqual(
      [s.version for s in history.stated["~00000"]],
      ["2026-01-11", "2026-04-08"],
    )
    self.assertEqual(
      [(rule.wording.text, pages) for rule, pages in history.moved_out],
      [(self.moved, ["docs/specification/overview.md"])],
    )
    self.assertEqual(
      dict(history.matched["2026-04-08"]),
      {build_catalog.SAME_WORDING: 1, build_catalog.LAST_STATED: 1},
    )
    self.assertEqual(
      dict(history.matched["2026-01-11"]),
      {
        build_catalog.REWORDED: 1,
        build_catalog.SAME_WORDING: 1,
        build_catalog.MOVED_OUT: 1,
      },
    )
    rule, key, newer, alike = history.reworded[0]
    self.assertEqual(
      (rule.wording.text, key, newer.text),
      (self.reworded, "REQ-SHOPPING-CHECKOUT-0001", self.kept),
    )
    self.assertGreaterEqual(alike, build_catalog.REWORD_SIMILARITY)

  def test_split(self):
    """Test that a split makes the older wording a rule of its own."""
    history = build_catalog.trace_history(
      VERSIONS,
      self.stated,
      self.current,
      self.outside,
      splits={"REQ-SHOPPING-CHECKOUT-0001": "2026-04-08"},
    )
    self.assertEqual(
      [s.version for s in history.stated["REQ-SHOPPING-CHECKOUT-0001"]],
      ["2026-04-08"],
    )
    self.assertEqual(len(history.removed), 2)
    self.assertEqual(history.split, {"REQ-SHOPPING-CHECKOUT-0001"})

  def test_split_that_changes_nothing(self):
    """Test that a split with no older wording to split off isn't noted."""
    history = build_catalog.trace_history(
      VERSIONS,
      self.stated,
      self.current,
      self.outside,
      splits={"REQ-SHOPPING-CHECKOUT-0001": "2026-01-11"},
    )
    self.assertEqual(history.split, set())


class AssignRemovedIdsTest(unittest.TestCase):
  """Tests for assign_removed_ids."""

  def test_reuses_and_mints(self):
    """Test a retired ID reused by wording, and a new one minted after it."""
    old = wording("must sign webhooks with a detached jws signature")
    entries = [
      {
        "id": "REQ-SHOPPING-CHECKOUT-0001",
        "file": CHECKOUT_PAGE,
        "text_digest": "aaaaaaaaaa",
        "context": None,
        "text": "must return the checkout",
        "previous_digests": [],
        "status": "active",
        "minted_at": COMMIT,
        "retired_at": None,
      },
      {
        "id": "REQ-SHOPPING-CHECKOUT-0002",
        "file": CHECKOUT_PAGE,
        "text_digest": old.text_digest,
        "context": None,
        "text": old.text,
        "previous_digests": [],
        "status": "retired",
        "minted_at": COMMIT,
        "retired_at": COMMIT,
      },
    ]
    before = json.dumps(entries)
    removed = [
      [stated("2026-01-11", old.text)],
      [
        stated("2026-01-11", "must answer within five seconds"),
        stated("2026-01-23", "must answer within ten seconds"),
      ],
    ]
    ids, after, minted = build_catalog.assign_removed_ids(
      removed, entries, COMMIT
    )
    self.assertEqual(
      ids, ["REQ-SHOPPING-CHECKOUT-0002", "REQ-SHOPPING-CHECKOUT-0003"]
    )
    self.assertEqual(minted, ["REQ-SHOPPING-CHECKOUT-0003"])
    self.assertEqual(json.dumps(entries), before)
    new = after[-1]
    self.assertEqual(
      (new["id"], new["status"], new["minted_at"], new["retired_at"]),
      ("REQ-SHOPPING-CHECKOUT-0003", "retired", COMMIT, COMMIT),
    )
    self.assertEqual(new["text"], "must answer within ten seconds")
    self.assertEqual(
      new["previous_digests"],
      [wording("must answer within five seconds").text_digest],
    )


class LifespanTest(unittest.TestCase):
  """Tests for lifespan, statements and removed_record."""

  versions = ("2026-01-11", "2026-01-23", "2026-04-08", "2026-08-25", "draft")

  def test_lifespan(self):
    """Test since and until, with None while the draft states the rule."""
    cases = {
      ("2026-01-11", "2026-01-23"): ("2026-01-11", "2026-04-08"),
      ("2026-08-25",): ("2026-08-25", "draft"),
      ("2026-04-08", "draft"): ("2026-04-08", None),
      ("draft",): ("draft", None),
    }
    for stated_in, expected in cases.items():
      with self.subTest(stated_in):
        self.assertEqual(
          build_catalog.lifespan(stated_in, self.versions), expected
        )

  def test_statements_and_removed_record(self):
    """Test that a statement gives its wording only when it differs."""
    rules = [
      stated("2026-01-11", "must answer within five seconds"),
      stated("2026-01-23", "must answer within ten seconds", line=7),
    ]
    entries = build_catalog.statements(rules, rules[1].wording.text_digest)
    self.assertEqual(
      entries[0]["clause"], "The business must answer within five seconds."
    )
    self.assertNotIn("clause", entries[1])
    self.assertEqual(
      (entries[1]["version"], entries[1]["file"], entries[1]["start_line"]),
      ("2026-01-23", "docs/specification/checkout.md", 7),
    )
    rules[1].record.update(
      id="REQ-POSITIONAL-01",
      text_digest=rules[1].wording.text_digest,
      source={"file": CHECKOUT_PAGE, "page": "x/"},
      demo={},
    )
    record = build_catalog.removed_record(
      rules, "REQ-SHOPPING-CHECKOUT-0009", self.versions, lambda f: f"p:{f}"
    )
    self.assertEqual(record["id"], "REQ-SHOPPING-CHECKOUT-0009")
    self.assertEqual(
      record["source"],
      {
        "file": "docs/specification/checkout.md",
        "page": "p:docs/specification/checkout.md",
      },
    )
    self.assertEqual(
      (record["demo"]["since"], record["demo"]["until"]),
      ("2026-01-11", "2026-04-08"),
    )
    self.assertEqual(len(record["demo"]["releases"]), 2)
    self.assertEqual(rules[1].record["id"], "REQ-POSITIONAL-01")


class InputsTest(unittest.TestCase):
  """Tests for load_releases and load_splits."""

  def write(self, document: dict[str, object]) -> Path:
    """Write a JSON file to a temporary directory and return its path."""
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    path = Path(directory.name) / "input.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path

  def test_load_releases(self):
    """Test a releases file, and the problems a malformed one has."""
    good = {
      "releases": [
        {"version": "2026-01-11", "ref": "release/2026-01-11", "commit": COMMIT}
      ],
      "flat_pages": {"checkout.md": "shopping/checkout/index.md"},
      "other_pages": ["overview.md"],
    }
    problems = []
    config = build_catalog.load_releases(self.write(good), problems)
    self.assertEqual(problems, [])
    self.assertEqual(config.versions, ("2026-01-11", "draft"))
    bad = {
      "releases": [
        {"version": "2026-04-08", "ref": "r", "commit": "abc"},
        {"version": "2026-01-11", "ref": "r", "commit": COMMIT},
      ],
      "flat_pages": {"overview.md": "overview/index.md"},
      "other_pages": [],
    }
    build_catalog.load_releases(self.write(bad), problems)
    self.assertEqual(len(problems), 3)
    problems.clear()
    build_catalog.load_releases(self.write({"releases": []}), problems)
    self.assertEqual(len(problems), 1)

  def test_load_splits(self):
    """Test the review's splits, and malformed ones."""
    problems = []
    splits = build_catalog.load_splits(
      self.write(
        {
          "decisions": [],
          "splits": [
            {
              "id": "REQ-SHOPPING-CHECKOUT-0001",
              "before": "2026-04-08",
              "reason": "the older wording allowed a 4xx",
            },
            {"id": "REQ-SHOPPING-CHECKOUT-0002", "before": "2026-04-08"},
            {"id": "not an ID", "before": "draft", "reason": "r"},
          ],
        }
      ),
      problems,
    )
    self.assertEqual(splits, {"REQ-SHOPPING-CHECKOUT-0001": "2026-04-08"})
    self.assertEqual(len(problems), 2)


class StageReleaseTest(unittest.TestCase):
  """Tests for stage_release, against a small git repository."""

  def setUp(self):
    """Commit a flat release layout to a new repository."""
    super().setUp()
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    self.root = Path(directory.name)
    self.repo = self.root / "repo"
    files = {
      "mkdocs.yml": "extra:\n  ucp_version: 2026-01-11\n",
      "docs/specification/checkout.md": "# Checkout\n",
      "docs/specification/overview.md": "# Overview\n",
      "docs/specification/mystery.md": "# Mystery\n",
      "source/schemas/shopping/checkout.json": "{}\n",
    }
    for name, text in files.items():
      path = self.repo / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(text, encoding="utf-8")
    self.git("init", "--quiet")
    self.git("add", ".")
    self.git(
      "-c",
      "user.name=Test",
      "-c",
      "user.email=test@example.com",
      "commit",
      "--quiet",
      "-m",
      "release",
    )
    self.commit = self.git("rev-parse", "HEAD").strip()

  def git(self, *args: str) -> str:
    """Run git in the test repository."""
    return subprocess.run(
      ["git", "-C", str(self.repo), *args],
      capture_output=True,
      text=True,
      check=True,
    ).stdout

  def test_moves_mapped_pages(self):
    """Test that shopping pages move to today's paths, and others are kept."""
    config = build_catalog.ReleaseConfig(
      releases=(build_catalog.Release("2026-01-11", "r", self.commit),),
      flat_pages={"checkout.md": "shopping/checkout/index.md"},
      other_pages=("over*.md",),
    )
    dest = self.root / "staged"
    dest.mkdir()
    moved, unknown = build_catalog.stage_release(
      self.repo, config.releases[0], config, dest
    )
    self.assertEqual(moved, {CHECKOUT_PAGE: "docs/specification/checkout.md"})
    self.assertEqual(unknown, ["mystery.md"])
    self.assertTrue((dest / CHECKOUT_PAGE).is_file())
    self.assertTrue((dest / "source/schemas/shopping/checkout.json").is_file())

  def test_missing_commit(self):
    """Test that a commit the clone doesn't have is an error."""
    release = build_catalog.Release("2026-01-11", "r", "0" * 40)
    with self.assertRaises(subprocess.CalledProcessError):
      build_catalog.stage_release(
        self.repo, release, build_catalog.ReleaseConfig(), self.root
      )


if __name__ == "__main__":
  unittest.main()
