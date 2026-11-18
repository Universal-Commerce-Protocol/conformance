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
the vectors and the verifier in ap2_receipt_verifier.py against each other.
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
      v.Mandate(m["credential_token"], m["business"])
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
    totals." A receipt that is not succeeded repeats the previous totals
    of its chain.
    """
    result = _verify(_load(path))
    previous: dict[v.ChainKey, dict[str, int]] = {}
    for receipt in result.verified:
      totals = receipt.payload["totals"]
      self.assertEqual(
        (totals["captured"], totals["refunded"]),
        (receipt.derived_totals.captured, receipt.derived_totals.refunded),
        f"receipt {receipt.index}",
      )
      if receipt.payload["event"]["status"] != "succeeded":
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

  def test_repeated_receipt(self) -> None:
    """A repeated `seq` is a sequence gap."""
    self.assertEqual(
      self._outcomes([*self.receipts, self.receipts[2]]),
      [(3, v.RECEIPT_SEQUENCE_GAP)],
    )

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


if __name__ == "__main__":
  absltest.main()
