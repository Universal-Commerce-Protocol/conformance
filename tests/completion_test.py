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

"""Unit tests for the checkout set-up and completion helpers.

They drive the suite's own helpers (`create_checkout_session`,
`complete_checkout_session`, `create_completed_order`) against the scripted
store in `strict_store.py`, which behaves the way a business the sample
server does not resemble may: it needs a buyer to place an order, answers
in-band, and returns no `payment` member. No server or fixture file is
needed.
"""

import json
import unittest
from unittest import mock

from shopping import base
from tests import strict_store


def make_probe(
  store: strict_store.StrictStore,
) -> base.ShoppingIntegrationTestBase:
  """Return a test instance whose requests land in `store`.

  The class is defined here, not at module level, so that pytest does not
  collect it as a test of its own.
  """

  class Probe(base.ShoppingIntegrationTestBase):
    """A test instance wired to a scripted store instead of a live server."""

    def runTest(self):  # noqa: N802 - unittest's default method name
      """Do nothing; the helpers are called directly."""

  probe = Probe()
  probe.client = store.client()
  probe._shopping_service_endpoint = strict_store.ENDPOINT
  probe.conformance_config = {}
  probe.fixture_ctx = base.DynamicFixtureContext({})
  # The completion poll waits for real time otherwise.
  probe.COMPLETION_POLL_SECONDS = 0
  return probe


def sent_json(request) -> dict:
  """Return the JSON body a recorded request carried."""
  return json.loads(request.content)


class CreateCheckoutPayloadTest(unittest.TestCase):
  """The Create Checkout body is what the spec takes as input."""

  def setUp(self):
    """Build the default body once."""
    self.probe = make_probe(strict_store.StrictStore())
    self.body = self.probe.create_checkout_payload().model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

  def test_destination_uses_the_spec_field_names(self):
    """`address_locality` and `address_region`, not `locality`/`region`."""
    destination = self.body["fulfillment"]["methods"][0]["destinations"][0]
    self.assertIn("address_locality", destination)
    self.assertIn("address_region", destination)
    self.assertNotIn("locality", destination)
    self.assertNotIn("region", destination)
    self.assertEqual(destination["type"], "shipping_address")

  def test_no_option_is_preselected_and_no_ids_are_invented(self):
    """The business offers the options; nothing names `std-ship`."""
    method = self.body["fulfillment"]["methods"][0]
    self.assertNotIn("groups", method)
    self.assertNotIn("line_item_ids", method)
    self.assertNotIn("id", method)
    self.assertEqual(method["selected_destination_id"], "dest_1")
    self.assertNotIn("std-ship", json.dumps(self.body))
    self.assertNotIn("line_item_123", json.dumps(self.body))

  def test_only_request_members_are_sent(self):
    """No id, status, currency, totals, links or ucp at the root."""
    for member in ("id", "status", "currency", "totals", "links", "ucp"):
      self.assertNotIn(member, self.body)
    line_item = self.body["line_items"][0]
    self.assertEqual(set(line_item), {"item", "quantity"})
    self.assertEqual(set(line_item["item"]), {"id"})

  def test_buyer_and_context_pass_through(self):
    """A buyer and a context are inputs and go through unchanged."""
    body = self.probe.create_checkout_payload(
      buyer={"email": "b@example.com"}, context={"source": "test"}
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    self.assertEqual(body["buyer"], {"email": "b@example.com"})
    self.assertEqual(body["context"], {"source": "test"})

  def test_fulfillment_can_be_left_out(self):
    """`include_fulfillment=False` sends no fulfillment at all."""
    body = self.probe.create_checkout_payload(
      include_fulfillment=False
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    self.assertNotIn("fulfillment", body)


class CompletionProblemTest(unittest.TestCase):
  """`completion_problem` reads a Complete Checkout response."""

  def test_completed_with_order_is_no_problem(self):
    """A completed checkout carrying an order placed it."""
    body = {"status": "completed", "order": {"id": "ord_1"}}
    self.assertIsNone(base.completion_problem(body))
    self.assertEqual(base.placed_order(body), {"id": "ord_1"})

  def test_incomplete_quotes_status_and_errors(self):
    """The business's status and its error messages are in the sentence."""
    body = {
      "status": "incomplete",
      "messages": [
        {
          "type": "error",
          "code": "missing_buyer_info",
          "path": "$.buyer.email",
          "content": "Buyer email is required to complete checkout.",
        },
        {"type": "info", "code": "note", "content": "ignored"},
      ],
    }
    problem = base.completion_problem(body)
    self.assertIn("status 'incomplete' without an order", problem)
    self.assertIn("missing_buyer_info at $.buyer.email", problem)
    self.assertIn("Buyer email is required", problem)
    self.assertNotIn("ignored", problem)

  def test_error_response_is_named_as_such(self):
    """An Error Response (ucp.status error, no resource) is described."""
    body = {
      "ucp": {"status": "error"},
      "messages": [{"type": "error", "code": "not_found", "content": "Gone."}],
    }
    problem = base.completion_problem(body)
    self.assertIn("Error Response", problem)
    self.assertIn("not_found: Gone.", problem)

  def test_completed_without_order_and_in_progress(self):
    """The two other shapes that have placed nothing yet."""
    self.assertIn(
      "'completed' without an order",
      base.completion_problem({"status": "completed"}),
    )
    self.assertIn(
      "complete_in_progress",
      base.completion_problem({"status": "complete_in_progress"}),
    )

  def test_non_object_body(self):
    """A body that is not a JSON object is reported, not crashed on."""
    self.assertIn("no JSON object", base.completion_problem("<html>"))
    self.assertEqual(base.error_messages("<html>"), [])

  def test_describe_messages_renders_rich_content(self):
    """A `{plain: ...}` content renders as its plain text."""
    text = base.describe_messages(
      [{"code": "x", "path": "$.y", "content": {"plain": "Plain."}}]
    )
    self.assertEqual(text, "  - x at $.y: Plain.")
    self.assertEqual(base.describe_messages([]), "  (no messages)")


class CompletionAgainstStrictStoreTest(unittest.TestCase):
  """The completion helpers against a business that needs a buyer."""

  def test_create_completed_order_sends_the_buyer_on_create(self):
    """The order is placed; the buyer went in the Create Checkout body."""
    store = strict_store.StrictStore()
    probe = make_probe(store)

    order_id = probe.create_completed_order()

    self.assertTrue(order_id.startswith("ord_"))
    create = sent_json(store.requests[0])
    self.assertEqual(
      create["buyer"]["email"],
      base.ShoppingIntegrationTestBase.DEFAULT_BUYER["email"],
    )
    completed = store.checkouts[next(iter(store.checkouts))]
    self.assertEqual(completed["status"], "completed")
    # No buyer update was needed: the buyer was on the checkout from the
    # start, and the only PUT selected the shipping option.
    puts = [r for r in store.requests if r.method == "PUT"]
    self.assertEqual(len(puts), 1)
    self.assertNotIn(
      "buyer",
      {
        k: v
        for k, v in sent_json(puts[0]).items()
        if k == "buyer" and v is None
      },
    )

  def test_complete_checkout_session_adds_a_missing_buyer(self):
    """A checkout created without a buyer gets one before completion."""
    store = strict_store.StrictStore()
    probe = make_probe(store)
    created = probe.create_checkout_session()
    self.assertEqual(created["status"], "incomplete")
    self.assertEqual(
      [m["code"] for m in created["messages"]], ["missing_buyer_info"]
    )

    completed = probe.complete_checkout_session(created["id"])

    self.assertEqual(completed["status"], "completed")
    self.assertIn("id", completed["order"])
    buyer_puts = [
      r
      for r in store.requests
      if r.method == "PUT" and sent_json(r).get("buyer", {}).get("email")
    ]
    self.assertEqual(len(buyer_puts), 1)
    self.assertEqual(
      sent_json(buyer_puts[0])["buyer"]["email"],
      probe.DEFAULT_BUYER["email"],
    )

  def test_a_buyer_already_on_the_checkout_is_kept(self):
    """An explicit buyer is not replaced by the default one."""
    store = strict_store.StrictStore()
    probe = make_probe(store)
    created = probe.create_checkout_session(buyer={"email": "me@example.com"})

    probe.complete_checkout_session(created["id"])

    checkout = store.checkouts[created["id"]]
    self.assertEqual(checkout["buyer"]["email"], "me@example.com")
    self.assertEqual(checkout["status"], "completed")

  def test_default_payload_raises_no_fulfillment_errors(self):
    """With the spec field names the store has nothing to complain about.

    Before, `locality`/`region` and the sample store's `std-ship` option
    gave every default checkout two errors the helper then dropped.
    """
    store = strict_store.StrictStore(needs_buyer=False)
    probe = make_probe(store)

    created = probe.create_checkout_session()

    self.assertEqual(created["messages"], [])
    self.assertEqual(created["status"], "ready_for_complete")
    method = created["fulfillment"]["methods"][0]
    self.assertEqual(method["groups"][0]["selected_option_id"], "ground")

  def test_refusal_fails_with_the_business_words(self):
    """No order: the failure quotes the status and the error message."""
    store = strict_store.StrictStore()
    probe = make_probe(store)
    created = probe.create_checkout_session(buyer=probe.DEFAULT_BUYER)
    failing = base.get_valid_payment_payload()
    failing["payment"]["instruments"][0]["credential"]["token"] = "fail_token"

    with self.assertRaises(AssertionError) as caught:
      probe.complete_checkout_session(created["id"], payment_payload=failing)

    message = str(caught.exception)
    self.assertIn("status 'incomplete' without an order", message)
    self.assertIn("payment_declined at $.payment", message)
    self.assertIn("read back as status 'ready_for_complete'", message)
    self.assertNotIn("KeyError", message)

  def test_accepted_completion_is_followed_with_get(self):
    """`complete_in_progress` is polled with Get Checkout until it settles."""
    store = strict_store.StrictStore(async_completion=True)
    probe = make_probe(store)

    order_id = probe.create_completed_order()

    self.assertTrue(order_id.startswith("ord_"))
    methods = [r.method for r in store.requests]
    # POST complete, then at least one GET, and no second POST complete.
    completes = [r for r in store.requests if r.url.path.endswith("/complete")]
    self.assertEqual(len(completes), 1)
    self.assertEqual(methods[-1], "GET")

  def test_accepted_completion_that_never_settles_fails_in_words(self):
    """A completion that stays in progress fails with that status named."""
    store = strict_store.StrictStore(async_completion=True)
    probe = make_probe(store)
    probe.COMPLETION_POLLS = 0  # Do not poll at all.
    created = probe.create_checkout_session(buyer=probe.DEFAULT_BUYER)

    with self.assertRaises(AssertionError) as caught:
      probe.complete_checkout_session(created["id"])

    self.assertIn("complete_in_progress", str(caught.exception))

  def test_no_payment_member_does_not_crash_the_update_helper(self):
    """The store returns no `payment`; updates are still built."""
    store = strict_store.StrictStore()
    probe = make_probe(store)
    created = probe.create_checkout_session(select_fulfillment=False)
    self.assertNotIn("payment", created)
    checkout_obj = base.f_models.Checkout(**created)

    payload = probe.checkout_update_payload(
      checkout_obj, buyer={"email": "x@example.com", "first_name": "X"}
    )

    self.assertEqual(payload["buyer"]["email"], "x@example.com")
    self.assertNotIn("payment", payload)

  def test_an_instrument_on_the_checkout_is_carried_over(self):
    """A checkout that already carries an instrument is still updatable.

    The response model's instrument objects are not the request model's;
    the helper dumps them to JSON, as it does the other carried fields.
    """
    store = strict_store.StrictStore()
    probe = make_probe(store)
    created = probe.create_checkout_session(select_fulfillment=False)
    created["payment"] = {
      "instruments": [
        {
          "id": "instr_1",
          "handler_id": "mock_payment_handler",
          "type": "card",
          "display": {"brand": "Visa", "last_digits": "1234"},
        }
      ]
    }
    checkout_obj = base.f_models.Checkout(**created)

    payload = probe.checkout_update_payload(
      checkout_obj, buyer={"email": "x@example.com"}
    )

    self.assertEqual(payload["payment"]["instruments"][0]["id"], "instr_1")
    self.assertEqual(payload["buyer"]["email"], "x@example.com")

  def test_errors_the_test_did_not_ask_for_are_logged(self):
    """Error messages on a fresh checkout reach the log."""
    store = strict_store.StrictStore()
    probe = make_probe(store)

    with mock.patch.object(base.logging, "warning") as warning:
      probe.create_checkout_session()

    logged = " ".join(str(call) for call in warning.call_args_list)
    self.assertIn("missing_buyer_info", logged)


if __name__ == "__main__":
  unittest.main()
