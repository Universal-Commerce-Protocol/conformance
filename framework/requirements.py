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

"""Look up specification rules in the shopping requirements catalog.

Demo only. `requirements/tools/build_catalog.py` writes the catalog to
`requirements/catalog/`: one file of rules per specification page, under
`shopping/`; `requirements.lock.json`, which records every rule ID ever
issued; and `history.json`, which records the releases that state each rule
and the rules that only older releases state. Tests cite rules by ID with
`framework.decorators.spec_assert`; this module says what an ID names.
"""

from collections.abc import Iterable, Mapping
import dataclasses
import functools
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

DEFAULT_CATALOG_DIR = (
  Path(__file__).resolve().parents[1] / "requirements" / "catalog"
)
PAGES_DIR = "shopping"
LOCK_FILE = "requirements.lock.json"
INDEX_FILE = "capabilities.json"
HISTORY_FILE = "history.json"
# The catalog schema's pattern for a rule ID.
ID_RE = re.compile(r"^REQ-[A-Z0-9-]+-[0-9]{2,}$")
# The version of the specification the catalog was built from: newer than
# every release.
DRAFT = "draft"


class CatalogError(Exception):
  """The catalog is missing, malformed, or out of step with its lock."""


@dataclasses.dataclass(frozen=True)
class Statement:
  """How one release of the specification states a rule.

  Attributes:
    version: The release, such as "2026-04-08".
    level: The rule's RFC 2119 level in that release.
    clause: The rule's wording in that release.
    source_file: The page that states it, as the release names the page.
    line: The line of that page it starts on.

  """

  version: str
  level: str
  clause: str
  source_file: str
  line: int


@dataclasses.dataclass(frozen=True)
class Rule:
  """One rule in the catalog.

  Attributes:
    id: The rule's stable ID.
    level: Its RFC 2119 level, as corrected by the review if it was.
    clause: Its wording, as the specification states it.
    capabilities: The capabilities it belongs to.
    source_file: The specification page it is on.
    line: The line of that page it starts on.
    link: Where it is published: its page and anchor, relative to the root
      of a release of the specification site.
    testable: Whether this suite can test it.
    testable_reason: Why it is or is not testable.
    is_rule: False when the review found it states no requirement.
    since: The first version that states it: a release, or `DRAFT`.
    until: For a removed rule, the first version after the last one that
      states it; None while the catalog's version states it.
    statements: How each release that states it states it, oldest first.
      The catalog's own version isn't among them.

  """

  id: str
  level: str
  clause: str
  capabilities: tuple[str, ...]
  source_file: str
  line: int
  link: str
  testable: bool
  testable_reason: str
  is_rule: bool
  since: str = DRAFT
  until: str | None = None
  statements: tuple[Statement, ...] = ()

  @property
  def versions(self) -> tuple[str, ...]:
    """Return every version that states the rule, oldest first."""
    stated = tuple(statement.version for statement in self.statements)
    return stated if self.until else (*stated, DRAFT)

  def level_in(self, version: str) -> str | None:
    """Return the rule's level in a version, or None if it isn't stated.

    Args:
      version: A release, or `DRAFT` for the catalog's own version.

    Returns:
      The level that version states, or None.

    """
    if version == DRAFT:
      return self.level if self.until is None else None
    for statement in self.statements:
      if statement.version == version:
        return statement.level
    return None


@dataclasses.dataclass(frozen=True)
class Capability:
  """One capability in the catalog's index.

  Attributes:
    name: Its name, such as `dev.ucp.shopping.checkout`.
    kind: "extension" for an extension, otherwise "capability".
    extends: For an extension, the capabilities it extends.

  """

  name: str
  kind: str = "capability"
  extends: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Catalog:
  """The catalog's rules by stable ID, and the IDs retired from it.

  Attributes:
    rules: Every rule in the catalog, by ID.
    retired: IDs issued for rules the specification no longer states.
    extends: For each extension, the capabilities it extends.
    capabilities: The capabilities in the catalog's index, in its order.
    spec_commit: The specification commit the catalog was built from.
    removed: The retired IDs of rules that only older releases state, with
      the rule each names, by ID.
    versions: Every version the history covers, oldest first: the
      releases, then `DRAFT`.

  """

  rules: Mapping[str, Rule]
  retired: frozenset[str]
  extends: Mapping[str, tuple[str, ...]] = dataclasses.field(
    default_factory=lambda: MappingProxyType({})
  )
  capabilities: tuple[Capability, ...] = ()
  spec_commit: str = ""
  removed: Mapping[str, Rule] = dataclasses.field(
    default_factory=lambda: MappingProxyType({})
  )
  versions: tuple[str, ...] = (DRAFT,)

  def status(self, rule_id: str) -> str:
    """Return what an ID names.

    One of: "rule", "not a rule", "removed", "retired" or "unknown". A test
    should cite only IDs whose status is "rule" or "removed". "not a rule" is
    a sentence the review found states no requirement. "removed" is a rule
    that only older releases state. Any other retired ID's rule was removed
    or rewritten, so a test citing it needs a new ID.

    Args:
      rule_id: The ID to look up.

    Returns:
      The ID's status.

    """
    rule = self.rules.get(rule_id)
    if rule is not None:
      return "rule" if rule.is_rule else "not a rule"
    removed = self.removed.get(rule_id)
    if removed is not None:
      return "removed" if removed.is_rule else "not a rule"
    return "retired" if rule_id in self.retired else "unknown"

  def rule(self, rule_id: str) -> Rule | None:
    """Return the rule an ID names, removed ones included, or None."""
    return self.rules.get(rule_id) or self.removed.get(rule_id)

  def with_parents(self, capabilities: Iterable[str]) -> frozenset[str]:
    """Return capabilities together with every capability they extend.

    A server that offers an extension also offers what it extends, so a
    test that requires the discount extension exercises checkout too.

    Args:
      capabilities: Capability names.

    Returns:
      The names, with the capabilities they extend, directly or not.

    """
    found = set(capabilities)
    pending = list(found)
    while pending:
      for parent in self.extends.get(pending.pop(), ()):
        if parent not in found:
          found.add(parent)
          pending.append(parent)
    return frozenset(found)


def load_catalog(catalog_dir: str | Path | None = None) -> Catalog:
  """Load the catalog, reading it from disk only once per directory.

  Args:
    catalog_dir: The catalog directory. By default, this repository's
      `requirements/catalog/`.

  Returns:
    The catalog.

  Raises:
    CatalogError: If the catalog is missing or malformed, its rules are not
      exactly its lock's active IDs, or a removed rule's ID isn't retired.

  """
  return _load(Path(catalog_dir or DEFAULT_CATALOG_DIR).resolve())


@functools.cache
def _load(catalog_dir: Path) -> Catalog:
  """Read and check the catalog in a directory."""
  pages = sorted((catalog_dir / PAGES_DIR).rglob("*.json"))
  lock_path = catalog_dir / LOCK_FILE
  if not pages or not lock_path.is_file():
    raise CatalogError(
      f"no catalog in {catalog_dir}; generate it with "
      "requirements/tools/build_catalog.py"
    )
  rules: dict[str, Rule] = {}
  for page in pages:
    for record in _read(page).get("requirements", []):
      rule = _checked_rule(page, record)
      if rule.id in rules:
        raise CatalogError(f"{page}: {rule.id} is malformed or repeated")
      rules[rule.id] = rule
  entries = _read(lock_path).get("entries", [])
  active = {entry["id"] for entry in entries if entry["status"] == "active"}
  if active != set(rules):
    raise CatalogError(
      f"the catalog in {catalog_dir} and its lock disagree (only in the "
      f"catalog: {sorted(set(rules) - active)[:5]}; only in the lock: "
      f"{sorted(active - set(rules))[:5]}); regenerate the catalog"
    )
  retired = frozenset(
    entry["id"] for entry in entries if entry["status"] == "retired"
  )
  history_path = catalog_dir / HISTORY_FILE
  history = _read(history_path) if history_path.is_file() else {}
  removed: dict[str, Rule] = {}
  for record in history.get("removed", []):
    rule = _checked_rule(history_path, record)
    if rule.id not in retired or rule.id in removed:
      raise CatalogError(
        f"{history_path}: removed rule {rule.id} is repeated or not retired "
        "in the lock; regenerate the catalog"
      )
    removed[rule.id] = rule
  index_path = catalog_dir / INDEX_FILE
  index = _read(index_path) if index_path.is_file() else {}
  capabilities = tuple(
    Capability(
      name=entry["name"],
      kind=entry.get("kind", "capability"),
      extends=tuple(entry.get("extends", ())),
    )
    for entry in index.get("capabilities", [])
  )
  return Catalog(
    rules=MappingProxyType(dict(sorted(rules.items()))),
    retired=retired,
    extends=MappingProxyType({c.name: c.extends for c in capabilities}),
    capabilities=capabilities,
    spec_commit=index.get("demo", {}).get("spec_commit", ""),
    removed=MappingProxyType(dict(sorted(removed.items()))),
    versions=tuple(history.get("versions", (DRAFT,))),
  )


def _read(path: Path) -> dict[str, Any]:
  """Read a JSON file of the catalog."""
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as error:
    raise CatalogError(f"cannot read {path}: {error}") from error


def _checked_rule(path: Path, record: dict[str, Any]) -> Rule:
  """Build a rule from a record of a catalog file, or raise CatalogError."""
  try:
    rule = _rule(record)
  except (KeyError, TypeError) as error:
    raise CatalogError(f"{path}: malformed record ({error})") from error
  if not ID_RE.match(rule.id):
    raise CatalogError(f"{path}: {rule.id} is malformed or repeated")
  return rule


def _rule(record: dict[str, Any]) -> Rule:
  """Build a rule from a catalog record and the demo's additions to it."""
  demo = record.get("demo", {})
  source = record["source"]
  anchor = source.get("anchor")
  return Rule(
    id=record["id"],
    level=demo.get("review", {}).get("level") or record["level"],
    clause=record["clause"],
    capabilities=tuple(record["capabilities"]),
    source_file=source["file"],
    line=source["start_line"],
    link=f"{source['page']}#{anchor}" if anchor else source["page"],
    testable=bool(demo.get("testable")),
    testable_reason=demo.get("testable_reason", ""),
    is_rule=not demo.get("excluded", False),
    since=demo.get("since", DRAFT),
    until=demo.get("until"),
    statements=tuple(
      Statement(
        version=entry["version"],
        level=entry["level"],
        clause=entry.get("clause", record["clause"]),
        source_file=entry["file"],
        line=entry["start_line"],
      )
      for entry in demo.get("releases", ())
    ),
  )
