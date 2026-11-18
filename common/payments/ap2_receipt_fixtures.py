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

"""Generate the AP2 Order receipt vectors in fixtures/ap2_order_receipts/.

Regenerate the fixtures, or check that the committed ones are current:

  uv run python -m common.payments.ap2_receipt_fixtures
  uv run python -m common.payments.ap2_receipt_fixtures --check

The output is byte-for-byte reproducible:

* Vectors built on the signed Order example in ucp#1109 copy its three
  receipts verbatim (`_SPEC_CHAIN`); they verify with the example key from
  the PR description (`_SPEC_KEY`). There is no private key for them here.
* Every other receipt is signed with a TEST-ONLY P-256 key derived from a
  fixed label (`_test_only_key`), using deterministic ECDSA (RFC 6979), so
  the same payload always yields the same signature.

The TEST-ONLY keys are public by construction. Never use them for anything
but these vectors.
"""

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from common.payments import ap2_receipt_verifier as v
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

FIXTURE_DIR = (
  Path(__file__).resolve().parent / "fixtures" / "ap2_order_receipts"
)

# Order example in ucp#1109 (docs/specification/payment/extensions/
# ap2-mandates.md, "Placement and Delivery"), at head 1d19291d.
SPEC_SOURCE = (
  "Universal-Commerce-Protocol/ucp#1109 at 1d19291d, ap2-mandates.md, "
  "Order example under 'Placement and Delivery'"
)
# cspell:disable
_SPEC_KEY = {
  "kty": "EC",
  "crv": "P-256",
  "kid": "merchant_2025",
  "use": "sig",
  "alg": "ES256",
  "x": "GzEF_HXZbmVOvExfSerMRYki1vTfR1fETwLcYi8MS50",
  "y": "iDtJQqA9IVKbVvAQ7tL_G73kkG1GoaHJKSty0pWD0b8",
}
_SPEC_CHAIN = (
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjU0MDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTA5VDE0OjUzOjIwWiIsInN0YXR1cyI6InN1Y2NlZWRlZCIsInR5cGUiOiJhdXRob3JpemF0aW9uIn0sImlhdCI6MTc2MjcwMDAwMCwiaXNzIjoiaHR0cHM6Ly9idXNpbmVzcy5leGFtcGxlLmNvbS8ud2VsbC1rbm93bi91Y3AiLCJtYW5kYXRlX3JlZiI6InNoYS0yNTY6emkweERrMTZfR0p3X1FFS0lIYVVia1VGU3RrREo2V2phS2xmMzFzWWZPayIsIm9yZGVyX2lkIjoib3JkZXJfYWJjMTIzIiwicHJldiI6bnVsbCwic2VxIjoxLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.yqOks6JCSk1SGQjykZw6WiNzk0-PC0S3cwMWXxbuH3KGCvTAFFpkT9HuJPS-krPUsWyLhVPNO2Ftz9SuEaoHpQ",
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjMwMDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTEwVDAyOjAwOjAwWiIsInJlZiI6eyJwcm9jZXNzaW5nX2VudHJ5X2lkIjoiZW50XzIifSwic3RhdHVzIjoic3VjY2VlZGVkIiwidHlwZSI6ImNhcHR1cmUifSwiaWF0IjoxNzYyNzQwMDAwLCJpc3MiOiJodHRwczovL2J1c2luZXNzLmV4YW1wbGUuY29tLy53ZWxsLWtub3duL3VjcCIsIm1hbmRhdGVfcmVmIjoic2hhLTI1Njp6aTB4RGsxNl9HSndfUUVLSUhhVWJrVUZTdGtESjZXamFLbGYzMXNZZk9rIiwib3JkZXJfaWQiOiJvcmRlcl9hYmMxMjMiLCJwcmV2Ijoic2hhLTI1Njp6RGU2eEhqc0FtMzhZRkQ0cWVXRHFKaGNNaXNzM2JqSjZCcURRVk1DSnRRIiwic2VxIjoyLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjMwMDAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.ZyT9V_cSIvPgIChIM9cF4xrc5_ROhG7J5E8LxCc95HfMIecZ4zRMfsj5638RrVDrcDybCHUFq3qXWftbRssvYg",
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjI0MDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTEyVDAyOjAwOjAwWiIsInJlZiI6eyJwcm9jZXNzaW5nX2VudHJ5X2lkIjoiZW50XzMifSwic3RhdHVzIjoic3VjY2VlZGVkIiwidHlwZSI6ImNhcHR1cmUifSwiaWF0IjoxNzYyOTEyODAwLCJpc3MiOiJodHRwczovL2J1c2luZXNzLmV4YW1wbGUuY29tLy53ZWxsLWtub3duL3VjcCIsIm1hbmRhdGVfcmVmIjoic2hhLTI1Njp6aTB4RGsxNl9HSndfUUVLSUhhVWJrVUZTdGtESjZXamFLbGYzMXNZZk9rIiwib3JkZXJfaWQiOiJvcmRlcl9hYmMxMjMiLCJwcmV2Ijoic2hhLTI1NjotTC1ZV3lkLVcxNmItR1ExM3l2bXZ4UTFmOThyMTltSzlZc3FEMlF4QkQ4Iiwic2VxIjozLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjU0MDAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.aGDMlvYL7ZjzXiv6HVKJg-sbdNLIOWSr_nNa_-mUrDSjSVcFv56nB8q2FFR0fAU5OGgrkLnwNmxV18b-SK6TPA",
)
# cspell:enable
_SPEC_BUSINESS = "https://business.example.com/.well-known/ucp"
# `mandate_ref` in the spec chain hashes the credential token of the
# complete_checkout example in the same document.
_SPEC_TOKEN = "examplePaymentMethodToken"

_TEST_ONLY_SIGNING = (
  "TEST-ONLY P-256 keys derived from fixed labels in "
  "common/payments/ap2_receipt_fixtures.py; deterministic ECDSA (RFC 6979). "
  "Never use them outside these vectors."
)
_SPEC_SIGNING = (
  "Receipts copied byte-for-byte from the signed Order example in "
  "ucp#1109; key from the PR description (example only)."
)

_MERCHANT = "https://merchant.example/.well-known/ucp"
_MARKETPLACE = "https://marketplace.example/.well-known/ucp"
_SELLER_A = "https://seller-a.example/.well-known/ucp"
_SELLER_B = "https://seller-b.example/.well-known/ucp"
_HOTEL = "https://hotel.example/.well-known/ucp"

# ucp#1109 rule text, quoted without Markdown emphasis.
_RULES = {
  "verify_1": (
    "Platform Verification 1: MUST verify each receipt's signature against "
    "the `keys` of the business named in `iss`, and MUST treat a receipt "
    "whose `iss` is not the business that signed the checkout covered by "
    "`mandate_ref` as `receipt_mandate_unknown`."
  ),
  "verify_2": (
    "Platform Verification 2: MUST evaluate `seq` and `prev` continuity, "
    "`captured` ≤ `mandated` and `refunded` ≤ `captured` per "
    "`(iss, mandate_ref)` chain."
  ),
  "verify_3": (
    "Platform Verification 3: SHOULD, when `event.seller_id` is present, "
    "also check per seller that succeeded captures do not exceed that "
    "seller's attributed total and that refunds do not exceed that seller's "
    "captures."
  ),
  "verify_4": (
    "Platform Verification 4: SHOULD reconcile each `refund` receipt that "
    "carries `event.ref.adjustment_id` against that Order adjustment. A "
    "mismatch is a commercial problem, not a cryptographic one: the receipt "
    "remains valid."
  ),
  "business_3": (
    "Business Requirements 3: MUST NOT capture so that `captured` exceeds "
    "`mandated`. Any increase in what the buyer pays beyond "
    "`mandated` (a tip, an overage, an order change, or a re-authorization "
    "above the original amount) MUST be covered by a new closed Payment "
    "Mandate from the platform. Movements that spend against the new "
    "mandate form their own chain, keyed by its `mandate_ref`."
  ),
  "business_6": (
    "Business Requirements 6: MUST keep retired public keys resolvable by "
    "`kid` for at least the dispute window of the payment method used."
  ),
  "split_shipments": (
    "Edge Cases, Split shipments: multiple `capture` receipts against the "
    "same `mandate_ref`, with `captured` accumulating."
  ),
  "marketplace": (
    "Edge Cases, Marketplace as merchant of record: the marketplace issues "
    "every receipt and sets `event.seller_id`, so a duplicate capture for "
    "one seller is detectable even when the chain's totals pass."
  ),
  "several_mor": (
    "Edge Cases, Several merchants of record in one Order: each has its own "
    "mandate and keys, so each `(iss, mandate_ref)` pair is an independent "
    "chain."
  ),
  "partial_refund": (
    "Edge Cases, Partial refunds under a cancellation policy: the refund "
    "receipt proves the money movement and cites the cancellation "
    "adjustment through `event.ref.adjustment_id`; whether the amount was "
    "commercially correct is checked against that adjustment, not inside "
    "the receipt."
  ),
  "pending": (
    "Edge Cases, Pending movements: a movement may first be receipted as "
    "`pending`. A later receipt in the same chain for the same movement "
    "carries `status: succeeded`. Totals change only on the `succeeded` "
    "receipt."
  ),
  "late_network_ref": (
    "Edge Cases, Late network references: when a network reference (for "
    "example the Acquirer Reference Number of a refund) is assigned after "
    "the movement is receipted, it appears on a follow-up receipt for the "
    "same `event.ref`. `network_ref` is optional on every status: a refund "
    "processed as a reversal of the original authorization may never have "
    "one."
  ),
}


def _test_only_key(label: str) -> ec.EllipticCurvePrivateKey:
  """Derive a TEST-ONLY P-256 private key from a fixed label."""
  seed = hashlib.sha256(
    b"UCP conformance TEST-ONLY AP2 receipt key: " + label.encode("utf-8")
  ).digest()
  order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
  return ec.derive_private_key(
    int.from_bytes(seed, "big") % (order - 1) + 1, ec.SECP256R1()
  )


class FixtureSigner:
  """Signs receipts with a business's TEST-ONLY key."""

  def __init__(self, iss: str, kid: str) -> None:
    """Derive the key for one `kid` of the business `iss`."""
    self.kid = kid
    self._key = _test_only_key(f"{iss} {kid}")

  @property
  def jwk(self) -> dict[str, str]:
    """The public key, as published in the business's `keys`."""
    numbers = self._key.public_key().public_numbers()
    return {
      "kty": "EC",
      "crv": "P-256",
      "kid": self.kid,
      "use": "sig",
      "alg": "ES256",
      "x": v.b64url_encode(numbers.x.to_bytes(32, "big")),
      "y": v.b64url_encode(numbers.y.to_bytes(32, "big")),
    }

  def sign(self, payload: dict[str, Any]) -> str:
    """Return the compact JWS receipt for a payload (Receipt Format)."""
    header = {"alg": "ES256", "kid": self.kid, "typ": v.RECEIPT_TYP}
    signing_input = (
      v.b64url_encode(_jcs(header)) + "." + v.b64url_encode(_jcs(payload))
    )
    der = self._key.sign(
      signing_input.encode("ascii"),
      ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    )
    r, s = utils.decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + v.b64url_encode(signature)


def _jcs(value: Any) -> bytes:
  """Canonicalize JSON per RFC 8785 (JCS).

  For the values used here (objects with ASCII member names, strings,
  integers, booleans and null) sorted members and compact separators are
  exactly RFC 8785. Anything else is refused rather than encoded wrongly.
  """

  def check(item: Any) -> None:
    if isinstance(item, dict):
      for key, value in item.items():
        if not key.isascii():
          raise ValueError(f"non-ASCII member name {key!r}")
        check(value)
    elif isinstance(item, list):
      for value in item:
        check(value)
    elif isinstance(item, float):
      raise ValueError("floats are not used in receipts")

  check(value)
  return json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
  ).encode("utf-8")


def _timestamp(value: str) -> int:
  """Seconds since the epoch for an RFC 3339 UTC time ending in `Z`."""
  # fromisoformat accepts a trailing `Z` only from Python 3.11.
  parsed = datetime.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
  return int(parsed.timestamp())


class _Chain:
  """Issues the receipts of one `(iss, mandate_ref)` chain in order.

  Running totals follow from the events: only succeeded events change
  them, `authorization` and `release` move `authorized`, and `captured` is
  net of capture reversals.
  """

  def __init__(
    self,
    signer: FixtureSigner,
    iss: str,
    token: str,
    mandated: int,
    order_id: str,
    checkout_id: str,
    currency: str,
  ) -> None:
    self.signer = signer
    self.iss = iss
    self.token = token
    self.mandate_ref = v.sha256_ref(token)
    self.order_id = order_id
    self.checkout_id = checkout_id
    self.currency = currency
    self.totals = {
      "mandated": mandated,
      "authorized": 0,
      "captured": 0,
      "refunded": 0,
    }
    self.seq = 0
    self.prev: str | None = None

  def issue(
    self,
    event_type: str,
    amount: int,
    occurred_at: str,
    status: str = "succeeded",
    signer: FixtureSigner | None = None,
    **extra: Any,
  ) -> str:
    """Sign the next receipt; `extra` adds optional `event` members."""
    if status == "succeeded":
      key = {
        "authorization": ("authorized", 1),
        "release": ("authorized", -1),
        "capture": ("captured", 1),
        "capture_reversal": ("captured", -1),
        "refund": ("refunded", 1),
      }[event_type]
      self.totals[key[0]] += key[1] * amount
    self.seq += 1
    payload = {
      "iss": self.iss,
      "iat": _timestamp(occurred_at) + 60,
      "order_id": self.order_id,
      "checkout_id": self.checkout_id,
      "mandate_ref": self.mandate_ref,
      "seq": self.seq,
      "prev": self.prev,
      "event": {
        "type": event_type,
        "status": status,
        "amount": amount,
        "currency": self.currency,
        "occurred_at": occurred_at,
        **extra,
      },
      "totals": dict(self.totals),
    }
    compact = (signer or self.signer).sign(payload)
    self.prev = v.sha256_ref(compact)
    return compact


def _line(line_id: str, amount: int, seller_id: str | None = None) -> dict:
  line: dict[str, Any] = {"id": line_id}
  if seller_id is not None:
    line["seller_id"] = seller_id
  line["totals"] = [
    {"type": "subtotal", "amount": amount},
    {"type": "total", "amount": amount},
  ]
  return line


def _order(
  order_id: str,
  checkout_id: str,
  currency: str,
  line_items: list[dict],
  totals: list[dict],
  receipts: list[str],
  adjustments: list[dict] | None = None,
  **extra: Any,
) -> dict[str, Any]:
  """Build the Order members the verifier reads, receipts in `ap2`."""
  return {
    "id": order_id,
    "checkout_id": checkout_id,
    "currency": currency,
    **extra,
    "line_items": line_items,
    "adjustments": adjustments or [],
    "totals": totals,
    "ap2": {"receipts": receipts},
  }


def _usd_totals(subtotal: int, tax: int, *extra: dict) -> list[dict]:
  return [
    {"type": "subtotal", "amount": subtotal},
    {"type": "tax", "amount": tax},
    *extra,
    {
      "type": "total",
      "amount": subtotal + tax + sum(e["amount"] for e in extra),
    },
  ]


def _fixture(
  vector_id: str,
  title: str,
  test_plan: str | None,
  rules: list[str],
  signing: str,
  issuers: dict[str, list[dict]],
  mandates: list[tuple[str, str]],
  order: dict[str, Any],
  outcomes: list[tuple[int, str]] = (),
  warnings: list[tuple[int, str]] = (),
  reconciliation: list[tuple[int, str]] = (),
  notes: str | None = None,
) -> dict[str, Any]:
  """Assemble one fixture document."""
  fixture: dict[str, Any] = {
    "id": vector_id,
    "title": title,
    "test_plan_vector": test_plan,
    "spec": [_RULES[r] for r in rules],
    "signing": signing,
  }
  if notes:
    fixture["notes"] = notes
  fixture["inputs"] = {
    "issuers": {iss: {"keys": keys} for iss, keys in issuers.items()},
    "mandates": [
      {"credential_token": token, "business": business}
      for token, business in mandates
    ],
    "order": order,
  }
  decoded = []
  for compact in order["ap2"]["receipts"]:
    header, payload = v.decode_receipt(compact)
    decoded.append({"header": header, "payload": payload})
  fixture["decoded_receipts"] = decoded

  def findings(items):
    return [{"receipt_index": i, "code": code} for i, code in items]

  fixture["expected"] = {
    "outcomes": findings(outcomes),
    "warnings": findings(warnings),
    "reconciliation": findings(reconciliation),
  }
  return fixture


def _tamper_amount(compact: str, amount: int) -> str:
  """Re-encode a receipt's payload with a new amount, keeping the rest."""
  header_b64, _, signature_b64 = compact.split(".")
  _, payload = v.decode_receipt(compact)
  payload["event"]["amount"] = amount
  return ".".join([header_b64, v.b64url_encode(_jcs(payload)), signature_b64])


def _simple_chain(
  vector: str, signer: FixtureSigner | None = None, currency: str = "USD"
) -> _Chain:
  return _Chain(
    signer or FixtureSigner(_MERCHANT, "test_only_2026"),
    _MERCHANT,
    f"test_only_payment_mandate_{vector}",
    5400,
    f"order_{vector}",
    f"chk_{vector}",
    currency,
  )


def _merchant_vector(vector: str, chain: _Chain, **kwargs: Any) -> dict:
  """Fixture for a single-merchant USD 54.00 order."""
  receipts = kwargs.pop("receipts")
  order = _order(
    chain.order_id,
    chain.checkout_id,
    "USD",
    kwargs.pop("line_items", [_line("li_1", 5000)]),
    kwargs.pop("totals", _usd_totals(5000, 400)),
    receipts,
    kwargs.pop("adjustments", None),
  )
  return _fixture(
    vector,
    issuers=kwargs.pop("issuers", {_MERCHANT: [chain.signer.jwk]}),
    mandates=kwargs.pop("mandates", [(chain.token, chain.iss)]),
    order=order,
    signing=_TEST_ONLY_SIGNING,
    **kwargs,
  )


def _spec_order(receipts: list[str]) -> dict[str, Any]:
  """Rebuild the ucp#1109 Order example, reduced to what is verified."""
  return _order(
    "order_abc123",
    "chk_abc123",
    "USD",
    [_line("li_shoes", 2800), _line("li_jacket", 2200)],
    _usd_totals(5000, 400),
    receipts,
  )


def _v01() -> dict:
  chain = _simple_chain("v01")
  receipts = [
    chain.issue("authorization", 5400, "2026-11-09T14:53:20Z"),
    chain.issue("capture", 5400, "2026-11-10T02:00:00Z"),
  ]
  return _merchant_vector(
    "v01_single_capture",
    chain,
    title="A single capture equal to the mandate verifies",
    test_plan="1",
    rules=["verify_1", "verify_2"],
    receipts=receipts,
    notes=(
      "The ucp#1109 chain has two split captures, so it is vector 2; a "
      "single capture of the full mandate needs its own signed receipt."
    ),
  )


def _v02() -> dict:
  return _fixture(
    "v02_split_captures",
    "Two split captures summing to the mandate verify",
    "2",
    ["verify_1", "verify_2", "split_shipments"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS)],
    _spec_order(list(_SPEC_CHAIN)),
  )


def _v03() -> dict:
  chain = _simple_chain("v03")
  receipts = [
    chain.issue("authorization", 5400, "2026-11-09T14:53:20Z"),
    chain.issue("capture", 6000, "2026-11-10T02:00:00Z"),
  ]
  return _merchant_vector(
    "v03_capture_exceeds_mandate",
    chain,
    title="A capture above the mandate, without a new mandate",
    test_plan="3",
    rules=["verify_2", "business_3"],
    receipts=receipts,
    outcomes=[(1, v.RECEIPT_EXCEEDS_MANDATE)],
  )


def _v04() -> dict:
  signer = FixtureSigner(_MERCHANT, "test_only_2026")
  original = _Chain(
    signer,
    _MERCHANT,
    "test_only_payment_mandate_v04_original",
    5400,
    "order_v04",
    "chk_v04",
    "USD",
  )
  increase = _Chain(
    signer,
    _MERCHANT,
    "test_only_payment_mandate_v04_increase",
    600,
    "order_v04",
    "chk_v04",
    "USD",
  )
  receipts = [
    original.issue("authorization", 5400, "2026-11-09T14:53:20Z"),
    original.issue("capture", 5400, "2026-11-10T02:00:00Z"),
    increase.issue("authorization", 600, "2026-11-10T03:00:00Z"),
    increase.issue("capture", 600, "2026-11-10T03:05:00Z"),
  ]
  return _merchant_vector(
    "v04_increase_second_mandate",
    original,
    title="An increase covered by a second mandate verifies",
    test_plan="4",
    rules=["verify_2", "business_3"],
    receipts=receipts,
    totals=_usd_totals(
      5000, 400, {"type": "fee", "amount": 600, "display_text": "Tip"}
    ),
    mandates=[
      ("test_only_payment_mandate_v04_original", _MERCHANT),
      ("test_only_payment_mandate_v04_increase", _MERCHANT),
    ],
    notes=(
      "The USD 6.00 tip is covered by a second closed Payment Mandate, "
      "whose movements form their own chain starting at seq 1."
    ),
  )


def _v05() -> dict:
  receipts = [*_SPEC_CHAIN[:2], _tamper_amount(_SPEC_CHAIN[2], 4400)]
  return _fixture(
    "v05_tampered_amount",
    "A tampered amount gives receipt_invalid_signature",
    "5",
    ["verify_1"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS)],
    _spec_order(receipts),
    outcomes=[(2, v.RECEIPT_INVALID_SIGNATURE)],
    notes=(
      "The ucp#1109 chain with event.amount of the third receipt changed "
      "from 2400 to 4400 after signing; header and signature are unchanged."
    ),
  )


def _v06() -> dict:
  return _fixture(
    "v06_missing_seq",
    "A missing seq gives receipt_sequence_gap",
    "6",
    ["verify_2"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS)],
    _spec_order([_SPEC_CHAIN[0], _SPEC_CHAIN[2]]),
    outcomes=[(1, v.RECEIPT_SEQUENCE_GAP)],
    notes="The ucp#1109 chain with its seq 2 receipt removed.",
  )


def _v07() -> dict:
  chain = _simple_chain("v07")
  receipts = [
    chain.issue("authorization", 5400, "2026-11-09T14:53:20Z"),
    chain.issue("capture", 5400, "2026-11-10T02:00:00Z"),
    chain.issue("refund", 6000, "2026-11-12T10:00:00Z"),
  ]
  return _merchant_vector(
    "v07_refund_exceeds_capture",
    chain,
    title="A refund above captured gives receipt_refund_exceeds_capture",
    test_plan="7",
    rules=["verify_2"],
    receipts=receipts,
    outcomes=[(2, v.RECEIPT_REFUND_EXCEEDS_CAPTURE)],
  )


def _v08_receipts() -> tuple[_Chain, FixtureSigner, list[str]]:
  retired = FixtureSigner(_MERCHANT, "test_only_2025")
  current = FixtureSigner(_MERCHANT, "test_only_2026")
  chain = _simple_chain("v08", signer=current)
  receipts = [
    chain.issue("authorization", 5400, "2026-11-09T14:53:20Z", signer=retired),
    chain.issue("capture", 5400, "2026-11-10T02:00:00Z"),
  ]
  return chain, retired, receipts


def _v08() -> dict:
  chain, retired, receipts = _v08_receipts()
  return _merchant_vector(
    "v08_rotated_retained_key",
    chain,
    title="A receipt signed with a rotated, retained key verifies",
    test_plan="8",
    rules=["verify_1", "business_6"],
    receipts=receipts,
    issuers={_MERCHANT: [retired.jwk, chain.signer.jwk]},
    notes=(
      "seq 1 is signed with test_only_2025, which was retired before seq 2 "
      "but is still published in keys."
    ),
  )


def _v08_not_retained() -> dict:
  chain, _, receipts = _v08_receipts()
  return _merchant_vector(
    "v08_key_not_retained",
    chain,
    title="A receipt whose retired key is no longer published",
    test_plan=None,
    rules=["verify_1", "verify_2", "business_6"],
    receipts=receipts,
    issuers={_MERCHANT: [chain.signer.jwk]},
    outcomes=[(0, v.RECEIPT_INVALID_SIGNATURE), (1, v.RECEIPT_SEQUENCE_GAP)],
    notes=(
      "Negative counterpart of v08_rotated_retained_key: test_only_2025 is "
      "gone from keys, so seq 1 cannot be verified. It is then not part of "
      "the chain, and seq 2 is reported as a gap."
    ),
  )


def _marketplace_vector(vector: str, steps: list[tuple], **kwargs) -> dict:
  signer = FixtureSigner(_MARKETPLACE, "test_only_2026")
  chain = _Chain(
    signer,
    _MARKETPLACE,
    f"test_only_payment_mandate_{vector}",
    5400,
    f"order_{vector}",
    f"chk_{vector}",
    "USD",
  )
  receipts = [chain.issue(*step[:3], **step[3]) for step in steps]
  order = _order(
    chain.order_id,
    chain.checkout_id,
    "USD",
    [_line("li_a", 2000, "seller_a"), _line("li_b", 3400, "seller_b")],
    [
      {"type": "subtotal", "amount": 5400},
      {"type": "total", "amount": 5400},
    ],
    receipts,
    sellers={
      "seller_a": {"name": "Seller A"},
      "seller_b": {"name": "Seller B"},
    },
  )
  return _fixture(
    vector,
    issuers={_MARKETPLACE: [signer.jwk]},
    mandates=[(f"test_only_payment_mandate_{vector}", _MARKETPLACE)],
    order=order,
    signing=_TEST_ONLY_SIGNING,
    **kwargs,
  )


def _v09() -> dict:
  return _marketplace_vector(
    "v09_two_sellers",
    [
      ("authorization", 5400, "2026-11-09T14:53:20Z", {}),
      ("capture", 2000, "2026-11-10T18:04:00Z", {"seller_id": "seller_a"}),
      ("capture", 3400, "2026-11-11T18:04:00Z", {"seller_id": "seller_b"}),
    ],
    title="Marketplace as merchant of record, two sellers: captures verify",
    test_plan="9",
    rules=["verify_2", "verify_3", "marketplace"],
  )


def _v09_duplicate() -> dict:
  return _marketplace_vector(
    "v09_duplicate_seller_capture",
    [
      ("authorization", 5400, "2026-11-09T14:53:20Z", {}),
      ("capture", 2000, "2026-11-10T18:04:00Z", {"seller_id": "seller_a"}),
      ("capture", 2000, "2026-11-11T18:04:00Z", {"seller_id": "seller_a"}),
    ],
    title="A duplicate capture for one seller gives a seller-total warning",
    test_plan="9",
    rules=["verify_2", "verify_3", "marketplace"],
    warnings=[(2, v.RECEIPT_EXCEEDS_SELLER_TOTAL)],
    notes=(
      "Chain totals pass (captured 4000 <= mandated 5400), but seller_a's "
      "captures (4000) exceed its attributed total (2000). The per-seller "
      "check is a SHOULD, so this is a warning, not a failed outcome."
    ),
  )


def _v09_seller_refund() -> dict:
  return _marketplace_vector(
    "v09_seller_refund_exceeds_captures",
    [
      ("authorization", 5400, "2026-11-09T14:53:20Z", {}),
      ("capture", 2000, "2026-11-10T18:04:00Z", {"seller_id": "seller_a"}),
      ("refund", 1000, "2026-11-12T09:00:00Z", {"seller_id": "seller_b"}),
    ],
    title="A refund above one seller's captures gives a seller-total warning",
    test_plan=None,
    rules=["verify_2", "verify_3"],
    warnings=[(2, v.RECEIPT_EXCEEDS_SELLER_TOTAL)],
    notes=(
      "Chain totals pass (refunded 1000 <= captured 2000), but seller_b "
      "has no captures. Second half of the per-seller SHOULD."
    ),
  )


def _two_mor_vector(vector: str, with_mismatch: bool, **kwargs) -> dict:
  signer_a = FixtureSigner(_SELLER_A, "test_only_2026")
  signer_b = FixtureSigner(_SELLER_B, "test_only_2026")
  token_a = f"test_only_payment_mandate_{vector}_seller_a"
  token_b = f"test_only_payment_mandate_{vector}_seller_b"
  order_id, checkout_id = f"order_{vector}", f"chk_{vector}"
  chain_a = _Chain(
    signer_a, _SELLER_A, token_a, 2000, order_id, checkout_id, "USD"
  )
  chain_b = _Chain(
    signer_b, _SELLER_B, token_b, 3400, order_id, checkout_id, "USD"
  )
  receipts = [
    chain_a.issue("authorization", 2000, "2026-11-09T14:53:20Z"),
    chain_a.issue(
      "capture", 2000, "2026-11-10T18:04:00Z", seller_id="seller_a"
    ),
    chain_b.issue("authorization", 3400, "2026-11-09T14:53:20Z"),
    chain_b.issue(
      "capture", 3400, "2026-11-10T18:04:00Z", seller_id="seller_b"
    ),
  ]
  if with_mismatch:
    # Spends chain A's mandate under seller-b's iss, signed with seller-b's
    # own key: the signature verifies, the mandate binding does not.
    rogue = _Chain(
      signer_b, _SELLER_B, token_a, 2000, order_id, checkout_id, "USD"
    )
    rogue.totals["authorized"] = 2000
    receipts.append(rogue.issue("capture", 2000, "2026-11-10T18:04:00Z"))
  order = _order(
    order_id,
    checkout_id,
    "USD",
    [_line("li_a", 2000, "seller_a"), _line("li_b", 3400, "seller_b")],
    [
      {"type": "subtotal", "amount": 5400},
      {"type": "total", "amount": 5400},
    ],
    receipts,
    sellers={
      "seller_a": {"name": "Seller A", "profile": _SELLER_A},
      "seller_b": {"name": "Seller B", "profile": _SELLER_B},
    },
  )
  return _fixture(
    vector,
    issuers={_SELLER_A: [signer_a.jwk], _SELLER_B: [signer_b.jwk]},
    mandates=[(token_a, _SELLER_A), (token_b, _SELLER_B)],
    order=order,
    signing=_TEST_ONLY_SIGNING,
    **kwargs,
  )


def _v10() -> dict:
  return _two_mor_vector(
    "v10_two_merchants_of_record",
    with_mismatch=False,
    title="Two merchants of record in one order: both chains verify",
    test_plan="10",
    rules=["verify_1", "verify_2", "several_mor"],
  )


def _v10_mismatch() -> dict:
  return _two_mor_vector(
    "v10_iss_mismatch",
    with_mismatch=True,
    title="A receipt whose iss does not match its mandate",
    test_plan="10",
    rules=["verify_1", "several_mor"],
    outcomes=[(4, v.RECEIPT_MANDATE_UNKNOWN)],
    notes=(
      "Receipt 4 carries seller A's mandate_ref with iss seller-b and is "
      "signed with seller-b's key. Both valid chains still verify."
    ),
  )


def _lodging_vector(vector: str, refund: int, **kwargs) -> dict:
  signer = FixtureSigner(_HOTEL, "test_only_2026")
  token = f"test_only_payment_mandate_{vector}"
  chain = _Chain(
    signer, _HOTEL, token, 60000, f"order_{vector}", f"chk_{vector}", "EUR"
  )
  receipts = [
    chain.issue("authorization", 60000, "2026-11-01T09:00:00Z"),
    chain.issue("capture", 60000, "2026-11-01T09:00:30Z"),
    chain.issue(
      "refund",
      refund,
      "2026-11-14T16:00:00Z",
      ref={"adjustment_id": "adj_cancel_1"},
    ),
  ]
  adjustment = {
    "id": "adj_cancel_1",
    "type": "cancellation",
    "occurred_at": "2026-11-14T15:30:00Z",
    "status": "completed",
    "totals": [{"type": "total", "amount": -45000}],
    "description": (
      "Cancelled under the booking's cancellation policy: EUR 150.00 "
      "penalty retained, EUR 450.00 refundable."
    ),
  }
  order = _order(
    chain.order_id,
    chain.checkout_id,
    "EUR",
    [_line("li_room", 60000)],
    [
      {"type": "subtotal", "amount": 60000},
      {"type": "total", "amount": 60000},
    ],
    receipts,
    adjustments=[adjustment],
  )
  return _fixture(
    vector,
    issuers={_HOTEL: [signer.jwk]},
    mandates=[(token, _HOTEL)],
    order=order,
    signing=_TEST_ONLY_SIGNING,
    **kwargs,
  )


def _v11() -> dict:
  return _lodging_vector(
    "v11_lodging_partial_refund",
    45000,
    title="Lodging partial refund matching the cancellation adjustment",
    test_plan="11",
    rules=["verify_2", "verify_4", "partial_refund"],
  )


def _v12() -> dict:
  return _lodging_vector(
    "v12_lodging_refund_mismatch",
    30000,
    title="Lodging refund that disagrees with the cancellation adjustment",
    test_plan="12",
    rules=["verify_2", "verify_4", "partial_refund"],
    reconciliation=[(2, v.RECONCILIATION_ADJUSTMENT_MISMATCH)],
    notes=(
      "The receipt chain verifies; the refund (EUR 300.00) disagrees with "
      "the adjustment it cites (EUR 450.00). Reported as a reconciliation "
      "finding, separately from the receipt outcomes."
    ),
  )


def _pending_refund_vector(vector: str, settled: str, **kwargs) -> dict:
  """Refund receipted pending, then succeeded, for the same event.ref."""
  network_ref = kwargs.pop("network_ref", None)
  chain = _simple_chain(vector)
  ref = {"ref": {"adjustment_id": "adj_refund_1"}}
  receipts = [
    chain.issue("authorization", 5400, "2026-11-09T14:53:20Z"),
    chain.issue("capture", 5400, "2026-11-10T02:00:00Z"),
    chain.issue("refund", 5400, kwargs.pop("requested"), "pending", **ref),
    chain.issue(
      "refund",
      5400,
      settled,
      **ref,
      **({"network_ref": network_ref} if network_ref else {}),
    ),
  ]
  adjustment = {
    "id": "adj_refund_1",
    "type": "refund",
    "occurred_at": "2026-11-10T04:00:00Z",
    "status": "completed",
    "totals": [{"type": "total", "amount": -5400}],
  }
  return _merchant_vector(
    vector,
    chain,
    receipts=receipts,
    adjustments=[adjustment],
    **kwargs,
  )


def _refund_delayed_network_ref() -> dict:
  return _pending_refund_vector(
    "refund_delayed_network_ref",
    "2026-11-17T10:00:00Z",
    requested="2026-11-10T04:00:00Z",
    network_ref="74000000000000000001063",
    title="Refund receipted pending, then succeeded with a late network_ref",
    test_plan=None,
    rules=["verify_2", "pending", "late_network_ref"],
    notes=(
      "seq 3 is pending and leaves totals unchanged; seq 4, for the same "
      "event.ref, is succeeded, carries network_ref (an ARN assigned days "
      "later) and is the only receipt that changes refunded."
    ),
  )


def _refund_reversal() -> dict:
  return _pending_refund_vector(
    "refund_reversal_no_network_ref",
    "2026-11-10T04:02:00Z",
    requested="2026-11-10T04:00:00Z",
    title="Refund processed as a reversal: succeeded without network_ref",
    test_plan=None,
    rules=["verify_2", "pending", "late_network_ref"],
    notes=(
      "The refund settles as a reversal of the authorization shortly after "
      "capture, so no network reference is ever assigned: the succeeded "
      "receipt for the same event.ref has no network_ref, and is valid."
    ),
  )


_BUILDERS = (
  _v01,
  _v02,
  _v03,
  _v04,
  _v05,
  _v06,
  _v07,
  _v08,
  _v08_not_retained,
  _v09,
  _v09_duplicate,
  _v09_seller_refund,
  _v10,
  _v10_mismatch,
  _v11,
  _v12,
  _refund_delayed_network_ref,
  _refund_reversal,
)


def build_fixtures() -> dict[str, str]:
  """Return every fixture file name with its exact content."""
  files = {}
  for build in _BUILDERS:
    fixture = build()
    text = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
    files[f"{fixture['id']}.json"] = text
  return files


def main(argv: list[str] | None = None) -> int:
  """Write the fixtures, or with --check report whether they are current."""
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
    "--check",
    action="store_true",
    help="Exit non-zero if a committed fixture differs from the output.",
  )
  args = parser.parse_args(argv)

  files = build_fixtures()
  stale = sorted(
    name
    for name, text in files.items()
    if not (FIXTURE_DIR / name).is_file()
    or (FIXTURE_DIR / name).read_text(encoding="utf-8") != text
  )
  extra = sorted(
    p.name for p in FIXTURE_DIR.glob("*.json") if p.name not in files
  )
  if args.check:
    for name in stale:
      sys.stderr.write(f"stale: {name}\n")
    for name in extra:
      sys.stderr.write(f"not generated: {name}\n")
    return 1 if stale or extra else 0

  FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
  for name in stale:
    (FIXTURE_DIR / name).write_text(files[name], encoding="utf-8")
    sys.stdout.write(f"wrote {name}\n")
  for name in extra:
    sys.stderr.write(f"not generated, left in place: {name}\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
