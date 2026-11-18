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

"""Unit tests for HTTP exchange logging, failure context, and summary counts."""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from absl.testing import flagsaver
from framework import base_test, outcomes
import httpx
import runner


def _make_base_probe(verbose_http: bool) -> base_test.BaseIntegrationTest:
  """Return a BaseIntegrationTest bound to an in-memory mock transport."""

  class Probe(base_test.BaseIntegrationTest):
    def runTest(self):  # noqa: N802
      """Do nothing."""

  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
      201,
      headers={"Content-Type": "application/json"},
      json={"id": "chk_123", "status": "incomplete"},
      request=request,
    )

  probe = Probe()
  probe._verbose_http = verbose_http
  probe._last_response = None
  probe.client = httpx.Client(
    base_url="https://store.example",
    transport=httpx.MockTransport(handler),
    event_hooks={
      "request": [probe._note_request],
      "response": [probe._note_response],
    },
  )
  return probe


def _run_cases(*classes: type[unittest.TestCase]) -> outcomes.RecordingResult:
  """Run test classes under RecordingResult and return the result."""
  suite = unittest.TestSuite(
    unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in classes
  )
  stream = io.StringIO()
  test_runner = unittest.TextTestRunner(
    stream=stream, resultclass=outcomes.RecordingResult
  )
  return test_runner.run(suite)


def _run_runner_suite(suite: unittest.TestSuite) -> str:
  """Run `suite` through `runner.main` and return its console output."""
  out = io.StringIO()
  fetch = mock.Mock(side_effect=OSError("offline"))
  with (
    flagsaver.flagsaver(),
    mock.patch.dict(os.environ),
    mock.patch.object(runner, "fetch_server_profile", fetch),
    mock.patch.object(runner, "_load_tests", return_value=(suite, [])),
    contextlib.redirect_stdout(out),
    contextlib.redirect_stderr(out),
  ):
    runner.main(["--server_url=https://shop.example"])
  return out.getvalue()


def _summary_counts(output: str) -> dict[str, int | None]:
  """Return the counts the runner summary printed, by label."""
  counts: dict[str, int | None] = {}
  for label in ("Total Executed", "Passed", "Skipped", "Failures", "Errors"):
    match = re.search(rf"^{label}: (\d+)$", output, re.MULTILINE)
    counts[label] = int(match.group(1)) if match else None
  return counts


class HttpExchangeTest(unittest.TestCase):
  """Rendering of requests and responses for logs and failure reports."""

  def test_request_shows_identifying_headers_and_redacts_credentials(self):
    """UCP-Agent and request IDs show; Authorization and Signature do not."""
    request = httpx.Request(
      "POST",
      "https://store.example/ucp/checkout-sessions",
      headers={
        "UCP-Agent": 'profile="https://p.example/profile"',
        "Idempotency-Key": "k-1",
        "Authorization": "Bearer secret-token",
        "Signature": "sig1=:abc:",
        "Content-Type": "application/json",
      },
      json={"line_items": [{"item": {"id": "sku"}, "quantity": 1}]},
    )
    text = base_test.describe_request(request)
    self.assertIn("> POST https://store.example/ucp/checkout-sessions", text)
    self.assertIn('profile="https://p.example/profile"', text)
    self.assertIn("k-1", text)
    self.assertIn("authorization: <redacted>", text.lower())
    self.assertNotIn("secret-token", text)
    self.assertNotIn("sig1=:abc:", text)
    self.assertIn('"quantity": 1', text)

  def test_response_and_exchange_clip_large_bodies(self):
    """A response renders status, content type and body; long bodies clip."""
    request = httpx.Request("GET", "https://store.example/ucp/x")
    response = httpx.Response(
      200, json={"status": "incomplete"}, request=request
    )
    text = base_test.describe_response(response)
    self.assertIn("< 200 for GET https://store.example/ucp/x", text)
    self.assertIn("application/json", text)
    self.assertIn('"status": "incomplete"', text)
    exchange = base_test.describe_exchange(response)
    self.assertTrue(exchange.startswith("> GET"))
    self.assertEqual(base_test.describe_exchange(None), "")

    long_body = "x" * (base_test.BODY_LIMIT + 10)
    clipped = base_test.describe_response(
      httpx.Response(200, text=long_body, request=request)
    )
    self.assertIn("(10 more characters)", clipped)

  def test_client_hooks_record_last_response_and_log_when_verbose(self):
    """The hooks keep the last response and log both sides when verbose."""
    probe = _make_base_probe(verbose_http=True)
    self.addCleanup(probe.client.close)

    with mock.patch.object(base_test.logging, "info") as info:
      probe.client.post("/ucp/checkout-sessions", json={"line_items": []})

    self.assertEqual(probe._last_response.status_code, 201)
    exchange = probe.describe_last_exchange()
    self.assertIn(
      "> POST https://store.example/ucp/checkout-sessions", exchange
    )
    self.assertIn('"id": "chk_123"', exchange)
    logged = " ".join(str(call) for call in info.call_args_list)
    self.assertIn("HTTP request", logged)
    self.assertIn("HTTP response", logged)

  def test_hooks_are_quiet_without_flag_but_keep_last_response(self):
    """Without --verbose_http nothing is logged, yet _last_response is kept."""
    probe = _make_base_probe(verbose_http=False)
    self.addCleanup(probe.client.close)

    with mock.patch.object(base_test.logging, "info") as info:
      probe.client.post("/ucp/checkout-sessions", json={"line_items": []})

    self.assertFalse([c for c in info.call_args_list if "HTTP" in str(c)])
    self.assertIsNotNone(probe._last_response)


class LastExchangeTest(unittest.TestCase):
  """A failure or error ends with the test's last HTTP exchange."""

  def test_attached_to_failures_errors_and_subtests(self):
    """The exchange follows the traceback in failures, errors, and subtests."""

    class Case(unittest.TestCase):
      def describe_last_exchange(self):
        return '> POST /x\n< 200 for POST /x\n{"status": "incomplete"}'

      def test_fails(self):
        self.fail("no order")

      def test_errors(self):
        raise KeyError("order")

      def test_subtest_fails(self):
        with self.subTest(part=1):
          self.fail("part one")

    result = _run_cases(Case)

    self.assertEqual(len(result.failures), 2)
    self.assertEqual(len(result.errors), 1)
    for _, formatted in result.failures + result.errors:
      self.assertIn("Last HTTP exchange:", formatted)
      self.assertIn('{"status": "incomplete"}', formatted)
    _, failure_text = result.failures[0]
    self.assertLess(
      failure_text.index("AssertionError"),
      failure_text.index("Last HTTP exchange:"),
    )

  def test_left_alone_without_an_exchange(self):
    """No method, or nothing recorded, leaves the traceback untouched."""

    class Plain(unittest.TestCase):
      def test_fails(self):
        self.fail("plain")

    class Empty(unittest.TestCase):
      def describe_last_exchange(self):
        return ""

      def test_fails(self):
        self.fail("empty")

    result = _run_cases(Plain, Empty)

    self.assertEqual(len(result.failures), 2)
    for _, formatted in result.failures:
      self.assertNotIn("Last HTTP exchange:", formatted)


class SummaryCountsTest(unittest.TestCase):
  """The console summary counts one outcome per test."""

  def test_two_failing_subtests_are_one_failure(self):
    """A test with two failing subtests counts once; passing tests stay."""

    class Case(unittest.TestCase):
      def test_two_subtests_fail(self):
        for part in (1, 2):
          with self.subTest(part=part):
            self.fail(f"part {part}")

      def test_passes(self):
        pass

      def test_skips(self):
        self.skipTest("optional feature not configured")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Case)
    output = _run_runner_suite(suite)

    self.assertEqual(
      _summary_counts(output),
      {
        "Total Executed": 3,
        "Passed": 1,
        "Skipped": 1,
        "Failures": 1,
        "Errors": 0,
      },
      output,
    )

  def test_errors_are_counted_per_test(self):
    """An uncaught exception counts once under Errors."""

    class Case(unittest.TestCase):
      def test_errors(self):
        raise KeyError("order")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Case)
    output = _run_runner_suite(suite)

    self.assertEqual(
      _summary_counts(output),
      {
        "Total Executed": 1,
        "Passed": 0,
        "Skipped": 0,
        "Failures": 0,
        "Errors": 1,
      },
      output,
    )


if __name__ == "__main__":
  unittest.main()
