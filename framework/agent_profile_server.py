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

"""Background mock agent server serving agent profile configurations."""

import json
from pathlib import Path
import threading
import time
from typing import Any
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import uvicorn

DEFAULT_PROFILE = {
  "agent": {
    "id": "mock-test-agent",
    "name": "Conformance Test Agent",
    "version": "1.0.0",
  },
  "endpoints": {
    "webhook": "http://localhost:{webhook_port}/webhooks/test",
  },
}


class AgentProfileServer:
  """Serves a test agent profile with dynamic port resolution."""

  PROFILE_PATH = "/profiles/agent.json"
  LEGACY_PROFILE_PATH = "/profiles/shopping-agent.json"

  def __init__(
    self,
    *,
    port: int,
    webhook_port: int,
    profile_template_path: str | None = None,
    profile_dict: dict[str, Any] | None = None,
  ):
    """Initialize AgentProfileServer with port and profile configuration.

    Args:
      port: The HTTP port to bind to.
      webhook_port: The mock webhook port to interpolate into endpoints.
      profile_template_path: Optional path to a JSON profile template.
      profile_dict: Optional dictionary to serialize as the profile.

    """
    self.port = port
    self.webhook_port = webhook_port
    self.app = FastAPI()

    if profile_dict is not None:
      self._profile_template = json.dumps(profile_dict)
    elif profile_template_path and Path(profile_template_path).exists():
      with Path(profile_template_path).open(encoding="utf-8") as f:
        self._profile_template = f.read()
    else:
      legacy_path = (
        Path(__file__).resolve().parent.parent / "shopping-agent-test.json"
      )
      if legacy_path.exists():
        with legacy_path.open(encoding="utf-8") as f:
          self._profile_template = f.read()
      else:
        self._profile_template = json.dumps(DEFAULT_PROFILE)

    self._setup_routes()
    self._server: uvicorn.Server | None = None
    self._thread: threading.Thread | None = None

  def _setup_routes(self) -> None:
    async def _respond_profile() -> JSONResponse:
      content = self._profile_template.replace(
        "{webhook_port}", str(self.webhook_port)
      )
      return JSONResponse(content=json.loads(content))

    self.app.get(self.PROFILE_PATH, response_model=None)(_respond_profile)
    self.app.get(self.LEGACY_PROFILE_PATH, response_model=None)(
      _respond_profile
    )

    @self.app.get("/healthz")
    async def health_check() -> dict[str, str]:
      return {"status": "ok"}

  def start(self) -> None:
    """Start the mock agent server in a background thread."""
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
    raise RuntimeError(
      f"AgentProfileServer failed to start on port {self.port}"
    )

  def stop(self) -> None:
    """Stop the background mock agent server."""
    if self._server is not None:
      self._server.should_exit = True
      if self._thread is not None:
        self._thread.join(timeout=3)
