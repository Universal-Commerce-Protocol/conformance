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

"""Shared utilities for UCP SDK integration tests."""

import csv
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from absl import flags
from absl.testing import absltest
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
from ucp_sdk.models.schemas.shopping import checkout_create_request
from ucp_sdk.models.schemas.shopping import checkout as f_models
from ucp_sdk.models.schemas.shopping import payment_create_request
from ucp_sdk.models.schemas.shopping import payment_update_request
from ucp_sdk.models.schemas.shopping.checkout_update_request import (
  CheckoutUpdateRequest,
)
from ucp_sdk.models.schemas.shopping.types import (
  fulfillment_group_create_request,
)
from ucp_sdk.models.schemas.shopping.types import (
  fulfillment_method_create_request,
)
from ucp_sdk.models.schemas.shopping.types import item_create_request
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_create_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request
from ucp_sdk.models.schemas import payment_handler
from ucp_sdk.models.schemas.shopping.types import shipping_destination
import uvicorn


class UnifiedUpdate(CheckoutUpdateRequest):
  """Client-side unified update model to support extensions."""


FLAGS = flags.FLAGS
try:
  flags.DEFINE_string(
    "server_url", os.getenv("SERVER_URL"), "Base URL of the server"
  )
  flags.DEFINE_string(
    "simulation_secret",
    os.getenv("SIMULATION_SECRET", str(uuid.uuid4())),
    "Secret for simulation endpoints",
  )
  flags.DEFINE_integer(
    "mock_webhook_port", 8284, "Port for the mock webhook server"
  )
  flags.DEFINE_integer(
    "mock_agent_port", 8285, "Port for the mock agent profile server"
  )
  flags.DEFINE_bool("verbose_http", False, "Whether to log HTTP requests.")
  flags.DEFINE_string(
    "conformance_input",
    "test_data/flower_shop/conformance_input.json",
    "Path to conformance input configuration JSON.",
  )
  flags.DEFINE_string(
    "fixture_config",
    "test_data/flower_shop/test_fixtures.json",
    "Path to test fixtures configuration JSON.",
  )
  flags.DEFINE_string(
    "test_data_dir",
    "test_data/flower_shop",
    "Directory containing test CSV data.",
  )
except flags.DuplicateFlagError:
  pass


class TestData:
  """Holder for loaded test data."""

  def __init__(self) -> None:
    """Initialize TestData."""
    self.payment_instruments: list[dict[str, Any]] = []
    self.addresses: list[dict[str, Any]] = []

  def load(self, data_dir: str) -> None:
    """Load data from CSV files in the given directory."""
    pi_path = Path(data_dir) / "payment_instruments.csv"
    if pi_path.exists():
      with pi_path.open() as f:
        self.payment_instruments = list(csv.DictReader(f))

    addr_path = Path(data_dir) / "addresses.csv"
    if addr_path.exists():
      with addr_path.open() as f:
        self.addresses = list(csv.DictReader(f))


# Global instance
test_data = TestData()


def get_headers(
  idempotency_key: str | None = None, request_id: str | None = None
) -> dict[str, str]:
  """Generate headers for UCP requests.

  Args:
      idempotency_key: Optional specific idempotency key.
      request_id: Optional specific request ID.

  Returns:
      A dictionary of HTTP headers including UCP-Agent, signature, and keys.

  """
  profile_url = (
    f"http://localhost:{FLAGS.mock_agent_port}{AgentProfileServer.PROFILE_PATH}"
  )
  return {
    "UCP-Agent": f'profile="{profile_url}"',
    "request-signature": "test",
    "idempotency-key": idempotency_key or str(uuid.uuid4()),
    "request-id": request_id or str(uuid.uuid4()),
  }


def get_valid_payment_payload(
  instrument_id: str = "instr_1", address_id: str = "addr_1"
) -> dict[str, Any]:
  """Return a valid payment payload using loaded test data."""
  # Find instrument
  instr_data = next(
    (pi for pi in test_data.payment_instruments if pi["id"] == instrument_id),
    None,
  )
  if not instr_data:
    # Fallback to hardcoded if not loaded (e.g. unit tests without files)
    instr_data = {
      "id": "instr_1",
      "type": "card",
      "brand": "Visa",
      "last_digits": "1234",
      "token": "success_token",
      "handler_id": "mock_payment_handler",
    }

  # Find address
  addr_data = next(
    (a for a in test_data.addresses if a["id"] == address_id), None
  )
  if not addr_data:
    addr_data = {
      "street_address": "123 Main St",
      "city": "Anytown",
      "state": "CA",
      "postal_code": "12345",
      "country": "US",
    }

  # Construct Billing Address
  billing_address = {
    "street_address": addr_data.get("street_address"),
    "address_locality": addr_data.get("city"),
    "address_region": addr_data.get("state"),
    "address_country": addr_data.get("country"),
    "postal_code": addr_data.get("postal_code"),
  }

  payment_instrument = {
    "id": instr_data["id"],
    "handler_id": instr_data["handler_id"],
    "type": instr_data["type"],
    "display": {
      "brand": instr_data["brand"],
      "last_digits": instr_data["last_digits"],
    },
    "credential": {"type": "token", "token": instr_data["token"]},
    "billing_address": billing_address,
  }

  return {
    "payment": {"instruments": [payment_instrument]},
    "risk_signals": {},
  }


class AgentProfileServer:
  """A background mock agent server that serves the agent profile."""

  PROFILE_PATH = "/profiles/shopping-agent.json"

  def __init__(self, *, port: int, webhook_port: int):
    """Initialize the AgentProfileServer.

    Args:
      port: The port to listen on.
      webhook_port: The port where the webhook server is listening.

    """
    self.port = port
    self.webhook_port = webhook_port
    self.app = FastAPI()

    # Resolve and pre-read the profile template to avoid repeated file I/O
    current_dir = Path(__file__).resolve().parent
    self.profile_path = current_dir / "shopping-agent-test.json"
    with self.profile_path.open() as f:
      self._profile_template = f.read()

    self._setup_routes()
    self._server: uvicorn.Server | None
    self._thread: threading.Thread | None

  def _setup_routes(self) -> None:
    """Set up the routes for the mock agent server."""

    @self.app.get(self.PROFILE_PATH, response_model=None)
    async def get_profile() -> JSONResponse:
      """Return the agent profile with the correct webhook port injected."""
      # Dynamically inject the correct webhook port into the cached template
      content = self._profile_template.replace(
        "{webhook_port}", str(self.webhook_port)
      )
      content_dict = json.loads(content)
      return JSONResponse(content=content_dict)

    @self.app.get("/healthz")
    async def health_check() -> dict[str, str]:
      """Return a simple health check response."""
      return {"status": "ok"}

  def start(self) -> None:
    """Start the mock server in a background thread."""
    config = uvicorn.Config(
      self.app, host="0.0.0.0", port=self.port, log_level="error"
    )
    self._server = uvicorn.Server(config)
    self._thread = threading.Thread(target=self._server.run, daemon=True)
    self._thread.start()
    # Wait for server to start
    for _ in range(50):
      try:
        with httpx.Client() as client:
          if (
            client.get(f"http://localhost:{self.port}/healthz").status_code
            == 200
          ):
            break
      except httpx.ConnectError:
        time.sleep(0.1)
    else:
      raise RuntimeError(f"Server failed to start on port {self.port}")

  def stop(self) -> None:
    """Stop the mock server."""
    if self._server is not None:
      self._server.should_exit = True
      if self._thread is not None:
        self._thread.join(timeout=5)


class MockWebhookServer:
  """A background mock webhook server that records incoming events."""

  def __init__(self, port: int):
    """Initialize the MockWebhookServer.

    Args:
      port: The port to listen on.

    """
    self.port = port
    self.app = FastAPI()
    self.events: list[dict[str, Any]] = []
    self._setup_routes()
    self._server: uvicorn.Server | None
    self._thread: threading.Thread | None

  def _setup_routes(self) -> None:
    """Set up the routes for the mock server."""

    @self.app.post("/webhooks/partners/{partner_id}/events/order")
    async def order_event(partner_id: str, request: Request) -> dict[str, str]:
      """Record an incoming order event."""
      payload = await request.json()
      self.events.append({"partner_id": partner_id, "payload": payload})
      return {"status": "ok"}

    @self.app.get("/healthz")
    async def health_check() -> dict[str, str]:
      """Return a simple health check response."""
      return {"status": "ok"}

  def start(self) -> None:
    """Start the mock server in a background thread."""
    config = uvicorn.Config(
      self.app, host="0.0.0.0", port=self.port, log_level="error"
    )
    self._server = uvicorn.Server(config)
    self._thread = threading.Thread(target=self._server.run, daemon=True)
    self._thread.start()
    # Wait for server to start
    for _ in range(50):
      try:
        with httpx.Client() as client:
          if (
            client.get(f"http://localhost:{self.port}/healthz").status_code
            == 200
          ):
            break
      except httpx.ConnectError:
        time.sleep(0.1)
    else:
      raise RuntimeError(f"Server failed to start on port {self.port}")

  def stop(self) -> None:
    """Stop the mock server."""
    if self._server is not None:
      self._server.should_exit = True
      if self._thread is not None:
        self._thread.join(timeout=5)

  def clear_events(self) -> None:
    """Clear all recorded events."""
    self.events = []


class DynamicFixtureContext:
  """Context for loading dynamic test fixtures from configuration."""

  def __init__(
    self,
    config_path: str | Path | dict[str, Any],
    fallback_config: dict[str, Any] | None = None,
  ):
    """Initialize DynamicFixtureContext from file path or dict."""
    if isinstance(config_path, (str, Path)):
      try:
        with Path(config_path).open() as f:
          self._config = json.load(f)
      except (FileNotFoundError, OSError):
        self._config = {}
    elif isinstance(config_path, dict):
      self._config = config_path
    else:
      self._config = {}
    self._fallback_config = fallback_config or {}

  def get_test_sku(self) -> str:
    """Get the test SKU or item ID to use in checkout tests."""
    val = self._config.get("test_sku")
    if val is None:
      val = self._fallback_config.get("test_sku")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_item", {}).get("sku")
    if val is None:
      val = (
        self._fallback_config.get("test_fixtures", {})
        .get("valid_item", {})
        .get("sku")
      )
    if val is not None:
      return str(val)

    items = self._config.get("items", [{}])
    if not items or items == [{}]:
      items = self._fallback_config.get("items", [{}])
    if items and isinstance(items, list) and len(items) > 0:
      return str(items[0].get("id", "item_1"))
    return "item_1"

  def get_test_price(self) -> int:
    """Get the expected price for the valid item in minor units."""
    val = self._config.get("test_price")
    if val is None:
      val = self._fallback_config.get("test_price")
    if val is not None:
      if isinstance(val, (int, float)):
        return int(round(val * 100))
      return int(val)

    val = (
      self._config.get("test_fixtures", {})
      .get("valid_item", {})
      .get("expected_price")
    )
    if val is None:
      val = (
        self._fallback_config.get("test_fixtures", {})
        .get("valid_item", {})
        .get("expected_price")
      )
    if val is not None:
      if isinstance(val, (int, float)):
        return int(round(val * 100))
      return int(val)

    items = self._config.get("items", [{}])
    if not items or items == [{}]:
      items = self._fallback_config.get("items", [{}])
    if items and isinstance(items, list) and len(items) > 0:
      return int(items[0].get("price", 3500))
    return 3500

  def get_test_destination(self) -> dict[str, Any]:
    """Get the destination address dictionary for shipping."""
    val = self._config.get("test_destination")
    if val is None:
      val = self._fallback_config.get("test_destination")

    if val is not None and isinstance(val, dict):
      dest = dict(val)
    else:
      dest = self._config.get("shipping_locations", {}).get(
        "domestic_destination", {}
      )
      if not dest:
        dest = self._fallback_config.get("shipping_locations", {}).get(
          "domestic_destination", {}
        )
      dest = dict(dest) if dest else {}

    if not dest:
      dest = {
        "street": "123 Market St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "US",
      }
    dest.setdefault("address_country", dest.get("country", "US"))
    dest.setdefault("postal_code", dest.get("postal_code", "94105"))
    dest.setdefault("locality", dest.get("city", "San Francisco"))
    dest.setdefault("region", dest.get("state", "CA"))
    dest.setdefault("street_address", dest.get("street", "123 Market St"))
    return dest

  def get_test_discount_code(self) -> str:
    """Get a valid discount code for tests."""
    val = self._config.get("test_discount_code")
    if val is None:
      val = self._fallback_config.get("test_discount_code")
    if val is not None:
      return str(val)

    val = self._config.get("test_fixtures", {}).get("valid_discount_code")
    if val is None:
      val = self._fallback_config.get("test_fixtures", {}).get(
        "valid_discount_code"
      )
    if val is not None:
      return str(val)
    return "SPRING20"


ConfiguredFixtureContext = DynamicFixtureContext


class IntegrationTestBase(absltest.TestCase):
  """Base class for UCP integration tests providing setup and helper methods."""

  def setUp(self) -> None:
    """Set up the test case, including clients and mock servers."""
    super().setUp()
    self.base_url = FLAGS.server_url
    self.client = httpx.Client(base_url=self.base_url)

    # Configure httpx logging based on flag
    httpx_logger = logging.getLogger("httpx")
    if FLAGS.verbose_http:
      httpx_logger.setLevel(logging.INFO)
    else:
      httpx_logger.setLevel(logging.WARNING)

    # Load conformance input configuration
    try:
      with Path(FLAGS.conformance_input).open() as f:
        self.conformance_config = json.load(f)
    except FileNotFoundError:
      logging.warning(
        "Conformance input file not found at %s. Using defaults.",
        FLAGS.conformance_input,
      )
      self.conformance_config = {}

    # Load fixture configuration
    fixture_config = {}
    if FLAGS.fixture_config:
      try:
        with Path(FLAGS.fixture_config).open() as f:
          fixture_config = json.load(f)
      except FileNotFoundError:
        logging.warning(
          "Fixture config file not found at %s. Using defaults.",
          FLAGS.fixture_config,
        )

    self.fixture_ctx = DynamicFixtureContext(
      fixture_config, fallback_config=self.conformance_config
    )

    # Load CSV Test Data
    try:
      # Resolve relative to this file if not absolute
      data_dir = FLAGS.test_data_dir
      if not Path(data_dir).is_absolute():
        # Assumption: run from where this file is reachable via relative path
        # Actually, FLAGS.test_data_dir is passed by run_conformance.sh
        # Let's try to resolve it.
        pass
      test_data.load(data_dir)
    except Exception as e:  # pylint: disable=broad-exception-caught
      logging.warning("Failed to load test CSV data: %s", e)

    # Start the agent profile server
    self.agent_server = AgentProfileServer(
      port=FLAGS.mock_agent_port, webhook_port=FLAGS.mock_webhook_port
    )
    self.agent_server.start()
    self._shopping_service_endpoint: str | None = None

  @property
  def shopping_service_endpoint(self) -> str:
    """Cached property for the shopping service endpoint."""
    if self._shopping_service_endpoint is None:
      discovery_resp = self.client.get("/.well-known/ucp")
      self.assert_response_status(discovery_resp, 200)

      profile_data = discovery_resp.json()
      # Support both wrapped and unwrapped (UCP wrapper)
      ucp_data = profile_data.get("ucp", profile_data)
      # UCP 01-23 validation changed dicts to lists
      shopping_services = ucp_data.get("services", {}).get(
        "dev.ucp.shopping", []
      )
      if not shopping_services:
        raise RuntimeError("Shopping service not found in discovery profile")

      shopping_service = (
        shopping_services[0]
        if isinstance(shopping_services, list)
        else shopping_services
      )

      endpoint = (
        shopping_service.get("endpoint")
        if shopping_service and shopping_service.get("transport") == "rest"
        else None
      )
      if not endpoint:
        raise RuntimeError(
          "Shopping service endpoint not found in discovery profile"
        )
      self._shopping_service_endpoint = str(endpoint)
    return self._shopping_service_endpoint

  def get_shopping_url(self, path: str) -> str:
    """Construct a full URL for the shopping service.

    Args:
        path: The path to append to the service endpoint
          (e.g., '/checkout-sessions').

    Returns:
        The full URL.

    """
    base = self.shopping_service_endpoint.rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"

  def get_order_url(self, order_id: str) -> str:
    """Construct a full URL for an order resource."""
    return self.get_shopping_url(f"/orders/{order_id}")

  def tearDown(self) -> None:
    """Tear down the test case, stopping servers and clients."""
    self.client.close()
    if hasattr(self, "agent_server"):
      self.agent_server.stop()
    super().tearDown()

  def create_checkout_payload(
    self,
    quantity=1,
    item_id: str | None = None,
    currency: str | None = None,
    handlers=None,
    buyer: dict[str, Any] | None = None,
    include_fulfillment: bool = True,
  ) -> checkout_create_request.CheckoutCreateRequest:
    """Create a valid checkout creation payload.

    Args:
        quantity: Number of items to purchase. Defaults to 1.
        item_id: ID of the item. Defaults to config or "item_1".
        currency: Currency code. Defaults to config or "USD".
        handlers: Optional list of payment handlers. If None, defaults to Google
          Pay.
        buyer: Optional buyer information dictionary.
        include_fulfillment: Whether to include default fulfillment details.

    Returns:
        A CheckoutCreateRequest object populated with the specified data.

    """
    # Load defaults dynamically via fixture context or fallback to config
    ctx = getattr(self, "fixture_ctx", None) or DynamicFixtureContext(
      getattr(self, "conformance_config", {})
    )

    if item_id is None:
      item_id = ctx.get_test_sku()
    if currency is None:
      currency = getattr(self, "conformance_config", {}).get("currency", "USD")

    if handlers is None:
      handlers = [
        payment_handler.Base(
          id="google_pay",
          name="google.pay",
          version="2026-04-08",
          spec="https://example.com/spec",
          config_schema="https://example.com/schema",
          instrument_schemas=["https://example.com/instrument_schema"],
          config={},
        )
      ]

    item = item_create_request.ItemCreateRequest(id=item_id)
    line_item = line_item_create_request.LineItemCreateRequest(
      quantity=quantity, item=item
    )

    # PaymentCreateRequest allows extra fields, so passing handlers is valid
    payment = payment_create_request.PaymentCreateRequest(
      instruments=[],
      handlers=[h.model_dump(mode="json", exclude_none=True) for h in handlers],
    )

    fulfillment = None
    if include_fulfillment:
      # Hierarchical Fulfillment Construction using dynamic destination
      dest_data = ctx.get_test_destination()
      destination = shipping_destination.ShippingDestination(
        id="dest_1",
        address_country=dest_data.get(
          "address_country", dest_data.get("country", "US")
        ),
        postal_code=dest_data.get("postal_code", "94105"),
        locality=dest_data.get("locality", dest_data.get("city")),
        region=dest_data.get("region", dest_data.get("state")),
        street_address=dest_data.get("street_address", dest_data.get("street")),
      )
      group = fulfillment_group_create_request.FulfillmentGroupCreateRequest(
        id="group_1",
        line_item_ids=["line_item_123"],
        selected_option_id="std-ship",
      )
      method = fulfillment_method_create_request.FulfillmentMethodCreateRequest(
        id="method_1",
        type="shipping",
        destinations=[destination],
        line_item_ids=["line_item_123"],
        selected_destination_id="dest_1",
        groups=[group],
      )
      fulfillment = {
        "methods": [
          method.model_dump(mode="json", exclude_none=True, by_alias=True)
        ]
      }

    # Set response fields on model objects for server validation workaround
    item.price = ctx.get_test_price()
    line_item.id = "line_item_123"
    line_item.totals = []

    checkout_req = checkout_create_request.CheckoutCreateRequest(
      id=str(uuid.uuid4()),
      currency=currency,
      line_items=[line_item],
      payment=payment,
      buyer=buyer,
      fulfillment=fulfillment,
    )
    checkout_req.status = "incomplete"
    checkout_req.ucp = {"version": "2026-04-08"}
    checkout_req.totals = []
    checkout_req.links = []

    return checkout_req

  def get_headers(
    self, idempotency_key: str | None = None, request_id: str | None = None
  ) -> dict[str, str]:
    """Generate headers for UCP requests (instance method).

    Args:
        idempotency_key: Optional specific idempotency key.
        request_id: Optional specific request ID.

    Returns:
        A dictionary of HTTP headers including UCP-Agent, signature, and keys.

    """
    return get_headers(idempotency_key, request_id)

  def assert_response_status(
    self, response: httpx.Response, expected_code: int | list[int]
  ) -> None:
    """Assert that the response status code matches the expected code(s).

    Args:
        response: The httpx response object.
        expected_code: An integer or list of integers representing valid status
          codes.

    Raises:
        AssertionError: If the response status code is not in expected_code.

    """
    if isinstance(expected_code, int):
      expected_codes = [expected_code]
    else:
      expected_codes = expected_code

    self.assertIn(
      response.status_code,
      expected_codes,
      msg=(
        f"Expected status {expected_code}, got {response.status_code}."
        f" Resp: {response.text}"
      ),
    )

  def create_checkout_session(
    self,
    quantity: int = 1,
    item_id: str | None = None,
    currency: str | None = None,
    handlers: list[Any] | None = None,
    buyer: dict[str, Any] | None = None,
    select_fulfillment: bool = True,
    headers: dict[str, str] | None = None,
  ) -> Any:
    """Create a checkout session and return the response JSON.

    Args:
        quantity: Number of items to purchase. Defaults to 1.
        item_id: ID of the item. Defaults to config or "item_1".
        currency: Currency code. Defaults to config or "USD".
        handlers: Optional list of payment handlers. If None, defaults to Google
          Pay.
        buyer: Optional buyer information dictionary.
        select_fulfillment: Whether to automatically select a fulfillment
          option. Defaults to True.
        headers: Optional headers to include in the request.

    Returns:
        The JSON response dictionary from the create request.

    """
    create_payload = self.create_checkout_payload(
      quantity=quantity,
      item_id=item_id,
      currency=currency,
      handlers=handlers,
      buyer=buyer,
      include_fulfillment=select_fulfillment,
    )

    request_headers = self.get_headers()
    if headers:
      request_headers.update(headers)

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=request_headers,
    )
    self.assert_response_status(response, [200, 201])
    checkout_data = response.json()

    if select_fulfillment:
      checkout_data = self.ensure_fulfillment_ready(checkout_data["id"])

    return checkout_data

  def ensure_fulfillment_ready(self, checkout_id: str) -> Any:
    """Ensure a fulfillment option is selected for the checkout.

    Args:
        checkout_id: The ID of the checkout to check and update.

    Returns:
        The updated checkout data dictionary.

    """
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_data = response.json()

    # Helper to check if ready
    def is_ready(data):
      if not data.get("fulfillment") or not data["fulfillment"].get("methods"):
        return False
      method = data["fulfillment"]["methods"][0]
      if not method.get("selected_destination_id"):
        return False
      return method.get("groups") and method["groups"][0].get(
        "selected_option_id"
      )

    if is_ready(checkout_data):
      return checkout_data

    checkout_obj = f_models.Checkout(**checkout_data)

    # 1. Trigger fulfillment with a default address if none exists
    has_destinations = (
      checkout_data.get("fulfillment")
      and checkout_data["fulfillment"].get("methods")
      and checkout_data["fulfillment"]["methods"][0].get("destinations")
    )

    if not has_destinations:
      # Inject a default US address
      address = {
        "id": "dest_default",
        "street_address": "123 Default St",
        "address_locality": "City",
        "address_region": "State",
        "postal_code": "12345",
        "address_country": "US",
      }
      # Preserve method ID if exists
      method_id = None
      if checkout_data.get("fulfillment") and checkout_data["fulfillment"].get(
        "methods"
      ):
        method_id = checkout_data["fulfillment"]["methods"][0].get("id")

      method_payload = {
        "type": "shipping",
        "destinations": [address],
        "selected_destination_id": "dest_default",
      }
      if method_id:
        method_payload["id"] = method_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
      )
      checkout_obj = f_models.Checkout(**checkout_data)

    # 2. Select Destination (if not already selected)
    method = checkout_data["fulfillment"]["methods"][0]
    if not method.get("selected_destination_id") and method.get("destinations"):
      dest_id = method["destinations"][0]["id"]

      # Construct update preserving destinations
      method_payload = method.copy()
      # method is a dict from json response
      # We need to ensure we send back valid update data
      # Response might have fields not valid for update?
      # Usually safe to send back what we got + changes for this simple server
      method_payload["selected_destination_id"] = dest_id
      # Ensure we keep destinations

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
      )
      checkout_obj = f_models.Checkout(**checkout_data)

    # 3. Select Option
    method = checkout_data["fulfillment"]["methods"][0]
    has_selection = False
    if method.get("groups"):
      for g in method["groups"]:
        if g.get("selected_option_id"):
          has_selection = True
          break

    if not has_selection and (
      method.get("groups") and method["groups"][0].get("options")
    ):
      option_id = method["groups"][0]["options"][0]["id"]

      # Update group
      method_payload = method.copy()
      # Ensure groups is a list of dicts
      method_payload["groups"][0]["selected_option_id"] = option_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
      )

    return checkout_data

  def complete_checkout_session(
    self, checkout_id: str, payment_payload: dict[str, Any] | None = None
  ) -> Any:
    """Complete a checkout session.

    Args:
        checkout_id: The ID of the checkout to complete.
        payment_payload: Optional custom payment payload. If None, uses a valid
          default.

    Returns:
        The JSON response dictionary from the complete request.

    """
    # Ensure fulfillment is set (required by server)
    self.ensure_fulfillment_ready(checkout_id)

    if payment_payload is None:
      payment_payload = get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=self.get_headers(),
    )
    self.assert_response_status(response, 200)
    return response.json()

  def create_completed_order(self) -> str:
    """Orchestrate checkout creation and completion.

    This helper combines create_checkout_session and complete_checkout_session
    to quickly reach a "completed order" state for testing post-order
    operations.

    Returns:
        The 'order_id' from the completion response.

    """
    checkout_data = self.create_checkout_session()
    checkout_id = checkout_data["id"]
    complete_data = self.complete_checkout_session(checkout_id)
    return complete_data["order"]["id"]

  def update_checkout_session(
    self,
    checkout_obj: Any,
    currency: str | None = None,
    line_items: list[Any] | None = None,
    payment: Any | None = None,
    buyer: Any | None = None,
    fulfillment: Any | None = None,
    discounts: Any | None = None,
    platform: Any | None = None,
    headers: dict[str, str] | None = None,
  ) -> Any:
    """Update a checkout session.

    Constructs a partial update request based on the existing checkout object
    and any provided override fields.

    Args:
      checkout_obj: The current checkout object (from response model).
      currency: Optional currency code.
      line_items: Optional list of line items.
      payment: Optional payment object.
      buyer: Optional buyer object.
      fulfillment: Optional fulfillment object (nested structure).
      discounts: Optional discounts.
      platform: Optional platform config.
      headers: Optional headers to include in the request.

    Returns:
        The JSON response dictionary from the update request.

    """
    # Default to existing values if not provided
    if currency is None:
      currency = checkout_obj.currency

    def to_json(obj):
      if obj is not None and hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
      return obj

    if buyer is None:
      buyer = to_json(getattr(checkout_obj, "buyer", None))

    if fulfillment is None:
      fulfillment = to_json(getattr(checkout_obj, "fulfillment", None))

    if discounts is None:
      discounts = to_json(getattr(checkout_obj, "discounts", None))

    if platform is None:
      platform = to_json(getattr(checkout_obj, "platform", None))

    # Construct Line Items
    if line_items is None:
      line_items = []
      for li in checkout_obj.line_items:
        item_update = item_update_request.ItemUpdateRequest(
          id=li.item.id,
        )
        line_items.append(
          line_item_update_request.LineItemUpdateRequest(
            id=li.id,
            item=item_update,
            quantity=li.quantity,
            parent_id=li.parent_id,
          )
        )

    # Construct Payment
    if payment is None:
      payment = (
        payment_update_request.PaymentUpdateRequest(
          instruments=getattr(checkout_obj.payment, "instruments", []),
        )
        if checkout_obj.payment
        else None
      )

    if buyer is None and checkout_obj.buyer is not None:
      buyer = checkout_obj.buyer.model_dump()

    update_payload = UnifiedUpdate(
      id=checkout_obj.id,
      currency=currency,
      line_items=line_items,
      payment=payment,
      buyer=buyer,
      fulfillment=fulfillment,
      discounts=discounts,
      platform=platform,
    )

    request_headers = self.get_headers()
    if headers:
      request_headers.update(headers)

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_obj.id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=request_headers,
    )
    self.assert_response_status(response, 200)
    return response.json()
