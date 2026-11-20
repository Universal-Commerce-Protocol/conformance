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

"""Catalog tests: search, lookup and get product over the REST binding.

Each test checks a MUST of the catalog specification, an item of the REST
binding's Conformance list, or a constraint of the catalog schemas. The pages
are specification/shopping/catalog/ in the 2026-08-25 release and
specification/catalog/ in 2026-04-08, and the rules tested are the same in
both.

The merchant names a query that returns at least one product under
`catalog.search_query` in its conformance input. Tests that need a product
skip without it, and the lookup tests that find their product by search also
need the search capability. Every test skips on a server that does not
advertise the capability it covers, or that lists a scope for that capability
in identity linking `config.scopes`: these tests send no user token.
"""

import re
import uuid
from absl.testing import absltest
from framework.base_test import BaseIntegrationTest
from framework.decorators import requires_capability
from framework.services import gated_scopes, rest_endpoint
import httpx

SEARCH = "dev.ucp.shopping.catalog.search"
LOOKUP = "dev.ucp.shopping.catalog.lookup"
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def unknown_id() -> str:
  """Return an identifier no catalog is expected to hold."""
  return f"ucp-conformance-unknown-{uuid.uuid4().hex}"


def is_integer(value) -> bool:
  """Return whether a JSON number is an integer, as JSON Schema counts it."""
  if isinstance(value, bool):
    return False
  return isinstance(value, int) or (
    isinstance(value, float) and value.is_integer()
  )


class CatalogTestBase(BaseIntegrationTest):
  """Shared setup: the shopping REST endpoint and the merchant's query."""

  def setUp(self) -> None:
    """Resolve the shopping REST endpoint and skip gated capabilities."""
    super().setUp()
    if self.server_profile is None:
      self.skipTest("Could not fetch the discovery profile")
    endpoint = rest_endpoint(self.server_profile, "dev.ucp.shopping")
    if endpoint is None:
      self.skipTest("The profile lists no REST endpoint for dev.ucp.shopping")
    self.endpoint = endpoint.rstrip("/")
    self.catalog_config = self.conformance_config.get("catalog") or {}

    # Identity linking, Scopes: a scope listed in `config.scopes` gates its
    # capability's operations, and scope tokens are `{capability}:{scope}`.
    required = set(getattr(self, "_required_capabilities", set()))
    method = getattr(self, self._testMethodName, None)
    required |= set(getattr(method, "_required_capabilities", set()))
    gated = gated_scopes(self.server_profile)
    for name in sorted({name for name, _ in required}):
      if any(scope.startswith(f"{name}:") for scope in gated):
        self.skipTest(f"{name} is gated by a scope in config.scopes")

  def post(self, path: str, body: dict) -> httpx.Response:
    """POST a catalog request with the standard UCP headers."""
    return self.client.post(
      self.endpoint + path, json=body, headers=self.get_headers()
    )

  def json_body(self, response: httpx.Response) -> dict:
    """Return the response body, failing unless it is a JSON object."""
    try:
      body = response.json()
    except ValueError:
      self.fail(f"Response is not JSON: {response.text[:200]}")
    self.assertIsInstance(body, dict, msg="Response body is not an object")
    return body

  def products_of(self, body: dict) -> list[dict]:
    """Return `products`, failing unless it is an array of objects."""
    products = body.get("products")
    self.assertIsInstance(products, list, msg="`products` is not an array")
    for product in products:
      self.assertIsInstance(product, dict, msg="A product is not an object")
    return products

  def variants_of(self, product: dict) -> list[dict]:
    """Return a product's `variants` that are objects."""
    variants = product.get("variants")
    if not isinstance(variants, list):
      return []
    return [v for v in variants if isinstance(v, dict)]

  def search(self, **fields) -> dict:
    """Run the merchant's search query and return the 200 response body."""
    query = self.catalog_config.get("search_query")
    if not query:
      self.skipTest("Set catalog.search_query in the conformance input")
    response = self.post("/catalog/search", {"query": query, **fields})
    self.assert_response_status(response, 200)
    return self.json_body(response)

  def known_product(self) -> dict:
    """Return the first product the merchant's query finds."""
    products = self.products_of(self.search(pagination={"limit": 1}))
    if not products:
      self.skipTest("catalog.search_query returned no products")
    product = products[0]
    product_id = product.get("id")
    self.assertTrue(
      isinstance(product_id, str) and product_id,
      msg=f"Search returned a product without a string `id`: {product_id!r}",
    )
    return product

  def assert_ucp_envelope(self, body: dict, capability: str) -> None:
    """Check the `ucp` field of a successful response.

    Overview, Business Requirements 4: businesses MUST include the `ucp`
    field in every response, with `version` and the capabilities active
    for the response.
    """
    ucp = body.get("ucp")
    self.assertIsInstance(ucp, dict, msg="Response has no `ucp` object")
    self.assertIsInstance(ucp.get("version"), str, msg="`ucp.version`")
    capabilities = ucp.get("capabilities")
    self.assertIsInstance(
      capabilities, dict, msg="`ucp.capabilities` is missing"
    )
    self.assertIn(
      capability,
      capabilities,
      msg=f"`ucp.capabilities` does not list {capability}",
    )


@requires_capability(SEARCH)
class CatalogSearchTest(CatalogTestBase):
  """POST /catalog/search."""

  def test_search_returns_products_in_ucp_envelope(self) -> None:
    """A search answers 200 with `ucp` and a `products` array.

    REST Conformance 1: implement an endpoint for each advertised catalog
    capability. The search response schema requires `ucp` and `products`.
    """
    body = self.search()
    self.assert_ucp_envelope(body, SEARCH)
    self.products_of(body)

  def test_search_cursor_returns_next_page(self) -> None:
    """A page with more results names a cursor, and the cursor is accepted.

    REST Conformance 3: support cursor-based pagination. Pagination schema:
    `cursor` MUST be present when `has_next_page` is true.
    """
    first = self.search(pagination={"limit": 1})
    pagination = first.get("pagination")
    if not isinstance(pagination, dict) or not pagination.get("has_next_page"):
      self.skipTest("The first page reports no next page")
    cursor = pagination.get("cursor")
    self.assertIsInstance(
      cursor, str, msg="`cursor` MUST be present when `has_next_page` is true"
    )
    self.assertTrue(cursor, msg="`cursor` is empty")
    second = self.search(pagination={"limit": 1, "cursor": cursor})
    self.assert_ucp_envelope(second, SEARCH)
    self.products_of(second)

  def test_search_variants_carry_valid_prices(self) -> None:
    """Every returned product has variants, and each has a valid Price.

    REST Conformance 2: return products with valid Price objects (amount +
    currency). Schemas: a product requires at least one variant, a variant
    requires `price`, and a Price requires an integer `amount` of at least 0
    and a `currency` matching ^[A-Z]{3}$.
    """
    products = self.products_of(self.search())
    if not products:
      self.skipTest("catalog.search_query returned no products")
    invalid = []
    for product in products:
      variants = self.variants_of(product)
      if not variants:
        invalid.append(f"{product.get('id')}: no variants")
      for variant in variants:
        price = variant.get("price")
        amount = price.get("amount") if isinstance(price, dict) else None
        currency = price.get("currency") if isinstance(price, dict) else None
        if not (
          is_integer(amount)
          and amount >= 0
          and isinstance(currency, str)
          and CURRENCY_CODE.match(currency)
        ):
          invalid.append(f"{variant.get('id')}: {price!r}")
    # One failure for the whole page, so the summary counts this rule once.
    self.assertFalse(
      invalid, msg="Variants without a valid `price`:\n" + "\n".join(invalid)
    )


@requires_capability(LOOKUP)
class CatalogLookupTest(CatalogTestBase):
  """POST /catalog/lookup and POST /catalog/product."""

  def test_lookup_unknown_id_returns_no_product(self) -> None:
    """An identifier that resolves to nothing still answers 200.

    REST Conformance 4: return HTTP 200 for lookup requests; unknown
    identifiers result in fewer products returned. Lookup: variants without
    an `inputs` entry MUST NOT appear, so nothing can come back here.
    """
    response = self.post("/catalog/lookup", {"ids": [unknown_id()]})
    self.assert_response_status(response, 200)
    body = self.json_body(response)
    self.assert_ucp_envelope(body, LOOKUP)
    self.assertEqual(self.products_of(body), [], msg="`products`")

  @requires_capability(SEARCH)
  def test_lookup_dedupes_ids_and_correlates_inputs(self) -> None:
    """Each product comes back once, and each variant names its inputs.

    Lookup: implementations MUST support lookup by product ID and variant
    ID. Duplicate identifiers MUST be deduplicated, and a product that
    several identifiers resolve to MUST be returned once. Variants without
    an `inputs` entry MUST NOT appear. So the requested variant ID must
    resolve to the variant with that ID, and nothing may resolve the
    unknown ID.
    """
    product = self.known_product()
    product_id = product["id"]
    variant_ids = [v.get("id") for v in self.variants_of(product)]
    variant_id = next(
      (v for v in variant_ids if isinstance(v, str) and v != product_id), None
    )
    missing = unknown_id()
    ids = [product_id, product_id, missing]
    if variant_id:
      ids.insert(2, variant_id)

    response = self.post("/catalog/lookup", {"ids": ids})
    self.assert_response_status(response, 200)
    body = self.json_body(response)
    self.assert_ucp_envelope(body, LOOKUP)
    products = self.products_of(body)
    returned = [p.get("id") for p in products]
    self.assertEqual(
      returned.count(product_id),
      1,
      msg=f"Product {product_id} MUST be returned exactly once: {returned}",
    )

    resolved_on: dict[str, set] = {}
    problems = []
    for item in products:
      for variant in self.variants_of(item):
        inputs = variant.get("inputs")
        if not isinstance(inputs, list) or not inputs:
          problems.append(f"{variant.get('id')}: no `inputs` entry")
          continue
        entry_ids = [
          e.get("id") if isinstance(e, dict) else None for e in inputs
        ]
        if not all(isinstance(e, str) for e in entry_ids):
          problems.append(f"{variant.get('id')}: `inputs` ids {entry_ids!r}")
          continue
        if len(entry_ids) != len(set(entry_ids)):
          problems.append(f"{variant.get('id')}: repeated `inputs` {entry_ids}")
        for entry_id in entry_ids:
          if entry_id not in ids:
            problems.append(f"{variant.get('id')}: input {entry_id!r} not sent")
          resolved_on.setdefault(entry_id, set()).add(variant.get("id"))
    self.assertFalse(problems, msg="\n".join(problems))
    self.assertNotIn(missing, resolved_on, msg="The unknown id resolved")
    self.assertIn(product_id, resolved_on, msg="The product id did not resolve")
    if variant_id:
      self.assertEqual(
        resolved_on.get(variant_id),
        {variant_id},
        msg="The variant id did not resolve to that variant",
      )

  def test_lookup_over_batch_limit_returns_request_too_large(self) -> None:
    """A batch over the server's limit answers 400 `request_too_large`.

    REST Conformance 5. The merchant states its limit under
    `catalog.max_lookup_ids`; the test skips without it. The specification
    does not define the 400 body, so the code is accepted at the top level
    or in `messages`.
    """
    limit = self.catalog_config.get("max_lookup_ids")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
      self.skipTest("Set catalog.max_lookup_ids in the conformance input")
    ids = [unknown_id() for _ in range(limit + 1)]
    response = self.post("/catalog/lookup", {"ids": ids})
    self.assert_response_status(response, 400)
    body = self.json_body(response)
    messages = body.get("messages")
    codes = [body.get("code")] + [
      m.get("code") for m in messages or [] if isinstance(m, dict)
    ]
    self.assertIn("request_too_large", codes, msg="No `request_too_large`")

  @requires_capability(SEARCH)
  def test_get_product_endpoint_is_available(self) -> None:
    """Get product answers 200 with a `product` for a known product ID.

    REST Conformance 1: when Lookup is advertised, both /catalog/lookup and
    /catalog/product MUST be available. The get product response schema
    requires `ucp` and `product`.
    """
    product_id = self.known_product()["id"]
    response = self.post("/catalog/product", {"id": product_id})
    self.assert_response_status(response, 200)
    body = self.json_body(response)
    self.assert_ucp_envelope(body, LOOKUP)
    self.assertIsInstance(body.get("product"), dict, msg="`product`")

  @requires_capability(SEARCH)
  def test_get_product_by_variant_id_features_that_variant(self) -> None:
    """Get product by a variant ID returns that variant first.

    Lookup, Get Product: for a variant ID, the requested variant MUST be
    the first element.
    """
    product = self.known_product()
    variant_ids = [v.get("id") for v in self.variants_of(product)]
    variant_id = next(
      (
        v
        for v in reversed(variant_ids)
        if isinstance(v, str) and v != product["id"]
      ),
      None,
    )
    if not variant_id:
      self.skipTest("The product found has no variant id of its own")
    response = self.post("/catalog/product", {"id": variant_id})
    self.assert_response_status(response, 200)
    body = self.json_body(response)
    self.assert_ucp_envelope(body, LOOKUP)
    found = body.get("product")
    self.assertIsInstance(found, dict, msg="`product`")
    returned = self.variants_of(found)
    self.assertTrue(returned, msg="`product.variants` is empty")
    self.assertEqual(
      returned[0].get("id"), variant_id, msg="First variant is not the one"
    )


if __name__ == "__main__":
  absltest.main()
