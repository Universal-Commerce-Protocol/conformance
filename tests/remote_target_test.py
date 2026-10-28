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

"""Tests for the options that point the suite at a server elsewhere."""

import contextlib
import io
import os
import unittest
from unittest import mock

from absl.testing import flagsaver
import runner
from framework.base_test import flag_value
from framework.discovery import resolve_discovery_url
from shopping.base import shopping_rest_endpoint


def run_main(*argv: str, names: tuple[str, ...] = ()) -> dict:
  """Run the runner as a dry run with no tests and no server.

  Flags and environment variables are restored afterwards.

  Returns:
    The exit code, the output, the mock that stood in for the discovery
    fetch, and what the tests would read for each of the named flags.

  """
  out = io.StringIO()
  fetch = mock.Mock(side_effect=OSError("offline"))
  with (
    flagsaver.flagsaver(),
    mock.patch.dict(os.environ),
    mock.patch.object(runner, "fetch_server_profile", fetch),
    mock.patch.object(runner, "_load_tests", return_value=unittest.TestSuite()),
    contextlib.redirect_stdout(out),
    contextlib.redirect_stderr(out),
  ):
    code = runner.main(["--dry-run", *argv])
    seen = {name: flag_value(name) for name in names}
  return {"code": code, "output": out.getvalue(), "fetch": fetch, **seen}


class DiscoveryUrlTest(unittest.TestCase):
  """Tests for finding the server's discovery profile."""

  def test_default(self):
    """Test that the profile is at /.well-known/ucp by default."""
    self.assertEqual(
      resolve_discovery_url("http://localhost:8182/"),
      "http://localhost:8182/.well-known/ucp",
    )

  def test_full_url(self):
    """Test that a full URL is used as it is."""
    self.assertEqual(
      resolve_discovery_url(
        "https://shop.example", "https://profiles.example/ucp.json"
      ),
      "https://profiles.example/ucp.json",
    )

  def test_path(self):
    """Test that a path is taken on the server."""
    self.assertEqual(
      resolve_discovery_url("https://shop.example/", "/.well-known/ucp/v2"),
      "https://shop.example/.well-known/ucp/v2",
    )

  def test_runner_fetches_the_profile_from_the_option(self):
    """Test that the runner fetches, names and passes on the profile URL."""
    run = run_main(
      "--server_url=https://shop.example",
      "--discovery_url=/.well-known/ucp/v2",
      names=("discovery_url",),
    )
    url = "https://shop.example/.well-known/ucp/v2"
    self.assertEqual(run["code"], 0, run["output"])
    run["fetch"].assert_called_once_with(
      "https://shop.example", discovery_url=url
    )
    self.assertIn(f"Discovery Profile: {url}\n", run["output"])
    self.assertIn(f"Warning: Failed to fetch {url}: offline", run["output"])
    self.assertEqual(run["discovery_url"], "/.well-known/ucp/v2")

  def test_runner_default(self):
    """Test that without the option the runner uses /.well-known/ucp."""
    run = run_main("--server_url=https://shop.example")
    url = "https://shop.example/.well-known/ucp"
    run["fetch"].assert_called_once_with(
      "https://shop.example", discovery_url=url
    )
    self.assertNotIn("Discovery Profile:", run["output"])
    self.assertIn(f"Warning: Failed to fetch {url}: offline", run["output"])


class ShoppingEndpointTest(unittest.TestCase):
  """Tests for finding the shopping service's REST endpoint."""

  def test_rest_entry_listed_second(self):
    """Test that the REST entry is found after another transport's."""
    profile = {
      "ucp": {
        "services": {
          "dev.ucp.shopping": [
            {"transport": "mcp", "endpoint": "https://shop.example/mcp"},
            {"transport": "rest", "endpoint": "https://shop.example/api"},
          ]
        }
      }
    }
    self.assertEqual(
      shopping_rest_endpoint(profile), "https://shop.example/api"
    )

  def test_single_entry(self):
    """Test an entry given on its own rather than in a list."""
    profile = {
      "services": {
        "dev.ucp.shopping": {
          "transport": "rest",
          "endpoint": "https://shop.example/api",
        }
      }
    }
    self.assertEqual(
      shopping_rest_endpoint(profile), "https://shop.example/api"
    )

  def test_no_rest_entry(self):
    """Test that a service with no REST entry is an error."""
    profile = {
      "services": {
        "dev.ucp.shopping": [
          {"transport": "mcp", "endpoint": "https://shop.example/mcp"}
        ]
      }
    }
    with self.assertRaisesRegex(RuntimeError, "endpoint not found"):
      shopping_rest_endpoint(profile)

  def test_no_shopping_service(self):
    """Test that a profile with no shopping service is an error."""
    with self.assertRaisesRegex(RuntimeError, "service not found"):
      shopping_rest_endpoint({"services": {}})


class FlagValueTest(unittest.TestCase):
  """Tests for reading a flag or its environment variable."""

  def test_flag_comes_first(self):
    """Test that a set flag wins over the environment."""
    with (
      flagsaver.flagsaver(discovery_url="https://a.example/ucp"),
      mock.patch.dict(
        os.environ, {"FLAGS_discovery_url": "https://b.example/ucp"}
      ),
    ):
      self.assertEqual(flag_value("discovery_url"), "https://a.example/ucp")

  def test_environment(self):
    """Test that an unset flag falls back to the environment."""
    with (
      flagsaver.flagsaver(discovery_url=None),
      mock.patch.dict(
        os.environ, {"FLAGS_discovery_url": "https://b.example/ucp"}
      ),
    ):
      self.assertEqual(flag_value("discovery_url"), "https://b.example/ucp")

  def test_neither(self):
    """Test that a flag set nowhere has no value."""
    with flagsaver.flagsaver(discovery_url=None), mock.patch.dict(os.environ):
      os.environ.pop("FLAGS_discovery_url", None)
      self.assertIsNone(flag_value("discovery_url"))

  def test_undefined_flag(self):
    """Test that a flag nobody has defined yet is read from the environment."""
    with mock.patch.dict(os.environ, {"FLAGS_defined_later": "value"}):
      self.assertEqual(flag_value("defined_later"), "value")


if __name__ == "__main__":
  unittest.main()
