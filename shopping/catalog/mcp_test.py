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
"""Tests for the catalog tools of the MCP binding."""

from typing import Any
import uuid

from absl.testing import absltest
from framework.decorators import spec_assert
from framework.decorators import no_rule
from framework.decorators import partly
from framework.decorators import requires_capability
from shopping.mcp import INVALID_PARAMS
from shopping.mcp import McpIntegrationTestBase

SEARCH = "dev.ucp.shopping.catalog.search"
LOOKUP = "dev.ucp.shopping.catalog.lookup"


class McpCatalogTest(McpIntegrationTestBase):
  """Tests for the catalog tools served over the MCP transport.

  Each test runs when the server advertises the capability it exercises.
  The search tests read `catalog.search_query` from the conformance input,
  a query that returns at least one product.

  Validated Tools:
  - search_catalog
  - lookup_catalog
  - get_product
  """

  def setUp(self) -> None:
    """Read the catalog settings of the conformance input."""
    super().setUp()
    self.catalog_config = self.conformance_config.get("catalog") or {}

  def catalog_call(self, name: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """Call a catalog tool and return the UCP payload it answered with."""
    return self.tool_payload(
      self.call_tool(
        name,
        meta=self.request_meta(include_idempotency_key=False),
        catalog=catalog,
      )
    )

  def search(self, **fields: Any) -> dict[str, Any]:
    """Search with the configured query, or skip without one."""
    query = self.catalog_config.get("search_query")
    if not query:
      self.skipTest("Set catalog.search_query in the conformance input")
    return self.catalog_call("search_catalog", {"query": query, **fields})

  def known_product_id(self) -> str:
    """Return the id of a product the server sells."""
    return self.product_sku()

  def products_of(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return `products`, failing unless it is a list of objects."""
    products = payload.get("products")
    self.assertIsInstance(products, list, msg=f"`products`: {payload}")
    for product in products:
      self.assertIsInstance(product, dict, msg="A product is not an object")
    return products

  def assert_envelope(self, payload: dict[str, Any]) -> None:
    """Assert the payload carries the UCP metadata."""
    ucp = payload.get("ucp")
    self.assertIsInstance(ucp, dict, msg="The payload has no `ucp` object")
    self.assertIsInstance(ucp.get("version"), str, msg="`ucp.version`")

  @requires_capability(SEARCH)
  @spec_assert(
    partly(
      "REQ-SHOPPING-CATALOG-0039",  # a conforming MCP implementation MUST
      "item 2 only (tools for each advertised catalog capability), for "
      "search: search_catalog is listed by tools/list",
    ),
  )
  def test_lists_search_tool(self) -> None:
    """Test that the search tool is provided when search is advertised.

    Given a server that advertises catalog search,
    When tools/list is called,
    Then search_catalog is listed.
    """
    self.assert_tools_listed(("search_catalog",))

  @requires_capability(LOOKUP)
  @spec_assert(
    # with Lookup advertised, lookup_catalog and get_product MUST be available
    "REQ-SHOPPING-CATALOG-0040",
  )
  def test_lists_lookup_tools(self) -> None:
    """Test that both lookup tools are provided when lookup is advertised.

    Given a server that advertises catalog lookup,
    When tools/list is called,
    Then lookup_catalog and get_product are listed.
    """
    self.assert_tools_listed(("lookup_catalog", "get_product"))

  @requires_capability(SEARCH)
  @spec_assert(
    partly(
      "REQ-SHOPPING-CATALOG-0039",  # a conforming MCP implementation MUST
      "item 6 only (products with valid Price objects), for the results of "
      "one search: an integer amount and a three-letter currency",
    ),
  )
  def test_search_returns_products(self) -> None:
    """Test that search_catalog returns products in the UCP envelope.

    Given a query that matches at least one product,
    When search_catalog is called,
    Then the result carries the products, each with a variant that has a
    price in minor units and an ISO 4217 currency.
    """
    payload = self.search()

    self.assert_envelope(payload)
    products = self.products_of(payload)
    self.assertTrue(products, msg="catalog.search_query returned no products")
    for product in products:
      variants = product.get("variants")
      self.assertTrue(variants, msg=f"{product.get('id')}: no variants")
      for variant in variants:
        price = variant.get("price", {})
        self.assertIsInstance(price.get("amount"), int, msg=str(variant))
        # bool is a subclass of int, and JSON `true` is not an amount.
        self.assertNotIsInstance(price.get("amount"), bool, msg=str(variant))
        self.assertRegex(str(price.get("currency")), r"^[A-Z]{3}$")

  @requires_capability(SEARCH)
  @spec_assert(
    partly(
      "REQ-SHOPPING-CATALOG-0039",  # a conforming MCP implementation MUST
      "item 7 only (cursor-based pagination for search): one page of one "
      "product and the page its cursor leads to",
    ),
  )
  def test_search_cursor_returns_next_page(self) -> None:
    """Test that a page with more results names a cursor that works.

    Given a search limited to one product per page,
    When the first page reports a next page,
    Then it carries a cursor, and searching with it returns other products.
    """
    first = self.search(pagination={"limit": 1})
    pagination = first.get("pagination") or {}
    if not pagination.get("has_next_page"):
      self.skipTest("The first page reports no next page")
    self.assertTrue(
      pagination.get("cursor"),
      msg="`cursor` is required when has_next_page is true",
    )

    second = self.search(
      pagination={"limit": 1, "cursor": pagination["cursor"]}
    )

    first_ids = {p.get("id") for p in self.products_of(first)}
    second_ids = {p.get("id") for p in self.products_of(second)}
    self.assertTrue(second_ids, msg="The second page is empty")
    self.assertFalse(first_ids & second_ids, msg="The second page repeats")

  @requires_capability(LOOKUP)
  @spec_assert(
    "REQ-SHOPPING-CATALOG-LOOKUP-0004",  # duplicate ids MUST be deduplicated
    # variants without an `inputs` entry MUST NOT appear
    "REQ-SHOPPING-CATALOG-LOOKUP-0007",
  )
  def test_lookup_returns_each_product_once(self) -> None:
    """Test that lookup_catalog deduplicates and correlates identifiers.

    Given a known product id sent twice and an id nothing has,
    When lookup_catalog is called,
    Then the product is returned once, every variant names the identifier
    it resolved from, and nothing resolves from the unknown id.
    """
    product_id = self.known_product_id()
    unknown = f"no_such_product_{uuid.uuid4()}"

    payload = self.catalog_call(
      "lookup_catalog", {"ids": [product_id, product_id, unknown]}
    )

    self.assert_envelope(payload)
    products = self.products_of(payload)
    returned = [p.get("id") for p in products]
    self.assertLen(set(returned), len(returned), msg=f"Repeated: {returned}")
    resolved_from = set()
    for product in products:
      for variant in product.get("variants", []):
        inputs = variant.get("inputs")
        self.assertTrue(
          inputs, msg=f"Variant {variant.get('id')} has no `inputs` entry"
        )
        resolved_from.update(entry.get("id") for entry in inputs)
    self.assertEqual(resolved_from, {product_id})

  @requires_capability(LOOKUP)
  @spec_assert(
    partly(
      "REQ-SHOPPING-CATALOG-0039",  # a conforming MCP implementation MUST
      "item 4 only (a successful result for lookups; unknown identifiers "
      "result in fewer products), for one unknown id",
    ),
  )
  def test_lookup_unknown_id_is_a_result(self) -> None:
    """Test that an identifier that resolves to nothing is not an error.

    Given an id nothing has,
    When lookup_catalog is called with it,
    Then the response is a JSON-RPC result with no products.
    """
    payload = self.catalog_call(
      "lookup_catalog", {"ids": [f"no_such_product_{uuid.uuid4()}"]}
    )

    self.assert_envelope(payload)
    self.assertEqual(self.products_of(payload), [])

  @requires_capability(LOOKUP)
  @spec_assert(
    partly(
      # MUST reject requests over the limit (-32602 for MCP)
      "REQ-SHOPPING-CATALOG-LOOKUP-0010",
      "the MCP error only; REST's HTTP 400 request_too_large is not "
      "checked here",
    ),
  )
  def test_lookup_over_batch_limit_is_invalid_params(self) -> None:
    """Test that a batch over the server's limit is rejected.

    Given the server's limit from `catalog.max_lookup_ids`,
    When lookup_catalog is called with one identifier more,
    Then the server answers with the JSON-RPC invalid params error.
    """
    limit = self.catalog_config.get("max_lookup_ids")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
      self.skipTest("Set catalog.max_lookup_ids in the conformance input")
    ids = [f"no_such_product_{n}_{uuid.uuid4()}" for n in range(limit + 1)]

    body = self.call_tool(
      "lookup_catalog",
      meta=self.request_meta(include_idempotency_key=False),
      catalog={"ids": ids},
    )

    self.assertNotIn("result", body, msg="The batch was not rejected")
    self.assertEqual(body.get("error", {}).get("code"), INVALID_PARAMS)

  @requires_capability(LOOKUP)
  @no_rule(
    "catalog/mcp.md, get_product: the tool returns a singular product "
    "object; stated without a keyword"
  )
  def test_get_product_returns_product(self) -> None:
    """Test that get_product returns a single product.

    Given a known product id,
    When get_product is called,
    Then the result carries one `product` object with at least one variant.
    """
    product_id = self.known_product_id()

    payload = self.catalog_call("get_product", {"id": product_id})

    self.assert_envelope(payload)
    product = payload.get("product")
    self.assertIsInstance(product, dict, msg=f"`product`: {payload}")
    self.assertTrue(product.get("variants"), msg="`product.variants` is empty")

  @requires_capability(LOOKUP)
  @spec_assert(
    partly(
      "REQ-SHOPPING-CATALOG-0039",  # a conforming MCP implementation MUST
      "item 3 only (JSON-RPC errors for transport issues, messages for "
      "business outcomes), for one outcome: get_product on an unknown id",
    ),
  )
  def test_get_product_not_found_is_a_result(self) -> None:
    """Test that a product that does not exist is not a JSON-RPC error.

    Given an id nothing has,
    When get_product is called with it,
    Then the response is a JSON-RPC result whose envelope has status error
    and an error message, and no product.
    """
    payload = self.catalog_call(
      "get_product", {"id": f"no_such_product_{uuid.uuid4()}"}
    )

    self.assertEqual(payload.get("ucp", {}).get("status"), "error")
    errors = [
      m for m in payload.get("messages", []) if m.get("type") == "error"
    ]
    self.assertTrue(errors, msg=f"Expected an error message: {payload}")
    self.assertNotIn("product", payload)

  @requires_capability(LOOKUP)
  @no_rule(
    'catalog/mcp.md, Request Metadata: meta["ucp-agent"] is required '
    "on all requests, in bold but without an RFC 2119 keyword; the "
    "invalid_profile_url discovery error (HTTP 400, -32001) is in the "
    "overview page's error registry"
  )
  def test_ucp_agent_is_required(self) -> None:
    """Test that a call without meta["ucp-agent"] fails discovery.

    Given a get_product call whose meta names no platform profile,
    When it is sent,
    Then the server answers with the discovery error, as REST does for a
    missing profile URL, or rejects the arguments as invalid.
    """
    body = self.call_tool(
      "get_product", meta={}, catalog={"id": self.known_product_id()}
    )

    self.assert_missing_agent_rejected(body)


if __name__ == "__main__":
  absltest.main()
