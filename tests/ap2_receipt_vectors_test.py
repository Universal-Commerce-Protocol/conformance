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

"""Platform-side vectors for AP2 Order receipts (ucp#1063, ucp#1109).

Each fixture in fixtures/ap2_order_receipts/ is an Order with its receipts,
the business keys and the Payment Mandates a platform holds, and the exact
findings verification must produce. These tests need no server: they check
the vectors and the verifier in common/payments/ap2_receipt_verifier.py
against each other. They live in tests/, run by `uv run pytest tests`, so that
live suite runs do not report them as server results.
ap2_order_receipts_test.py applies the same verifier to receipts a live
business issues.
"""

import json
from pathlib import Path
from typing import Any

from absl.testing import absltest, parameterized
from common.payments import ap2_receipt_fixtures as fixtures
from common.payments import ap2_receipt_verifier as v

_FIXTURE_PATHS = sorted(fixtures.FIXTURE_DIR.glob("*.json"))

# `receipt_verification_outcome` in payment_ap2_mandate.json (ucp#1109).
_SPEC_OUTCOMES = {
  "receipt_invalid_signature",
  "receipt_sequence_gap",
  "receipt_totals_mismatch",
  "receipt_exceeds_mandate",
  "receipt_refund_exceeds_capture",
  "receipt_exceeds_seller_total",
  "receipt_mandate_unknown",
  "reconciliation_adjustment_mismatch",
}


def _load(path: Path) -> dict[str, Any]:
  with path.open(encoding="utf-8") as f:
    return json.load(f)


def _verify(fixture: dict[str, Any]) -> v.VerificationResult:
  inputs = fixture["inputs"]
  return v.verify_order_receipts(
    inputs["order"],
    {iss: issuer["keys"] for iss, issuer in inputs["issuers"].items()},
    [
      v.Mandate(m["credential_token"], m["business"], m["payment_amount"])
      for m in inputs["mandates"]
    ],
  )


def _as_expected(findings: list[v.Finding]) -> list[dict[str, Any]]:
  return [{"receipt_index": f.receipt_index, "code": f.code} for f in findings]


class Ap2ReceiptVectorsTest(parameterized.TestCase):
  """Runs every vector through the platform-side verifier."""

  def test_fixtures_are_present(self) -> None:
    """The fixture directory holds the generated vectors."""
    self.assertNotEmpty(
      _FIXTURE_PATHS, f"no fixtures in {fixtures.FIXTURE_DIR}"
    )

  def test_fixtures_match_generator(self) -> None:
    """The committed fixtures are exactly what the generator produces.

    Regenerate with `uv run python -m common.payments.ap2_receipt_fixtures`.
    """
    generated = fixtures.build_fixtures()
    self.assertCountEqual(generated, [p.name for p in _FIXTURE_PATHS])
    for path in _FIXTURE_PATHS:
      self.assertEqual(
        path.read_text(encoding="utf-8"),
        generated[path.name],
        f"{path.name} differs from the generator output",
      )

  def test_test_plan_is_covered(self) -> None:
    """Vectors 1-12 of the ucp#1063 test plan each have a fixture."""
    covered = {_load(p)["test_plan_vector"] for p in _FIXTURE_PATHS}
    self.assertContainsSubset({str(n) for n in range(1, 13)}, covered)

  def test_every_outcome_is_exercised(self) -> None:
    """Each `receipt_verification_outcome` value is expected somewhere."""
    seen = set()
    for path in _FIXTURE_PATHS:
      for group in _load(path)["expected"].values():
        seen.update(finding["code"] for finding in group)
    self.assertEqual(seen, _SPEC_OUTCOMES)

  @parameterized.named_parameters((path.stem, path) for path in _FIXTURE_PATHS)
  def test_vector(self, path: Path) -> None:
    """The verifier reports exactly the fixture's expected findings."""
    fixture = _load(path)
    result = _verify(fixture)
    expected = fixture["expected"]
    self.assertEqual(
      _as_expected(result.outcomes), expected["outcomes"], "outcomes"
    )
    self.assertEqual(
      _as_expected(result.warnings), expected["warnings"], "warnings"
    )
    self.assertEqual(
      _as_expected(result.reconciliation),
      expected["reconciliation"],
      "reconciliation",
    )

  @parameterized.named_parameters((path.stem, path) for path in _FIXTURE_PATHS)
  def test_decoded_receipts_match(self, path: Path) -> None:
    """The decoded copy in each fixture is the receipts' real content."""
    fixture = _load(path)
    decoded = []
    for compact in fixture["inputs"]["order"]["ap2"]["receipts"]:
      header, payload = v.decode_receipt(compact)
      decoded.append({"header": header, "payload": payload})
    self.assertEqual(decoded, fixture["decoded_receipts"])

  @parameterized.named_parameters(
    (path.stem, path)
    for path in _FIXTURE_PATHS
    if not any(_load(path)["expected"].values())
  )
  def test_totals_change_only_on_succeeded(self, path: Path) -> None:
    """In each valid vector the stated totals follow from the events.

    receipt_event.status: "Only `succeeded` events change the running
    totals." A receipt that is not counted (pending, failed, or a follow-up
    to a movement already counted) repeats the previous totals of its chain.
    """
    result = _verify(_load(path))
    previous: dict[v.ChainKey, dict[str, int]] = {}
    for receipt in result.verified:
      totals = receipt.payload["totals"]
      derived = receipt.derived_totals
      self.assertEqual(
        (totals["authorized"], totals["captured"], totals["refunded"]),
        (derived.authorized, derived.captured, derived.refunded),
        f"receipt {receipt.index}",
      )
      if not receipt.counted:
        self.assertEqual(
          totals, previous[receipt.chain], f"receipt {receipt.index}"
        )
      previous[receipt.chain] = totals

  @parameterized.named_parameters(
    ("delayed_network_ref", "refund_delayed_network_ref", True),
    ("reversal_no_network_ref", "refund_reversal_no_network_ref", False),
  )
  def test_pending_refund_then_succeeded(
    self, vector_id: str, has_network_ref: bool
  ) -> None:
    """A pending refund is followed by a succeeded one for the same ref.

    Edge Cases, "Pending movements" and "Late network references":
    `network_ref` is optional on every status, so the succeeded receipt
    carries it only when the network assigned one.
    """
    result = _verify(_load(fixtures.FIXTURE_DIR / f"{vector_id}.json"))
    self.assertEmpty(result.outcomes)
    pending, succeeded = result.verified[-2:]
    pending_event = pending.payload["event"]
    succeeded_event = succeeded.payload["event"]
    self.assertEqual(pending_event["type"], "refund")
    self.assertEqual(pending_event["status"], "pending")
    self.assertNotIn("network_ref", pending_event)
    self.assertEqual(succeeded_event["status"], "succeeded")
    self.assertEqual(succeeded_event["ref"], pending_event["ref"])
    self.assertEqual(succeeded.payload["seq"], pending.payload["seq"] + 1)
    self.assertEqual(pending.payload["totals"]["refunded"], 0)
    self.assertEqual(
      succeeded.payload["totals"]["refunded"], succeeded_event["amount"]
    )
    self.assertEqual("network_ref" in succeeded_event, has_network_ref)


class Ap2ReceiptNewRulesTest(absltest.TestCase):
  """Checks that the vectors for the revised rules show what they claim."""

  def _result(self, vector_id: str) -> v.VerificationResult:
    return _verify(_load(fixtures.FIXTURE_DIR / f"{vector_id}.json"))

  def test_authorized_after_capture_and_release(self) -> None:
    """3000 captured and 2400 released leaves authorized at 3000."""
    result = self._result("authorized_after_capture_and_release")
    self.assertEmpty(result.outcomes)
    self.assertEqual(
      result.verified[-1].derived_totals, v.Totals(3000, 3000, 0)
    )

  def test_mandated_mismatch_case(self) -> None:
    """The 9000 authorization against a 5400 mandate."""
    result = self._result("totals_mismatch_mandated_9000_auth")
    self.assertEqual(
      [f.code for f in result.outcomes],
      [v.RECEIPT_TOTALS_MISMATCH, v.RECEIPT_EXCEEDS_MANDATE],
    )

  def test_duplicate_seq_keeps_the_first_receipt_in_the_chain(self) -> None:
    """The later receipt for seq 2 is not counted and seq 3 still chains."""
    result = self._result("duplicate_different_seq")
    self.assertEqual([r.index for r in result.verified], [0, 1, 3])
    self.assertEqual(result.verified[-1].derived_totals.captured, 5400)
    self.assertEqual(
      [(f.receipt_index, f.code) for f in result.outcomes],
      [(2, v.RECEIPT_SEQUENCE_GAP)],
    )

  def test_non_jcs_vector_is_not_jcs(self) -> None:
    """The vector really has payload bytes that differ from JCS form."""
    fixture = _load(fixtures.FIXTURE_DIR / "non_jcs_bytes_accepted.json")
    receipts = fixture["inputs"]["order"]["ap2"]["receipts"]
    for compact, jcs in zip(receipts, [False, False, True], strict=True):
      _, payload_b64, _ = compact.split(".")
      raw = v.b64url_decode(payload_b64)
      canonical = fixtures._jcs(json.loads(raw))
      self.assertEqual(raw == canonical, jcs, compact[:20])
    self.assertEmpty(_verify(fixture).outcomes)

  def test_follow_up_does_not_count_twice(self) -> None:
    """A network_ref follow-up repeats the totals and is not counted."""
    result = self._result("network_ref_follow_up")
    self.assertEmpty(result.outcomes)
    self.assertEmpty(result.reconciliation)
    self.assertEqual([r.counted for r in result.verified], [True] * 3 + [False])
    self.assertEqual(result.verified[-1].derived_totals.refunded, 5400)

  def test_no_receipt_is_dropped_by_a_duplicate(self) -> None:
    """All four receipts of the duplicate vector are in the Order."""
    fixture = _load(fixtures.FIXTURE_DIR / "duplicate_different_seq.json")
    seqs = [p["payload"]["seq"] for p in fixture["decoded_receipts"]]
    self.assertEqual(seqs, [1, 2, 2, 3])


class Ap2ReceiptMutationTest(absltest.TestCase):
  """Changes to a valid chain that the verifier must reject.

  Starts from the ucp#1109 chain (v02_split_captures), which verifies.
  """

  def setUp(self) -> None:
    """Load the valid ucp#1109 chain."""
    super().setUp()
    self.fixture = _load(fixtures.FIXTURE_DIR / "v02_split_captures.json")
    self.receipts = self.fixture["inputs"]["order"]["ap2"]["receipts"]

  def _outcomes(self, receipts: list[str]) -> list[tuple[int, str]]:
    self.fixture["inputs"]["order"]["ap2"]["receipts"] = receipts
    return [(f.receipt_index, f.code) for f in _verify(self.fixture).outcomes]

  def test_unchanged_chain_verifies(self) -> None:
    """The starting point has no findings."""
    self.assertEqual(self._outcomes(list(self.receipts)), [])

  def test_flipped_signature_byte(self) -> None:
    """A changed signature character invalidates the signature."""
    last = self.receipts[2]
    flipped = last[:-5] + ("A" if last[-5] != "A" else "B") + last[-4:]
    self.assertEqual(
      self._outcomes([*self.receipts[:2], flipped]),
      [(2, v.RECEIPT_INVALID_SIGNATURE)],
    )

  def test_flipped_payload_byte(self) -> None:
    """A changed payload character invalidates the signature."""
    header, payload, signature = self.receipts[1].split(".")
    index = payload.index("6")  # inside the encoded payload
    payload = payload[:index] + "7" + payload[index + 1 :]
    self.assertEqual(
      self._outcomes(
        [self.receipts[0], f"{header}.{payload}.{signature}", self.receipts[2]]
      )[0],
      (1, v.RECEIPT_INVALID_SIGNATURE),
    )

  def test_reordered_receipts(self) -> None:
    """Receipts out of `seq` order are a sequence gap."""
    self.assertIn(
      (1, v.RECEIPT_SEQUENCE_GAP),
      self._outcomes([self.receipts[0], self.receipts[2], self.receipts[1]]),
    )

  def test_identical_repeat_is_ignored(self) -> None:
    """A byte-identical redelivery of a held receipt is ignored.

    Platform Verification 4 (ucp#1109 at fc59727): a byte-identical
    redelivery is ignored, and the outcome table says "repeated with
    different content". The copy is neither chained nor flagged.
    """
    self.assertEqual(self._outcomes([*self.receipts, self.receipts[2]]), [])

  def test_gap_reports_no_totals_mismatch(self) -> None:
    """Recomputation stops at a gap, so only the gap is reported."""
    self.assertEqual(
      self._outcomes([self.receipts[0], self.receipts[2]]),
      [(1, v.RECEIPT_SEQUENCE_GAP)],
    )

  def test_unresolved_kid_excludes_receipt_from_chain(self) -> None:
    """The receipt after one whose `kid` does not resolve is a gap."""
    issuers = self.fixture["inputs"]["issuers"]
    for issuer in issuers.values():
      issuer["keys"] = [dict(k, kid="other_kid") for k in issuer["keys"]]
    outcomes = self._outcomes(list(self.receipts))
    self.assertEqual(
      outcomes[:2],
      [(0, v.RECEIPT_INVALID_SIGNATURE), (1, v.RECEIPT_INVALID_SIGNATURE)],
    )

  def test_missing_typ_header(self) -> None:
    """A header without `typ` cannot be verified as a receipt."""
    header, payload, signature = self.receipts[2].split(".")
    decoded = json.loads(v.b64url_decode(header))
    del decoded["typ"]
    stripped = v.b64url_encode(json.dumps(decoded).encode())
    self.assertEqual(
      self._outcomes([*self.receipts[:2], f"{stripped}.{payload}.{signature}"]),
      [(2, v.RECEIPT_INVALID_SIGNATURE)],
    )

  def test_stated_mandated_must_equal_payment_amount(self) -> None:
    """`totals.mandated` is checked against the mandate's payment_amount."""
    self.fixture["inputs"]["mandates"][0]["payment_amount"] = 6000
    outcomes = self._outcomes(list(self.receipts))
    self.assertEqual(
      outcomes, [(i, v.RECEIPT_TOTALS_MISMATCH) for i in range(3)]
    )

  def test_unknown_payment_amount_skips_the_mandated_check(self) -> None:
    """A caller that cannot read `payment_amount` still gets the rest."""
    self.fixture["inputs"]["mandates"][0]["payment_amount"] = None
    self.assertEqual(self._outcomes(list(self.receipts)), [])

  def test_unknown_issuer_key(self) -> None:
    """A `kid` that does not resolve in the keys of `iss` cannot verify."""
    issuers = self.fixture["inputs"]["issuers"]
    for issuer in issuers.values():
      issuer["keys"] = [dict(k, kid="other_kid") for k in issuer["keys"]]
    self.assertEqual(
      self._outcomes(list(self.receipts))[0],
      (0, v.RECEIPT_INVALID_SIGNATURE),
    )

  def test_unknown_mandate(self) -> None:
    """A receipt whose `mandate_ref` the platform never sent is unknown."""
    self.fixture["inputs"]["mandates"][0]["credential_token"] = "other"
    self.assertEqual(
      self._outcomes(list(self.receipts)),
      [(i, v.RECEIPT_MANDATE_UNKNOWN) for i in range(3)],
    )

  def test_mandate_covers_another_business(self) -> None:
    """A mandate that covers another business makes `iss` mismatch."""
    self.fixture["inputs"]["mandates"][0]["business"] = (
      "https://other.example/.well-known/ucp"
    )
    self.assertEqual(
      self._outcomes(list(self.receipts)),
      [(i, v.RECEIPT_MANDATE_UNKNOWN) for i in range(3)],
    )

  def test_not_a_compact_jws(self) -> None:
    """A receipt that is not a compact JWS cannot be verified."""
    self.assertEqual(
      self._outcomes([*self.receipts, "not-a-jws"]),
      [(3, v.RECEIPT_INVALID_SIGNATURE)],
    )

  def test_signed_receipt_missing_members(self) -> None:
    """A validly signed payload without required members is malformed.

    The specification defines no outcome for it. It is recorded apart, and
    the other receipts are still verified.
    """
    iss = "https://merchant.example/.well-known/ucp"
    signer = fixtures.FixtureSigner(iss, "test_only_2026")
    receipt = signer.sign({"iss": iss, "seq": "1"})
    self.fixture["inputs"]["issuers"][iss] = {"keys": [signer.jwk]}
    self.fixture["inputs"]["order"]["ap2"]["receipts"] = [
      *self.receipts,
      receipt,
    ]
    result = _verify(self.fixture)
    self.assertEqual(result.outcomes, [])
    self.assertEqual([i for i, _ in result.malformed], [3])
    self.assertRegex(result.malformed[0][1], "mandate_ref.*seq")
    self.assertLen(result.verified, 3)

  def test_receipt_that_is_not_a_string(self) -> None:
    """A non-string entry in `ap2.receipts` cannot be verified."""
    self.assertEqual(
      self._outcomes([*self.receipts, {"not": "a receipt"}]),
      [(3, v.RECEIPT_INVALID_SIGNATURE)],
    )

  def test_seller_not_in_sellers_map_is_not_checked(self) -> None:
    """An unresolved `seller_id` makes attribution unavailable (ucp#1018)."""
    fixture = _load(fixtures.FIXTURE_DIR / "v09_duplicate_seller_capture.json")
    del fixture["inputs"]["order"]["sellers"]["seller_a"]
    self.assertEqual(_verify(fixture).warnings, [])


_MALFORMED_ISS = "https://malformed.example/.well-known/ucp"


def _valid_payload() -> dict[str, Any]:
  """Return a Receipt Payload that passes the shape checks."""
  return {
    "iss": _MALFORMED_ISS,
    "iat": 1794236000,
    "order_id": "order_1",
    "checkout_id": "checkout_1",
    "mandate_ref": v.sha256_ref("token"),
    "seq": 1,
    "prev": None,
    "event": {
      "type": "authorization",
      "status": "succeeded",
      "amount": 100,
      "currency": "USD",
      "occurred_at": "2026-11-01T00:00:00Z",
    },
    "totals": {
      "mandated": 100,
      "authorized": 100,
      "captured": 0,
      "refunded": 0,
    },
  }


def _with(path: str, value: Any) -> dict[str, Any]:
  """Return a valid payload with the member at `path` ("a.b") set."""
  payload = _valid_payload()
  *parents, leaf = path.split(".")
  target = payload
  for name in parents:
    target = target[name]
  target[leaf] = value
  return payload


class Ap2ReceiptMalformedInputTest(parameterized.TestCase):
  """Signed receipts whose payload breaks the schema are `malformed`.

  They are signed by a business whose keys are known, so the signature
  verifies and the shape checks are what reject them. None may raise.
  """

  def _verify(self, payload: dict[str, Any], order: dict | None = None):
    signer = fixtures.FixtureSigner(_MALFORMED_ISS, "test_only_2026")
    order = order or {}
    order["ap2"] = {"receipts": [signer.sign(payload)]}
    return v.verify_order_receipts(
      order,
      {_MALFORMED_ISS: [signer.jwk]},
      [v.Mandate("token", _MALFORMED_ISS, 100)],
    )

  def test_valid_payload_is_not_malformed(self) -> None:
    """The base payload for the cases below passes the shape checks."""
    result = self._verify(_valid_payload())
    self.assertEqual(result.malformed, [])
    self.assertEqual(result.outcomes, [])

  @parameterized.named_parameters(
    ("iat_negative", "iat", -1, "iat"),
    ("iat_string", "iat", "1794236000", "iat"),
    ("iat_bool", "iat", True, "iat"),
    ("order_id_int", "order_id", 7, "order_id"),
    ("order_id_null", "order_id", None, "order_id"),
    ("seq_zero", "seq", 0, "seq"),
    ("seq_negative", "seq", -3, "seq"),
    ("seq_string", "seq", "1", "seq"),
    ("amount_negative", "event.amount", -1, "event.amount"),
    ("amount_string", "event.amount", "100", "event.amount"),
    ("mandated_negative", "totals.mandated", -1, "totals.mandated"),
    ("authorized_negative", "totals.authorized", -1, "totals.authorized"),
    ("captured_negative", "totals.captured", -1, "totals.captured"),
    ("refunded_negative", "totals.refunded", -1, "totals.refunded"),
    ("unknown_type", "event.type", "chargeback", "event.type"),
    ("unhashable_type", "event.type", ["capture"], "event.type"),
    ("unknown_status", "event.status", "settled", "event.status"),
    ("seller_id_int", "event.seller_id", 5, "event.seller_id"),
    ("ref_list", "event.ref", ["ent_1"], "event.ref"),
    ("ref_string", "event.ref", "ent_1", "event.ref"),
    (
      "entry_id_list",
      "event.ref",
      {"processing_entry_id": ["a"]},
      "event.ref.processing_entry_id",
    ),
    (
      "adjustment_id_dict",
      "event.ref",
      {"adjustment_id": {"a": 1}},
      "event.ref.adjustment_id",
    ),
  )
  def test_rejected_as_malformed(
    self, path: str, value: Any, member: str
  ) -> None:
    """A bad value is recorded as malformed, naming the member."""
    result = self._verify(_with(path, value))
    self.assertEqual([i for i, _ in result.malformed], [0])
    self.assertIn(member, result.malformed[0][1])
    self.assertEqual(result.outcomes, [])
    self.assertEqual(result.verified, [])

  @parameterized.named_parameters(
    # An empty id is absent, so both captures count and 200 is right.
    ("empty", "", []),
    # The same id makes the second a follow-up: 100 is right, not 200.
    ("same_id", "ent_1", [v.RECEIPT_TOTALS_MISMATCH]),
  )
  def test_empty_processing_entry_id_is_absent(
    self, entry_id: str, codes: list[str]
  ) -> None:
    """An empty `processing_entry_id` does not make a follow-up."""
    signer = fixtures.FixtureSigner(_MALFORMED_ISS, "test_only_2026")
    receipts: list[str] = []
    for seq in (1, 2):
      payload = _valid_payload()
      payload["seq"] = seq
      payload["prev"] = v.sha256_ref(receipts[-1]) if receipts else None
      payload["event"].update(
        type="capture", ref={"processing_entry_id": entry_id}
      )
      payload["totals"].update(mandated=300, authorized=0, captured=100 * seq)
      receipts.append(signer.sign(payload))
    result = v.verify_order_receipts(
      {"ap2": {"receipts": receipts}},
      {_MALFORMED_ISS: [signer.jwk]},
      [v.Mandate("token", _MALFORMED_ISS, 300)],
    )
    self.assertEqual(result.malformed, [])
    self.assertEqual([f.code for f in result.outcomes], codes)

  def _chain(self, stated: list[dict[str, int]], seqs: list[int]):
    """Verify captures of 100 stating `stated` totals, with these `seq`s."""
    signer = fixtures.FixtureSigner(_MALFORMED_ISS, "test_only_2026")
    receipts: list[str] = []
    for seq, totals in zip(seqs, stated, strict=True):
      payload = _valid_payload()
      payload["seq"] = seq
      payload["prev"] = v.sha256_ref(receipts[-1]) if receipts else None
      payload["event"].update(
        type="capture", ref={"processing_entry_id": f"ent_{seq}"}
      )
      payload["totals"].update(mandated=300, authorized=0, **totals)
      receipts.append(signer.sign(payload))
    result = v.verify_order_receipts(
      {"ap2": {"receipts": receipts}},
      {_MALFORMED_ISS: [signer.jwk]},
      [v.Mandate("token", _MALFORMED_ISS, 300)],
    )
    return [(f.receipt_index, f.code) for f in result.outcomes]

  def test_after_a_gap_rule_6_uses_stated_totals(self) -> None:
    """Later receipts report no totals mismatch but still the exceed."""
    outcomes = self._chain(
      [{"captured": 100, "refunded": 0}, {"captured": 400, "refunded": 0}],
      [1, 3],
    )
    self.assertEqual(
      outcomes,
      [(1, v.RECEIPT_SEQUENCE_GAP), (1, v.RECEIPT_EXCEEDS_MANDATE)],
    )

  def test_honest_totals_over_the_mandate_report_only_the_exceed(self) -> None:
    """`receipt_exceeds_mandate` does not imply `receipt_totals_mismatch`."""
    outcomes = self._chain(
      [{"captured": 100 * n, "refunded": 0} for n in range(1, 5)],
      [1, 2, 3, 4],
    )
    self.assertEqual(outcomes, [(3, v.RECEIPT_EXCEEDS_MANDATE)])

  @parameterized.named_parameters(
    ("null", None, []),
    ("empty_list", [], []),
    ("string", "", [(-1, "ap2.receipts is not an array")]),
    ("zero", 0, [(-1, "ap2.receipts is not an array")]),
    ("object", {}, [(-1, "ap2.receipts is not an array")]),
  )
  def test_receipts_field(self, value: Any, malformed: list) -> None:
    """`ap2.receipts` of null is none; other non-arrays are malformed."""
    result = v.verify_order_receipts({"ap2": {"receipts": value}}, {}, [])
    self.assertEqual(result.malformed, malformed)
    self.assertEqual(result.outcomes, [])

  @parameterized.named_parameters(
    ("ap2_null", {"ap2": None}),
    ("ap2_list", {"ap2": []}),
    ("no_ap2", {}),
  )
  def test_ap2_member(self, order: dict) -> None:
    """An Order without a usable `ap2` object has no receipts."""
    result = v.verify_order_receipts(order, {}, [])
    self.assertEqual((result.malformed, result.outcomes), ([], []))

  def test_unreadable_order_data_does_not_raise(self) -> None:
    """Odd `line_items`, `adjustments` and `sellers` are skipped."""
    payload = _valid_payload()
    payload["event"].update(
      type="refund", seller_id="s1", ref={"adjustment_id": "adj_1"}
    )
    payload["totals"].update(authorized=0, refunded=100)
    order = {
      "sellers": {"s1": {}},
      "line_items": [
        None,
        {"seller_id": "s1"},
        {"seller_id": "s1", "totals": [None, {"type": "total"}, 3]},
        {"seller_id": ["x"], "totals": "none"},
      ],
      "adjustments": [None, {}, {"id": "adj_1"}, {"id": 4, "totals": 1}],
    }
    result = self._verify(payload, order)
    self.assertEqual(result.malformed, [])
    self.assertEqual(result.reconciliation, [])

  @parameterized.named_parameters(
    ("line_items_string", {"line_items": "x", "adjustments": "x"}),
    ("sellers_list", {"sellers": [], "adjustments": {}}),
  )
  def test_wrong_typed_order_collections(self, extra: dict) -> None:
    """Order collections of the wrong type are ignored."""
    result = self._verify(_valid_payload(), dict(extra))
    self.assertEqual((result.malformed, result.warnings), ([], []))

  def test_non_object_keys_are_ignored(self) -> None:
    """A `keys` entry that is not an object does not stop `kid` lookup."""
    signer = fixtures.FixtureSigner(_MALFORMED_ISS, "test_only_2026")
    receipt = signer.sign(_valid_payload())
    result = v.verify_order_receipts(
      {"ap2": {"receipts": [receipt]}},
      {_MALFORMED_ISS: [None, "k", signer.jwk]},
      [v.Mandate("token", _MALFORMED_ISS, 100)],
    )
    self.assertEqual((result.malformed, result.outcomes), ([], []))


if __name__ == "__main__":
  absltest.main()
