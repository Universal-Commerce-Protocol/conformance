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

"""Background mock webhook receiver for capturing incoming server callbacks."""

import contextlib
import threading
import time
from typing import Any
from fastapi import FastAPI, Request
import httpx
import uvicorn


class MockWebhookServer:
  """Captures incoming webhook events for assertion during tests."""

  def __init__(self, port: int):
    """Initialize MockWebhookServer listening on the specified port.

    Args:
      port: The port to listen on.

    """
    self.port = port
    self.app = FastAPI()
    self.events: list[dict[str, Any]] = []
    self._setup_routes()
    self._server: uvicorn.Server | None = None
    self._thread: threading.Thread | None = None

  def _setup_routes(self) -> None:
    @self.app.post("/{full_path:path}")
    async def capture_all(request: Request, full_path: str) -> dict[str, str]:
      headers = dict(request.headers)
      body = await request.body()
      json_payload = None
      with contextlib.suppress(Exception):
        json_payload = await request.json()
      self.events.append(
        {
          "path": full_path,
          "headers": headers,
          "raw_body": body,
          "json": json_payload,
        }
      )
      return {"status": "ok"}

    @self.app.get("/healthz")
    async def health() -> dict[str, str]:
      return {"status": "ok"}

  def start(self) -> None:
    """Start the mock webhook server in a background thread."""
    config = uvicorn.Config(
      self.app, host="0.0.0.0", port=self.port, log_level="error"
    )
    self._server = uvicorn.Server(config)
    self._thread = threading.Thread(target=self._server.run, daemon=True)
    self._thread.start()

    for _ in range(50):
      try:
        with httpx.Client() as client:
          resp = client.get(f"http://localhost:{self.port}/healthz")
          if resp.status_code == 200:
            return
      except httpx.ConnectError:
        time.sleep(0.05)
    raise RuntimeError(f"MockWebhookServer failed to start on port {self.port}")

  def stop(self) -> None:
    """Stop the background mock webhook server."""
    if self._server is not None:
      self._server.should_exit = True
      if self._thread is not None:
        self._thread.join(timeout=3)

  def clear_events(self) -> None:
    """Clear all recorded webhook events."""
    self.events.clear()
