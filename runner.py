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

"""Dynamic CLI test runner and capability orchestrator for UCP."""

import argparse
from collections.abc import Iterator
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from absl import flags
from framework import marker_check
from framework.base_test import BaseIntegrationTest, is_local_url
from framework.discovery import (
  UcpProfile,
  fetch_server_profile,
  resolve_discovery_url,
)
from framework.platform import PlatformProfile

SUITE_REGISTRY: dict[str, list[str]] = {
  "core": [
    "core.protocol_test",
    "core.binding_test",
  ],
  "common": [
    "common.payments.card_credential_test",
    "common.payments.ap2_test",
    "common.payments.token_binding_test",
  ],
  "shopping": [
    "shopping.checkout.protocol_test",
    "shopping.checkout.lifecycle_test",
    "shopping.checkout.business_logic_test",
    "shopping.checkout.idempotency_test",
    "shopping.checkout.totals_test",
    "shopping.discount.discount_test",
    "shopping.discount.semantics_test",
    "shopping.fulfillment.fulfillment_test",
    "shopping.fulfillment.structure_test",
    "shopping.order.order_test",
    "shopping.order.webhook_test",
    "shopping.order.webhook_structure_test",
    "shopping.order.simulation_url_security_test",
    "shopping.validation.validation_test",
    "shopping.validation.invalid_input_test",
  ],
}


def _resolve_suites(suite_arg: str) -> list[str]:
  """Resolve comma-separated suite names to target test class paths."""
  selected = set()
  tokens = [s.strip() for s in suite_arg.split(",") if s.strip()]
  for token in tokens:
    if token == "all":
      for tests in SUITE_REGISTRY.values():
        selected.update(tests)
    elif token in SUITE_REGISTRY:
      selected.update(SUITE_REGISTRY[token])
    else:
      # Assume direct class, module, or file path
      normalized = token.replace("/", ".").removesuffix(".py")
      selected.add(normalized)
  return sorted(selected)


def _iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
  """Iterate through all test cases in a nested TestSuite."""
  for item in suite:
    if isinstance(item, unittest.TestSuite):
      yield from _iter_tests(item)
    elif isinstance(item, unittest.TestCase):
      yield item


def _load_specifier(
  loader: unittest.TestLoader, spec: str
) -> unittest.TestSuite | None:
  """Load one specifier: a module, or a TestCase class as `module.Class`.

  Returns None, after logging why, when nothing could be loaded.
  """
  try:
    return loader.loadTestsFromModule(importlib.import_module(spec))
  except ModuleNotFoundError as exc:
    if exc.name != spec:
      # The module exists but one of its own imports is missing.
      logging.error("Failed to import test module %s: %s", spec, exc)
      return None

  if "." not in spec:
    logging.error("Unknown test specifier: %s", spec)
    return None

  parent, attr = spec.rsplit(".", 1)
  try:
    mod = importlib.import_module(parent)
  except ImportError as exc:
    logging.error("Failed to import test specifier %s: %s", spec, exc)
    return None
  cls = getattr(mod, attr, None)
  if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
    return loader.loadTestsFromTestCase(cls)
  logging.error(
    "Unknown test specifier: %s (%s has no TestCase class named %r)",
    spec,
    parent,
    attr,
  )
  return None


def _load_tests(
  specifiers: list[str], filter_pattern: str | None = None
) -> tuple[unittest.TestSuite, list[str]]:
  """Load unittest test cases from module or class specifiers.

  Returns the suite and the specifiers that could not be loaded.
  """
  loader = unittest.TestLoader()
  suite = unittest.TestSuite()
  unresolved: list[str] = []

  for spec in specifiers:
    tests = _load_specifier(loader, spec)
    if tests is None:
      unresolved.append(spec)
    else:
      suite.addTests(tests)

  if filter_pattern:
    filtered = unittest.TestSuite()
    pattern = filter_pattern.lower()
    for test in _iter_tests(suite):
      if pattern in test.id().lower():
        filtered.addTest(test)
    return filtered, unresolved

  return suite, unresolved


# Optional runner options that reach the tests as absl flags of the same
# name (see framework.base_test.flag_value).
_TEST_OPTIONS = (
  "agent_profile_url",
  "conformance_input",
  "fixture_config",
  "test_data_dir",
  "discovery_url",
  "webhook_base_url",
)

# Options that name a file, and one that names a folder. The tests use
# their defaults for a path that does not exist, so the runner checks them.
_FILE_OPTIONS = ("conformance_input", "fixture_config")
_DIR_OPTIONS = ("test_data_dir",)


def _missing_paths(args: argparse.Namespace) -> list[str]:
  """Return a message for each path option that names nothing usable."""
  problems = []
  for name in _FILE_OPTIONS:
    value = getattr(args, name)
    if value and not Path(value).is_file():
      problems.append(f"--{name}: no such file: {value}")
  for name in _DIR_OPTIONS:
    value = getattr(args, name)
    if value and not Path(value).is_dir():
      problems.append(f"--{name}: no such directory: {value}")
  return problems


def _remote_notes(args: argparse.Namespace) -> list[str]:
  """Say what a server elsewhere can't reach on this machine's localhost."""
  if is_local_url(args.server_url):
    return []
  notes = []
  if not args.agent_profile_url:
    notes.append(
      "the server is not on this machine, but the UCP-Agent header names a "
      "localhost profile; a server that fetches it needs --agent_profile_url"
    )
  if not args.webhook_base_url:
    notes.append(
      "webhook tests will skip: the server is not on this machine and no "
      "--webhook_base_url was given"
    )
  return notes


def _sync_absl_flags(args: argparse.Namespace) -> None:
  """Pass the parsed CLI arguments on to the absl flags the suites read.

  Call this after the test modules are imported: they register their flags
  on import, and absl ignores flags it does not know yet.
  """
  flag_args = [
    sys.argv[0],
    f"--server_url={args.server_url}",
    f"--mock_webhook_port={args.mock_webhook_port}",
    f"--mock_agent_port={args.mock_agent_port}",
  ]
  if args.simulation_secret:
    flag_args.append(f"--simulation_secret={args.simulation_secret}")
  for name in _TEST_OPTIONS:
    value = getattr(args, name)
    if value:
      flag_args.append(f"--{name}={value}")
  if args.verbose_http:
    flag_args.append("--verbose_http")

  flags.FLAGS(flag_args, known_only=True)


# A target folder describes one server under test in this file.
TARGET_FILE = "target.json"
# The options a target file may give; other keys are ignored.
TARGET_KEYS = (
  "server_url",
  "discovery_url",
  "conformance_input",
  "fixture_config",
  "test_data_dir",
  "agent_profile_url",
  "webhook_base_url",
)
# The options that are paths, resolved against the target folder.
_TARGET_PATH_KEYS = ("conformance_input", "fixture_config", "test_data_dir")
DEFAULT_SERVER_URL = "http://localhost:8182"


class TargetError(Exception):
  """The target folder's file is missing or is not usable."""


def load_target(target_dir: str) -> dict[str, str]:
  """Read the options a target folder's file gives.

  Paths are made relative to the folder, unless absolute. Keys other than
  TARGET_KEYS, and empty values, are left out.

  Raises:
    TargetError: The file is missing, is not JSON, or is not an object, or
      a known option is not a string.

  """
  path = Path(target_dir) / TARGET_FILE
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError as error:
    raise TargetError(f"no {TARGET_FILE} in {target_dir}") from error
  except (OSError, ValueError) as error:
    raise TargetError(f"cannot read {path}: {error}") from error
  if not isinstance(data, dict):
    raise TargetError(f"{path} is not a JSON object")
  options = {}
  for key in TARGET_KEYS:
    value = data.get(key)
    if value in (None, ""):
      continue
    if not isinstance(value, str):
      raise TargetError(f"{path}: {key} is not a string")
    if key in _TARGET_PATH_KEYS:
      value = str(Path(target_dir) / value)
    options[key] = value
  return options


def apply_target(args: argparse.Namespace, target: dict[str, str]) -> None:
  """Fill in the options not given on the command line from a target.

  Then give --server_url its remaining defaults: $SERVER_URL, then
  $UCP_SERVER_URL, then DEFAULT_SERVER_URL.
  """
  for key, value in target.items():
    if getattr(args, key) is None:
      setattr(args, key, value)
  if args.server_url is None:
    args.server_url = (
      os.environ.get("SERVER_URL")
      or os.environ.get("UCP_SERVER_URL")
      or DEFAULT_SERVER_URL
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  """Parse command line arguments for the conformance runner."""
  parser = argparse.ArgumentParser(
    prog="ucp-conformance",
    description="Universal Commerce Protocol Conformance Test Runner",
  )
  parser.add_argument(
    "--target_dir",
    metavar="DIR",
    default=None,
    help=f"Folder with a {TARGET_FILE} that gives the target server's "
    f"options ({', '.join(TARGET_KEYS)}); paths in it are relative to the "
    "folder, and options given on the command line win.",
  )
  parser.add_argument(
    "--server_url",
    default=None,
    help="Target UCP server base URL (default: the target file's, else "
    "$SERVER_URL, else $UCP_SERVER_URL, else http://localhost:8182).",
  )
  parser.add_argument(
    "--discovery_url",
    default=None,
    help="URL of the server's discovery profile, if it is not at "
    "SERVER_URL/.well-known/ucp: a full URL, or a path on --server_url. The "
    "core tests still check /.well-known/ucp.",
  )
  parser.add_argument(
    "--platform",
    default=None,
    help="Platform certification profile name or path (e.g. google).",
  )
  parser.add_argument(
    "--suite",
    default="all",
    help="Test suite(s) to run: all, core, common, shopping (default: all).",
  )
  parser.add_argument(
    "--simulation_secret",
    default=os.environ.get("SIMULATION_SECRET", ""),
    help="Secret for calling simulation endpoints.",
  )
  parser.add_argument(
    "--conformance_input",
    default=None,
    help="Path to the conformance input JSON describing the merchant under "
    "test (default: the bundled flower shop input).",
  )
  parser.add_argument(
    "--fixture_config",
    default=None,
    help="Path to the test fixtures JSON (default: the bundled flower shop "
    "fixtures).",
  )
  parser.add_argument(
    "--test_data_dir",
    default=None,
    help="Directory containing CSV test data (default: the bundled flower "
    "shop data).",
  )
  parser.add_argument(
    "--mock_webhook_port",
    type=int,
    default=8284,
    help="Port for local mock webhook server (default: 8284).",
  )
  parser.add_argument(
    "--mock_agent_port",
    type=int,
    default=8285,
    help="Port for local mock agent profile server (default: 8285).",
  )
  parser.add_argument(
    "--agent_profile_url",
    default=None,
    help="Public URL of the test agent's profile, sent in the UCP-Agent "
    "header, for a server that cannot reach this machine's localhost: for "
    "example a tunnel to the mock agent port, ending in /profiles/agent.json.",
  )
  parser.add_argument(
    "--webhook_base_url",
    default=None,
    help="Public URL that reaches the mock webhook port, such as a tunnel, "
    "for a server that cannot reach this machine's localhost. Without it, "
    "webhook tests skip when the server is not on this machine.",
  )
  parser.add_argument(
    "-k",
    "--filter",
    default=None,
    help="Filter expression to select tests by identifier.",
  )
  parser.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    help="Enable verbose test execution output.",
  )
  parser.add_argument(
    "--verbose_http",
    action="store_true",
    help="Log every HTTP request the tests send.",
  )
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="List selected test cases without contacting the server.",
  )
  parser.add_argument(
    "--requirements_check",
    choices=marker_check.MODES,
    default="strict",
    help="Check the shopping tests' spec markers against the requirements "
    "catalog before running: strict stops on problems, warn only reports "
    "them, off skips the check (default: strict).",
  )
  args = parser.parse_args(argv)
  target = {}
  if args.target_dir:
    try:
      target = load_target(args.target_dir)
    except TargetError as error:
      parser.error(f"--target_dir: {error}")
  apply_target(args, target)
  return args


def _platform_file(name_or_path: str) -> Path | None:
  """Resolve a platform profile given as a path or a name in `platforms/`."""
  candidates = (
    Path(name_or_path),
    Path(__file__).resolve().parent / "platforms" / f"{name_or_path}.yaml",
  )
  for candidate in candidates:
    if candidate.is_file():
      return candidate
  return None


def main(argv: list[str] | None = None) -> int:
  """Execute the UCP conformance test orchestrator."""
  args = parse_args(argv)

  problems = _missing_paths(args)
  if problems:
    sys.stderr.write("Error: " + "\n       ".join(problems) + "\n")
    return 1

  logging_level = logging.DEBUG if args.verbose else logging.INFO
  logging.basicConfig(level=logging_level, format="%(levelname)s: %(message)s")

  # Import the test modules first: they register the absl flags that
  # _sync_absl_flags forwards.
  test_specs = _resolve_suites(args.suite)
  test_suite, unresolved = _load_tests(test_specs, filter_pattern=args.filter)
  total_tests = test_suite.countTestCases()
  _sync_absl_flags(args)
  discovery_url = resolve_discovery_url(args.server_url, args.discovery_url)

  sys.stdout.write("=" * 70 + "\n")
  sys.stdout.write("UCP CONFORMANCE TEST RUNNER\n")
  sys.stdout.write(f"Target Server: {args.server_url}\n")
  if args.target_dir:
    sys.stdout.write(f"Target Folder: {args.target_dir}\n")
  if args.discovery_url:
    sys.stdout.write(f"Discovery Profile: {discovery_url}\n")
  sys.stdout.write("=" * 70 + "\n")
  for note in _remote_notes(args):
    sys.stdout.write(f"Note: {note}\n")

  # An empty suite would "pass", so a selection that loads nothing is an
  # error rather than a successful run of zero tests.
  if unresolved:
    sys.stderr.write(
      "Error: could not load test specifier(s): " + ", ".join(unresolved) + "\n"
    )
    return 1
  if total_tests == 0:
    sys.stderr.write(
      f"Error: no tests matched suite '{args.suite}'"
      + (f" with filter '{args.filter}'" if args.filter else "")
      + "\n"
    )
    return 1

  sys.stdout.write(
    f"Resolved {total_tests} test cases across suite '{args.suite}'\n"
  )

  loaded = [
    marker_check.from_unittest(test) for test in _iter_tests(test_suite)
  ]
  report, stop = marker_check.evaluate(loaded, args.requirements_check)
  if report:
    sys.stdout.write("\n" + report)
  if stop:
    sys.stdout.flush()
    sys.stderr.write(
      "\nRequirements check failed: fix the problems above, or pass "
      "--requirements_check=warn to run anyway.\n"
    )
    return 1

  if args.dry_run:
    sys.stdout.write("\nDry Run - Selected Tests:\n")
    for test in _iter_tests(test_suite):
      sys.stdout.write(f"  - {test.id()}\n")
    return 0

  server_profile: UcpProfile | None = None
  try:
    server_profile = fetch_server_profile(
      args.server_url, discovery_url=discovery_url
    )
    BaseIntegrationTest.server_profile = server_profile
    sys.stdout.write("\nDiscovered Server Capabilities:\n")
    for cap in server_profile.capabilities.values():
      sys.stdout.write(f"  - {cap.name} (v{cap.version})\n")
    if server_profile.payment_handlers:
      handlers_str = ", ".join(sorted(server_profile.payment_handlers))
      sys.stdout.write(f"Payment Handlers: {handlers_str}\n")
  except Exception as exc:  # pylint: disable=broad-exception-caught
    sys.stdout.write(f"Warning: Failed to fetch {discovery_url}: {exc}\n")

  if args.platform:
    platform_path = _platform_file(args.platform)
    if platform_path is None:
      sys.stderr.write(
        f"Error: Platform profile not found at {args.platform}\n"
      )
      return 1

    profile = PlatformProfile.from_yaml(platform_path)
    sys.stdout.write(
      f"\nEvaluating Platform Profile: {profile.platform_name}\n"
    )
    if server_profile is None:
      sys.stderr.write(
        "Error: Cannot validate platform profile: discovery profile"
        " unavailable.\n"
      )
      return 1

    errors = profile.validate(server_profile)
    if errors:
      sys.stderr.write("\nPlatform Certification Failures:\n")
      for err in errors:
        sys.stderr.write(f"  - {err}\n")
      return 1
    sys.stdout.write("  Platform requirements satisfied!\n")

  sys.stdout.write("-" * 70 + "\n")
  runner = unittest.TextTestRunner(
    verbosity=2 if args.verbose else 1, stream=sys.stdout
  )
  result = runner.run(test_suite)

  # testsRun counts skipped tests too, so take them out of the passes.
  passed = (
    result.testsRun
    - len(result.failures)
    - len(result.errors)
    - len(result.skipped)
  )
  sys.stdout.write("\n" + "=" * 70 + "\n")
  sys.stdout.write("CONFORMANCE TEST SUMMARY\n")
  sys.stdout.write(f"Total Executed: {result.testsRun}\n")
  sys.stdout.write(f"Passed: {passed}\n")
  sys.stdout.write(f"Skipped: {len(result.skipped)}\n")
  sys.stdout.write(f"Failures: {len(result.failures)}\n")
  sys.stdout.write(f"Errors: {len(result.errors)}\n")
  sys.stdout.write("=" * 70 + "\n")

  return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
  sys.exit(main())
