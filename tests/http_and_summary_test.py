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
  probe._last_request = None
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


def _run_runner_suite(suite: unittest.TestSuite) -> tuple[int, str]:
  """Run `suite` through `runner.main` and return `(exit_code, output)`."""
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
    code = runner.main(["--server_url=https://shop.example"])
  return code, out.getvalue()


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
    """UCP-Agent and request IDs show; credential headers do not."""
    request = httpx.Request(
      "POST",
      "https://store.example/ucp/checkout-sessions",
      headers={
        "UCP-Agent": 'profile="https://p.example/profile"',
        "Idempotency-Key": "k-1",
        "Authorization": "Bearer secret-token",
        "Signature": "sig1=:abc:",
        "Simulation-Secret": "sim-secret-value",
        "X-API-Key": "api-key-value",
        "Content-Type": "application/json",
      },
      json={
        "line_items": [{"item": {"id": "sku"}, "quantity": 1}],
        "note": "Café €",
      },
    )
    text = base_test.describe_request(request)
    self.assertIn("> POST https://store.example/ucp/checkout-sessions", text)
    self.assertIn('profile="https://p.example/profile"', text)
    self.assertIn("k-1", text)
    self.assertIn("authorization: <redacted>", text.lower())
    self.assertIn("simulation-secret: <redacted>", text.lower())
    self.assertIn("x-api-key: <redacted>", text.lower())
    self.assertNotIn("secret-token", text)
    self.assertNotIn("sig1=:abc:", text)
    self.assertNotIn("sim-secret-value", text)
    self.assertNotIn("api-key-value", text)
    self.assertIn('"quantity": 1', text)
    self.assertIn("Café €", text)

  def test_unread_streaming_request_does_not_raise(self):
    """A streaming request whose body has not been read renders safely."""
    request = httpx.Request(
      "POST",
      "https://store.example/ucp/upload",
      content=iter([b"chunk-1", b"chunk-2"]),
    )
    text = base_test.describe_request(request)
    self.assertIn("> POST https://store.example/ucp/upload", text)
    self.assertIn("(body not read)", text)

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

  def test_transport_error_clears_stale_response_and_shows_failed_request(self):
    """A timeout on request 2 renders request 2, not request 1's 201."""

    class Probe(base_test.BaseIntegrationTest):
      def runTest(self):  # noqa: N802
        """Do nothing."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      if calls == 1:
        return httpx.Response(
          201,
          json={"id": "chk_123", "status": "incomplete"},
          request=request,
        )
      raise httpx.ReadTimeout("timed out", request=request)

    probe = Probe()
    probe._verbose_http = False
    probe._last_request = None
    probe._last_response = None
    probe.client = httpx.Client(
      base_url="https://store.example",
      transport=httpx.MockTransport(handler),
      event_hooks={
        "request": [probe._note_request],
        "response": [probe._note_response],
      },
    )
    self.addCleanup(probe.client.close)

    probe.client.post("/ucp/checkout-sessions", json={"line_items": []})
    with self.assertRaises(httpx.ReadTimeout):
      probe.client.get("/ucp/checkout-sessions/second")

    exchange = probe.describe_last_exchange()
    self.assertIn(
      "> GET https://store.example/ucp/checkout-sessions/second", exchange
    )
    self.assertIn("< (no response)", exchange)
    self.assertNotIn("201", exchange)
    self.assertNotIn("chk_123", exchange)


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
    code, output = _run_runner_suite(suite)

    self.assertEqual(code, 1)
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
    code, output = _run_runner_suite(suite)

    self.assertEqual(code, 1)
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

  def test_class_fixture_errors_are_counted_in_summary(self):
    """Failing `setUpClass` and `tearDownClass` increment summary Errors."""

    class BrokenSetUp(unittest.TestCase):
      @classmethod
      def setUpClass(cls):
        raise RuntimeError("setUpClass boom")

      def test_never_runs(self):
        pass

    suite1 = unittest.defaultTestLoader.loadTestsFromTestCase(BrokenSetUp)
    code1, output1 = _run_runner_suite(suite1)
    self.assertEqual(code1, 1)
    self.assertEqual(
      _summary_counts(output1),
      {
        "Total Executed": 0,
        "Passed": 0,
        "Skipped": 0,
        "Failures": 0,
        "Errors": 1,
      },
      output1,
    )

    class BrokenTearDown(unittest.TestCase):
      @classmethod
      def tearDownClass(cls):
        raise RuntimeError("tearDownClass boom")

      def test_runs_and_passes(self):
        pass

    suite2 = unittest.defaultTestLoader.loadTestsFromTestCase(BrokenTearDown)
    code2, output2 = _run_runner_suite(suite2)
    self.assertEqual(code2, 1)
    self.assertEqual(
      _summary_counts(output2),
      {
        "Total Executed": 1,
        "Passed": 1,
        "Skipped": 0,
        "Failures": 0,
        "Errors": 1,
      },
      output2,
    )

  def test_unexpected_success_counts_as_failure(self):
    """An unexpected success increments Failures to match exit code 1."""

    class Case(unittest.TestCase):
      @unittest.expectedFailure
      def test_unexpectedly_passes(self):
        pass

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Case)
    code, output = _run_runner_suite(suite)

    self.assertEqual(code, 1)
    self.assertEqual(
      _summary_counts(output),
      {
        "Total Executed": 1,
        "Passed": 0,
        "Skipped": 0,
        "Failures": 1,
        "Errors": 0,
      },
      output,
    )


if __name__ == "__main__":
  unittest.main()
