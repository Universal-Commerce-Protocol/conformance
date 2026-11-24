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
import copy
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
# ap2-mandates.md, "Placement and Delivery"), at head 0110eac.
SPEC_SOURCE = (
  "Universal-Commerce-Protocol/ucp#1109 at 0110eac, ap2-mandates.md, "
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
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjU0MDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTA5VDE0OjUzOjIwWiIsInN0YXR1cyI6InN1Y2NlZWRlZCIsInR5cGUiOiJhdXRob3JpemF0aW9uIn0sImlhdCI6MTc5NDIzNjAwMCwiaXNzIjoiaHR0cHM6Ly9idXNpbmVzcy5leGFtcGxlLmNvbS8ud2VsbC1rbm93bi91Y3AiLCJtYW5kYXRlX3JlZiI6InNoYS0yNTY6emkweERrMTZfR0p3X1FFS0lIYVVia1VGU3RrREo2V2phS2xmMzFzWWZPayIsIm9yZGVyX2lkIjoib3JkZXJfYWJjMTIzIiwicHJldiI6bnVsbCwic2VxIjoxLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.B1qq-ooaU-iv_gU4sxGg6OodmLq3kHhMyMnmI-rQTr9aziOACY7l1Vbkws7lSTm09IpVugB_QksMQiXUi6gvVQ",
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjMwMDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTEwVDAyOjAwOjAwWiIsInJlZiI6eyJwcm9jZXNzaW5nX2VudHJ5X2lkIjoiZW50XzIifSwic3RhdHVzIjoic3VjY2VlZGVkIiwidHlwZSI6ImNhcHR1cmUifSwiaWF0IjoxNzk0Mjc2MDAwLCJpc3MiOiJodHRwczovL2J1c2luZXNzLmV4YW1wbGUuY29tLy53ZWxsLWtub3duL3VjcCIsIm1hbmRhdGVfcmVmIjoic2hhLTI1Njp6aTB4RGsxNl9HSndfUUVLSUhhVWJrVUZTdGtESjZXamFLbGYzMXNZZk9rIiwib3JkZXJfaWQiOiJvcmRlcl9hYmMxMjMiLCJwcmV2Ijoic2hhLTI1NjptYkhyY29KVWZCUmxFQS1iQWdFTHZZRUh3bTlCOFctRHNoSzR5dURIWGdnIiwic2VxIjoyLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjMwMDAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.kffxBLFlPYyfhAD_qXCpG2KIPDtmOfyRwBS9AlHUsHcmZlrFJ5YHBqPdT2fwLAzwdhFv2InssCj344UuHag1Wg",
  "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50XzIwMjUiLCJ0eXAiOiJ1Y3AtYXAyLXJlY2VpcHQrand0In0.eyJjaGVja291dF9pZCI6ImNoa19hYmMxMjMiLCJldmVudCI6eyJhbW91bnQiOjI0MDAsImN1cnJlbmN5IjoiVVNEIiwib2NjdXJyZWRfYXQiOiIyMDI2LTExLTEyVDAyOjAwOjAwWiIsInJlZiI6eyJwcm9jZXNzaW5nX2VudHJ5X2lkIjoiZW50XzMifSwic3RhdHVzIjoic3VjY2VlZGVkIiwidHlwZSI6ImNhcHR1cmUifSwiaWF0IjoxNzk0NDQ4ODAwLCJpc3MiOiJodHRwczovL2J1c2luZXNzLmV4YW1wbGUuY29tLy53ZWxsLWtub3duL3VjcCIsIm1hbmRhdGVfcmVmIjoic2hhLTI1Njp6aTB4RGsxNl9HSndfUUVLSUhhVWJrVUZTdGtESjZXamFLbGYzMXNZZk9rIiwib3JkZXJfaWQiOiJvcmRlcl9hYmMxMjMiLCJwcmV2Ijoic2hhLTI1NjotNG9URC1FS1lfQ3lwTlM1TzhXRWhYRE5ZNkVGQkRwbDR3aUxXem9EMnFnIiwic2VxIjozLCJ0b3RhbHMiOnsiYXV0aG9yaXplZCI6NTQwMCwiY2FwdHVyZWQiOjU0MDAsIm1hbmRhdGVkIjo1NDAwLCJyZWZ1bmRlZCI6MH19.J-C66O6O4XjAD_TvtvS8endj_FfRIe_u3SysJ4U2NwX0zqQ0MlAy6FeGJZuSPUuCnY8twK9-9TjQdMT7ylL7Yw",
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

# ucp#1109 rule text at 0110eac (docs; schema 89600e5), quoted without
# Markdown emphasis.
_RULES = {
  "verify_1": (
    "Platform Verification 1: MUST verify each receipt's signature against "
    "the `keys` of the business named in `iss`. A receipt whose `typ` is "
    "not `ucp-ap2-receipt+jwt`, or whose `kid` does not resolve in the "
    "`keys` of `iss`, is `receipt_invalid_signature`."
  ),
  "verify_2": (
    "Platform Verification 2: MUST, only for receipts whose signature "
    "verified, check that `mandate_ref` is a Payment Mandate the platform "
    "submitted and that `iss` is the business that signed the checkout it "
    "covers. A receipt that fails is `receipt_mandate_unknown`."
  ),
  "verify_3": (
    "Platform Verification 3: MUST exclude a receipt that fails rule 1 or 2 "
    "from its chain, so the next receipt in that chain is reported as "
    "`receipt_sequence_gap`."
  ),
  "verify_4": (
    "Platform Verification 4: MUST evaluate `seq` and `prev` continuity per "
    "`(iss, mandate_ref)` chain. A byte-identical redelivery of a receipt "
    "the platform already holds is ignored. A second, different receipt "
    "for a `seq` the platform already holds is `receipt_sequence_gap`; the "
    "platform keeps both. A receipt whose `prev` is the hash of a receipt "
    "reported as `receipt_sequence_gap` continues the chain from that "
    "receipt and is not itself a gap."
  ),
  "verify_5": (
    "Platform Verification 5: MUST recompute `authorized`, `captured` and "
    "`refunded` for each chain from the `succeeded` events it holds, per "
    "Running Totals, counting each movement once and never counting a "
    "second receipt kept under rule 4, and MUST check that "
    "`totals.mandated` equals the `payment_amount` of the Payment Mandate "
    "that `mandate_ref` covers. A "
    "receipt whose stated `totals` differ from these values is "
    "`receipt_totals_mismatch`. Recomputation stops at the first "
    "`receipt_sequence_gap` or excluded receipt in a chain: the platform "
    "does not report `receipt_totals_mismatch` for later receipts in that "
    "chain, because the events it holds are incomplete."
  ),
  "verify_6": (
    "Platform Verification 6: MUST check that `authorized` and `captured` "
    "do not exceed `mandated` (`receipt_exceeds_mandate`), that "
    "`captured` does not exceed `authorized` "
    "(`receipt_capture_exceeds_authorization`), and that `refunded` does "
    "not exceed `captured` (`receipt_refund_exceeds_capture`). The check "
    "uses the recomputed totals, or the stated totals for the receipt at "
    "which recomputation stopped and every receipt after it. An outcome is "
    "reported on every receipt for which the totals are over, not only the "
    "first. These outcomes are independent of `receipt_totals_mismatch`: a "
    "receipt can report both."
  ),
  "verify_7": (
    "Platform Verification 7: SHOULD, when `event.seller_id` is present, "
    "also check per seller that succeeded captures do not exceed that "
    "seller's attributed total and that refunds do not exceed that seller's "
    "captures."
  ),
  "verify_8": (
    "Platform Verification 8: SHOULD reconcile refunds against Order "
    "adjustments. For each `event.ref.adjustment_id` cited, the platform "
    "sums `event.amount` over the `succeeded` `refund` receipts that cite "
    "it, minus `succeeded` `refund_reversal` receipts that cite it, "
    "counting each movement once per Running Totals, and compares the "
    "result with the magnitude of that adjustment's `total` entry. "
    "`pending` and `failed` receipts are not counted. A cited "
    "`adjustment_id` that matches no adjustment is also "
    "`reconciliation_adjustment_mismatch`. The outcome is reported on the "
    "last `succeeded` receipt that cites the adjustment. A mismatch is a "
    "commercial problem, not a cryptographic one: the receipts remain "
    "valid."
  ),
  "running_totals": (
    "Running Totals: `totals.mandated` is the `payment_amount` of the "
    "Payment Mandate that `mandate_ref` covers. The other totals start at 0 "
    "and change only on `succeeded` events: `authorization` and "
    "`authorization_adjustment` add to `authorized`, `release` subtracts "
    "from it, `capture` adds to `captured`, `capture_reversal` subtracts "
    "from it, `refund` adds to `refunded`, `refund_reversal` subtracts "
    "from it. A capture does not reduce `authorized`."
  ),
  "each_once": (
    "Running Totals: each movement counts once. A later `succeeded` "
    "receipt with the same `type` and `event.ref.processing_entry_id` as an "
    "earlier `succeeded` receipt in the chain is a follow-up (for example, "
    "one that only adds `network_ref`) and does not change the totals "
    "again. A second, different receipt for a `seq` already held (see "
    "Platform Verification rule 4) is never counted."
  ),
  "no_jcs": (
    "Hashes and Chaining: the signature covers the payload bytes as sent, "
    "and `prev` covers the compact JWS as sent. A business SHOULD emit the "
    "payload in JCS form, but verifiers MUST NOT require it."
  ),
  "business_3": (
    "Business Requirements 3: MUST NOT authorize or capture so that "
    "`authorized` or `captured` exceeds `mandated`. Any increase in what "
    "the buyer pays beyond `mandated` (a tip, an overage, an order change, "
    "or a re-authorization above the original amount) MUST be covered by a "
    "new closed Payment Mandate from the platform. Movements that spend "
    "against the new mandate form their own chain, keyed by its "
    "`mandate_ref`."
  ),
  "business_7": (
    "Business Requirements 7: MUST, on a follow-up receipt that only adds "
    "`network_ref` to a movement already receipted as `succeeded`, carry "
    "that movement's `event.ref.processing_entry_id`. A business that "
    "cannot supply a `processing_entry_id` issues the movement as `pending` "
    "first and adds `network_ref` on the `succeeded` receipt."
  ),
  "business_9": (
    "Business Requirements 9: MUST keep retired public keys resolvable by "
    "`kid` for at least the dispute window of the payment method used."
  ),
  "split_shipments": (
    "Edge Cases, Split shipments: multiple `capture` receipts against the "
    "same `mandate_ref`, with `captured` accumulating."
  ),
  "overcapture": (
    "Edge Cases, Overcapture: when the processor allows capturing more "
    "than was authorized, the business first issues an "
    "`authorization_adjustment` receipt for the difference, so `captured` "
    "never exceeds `authorized`. Without it, the capture is "
    "`receipt_capture_exceeds_authorization`. An overcapture above "
    "`mandated` needs a new mandate, as for any increase."
  ),
  "refund_returned": (
    "Edge Cases, Refund returned after success: a refund that was "
    "receipted as `succeeded` and later fails or is returned by the issuer "
    "gets a `refund_reversal` receipt with the same `event.ref`, which "
    "lowers `refunded`."
  ),
  "auth_expiry": (
    "Edge Cases, Authorization expiry: a `release` receipt for the "
    "expired, uncaptured amount, then a new `authorization` receipt with "
    "the same `mandate_ref`. Without a new mandate, the new authorization "
    "is limited to `mandated` minus `captured`, so `authorized` never "
    "exceeds `mandated`."
  ),
  "partial_capture_release": (
    "Edge Cases, Partial capture, remainder released: a 3000 capture "
    "followed by a `release` of the remaining 2400 leaves `authorized` at "
    "3000 and `captured` at 3000."
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
    "the movement is receipted as `succeeded`, it appears on a follow-up "
    "receipt with the same `type` and `event.ref.processing_entry_id`, "
    "which does not change the totals. `network_ref` is optional on every "
    "status: a refund that the processor settles as a reversal of the "
    "original transaction may never have one. It is still receipted as a "
    "`refund`."
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

  def sign(
    self,
    payload: dict[str, Any],
    typ: str | None = v.RECEIPT_TYP,
    canonical: bool = True,
  ) -> str:
    """Return the compact JWS receipt for a payload (Receipt Format).

    Args:
      payload: The receipt payload.
      typ: The header `typ`; None omits it.
      canonical: If False, sign the payload as indented JSON with its
        members in reverse order, which is valid JSON but not JCS form
        ("verifiers MUST NOT require" JCS).

    """
    header = {"alg": "ES256", "kid": self.kid}
    if typ is not None:
      header["typ"] = typ
    payload_bytes = (
      _jcs(payload)
      if canonical
      else json.dumps(
        dict(reversed(list(payload.items()))), indent=2, ensure_ascii=False
      ).encode("utf-8")
    )
    signing_input = (
      v.b64url_encode(_jcs(header)) + "." + v.b64url_encode(payload_bytes)
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

  Running totals follow from the events ("Running Totals"): only succeeded
  events change them, `authorization`, `authorization_adjustment` and
  `release` move `authorized`, `captured` is net of capture reversals, and a
  succeeded receipt that repeats an earlier one's `type` and
  `processing_entry_id` is a follow-up that changes nothing.
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
    self._counted: set[tuple[str, str]] = set()

  def clone(self) -> "_Chain":
    """Return a copy that continues from this chain's state.

    It reuses the signer, because a `cryptography` private key cannot be
    deep-copied, and copies the mutable state so issuing from the clone
    leaves this chain alone.
    """
    twin = copy.copy(self)
    twin.totals = dict(self.totals)
    twin._counted = set(self._counted)
    return twin

  def issue(
    self,
    event_type: str,
    amount: int,
    occurred_at: str,
    status: str = "succeeded",
    signer: FixtureSigner | None = None,
    sign_options: dict[str, Any] | None = None,
    stated_totals: dict[str, int] | None = None,
    **extra: Any,
  ) -> str:
    """Sign the next receipt; `extra` adds optional `event` members.

    `sign_options` goes to `FixtureSigner.sign`. `stated_totals` overrides
    members of the stated totals only, leaving the chain's true running
    totals alone, to build a receipt that misstates them.
    """
    entry_id = (extra.get("ref") or {}).get("processing_entry_id")
    follow_up = entry_id is not None and (event_type, entry_id) in self._counted
    if status == "succeeded" and not follow_up:
      key = {
        "authorization": ("authorized", 1),
        "authorization_adjustment": ("authorized", 1),
        "release": ("authorized", -1),
        "capture": ("captured", 1),
        "capture_reversal": ("captured", -1),
        "refund": ("refunded", 1),
        "refund_reversal": ("refunded", -1),
      }[event_type]
      self.totals[key[0]] += key[1] * amount
      if entry_id is not None:
        self._counted.add((event_type, entry_id))
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
      "totals": {**self.totals, **(stated_totals or {})},
    }
    compact = (signer or self.signer).sign(payload, **(sign_options or {}))
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
  mandates: list[tuple[str, str, int]],
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
      {
        "credential_token": token,
        "business": business,
        "payment_amount": amount,
      }
      for token, business, amount in mandates
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
    mandates=kwargs.pop(
      "mandates", [(chain.token, chain.iss, chain.totals["mandated"])]
    ),
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
    rules=["verify_1", "verify_4", "verify_5", "verify_6"],
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
    ["verify_1", "verify_4", "verify_5", "verify_6", "split_shipments"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS, 5400)],
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
    rules=["verify_4", "verify_6", "business_3"],
    receipts=receipts,
    outcomes=[
      (1, v.RECEIPT_EXCEEDS_MANDATE),
      (1, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
    ],
    notes=(
      "captured 6000 exceeds mandated 5400 and also authorized 5400, with "
      "no authorization_adjustment, so both outcomes are reported."
    ),
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
    rules=["verify_4", "verify_6", "business_3"],
    receipts=receipts,
    totals=_usd_totals(
      5000, 400, {"type": "fee", "amount": 600, "display_text": "Tip"}
    ),
    mandates=[
      ("test_only_payment_mandate_v04_original", _MERCHANT, 5400),
      ("test_only_payment_mandate_v04_increase", _MERCHANT, 600),
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
    ["verify_1", "verify_3"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS, 5400)],
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
    ["verify_4", "verify_5", "verify_6"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [(_SPEC_TOKEN, _SPEC_BUSINESS, 5400)],
    _spec_order([_SPEC_CHAIN[0], _SPEC_CHAIN[2]]),
    outcomes=[(1, v.RECEIPT_SEQUENCE_GAP)],
    notes=(
      "The ucp#1109 chain with its seq 2 receipt removed. The platform "
      "holds incomplete events, so recomputation stops at the gap "
      "(Platform Verification 5): the seq 3 receipt, which states captured "
      "5400 where the events held give 2400, reports only "
      "receipt_sequence_gap, not receipt_totals_mismatch. Rule 6 still "
      "runs on its stated totals, which are within the mandate."
    ),
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
    rules=["verify_4", "verify_6"],
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
    rules=["verify_1", "business_9"],
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
    rules=["verify_1", "verify_3", "verify_4", "verify_5", "business_9"],
    receipts=receipts,
    issuers={_MERCHANT: [chain.signer.jwk]},
    outcomes=[
      (0, v.RECEIPT_INVALID_SIGNATURE),
      (1, v.RECEIPT_SEQUENCE_GAP),
    ],
    notes=(
      "Negative counterpart of v08_rotated_retained_key: test_only_2025 is "
      "gone from keys, so the `kid` of seq 1 does not resolve and it is "
      "receipt_invalid_signature. It is then excluded from the chain, so "
      "seq 2 is reported as a gap only: recomputation stops at the excluded "
      "receipt, so no receipt_totals_mismatch is reported for seq 2 (see "
      "v06_missing_seq)."
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
    mandates=[(f"test_only_payment_mandate_{vector}", _MARKETPLACE, 5400)],
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
    rules=["verify_4", "verify_6", "verify_7", "marketplace"],
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
    rules=["verify_4", "verify_6", "verify_7", "marketplace"],
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
    rules=["verify_4", "verify_6", "verify_7"],
    warnings=[(2, v.RECEIPT_EXCEEDS_SELLER_TOTAL)],
    notes=(
      "Chain totals pass (refunded 1000 <= captured 2000), but seller_b "
      "has no captures. Second half of the per-seller SHOULD."
    ),
  )


def _v09_seller_refund_reversal() -> dict:
  return _marketplace_vector(
    "seller_refund_reversal_nets",
    [
      ("authorization", 5400, "2026-11-09T14:53:20Z", {}),
      ("capture", 2000, "2026-11-10T18:04:00Z", {"seller_id": "seller_a"}),
      ("capture", 3400, "2026-11-11T18:04:00Z", {"seller_id": "seller_b"}),
      ("refund", 1500, "2026-11-12T09:00:00Z", {"seller_id": "seller_a"}),
      (
        "refund_reversal",
        1500,
        "2026-11-13T09:00:00Z",
        {"seller_id": "seller_a"},
      ),
      ("refund", 2000, "2026-11-14T09:00:00Z", {"seller_id": "seller_a"}),
    ],
    title="A refund_reversal lowers a seller's refunds",
    test_plan=None,
    rules=["verify_7", "refund_returned"],
    notes=(
      "seller_a's refunds go 1500, 0, 2000: never above its 2000 captures. "
      "Summed without the reversal they would reach 3500 and warn."
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
    mandates=[(token_a, _SELLER_A, 2000), (token_b, _SELLER_B, 3400)],
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
    rules=["verify_1", "verify_4", "verify_6", "several_mor"],
  )


def _v10_mismatch() -> dict:
  return _two_mor_vector(
    "v10_iss_mismatch",
    with_mismatch=True,
    title="A receipt whose iss does not match its mandate",
    test_plan="10",
    rules=["verify_1", "verify_2", "verify_3", "several_mor"],
    outcomes=[(4, v.RECEIPT_MANDATE_UNKNOWN)],
    notes=(
      "Receipt 4 carries seller A's mandate_ref with iss seller-b and is "
      "signed with seller-b's key. Both valid chains still verify."
    ),
  )


def _lodging_vector(
  vector: str,
  refunds: int | list[tuple[int, str]],
  adjustment_id: str = "adj_cancel_1",
  with_adjustment: bool = True,
  **kwargs: Any,
) -> dict:
  """EUR 600.00 booking, cancelled; `refunds` is an amount or a list."""
  if isinstance(refunds, int):
    refunds = [(refunds, "succeeded")]
  signer = FixtureSigner(_HOTEL, "test_only_2026")
  token = f"test_only_payment_mandate_{vector}"
  chain = _Chain(
    signer, _HOTEL, token, 60000, f"order_{vector}", f"chk_{vector}", "EUR"
  )
  receipts = [
    chain.issue("authorization", 60000, "2026-11-01T09:00:00Z"),
    chain.issue("capture", 60000, "2026-11-01T09:00:30Z"),
  ]
  for number, (amount, status) in enumerate(refunds):
    receipts.append(
      chain.issue(
        "refund",
        amount,
        f"2026-11-14T16:0{number}:00Z",
        status,
        ref={"adjustment_id": adjustment_id},
      )
    )
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
    adjustments=[adjustment] if with_adjustment else [],
  )
  return _fixture(
    vector,
    issuers={_HOTEL: [signer.jwk]},
    mandates=[(token, _HOTEL, 60000)],
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
    rules=["verify_4", "verify_6", "verify_8", "partial_refund"],
  )


def _v12() -> dict:
  return _lodging_vector(
    "v12_lodging_refund_mismatch",
    30000,
    title="Lodging refund that disagrees with the cancellation adjustment",
    test_plan="12",
    rules=["verify_4", "verify_6", "verify_8", "partial_refund"],
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
    rules=["verify_4", "verify_5", "pending", "business_7"],
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
    rules=["verify_4", "verify_5", "pending", "late_network_ref"],
    notes=(
      "The refund settles as a reversal of the authorization shortly after "
      "capture, so no network reference is ever assigned: the succeeded "
      "receipt for the same event.ref has no network_ref, and is valid."
    ),
  )


def _extra(
  vector: str,
  title: str,
  rules: list[str],
  steps: list[tuple],
  mandated: int = 5400,
  mandate_amount: int = 5400,
  **kwargs: Any,
) -> dict:
  """Single-merchant USD vector, receipts issued from `steps`.

  Each step is (event_type, amount, occurred_at, status, issue_kwargs).
  """
  chain = _simple_chain(vector)
  chain.totals["mandated"] = mandated
  receipts = [
    chain.issue(kind, amount, at, status, **extra)
    for kind, amount, at, status, extra in steps
  ]
  return _merchant_vector(
    vector,
    chain,
    title=title,
    test_plan=None,
    rules=rules,
    receipts=receipts,
    mandates=[(chain.token, chain.iss, mandate_amount)],
    **kwargs,
  )


_AUTH_AT = "2026-11-09T14:53:20Z"
_CAPTURE_AT = "2026-11-10T02:00:00Z"
_RELEASE_AT = "2026-11-10T02:05:00Z"
_LATER_AT = "2026-11-12T10:00:00Z"
_OK = "succeeded"


def _totals_mismatch_captured() -> dict:
  return _extra(
    "totals_mismatch_captured",
    "Stated captured differs from the succeeded events",
    ["verify_5", "running_totals"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      (
        "capture",
        3000,
        _CAPTURE_AT,
        _OK,
        {"stated_totals": {"captured": 2400}},
      ),
      ("capture", 2400, _LATER_AT, _OK, {}),
    ],
    outcomes=[(1, v.RECEIPT_TOTALS_MISMATCH)],
    notes=(
      "Receipt 1 captures 3000 but states captured 2400. Totals are "
      "recomputed from the events, so receipt 2, which states the correct "
      "running totals, is not affected."
    ),
  )


def _totals_mismatch_capture_reduces_authorized() -> dict:
  return _extra(
    "totals_mismatch_capture_reduces_authorized",
    "A capture does not reduce authorized",
    ["verify_5", "running_totals"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      (
        "capture",
        3000,
        _CAPTURE_AT,
        _OK,
        {"stated_totals": {"authorized": 2400}},
      ),
    ],
    outcomes=[(1, v.RECEIPT_TOTALS_MISMATCH)],
    notes=(
      "The business states authorized 2400 after a 3000 capture, as if the "
      "capture used up the authorization. Recomputed authorized is 5400."
    ),
  )


def _totals_mismatch_mandated() -> dict:
  return _extra(
    "totals_mismatch_mandated_9000_auth",
    "totals.mandated differs from the mandate's payment_amount",
    ["verify_5", "verify_6", "business_3"],
    [("authorization", 9000, _AUTH_AT, _OK, {})],
    mandated=9000,
    mandate_amount=5400,
    outcomes=[
      (0, v.RECEIPT_TOTALS_MISMATCH),
      (0, v.RECEIPT_EXCEEDS_MANDATE),
    ],
    notes=(
      "The business authorizes 9000 and states mandated 9000 against a "
      "Payment Mandate whose payment_amount is 5400. Its totals are "
      "consistent with its events, so only the comparison with "
      "payment_amount shows the problem: totals.mandated differs "
      "(receipt_totals_mismatch) and the recomputed authorized 9000 "
      "exceeds the mandate's 5400 (receipt_exceeds_mandate). The two "
      "outcomes are independent, so both are reported when both "
      "conditions hold. Compare reauthorization_exceeds_mandate, whose "
      "stated totals are honest and which reports only the exceed."
    ),
  )


def _authorized_after_capture_and_release() -> dict:
  return _extra(
    "authorized_after_capture_and_release",
    "3000 captured, 2400 released: authorized stays 3000",
    ["verify_5", "verify_6", "running_totals", "partial_capture_release"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 3000, _CAPTURE_AT, _OK, {}),
      ("release", 2400, _RELEASE_AT, _OK, {}),
    ],
    notes=(
      "authorized = authorization + authorization_adjustment - release, so "
      "5400 - 2400 = 3000 and the capture does not reduce it. Totals after "
      "receipt 2: authorized 3000, captured 3000."
    ),
  )


def _reauthorization_within_mandate() -> dict:
  return _extra(
    "reauthorization_within_mandate",
    "Authorization expiry, then a re-authorization of mandated - captured",
    ["verify_6", "running_totals", "auth_expiry"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 3000, _CAPTURE_AT, _OK, {}),
      ("release", 2400, _RELEASE_AT, _OK, {}),
      ("authorization", 2400, _LATER_AT, _OK, {}),
    ],
    notes="authorized returns to 5400, which equals mandated.",
  )


def _reauthorization_exceeds_mandate() -> dict:
  return _extra(
    "reauthorization_exceeds_mandate",
    "A re-authorization above the mandate gives receipt_exceeds_mandate",
    ["verify_6", "business_3", "auth_expiry"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 3000, _CAPTURE_AT, _OK, {}),
      ("release", 2400, _RELEASE_AT, _OK, {}),
      ("authorization", 5400, _LATER_AT, _OK, {}),
    ],
    outcomes=[(3, v.RECEIPT_EXCEEDS_MANDATE)],
    notes=(
      "After the release authorized is 3000; authorizing the full 5400 "
      "again makes it 8400, above mandated 5400. The totals are stated "
      "honestly, so only receipt_exceeds_mandate applies."
    ),
  )


def _authorization_adjustment() -> dict:
  return _extra(
    "authorization_adjustment_increase",
    "An authorization_adjustment adds to authorized",
    ["verify_5", "running_totals"],
    [
      ("authorization", 3000, _AUTH_AT, _OK, {}),
      ("authorization_adjustment", 2400, "2026-11-09T15:00:00Z", _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
    ],
    notes="authorized 3000 + 2400 = 5400 after receipt 1.",
  )


def _capture_reversal() -> dict:
  return _extra(
    "capture_reversal",
    "A capture_reversal subtracts from captured",
    ["verify_5", "running_totals"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
      ("capture_reversal", 5400, _RELEASE_AT, _OK, {}),
      ("capture", 5400, _LATER_AT, _OK, {}),
    ],
    notes="captured goes 5400, 0, 5400: never above mandated.",
  )


def _duplicate_different_seq() -> dict:
  chain = _simple_chain("duplicate_different_seq")
  receipts = [
    chain.issue("authorization", 5400, _AUTH_AT),
    chain.issue(
      "capture", 3000, _CAPTURE_AT, ref={"processing_entry_id": "ent_2"}
    ),
  ]
  # A second, different receipt for seq 2, signed by the same business.
  rival = chain.clone()
  rival.seq -= 1
  rival.totals["captured"] -= 3000
  rival.prev = v.sha256_ref(receipts[0])
  receipts.append(
    rival.issue(
      "capture", 3000, _CAPTURE_AT, ref={"processing_entry_id": "ent_2b"}
    )
  )
  receipts.append(
    chain.issue(
      "capture", 2400, _LATER_AT, ref={"processing_entry_id": "ent_3"}
    )
  )
  return _merchant_vector(
    "duplicate_different_seq",
    chain,
    title="A second, different receipt for a seq already held",
    test_plan=None,
    rules=["verify_4"],
    receipts=receipts,
    outcomes=[(2, v.RECEIPT_SEQUENCE_GAP)],
    notes=(
      "Receipts 1 and 2 are both seq 2, with different processing entries. "
      "The one held first stays in the chain, so receipt 3 (seq 3, whose "
      "prev is receipt 1) continues it. The second is reported as a gap "
      "and neither chained nor counted; the platform keeps both. "
      "Recomputation stops at that gap, so receipt 3 is not compared with "
      "recomputed totals; its stated totals are within the mandate. A "
      "byte-identical redelivery of a held receipt would be ignored, not "
      "a gap."
    ),
  )


def _bad_typ(vector: str, typ: str | None, title: str) -> dict:
  return _extra(
    vector,
    title,
    ["verify_1"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      (
        "capture",
        5400,
        _CAPTURE_AT,
        _OK,
        {"sign_options": {"typ": typ}},
      ),
    ],
    outcomes=[(1, v.RECEIPT_INVALID_SIGNATURE)],
    notes=(
      "The last receipt is correctly signed, but its header "
      f"typ is {typ!r}, not ucp-ap2-receipt+jwt."
    ),
  )


def _wrong_typ() -> dict:
  return _bad_typ(
    "wrong_typ", "JWT", "A wrong typ gives receipt_invalid_signature"
  )


def _missing_typ() -> dict:
  return _bad_typ(
    "missing_typ", None, "A missing typ gives receipt_invalid_signature"
  )


def _signature_before_mandate_binding() -> dict:
  receipts = [*_SPEC_CHAIN[:2], _tamper_amount(_SPEC_CHAIN[2], 4400)]
  return _fixture(
    "signature_checked_before_mandate_binding",
    "mandate_ref/iss binding is checked only after the signature verifies",
    None,
    ["verify_1", "verify_2", "verify_3"],
    _SPEC_SIGNING + " Source: " + SPEC_SOURCE + ".",
    {_SPEC_BUSINESS: [_SPEC_KEY]},
    [("a_mandate_the_platform_did_not_send", _SPEC_BUSINESS, 5400)],
    _spec_order(receipts),
    outcomes=[
      (0, v.RECEIPT_MANDATE_UNKNOWN),
      (1, v.RECEIPT_MANDATE_UNKNOWN),
      (2, v.RECEIPT_INVALID_SIGNATURE),
    ],
    notes=(
      "The platform never sent the mandate behind this chain. Receipts 0 "
      "and 1 verify and are mandate_unknown. Receipt 2 is tampered (as in "
      "v05_tampered_amount): its signature fails, so its mandate_ref is "
      "not checked and it is invalid_signature only."
    ),
  )


def _non_jcs_bytes() -> dict:
  chain = _simple_chain("non_jcs_bytes")
  loose = {"sign_options": {"canonical": False}}
  receipts = [
    chain.issue("authorization", 5400, _AUTH_AT, **loose),
    chain.issue(
      "capture",
      3000,
      _CAPTURE_AT,
      ref={"processing_entry_id": "ent_2"},
      **loose,
    ),
    chain.issue(
      "capture", 2400, _LATER_AT, ref={"processing_entry_id": "ent_3"}
    ),
  ]
  return _merchant_vector(
    "non_jcs_bytes_accepted",
    chain,
    title="Receipts whose payload bytes are not JCS form verify",
    test_plan=None,
    rules=["no_jcs", "verify_1", "verify_4"],
    receipts=receipts,
    notes=(
      "Receipts 0 and 1 are signed over indented JSON with members in "
      "reverse order. The signature and the next receipt's prev cover "
      "those bytes as sent, so verification does not depend on canonical "
      "form."
    ),
  )


def _network_ref_follow_up(vector: str, misstated: bool) -> dict:
  chain = _simple_chain(vector)
  ref = {"processing_entry_id": "ent_r1", "adjustment_id": "adj_refund_1"}
  receipts = [
    chain.issue("authorization", 5400, _AUTH_AT),
    chain.issue("capture", 5400, _CAPTURE_AT),
    chain.issue("refund", 5400, "2026-11-10T04:00:00Z", ref=ref),
    chain.issue(
      "refund",
      5400,
      "2026-11-10T04:00:00Z",
      ref=ref,
      network_ref="74000000000000000001063",
      **({"stated_totals": {"refunded": 10800}} if misstated else {}),
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
    test_plan=None,
    **(
      {
        "title": "A network_ref follow-up that changes the totals",
        "rules": ["each_once", "verify_5"],
        "outcomes": [(3, v.RECEIPT_TOTALS_MISMATCH)],
        "notes": (
          "The follow-up repeats the refund's type and processing_entry_id "
          "but states refunded 10800, as if the 5400 counted twice."
        ),
      }
      if misstated
      else {
        "title": "A late network_ref follow-up does not change the totals",
        "rules": ["each_once", "business_7", "late_network_ref"],
        "notes": (
          "Receipt 3 repeats the succeeded refund's type and "
          "processing_entry_id and adds network_ref. refunded stays 5400, "
          "and the refund counts once when reconciled against its "
          "adjustment."
        ),
      }
    ),
  )


def _reconcile_split_refunds() -> dict:
  return _lodging_vector(
    "reconcile_split_refunds",
    [(30000, "succeeded"), (15000, "succeeded")],
    title="Two succeeded refunds citing one adjustment sum to its total",
    test_plan=None,
    rules=["verify_8"],
    notes="30000 + 15000 matches the 45000 adjustment.",
  )


def _reconcile_pending_not_counted() -> dict:
  return _lodging_vector(
    "reconcile_pending_not_counted",
    [(30000, "succeeded"), (15000, "pending")],
    title="A pending refund is not counted when reconciling",
    test_plan=None,
    rules=["verify_8"],
    reconciliation=[(2, v.RECONCILIATION_ADJUSTMENT_MISMATCH)],
    notes=(
      "Only the succeeded 30000 counts against the 45000 adjustment. The "
      "finding is attached to the last succeeded refund that cites it."
    ),
  )


def _reconcile_unmatched_adjustment() -> dict:
  return _lodging_vector(
    "reconcile_unmatched_adjustment_id",
    45000,
    adjustment_id="adj_does_not_exist",
    title="A refund citing an adjustment the Order does not have",
    test_plan=None,
    rules=["verify_8"],
    reconciliation=[(2, v.RECONCILIATION_ADJUSTMENT_MISMATCH)],
    notes=(
      "The refund cites adj_does_not_exist; the Order's only adjustment "
      "is adj_cancel_1."
    ),
  )


def _refund_adjustment(amount: int = 5400) -> dict:
  return {
    "id": "adj_refund_1",
    "type": "refund",
    "occurred_at": "2026-11-10T04:00:00Z",
    "status": "completed",
    "totals": [{"type": "total", "amount": -amount}],
  }


_R1 = {
  "ref": {"processing_entry_id": "ent_r1", "adjustment_id": "adj_refund_1"}
}
_R2 = {
  "ref": {"processing_entry_id": "ent_r2", "adjustment_id": "adj_refund_1"}
}


def _refund_reversal_chain() -> dict:
  return _extra(
    "refund_reversal_chain",
    "A refund_reversal lowers refunded and nets against its adjustment",
    ["verify_8", "running_totals", "refund_returned"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
      ("refund", 5400, "2026-11-10T04:00:00Z", _OK, _R1),
      ("refund_reversal", 5400, "2026-11-12T10:00:00Z", _OK, _R1),
      ("refund", 5400, "2026-11-13T10:00:00Z", _OK, _R2),
    ],
    adjustments=[_refund_adjustment()],
    notes=(
      "refunded goes 5400, 0, 5400. The reversal has the same type-distinct "
      "event.ref as the refund it reverses, so it is not a follow-up. "
      "Reconciliation: 5400 - 5400 + 5400 matches the 5400 adjustment."
    ),
  )


def _reconcile_refund_reversal_mismatch() -> dict:
  return _extra(
    "reconcile_refund_reversal_mismatch",
    "A reversed refund no longer matches the adjustment it cites",
    ["verify_8", "refund_returned"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
      ("refund", 5400, "2026-11-10T04:00:00Z", _OK, _R1),
      ("refund_reversal", 5400, "2026-11-12T10:00:00Z", _OK, _R1),
    ],
    adjustments=[_refund_adjustment()],
    reconciliation=[(3, v.RECONCILIATION_ADJUSTMENT_MISMATCH)],
    notes=(
      "The refund is returned and not reissued: 5400 - 5400 = 0 against a "
      "5400 adjustment. The finding is on the last succeeded receipt that "
      "cites it, the reversal. The receipts remain valid."
    ),
  )


def _overcapture() -> dict:
  return _extra(
    "overcapture_after_authorization_adjustment",
    "An overcapture is preceded by an authorization_adjustment",
    ["overcapture", "running_totals"],
    [
      ("authorization", 5000, _AUTH_AT, _OK, {}),
      ("authorization_adjustment", 400, "2026-11-10T01:00:00Z", _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
    ],
    notes=(
      "authorized 5000 + 400 = 5400 before a 5400 capture, so captured "
      "never exceeds authorized and both stay within mandated 5400, so no "
      "outcome applies. Compare capture_exceeds_authorized_without_"
      "adjustment."
    ),
  )


def _capture_exceeds_authorized() -> dict:
  return _extra(
    "capture_exceeds_authorized_without_adjustment",
    "A capture above authorized with no adjustment",
    ["overcapture", "verify_6"],
    [
      ("authorization", 3000, _AUTH_AT, _OK, {}),
      ("capture", 5000, _CAPTURE_AT, _OK, {}),
    ],
    outcomes=[(1, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION)],
    notes=(
      "authorized 3000, captured 5000, mandated 5400, with no "
      "authorization_adjustment before the capture. captured exceeds "
      "authorized but not mandated, so only "
      "receipt_capture_exceeds_authorization applies."
    ),
  )


def _capture_exceeds_authorized_and_mandate() -> dict:
  return _extra(
    "capture_exceeds_authorized_and_mandate",
    "A capture above both authorized and mandated reports both",
    ["overcapture", "verify_6"],
    [
      ("authorization", 3000, _AUTH_AT, _OK, {}),
      ("capture", 6000, _CAPTURE_AT, _OK, {}),
    ],
    outcomes=[
      (1, v.RECEIPT_EXCEEDS_MANDATE),
      (1, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
    ],
    notes=(
      "captured 6000 exceeds authorized 3000 and mandated 5400. The two "
      "checks are independent, so receipt 1 reports both. authorized 3000 "
      "is within the mandate."
    ),
  )


def _exceed_persists_capture() -> dict:
  return _extra(
    "exceed_persists_capture_over_authorized",
    "receipt_capture_exceeds_authorization on every receipt still over",
    ["verify_6", "overcapture"],
    [
      ("authorization", 3000, _AUTH_AT, _OK, {}),
      ("capture", 3500, _CAPTURE_AT, _OK, {}),
      ("capture", 100, "2026-11-12T10:00:00Z", _OK, {}),
    ],
    outcomes=[
      (1, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
      (2, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
    ],
    notes=(
      "captured is 3500 then 3600 against authorized 3000: both receipts "
      "are reported, not only the first. Both stay within mandated 5400."
    ),
  )


def _exceed_persists_refund() -> dict:
  return _extra(
    "exceed_persists_refund_over_captured",
    "receipt_refund_exceeds_capture on every receipt still over",
    ["verify_6"],
    [
      ("authorization", 5400, _AUTH_AT, _OK, {}),
      ("capture", 5400, _CAPTURE_AT, _OK, {}),
      ("refund", 6000, "2026-11-12T10:00:00Z", _OK, {}),
      ("refund", 100, "2026-11-13T10:00:00Z", _OK, {}),
    ],
    outcomes=[
      (2, v.RECEIPT_REFUND_EXCEEDS_CAPTURE),
      (3, v.RECEIPT_REFUND_EXCEEDS_CAPTURE),
    ],
    notes=(
      "refunded is 6000 then 6100 against captured 5400: both receipts "
      "are reported, not only the first. Their stated totals are honest, "
      "so there is no receipt_totals_mismatch."
    ),
  )


def _exceed_persists_mandate() -> dict:
  return _extra(
    "exceed_persists_authorized_over_mandate",
    "receipt_exceeds_mandate on every receipt still over",
    ["verify_6", "business_3"],
    [
      ("authorization", 3000, _AUTH_AT, _OK, {}),
      ("authorization_adjustment", 3000, "2026-11-09T15:00:00Z", _OK, {}),
      ("capture", 2000, _CAPTURE_AT, _OK, {}),
    ],
    outcomes=[
      (1, v.RECEIPT_EXCEEDS_MANDATE),
      (2, v.RECEIPT_EXCEEDS_MANDATE),
    ],
    notes=(
      "authorized is 6000 after receipt 1 and stays 6000 after the capture "
      "in receipt 2, against mandated 5400. Both are reported."
    ),
  )


def _gap_vector(vector: str, title: str, rules: list[str], build, **kwargs):
  chain = _simple_chain(vector)
  receipts = [
    chain.issue("authorization", 5400, _AUTH_AT),
    chain.issue(
      "capture", 3000, _CAPTURE_AT, ref={"processing_entry_id": "ent_2"}
    ),
  ]
  receipts.extend(build(chain, receipts))
  return _merchant_vector(
    vector,
    chain,
    title=title,
    test_plan=None,
    rules=rules,
    receipts=receipts,
    **kwargs,
  )


def _rival(chain: _Chain, receipts: list[str], **stated) -> tuple:
  """Return a second, different seq 2 receipt and the chain continuing it."""
  rival = chain.clone()
  rival.seq -= 1
  rival.totals["captured"] -= 3000
  rival.prev = v.sha256_ref(receipts[0])
  compact = rival.issue(
    "capture",
    3000,
    _CAPTURE_AT,
    ref={"processing_entry_id": "ent_2b"},
    stated_totals=stated or None,
  )
  return compact, rival


def _continues_from_gapped() -> dict:
  def build(chain, receipts):
    rival, twin = _rival(chain, receipts)
    return [
      rival,
      twin.issue(
        "capture", 2400, _LATER_AT, ref={"processing_entry_id": "ent_3"}
      ),
    ]

  return _gap_vector(
    "receipt_after_gap_continues_chain",
    "A receipt whose prev is the gapped receipt is not a second gap",
    ["verify_4", "verify_5"],
    build,
    outcomes=[(2, v.RECEIPT_SEQUENCE_GAP)],
    notes=(
      "Receipts 1 and 2 are both seq 2. Receipt 3 (seq 3) has prev set to "
      "the hash of receipt 2, the one reported as a gap, so it continues "
      "the chain from it and reports nothing. Contrast "
      "duplicate_different_seq, where seq 3 chains from the first receipt "
      "held."
    ),
  )


def _gapped_receipt_checked_duplicate() -> dict:
  def build(chain, receipts):
    return [_rival(chain, receipts, captured=9000)[0]]

  return _gap_vector(
    "gapped_receipt_checked_on_stated_totals_duplicate_seq",
    "Rule 6 checks a duplicate-seq gapped receipt on its stated totals",
    ["verify_4", "verify_6"],
    build,
    outcomes=[
      (2, v.RECEIPT_SEQUENCE_GAP),
      (2, v.RECEIPT_EXCEEDS_MANDATE),
      (2, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
    ],
    notes=(
      "The second seq 2 receipt is the gap and states captured 9000 against "
      "authorized 5400 and mandated 5400, so it is over both. "
      "Recomputation stopped at it, so there is no "
      "receipt_totals_mismatch, but rule 6 applies to the receipt itself."
    ),
  )


def _gapped_receipt_checked_skip() -> dict:
  chain = _simple_chain("gapped_receipt_checked_on_stated_totals_skipped_seq")
  receipts = [chain.issue("authorization", 5400, _AUTH_AT)]
  chain.issue("capture", 3000, _CAPTURE_AT)  # seq 2, never delivered
  receipts.append(
    chain.issue(
      "capture",
      2400,
      _LATER_AT,
      stated_totals={"captured": 6000},
    )
  )
  return _merchant_vector(
    "gapped_receipt_checked_on_stated_totals_skipped_seq",
    chain,
    title="Rule 6 checks the gapped receipt itself on its stated totals",
    test_plan=None,
    rules=["verify_4", "verify_5", "verify_6"],
    receipts=receipts,
    outcomes=[
      (1, v.RECEIPT_SEQUENCE_GAP),
      (1, v.RECEIPT_EXCEEDS_MANDATE),
      (1, v.RECEIPT_CAPTURE_EXCEEDS_AUTHORIZATION),
    ],
    notes=(
      "seq 2 is missing, so the seq 3 receipt is the gap and the point "
      "where recomputation stops. It states captured 6000 against "
      "authorized 5400 and mandated 5400: rule 6 reports both on those "
      "stated totals, and rule 5 reports no receipt_totals_mismatch."
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
  _totals_mismatch_captured,
  _totals_mismatch_capture_reduces_authorized,
  _totals_mismatch_mandated,
  _authorized_after_capture_and_release,
  _reauthorization_within_mandate,
  _reauthorization_exceeds_mandate,
  _authorization_adjustment,
  _capture_reversal,
  _duplicate_different_seq,
  _wrong_typ,
  _missing_typ,
  _signature_before_mandate_binding,
  _non_jcs_bytes,
  lambda: _network_ref_follow_up("network_ref_follow_up", False),
  lambda: _network_ref_follow_up("network_ref_follow_up_changes_totals", True),
  _reconcile_split_refunds,
  _reconcile_pending_not_counted,
  _reconcile_unmatched_adjustment,
  _refund_reversal_chain,
  _reconcile_refund_reversal_mismatch,
  _overcapture,
  _capture_exceeds_authorized,
  _capture_exceeds_authorized_and_mandate,
  _exceed_persists_capture,
  _v09_seller_refund_reversal,
  _exceed_persists_refund,
  _exceed_persists_mandate,
  _continues_from_gapped,
  _gapped_receipt_checked_duplicate,
  _gapped_receipt_checked_skip,
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
