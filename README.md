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

This repository contains the official Universal Commerce Protocol (UCP)
conformance test suite. The suite validates merchant server implementations
against the UCP specification across core protocol requirements, common extensions
(payments, webhooks), and vertical-specific domains (such as retail shopping).

## Architecture

The test suite follows a 4-tier modular architecture per UCP RFC #520:

- **`framework/`**: Shared testing harness decoupled from any business vertical.
  Provides capability discovery, platform profile evaluation, mock webhook and
  agent servers, base test cases, and capability decorators.
- **`core/`**: Agnostic protocol tests (`protocol_test`, `binding_test`,
  `security_test`, generic `idempotency_test`). Can be run against any UCP server
  regardless of business vertical.
- **`common/`**: Cross-cutting protocol capabilities such as webhooks
  (`common/webhooks/`) and payment handlers (`common/payments/`).
- **`shopping/`**: Retail shopping domain tests organized by functional domain:
  `checkout/`, `discount/`, `fulfillment/`, `order/`, and `validation/`.
- **`platforms/`**: Platform certification profiles (e.g. `google.yaml`) that
  mandate required capabilities and payment handlers.

## Prerequisites

The tests assume a UCP Merchant Server is running and accessible via HTTP.
For testing the shopping vertical, the server must be started with databases
initialized using data from `shopping/fixtures/flower_shop` (or `test_data/flower_shop`).

### Updating dependencies

```bash
uv sync

uv sync --directory ../samples/rest/python/server/

uv sync --directory ../python-sdk/
```

### Initializing the test database

```bash
DATABASE_PATH=/tmp/ucp_test

rm -rf ${DATABASE_PATH}
mkdir -p ${DATABASE_PATH}

uv run --directory ../samples/rest/python/server import_csv.py \
    --products_db_path=${DATABASE_PATH}/products.db \
    --transactions_db_path=${DATABASE_PATH}/transactions.db \
    --data_dir=../../../../conformance/shopping/fixtures/flower_shop
```

### Starting the server

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

## Running the Conformance Tests

Use the `ucp-conformance` CLI orchestrator (or `uv run runner.py`):

```bash
# Run all discovered tests
uv run ucp-conformance \
    --server_url=http://localhost:${MERCHANT_SERVER_PORT} \
    --simulation_secret=${SIMULATION_SECRET}

# Run only agnostic core protocol tests (zero retail dependencies)
uv run ucp-conformance --suite=core --server_url=http://localhost:${MERCHANT_SERVER_PORT}

# Run retail shopping tests
uv run ucp-conformance --suite=shopping --server_url=http://localhost:${MERCHANT_SERVER_PORT}

# Certify server compliance against a platform profile (e.g., Google)
uv run ucp-conformance \
    --platform=google \
    --server_url=http://localhost:${MERCHANT_SERVER_PORT} \
    --simulation_secret=${SIMULATION_SECRET}

# Dry run to preview test execution list
uv run ucp-conformance --suite=all --dry-run
```

### CLI Options

| Option                | Default                 | Description                                                    |
| --------------------- | ----------------------- | -------------------------------------------------------------- |
| `--server_url`        | `http://localhost:8182` | Base URL of the target UCP server.                             |
| `--platform`          | `None`                  | Platform profile to validate compliance against (`google`).    |
| `--suite`             | `all`                   | Test suite(s) to execute: `all`, `core`, `common`, `shopping`. |
| `--simulation_secret` | `""`                    | Secret for simulation endpoints.                               |
| `--conformance_input` | `None`                  | Optional path to custom conformance input JSON.                |
| `-k`, `--filter`      | `None`                  | Filter test cases matching a pattern.                          |
| `-v`, `--verbose`     | `False`                 | Enable verbose execution logs.                                 |
| `--dry-run`           | `False`                 | List matching tests without executing.                         |

### Customizing Test Fixtures

Shopping test fixtures (SKU, expected pricing, discount codes, shipping destinations)
can be customized in `shopping/fixtures/flower_shop/test_fixtures.json` or by passing
a custom configuration file using `--fixture_config`.

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
