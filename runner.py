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
import logging
import os
from pathlib import Path
import sys
import unittest
from absl import flags
from framework.base_test import BaseIntegrationTest
from framework.discovery import UcpProfile, fetch_server_profile
from framework.platform import PlatformProfile

SUITE_REGISTRY: dict[str, list[str]] = {
  "core": [
    "core.protocol_test",
    "core.binding_test",
    "core.security_test",
    "core.idempotency_test",
  ],
  "common": [
    "common.webhooks.webhook_test",
    "common.payments.card_credential_test",
    "common.payments.ap2_test",
  ],
  "shopping": [
    "shopping.checkout.lifecycle_test",
    "shopping.checkout.business_logic_test",
    "shopping.discount.discount_test",
    "shopping.fulfillment.fulfillment_test",
    "shopping.order.order_test",
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


def _load_tests(
  specifiers: list[str], filter_pattern: str | None = None
) -> unittest.TestSuite:
  """Load unittest test cases from module or class specifiers."""
  loader = unittest.TestLoader()
  suite = unittest.TestSuite()

  for spec in specifiers:
    # First try loading directly as a module
    try:
      mod = importlib.import_module(spec)
      suite.addTests(loader.loadTestsFromModule(mod))
      continue
    except ModuleNotFoundError:
      pass

    # If not a direct module, try parent module and class/method attribute
    if "." in spec:
      parent, attr = spec.rsplit(".", 1)
      try:
        mod = importlib.import_module(parent)
        cls = getattr(mod, attr, None)
        if cls and isinstance(cls, type) and issubclass(cls, unittest.TestCase):
          suite.addTests(loader.loadTestsFromTestCase(cls))
          continue
      except (ImportError, AttributeError) as exc:
        logging.error("Failed to import test specifier %s: %s", spec, exc)
    else:
      logging.error("Unknown test specifier: %s", spec)

  if filter_pattern:
    filtered = unittest.TestSuite()
    pattern = filter_pattern.lower()
    for test in _iter_tests(suite):
      if pattern in test.id().lower():
        filtered.addTest(test)
    return filtered

  return suite


def _sync_absl_flags(args: argparse.Namespace) -> None:
  """Synchronize parsed CLI arguments with absl flags and environment."""
  flag_args = [
    sys.argv[0],
    f"--server_url={args.server_url}",
    f"--simulation_secret={args.simulation_secret or ''}",
    f"--mock_webhook_port={args.mock_webhook_port}",
    f"--mock_agent_port={args.mock_agent_port}",
  ]
  if args.conformance_input:
    flag_args.append(f"--conformance_input={args.conformance_input}")
    os.environ["FLAGS_conformance_input"] = (  # noqa: SIM112
      args.conformance_input
    )

  os.environ["FLAGS_server_url"] = args.server_url  # noqa: SIM112
  if args.simulation_secret:
    os.environ["FLAGS_simulation_secret"] = (  # noqa: SIM112
      args.simulation_secret
    )

  flags.FLAGS(flag_args, known_only=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  """Parse command line arguments for the conformance runner."""
  parser = argparse.ArgumentParser(
    prog="ucp-conformance",
    description="Universal Commerce Protocol Conformance Test Runner",
  )
  parser.add_argument(
    "--server_url",
    default=os.environ.get("UCP_SERVER_URL", "http://localhost:8182"),
    help="Target UCP server base URL (default: http://localhost:8182).",
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
    help="Optional path to custom conformance input JSON configuration.",
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
    "--dry-run",
    action="store_true",
    help="List selected test cases without executing them.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  """Execute the UCP conformance test orchestrator."""
  args = parse_args(argv)

  logging_level = logging.DEBUG if args.verbose else logging.INFO
  logging.basicConfig(level=logging_level, format="%(levelname)s: %(message)s")

  _sync_absl_flags(args)

  sys.stdout.write("=" * 70 + "\n")
  sys.stdout.write("UCP CONFORMANCE TEST RUNNER\n")
  sys.stdout.write(f"Target Server: {args.server_url}\n")
  sys.stdout.write("=" * 70 + "\n")

  server_profile: UcpProfile | None = None
  try:
    server_profile = fetch_server_profile(args.server_url)
    BaseIntegrationTest.server_profile = server_profile
    sys.stdout.write("Discovered Server Capabilities:\n")
    for cap in server_profile.capabilities.values():
      sys.stdout.write(f"  - {cap.name} (v{cap.version})\n")
    if server_profile.payment_handlers:
      handlers_str = ", ".join(sorted(server_profile.payment_handlers))
      sys.stdout.write(f"Payment Handlers: {handlers_str}\n")
  except Exception as exc:  # pylint: disable=broad-exception-caught
    sys.stdout.write(f"Warning: Failed to fetch /.well-known/ucp: {exc}\n")

  if args.platform:
    platform_path = Path(args.platform)
    if not platform_path.is_file():
      candidate = Path("platforms") / f"{args.platform}.yaml"
      if candidate.is_file():
        platform_path = candidate
    if not platform_path.is_file():
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

  test_specs = _resolve_suites(args.suite)
  test_suite = _load_tests(test_specs, filter_pattern=args.filter)
  total_tests = test_suite.countTestCases()

  sys.stdout.write(
    f"\nResolved {total_tests} test cases across suite '{args.suite}'\n"
  )

  if args.dry_run:
    sys.stdout.write("\nDry Run - Selected Tests:\n")
    for test in _iter_tests(test_suite):
      sys.stdout.write(f"  - {test.id()}\n")
    return 0

  sys.stdout.write("-" * 70 + "\n")
  runner = unittest.TextTestRunner(
    verbosity=2 if args.verbose else 1, stream=sys.stdout
  )
  result = runner.run(test_suite)

  passed = result.testsRun - len(result.failures) - len(result.errors)
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
