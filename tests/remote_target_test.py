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
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from absl.testing import flagsaver
import runner
from framework.agent_profile_server import AgentProfileServer
from framework.base_test import agent_profile_url, flag_value, is_local_url
from framework.discovery import resolve_discovery_url
from shopping.base import (
  DEFAULT_SHOPPING_FIXTURES,
  fixture_paths,
  shopping_rest_endpoint,
)


class _Probe(unittest.TestCase):
  """A test that needs no server, so the runner has something to run."""

  def test_nothing(self):
    """Pass."""


def run_main(
  *argv: str, names: tuple[str, ...] = (), dry_run: bool = True
) -> dict:
  """Run the runner on one trivial test with the discovery fetch failing.

  A dry run lists the test and returns before contacting the server; a real
  run fetches the discovery profile first. Flags and environment variables
  are restored afterwards.

  Returns:
    The exit code, the output, the mock that stood in for the discovery
    fetch, and what the tests would read for each of the named flags.

  """
  out = io.StringIO()
  fetch = mock.Mock(side_effect=OSError("offline"))
  suite = unittest.TestSuite([_Probe("test_nothing")])
  with (
    flagsaver.flagsaver(),
    mock.patch.dict(os.environ),
    mock.patch.object(runner, "fetch_server_profile", fetch),
    mock.patch.object(runner, "_load_tests", return_value=(suite, [])),
    contextlib.redirect_stdout(out),
    contextlib.redirect_stderr(out),
  ):
    code = runner.main([*(["--dry-run"] if dry_run else []), *argv])
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
      dry_run=False,
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
    run = run_main("--server_url=https://shop.example", dry_run=False)
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


def write_target(directory: str, data: object) -> None:
  """Write a target file into a folder."""
  (Path(directory) / runner.TARGET_FILE).write_text(
    json.dumps(data), encoding="utf-8"
  )


class TargetDirTest(unittest.TestCase):
  """Tests for reading a server's options from a target folder."""

  def test_load(self):
    """Test the options read, paths resolved and keys ignored."""
    with tempfile.TemporaryDirectory() as directory:
      write_target(
        directory,
        {
          "name": "a shop",
          "server_url": "https://shop.example",
          "discovery_url": "/.well-known/ucp/v2",
          "fixture_config": "fixtures.json",
          "test_data_dir": "/data",
          "webhook_base_url": "",
        },
      )
      target = runner.load_target(directory)
    self.assertEqual(
      target,
      {
        "server_url": "https://shop.example",
        "discovery_url": "/.well-known/ucp/v2",
        "fixture_config": f"{directory}/fixtures.json",
        "test_data_dir": "/data",
      },
    )

  def test_command_line_wins(self):
    """Test that options on the command line override the target's."""
    with tempfile.TemporaryDirectory() as directory:
      write_target(
        directory,
        {
          "server_url": "https://shop.example",
          "discovery_url": "/.well-known/ucp/v2",
        },
      )
      args = runner.parse_args(
        [f"--target_dir={directory}", "--server_url=http://localhost:9"]
      )
    self.assertEqual(args.server_url, "http://localhost:9")
    self.assertEqual(args.discovery_url, "/.well-known/ucp/v2")

  def test_server_url_defaults(self):
    """Test the server URL's fallbacks when no target gives one."""
    with mock.patch.dict(os.environ):
      os.environ.pop("UCP_SERVER_URL", None)
      self.assertEqual(
        runner.parse_args([]).server_url, runner.DEFAULT_SERVER_URL
      )
      os.environ["UCP_SERVER_URL"] = "https://env.example"
      self.assertEqual(runner.parse_args([]).server_url, "https://env.example")
      with tempfile.TemporaryDirectory() as directory:
        write_target(directory, {"server_url": "https://shop.example"})
        args = runner.parse_args([f"--target_dir={directory}"])
      self.assertEqual(args.server_url, "https://shop.example")

  def test_bad_targets(self):
    """Test that an unusable target file is a usage error."""
    with tempfile.TemporaryDirectory() as directory:
      cases = {
        "missing": (None, "no target.json in"),
        "not_json": ("{", "cannot read"),
        "list": ([], "is not a JSON object"),
        "number": ({"server_url": 8182}, "server_url is not a string"),
      }
      for name, (data, message) in cases.items():
        with self.subTest(name=name):
          folder = Path(directory) / name
          folder.mkdir()
          if isinstance(data, str):
            (folder / runner.TARGET_FILE).write_text(data, encoding="utf-8")
          elif data is not None:
            write_target(str(folder), data)
          err = io.StringIO()
          with (
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit),
          ):
            runner.parse_args([f"--target_dir={folder}"])
          self.assertIn(message, err.getvalue())

  def test_runner_uses_the_target(self):
    """Test a run that takes its server and profile from a target."""
    with tempfile.TemporaryDirectory() as directory:
      write_target(
        directory,
        {
          "server_url": "https://shop.example",
          "discovery_url": "/.well-known/ucp/v2",
          "agent_profile_url": "https://agent.example/profiles/agent.json",
          "webhook_base_url": "https://hooks.example",
        },
      )
      run = run_main(
        f"--target_dir={directory}", names=("discovery_url",), dry_run=False
      )
    self.assertEqual(run["code"], 0, run["output"])
    self.assertIn("Target Server: https://shop.example\n", run["output"])
    self.assertIn(f"Target Folder: {directory}\n", run["output"])
    self.assertNotIn("Note:", run["output"])
    run["fetch"].assert_called_once_with(
      "https://shop.example",
      discovery_url="https://shop.example/.well-known/ucp/v2",
    )
    self.assertEqual(run["discovery_url"], "/.well-known/ucp/v2")


class PublicAddressTest(unittest.TestCase):
  """Tests for the addresses a server on another machine can reach."""

  def test_is_local_url(self):
    """Test which servers count as on this machine."""
    for url in (
      "http://localhost:8182",
      "http://127.0.0.1:8182",
      "http://[::1]:8182",
      "http://0.0.0.0:8182",
      "http://shop.localhost",
    ):
      with self.subTest(url=url):
        self.assertTrue(is_local_url(url))
    for url in ("https://shop.example", "http://192.168.1.20:8182"):
      with self.subTest(url=url):
        self.assertFalse(is_local_url(url))

  def test_profile_webhook_address(self):
    """Test that a webhook base URL replaces the localhost address."""
    template = {
      "webhook": "http://localhost:{webhook_port}/webhooks/test",
      "version": "{ucp_version}",
    }
    local = AgentProfileServer(
      port=0, webhook_port=8284, ucp_version="2026-04-08", profile_dict=template
    )
    public = AgentProfileServer(
      port=0,
      webhook_port=8284,
      ucp_version="2026-04-08",
      profile_dict=template,
      webhook_base_url="https://hooks.example/",
    )
    self.assertEqual(
      local.render_profile(),
      {
        "webhook": "http://localhost:8284/webhooks/test",
        "version": "2026-04-08",
      },
    )
    self.assertEqual(
      public.render_profile(),
      {
        "webhook": "https://hooks.example/webhooks/test",
        "version": "2026-04-08",
      },
    )

  def test_default_profile_template(self):
    """Test the repository's agent profile with a webhook base URL."""
    server = AgentProfileServer(
      port=0, webhook_port=8284, webhook_base_url="https://hooks.example"
    )
    text = json.dumps(server.render_profile())
    self.assertIn(
      "https://hooks.example/webhooks/partners/test_partner/events/order", text
    )
    self.assertNotIn("localhost", text)

  def test_agent_profile_url(self):
    """Test the UCP-Agent profile URL, by default and when set."""
    with (
      flagsaver.flagsaver(agent_profile_url=None),
      mock.patch.dict(os.environ),
    ):
      os.environ.pop("FLAGS_agent_profile_url", None)
      self.assertRegex(
        agent_profile_url(), r"^http://localhost:\d+/profiles/agent\.json$"
      )
    with flagsaver.flagsaver(
      agent_profile_url="https://agent.example/profiles/agent.json"
    ):
      self.assertEqual(
        agent_profile_url(), "https://agent.example/profiles/agent.json"
      )

  def test_runner_notes_for_a_remote_server(self):
    """Test the runner's notes about addresses a remote server lacks."""
    run = run_main("--server_url=https://shop.example")
    self.assertIn("needs --agent_profile_url", run["output"])
    self.assertIn("webhook tests will skip", run["output"])

  def test_runner_passes_the_addresses(self):
    """Test that the runner passes both addresses and makes no notes."""
    run = run_main(
      "--server_url=https://shop.example",
      "--agent_profile_url=https://agent.example/profiles/agent.json",
      "--webhook_base_url=https://hooks.example",
      names=("agent_profile_url", "webhook_base_url"),
    )
    self.assertNotIn("Note:", run["output"])
    self.assertEqual(
      run["agent_profile_url"], "https://agent.example/profiles/agent.json"
    )
    self.assertEqual(run["webhook_base_url"], "https://hooks.example")

  def test_runner_no_notes_for_a_local_server(self):
    """Test that a server on this machine needs no public addresses."""
    run = run_main("--server_url=http://localhost:8182")
    self.assertNotIn("Note:", run["output"])


_FIXTURE_FLAGS = (
  "conformance_input",
  "fixture_config",
  "test_data_dir",
)


@contextlib.contextmanager
def no_fixture_options():
  """Clear the fixture flags and their environment; restore them after."""
  with (
    flagsaver.flagsaver(**dict.fromkeys(_FIXTURE_FLAGS)),
    mock.patch.dict(os.environ),
  ):
    for name in _FIXTURE_FLAGS:
      os.environ.pop(f"FLAGS_{name}", None)
    yield


class FixturePathsTest(unittest.TestCase):
  """Tests for choosing the shopping tests' fixture files."""

  def test_defaults(self):
    """Test that the flower shop's files are used when nothing is set."""
    with no_fixture_options():
      paths = fixture_paths()
    self.assertEqual(
      paths,
      {
        "conformance_input": f"{DEFAULT_SHOPPING_FIXTURES}/"
        "conformance_input.json",
        "fixture_config": f"{DEFAULT_SHOPPING_FIXTURES}/test_fixtures.json",
        "data_dir": DEFAULT_SHOPPING_FIXTURES,
      },
    )

  def test_general_flags(self):
    """Test that the general flags are no longer shadowed by defaults."""
    with (
      no_fixture_options(),
      flagsaver.flagsaver(
        conformance_input="shop/input.json",
        fixture_config="shop/fixtures.json",
        test_data_dir="shop",
      ),
    ):
      paths = fixture_paths()
    self.assertEqual(
      paths,
      {
        "conformance_input": "shop/input.json",
        "fixture_config": "shop/fixtures.json",
        "data_dir": "shop",
      },
    )

  def test_environment(self):
    """Test the runner's environment variables, as pytest runs see them."""
    with no_fixture_options():
      os.environ["FLAGS_fixture_config"] = "shop/fixtures.json"  # noqa: SIM112
      os.environ["FLAGS_test_data_dir"] = "shop"  # noqa: SIM112
      paths = fixture_paths()
    self.assertEqual(paths["fixture_config"], "shop/fixtures.json")
    self.assertEqual(paths["data_dir"], "shop")

  def test_runner_passes_the_options(self):
    """Test that the runner's options reach the tests."""
    fixture = f"{DEFAULT_SHOPPING_FIXTURES}/test_fixtures.json"
    with no_fixture_options():
      run = run_main(
        f"--fixture_config={fixture}",
        f"--test_data_dir={DEFAULT_SHOPPING_FIXTURES}",
        names=("fixture_config", "test_data_dir"),
      )
    self.assertEqual(run["code"], 0, run["output"])
    self.assertEqual(run["fixture_config"], fixture)
    self.assertEqual(run["test_data_dir"], DEFAULT_SHOPPING_FIXTURES)

  def test_runner_stops_on_missing_paths(self):
    """Test that paths that name nothing stop the run before it starts."""
    with no_fixture_options():
      run = run_main(
        "--conformance_input=nowhere/input.json",
        f"--fixture_config={DEFAULT_SHOPPING_FIXTURES}",
        "--test_data_dir=nowhere",
      )
    self.assertEqual(run["code"], 1)
    for message in (
      "--conformance_input: no such file: nowhere/input.json",
      f"--fixture_config: no such file: {DEFAULT_SHOPPING_FIXTURES}",
      "--test_data_dir: no such directory: nowhere",
    ):
      self.assertIn(message, run["output"])
    run["fetch"].assert_not_called()


if __name__ == "__main__":
  unittest.main()
