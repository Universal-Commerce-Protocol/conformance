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

"""Platform-side verifier for AP2 Order receipts.

Implements the checks in the "Order Receipts" section of the AP2 Mandates
extension (docs/specification/payment/extensions/ap2-mandates.md, as proposed
in Universal-Commerce-Protocol/ucp#1109). Section names in the comments below
refer to that document.

The verifier takes what a platform holds after checkout:

* the Order, with its receipts in `ap2.receipts`;
* the `keys` of each business profile, keyed by profile URI;
* the Payment Mandates it sent, each with the profile URI of the business
  whose checkout the mandate covers.

It reports three groups of findings, kept apart on purpose:

* `outcomes`: the MUST checks of "Platform Verification" 1 and 2;
* `warnings`: the per-seller SHOULD of "Platform Verification" 3
  (`receipt_exceeds_seller_total`);
* `reconciliation`: the SHOULD of "Platform Verification" 4
  (`reconciliation_adjustment_mismatch`). It is commercial, not
  cryptographic, so a receipt it flags stays valid.

Only ECDSA is implemented (ES256, ES384, ES512): the extension's algorithm
rule follows AP2 v0.2, which requires it ("Cryptographic Requirements").

Where the specification leaves a choice open, this verifier:

* reports a receipt whose `kid` does not resolve in the `keys` of `iss`, or
  whose `iss` has no known keys, as `receipt_invalid_signature` (its
  signature "cannot be verified");
* leaves a receipt that fails the signature or mandate binding check out of
  its chain, so the next receipt in that chain is a sequence gap;
* reconciles a refund against the magnitude of the cited adjustment's
  `total`, because adjustment totals are signed and receipt amounts are not;
* checks a seller only when it resolves in `sellers` and has attributed
  line items;
* evaluates both total invariants on the totals each receipt states, as
  Platform Verification 2 is written; `derived_totals` exposes what the
  chain's events add up to, but a difference has no outcome;
* records a signed receipt that is not a Receipt Payload in `malformed`
  rather than as an outcome, since no outcome covers it.
"""

import base64
from collections.abc import Iterable, Mapping
import dataclasses
import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

# "Receipt Format": the `typ` every receipt header carries.
RECEIPT_TYP = "ucp-ap2-receipt+jwt"

# `receipt_verification_outcome` in payment_ap2_mandate.json.
RECEIPT_INVALID_SIGNATURE = "receipt_invalid_signature"
RECEIPT_SEQUENCE_GAP = "receipt_sequence_gap"
RECEIPT_EXCEEDS_MANDATE = "receipt_exceeds_mandate"
RECEIPT_REFUND_EXCEEDS_CAPTURE = "receipt_refund_exceeds_capture"
RECEIPT_EXCEEDS_SELLER_TOTAL = "receipt_exceeds_seller_total"
RECEIPT_MANDATE_UNKNOWN = "receipt_mandate_unknown"
RECONCILIATION_ADJUSTMENT_MISMATCH = "reconciliation_adjustment_mismatch"

# `required` of `receipt_payload` in payment_ap2_mandate.json.
REQUIRED_PAYLOAD_MEMBERS = (
  "iss",
  "iat",
  "order_id",
  "mandate_ref",
  "seq",
  "prev",
  "event",
  "totals",
)
# `required` of `receipt_event` and `receipt_totals`.
REQUIRED_EVENT_MEMBERS = ("type", "status", "amount", "currency", "occurred_at")
REQUIRED_TOTALS_MEMBERS = ("mandated", "authorized", "captured", "refunded")

# JWS `alg` -> (JWK `crv`, hash, byte length of each of r and s).
_ECDSA_ALGORITHMS: dict[str, tuple[str, type[hashes.HashAlgorithm], int]] = {
  "ES256": ("P-256", hashes.SHA256, 32),
  "ES384": ("P-384", hashes.SHA384, 48),
  "ES512": ("P-521", hashes.SHA512, 66),
}
_CURVES: dict[str, ec.EllipticCurve] = {
  "P-256": ec.SECP256R1(),
  "P-384": ec.SECP384R1(),
  "P-521": ec.SECP521R1(),
}

ChainKey = tuple[str, str]


@dataclasses.dataclass(frozen=True)
class Finding:
  """One finding about one receipt.

  Attributes:
    receipt_index: Position of the receipt in the Order's `ap2.receipts`.
    code: A `receipt_verification_outcome` value.
    detail: Human-readable explanation.

  """

  receipt_index: int
  code: str
  detail: str


@dataclasses.dataclass(frozen=True)
class Totals:
  """`captured` and `refunded` derived from a chain's succeeded events."""

  captured: int
  refunded: int


@dataclasses.dataclass(frozen=True)
class VerifiedReceipt:
  """A receipt whose signature and mandate binding both verified."""

  index: int
  compact: str
  header: dict[str, Any]
  payload: dict[str, Any]
  derived_totals: Totals

  @property
  def chain(self) -> ChainKey:
    """The `(iss, mandate_ref)` chain the receipt belongs to."""
    return (self.payload["iss"], self.payload["mandate_ref"])


@dataclasses.dataclass
class VerificationResult:
  """Findings for one Order, grouped as described in the module docstring."""

  outcomes: list[Finding] = dataclasses.field(default_factory=list)
  warnings: list[Finding] = dataclasses.field(default_factory=list)
  reconciliation: list[Finding] = dataclasses.field(default_factory=list)
  verified: list[VerifiedReceipt] = dataclasses.field(default_factory=list)
  # Receipts that verify but are not a Receipt Payload, as (index, detail).
  # The specification defines no outcome for them; they join no chain.
  malformed: list[tuple[int, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class Mandate:
  """A closed Payment Mandate the platform sent at `complete_checkout`.

  Attributes:
    credential_token: The exact string sent in
      `payment.instruments[*].credential.token`.
    business: Profile URI of the business that signed the checkout the
      mandate covers (its `ap2.merchant_authorization`).

  """

  credential_token: str
  business: str

  @property
  def mandate_ref(self) -> str:
    """The `mandate_ref` a receipt spending against this mandate carries."""
    return sha256_ref(self.credential_token)


def b64url_decode(value: str) -> bytes:
  """Decode unpadded base64url, rejecting other alphabets."""
  if not value or any(
    c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for c in value
  ):
    raise ValueError("not unpadded base64url")
  return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(data: bytes) -> str:
  """Encode bytes as unpadded base64url."""
  return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sha256_ref(artifact: str) -> str:
  """Hash an artifact's exact UTF-8 bytes ("Hashes and Chaining").

  `mandate_ref` and `prev` both use this format: `sha-256:` followed by the
  unpadded base64url SHA-256 digest.
  """
  digest = hashlib.sha256(artifact.encode("utf-8")).digest()
  return "sha-256:" + b64url_encode(digest)


def public_key_from_jwk(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
  """Load an EC public key from a JWK (RFC 7517)."""
  if jwk.get("kty") != "EC" or jwk.get("crv") not in _CURVES:
    raise ValueError(f"unsupported JWK: kty={jwk.get('kty')}")
  numbers = ec.EllipticCurvePublicNumbers(
    int.from_bytes(b64url_decode(jwk["x"]), "big"),
    int.from_bytes(b64url_decode(jwk["y"]), "big"),
    _CURVES[jwk["crv"]],
  )
  return numbers.public_key()


def decode_receipt(compact: str) -> tuple[dict[str, Any], dict[str, Any]]:
  """Split a compact JWS receipt into its decoded header and payload."""
  if not isinstance(compact, str):
    raise ValueError("a receipt is a string")
  parts = compact.split(".")
  if len(parts) != 3:
    raise ValueError("a receipt is a compact JWS with three segments")
  header = json.loads(b64url_decode(parts[0]))
  payload = json.loads(b64url_decode(parts[1]))
  if not isinstance(header, dict) or not isinstance(payload, dict):
    raise ValueError("JWS header and payload must be JSON objects")
  return header, payload


def _check_signature(
  compact: str,
  header: Mapping[str, Any],
  issuer_keys: Iterable[Mapping[str, Any]] | None,
) -> str | None:
  """Return why the signature cannot be verified, or None if it verifies.

  "Receipt Format": the header carries `alg` per the extension's algorithm
  rule, `kid` resolving in the issuing business's `keys`, and `typ`.
  "Platform Verification" 1: verify against the `keys` of the business named
  in `iss`.
  """
  if header.get("typ") != RECEIPT_TYP:
    return f"header typ is {header.get('typ')!r}, expected {RECEIPT_TYP!r}"
  alg = header.get("alg")
  if alg not in _ECDSA_ALGORITHMS:
    return f"header alg {alg!r} is not ES256, ES384 or ES512"
  if issuer_keys is None:
    return "no keys are known for the business named in iss"
  kid = header.get("kid")
  jwk = next((k for k in issuer_keys if kid and k.get("kid") == kid), None)
  if jwk is None:
    return f"kid {header.get('kid')!r} does not resolve in the keys of iss"
  crv, hash_cls, size = _ECDSA_ALGORITHMS[alg]
  if jwk.get("crv") != crv or jwk.get("alg", alg) != alg:
    return f"key {jwk.get('kid')!r} cannot verify {alg}"

  signing_input, _, signature_b64 = compact.rpartition(".")
  try:
    signature = b64url_decode(signature_b64)
    if len(signature) != 2 * size:
      return "signature has the wrong length for the algorithm"
    der = utils.encode_dss_signature(
      int.from_bytes(signature[:size], "big"),
      int.from_bytes(signature[size:], "big"),
    )
    public_key_from_jwk(jwk).verify(
      der, signing_input.encode("ascii"), ec.ECDSA(hash_cls())
    )
  except (InvalidSignature, KeyError, TypeError, ValueError):
    return "signature does not verify"
  return None


def _payload_problems(payload: Mapping[str, Any]) -> list[str]:
  """List required members a payload lacks or has with the wrong type.

  Checks the members the verifier reads against `receipt_payload`,
  `receipt_event` and `receipt_totals`.
  """
  event = payload.get("event")
  totals = payload.get("totals")
  expected: dict[str, tuple[Any, type | tuple[type, ...]]] = {
    "iss": (payload.get("iss"), str),
    "mandate_ref": (payload.get("mandate_ref"), str),
    "seq": (payload.get("seq"), int),
    "prev": (payload.get("prev"), (str, type(None))),
    "event": (event, dict),
    "totals": (totals, dict),
  }
  if isinstance(event, dict):
    for member in REQUIRED_EVENT_MEMBERS:
      kind = int if member == "amount" else str
      expected[f"event.{member}"] = (event.get(member), kind)
  if isinstance(totals, dict):
    for member in REQUIRED_TOTALS_MEMBERS:
      expected[f"totals.{member}"] = (totals.get(member), int)
  problems = [m for m in REQUIRED_PAYLOAD_MEMBERS if m not in payload]
  problems += [
    name
    for name, (value, kind) in expected.items()
    if name.split(".")[0] in payload
    and (not isinstance(value, kind) or isinstance(value, bool))
  ]
  return problems


def _seller_of_line(line_item: Mapping[str, Any], order: Mapping[str, Any]):
  """Return the seller of record of an Order line item.

  Seller attribution (ucp#1018): `line_items[].seller_id` overrides the
  order-level `seller_id`.
  """
  return line_item.get("seller_id", order.get("seller_id"))


def _attributed_totals(order: Mapping[str, Any]) -> dict[str, int]:
  """Sum each seller's line item `total` amounts."""
  totals: dict[str, int] = {}
  for line_item in order.get("line_items", []):
    seller = _seller_of_line(line_item, order)
    if seller is None:
      continue
    amount = sum(
      t["amount"]
      for t in line_item.get("totals", [])
      if t.get("type") == "total"
    )
    totals[seller] = totals.get(seller, 0) + amount
  return totals


def _adjustment_amount(adjustment: Mapping[str, Any]) -> int | None:
  """Return the magnitude of an adjustment's `total`, if it has one.

  Adjustment totals are signed, negative for money returned to the buyer
  (adjustment.json), while a receipt `amount` is always non-negative.
  """
  totals = [
    t["amount"]
    for t in adjustment.get("totals", [])
    if t.get("type") == "total"
  ]
  return abs(totals[0]) if len(totals) == 1 else None


def verify_order_receipts(
  order: Mapping[str, Any],
  issuer_keys: Mapping[str, Iterable[Mapping[str, Any]]],
  mandates: Iterable[Mandate],
) -> VerificationResult:
  """Verify every receipt in `order["ap2"]["receipts"]`.

  Args:
    order: The Order, carrying its receipts in `ap2.receipts`.
    issuer_keys: The `keys` of each known business profile, by profile URI.
    mandates: The Payment Mandates the platform sent for this Order.

  Returns:
    The findings, grouped into outcomes, warnings and reconciliation.

  """
  result = VerificationResult()
  mandates_by_ref = {m.mandate_ref: m for m in mandates}
  # Last verified receipt and derived totals of each (iss, mandate_ref) chain.
  chain_tail: dict[ChainKey, VerifiedReceipt] = {}

  receipts = (order.get("ap2") or {}).get("receipts", [])
  for index, compact in enumerate(receipts):
    try:
      header, payload = decode_receipt(compact)
    except ValueError as exc:
      result.outcomes.append(
        Finding(index, RECEIPT_INVALID_SIGNATURE, f"not a compact JWS: {exc}")
      )
      continue

    # Platform Verification 1: the signature, against the keys of `iss`.
    iss = payload.get("iss")
    keys = issuer_keys.get(iss) if isinstance(iss, str) else None
    reason = _check_signature(compact, header, keys)
    if reason is not None:
      result.outcomes.append(Finding(index, RECEIPT_INVALID_SIGNATURE, reason))
      continue
    problems = _payload_problems(payload)
    if problems:
      result.malformed.append(
        (index, "missing or mistyped members: " + ", ".join(problems))
      )
      continue

    # Platform Verification 1: `iss` must be the business that signed the
    # checkout covered by `mandate_ref`; the outcome table adds the case of
    # an unknown `mandate_ref`. Business Requirements 2 says the same from
    # the business side.
    mandate = mandates_by_ref.get(payload["mandate_ref"])
    if mandate is None:
      result.outcomes.append(
        Finding(index, RECEIPT_MANDATE_UNKNOWN, "mandate_ref is unknown")
      )
      continue
    if mandate.business != payload["iss"]:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_MANDATE_UNKNOWN,
          f"iss {payload['iss']} is not the business the mandate covers "
          f"({mandate.business})",
        )
      )
      continue

    # Hashes and Chaining / Platform Verification 2: one chain per
    # (iss, mandate_ref). `seq` starts at 1 and increases by exactly 1;
    # `prev` is the hash of the previous receipt's compact JWS, null at 1.
    chain: ChainKey = (payload["iss"], payload["mandate_ref"])
    tail = chain_tail.get(chain)
    expected_seq = 1 if tail is None else tail.payload["seq"] + 1
    expected_prev = None if tail is None else sha256_ref(tail.compact)
    if payload["seq"] != expected_seq:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_SEQUENCE_GAP,
          f"seq {payload['seq']}, expected {expected_seq}",
        )
      )
    elif payload["prev"] != expected_prev:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_SEQUENCE_GAP,
          f"prev {payload['prev']} does not match {expected_prev}",
        )
      )

    # Platform Verification 2: both total invariants, per chain, on the
    # running totals the receipt states.
    totals = payload["totals"]
    if totals["captured"] > totals["mandated"]:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_EXCEEDS_MANDATE,
          f"captured {totals['captured']} > mandated {totals['mandated']}",
        )
      )
    if totals["refunded"] > totals["captured"]:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_REFUND_EXCEEDS_CAPTURE,
          f"refunded {totals['refunded']} > captured {totals['captured']}",
        )
      )

    # receipt_event.status: only `succeeded` events change the running
    # totals (Edge Cases, "Pending movements"). `captured` is net of capture
    # reversals (receipt_totals).
    previous = Totals(0, 0) if tail is None else tail.derived_totals
    event = payload["event"]
    captured, refunded = previous.captured, previous.refunded
    if event["status"] == "succeeded":
      if event["type"] == "capture":
        captured += event["amount"]
      elif event["type"] == "capture_reversal":
        captured -= event["amount"]
      elif event["type"] == "refund":
        refunded += event["amount"]
    verified = VerifiedReceipt(
      index, compact, header, payload, Totals(captured, refunded)
    )
    chain_tail[chain] = verified
    result.verified.append(verified)

  result.warnings.extend(_check_sellers(order, result.verified))
  result.reconciliation.extend(_reconcile(order, result.verified))
  return result


def _check_sellers(
  order: Mapping[str, Any], verified: Iterable[VerifiedReceipt]
) -> list[Finding]:
  """Platform Verification 3 (SHOULD), reported as warnings.

  When `event.seller_id` is present: succeeded captures must not exceed that
  seller's attributed total, and refunds must not exceed that seller's
  captures. A `seller_id` that does not resolve in the Order's `sellers` map
  is not checked: ucp#1018 says the platform "MUST treat seller attribution
  for that scope as unavailable". Neither is a seller with no attributed
  line items, which has no attributed total to compare with.
  """
  sellers = order.get("sellers") or {}
  attributed = {
    seller: total
    for seller, total in _attributed_totals(order).items()
    if seller in sellers
  }
  captured: dict[str, int] = {}
  refunded: dict[str, int] = {}
  findings = []
  for receipt in verified:
    event = receipt.payload["event"]
    seller = event.get("seller_id")
    if seller is None or seller not in attributed:
      continue
    if event["status"] != "succeeded":
      continue
    if event["type"] == "capture":
      captured[seller] = captured.get(seller, 0) + event["amount"]
    elif event["type"] == "capture_reversal":
      captured[seller] = captured.get(seller, 0) - event["amount"]
    elif event["type"] == "refund":
      refunded[seller] = refunded.get(seller, 0) + event["amount"]
    else:
      continue
    if captured.get(seller, 0) > attributed[seller]:
      findings.append(
        Finding(
          receipt.index,
          RECEIPT_EXCEEDS_SELLER_TOTAL,
          f"captures for {seller} ({captured[seller]}) exceed its "
          f"attributed total ({attributed[seller]})",
        )
      )
    if refunded.get(seller, 0) > captured.get(seller, 0):
      findings.append(
        Finding(
          receipt.index,
          RECEIPT_EXCEEDS_SELLER_TOTAL,
          f"refunds for {seller} ({refunded[seller]}) exceed its captures "
          f"({captured.get(seller, 0)})",
        )
      )
  return findings


def _reconcile(
  order: Mapping[str, Any], verified: Iterable[VerifiedReceipt]
) -> list[Finding]:
  """Platform Verification 4 (SHOULD): refunds against Order adjustments.

  Each `refund` receipt that carries `event.ref.adjustment_id` is compared
  with that adjustment. A mismatch leaves the receipt valid.
  """
  adjustments = {a["id"]: a for a in order.get("adjustments", [])}
  findings = []
  for receipt in verified:
    event = receipt.payload["event"]
    adjustment_id = (event.get("ref") or {}).get("adjustment_id")
    if event["type"] != "refund" or adjustment_id is None:
      continue
    adjustment = adjustments.get(adjustment_id)
    expected = None if adjustment is None else _adjustment_amount(adjustment)
    if expected is None:
      # Not reconcilable: no such adjustment, or no single `total`.
      continue
    if event["amount"] != expected:
      findings.append(
        Finding(
          receipt.index,
          RECONCILIATION_ADJUSTMENT_MISMATCH,
          f"refund of {event['amount']} cites adjustment {adjustment_id}, "
          f"whose total is {expected}",
        )
      )
  return findings
