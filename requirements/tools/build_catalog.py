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

# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "markdown-it-py==3.0.0",
#   "markdown==3.10.2",
#   "jsonschema==4.26.0",
# ]
# ///

"""Build the shopping requirements catalog this suite's markers cite.

This wraps the requirements extractor from the `.github` repository
(`org-tools/requirements-extractor`) and adds two options the extractor does
not have yet:

- a capability for every page, from its capability bullet or else its file
  path (a preview of the planned file-path rule); and
- counting upper-case RFC 2119 keywords that are not bold (a preview of
  counting capitals).

Every record gains a `demo` object, the fields this repository adds to the
extractor's record: how its capability was found, whether its keyword was
bold, and whether this suite can test it.

IDs are stable. Each rule's ID comes from `requirements.lock.json`, beside
the catalog, so it no longer changes when a rule is added above it; a
reworded rule keeps its ID, and a removed rule's ID is retired, never
reused. The extractor's positional ID is kept as `demo.positional_id`.

It also writes `capabilities.json`, an index of the capabilities in the
catalog: whether each is an extension, which capabilities it extends, its
schema and its pages. An extension's parents are read from its schema: the
spec requires a `$defs` entry, keyed by the parent's full name, for every
parent an extension declares.

Decisions in `requirements/review.json`, made by a person, override the
automatic testability and can mark a rule as not a rule.

It also records which releases state each rule. `requirements/releases.json`
lists the releases, each at a pinned commit; their shopping pages are read
from a clone of the specification repository and matched to the catalog's
rules by wording, newest release first. Each rule records the releases that
state it, from `demo.since` on. A rule only older releases state is a
removed rule: it gets an ID of its own, retired in the lock, and is written
to `history.json` with `demo.until`, the first version that doesn't state it.

Usage, from the repository root:

  uv run --no-project requirements/tools/build_catalog.py \
    --spec-root=<ucp checkout> --spec-commit=<commit> \
    --releases-repo=<ucp clone with the release commits> \
    --extractor-dir=<.github checkout>/org-tools/requirements-extractor/scripts

With `--check`, nothing is written, and the run fails if any output, the
lock included, would change.
"""

import argparse
import collections
from collections.abc import Callable, Iterator, Mapping, Sequence
import contextlib
import copy
import dataclasses
import difflib
import fnmatch
import hashlib
import importlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import ModuleType
from typing import Any

DOCS_ROOT = PurePosixPath("docs/specification")
SHOPPING_DIR = DOCS_ROOT / "shopping"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "requirements" / "catalog"
DEFAULT_REVIEW_FILE = REPO_ROOT / "requirements" / "review.json"
REPORT_FILE = "extraction_report.json"
INDEX_FILE = "capabilities.json"
LOCK_FILE = "requirements.lock.json"
OPTIONS = ("capability-from-bullet-or-path", "count-capitals")

# Stable IDs: one lock entry per ID ever issued, in ID order.
LOCK_VERSION = 1
LOCK_ABOUT = (
  "Stable IDs for the shopping requirements catalog: one entry for every ID "
  "ever issued. An ID never changes, and a retired ID's number is never "
  "issued again. See requirements/README.md."
)
LOCK_FIELDS = (
  "id",
  "file",
  "text_digest",
  "context",
  "text",
  "previous_digests",
  "status",
  "minted_at",
  "retired_at",
)
LOCK_STATUSES = ("active", "retired")
ID_NUMBER_WIDTH = 4
# The catalog schema's pattern for a requirement ID.
ID_RE = re.compile(r"^REQ-[A-Z0-9-]+-[0-9]{2,}$")
# How alike a rule's wording must be to that of a rule missing from the same
# page for the two to count as one rule, reworded.
REWORD_SIMILARITY = 0.85
# The summary lists changed IDs one by one when there are at most this many.
LIST_LIMIT = 20

# Release history.
DEFAULT_RELEASES_FILE = REPO_ROOT / "requirements" / "releases.json"
HISTORY_FILE = "history.json"
HISTORY_ABOUT = (
  "Which releases state each rule. A rule in the catalog lists them in "
  "demo.releases, from demo.since on. A rule that only older releases state "
  "is under removed, with demo.until, the first version that doesn't state "
  "it. See requirements/README.md."
)
# The pinned specification's version: newer than every release.
DRAFT = "draft"
# What is read from each release's commit.
RELEASE_PATHS = ("mkdocs.yml", DOCS_ROOT.as_posix(), "source/schemas")
VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# A rule is matched on its wording alone, under another list stem or none,
# only when the wording has at least this many words: a cell of the checkout
# obligation matrix reads just "must".
SAME_TEXT_MIN_WORDS = 5
# How a release's rule matched the next newer version, in the order the
# passes run; and the two outcomes for a rule that matched nothing.
SAME_WORDING = "same wording"
OTHER_PAGE = "same wording, other page"
OTHER_STEM = "same wording, other list stem"
REWORDED = "reworded"
REWORDED_OTHER_PAGE = "reworded, other page"
LAST_STATED = "not stated after this release"
MOVED_OUT = "moved out of the shopping pages"
MATCH_KINDS = (
  SAME_WORDING,
  OTHER_PAGE,
  OTHER_STEM,
  REWORDED,
  REWORDED_OTHER_PAGE,
  LAST_STATED,
  MOVED_OUT,
)

BOLD_KEYWORD_RE = re.compile(
  r"\*\*\s*(?:MUST\s+NOT|MUST|SHALL\s+NOT|SHALL|SHOULD\s+NOT|SHOULD|"
  r"NOT\s+RECOMMENDED|RECOMMENDED|REQUIRED|MAY|OPTIONAL)\s*\*\*"
)

# Whether this suite can test a rule. The suite plays the platform and calls a
# business over REST, so it can check what a business must do on a
# transport-neutral page or the REST binding page, and nothing else. A rule
# with no clear actor counts as testable, so coverage is not overstated.
BINDING_PAGES = {
  "rest.md": "rest",
  "mcp.md": "mcp",
  "a2a.md": "a2a",
  "embedded.md": "embedded",
}
UNTESTABLE_TRANSPORTS = {
  "mcp": "MCP binding page",
  "a2a": "A2A binding page",
  "embedded": "embedded protocol page",
}
TESTABLE_ACTORS = {
  "BUSINESS": "aimed at the business",
  None: "no actor detected",
  "IMPLEMENTATION": "implementation guidance",
  "EXTENSION": "attributed to the extension",
}
UNTESTABLE_ACTORS = {
  "PLATFORM": "aimed at the platform",
  "AGENT": "aimed at the agent",
  "HOST": "aimed at the host",
  "EMBEDDED_CHECKOUT": "aimed at the embedded checkout",
  "BUYER": "aimed at the buyer",
}
MUST_LEVELS = ("MUST", "MUST NOT")


def testability(record: dict[str, Any]) -> tuple[str, bool | None, str]:
  """Return a rule's transport, whether this suite can test it, and why.

  Returns None for testability when the rule's actor is not one this tool
  knows, which the caller reports as a problem.
  """
  transport = BINDING_PAGES.get(
    PurePosixPath(record["source"]["file"]).name, "core"
  )
  if transport in UNTESTABLE_TRANSPORTS:
    return transport, False, UNTESTABLE_TRANSPORTS[transport]
  actor = record["target_actor"]
  if actor in UNTESTABLE_ACTORS:
    return transport, False, UNTESTABLE_ACTORS[actor]
  if actor in TESTABLE_ACTORS:
    return transport, True, TESTABLE_ACTORS[actor]
  return transport, None, f"unknown actor {actor}"


@dataclasses.dataclass(frozen=True)
class PageLabel:
  """The capability label of one specification page.

  Attributes:
    capabilities: Capability names the page's rules belong to.
    source: How the label was found: "bullet", "path", "path (family)" or
      "unresolved".
    id_name: The name the extractor builds IDs from. For a folder of several
      capabilities this is the folder's shared prefix.

  """

  capabilities: tuple[str, ...]
  source: str
  id_name: str | None


@dataclasses.dataclass(frozen=True)
class CapabilitySchema:
  """What a capability's schema says about it.

  Attributes:
    file: The schema's path, relative to the spec root.
    defs_names: `$defs` keys that are capability names, in schema order.

  """

  file: str
  defs_names: tuple[str, ...]


def schema_capabilities(spec_root: Path) -> dict[str, CapabilitySchema]:
  """Return every capability a schema in the spec declares, by name."""
  schemas = {}
  for path in sorted((spec_root / "source" / "schemas").rglob("*.json")):
    try:
      document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
      continue
    name = document.get("name") if isinstance(document, dict) else None
    if isinstance(name, str) and name.startswith("dev.ucp."):
      schemas[name] = CapabilitySchema(
        file=path.relative_to(spec_root).as_posix(),
        defs_names=tuple(
          key for key in document.get("$defs", {}) if key.startswith("dev.ucp.")
        ),
      )
  return schemas


def parents_of(name: str, schemas: dict[str, CapabilitySchema]) -> list[str]:
  """Return the capabilities an extension extends, from its schema's `$defs`.

  A `$defs` key naming the capability itself is not a parent.
  """
  return sorted(
    key for key in schemas[name].defs_names if key in schemas and key != name
  )


def effective_level(record: dict[str, Any]) -> str | None:
  """Return a rule's level, as corrected by the review if it was."""
  return record["demo"].get("review", {}).get("level") or record["level"]


def is_active(record: dict[str, Any]) -> bool:
  """Whether a rule counts: false when the review found it is not a rule."""
  return not record["demo"].get("excluded")


def capability_index(
  schemas: dict[str, CapabilitySchema],
  labels: dict[str, PageLabel],
  records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
  """Describe every capability that has rules in the catalog.

  Rules the review found are not rules are left out of every count.

  Args:
    schemas: Capabilities declared by the schemas.
    labels: Each page's capability label.
    records: The catalog's records.

  Returns:
    One entry per capability, by name: its kind, the capabilities it
    extends and is extended by, its schema, and its pages with their rule
    counts.

  """
  records = [r for r in records if is_active(r)]
  per_page = collections.Counter(r["source"]["file"] for r in records)
  in_scope = sorted({c for r in records for c in r["capabilities"]})
  parents = {name: parents_of(name, schemas) for name in in_scope}
  entries = []
  for name in in_scope:
    pages = []
    for rel_path in sorted(labels):
      label = labels[rel_path]
      if name not in label.capabilities or not per_page[rel_path]:
        continue
      page = {"file": rel_path, "rules": per_page[rel_path]}
      shared = [c for c in label.capabilities if c != name]
      if shared:
        page["shared_with"] = shared
      pages.append(page)
    own = [r for r in records if name in r["capabilities"]]
    testable = [r for r in own if r["demo"]["testable"]]
    entries.append(
      {
        "name": name,
        "kind": "extension" if parents[name] else "capability",
        "extends": parents[name],
        "extended_by": sorted(e for e in in_scope if name in parents[e]),
        "schema": schemas[name].file,
        "rules": sum(page["rules"] for page in pages),
        "testable_rules": len(testable),
        "testable_must": sum(
          effective_level(r) in MUST_LEVELS for r in testable
        ),
        "pages": pages,
      }
    )
  return entries


def path_label(rel_path: str, names: set[str]) -> PageLabel:
  """Label a page from its file path, with hyphens read as underscores.

  - `<vertical>/extensions/<page>.md` belongs to `dev.ucp.<vertical>.<page>`;
  - a page named after a capability belongs to it
    (`shopping/catalog/search.md`);
  - a page in a capability's folder belongs to that capability
    (`shopping/cart/rest.md`);
  - a page in a folder of several capabilities belongs to all of them
    (`shopping/catalog/rest.md`).

  Every name produced is one a schema declares.

  Args:
    rel_path: Repository-relative POSIX path of the page.
    names: Capability names declared by the schemas.

  Returns:
    The label, with source "unresolved" when no rule applies.

  """
  parts = [
    part.replace("-", "_")
    for part in PurePosixPath(rel_path)
    .relative_to(DOCS_ROOT)
    .with_suffix("")
    .parts
  ]
  unresolved = PageLabel((), "unresolved", None)
  if len(parts) < 3:
    return unresolved
  vertical, folder, stem = parts[0], parts[1], parts[-1]
  if folder == "extensions":
    name = f"dev.ucp.{vertical}.{stem}"
    if len(parts) == 3 and name in names:
      return PageLabel((name,), "path", name)
    return unresolved
  exact = f"dev.ucp.{vertical}.{folder}.{stem}"
  if len(parts) == 3 and exact in names:
    return PageLabel((exact,), "path", exact)
  folder_name = f"dev.ucp.{vertical}.{folder}"
  if folder_name in names:
    return PageLabel((folder_name,), "path", folder_name)
  family = tuple(sorted(n for n in names if n.startswith(folder_name + ".")))
  if family:
    return PageLabel(family, "path (family)", folder_name)
  return unresolved


class DemoResolver:
  """Resolve each page's capability once: the bullet first, then the path."""

  def __init__(
    self, declared: Callable[[Path], str | None], names: set[str]
  ) -> None:
    """Initialize the resolver.

    Args:
      declared: The extractor's reader for a page's capability bullet.
      names: Capability names declared by the schemas.

    """
    self._declared = declared
    self._names = names
    self.labels: dict[str, PageLabel] = {}
    self.problems: list[str] = []

  def __call__(self, rel_path: str, abs_path: Path | None = None) -> str | None:
    """Return the name the extractor should build this page's IDs from."""
    if rel_path not in self.labels:
      self.labels[rel_path] = self._label(rel_path, abs_path)
    return self.labels[rel_path].id_name

  def _label(self, rel_path: str, abs_path: Path | None) -> PageLabel:
    declared = self._declared(abs_path) if abs_path is not None else None
    from_path = path_label(rel_path, self._names)
    if not declared:
      return from_path
    if declared not in self._names:
      self.problems.append(
        f"{rel_path}: the bullet names {declared}, which no schema declares"
      )
    if from_path.capabilities and declared not in from_path.capabilities:
      self.problems.append(
        f"{rel_path}: the bullet says {declared} but the file path says "
        + ", ".join(from_path.capabilities)
      )
    return PageLabel((declared,), "bullet", declared)


@contextlib.contextmanager
def replaced(module: ModuleType, name: str, value: Any) -> Iterator[None]:
  """Temporarily replace a module attribute."""
  original = getattr(module, name)
  setattr(module, name, value)
  try:
    yield
  finally:
    setattr(module, name, original)


def keyword_style(clause: str) -> str:
  """Return "bold" if the clause states its keyword in bold, else "capitals"."""
  return "bold" if BOLD_KEYWORD_RE.search(clause) else "capitals"


def git_commit(path: Path) -> tuple[str, bool]:
  """Return the commit a directory is at, and whether it has changes."""
  commit = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "HEAD"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  status = subprocess.run(
    ["git", "-C", str(path), "status", "--porcelain", "--", "."],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  return commit, bool(status)


def load_extractor(extractor_dir: Path) -> dict[str, ModuleType]:
  """Import the extractor modules from a `.github` checkout."""
  sys.path.insert(0, str(extractor_dir))
  return {
    name: importlib.import_module(f"requirements_extractor.{name}")
    for name in ("catalog", "classifier", "config", "identity")
  }


REVIEW_VERDICTS = ("rule", "not a rule")
REVIEW_OPTIONAL = ("level", "note", "needs_your_call")


def load_review(
  path: Path, problems: list[str]
) -> dict[tuple[str, str], dict[str, Any]]:
  """Load review decisions, keyed by page and text digest.

  The digest moves only when a rule's wording does, so a decision survives
  ID renumbering and lapses, visibly, when the rule is reworded. A sentence
  repeated word for word on one page shares a digest, so its copies share a
  key: their decisions must agree.

  Args:
    path: The review file; a missing file means no decisions.
    problems: Collects malformed entries and conflicting decisions.

  Returns:
    The decisions by (file, text_digest).

  """
  if not path.exists():
    return {}
  decisions = {}
  compared = ("verdict", "actor", "testable", "reason", "level")
  for entry in json.loads(path.read_text(encoding="utf-8"))["decisions"]:
    key = (entry["file"], entry["text_digest"])
    if entry["verdict"] not in REVIEW_VERDICTS:
      problems.append(f"review: unknown verdict for {entry['id_at_review']}")
    earlier = decisions.get(key)
    if earlier and any(earlier.get(k) != entry.get(k) for k in compared):
      problems.append(
        f"review: conflicting decisions for repeated wording "
        f"({earlier['id_at_review']}, {entry['id_at_review']})"
      )
    decisions.setdefault(key, entry)
  return decisions


def apply_review(record: dict[str, Any], decision: dict[str, Any]) -> None:
  """Record a review decision on a rule and let it override testability.

  A rule found not to be a rule stays in the catalog, flagged as excluded,
  so the decision is traceable; it is left out of every count.
  """
  review = {k: decision[k] for k in ("verdict", "actor", "testable", "reason")}
  review.update({k: decision[k] for k in REVIEW_OPTIONAL if k in decision})
  demo = record["demo"]
  demo["review"] = review
  if decision["verdict"] == "not a rule":
    demo["excluded"] = True
    demo["testable"] = False
    demo["testable_reason"] = "not a rule (reviewed): " + decision["reason"]
  else:
    demo["testable"] = decision["testable"]
    demo["testable_reason"] = "reviewed: " + decision["reason"]


@dataclasses.dataclass(frozen=True)
class LockRule:
  """What the lock needs to know about one rule.

  Attributes:
    prefix: The ID prefix for the rule's page, such as `REQ-SHOPPING-CART`.
    file: The rule's page.
    text_digest: The rule's text digest.
    context: The rule's normalized compound parent, if it has one.
    text: The rule's normalized wording.
    position: Where the rule sits on its page: its first line, then the
      extractor's document order.

  """

  prefix: str
  file: str
  text_digest: str
  context: str | None
  text: str
  position: tuple[int, int]


@dataclasses.dataclass
class LockResult:
  """The outcome of giving every rule a stable ID.

  Attributes:
    ids: Each rule's stable ID, in the order the rules were given.
    entries: Every lock entry after the run, in ID order.
    kept: IDs whose rule was found word for word.
    carried: IDs whose rule was found reworded, each with the similarity and
      the wording before.
    minted: New IDs.
    retired: IDs whose rule is gone.
    restored: Retired IDs whose rule is back, word for word.

  """

  ids: list[str] = dataclasses.field(default_factory=list)
  entries: list[dict[str, Any]] = dataclasses.field(default_factory=list)
  kept: list[str] = dataclasses.field(default_factory=list)
  carried: list[tuple[str, float, str]] = dataclasses.field(
    default_factory=list
  )
  minted: list[str] = dataclasses.field(default_factory=list)
  retired: list[str] = dataclasses.field(default_factory=list)
  restored: list[str] = dataclasses.field(default_factory=list)


def split_id(rule_id: str) -> tuple[str, int]:
  """Split a stable ID into its prefix and its number."""
  prefix, _, number = rule_id.rpartition("-")
  return prefix, int(number)


def match_text(context: str | None, text: str) -> str:
  """Return the wording compared when looking for a reworded rule.

  The context is included because the wording alone can be a single word:
  the cells of the checkout obligation matrix read "must" or "may".
  """
  return f"{context}\n{text}" if context else text


def mint_order(rule: LockRule) -> tuple[Any, ...]:
  """Return the order new IDs go out in: core page first, then page order."""
  return (
    rule.prefix,
    PurePosixPath(rule.file).name != "index.md",
    rule.file,
    rule.position,
  )


def assign_stable_ids(
  rules: list[LockRule], lock: list[dict[str, Any]], spec_commit: str
) -> LockResult:
  """Give every rule a stable ID from the lock, issuing new IDs as needed.

  Four passes:

  1. Exact: a rule takes the ID of an entry on the same page with the same
     text digest. Copies of one sentence on a page take that wording's IDs
     oldest first, in page order. A retired entry can match, which restores
     its ID.
  2. Reworded: a rule still without an ID takes the ID of an unclaimed
     active entry on the same page whose wording is at least
     `REWORD_SIMILARITY` alike, the most alike pairs first. The entry keeps
     its old digest in `previous_digests`.
  3. New: every rule still without an ID gets the next number for its
     prefix, in `mint_order`.
  4. Retired: an active entry no rule claimed is retired. Its number is
     never issued again.

  Args:
    rules: The catalog's rules.
    lock: The lock's entries. They are not modified.
    spec_commit: The specification commit, recorded on new and retired
      entries.

  Returns:
    The IDs, the updated entries and what changed.

  """
  entries = {entry["id"]: copy.deepcopy(entry) for entry in lock}
  ids: list[str | None] = [None] * len(rules)
  claimed: set[str] = set()
  result = LockResult()
  page_order = sorted(
    range(len(rules)), key=lambda i: (rules[i].file, rules[i].position)
  )

  # 1. Exact.
  by_wording = collections.defaultdict(list)
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    by_wording[entry["file"], entry["text_digest"]].append(rule_id)
  for i in page_order:
    for rule_id in by_wording[rules[i].file, rules[i].text_digest]:
      if rule_id in claimed:
        continue
      claimed.add(rule_id)
      ids[i] = rule_id
      entry = entries[rule_id]
      if entry["status"] == "retired":
        entry["status"], entry["retired_at"] = "active", None
        result.restored.append(rule_id)
      else:
        result.kept.append(rule_id)
      break

  # 2. Reworded.
  open_entries = collections.defaultdict(list)
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    if rule_id not in claimed and entry["status"] == "active":
      open_entries[entry["file"]].append(rule_id)
  pairs = []
  for rank, i in enumerate(page_order):
    if ids[i] is not None:
      continue
    wording = match_text(rules[i].context, rules[i].text)
    for rule_id in open_entries[rules[i].file]:
      entry = entries[rule_id]
      matcher = difflib.SequenceMatcher(
        None,
        match_text(entry["context"], entry["text"]),
        wording,
        autojunk=False,
      )
      if (
        matcher.real_quick_ratio() >= REWORD_SIMILARITY
        and matcher.quick_ratio() >= REWORD_SIMILARITY
        and (similarity := matcher.ratio()) >= REWORD_SIMILARITY
      ):
        pairs.append((-similarity, split_id(rule_id), rank, i, rule_id))
  for negative_similarity, _, _, i, rule_id in sorted(pairs):
    if ids[i] is not None or rule_id in claimed:
      continue
    claimed.add(rule_id)
    ids[i] = rule_id
    entry = entries[rule_id]
    result.carried.append((rule_id, -negative_similarity, entry["text"]))
    entry["previous_digests"].append(entry["text_digest"])
    entry["text_digest"] = rules[i].text_digest
    entry["context"] = rules[i].context
    entry["text"] = rules[i].text

  # 3. New.
  highest: collections.Counter[str] = collections.Counter()
  for rule_id in entries:
    prefix, number = split_id(rule_id)
    highest[prefix] = max(highest[prefix], number)
  unmatched = [i for i in range(len(rules)) if ids[i] is None]
  for i in sorted(unmatched, key=lambda i: mint_order(rules[i])):
    rule = rules[i]
    highest[rule.prefix] += 1
    rule_id = f"{rule.prefix}-{highest[rule.prefix]:0{ID_NUMBER_WIDTH}d}"
    entries[rule_id] = {
      "id": rule_id,
      "file": rule.file,
      "text_digest": rule.text_digest,
      "context": rule.context,
      "text": rule.text,
      "previous_digests": [],
      "status": "active",
      "minted_at": spec_commit,
      "retired_at": None,
    }
    claimed.add(rule_id)
    ids[i] = rule_id
    result.minted.append(rule_id)

  # 4. Retired.
  for rule_id in sorted(entries, key=split_id):
    entry = entries[rule_id]
    if rule_id not in claimed and entry["status"] == "active":
      entry["status"], entry["retired_at"] = "retired", spec_commit
      result.retired.append(rule_id)

  result.ids = [rule_id or "" for rule_id in ids]
  result.entries = [entries[k] for k in sorted(entries, key=split_id)]
  return result


def load_lock(
  path: Path,
  digest_of: Callable[[str | None, str], str],
  problems: list[str],
) -> list[dict[str, Any]]:
  """Load the lock's entries and check that they are well formed.

  Args:
    path: The lock file. A missing file means no ID has been issued yet.
    digest_of: Computes a text digest from a context and a wording.
    problems: Collects anything malformed.

  Returns:
    The entries, as stored.

  """
  if not path.exists():
    return []
  document = json.loads(path.read_text(encoding="utf-8"))
  if document.get("lock_version") != LOCK_VERSION:
    problems.append(
      f"lock: unsupported lock_version {document.get('lock_version')!r}"
    )
    return []
  entries = document.get("entries", [])
  seen = set()
  for entry in entries:
    rule_id = str(entry.get("id"))
    if sorted(entry) != sorted(LOCK_FIELDS):
      problems.append(f"lock: {rule_id} does not have the expected fields")
      continue
    if not ID_RE.match(rule_id) or rule_id in seen:
      problems.append(f"lock: {rule_id} is malformed or repeated")
    seen.add(rule_id)
    retired = entry["status"] == "retired"
    if entry["status"] not in LOCK_STATUSES or retired != bool(
      entry["retired_at"]
    ):
      problems.append(f"lock: {rule_id} has an inconsistent status")
    if digest_of(entry["context"], entry["text"]) != entry["text_digest"]:
      problems.append(f"lock: {rule_id}'s wording does not match its digest")
  return entries


def lock_problems(lock: list[dict[str, Any]], result: LockResult) -> list[str]:
  """Check what giving out stable IDs must never do.

  Every ID ever issued stays in the lock; a new ID's number is above every
  number its prefix has used, retired ones included; and every rule has an
  ID that fits the catalog schema's pattern and an active entry of its own.
  """
  problems = []
  dropped = sorted({e["id"] for e in lock} - {e["id"] for e in result.entries})
  if dropped:
    problems.append(f"lock: IDs dropped: {dropped[:5]}")
  highest: collections.Counter[str] = collections.Counter()
  for entry in lock:
    prefix, number = split_id(entry["id"])
    highest[prefix] = max(highest[prefix], number)
  reused = [
    rule_id
    for rule_id in result.minted
    if split_id(rule_id)[1] <= highest[split_id(rule_id)[0]]
  ]
  if reused:
    problems.append(f"lock: numbers issued again: {reused[:5]}")
  malformed = [rule_id for rule_id in result.ids if not ID_RE.match(rule_id)]
  if malformed:
    problems.append(f"IDs that do not fit the schema: {malformed[:5]}")
  active = sum(entry["status"] == "active" for entry in result.entries)
  if active != len(result.ids):
    problems.append(f"lock: {active} active IDs for {len(result.ids)} rules")
  return problems


def lock_document(entries: list[dict[str, Any]]) -> dict[str, Any]:
  """Return the lock file's contents, each entry's fields in a fixed order."""
  return {
    "about": LOCK_ABOUT,
    "lock_version": LOCK_VERSION,
    "entries": [{field: e[field] for field in LOCK_FIELDS} for e in entries],
  }


def without_demo(document: dict[str, Any]) -> dict[str, Any]:
  """Return a catalog document without the demo's additions."""
  return {
    **{k: v for k, v in document.items() if k != "demo"},
    "requirements": [
      {k: v for k, v in record.items() if k != "demo"}
      for record in document["requirements"]
    ],
  }


@dataclasses.dataclass
class Extraction:
  """What one run of the extractor found in a specification.

  Attributes:
    schemas: The capabilities the specification's schemas declare.
    resolver: Each page's capability label, and problems found labelling.
    documents: One catalog document per page, by output path.
    report_document: The extractor's report.
    bold_only: The rules found when only bold keywords count, if asked for.

  """

  schemas: dict[str, CapabilitySchema]
  resolver: DemoResolver
  documents: dict[Any, dict[str, Any]]
  report_document: dict[str, Any]
  bold_only: list[Any]


def extract(
  modules: dict[str, ModuleType],
  spec_root: Path,
  spec_dir: PurePosixPath,
  *,
  bold_only: bool = False,
) -> Extraction:
  """Run the extractor over one folder of a specification.

  With the demo's two options: every page gets a capability from its bullet
  or its file path, and upper-case keywords count whether bold or not.

  Args:
    modules: The extractor's modules.
    spec_root: The specification checkout.
    spec_dir: The folder to read, relative to `spec_root`.
    bold_only: Also run counting only bold keywords, as the extractor does.

  Returns:
    What the extractor found.

  """
  catalog, classifier, config = (
    modules["catalog"],
    modules["classifier"],
    modules["config"],
  )
  schemas = schema_capabilities(spec_root)
  resolver = DemoResolver(config.declared_capability, set(schemas))
  original_find = classifier.find_keywords

  def find_all_keywords(text: str, keywords: tuple[str, ...]) -> list[Any]:
    return [
      dataclasses.replace(hit, bolded=True)
      for hit in original_find(text, keywords)
    ]

  with (
    replaced(config, "SPEC_DIRS", [Path(spec_dir)]),
    replaced(config, "resolve_capability", resolver),
  ):
    bold = catalog.build(spec_root)[0] if bold_only else []
    with replaced(classifier, "find_keywords", find_all_keywords):
      requirements, report = catalog.build(spec_root)
    documents = catalog.catalog_documents(requirements, report, spec_root)
    report_document = catalog.report_document(requirements, report, spec_root)
  return Extraction(schemas, resolver, documents, report_document, bold)


def annotate(record: dict[str, Any], label: PageLabel) -> None:
  """Give a rule its page's capabilities and the demo's `demo` object."""
  if record["capabilities"]:
    record["capabilities"] = list(label.capabilities)
  transport, testable, reason = testability(record)
  record["demo"] = {
    "positional_id": record["id"],
    "capability_source": label.source,
    "keyword_style": keyword_style(record["clause"]),
    "transport": transport,
    "testable": testable,
    "testable_reason": reason,
  }


def in_shopping(file: str) -> bool:
  """Return whether a page is in the shopping folder."""
  return file.startswith(f"{SHOPPING_DIR}/")


@dataclasses.dataclass(frozen=True)
class Release:
  """A release of the specification, read at a pinned commit.

  Attributes:
    version: The release, such as "2026-04-08".
    ref: The branch it was pinned from.
    commit: The commit it is read at.

  """

  version: str
  ref: str
  commit: str


@dataclasses.dataclass(frozen=True)
class ReleaseConfig:
  """The releases the history is read from, and where their pages are.

  Attributes:
    releases: The releases, oldest first.
    flat_pages: For a release without a shopping folder, each shopping
      page's path today, by its path in the release; both relative to the
      specification's docs.
    other_pages: Patterns for that release's pages outside the shopping
      vertical.

  """

  releases: tuple[Release, ...] = ()
  flat_pages: Mapping[str, str] = dataclasses.field(default_factory=dict)
  other_pages: tuple[str, ...] = ()

  @property
  def versions(self) -> tuple[str, ...]:
    """Return every version, oldest first: the releases, then the draft."""
    return (*(release.version for release in self.releases), DRAFT)


def load_releases(path: Path, problems: list[str]) -> ReleaseConfig:
  """Load the releases the history is read from, and check them.

  Args:
    path: The releases file.
    problems: Collects anything malformed.

  Returns:
    The releases and the page map; none when the file can't be read.

  """
  try:
    document = json.loads(path.read_text(encoding="utf-8"))
    config = ReleaseConfig(
      releases=tuple(
        Release(entry["version"], entry["ref"], entry["commit"])
        for entry in document["releases"]
      ),
      flat_pages=dict(document["flat_pages"]),
      other_pages=tuple(document["other_pages"]),
    )
  except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
    problems.append(f"releases: cannot read {path}: {error}")
    return ReleaseConfig()
  versions = [release.version for release in config.releases]
  if not all(VERSION_RE.match(v) for v in versions) or versions != sorted(
    set(versions)
  ):
    problems.append(
      f"releases: versions must be distinct dates, oldest first: {versions}"
    )
  short = [r.version for r in config.releases if not COMMIT_RE.match(r.commit)]
  if short:
    problems.append(f"releases: commits must be full hashes: {short}")
  elsewhere = sorted(
    page
    for page in config.flat_pages.values()
    if not page.startswith(f"{SHOPPING_DIR.name}/")
  )
  if elsewhere:
    problems.append(
      f"releases: flat pages mapped outside the shopping folder: {elsewhere}"
    )
  return config


def load_splits(path: Path, problems: list[str]) -> dict[str, str]:
  """Load the review's splits: older wordings found to be other rules.

  A split says that releases before a version state another rule in the
  wording that matched an ID. That wording becomes a removed rule of its
  own, and the ID's history starts at the version.

  Args:
    path: The review file; a missing file, or one without splits, has none.
    problems: Collects malformed splits.

  Returns:
    For each split ID, the first version whose wording is that rule.

  """
  if not path.exists():
    return {}
  splits: dict[str, str] = {}
  document = json.loads(path.read_text(encoding="utf-8"))
  for entry in document.get("splits", []):
    rule_id, first = entry.get("id"), entry.get("before")
    if (
      not isinstance(rule_id, str)
      or not ID_RE.match(rule_id)
      or rule_id in splits
      or not (first == DRAFT or VERSION_RE.match(str(first)))
      or not entry.get("reason")
    ):
      problems.append(f"review: malformed split {entry!r}")
      continue
    splits[rule_id] = first
  return splits


def stage_release(
  repo: Path, release: Release, config: ReleaseConfig, dest: Path
) -> tuple[dict[str, str], list[str]]:
  """Write a release's pages and schemas, shopping pages at today's paths.

  A release with a shopping folder is written as it is. In one without, each
  page in `config.flat_pages` moves to its path today, so its rules get the
  capability and testability the pinned specification's page gives them.

  Args:
    repo: A clone of the specification repository with the release's commit.
    release: The release.
    config: The page map.
    dest: An empty directory to write to.

  Returns:
    Each moved page's path in the release, by its path today; and the
    release's pages that are neither in the page map nor other pages.

  Raises:
    subprocess.CalledProcessError: If the clone doesn't have the commit.

  """
  archive = subprocess.run(
    [
      "git",
      "-C",
      str(repo),
      "archive",
      "--format=tar",
      release.commit,
      *RELEASE_PATHS,
    ],
    capture_output=True,
    check=True,
  ).stdout
  with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(dest, filter="data")
  docs = dest / DOCS_ROOT
  if (docs / SHOPPING_DIR.name).is_dir():
    return {}, []
  moved: dict[str, str] = {}
  unknown: list[str] = []
  for path in sorted(docs.rglob("*.md")):
    page = path.relative_to(docs).as_posix()
    if page in config.flat_pages:
      target = docs / config.flat_pages[page]
      target.parent.mkdir(parents=True, exist_ok=True)
      path.rename(target)
      moved[(DOCS_ROOT / config.flat_pages[page]).as_posix()] = (
        DOCS_ROOT / page
      ).as_posix()
    elif not any(
      fnmatch.fnmatchcase(page, pattern) for pattern in config.other_pages
    ):
      unknown.append(page)
  return moved, unknown


@dataclasses.dataclass(frozen=True)
class Wording:
  """A rule's wording, as the history compares it.

  Attributes:
    file: The rule's page, at its path in the pinned specification.
    text_digest: The digest of its context and wording.
    context: Its normalized compound parent, if it has one.
    text: Its normalized wording.

  """

  file: str
  text_digest: str
  context: str | None
  text: str

  @property
  def compared(self) -> str:
    """Return what is compared to find a rewording."""
    return match_text(self.context, self.text)


def wording_of(
  record: dict[str, Any], normalize: Callable[[str], str]
) -> Wording:
  """Return the wording of an extracted rule."""
  parent = record["compound_parent"]
  return Wording(
    file=record["source"]["file"],
    text_digest=record["text_digest"],
    context=normalize(parent) if parent else None,
    text=record["normalized"],
  )


@dataclasses.dataclass(frozen=True)
class Stated:
  """A rule as one release states it.

  Attributes:
    version: The release.
    wording: The rule's wording, with its page at the path it has today.
    position: Where it sits on its page: its first line, then the
      extractor's document order.
    file: Its page, at the path the release gives it.
    level: Its level, as corrected by the review if it was.
    prefix: The ID prefix for its page.
    record: Its catalog record from the release.

  """

  version: str
  wording: Wording
  position: tuple[int, int]
  file: str
  level: str
  prefix: str
  record: dict[str, Any] = dataclasses.field(
    default_factory=dict, compare=False, repr=False
  )


def similarity(before: str, after: str) -> float:
  """Return how alike two wordings are, or 0 under `REWORD_SIMILARITY`."""
  matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
  if (
    matcher.real_quick_ratio() < REWORD_SIMILARITY
    or matcher.quick_ratio() < REWORD_SIMILARITY
  ):
    return 0.0
  ratio = matcher.ratio()
  return ratio if ratio >= REWORD_SIMILARITY else 0.0


def match_release(
  stated: Sequence[Stated],
  candidates: Mapping[str, Wording],
  related: Callable[[str, str], bool] | None = None,
) -> list[tuple[str, str] | None]:
  """Match a release's rules to the rules of the next newer version.

  Five passes, each giving a rule at most one candidate and a candidate at
  most one rule; rules go in page order and candidates in key order:

  1. the same wording on the same page;
  2. the same wording on another, related page, for a rule that moved;
  3. the same wording under another list stem, or none, on the same page,
     for a wording of at least `SAME_TEXT_MIN_WORDS` words;
  4. a wording at least `REWORD_SIMILARITY` alike on the same page, most
     alike first;
  5. the same, on another related page.

  Args:
    stated: The release's rules.
    candidates: The rules of newer versions, by key, each with its wording
      in the next newer version that states it.
    related: Whether a rule can move between two pages, such as two pages
      of one capability. By default it can move between any two.

  Returns:
    For each of the release's rules, the key it matched and how, or None.

  """
  found: list[tuple[str, str] | None] = [None] * len(stated)
  claimed: set[str] = set()
  order = sorted(
    range(len(stated)),
    key=lambda i: (stated[i].wording.file, stated[i].position),
  )
  keys = sorted(candidates)

  def take(i: int, key: str, kind: str) -> None:
    found[i] = (key, kind)
    claimed.add(key)

  def allowed(key: str, wording: Wording, same_page: bool) -> bool:
    page = candidates[key].file
    if same_page:
      return page == wording.file
    return page != wording.file and (
      related is None or related(page, wording.file)
    )

  by_digest = collections.defaultdict(list)
  for key in keys:
    by_digest[candidates[key].text_digest].append(key)
  for same_page, kind in ((True, SAME_WORDING), (False, OTHER_PAGE)):
    for i in order:
      if found[i] is not None:
        continue
      wording = stated[i].wording
      for key in by_digest[wording.text_digest]:
        if key not in claimed and allowed(key, wording, same_page):
          take(i, key, kind)
          break
  for i in order:
    wording = stated[i].wording
    if found[i] is not None or len(wording.text.split()) < SAME_TEXT_MIN_WORDS:
      continue
    for key in keys:
      if (
        key not in claimed
        and allowed(key, wording, same_page=True)
        and candidates[key].text == wording.text
      ):
        take(i, key, OTHER_STEM)
        break
  compared = {key: candidates[key].compared for key in keys}
  for same_page, kind in ((True, REWORDED), (False, REWORDED_OTHER_PAGE)):
    pairs = []
    for rank, i in enumerate(order):
      if found[i] is not None:
        continue
      wording = stated[i].wording
      for key in keys:
        if key in claimed or not allowed(key, wording, same_page):
          continue
        alike = similarity(compared[key], wording.compared)
        if alike:
          pairs.append((-alike, key, rank, i))
    for _, key, _, i in sorted(pairs):
      if found[i] is None and key not in claimed:
        take(i, key, kind)
  return found


def moved_to(wording: Wording, outside: Sequence[Wording]) -> list[str]:
  """Return the pages outside the shopping pages that state a wording now.

  Every page with the same wording counts; failing that, the page of the
  most alike wording, if one is at least `REWORD_SIMILARITY` alike.

  Args:
    wording: A release's rule that no newer shopping rule matched.
    outside: The rules the pinned specification states outside the
      shopping pages.

  Returns:
    The pages, in path order; empty if none states the wording.

  """
  long_enough = len(wording.text.split()) >= SAME_TEXT_MIN_WORDS
  same = {
    other.file
    for other in outside
    if other.text_digest == wording.text_digest
    or (long_enough and other.text == wording.text)
  }
  if same:
    return sorted(same)
  alike, page = max(
    (
      (similarity(other.compared, wording.compared), other.file)
      for other in outside
    ),
    default=(0.0, ""),
  )
  return [page] if alike else []


@dataclasses.dataclass
class History:
  """Which releases state each rule.

  Attributes:
    stated: For each rule, how each release that states it states it,
      oldest first. A rule of the pinned specification is keyed by its ID;
      a removed rule by its key in `removed`.
    matched: For each release, how many of its rules matched each way, by
      the kinds in `MATCH_KINDS`.
    removed: The keys of rules no newer version states, in the order found:
      newest release first.
    moved_out: Rules of a release that the pinned specification states
      outside the shopping pages, each with the pages that state it now.
    reworded: Matches on a reworded wording, for a person to check: the
      release's rule, the key it matched, the newer wording, and how alike
      the two are.
    split: The IDs whose split took effect.

  """

  stated: dict[str, list[Stated]] = dataclasses.field(
    default_factory=lambda: collections.defaultdict(list)
  )
  matched: dict[str, collections.Counter[str]] = dataclasses.field(
    default_factory=dict
  )
  removed: list[str] = dataclasses.field(default_factory=list)
  moved_out: list[tuple[Stated, list[str]]] = dataclasses.field(
    default_factory=list
  )
  reworded: list[tuple[Stated, str, Wording, float]] = dataclasses.field(
    default_factory=list
  )
  split: set[str] = dataclasses.field(default_factory=set)


def trace_history(
  versions: Sequence[str],
  stated: Mapping[str, Sequence[Stated]],
  current: Mapping[str, Wording],
  outside: Sequence[Wording],
  splits: Mapping[str, str] | None = None,
  related: Callable[[str, str], bool] | None = None,
) -> History:
  """Trace the catalog's rules back through the releases, newest first.

  Each release's rules are matched by `match_release` to the wording each
  rule has in the next newer version that states it, starting with the
  pinned specification. A rule that matches none moved out of the shopping
  pages if the pinned specification states it outside them. Otherwise it is
  a removed rule, which older releases are matched to in turn.

  Args:
    versions: Every version, oldest first, ending with `DRAFT`.
    stated: Each release's rules, by version.
    current: The wording of each rule of the pinned specification, by ID.
    outside: The rules the pinned specification states outside the
      shopping pages.
    splits: For an ID whose older wording the review found is another rule,
      the first version whose wording is this rule.
    related: Whether a rule can move between two pages; see
      `match_release`.

  Returns:
    The history.

  """
  splits = splits or {}
  history = History()
  candidates = dict(current)
  for version in reversed(versions[:-1]):
    index = versions.index(version)
    blocked = {
      rule_id
      for rule_id, first in splits.items()
      if index < versions.index(first) and rule_id in candidates
    }
    rules = stated[version]
    found = match_release(
      rules,
      {key: w for key, w in candidates.items() if key not in blocked},
      related,
    )
    counts: collections.Counter[str] = collections.Counter()
    keyed: list[tuple[Stated, str]] = []
    for rule, match in zip(rules, found, strict=True):
      if match is None:
        pages = moved_to(rule.wording, outside)
        if pages:
          history.moved_out.append((rule, pages))
          counts[MOVED_OUT] += 1
          continue
        key, kind = f"~{len(history.removed):05d}", LAST_STATED
        history.removed.append(key)
      else:
        key, kind = match
      counts[kind] += 1
      if kind in (REWORDED, REWORDED_OTHER_PAGE):
        newer = candidates[key]
        alike = similarity(newer.compared, rule.wording.compared)
        history.reworded.append((rule, key, newer, alike))
      keyed.append((rule, key))
    for rule_id in blocked:
      if index + 1 == versions.index(splits[rule_id]) and any(
        match_release(rules, {rule_id: candidates[rule_id]}, related)
      ):
        history.split.add(rule_id)
    for rule, key in keyed:
      history.stated[key].insert(0, rule)
      candidates[key] = rule.wording
    history.matched[version] = counts
  return history


def stated_order(stated: Stated) -> tuple[Any, ...]:
  """Return the order new IDs go out in: core page first, then page order."""
  file = stated.wording.file
  return (
    stated.prefix,
    PurePosixPath(file).name != "index.md",
    file,
    stated.position,
  )


def assign_removed_ids(
  removed: Sequence[Sequence[Stated]],
  entries: Sequence[dict[str, Any]],
  spec_commit: str,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
  """Give each removed rule a stable ID.

  A removed rule takes the ID of a retired lock entry that has one of its
  wordings, on its newest page first; failing that, of one whose wording is
  at least `REWORD_SIMILARITY` alike to its newest, most alike first.
  Otherwise it gets the next number for its prefix, in a new entry retired
  from the start: the pinned specification doesn't state it. Rules go in
  the order `stated_order` gives their newest statements.

  Args:
    removed: Each removed rule's statements, oldest first.
    entries: The lock's entries. They are not modified.
    spec_commit: The pinned specification's commit, for new entries.

  Returns:
    Each removed rule's ID; the lock's entries after, in ID order; and the
    new IDs.

  """
  entries = [copy.deepcopy(entry) for entry in entries]
  retired = sorted(
    (entry for entry in entries if entry["status"] == "retired"),
    key=lambda entry: split_id(entry["id"]),
  )
  ids: list[str | None] = [None] * len(removed)
  claimed: set[str] = set()
  order = sorted(
    range(len(removed)), key=lambda i: stated_order(removed[i][-1])
  )
  for same_page in (True, False):
    for i in order:
      if ids[i] is not None:
        continue
      newest = removed[i][-1].wording
      digests = {statement.wording.text_digest for statement in removed[i]}
      for entry in retired:
        known = {entry["text_digest"], *entry["previous_digests"]}
        on_page = entry["file"] == newest.file
        if (
          entry["id"] not in claimed
          and on_page == same_page
          and digests & known
        ):
          ids[i] = entry["id"]
          claimed.add(entry["id"])
          break
  for same_page in (True, False):
    pairs = []
    for rank, i in enumerate(order):
      if ids[i] is not None:
        continue
      newest = removed[i][-1].wording
      for entry in retired:
        on_page = entry["file"] == newest.file
        if entry["id"] in claimed or on_page != same_page:
          continue
        alike = similarity(
          match_text(entry["context"], entry["text"]), newest.compared
        )
        if alike:
          pairs.append((-alike, split_id(entry["id"]), rank, i, entry["id"]))
    for *_, i, rule_id in sorted(pairs):
      if ids[i] is None and rule_id not in claimed:
        ids[i] = rule_id
        claimed.add(rule_id)
  highest: collections.Counter[str] = collections.Counter()
  for entry in entries:
    prefix, number = split_id(entry["id"])
    highest[prefix] = max(highest[prefix], number)
  minted = []
  for i in order:
    if ids[i] is not None:
      continue
    newest = removed[i][-1]
    highest[newest.prefix] += 1
    rule_id = f"{newest.prefix}-{highest[newest.prefix]:0{ID_NUMBER_WIDTH}d}"
    older = [statement.wording.text_digest for statement in removed[i][:-1]]
    entries.append(
      {
        "id": rule_id,
        "file": newest.wording.file,
        "text_digest": newest.wording.text_digest,
        "context": newest.wording.context,
        "text": newest.wording.text,
        "previous_digests": [
          digest
          for digest in dict.fromkeys(older)
          if digest != newest.wording.text_digest
        ],
        "status": "retired",
        "minted_at": spec_commit,
        "retired_at": spec_commit,
      }
    )
    ids[i] = rule_id
    minted.append(rule_id)
  entries.sort(key=lambda entry: split_id(entry["id"]))
  return [rule_id or "" for rule_id in ids], entries, minted


def lifespan(
  stated_in: Sequence[str], versions: Sequence[str]
) -> tuple[str, str | None]:
  """Return a rule's first version, and the first version after its last.

  Args:
    stated_in: The versions that state the rule, oldest first.
    versions: Every version, oldest first, ending with `DRAFT`.

  Returns:
    `since` and `until`; `until` is None while the draft states the rule.

  """
  last = versions.index(stated_in[-1])
  until = versions[last + 1] if last + 1 < len(versions) else None
  return stated_in[0], until


def statements(
  stated: Sequence[Stated], own_digest: str
) -> list[dict[str, Any]]:
  """Describe how each release states a rule, oldest first.

  Each entry gives the release; the page and line as the release has them;
  the level and the wording's digest; and the wording itself when it isn't
  the record's own.
  """
  entries = []
  for statement in stated:
    entry = {
      "version": statement.version,
      "file": statement.file,
      "start_line": statement.position[0],
      "level": statement.level,
      "text_digest": statement.wording.text_digest,
    }
    if statement.wording.text_digest != own_digest:
      entry["clause"] = statement.record["clause"]
    entries.append(entry)
  return entries


def removed_record(
  stated: Sequence[Stated],
  rule_id: str,
  versions: Sequence[str],
  published_page: Callable[[str], str],
) -> dict[str, Any]:
  """Return the record of a rule that only older releases state.

  It is the newest release's record, at that release's page, with the
  rule's stable ID and its history in `demo`.
  """
  newest = stated[-1]
  record = copy.deepcopy(newest.record)
  record["id"] = rule_id
  record["source"]["file"] = newest.file
  record["source"]["page"] = published_page(newest.file)
  since, until = lifespan([statement.version for statement in stated], versions)
  record["demo"]["since"] = since
  record["demo"]["until"] = until
  record["demo"]["releases"] = statements(stated, record["text_digest"])
  return record


def main(argv: list[str] | None = None) -> int:
  """Build the catalog, write it, and print a summary."""
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--spec-root", type=Path, required=True)
  parser.add_argument("--spec-commit", required=True)
  parser.add_argument("--extractor-dir", type=Path, required=True)
  parser.add_argument(
    "--releases-repo",
    type=Path,
    required=True,
    help="a clone of the specification repository with the release commits",
  )
  parser.add_argument("--releases", type=Path, default=DEFAULT_RELEASES_FILE)
  parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
  parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_FILE)
  parser.add_argument(
    "--check",
    action="store_true",
    help="write nothing; fail if any output, the lock included, would change",
  )
  args = parser.parse_args(argv)
  spec_root = args.spec_root.resolve()
  out_dir = args.out_dir.resolve()
  out = sys.stdout.write

  modules = load_extractor(args.extractor_dir.resolve())
  catalog, config, identity = (
    modules["catalog"],
    modules["config"],
    modules["identity"],
  )
  extractor_commit, extractor_dirty = git_commit(args.extractor_dir)
  pinned = extract(modules, spec_root, SHOPPING_DIR, bold_only=True)
  schemas, resolver = pinned.schemas, pinned.resolver
  names = set(schemas)
  documents = pinned.documents
  report_document = pinned.report_document
  bold_only = pinned.bold_only

  demo_header = {
    "spec_commit": args.spec_commit,
    "extractor_commit": extractor_commit,
    "options": list(OPTIONS),
    "ids": f"stable, from {LOCK_FILE}",
  }
  review_problems: list[str] = []
  reviews = load_review(args.review.resolve(), review_problems)
  used_reviews = set()
  pre_review = {}
  records = []
  for path, document in documents.items():
    label = resolver.labels[document["source_file"]]
    for record in document["requirements"]:
      annotate(record, label)
      pre_review[record["id"]] = (record["demo"]["testable"], record["level"])
      key = (record["source"]["file"], record["text_digest"])
      if key in reviews:
        used_reviews.add(key)
        apply_review(record, reviews[key])
      records.append(record)
    documents[path] = dict(
      [
        *[(k, v) for k, v in document.items() if k != "requirements"],
        ("demo", demo_header),
        ("requirements", document["requirements"]),
      ]
    )

  # Stable IDs. A malformed lock stops the run before anything is written.
  def digest_of(context: str | None, text: str) -> str:
    joined = identity.DIGEST_SEPARATOR.join((context or "", text))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return digest[: config.ID_DIGEST_LENGTH]

  def prefix_of(labels: dict[str, PageLabel], file: str) -> str:
    slug = config.capability_slug(labels[file].id_name)
    return f"{config.READABLE_ID_PREFIX}-{slug}"

  lock_path = out_dir / LOCK_FILE
  lock_load_problems: list[str] = []
  lock = load_lock(lock_path, digest_of, lock_load_problems)
  if lock_load_problems:
    out("PROBLEMS:\n" + "".join(f"  - {p}\n" for p in lock_load_problems))
    return 1
  rules = [
    LockRule(
      prefix=prefix_of(resolver.labels, r["source"]["file"]),
      file=r["source"]["file"],
      text_digest=r["text_digest"],
      context=identity.normalize(r["compound_parent"])
      if r["compound_parent"]
      else None,
      text=r["normalized"],
      position=(r["source"]["start_line"], r["document_order"]),
    )
    for r in records
  ]
  assigned = assign_stable_ids(rules, lock, args.spec_commit)
  for record, stable_id in zip(records, assigned.ids, strict=True):
    record["id"] = stable_id
  stable_ids = {r["demo"]["positional_id"]: r["id"] for r in records}
  for entry in report_document["diagnostics"].get("duplicate_requirements", []):
    entry["ids"] = [stable_ids.get(i, i) for i in entry["ids"]]

  # Release history: read each release, trace the rules back through them,
  # and give removed rules IDs of their own.
  history_problems: list[str] = []
  releases = load_releases(args.releases.resolve(), history_problems)
  splits = load_splits(args.review.resolve(), history_problems)
  versions = releases.versions
  everything = extract(modules, spec_root, DOCS_ROOT)
  outside = [
    wording_of(record, identity.normalize)
    for document in everything.documents.values()
    for record in document["requirements"]
    if not in_shopping(record["source"]["file"])
  ]
  stated_by_version: dict[str, list[Stated]] = {}
  page_capabilities: dict[str, set[str]] = collections.defaultdict(set)
  for file, label in resolver.labels.items():
    page_capabilities[file].update(label.capabilities)
  for release in releases.releases:
    with tempfile.TemporaryDirectory() as staging:
      root = Path(staging)
      try:
        moved, unknown = stage_release(
          args.releases_repo.resolve(), release, releases, root
        )
      except subprocess.CalledProcessError as error:
        out(
          f"PROBLEMS:\n  - release {release.version}: cannot read commit "
          f"{release.commit[:8]} from {args.releases_repo}: "
          f"{error.stderr.decode(errors='replace').strip()}\n"
        )
        return 1
      found = extract(modules, root, DOCS_ROOT)
      declared = config.spec_version(root)
    if declared != release.version:
      history_problems.append(
        f"release {release.version}: its mkdocs.yml says {declared}"
      )
    if unknown:
      history_problems.append(
        f"release {release.version}: pages neither mapped nor listed as "
        f"other pages: {unknown}"
      )
    history_problems.extend(
      f"release {release.version}: {problem}"
      for problem in found.resolver.problems
      if in_shopping(problem.split(":", 1)[0])
    )
    stated_by_version[release.version] = []
    for document in found.documents.values():
      if not in_shopping(document["source_file"]):
        continue
      label = found.resolver.labels[document["source_file"]]
      page_capabilities[document["source_file"]].update(label.capabilities)
      for record in document["requirements"]:
        annotate(record, label)
        file = record["source"]["file"]
        key = (file, record["text_digest"])
        if key in reviews:
          used_reviews.add(key)
          apply_review(record, reviews[key])
        stated_by_version[release.version].append(
          Stated(
            version=release.version,
            wording=wording_of(record, identity.normalize),
            position=(record["source"]["start_line"], record["document_order"]),
            file=moved.get(file, file),
            level=effective_level(record),
            prefix=prefix_of(found.resolver.labels, file),
            record=record,
          )
        )

  def related(page: str, other: str) -> bool:
    """Return whether two pages share a capability."""
    return bool(page_capabilities[page] & page_capabilities[other])

  history = trace_history(
    versions,
    stated_by_version,
    {
      entry["id"]: Wording(
        entry["file"], entry["text_digest"], entry["context"], entry["text"]
      )
      for entry in assigned.entries
      if entry["status"] == "active"
    },
    outside,
    splits,
    related,
  )
  removed_ids, lock_entries, minted_removed = assign_removed_ids(
    [history.stated[key] for key in history.removed],
    assigned.entries,
    args.spec_commit,
  )
  keys = dict(zip(history.removed, removed_ids, strict=True))
  for record in records:
    stated = history.stated.get(record["id"], [])
    record["demo"]["since"] = stated[0].version if stated else DRAFT
    record["demo"]["releases"] = statements(stated, record["text_digest"])
  removed_records = sorted(
    (
      removed_record(
        history.stated[key], keys[key], versions, config.published_page
      )
      for key in history.removed
    ),
    key=lambda record: split_id(record["id"]),
  )

  # Checks. Anything here is unexpected and fails the run.
  problems = list(resolver.problems) + review_problems
  stale = sorted(
    reviews[key]["id_at_review"] for key in set(reviews) - used_reviews
  )
  if stale:
    problems.append(
      f"review: {len(stale)} decisions match no rule (reworded or "
      f"removed): {stale[:5]}"
    )
  unknown_actors = sorted(
    {r["target_actor"] for r in records if r["demo"]["testable"] is None},
    key=str,
  )
  if unknown_actors:
    problems.append(f"actors this tool does not know: {unknown_actors}")
  if extractor_dirty:
    problems.append(
      f"the extractor checkout has local changes ({extractor_commit})"
    )
  unresolved = [r["id"] for r in records if not r["capabilities"]]
  if unresolved:
    problems.append(
      f"{len(unresolved)} rules have no capability: {unresolved[:5]}"
    )
  unknown = sorted({c for r in records for c in r["capabilities"]} - names)
  if unknown:
    problems.append(f"capabilities no schema declares: {unknown}")
  duplicate_ids = [
    i for i, n in collections.Counter(r["id"] for r in records).items() if n > 1
  ]
  if duplicate_ids:
    problems.append(f"duplicate IDs: {duplicate_ids[:5]}")
  problems.extend(lock_problems(lock, assigned))
  for path, document in documents.items():
    problems.extend(
      f"{path}: {error}" for error in catalog.validate(without_demo(document))
    )
  index = capability_index(schemas, resolver.labels, records)
  kinds = {entry["name"]: entry["kind"] for entry in index}
  for rel_path, label in sorted(resolver.labels.items()):
    in_extensions = PurePosixPath(rel_path).parent.name == "extensions"
    for name in label.capabilities:
      if in_extensions and kinds.get(name) != "extension":
        problems.append(
          f"{rel_path} is an extension page but {name} extends nothing"
        )
      if not in_extensions and kinds.get(name) == "extension":
        problems.append(f"{rel_path} is labelled with the extension {name}")
  problems.extend(history_problems)
  idle_splits = sorted(set(splits) - history.split)
  if idle_splits:
    problems.append(
      f"review: splits that change nothing (the ID isn't stated in that "
      f"version, or no older wording matches it): {idle_splits}"
    )
  removed_actors = sorted(
    {
      r["target_actor"]
      for r in removed_records
      if r["demo"]["testable"] is None
    },
    key=str,
  )
  if removed_actors:
    problems.append(
      f"actors this tool does not know, in removed rules: {removed_actors}"
    )
  removed_unresolved = [
    r["id"] for r in removed_records if not r["capabilities"]
  ]
  if removed_unresolved:
    problems.append(
      f"{len(removed_unresolved)} removed rules have no capability: "
      f"{removed_unresolved[:5]}"
    )
  retired_ids = {e["id"] for e in lock_entries if e["status"] == "retired"}
  if len(set(removed_ids)) != len(removed_ids) or not retired_ids.issuperset(
    removed_ids
  ):
    problems.append("lock: removed rules must have distinct, retired IDs")
  problems.extend(
    lock_problems(
      assigned.entries,
      LockResult(
        ids=assigned.ids,
        entries=lock_entries,
        minted=minted_removed,
      ),
    )
  )

  # Write, or with --check only compare.
  report_document = {**report_document, "demo": demo_header}
  index_document = {
    "demo": demo_header,
    "extends_from": "each extension schema's $defs keys that name another "
    "capability",
    "capabilities": index,
  }
  history_document = {
    "about": HISTORY_ABOUT,
    "demo": demo_header,
    "versions": list(versions),
    "releases": [
      {
        "version": release.version,
        "ref": release.ref,
        "commit": release.commit,
        "rules": len(stated_by_version[release.version]),
        "matched": {
          kind: history.matched[release.version][kind] for kind in MATCH_KINDS
        },
      }
      for release in releases.releases
    ],
    "removed": removed_records,
    "moved_out": [
      {
        "version": rule.version,
        "file": rule.file,
        "start_line": rule.position[0],
        "clause": rule.record["clause"],
        "now_on": pages,
      }
      for rule, pages in sorted(
        history.moved_out,
        key=lambda item: (
          versions.index(item[0].version),
          item[0].file,
          item[0].position,
        ),
      )
    ],
  }
  outputs = {
    out_dir / path: catalog.serialize(document)
    for path, document in documents.items()
  }
  outputs[out_dir / REPORT_FILE] = catalog.serialize(report_document)
  outputs[out_dir / INDEX_FILE] = catalog.serialize(index_document)
  outputs[out_dir / HISTORY_FILE] = catalog.serialize(history_document)
  outputs[lock_path] = catalog.serialize(lock_document(lock_entries))
  pages_dir = out_dir / "shopping"
  old_pages = set(pages_dir.rglob("*.json")) if pages_dir.exists() else set()
  out_of_date = sorted(
    (old_pages - set(outputs))
    | {
      path
      for path, text in outputs.items()
      if not path.exists() or path.read_text(encoding="utf-8") != text
    }
  )
  if not args.check:
    if pages_dir.exists():
      shutil.rmtree(pages_dir)
    for path, text in outputs.items():
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(text, encoding="utf-8")

  # Summary.
  styles = collections.Counter(r["demo"]["keyword_style"] for r in records)
  out(f"spec {args.spec_commit[:8]}, extractor {extractor_commit[:7]}\n")
  out(
    f"rules: {len(records)} ({styles['bold']} bold, "
    f"{styles['capitals']} capitals); bold-only run: {len(bold_only)}\n"
  )
  bold_digests = collections.Counter(r.text_digest for r in bold_only)
  new_digests = collections.Counter(r["text_digest"] for r in records)
  changed = bold_digests - new_digests
  out(
    "bold-only rules whose wording changed once capitals count: "
    f"{sum(changed.values())}\n"
  )
  bold_levels = {
    (r.source.file, r.text_digest): str(r.level) for r in bold_only
  }
  level_changes = [
    (r["id"], bold_levels[key], r["level"])
    for r in records
    if (key := (r["source"]["file"], r["text_digest"])) in bold_levels
    and bold_levels[key] != r["level"]
  ]
  out(f"rules whose level changed once capitals count: {len(level_changes)}\n")
  for change in level_changes[:10]:
    out(f"  {change[0]}: {change[1]} -> {change[2]}\n")

  out("\nby capability (bold / capitals):\n")
  by_capability = collections.defaultdict(collections.Counter)
  for record in records:
    key = " + ".join(record["capabilities"])
    by_capability[key][record["demo"]["keyword_style"]] += 1
  for key in sorted(by_capability):
    counts = by_capability[key]
    out(
      f"  {sum(counts.values()):4}  {key}  "
      f"({counts['bold']} / {counts['capitals']})\n"
    )

  out("\nby page:\n")
  per_page = collections.Counter(r["source"]["file"] for r in records)
  for rel_path in sorted(resolver.labels):
    label = resolver.labels[rel_path]
    short = PurePosixPath(rel_path).relative_to(SHOPPING_DIR)
    caps = ", ".join(
      c.removeprefix("dev.ucp.shopping.") for c in label.capabilities
    )
    out(
      f"  {per_page[rel_path]:4}  {short!s:28} "
      f"{label.source:14} {caps or '-'}\n"
    )

  out("\ncapability index:\n")
  for entry in index:
    short_name = entry["name"].removeprefix("dev.ucp.shopping.")
    if entry["kind"] == "extension":
      relation = "extends " + ", ".join(
        e.removeprefix("dev.ucp.shopping.") for e in entry["extends"]
      )
    else:
      relation = "extended by " + (
        ", ".join(
          e.removeprefix("dev.ucp.shopping.") for e in entry["extended_by"]
        )
        or "-"
      )
    out(
      f"  {short_name:16} {entry['kind']:10} {relation:44} "
      f"{len(entry['pages'])} pages, {entry['rules']} rules\n"
    )
  in_scope = set(kinds)
  elsewhere = sorted(
    name
    for name in names - in_scope
    if set(parents_of(name, schemas)) & in_scope
  )
  if elsewhere:
    out(
      "  not in scope (outside the shopping folder) but extending a shopping "
      "capability: " + ", ".join(elsewhere) + "\n"
    )

  out("\ntestable by this suite (REST, against a business):\n")
  out(f"  {'capability':16} {'rules':>5} {'testable':>8} {'MUST':>5}\n")
  for entry in index:
    out(
      f"  {entry['name'].removeprefix('dev.ucp.shopping.'):16} "
      f"{entry['rules']:5} {entry['testable_rules']:8} "
      f"{entry['testable_must']:5}\n"
    )
  active = [r for r in records if is_active(r)]
  testable_records = [r for r in active if r["demo"]["testable"]]
  out(
    f"  {'unique rules':16} {len(active):5} {len(testable_records):8} "
    f"{sum(effective_level(r) in MUST_LEVELS for r in testable_records):5}\n"
  )
  for flag, title in ((True, "testable"), (False, "not testable")):
    reasons = collections.Counter(
      "reviewed" if "review" in r["demo"] else r["demo"]["testable_reason"]
      for r in active
      if r["demo"]["testable"] is flag
    )
    out(
      f"  {title}: "
      + ", ".join(f"{reason} {n}" for reason, n in reasons.most_common())
      + "\n"
    )

  reviewed = [r for r in records if "review" in r["demo"]]
  if reviewed:
    before = [t for t, _ in pre_review.values() if t]
    before_must = sum(
      bool(t) and level in MUST_LEVELS for t, level in pre_review.values()
    )
    flips = collections.Counter(
      (bool(pre_review[r["demo"]["positional_id"]][0]), r["demo"]["testable"])
      for r in reviewed
      if is_active(r)
    )
    actor_fixes = sum(
      r["demo"]["review"]["actor"] != (r["target_actor"] or "none")
      for r in reviewed
    )
    calls = sum(
      bool(r["demo"]["review"].get("needs_your_call")) for r in reviewed
    )
    out(
      f"\nreview ({args.review.name}): {len(reviewed)} decisions applied; "
      f"{len(records) - len(active)} not a rule; {calls} need your call\n"
    )
    out(
      f"  testable before review: {len(before)} ({before_must} MUST); "
      f"after: {len(testable_records)} "
      f"({sum(effective_level(r) in MUST_LEVELS for r in testable_records)}"
      " MUST)\n"
    )
    out(
      f"  testable -> not testable: {flips[(True, False)]}; "
      f"not testable -> testable: {flips[(False, True)]}; "
      f"actor set or corrected: {actor_fixes}\n"
    )

  statuses = collections.Counter(e["status"] for e in lock_entries)
  out(
    f"\nstable IDs ({LOCK_FILE}): kept {len(assigned.kept)}, carried over "
    f"{len(assigned.carried)}, new {len(assigned.minted)}, retired "
    f"{len(assigned.retired)}, restored {len(assigned.restored)}\n"
    f"  the lock holds {len(lock_entries)} IDs: {statuses['active']} "
    f"active, {statuses['retired']} retired\n"
  )
  if assigned.minted:
    by_prefix = collections.Counter(split_id(i)[0] for i in assigned.minted)
    out(
      "  new, by prefix: "
      + ", ".join(f"{p} {n}" for p, n in sorted(by_prefix.items()))
      + "\n"
    )
  where = {
    e["id"]: e["file"].removeprefix(f"{SHOPPING_DIR}/")
    for e in assigned.entries
  }
  for record in records:
    where[record["id"]] += f":{record['source']['start_line']}"
  for title, listed in (
    ("new", assigned.minted),
    ("retired", assigned.retired),
    ("restored", assigned.restored),
  ):
    if len(listed) <= LIST_LIMIT:
      for rule_id in listed:
        out(f"  {title}: {rule_id} at {where[rule_id]}\n")
  wording = {e["id"]: e["text"] for e in assigned.entries}
  for rule_id, alike, before in assigned.carried:
    out(
      f"  carried over: {rule_id} at {where[rule_id]}, {alike:.0%} "
      "alike; check it is the same rule\n"
      f"    was: {before[:100]}\n    now: {wording[rule_id][:100]}\n"
    )

  def must_in(record: dict[str, Any]) -> list[str]:
    """Return the versions in which a testable rule is a MUST."""
    if not is_active(record) or not record["demo"]["testable"]:
      return []
    stated = [(s["version"], s["level"]) for s in record["demo"]["releases"]]
    if record["demo"].get("until") is None:
      stated.append((DRAFT, effective_level(record)))
    return [version for version, level in stated if level in MUST_LEVELS]

  out(
    f"\nrelease history ({args.releases.name}, {HISTORY_FILE}): "
    f"{len(releases.releases)} releases\n"
  )
  for release in releases.releases:
    counts = history.matched[release.version]
    out(
      f"  {release.version} ({release.commit[:8]}): "
      f"{len(stated_by_version[release.version])} shopping rules; "
      + ", ".join(
        f"{kind} {counts[kind]}" for kind in MATCH_KINDS if counts[kind]
      )
      + "\n"
    )
  first = collections.Counter(
    r["demo"]["since"] for r in records if is_active(r)
  )
  first_must = collections.Counter(
    r["demo"]["since"] for r in records if DRAFT in must_in(r)
  )
  stated_must = collections.Counter(
    version for r in (*records, *removed_records) for version in must_in(r)
  )
  for title, counts in (
    ("rules by first version", first),
    ("testable MUST rules by first version", first_must),
    ("testable MUST rules each version states", stated_must),
  ):
    out(
      f"  {title}: "
      + ", ".join(f"{v} {counts[v]}" for v in versions if counts[v])
      + "\n"
    )
  removed_must = [r for r in removed_records if must_in(r)]
  out(
    f"  removed rules: {len(removed_records)}, {len(removed_must)} of them "
    f"testable MUST; new IDs {len(minted_removed)}\n"
  )
  for record in removed_must:
    if "review" not in record["demo"]:
      out(
        f"    not reviewed: {record['id']} ({record['demo']['since']} to "
        f"{record['demo']['releases'][-1]['version']}, "
        f"{record['demo']['testable_reason']}): {record['clause'][:90]}\n"
      )
  if history.moved_out:
    out(f"  moved out of the shopping pages: {len(history.moved_out)}\n")
  for rule, key, newer, alike in history.reworded:
    out(
      f"  reworded: {keys.get(key, key)} in {rule.version} at "
      f"{rule.file.removeprefix(f'{DOCS_ROOT}/')}:{rule.position[0]}, "
      f"{alike:.0%} alike; check it is the same rule\n"
      f"    was: {rule.wording.text[:100]}\n    now: {newer.text[:100]}\n"
    )

  out(
    f"\n{'would write' if args.check else 'wrote'} {len(documents)} page "
    f"files, {REPORT_FILE}, {INDEX_FILE}, {HISTORY_FILE} and {LOCK_FILE} to "
    f"{out_dir}; {len(out_of_date)} files "
    f"{'differ' if args.check else 'changed'}\n"
  )
  if args.check and out_of_date:
    out("check: nothing was written; out of date:\n")
    for path in out_of_date[:LIST_LIMIT]:
      out(f"  - {path.relative_to(out_dir)}\n")
    if len(out_of_date) > LIST_LIMIT:
      out(f"  - and {len(out_of_date) - LIST_LIMIT} more\n")
  elif args.check:
    out("check: every output is up to date\n")
  if problems:
    out("\nPROBLEMS:\n" + "".join(f"  - {p}\n" for p in problems))
    return 1
  if args.check and out_of_date:
    return 1
  out(
    "checks: every rule has a capability and its own stable ID, all names "
    "are declared, no conflicts, every extension page belongs to an "
    "extension, every page fits the catalog schema, every release was read "
    "and every removed rule has a retired ID\n"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
