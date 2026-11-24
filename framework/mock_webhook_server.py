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
import json
import threading
import time
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import uvicorn


def _partner_id_from_path(full_path: str) -> str | None:
  """Extract the partner segment from a `.../partners/{id}/...` path.

  Delivery routes are vertical-specific, so the receiver accepts any path
  and recovers the partner identifier positionally when one is present.
  """
  segments = [s for s in full_path.split("/") if s]
  with contextlib.suppress(ValueError, IndexError):
    return segments[segments.index("partners") + 1]
  return None


def _delivery_key(payload: Any, headers: dict[str, str], full_path: str) -> str:
  """Name what a delivery is about, so its attempts can be counted apart.

  The order in the body identifies a delivery. A body without one (a delta,
  or no JSON at all) falls back to the Webhook-Id, which a retry keeps, and
  then to the path. The order comes first on purpose: a sender that gives
  each retry a new Webhook-Id is still retrying the same order, and should
  be refused only as often as asked, so that the retry test reports the
  changed id instead of never seeing a delivery accepted.
  """
  if isinstance(payload, dict):
    order_id = payload.get("id")
    if isinstance(order_id, str) and order_id:
      return f"order:{order_id}"
  webhook_id = headers.get("webhook-id")
  if webhook_id:
    return f"webhook-id:{webhook_id}"
  return f"path:{full_path}"


class MockWebhookServer:
  """Captures incoming webhook events for assertion during tests."""

  def __init__(self, port: int, failures_before_success: int = 0):
    """Initialize MockWebhookServer listening on the specified port.

    Args:
      port: The port to listen on.
      failures_before_success: How many delivery attempts for each order to
        record and then reject with HTTP 500, so retry semantics can be
        exercised. The count is kept per order, not over all deliveries: a
        business that retries may still be delivering an earlier test's
        order when a test starts, and that stray attempt must not use up
        the refusal meant for the test's own order.

    """
    self.port = port
    self.failures_before_success = failures_before_success
    self.app = FastAPI()
    self.events: list[dict[str, Any]] = []
    # Refusals given so far, by what the delivery is about (_delivery_key).
    self._refusals: dict[str, int] = {}
    self._setup_routes()
    self._server: uvicorn.Server | None = None
    self._thread: threading.Thread | None = None

  def _setup_routes(self) -> None:
    @self.app.post("/{full_path:path}", response_model=None)
    async def capture_all(
      request: Request, full_path: str
    ) -> JSONResponse | dict[str, str]:
      headers = dict(request.headers)
      # Keep the raw bytes: Content-Digest (RFC 9530) is computed over the
      # body as transmitted, so verifiers must not re-serialize the JSON.
      raw_body = await request.body()
      payload = None
      with contextlib.suppress(ValueError):
        payload = json.loads(raw_body)
      key = _delivery_key(payload, headers, full_path)
      refused = self._refusals.get(key, 0)
      response_status = 200
      if refused < self.failures_before_success:
        self._refusals[key] = refused + 1
        response_status = 500
      self.events.append(
        {
          "path": full_path,
          "partner_id": _partner_id_from_path(full_path),
          "payload": payload,
          "headers": headers,
          "raw_body": raw_body,
          "response_status": response_status,
        }
      )
      if response_status == 500:
        return JSONResponse(
          status_code=response_status, content={"status": "retry"}
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
    """Clear all recorded webhook events, and the refusal counts with them."""
    self.events.clear()
    self._refusals.clear()
