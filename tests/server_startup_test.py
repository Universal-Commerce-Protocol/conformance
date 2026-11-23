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

"""Regression tests for ownership of mock server startup readiness."""

import socket
from unittest import mock
from absl.testing import absltest
from framework import agent_profile_server, mock_webhook_server
import httpx


class ServerStartupTest(absltest.TestCase):
  """Only a running instance may satisfy its own startup health check."""

  def run_start(
    self, started=False, alive=True, status=200, error=None, delayed=False
  ):
    """Exercise both real startup methods with isolated server resources."""
    for module, server_class in (
      (agent_profile_server, agent_profile_server.AgentProfileServer),
      (mock_webhook_server, mock_webhook_server.MockWebhookServer),
    ):
      with self.subTest(server=server_class.__name__):
        instance = (
          server_class(port=8285, webhook_port=8284, profile_dict={})
          if server_class is agent_profile_server.AgentProfileServer
          else server_class(port=8284)
        )
        with (
          mock.patch.object(module.uvicorn, "Server") as server,
          mock.patch.object(module.threading, "Thread") as thread,
          mock.patch.object(module.httpx, "Client") as client,
          mock.patch.object(module.time, "sleep") as sleep,
        ):
          server.return_value.started = started
          server.return_value.should_exit = False
          thread.return_value.is_alive.return_value = alive
          get = client.return_value.__enter__.return_value.get
          get.return_value.status_code = status
          get.side_effect = error
          if delayed:
            sleep.side_effect = lambda _: setattr(
              server.return_value, "started", True
            )
          if (started or delayed) and alive and status == 200 and error is None:
            instance.start()
            thread.return_value.start.assert_called_once_with()
            client.assert_called_once_with(timeout=1.0)
            get.assert_called_once_with(
              f"http://localhost:{instance.port}/healthz"
            )
            if delayed:
              sleep.assert_called_once_with(0.05)
            else:
              sleep.assert_not_called()
            self.assertFalse(server.return_value.should_exit)
            thread.return_value.join.assert_not_called()
            instance.stop()
          else:
            with self.assertRaisesRegex(
              RuntimeError,
              f"{server_class.__name__} failed to start "
              f"on port {instance.port}",
            ):
              instance.start()
            thread.return_value.start.assert_called_once_with()
            if alive:
              self.assertEqual(get.call_count, 50 if started else 0)
              self.assertEqual(sleep.call_args_list, [mock.call(0.05)] * 50)
              if started:
                self.assertEqual(
                  client.call_args_list, [mock.call(timeout=1.0)] * 50
                )
              else:
                client.assert_not_called()
            else:
              client.assert_not_called()
              sleep.assert_not_called()
          self.assertTrue(server.return_value.should_exit)
          thread.return_value.join.assert_called_once_with(timeout=3)

  def test_server_not_started_cannot_use_another_health_response(self):
    """A healthy listener must not hide failure to start this instance."""
    self.run_start()

  def test_dead_server_stops_without_a_health_probe(self):
    """A terminated server thread fails promptly even if the port is healthy."""
    self.run_start(alive=False)

  def test_successful_start_and_stop_are_preserved(self):
    """A running instance still checks its health and stops on request."""
    self.run_start(started=True)

  def test_connection_failure_cleans_up_after_retries(self):
    """Connection errors exhaust retries and stop the background thread."""
    self.run_start(
      started=True, error=httpx.ConnectError("connection unavailable")
    )

  def test_request_timeout_cleans_up_after_retries(self):
    """Read timeouts follow the same retry and cleanup path."""
    self.run_start(started=True, error=httpx.ReadTimeout("health timed out"))

  def test_delayed_start_waits_before_the_health_probe(self):
    """A server that starts after a wait can still become ready."""
    self.run_start(delayed=True)

  def test_occupied_port_fails_with_real_server_resources(self):
    """An occupied socket prevents either Uvicorn instance from starting."""
    for server_class in (
      agent_profile_server.AgentProfileServer,
      mock_webhook_server.MockWebhookServer,
    ):
      with self.subTest(server=server_class.__name__), socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        port = sock.getsockname()[1]
        instance = (
          server_class(port=port, webhook_port=8284, profile_dict={})
          if server_class is agent_profile_server.AgentProfileServer
          else server_class(port=port)
        )
        exits = []
        run = agent_profile_server.uvicorn.Server.run

        def run_occupied(server, run=run, exits=exits):
          """Record Uvicorn's expected bind-failure exit without hiding it."""
          try:
            run(server)
          except SystemExit as error:
            exits.append(error.code)

        try:
          with (
            mock.patch.object(
              agent_profile_server.uvicorn.Server, "run", run_occupied
            ),
            self.assertRaisesRegex(RuntimeError, "failed to start"),
          ):
            instance.start()
          self.assertEqual(exits, [1])
          self.assertFalse(instance._server.started)
          self.assertTrue(instance._server.should_exit)
          self.assertFalse(instance._thread.is_alive())
          self.assertEqual(sock.getsockname()[1], port)
        finally:
          instance.stop()

  def test_non_successful_health_response_still_fails(self):
    """A bound server with a failing health endpoint is not ready."""
    self.run_start(started=True, status=503)


if __name__ == "__main__":
  absltest.main()
