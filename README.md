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

# UCP SDK Integration Tests

This directory contains integration tests that run against a running UCP
Merchant Server instance. These tests are language-agnostic regarding the server
implementation (Python, Node.js, etc.) and verify adherence to the UCP
specification.

## Prerequisites

The tests assume a UCP Merchant Server is running and accessible via HTTP. The
server must be started with databases initialized using data from
`test_data/flower_shop` directory. Instructions to start the servers follow.

NOTE: These instructions assume the commands are executed from the directory
containing this README.

### Updating dependencies

```bash
uv sync

uv sync --directory ../samples/rest/python/server/

uv sync --directory ../sdk/python/
```

### Initializing the database

```bash
DATABASE_PATH=/tmp/ucp_test

rm -rf ${DATABASE_PATH}
mkdir ${DATABASE_PATH}

uv run --directory ../samples/rest/python/server import_csv.py \
    --products_db_path=${DATABASE_PATH}/products.db \
    --transactions_db_path=${DATABASE_PATH}/transactions.db \
    --data_dir=../../../../conformance/test_data/flower_shop
```

Starting the server:

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

## Running the Tests

```bash
for test_file in *_test.py; do
uv run ${test_file} \
    --server_url=http://localhost:${MERCHANT_SERVER_PORT} \
    --simulation_secret=${SIMULATION_SECRET} \
    --conformance_input=test_data/flower_shop/conformance_input.json \
    --fixture_config=test_data/flower_shop/test_fixtures.json
done

# Or, if you prefer:
SERVER_URL=http://localhost:${MERCHANT_SERVER_PORT} SIMULATION_SECRET=${SIMULATION_SECRET} uv run pytest
```

### Customizing Test Fixtures

You can customize the test fixtures (SKU, expected pricing, discount codes, shipping destinations) by editing `test_fixtures.json` or passing a custom configuration file using the `--fixture_config` flag.

## Cleaning Up

Terminate the server using:

```bash
kill ${MERCHANT_SERVER_PID}
```

## Examining the database state

After running tests, one can examine the database state using the
`dump_transactions` and `dump_log` tools:

```bash
uv run --directory ../samples/rest/python/server dump_transactions.py \
    --transactions_db_path=${DATABASE_PATH}/transactions.db
```

```bash
uv run --directory ../samples/rest/python/server dump_log.py \
    --transactions_db_path=${DATABASE_PATH}/transactions.db
```
