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

"""Unit tests for the mock webhook receiver and the waits that read it.

A business that delivers webhooks asynchronously and retries may still be
delivering an earlier test's order when a webhook test starts. These tests
send such stray deliveries first. The receiver's app is driven in-process
through httpx's ASGI transport, so no port is opened.
"""

import asyncio
import threading
import types
import unittest

from framework.mock_webhook_server import MockWebhookServer
import httpx
from shopping.order import webhook_test

PATH = "/webhooks/partners/test_partner/events/order"


def deliver(
  server: MockWebhookServer,
  body: object = None,
  *,
  content: bytes | None = None,
  headers: dict[str, str] | None = None,
  path: str = PATH,
) -> int:
  """POST one delivery to the receiver; return the status it answered."""

  async def post() -> int:
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
      transport=transport, base_url="http://hooks.test"
    ) as client:
      if content is not None:
        response = await client.post(path, content=content, headers=headers)
      else:
        response = await client.post(path, json=body, headers=headers)
      return response.status_code

  return asyncio.run(post())


class RefusalCountTest(unittest.TestCase):
  """`failures_before_success` is counted for each order on its own."""

  def test_nothing_is_refused_by_default(self):
    """A receiver that was asked for no failures accepts every delivery."""
    server = MockWebhookServer(port=0)

    self.assertEqual(deliver(server, {"id": "ord_1"}), 200)
    self.assertEqual(deliver(server, {"id": "ord_1"}), 200)
    self.assertEqual([e["response_status"] for e in server.events], [200, 200])

  def test_each_order_is_refused_as_often_as_asked(self):
    """The first attempts for an order get a 500, the later ones a 200."""
    server = MockWebhookServer(port=0, failures_before_success=2)

    answers = [deliver(server, {"id": "ord_1"}) for _ in range(4)]

    self.assertEqual(answers, [500, 500, 200, 200])

  def test_a_stray_delivery_leaves_the_tests_refusal_alone(self):
    """Another order's retry gets its own refusal, not this order's.

    Counted over all deliveries, the stray took the one 500, the order
    under test was accepted at once, and its sender had nothing to retry.
    """
    server = MockWebhookServer(port=0, failures_before_success=1)

    stray = deliver(server, {"id": "order_from_an_earlier_test"})
    first = deliver(server, {"id": "order_under_test"})
    retry = deliver(server, {"id": "order_under_test"})

    self.assertEqual((stray, first, retry), (500, 500, 200))
    under_test = [
      e["response_status"]
      for e in server.events
      if e["payload"]["id"] == "order_under_test"
    ]
    self.assertEqual(under_test, [500, 200])

  def test_a_retry_with_a_new_webhook_id_is_the_same_order(self):
    """The order in the body is what counts, before the Webhook-Id.

    A sender that gives a retry a new Webhook-Id is accepted on the retry,
    so the retry test can report the changed id.
    """
    server = MockWebhookServer(port=0, failures_before_success=1)

    first = deliver(server, {"id": "ord_1"}, headers={"Webhook-Id": "evt_a"})
    retry = deliver(server, {"id": "ord_1"}, headers={"Webhook-Id": "evt_b"})

    self.assertEqual((first, retry), (500, 200))
    self.assertEqual(
      [e["headers"]["webhook-id"] for e in server.events], ["evt_a", "evt_b"]
    )

  def test_a_body_without_an_order_is_counted_by_webhook_id(self):
    """A delta, or a body that isn't an object, falls back to Webhook-Id."""
    server = MockWebhookServer(port=0, failures_before_success=1)
    event_a = {"Webhook-Id": "evt_a"}
    event_b = {"Webhook-Id": "evt_b"}

    self.assertEqual(deliver(server, {"delta": 1}, headers=event_a), 500)
    self.assertEqual(deliver(server, {"delta": 1}, headers=event_a), 200)
    self.assertEqual(deliver(server, ["no", "id"], headers=event_b), 500)
    self.assertEqual(deliver(server, {"id": ""}, headers=event_b), 200)

  def test_without_order_or_webhook_id_the_path_is_counted(self):
    """A body that isn't JSON and names no event is counted by its path."""
    server = MockWebhookServer(port=0, failures_before_success=1)

    self.assertEqual(deliver(server, content=b"not json"), 500)
    self.assertEqual(deliver(server, content=b"not json"), 200)
    self.assertEqual(
      deliver(server, content=b"not json", path="/elsewhere"), 500
    )
    self.assertIsNone(server.events[0]["payload"])
    self.assertEqual(server.events[0]["raw_body"], b"not json")

  def test_clear_events_forgets_the_refusals_too(self):
    """After clear_events an order is refused afresh."""
    server = MockWebhookServer(port=0, failures_before_success=1)
    self.assertEqual(deliver(server, {"id": "ord_1"}), 500)

    server.clear_events()

    self.assertEqual(server.events, [])
    self.assertEqual(deliver(server, {"id": "ord_1"}), 500)
    self.assertEqual(deliver(server, {"id": "ord_1"}), 200)

  def test_a_delivery_is_recorded_as_before(self):
    """The recorded event keeps the body, headers, path and answer."""
    server = MockWebhookServer(port=0, failures_before_success=1)

    deliver(server, {"id": "ord_1"}, headers={"Webhook-Id": "evt_a"})

    event = server.events[0]
    self.assertEqual(event["payload"], {"id": "ord_1"})
    self.assertEqual(event["partner_id"], "test_partner")
    self.assertEqual(event["path"], PATH.lstrip("/"))
    self.assertEqual(event["response_status"], 500)
    self.assertEqual(event["headers"]["webhook-id"], "evt_a")


class FirstDeliveryForTest(unittest.TestCase):
  """`WebhookTest._first_delivery_for` waits for the order's own delivery."""

  def _test_with(self, events: list) -> object:
    """Return a WebhookTest reading `events`; its setUp is not run."""
    test = webhook_test.WebhookTest("test_webhook_event_stream")
    test.webhook_server = types.SimpleNamespace(events=events)
    return test

  def test_strays_and_bodies_without_an_order_are_passed_over(self):
    """Deliveries about other orders, or about none, are not this test's.

    Before, the wait ended on the first delivery of any kind and the
    lookup read `payload["id"]` off each one, which a body without an id
    answered with KeyError.
    """
    own = {"payload": {"id": "ord_mine", "fulfillment": {}}}
    events = [
      {"payload": {"delta": True}},
      {"payload": None},
      {"payload": "not an object"},
      {"payload": {"id": "ord_from_an_earlier_test"}},
      own,
    ]

    found = self._test_with(events)._first_delivery_for("ord_mine")

    self.assertIs(found, own)

  def test_it_keeps_waiting_after_a_stray_arrived(self):
    """A stray that is already there does not end the wait."""
    own = {"payload": {"id": "ord_mine"}}
    events = [{"payload": {"id": "ord_from_an_earlier_test"}}]
    arrival = threading.Timer(0.15, events.append, args=(own,))
    arrival.start()
    self.addCleanup(arrival.cancel)

    found = self._test_with(events)._first_delivery_for("ord_mine")

    self.assertIs(found, own)

  def test_nothing_for_the_order_is_none(self):
    """When only strays arrive, the answer is None, not a stray."""
    events = [{"payload": {"id": "ord_from_an_earlier_test"}}]

    found = self._test_with(events)._first_delivery_for("ord_mine", polls=2)

    self.assertIsNone(found)


if __name__ == "__main__":
  unittest.main()
