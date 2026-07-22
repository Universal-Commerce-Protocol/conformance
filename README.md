<!--
   Copyright 2026 UCP Authors

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
-->

# UCP Conformance Test Suite

This suite contains language-agnostic integration tests that run against a running UCP Merchant Server instance. It verifies the server's adherence to the [Universal Commerce Protocol (UCP) specification](https://github.com/Universal-Commerce-Protocol/ucp).

## Testing a Custom Server (Agnostic)

The conformance tests are designed to be run against _any_ UCP server implementation. To run tests against your own server, follow these steps:

### 1. Seed Your Server with Test Data

Your server must be populated with test data (products, inventory, shipping rates, discounts) that match the expectations defined in your test configuration.

You can use the default [flower_shop test data](test_data/flower_shop) as a reference for what data is needed.

### 2. Configure Conformance Input (`conformance_input.json`)

Create a `conformance_input.json` file to define your merchant-specific values and expectations. The tests use this file to know what to assert against.

Example `conformance_input.json`:

```json
{
  "ucp_version": "2026-04-08",
  "required_capabilities": [
    "dev.ucp.shopping.checkout",
    "dev.ucp.shopping.order"
  ],
  "currency": "USD",
  "items": [
    {
      "id": "item_1",
      "title": "Valid Item",
      "price": 1000
    }
  ],
  "out_of_stock_item": {
    "id": "item_out_of_stock",
    "title": "Out of Stock Item"
  },
  "non_existent_item": {
    "id": "non_existent",
    "title": "Non-existent Item"
  }
}
```

- `ucp_version`: The spec version your server targets.
- `required_capabilities`: The capability names your server is expected to declare in discovery.
- `items`: List of items available on your server. The first item in this list will be used for standard checkout tests.
- `out_of_stock_item`: An item ID that should trigger an out-of-stock error (status `409`) when trying to complete checkout.
- `non_existent_item`: An item ID that does not exist in your catalog, used to test invalid input handling.

### 3. Configure Test Fixtures (`test_fixtures.json`)

If your server's test data differs from the default, create a `test_fixtures.json` to map the exact values for calculations and assertions.

Example `test_fixtures.json`:

```json
{
  "test_fixtures": {
    "valid_item": {
      "sku": "item_1",
      "expected_price": 10.0,
      "quantity": 1
    },
    "valid_discount_code": "PROMO10",
    "expected_discount_reduction": 1.0,
    "valid_discount_code_2": "PROMO20",
    "expected_discount_reduction_2": 1.8,
    "valid_fixed_discount_code": "FIXED5",
    "expected_fixed_discount_reduction": 5.0
  },
  "shipping_locations": {
    "domestic_destination": {
      "street": "123 Main St",
      "city": "Anytown",
      "state": "CA",
      "postal_code": "12345",
      "country": "US"
    }
  }
}
```

- `valid_item`: The SKU and expected price (in major units, e.g., `10.00`) of the item used in tests.
- **Discounts Configuration**:
  - `valid_discount_code` & `expected_discount_reduction`: **Required** for basic discount flow tests. The code must be valid on your server, and the reduction is the expected amount in major units.
  - `valid_discount_code_2` & `expected_discount_reduction_2`: **Optional**. Used for testing multiple discount codes applied sequentially. If your server does not support multiple discounts or you don't configure this, the corresponding test will be skipped.
  - `valid_fixed_discount_code` & `expected_fixed_discount_reduction`: **Optional**. Used for testing fixed-amount discounts. If your server only supports percentage discounts or you don't configure this, the test will be skipped.
- `shipping_locations`: Addresses used for fulfillment tests.

### 4. Run the Tests

Install dependencies:

```bash
uv sync
```

Run the tests pointing to your server:

```bash
SERVER_URL=https://your-merchant-server.com \
SIMULATION_SECRET=your-sim-secret \
uv run pytest \
  --conformance_input=path/to/your/conformance_input.json \
  --fixture_config=path/to/your/test_fixtures.json
```

Alternatively, you can run individual test files and pass arguments:

```bash
uv run checkout_lifecycle_test.py \
  --server_url=https://your-merchant-server.com \
  --simulation_secret=your-sim-secret \
  --conformance_input=path/to/your/conformance_input.json \
  --fixture_config=path/to/your/test_fixtures.json
```

---

## Example Walkthrough: Running against the Python Sample Server

For a quick demonstration or local development, you can run the suite against the reference Python sample server included in this repository.

NOTE: These instructions assume the commands are executed from the directory containing this README.

### Prerequisites

Sync dependencies for the test suite and the sample server:

```bash
uv sync
uv sync --directory ../samples/rest/python/server/
```

### 1. Initialize the Sample Database

Populate the SQLite databases with the default "flower shop" test data:

```bash
DATABASE_PATH=/tmp/ucp_test
rm -rf ${DATABASE_PATH} && mkdir -p ${DATABASE_PATH}

uv run --directory ../samples/rest/python/server import_csv.py \
    --products_db_path=${DATABASE_PATH}/products.db \
    --transactions_db_path=${DATABASE_PATH}/transactions.db \
    --data_dir=../../../../conformance/test_data/flower_shop
```

### 2. Start the Sample Server

Start the server in the background:

```bash
SIMULATION_SECRET=super-secret-sim-key
MERCHANT_SERVER_PORT=8182

uv run --directory ../samples/rest/python/server server.py \
    --products_db_path=${DATABASE_PATH}/products.db \
    --transactions_db_path=${DATABASE_PATH}/transactions.db \
    --port=${MERCHANT_SERVER_PORT} \
    --simulation_secret=${SIMULATION_SECRET} &
MERCHANT_SERVER_PID=$!
```

### 3. Run the Tests

Run the tests using the default flower shop configurations:

```bash
SERVER_URL=http://localhost:${MERCHANT_SERVER_PORT} \
SIMULATION_SECRET=${SIMULATION_SECRET} \
uv run pytest
```

### 4. Clean Up

Stop the background server:

```bash
kill ${MERCHANT_SERVER_PID}
```

### Examining Database State (Optional)

After running tests, you can inspect the sample server's database state:

```bash
# Dump transactions table
uv run --directory ../samples/rest/python/server dump_transactions.py \
    --transactions_db_path=${DATABASE_PATH}/transactions.db

# Dump request logs
uv run --directory ../samples/rest/python/server dump_log.py \
    --transactions_db_path=${DATABASE_PATH}/transactions.db
```
