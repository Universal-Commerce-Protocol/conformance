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

"""Base integration test and payload helpers for UCP Shopping vertical."""

import csv
import json
import logging
from pathlib import Path
from typing import Any
import uuid
from absl import flags
from framework.base_test import BaseIntegrationTest
from ucp_sdk.models.schemas import payment_handler
from ucp_sdk.models.schemas.shopping import checkout as f_models
from ucp_sdk.models.schemas.shopping import checkout_create_request
from ucp_sdk.models.schemas.shopping.checkout_update_request import (
  CheckoutUpdateRequest,
)
from ucp_sdk.models.schemas.shopping.types import (
  fulfillment_group_create_request,
  fulfillment_method_create_request,
  item_create_request,
  item_update_request,
  line_item_create_request,
  line_item_update_request,
  shipping_destination,
)

try:
  from ucp_sdk.models.schemas.shopping import (
    payment_create_request,
    payment_update_request,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types import (
    payment_create_request,
    payment_update_request,
  )

try:
  from ucp_sdk.models.schemas.shopping.payment import (
    Payment,
  )
except ImportError:
  from ucp_sdk.models.schemas.common.types.payment import (
    Payment,
  )

# Rebuild models to resolve forward references
f_models.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class UnifiedUpdate(CheckoutUpdateRequest):
  """Client-side unified update model to support extensions."""


FLAGS = flags.FLAGS
DEFAULT_SHOPPING_FIXTURES = str(
  Path(__file__).resolve().parent / "fixtures" / "flower_shop"
)

try:
  flags.DEFINE_string(
    "shopping_fixtures_dir",
    DEFAULT_SHOPPING_FIXTURES,
    "Directory containing retail CSVs and fixtures.",
  )
  flags.DEFINE_string(
    "shopping_conformance_input",
    str(Path(DEFAULT_SHOPPING_FIXTURES) / "conformance_input.json"),
    "Path to retail conformance input JSON.",
  )
  flags.DEFINE_string(
    "fixture_config",
    str(Path(DEFAULT_SHOPPING_FIXTURES) / "test_fixtures.json"),
    "Path to test fixtures configuration JSON.",
  )
  flags.DEFINE_string(
    "conformance_input",
    str(Path(DEFAULT_SHOPPING_FIXTURES) / "conformance_input.json"),
    "Path to conformance input configuration JSON.",
  )
  flags.DEFINE_string(
    "test_data_dir",
    DEFAULT_SHOPPING_FIXTURES,
    "Directory containing test CSV data.",
  )
except flags.DuplicateFlagError:
  pass


class ShoppingTestData:
  """Holder for loaded retail test data."""

  def __init__(self) -> None:
    """Initialize ShoppingTestData container."""
    self.payment_instruments: list[dict[str, Any]] = []
    self.addresses: list[dict[str, Any]] = []

  def load(self, data_dir: str | Path) -> None:
    """Load data from CSV files in the given directory."""
    pi_path = Path(data_dir) / "payment_instruments.csv"
    if pi_path.exists():
      with pi_path.open(encoding="utf-8") as f:
        self.payment_instruments = list(csv.DictReader(f))

    addr_path = Path(data_dir) / "addresses.csv"
    if addr_path.exists():
      with addr_path.open(encoding="utf-8") as f:
        self.addresses = list(csv.DictReader(f))


shopping_test_data = ShoppingTestData()


def get_valid_payment_payload(
  instrument_id: str = "instr_1", address_id: str = "addr_1"
) -> dict[str, Any]:
  """Return a valid payment payload using loaded test data."""
  instr_data = next(
    (
      pi
      for pi in shopping_test_data.payment_instruments
      if pi["id"] == instrument_id
    ),
    None,
  )
  if not instr_data:
    instr_data = {
      "id": "instr_1",
      "type": "card",
      "brand": "Visa",
      "last_digits": "1234",
      "token": "success_token",
      "handler_id": "mock_payment_handler",
    }

  addr_data = next(
    (a for a in shopping_test_data.addresses if a["id"] == address_id),
    None,
  )
  if not addr_data:
    addr_data = {
      "street_address": "123 Main St",
      "city": "Anytown",
      "state": "CA",
      "postal_code": "12345",
      "country": "US",
    }

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


class DynamicFixtureContext:
  """Context for loading dynamic test fixtures from configuration."""

  def __init__(
    self,
    config_path: str | Path | dict[str, Any],
    fallback_config: dict[str, Any] | None = None,
  ):
    """Initialize DynamicFixtureContext with path or dictionary."""
    if isinstance(config_path, (str, Path)):
      try:
        with Path(config_path).open(encoding="utf-8") as f:
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


class ShoppingIntegrationTestBase(BaseIntegrationTest):
  """Base integration test class for all UCP Retail Shopping tests."""

  def setUp(self) -> None:
    """Set up shopping fixtures, configs, and endpoints."""
    super().setUp()

    # Verify server advertises shopping checkout
    if self.server_profile and not self.server_profile.supports_capability(
      "dev.ucp.shopping.checkout"
    ):
      self.skipTest(
        "Server does not support capability: dev.ucp.shopping.checkout"
      )

    # Load conformance input configuration
    self.conformance_config = {}
    config_path = (
      getattr(FLAGS, "shopping_conformance_input", None)
      or getattr(FLAGS, "conformance_input", None)
      or str(Path(DEFAULT_SHOPPING_FIXTURES) / "conformance_input.json")
    )
    if Path(config_path).exists():
      with Path(config_path).open(encoding="utf-8") as f:
        self.conformance_config = json.load(f)

    # Load fixture configuration
    fixture_config = {}
    fix_cfg_path = getattr(FLAGS, "fixture_config", None) or str(
      Path(DEFAULT_SHOPPING_FIXTURES) / "test_fixtures.json"
    )
    if fix_cfg_path and Path(fix_cfg_path).exists():
      with Path(fix_cfg_path).open(encoding="utf-8") as f:
        fixture_config = json.load(f)

    self.fixture_ctx = DynamicFixtureContext(
      fixture_config, fallback_config=self.conformance_config
    )

    # Load CSV test data
    fixtures_dir = (
      getattr(FLAGS, "shopping_fixtures_dir", None)
      or getattr(FLAGS, "test_data_dir", None)
      or DEFAULT_SHOPPING_FIXTURES
    )
    if Path(fixtures_dir).exists():
      try:
        shopping_test_data.load(fixtures_dir)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning("Failed to load shopping CSV fixtures: %s", e)

    self._shopping_service_endpoint: str | None = None

  @property
  def shopping_service_endpoint(self) -> str:
    """Cached property for the shopping service endpoint."""
    if self._shopping_service_endpoint is None:
      discovery_resp = self.client.get("/.well-known/ucp")
      self.assert_response_status(discovery_resp, 200)

      profile_data = discovery_resp.json()
      ucp_data = profile_data.get("ucp", profile_data)
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
    """Construct a full URL for the shopping service."""
    base = self.shopping_service_endpoint.rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"

  def get_order_url(self, order_id: str) -> str:
    """Construct a full URL for an order resource."""
    return self.get_shopping_url(f"/orders/{order_id}")

  def create_checkout_payload(
    self,
    quantity=1,
    item_id: str | None = None,
    currency: str | None = None,
    handlers=None,
    buyer: dict[str, Any] | None = None,
    include_fulfillment: bool = True,
  ) -> checkout_create_request.CheckoutCreateRequest:
    """Create a valid checkout creation payload."""
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

    payment = payment_create_request.PaymentCreateRequest(
      instruments=[],
      handlers=[h.model_dump(mode="json", exclude_none=True) for h in handlers],
    )

    fulfillment = None
    if include_fulfillment:
      dest_data = ctx.get_test_destination()
      destination = shipping_destination.ShippingDestination(
        id="dest_1",
        type="shipping_address",
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
    """Create a checkout session and return the response JSON."""
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
    """Ensure a fulfillment option is selected for the checkout."""
    response = self.client.get(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      headers=self.get_headers(),
    )
    checkout_data = response.json()

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

    has_destinations = (
      checkout_data.get("fulfillment")
      and checkout_data["fulfillment"].get("methods")
      and checkout_data["fulfillment"]["methods"][0].get("destinations")
    )

    if not has_destinations:
      address = {
        "id": "dest_default",
        "street_address": "123 Default St",
        "address_locality": "City",
        "address_region": "State",
        "postal_code": "12345",
        "address_country": "US",
      }
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

    method = checkout_data["fulfillment"]["methods"][0]
    if not method.get("selected_destination_id") and method.get("destinations"):
      dest_id = method["destinations"][0]["id"]
      method_payload = method.copy()
      method_payload["selected_destination_id"] = dest_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
      )
      checkout_obj = f_models.Checkout(**checkout_data)

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
      method_payload = method.copy()
      method_payload["groups"][0]["selected_option_id"] = option_id

      checkout_data = self.update_checkout_session(
        checkout_obj,
        fulfillment={"methods": [method_payload]},
      )

    return checkout_data

  def complete_checkout_session(
    self, checkout_id: str, payment_payload: dict[str, Any] | None = None
  ) -> Any:
    """Complete a checkout session."""
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
    """Orchestrate checkout creation and completion."""
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
    """Update a checkout session."""
    currency = currency if currency is not None else checkout_obj.currency

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

    if payment is None:
      payment = (
        payment_update_request.PaymentUpdateRequest(
          instruments=getattr(checkout_obj.payment, "instruments", []),
        )
        if checkout_obj.payment
        else None
      )

    if isinstance(fulfillment, dict) and "methods" in fulfillment:
      for m in fulfillment["methods"]:
        if isinstance(m, dict) and "destinations" in m and m["destinations"]:
          for d in m["destinations"]:
            if isinstance(d, dict) and "type" not in d:
              d["type"] = "shipping_address"

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
