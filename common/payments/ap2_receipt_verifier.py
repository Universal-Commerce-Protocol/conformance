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

* `outcomes`: the MUST checks of "Platform Verification" 1-6;
* `warnings`: the per-seller SHOULD of "Platform Verification" 7
  (`receipt_exceeds_seller_total`);
* `reconciliation`: the SHOULD of "Platform Verification" 8
  (`reconciliation_adjustment_mismatch`). It is commercial, not
  cryptographic, so a receipt it flags stays valid.

Only ECDSA is implemented (ES256, ES384, ES512): the extension's algorithm
rule follows AP2 v0.2, which requires it ("Cryptographic Requirements").

The verifier never requires JCS form: the signature and `prev` cover the
bytes as sent ("Hashes and Chaining").

Where the specification leaves a choice open, this verifier:

* reports a receipt whose `iss` has no known keys as
  `receipt_invalid_signature` (its signature "cannot be verified"), like an
  unresolved `kid`;
* treats a second receipt for a `seq` the chain already holds as a repeat,
  and so as a sequence gap, only if its bytes differ ("A second, different
  receipt"); a byte-identical copy is ignored. The first receipt held stays
  in the chain; the second is neither chained nor counted in the totals;
* recomputes totals cumulatively, receipt by receipt, over the chain's
  succeeded events, so each receipt's stated `totals` are compared with the
  state right after that receipt;
* takes the mandate's `payment_amount` (when the caller knows it) as the
  `mandated` that `authorized` and `captured` are held to, not the amount
  the receipt states;
* reconciles a refund against the magnitude of the cited adjustment's
  `total`, because adjustment totals are signed and receipt amounts are not,
  once per adjustment, at the last succeeded refund that cites it; an
  adjustment cited only by pending or failed refunds is not checked;
* checks a seller only when it resolves in `sellers` and has attributed
  line items;
* records a signed receipt that is not a Receipt Payload in `malformed`
  rather than as an outcome, since no outcome covers it. That covers a
  missing member, a wrong type, a negative `iat`, amount or total, a `seq`
  below 1, an `event.type` or `event.status` outside its enum, and an
  `event.ref` or id of the wrong type. `ap2.receipts` that is not an array
  is recorded there too, at index -1; `null` is no receipts;
* treats an empty `processing_entry_id` or `adjustment_id` as absent;
* ignores Order data it cannot read (`line_items`, `adjustments`, `sellers`
  of the wrong type, or entries without the members it needs) rather than
  raising.
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
RECEIPT_TOTALS_MISMATCH = "receipt_totals_mismatch"
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
# `receipt_event.type` and `receipt_event.status` enums.
EVENT_TYPES = (
  "authorization",
  "authorization_adjustment",
  "capture",
  "capture_reversal",
  "release",
  "refund",
)
EVENT_STATUSES = ("pending", "succeeded", "failed")

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
  """Totals recomputed from a chain's succeeded events ("Running Totals")."""

  authorized: int = 0
  captured: int = 0
  refunded: int = 0

  def after(self, event: Mapping[str, Any]) -> "Totals":
    """Return the totals once a succeeded `event` counts."""
    amount = event["amount"]
    kind = event["type"]
    return Totals(
      self.authorized
      + (amount if kind in ("authorization", "authorization_adjustment") else 0)
      - (amount if kind == "release" else 0),
      self.captured
      + (amount if kind == "capture" else 0)
      - (amount if kind == "capture_reversal" else 0),
      self.refunded + (amount if kind == "refund" else 0),
    )


@dataclasses.dataclass(frozen=True)
class VerifiedReceipt:
  """A receipt whose signature and mandate binding both verified."""

  index: int
  compact: str
  header: dict[str, Any]
  payload: dict[str, Any]
  derived_totals: Totals
  # False for a follow-up to a movement already counted ("Running Totals",
  # "Each movement counts once") and for events that are not succeeded.
  counted: bool = True

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
    payment_amount: The mandate's `payment_amount` in minor units, which
      `totals.mandated` must equal. None if the caller cannot read it; the
      stated `mandated` is then not checked.

  """

  credential_token: str
  business: str
  payment_amount: int | None = None

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
  jwk = next(
    (
      k
      for k in issuer_keys
      if kid and isinstance(k, Mapping) and k.get("kid") == kid
    ),
    None,
  )
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


def _is_int(value: Any, minimum: int | None = None) -> bool:
  """Return whether `value` is an integer (not a bool), at least `minimum`."""
  return (
    isinstance(value, int)
    and not isinstance(value, bool)
    and (minimum is None or value >= minimum)
  )


def _ref_id(event: Mapping[str, Any], name: str) -> str | None:
  """Return `event.ref[name]`, or None if it is absent or an empty string."""
  ref = event.get("ref")
  value = ref.get(name) if isinstance(ref, Mapping) else None
  return value if isinstance(value, str) and value else None


def _payload_problems(payload: Mapping[str, Any]) -> list[str]:
  """List required members a payload lacks or has with the wrong value.

  Checks the members the verifier reads against `receipt_payload`,
  `receipt_event` and `receipt_totals`: types, minimums (`iat` >= 0,
  `seq` >= 1, amounts and totals >= 0) and the `type` and `status` enums.
  The optional `event.seller_id`, `event.ref` and its ids are checked when
  present.
  """
  event = payload.get("event")
  totals = payload.get("totals")
  # Member name -> whether its value is acceptable.
  checks: dict[str, bool] = {
    "iss": isinstance(payload.get("iss"), str),
    "iat": _is_int(payload.get("iat"), 0),
    "order_id": isinstance(payload.get("order_id"), str),
    "mandate_ref": isinstance(payload.get("mandate_ref"), str),
    "seq": _is_int(payload.get("seq"), 1),
    "prev": payload.get("prev") is None or isinstance(payload.get("prev"), str),
    "event": isinstance(event, dict),
    "totals": isinstance(totals, dict),
  }
  if isinstance(event, dict):
    for member in REQUIRED_EVENT_MEMBERS:
      value = event.get(member)
      checks[f"event.{member}"] = (
        _is_int(value, 0) if member == "amount" else isinstance(value, str)
      )
    if checks["event.type"]:
      checks["event.type"] = event["type"] in EVENT_TYPES
    if checks["event.status"]:
      checks["event.status"] = event["status"] in EVENT_STATUSES
    if event.get("seller_id") is not None:
      checks["event.seller_id"] = isinstance(event["seller_id"], str)
    ref = event.get("ref")
    if ref is not None:
      checks["event.ref"] = isinstance(ref, dict)
      if isinstance(ref, dict):
        for member in ("processing_entry_id", "adjustment_id"):
          if ref.get(member) is not None:
            checks[f"event.ref.{member}"] = isinstance(ref[member], str)
  if isinstance(totals, dict):
    for member in REQUIRED_TOTALS_MEMBERS:
      checks[f"totals.{member}"] = _is_int(totals.get(member), 0)
  problems = [m for m in REQUIRED_PAYLOAD_MEMBERS if m not in payload]
  problems += [
    name
    for name, ok in checks.items()
    if not ok and name.split(".")[0] in payload and name not in problems
  ]
  return problems


def _seller_of_line(line_item: Mapping[str, Any], order: Mapping[str, Any]):
  """Return the seller of record of an Order line item.

  Seller attribution (ucp#1018): `line_items[].seller_id` overrides the
  order-level `seller_id`. Anything but a string is no seller.
  """
  seller = line_item.get("seller_id", order.get("seller_id"))
  return seller if isinstance(seller, str) else None


def _total_amounts(totals: Any) -> list[int]:
  """Return the integer `amount` of each `type: total` entry in `totals`."""
  if not isinstance(totals, list):
    return []
  return [
    t["amount"]
    for t in totals
    if isinstance(t, Mapping)
    and t.get("type") == "total"
    and _is_int(t.get("amount"))
  ]


def _attributed_totals(order: Mapping[str, Any]) -> dict[str, int]:
  """Sum each seller's line item `total` amounts."""
  totals: dict[str, int] = {}
  line_items = order.get("line_items")
  for line_item in line_items if isinstance(line_items, list) else []:
    if not isinstance(line_item, Mapping):
      continue
    seller = _seller_of_line(line_item, order)
    if seller is None:
      continue
    amount = sum(_total_amounts(line_item.get("totals")))
    totals[seller] = totals.get(seller, 0) + amount
  return totals


def _adjustment_amount(adjustment: Mapping[str, Any]) -> int | None:
  """Return the magnitude of an adjustment's `total`, if it has one.

  Adjustment totals are signed, negative for money returned to the buyer
  (adjustment.json), while a receipt `amount` is always non-negative.
  """
  totals = _total_amounts(adjustment.get("totals"))
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
  # Per (iss, mandate_ref) chain: the last chained receipt, the receipts
  # held by `seq`, and the (type, processing_entry_id) pairs already counted.
  chain_tail: dict[ChainKey, VerifiedReceipt] = {}
  held: dict[ChainKey, dict[int, str]] = {}
  counted: dict[ChainKey, set[tuple[str, str]]] = {}

  ap2 = order.get("ap2")
  receipts = ap2.get("receipts") if isinstance(ap2, Mapping) else None
  if receipts is None:
    receipts = []  # `ap2.receipts: null` is no receipts.
  elif not isinstance(receipts, list):
    # Not an array: nothing to iterate. Index -1 stands for the field.
    result.malformed.append((-1, "ap2.receipts is not an array"))
    receipts = []
  for index, compact in enumerate(receipts):
    try:
      header, payload = decode_receipt(compact)
    except ValueError as exc:
      result.outcomes.append(
        Finding(index, RECEIPT_INVALID_SIGNATURE, f"not a compact JWS: {exc}")
      )
      continue

    # Platform Verification 1: the signature, against the keys of `iss`. A
    # wrong or missing `typ` and a `kid` that does not resolve are
    # `receipt_invalid_signature` too. Platform Verification 3: a receipt
    # that fails is left out of its chain.
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

    # Platform Verification 2: only for a receipt whose signature verified,
    # `mandate_ref` must be a Payment Mandate the platform submitted and
    # `iss` the business that signed the checkout it covers. Rule 3 leaves
    # a failing receipt out of its chain.
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

    # Platform Verification 4: `seq` and `prev` continuity per
    # (iss, mandate_ref). `seq` starts at 1 and increases by exactly 1;
    # `prev` is the hash of the previous receipt's compact JWS, null at 1.
    # A second, different receipt for a `seq` already held is a gap; both
    # are kept (the platform keeps every receipt it received).
    chain: ChainKey = (payload["iss"], payload["mandate_ref"])
    seq = payload["seq"]
    chain_held = held.setdefault(chain, {})
    if seq in chain_held:
      if chain_held[seq] != compact:
        result.outcomes.append(
          Finding(
            index,
            RECEIPT_SEQUENCE_GAP,
            f"a different receipt for seq {seq} is already held",
          )
        )
      continue
    chain_held[seq] = compact
    tail = chain_tail.get(chain)
    expected_seq = 1 if tail is None else tail.payload["seq"] + 1
    expected_prev = None if tail is None else sha256_ref(tail.compact)
    if seq != expected_seq:
      result.outcomes.append(
        Finding(
          index, RECEIPT_SEQUENCE_GAP, f"seq {seq}, expected {expected_seq}"
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

    # Running Totals: only a `succeeded` event changes the totals, and each
    # movement counts once: a later succeeded receipt with the same `type`
    # and `event.ref.processing_entry_id` is a follow-up.
    event = payload["event"]
    previous = Totals() if tail is None else tail.derived_totals
    entry_id = _ref_id(event, "processing_entry_id")
    key = (event["type"], entry_id)
    chain_counted = counted.setdefault(chain, set())
    counts = event["status"] == "succeeded" and not (
      entry_id is not None and key in chain_counted
    )
    if counts:
      recomputed = previous.after(event)
      if entry_id is not None:
        chain_counted.add(key)
    else:
      recomputed = previous

    # Platform Verification 5: stated totals against the recomputed ones,
    # and `mandated` against the mandate's `payment_amount`.
    stated = payload["totals"]
    differences = [
      f"{name} {stated[name]} (recomputed {getattr(recomputed, name)})"
      for name in ("authorized", "captured", "refunded")
      if stated[name] != getattr(recomputed, name)
    ]
    if mandate.payment_amount is not None and (
      stated["mandated"] != mandate.payment_amount
    ):
      differences.append(
        f"mandated {stated['mandated']} (payment_amount "
        f"{mandate.payment_amount})"
      )
    if differences:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_TOTALS_MISMATCH,
          "stated totals differ: " + "; ".join(differences),
        )
      )

    # Platform Verification 6: on the recomputed totals, `authorized` and
    # `captured` against `mandated`, and `refunded` against `captured`.
    mandated = (
      stated["mandated"]
      if mandate.payment_amount is None
      else mandate.payment_amount
    )
    for name in ("authorized", "captured"):
      if getattr(recomputed, name) > mandated:
        result.outcomes.append(
          Finding(
            index,
            RECEIPT_EXCEEDS_MANDATE,
            f"{name} {getattr(recomputed, name)} > mandated {mandated}",
          )
        )
    if recomputed.refunded > recomputed.captured:
      result.outcomes.append(
        Finding(
          index,
          RECEIPT_REFUND_EXCEEDS_CAPTURE,
          f"refunded {recomputed.refunded} > captured {recomputed.captured}",
        )
      )

    verified = VerifiedReceipt(
      index, compact, header, payload, recomputed, counts
    )
    chain_tail[chain] = verified
    result.verified.append(verified)

  result.warnings.extend(_check_sellers(order, result.verified))
  result.reconciliation.extend(_reconcile(order, result.verified))
  return result


def _check_sellers(
  order: Mapping[str, Any], verified: Iterable[VerifiedReceipt]
) -> list[Finding]:
  """Platform Verification 7 (SHOULD), reported as warnings.

  When `event.seller_id` is present: succeeded captures must not exceed that
  seller's attributed total, and refunds must not exceed that seller's
  captures. A `seller_id` that does not resolve in the Order's `sellers` map
  is not checked: ucp#1018 says the platform "MUST treat seller attribution
  for that scope as unavailable". Neither is a seller with no attributed
  line items, which has no attributed total to compare with.
  """
  sellers = order.get("sellers")
  if not isinstance(sellers, Mapping):
    sellers = {}
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
    if seller is None or seller not in attributed or not receipt.counted:
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
  """Platform Verification 8 (SHOULD): refunds against Order adjustments.

  For each `event.ref.adjustment_id` cited by a succeeded refund, the sum of
  `event.amount` over the succeeded refund receipts citing it is compared
  with the magnitude of that adjustment's `total`. Pending and failed
  receipts are not counted, nor is a follow-up to a movement already
  counted. A cited `adjustment_id` that matches no adjustment is a mismatch
  too. A mismatch leaves the receipts valid.
  """
  listed = order.get("adjustments")
  adjustments = {
    a["id"]: a
    for a in (listed if isinstance(listed, list) else [])
    if isinstance(a, Mapping) and isinstance(a.get("id"), str)
  }
  cited: dict[str, list[VerifiedReceipt]] = {}
  for receipt in verified:
    event = receipt.payload["event"]
    adjustment_id = _ref_id(event, "adjustment_id")
    if (
      event["type"] == "refund"
      and event["status"] == "succeeded"
      and adjustment_id is not None
    ):
      cited.setdefault(adjustment_id, []).append(receipt)

  findings = []
  for adjustment_id, receipts in cited.items():
    last = receipts[-1]
    adjustment = adjustments.get(adjustment_id)
    if adjustment is None:
      findings.append(
        Finding(
          last.index,
          RECONCILIATION_ADJUSTMENT_MISMATCH,
          f"adjustment {adjustment_id} does not exist on the Order",
        )
      )
      continue
    expected = _adjustment_amount(adjustment)
    if expected is None:
      # Not reconcilable: the adjustment has no single `total`.
      continue
    refunded = sum(r.payload["event"]["amount"] for r in receipts if r.counted)
    if refunded != expected:
      findings.append(
        Finding(
          last.index,
          RECONCILIATION_ADJUSTMENT_MISMATCH,
          f"succeeded refunds citing adjustment {adjustment_id} sum to "
          f"{refunded}, whose total is {expected}",
        )
      )
  return findings
