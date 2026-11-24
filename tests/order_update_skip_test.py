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


"""Unit tests for ShoppingIntegrationTestBase.put_order_or_skip."""

from types import SimpleNamespace
import unittest

from shopping.base import ShoppingIntegrationTestBase


class _FakeClient:
  """Records PUT calls and answers with a fixed status."""

  def __init__(self, status_code: int) -> None:
    self.status_code = status_code
    self.calls = []

  def put(self, url, json=None, headers=None):
    self.calls.append((url, json, headers))
    return SimpleNamespace(status_code=self.status_code)


def _base_with_status(status_code: int) -> ShoppingIntegrationTestBase:
  """Return a base test case whose client answers PUT with `status_code`."""
  case = ShoppingIntegrationTestBase("put_order_or_skip")
  case.client = _FakeClient(status_code)
  case.get_order_url = lambda order_id: (
    f"https://shop.example/orders/{order_id}"
  )
  case.get_headers = lambda: {"UCP-Agent": "test"}
  return case


class PutOrderOrSkipTest(unittest.TestCase):
  """A server without PUT /orders/{id} skips the test rather than failing."""

  def test_skips_on_404_and_405(self) -> None:
    """A spec-following server answers 404 or 405, so the test is skipped."""
    for status in (404, 405):
      with self.subTest(status=status):
        case = _base_with_status(status)
        with self.assertRaises(unittest.SkipTest) as raised:
          case.put_order_or_skip("order_1", {"id": "order_1"})
        self.assertIn(str(status), str(raised.exception))
        self.assertIn("GET /orders/{id}", str(raised.exception))

  def test_returns_response_when_supported(self) -> None:
    """A server that has PUT gets the request and the test sees its reply."""
    for status in (200, 422):
      with self.subTest(status=status):
        case = _base_with_status(status)
        response = case.put_order_or_skip("order_1", {"id": "order_1"})
        self.assertEqual(response.status_code, status)
        self.assertEqual(
          case.client.calls,
          [
            (
              "https://shop.example/orders/order_1",
              {"id": "order_1"},
              {"UCP-Agent": "test"},
            )
          ],
        )


if __name__ == "__main__":
  unittest.main()
