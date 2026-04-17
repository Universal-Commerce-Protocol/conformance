#!/usr/bin/env python3
# ruff: noqa: T201
# Copyright 2026 UCP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""UCP Readiness Validator - validates merchant UCP endpoint readiness.

A lightweight CLI tool that fetches a merchant's /.well-known/ucp
discovery profile and validates it against the UCP specification.

Usage:
  uv run ucp_validate.py https://merchant.example.com
  uv run ucp_validate.py http://localhost:8182 --smoke
  uv run ucp_validate.py https://merchant.example.com --json
  uv run ucp_validate.py https://merchant.example.com --skip-urls
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from ucp_sdk.models.schemas.ucp import BusinessSchema, ReverseDomainName

# Type alias for readability
UcpProfile = BusinessSchema


# -- Constants ---------------------------------------------------------------

CURRENT_SPEC_VERSION = "2026-01-23"
KNOWN_VERSIONS = {"2026-01-11", "2026-01-23"}

REQUIRED_CAPABILITIES = {
    "dev.ucp.shopping.checkout",
}

KNOWN_CAPABILITIES = {
    "dev.ucp.shopping.checkout",
    "dev.ucp.shopping.order",
    "dev.ucp.shopping.discount",
    "dev.ucp.shopping.fulfillment",
}

OPTIONAL_CAPABILITIES = {
    "dev.ucp.shopping.buyer_consent",
    "dev.ucp.shopping.binding",
    "dev.ucp.shopping.card_credential",
    "dev.ucp.shopping.ap2_mandate",
}

REQUIRED_JWK_FIELDS = {"kid", "kty"}
EC_JWK_FIELDS = {"crv", "x", "y"}
RSA_JWK_FIELDS = {"n", "e"}


# -- Data model --------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a single validation check."""

    name: str
    status: str  # pass, warn, fail, skip, info
    detail: str = ""
    section: str = ""


@dataclass
class ValidationReport:
    """Complete validation report."""

    endpoint: str
    timestamp: str
    version: str | None = None
    overall_status: str = "unknown"
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, check: CheckResult) -> None:
        """Append a check result to the report."""
        self.checks.append(check)

    @property
    def summary(self) -> dict[str, int]:
        """Return counts of each check status."""
        counts: dict[str, int] = {
            "pass": 0,
            "warn": 0,
            "fail": 0,
            "skip": 0,
            "info": 0,
        }
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        return counts

    def compute_status(self) -> str:
        """Compute overall READY/NOT_READY status."""
        s = self.summary
        if s["fail"] > 0:
            return "NOT_READY"
        return "READY"


# -- ANSI colors (degrade gracefully on Windows) ----------------------------

try:
    import os

    _no_color = os.environ.get("NO_COLOR") is not None
except Exception:
    _no_color = True

if sys.platform == "win32" and not _no_color:
    import contextlib

    with contextlib.suppress(Exception):
        os.system("")  # enable ANSI on Windows


def _c(code: str, text: str) -> str:
    if _no_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    """Return green-colored text."""
    return _c("32", t)


def yellow(t: str) -> str:
    """Return yellow-colored text."""
    return _c("33", t)


def red(t: str) -> str:
    """Return red-colored text."""
    return _c("31", t)


def cyan(t: str) -> str:
    """Return cyan-colored text."""
    return _c("36", t)


def bold(t: str) -> str:
    """Return bold text."""
    return _c("1", t)


def dim(t: str) -> str:
    """Return dim text."""
    return _c("2", t)


STATUS_BADGE = {
    "pass": green("[PASS]"),
    "warn": yellow("[WARN]"),
    "fail": red("[FAIL]"),
    "skip": dim("[SKIP]"),
    "info": cyan("[INFO]"),
}


# -- Validators --------------------------------------------------------------


def validate_discovery(
    base_url: str,
    timeout: float,
    report: ValidationReport,
    verbose: bool = False,
) -> UcpProfile | None:
    """Fetch and parse /.well-known/ucp."""
    well_known = f"{base_url.rstrip('/')}/.well-known/ucp"

    # Check reachability
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(well_known)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except httpx.ConnectError as e:
        report.add(
            CheckResult(
                "discovery.reachable",
                "fail",
                f"Connection refused: {e}",
                "Discovery",
            )
        )
        return None
    except httpx.TimeoutException:
        report.add(
            CheckResult(
                "discovery.reachable",
                "fail",
                f"Timeout after {timeout}s",
                "Discovery",
            )
        )
        return None
    except Exception as e:
        report.add(
            CheckResult(
                "discovery.reachable",
                "fail",
                f"HTTP error: {e}",
                "Discovery",
            )
        )
        return None

    if resp.status_code != 200:
        report.add(
            CheckResult(
                "discovery.reachable",
                "fail",
                f"HTTP {resp.status_code} (expected 200)",
                "Discovery",
            )
        )
        return None

    report.add(
        CheckResult(
            "discovery.reachable",
            "pass",
            f"200 OK, {elapsed_ms}ms",
            "Discovery",
        )
    )

    # Parse JSON
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        report.add(
            CheckResult(
                "discovery.valid_json",
                "fail",
                f"Invalid JSON: {e}",
                "Discovery",
            )
        )
        return None

    report.add(
        CheckResult(
            "discovery.valid_json",
            "pass",
            "Valid JSON response",
            "Discovery",
        )
    )

    # Parse as UcpProfile (SDK v0.3.0: parse the "ucp" key, not the full response)
    ucp_data = data.get("ucp", data)
    try:
        profile = UcpProfile(**ucp_data)
    except ValidationError as e:
        detail = str(e)
        if not verbose:
            # Truncate for readability
            lines = detail.split("\n")
            if len(lines) > 5:
                detail = "\n".join(lines[:5]) + f"\n... ({len(lines)-5} more)"
        report.add(
            CheckResult(
                "discovery.schema",
                "fail",
                f"Schema validation failed:\n{detail}",
                "Discovery",
            )
        )
        return None

    report.add(
        CheckResult(
            "discovery.schema",
            "pass",
            "Parses as UcpProfile",
            "Discovery",
        )
    )

    return profile


def validate_version(
    profile: UcpProfile,
    report: ValidationReport,
) -> None:
    """Check schema version and structural consistency."""
    version = profile.version.root
    report.version = version

    if version not in KNOWN_VERSIONS:
        report.add(
            CheckResult(
                "version.known",
                "warn",
                f"Unknown version: {version} "
                f"(known: {', '.join(sorted(KNOWN_VERSIONS))})",
                "Schema Version",
            )
        )
    elif version == CURRENT_SPEC_VERSION:
        report.add(
            CheckResult(
                "version.known",
                "pass",
                f"Version: {version} (current)",
                "Schema Version",
            )
        )
    else:
        report.add(
            CheckResult(
                "version.known",
                "warn",
                f"Version: {version} (not current; "
                f"latest is {CURRENT_SPEC_VERSION})",
                "Schema Version",
            )
        )

    # Check structural consistency
    services_val = profile.services
    caps_val = profile.capabilities

    # Detect service structure
    sample_svc = (
        next(iter(services_val.values()), None)
        if services_val
        else None
    )
    svc_format = (
        "list" if isinstance(sample_svc, list) else "object"
    )

    # Detect capabilities structure
    cap_format = (
        "dict" if isinstance(caps_val, dict) else "list"
    )

    if version == "2026-01-23":
        expected_svc = "list"
        expected_cap = "dict"
    else:
        expected_svc = "object"
        expected_cap = "list"

    if svc_format == expected_svc:
        report.add(
            CheckResult(
                "version.services_structure",
                "pass",
                f"Services: {svc_format} format (matches {version})",
                "Schema Version",
            )
        )
    else:
        report.add(
            CheckResult(
                "version.services_structure",
                "warn",
                f"Services: {svc_format} format "
                f"(expected {expected_svc} for {version})",
                "Schema Version",
            )
        )

    if cap_format == expected_cap:
        report.add(
            CheckResult(
                "version.capabilities_structure",
                "pass",
                f"Capabilities: {cap_format} format (matches {version})",
                "Schema Version",
            )
        )
    else:
        report.add(
            CheckResult(
                "version.capabilities_structure",
                "warn",
                f"Capabilities: {cap_format} format "
                f"(expected {expected_cap} for {version})",
                "Schema Version",
            )
        )


def _extract_capability_names(
    profile: UcpProfile,
) -> set[str]:
    """Extract capability names from the profile."""
    caps = profile.capabilities
    if isinstance(caps, dict):
        return {str(k.root) if hasattr(k, "root") else str(k) for k in caps.keys()}
    else:
        return {c.name for c in caps if c.name}


def validate_capabilities(
    profile: UcpProfile,
    report: ValidationReport,
) -> None:
    """Check required and declared capabilities."""
    present = _extract_capability_names(profile)

    # Check required capabilities (only checkout is required)
    for cap_name in sorted(REQUIRED_CAPABILITIES):
        if cap_name in present:
            report.add(
                CheckResult(
                    f"capabilities.{cap_name}",
                    "pass",
                    f"{cap_name} (required)",
                    "Capabilities",
                )
            )
        else:
            report.add(
                CheckResult(
                    f"capabilities.{cap_name}",
                    "fail",
                    f"{cap_name} (required, missing)",
                    "Capabilities",
                )
            )

    # Report known capabilities that are declared (pass = declared, info = not)
    known_optional = KNOWN_CAPABILITIES - REQUIRED_CAPABILITIES
    for cap_name in sorted(known_optional):
        if cap_name in present:
            report.add(
                CheckResult(
                    f"capabilities.{cap_name}",
                    "pass",
                    f"{cap_name} (declared)",
                    "Capabilities",
                )
            )

    # Report optional spec capabilities
    for cap_name in sorted(OPTIONAL_CAPABILITIES):
        if cap_name in present:
            report.add(
                CheckResult(
                    f"capabilities.{cap_name}",
                    "info",
                    f"{cap_name} (optional, present)",
                    "Capabilities",
                )
            )

    # Report extra (unknown) capabilities as info
    known = KNOWN_CAPABILITIES | OPTIONAL_CAPABILITIES | REQUIRED_CAPABILITIES
    extras = present - known
    for cap_name in sorted(extras):
        report.add(
            CheckResult(
                f"capabilities.{cap_name}",
                "info",
                f"{cap_name} (custom)",
                "Capabilities",
            )
        )


def validate_handlers(
    profile: UcpProfile,
    report: ValidationReport,
) -> None:
    """Check payment handler declarations."""
    if not profile.payment_handlers:
        report.add(
            CheckResult(
                "handlers.present",
                "warn",
                "No payment handlers declared",
                "Payment Handlers",
            )
        )
        return

    handler_count = 0
    for group_name, handler_list in profile.payment_handlers.items():
        for h in handler_list:
            handler_count += 1
    report.add(
        CheckResult(
            "handlers.present",
            "pass",
            f"{handler_count} handler(s) declared",
            "Payment Handlers",
        )
    )

    for group_name, handler_list in profile.payment_handlers.items():
        for h in handler_list:
            handler_id = h.id if hasattr(h, "id") and h.id else "unknown"
            handler_name = getattr(h, "name", None)

            label = handler_id
            if handler_name:
                label = f"{handler_id} ({handler_name})"

            if hasattr(h, "config") and h.config:
                report.add(
                    CheckResult(
                        f"handlers.{handler_id}.config",
                        "pass",
                        f"{label} - config present "
                        f"({', '.join(h.config.keys())})",
                    "Payment Handlers",
                )
            )
        else:
            report.add(
                CheckResult(
                    f"handlers.{handler_id}.config",
                    "info",
                    f"{label} - no config (may be OK)",
                    "Payment Handlers",
                )
            )


def _extract_urls(profile: UcpProfile) -> list[tuple[str, str]]:
    """Extract all spec/schema URLs from the discovery profile."""
    urls: list[tuple[str, str]] = []

    # Services (dict[ReverseDomainName, list[ServiceBusinessSchema]])
    for svc_name, svc_list in profile.services.items():
        services = svc_list if isinstance(svc_list, list) else [svc_list]
        for idx, svc in enumerate(services):
            s = svc.root if hasattr(svc, "root") else svc
            prefix = f"services['{svc_name}'][{idx}]"
            if hasattr(s, "spec") and s.spec:
                urls.append((f"{prefix}.spec", str(s.spec)))
            if hasattr(s, "schema_") and s.schema_:
                urls.append((f"{prefix}.schema", str(s.schema_)))

    # Capabilities (dict[ReverseDomainName, list[CapabilityBusinessSchema]])
    caps = profile.capabilities
    if isinstance(caps, dict):
        for cap_name, cap_list in caps.items():
            cap_items = cap_list if isinstance(cap_list, list) else [cap_list]
            for i, cap in enumerate(cap_items):
                c = cap.root if hasattr(cap, "root") else cap
                prefix = f"capabilities['{cap_name}'][{i}]"
                if hasattr(c, "spec") and c.spec:
                    urls.append((f"{prefix}.spec", str(c.spec)))
                if hasattr(c, "schema_") and c.schema_:
                    urls.append((f"{prefix}.schema", str(c.schema_)))

    # Payment handler schemas
    if profile.payment_handlers:
        for group_name, handler_list in profile.payment_handlers.items():
            for h in handler_list:
                hid = h.id if hasattr(h, "id") else "unknown"
                if hasattr(h, "config_schema") and h.config_schema:
                    urls.append(
                        (f"handlers['{hid}'].config_schema", str(h.config_schema))
                    )
                if hasattr(h, "instrument_schemas") and h.instrument_schemas:
                    for j, ischema in enumerate(h.instrument_schemas):
                        urls.append(
                            (
                                f"handlers['{hid}'].instrument_schemas[{j}]",
                                str(ischema),
                            )
                        )
                if hasattr(h, "spec") and h.spec:
                    urls.append(
                        (f"handlers['{hid}'].spec", str(h.spec))
                    )

    return urls


def validate_urls(
    profile: UcpProfile,
    report: ValidationReport,
    timeout: float,
    verbose: bool = False,
) -> None:
    """Check reachability of all spec/schema URLs."""
    urls = _extract_urls(profile)
    if not urls:
        report.add(
            CheckResult(
                "urls.present",
                "info",
                "No spec/schema URLs found in profile",
                "Spec/Schema URLs",
            )
        )
        return

    reachable = 0
    failures: list[str] = []

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for label, url in urls:
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    reachable += 1
                else:
                    failures.append(
                        f"[{label}] {url} returned {resp.status_code}"
                    )
            except Exception as e:
                failures.append(f"[{label}] {url} error: {e}")

    total = len(urls)
    if failures:
        detail = f"{reachable}/{total} reachable"
        if verbose:
            detail += "\n  " + "\n  ".join(failures)
        else:
            detail += f" ({len(failures)} failed; use --verbose for details)"
        report.add(
            CheckResult(
                "urls.reachable",
                "warn",
                detail,
                "Spec/Schema URLs",
            )
        )
    else:
        report.add(
            CheckResult(
                "urls.reachable",
                "pass",
                f"{total}/{total} reachable",
                "Spec/Schema URLs",
            )
        )


def validate_signing_keys(
    profile: UcpProfile,
    report: ValidationReport,
) -> None:
    """Validate signing keys if present."""
    signing_keys = getattr(profile, "signing_keys", None)
    if not signing_keys:
        report.add(
            CheckResult(
                "signing_keys.present",
                "info",
                "No signing keys declared (optional)",
                "Signing Keys",
            )
        )
        return

    keys = signing_keys
    report.add(
        CheckResult(
            "signing_keys.present",
            "pass",
            f"{len(keys)} key(s) declared",
            "Signing Keys",
        )
    )

    for key in keys:
        kid = key.kid
        kty = key.kty

        if not kid or not kty:
            report.add(
                CheckResult(
                    f"signing_keys.{kid or 'unknown'}.format",
                    "fail",
                    "Missing required fields: kid, kty",
                    "Signing Keys",
                )
            )
            continue

        if kty == "EC":
            missing = []
            if not key.crv:
                missing.append("crv")
            if not key.x:
                missing.append("x")
            if not key.y:
                missing.append("y")
            if missing:
                report.add(
                    CheckResult(
                        f"signing_keys.{kid}.format",
                        "fail",
                        f"EC key missing: {', '.join(missing)}",
                        "Signing Keys",
                    )
                )
            else:
                report.add(
                    CheckResult(
                        f"signing_keys.{kid}.format",
                        "pass",
                        f"Key '{kid}' - valid EC {key.crv} JWK",
                        "Signing Keys",
                    )
                )
        elif kty == "RSA":
            missing = []
            if not key.n:
                missing.append("n")
            if not key.e:
                missing.append("e")
            if missing:
                report.add(
                    CheckResult(
                        f"signing_keys.{kid}.format",
                        "fail",
                        f"RSA key missing: {', '.join(missing)}",
                        "Signing Keys",
                    )
                )
            else:
                report.add(
                    CheckResult(
                        f"signing_keys.{kid}.format",
                        "pass",
                        f"Key '{kid}' - valid RSA JWK",
                        "Signing Keys",
                    )
                )
        else:
            report.add(
                CheckResult(
                    f"signing_keys.{kid}.format",
                    "info",
                    f"Key '{kid}' - type '{kty}' "
                    f"(not EC or RSA; skipping field validation)",
                    "Signing Keys",
                )
            )


def _find_rest_endpoint(profile: UcpProfile) -> str | None:
    """Find the REST shopping endpoint from the discovery profile."""
    rdn = ReverseDomainName(root="dev.ucp.shopping")
    shopping = profile.services.get(rdn)
    if not shopping:
        return None

    if isinstance(shopping, list):
        for svc in shopping:
            s = svc.root if hasattr(svc, "root") else svc
            if hasattr(s, "transport") and s.transport == "rest" and s.endpoint:
                return str(s.endpoint)
            if hasattr(s, "rest") and s.rest and s.rest.endpoint:
                return str(s.rest.endpoint)
    return None


def validate_smoke(
    profile: UcpProfile,
    report: ValidationReport,
    timeout: float,
    verbose: bool = False,
) -> None:
    """Run a lightweight smoke test against the checkout endpoint."""
    endpoint = _find_rest_endpoint(profile)
    if not endpoint:
        report.add(
            CheckResult(
                "smoke.endpoint",
                "fail",
                "Cannot find REST shopping endpoint in profile",
                "Smoke Test",
            )
        )
        return

    checkout_url = f"{endpoint.rstrip('/')}/checkout-sessions"
    version = profile.version.root

    # Minimal checkout create payload (per spec, only line_items with
    # item.id + quantity are valid for create requests)
    payload = {
        "line_items": [
            {
                "quantity": 1,
                "item": {
                    "id": "smoke-test-item",
                },
            }
        ],
        "context": {
            "address_country": "US",
        },
    }

    headers = {
        "Content-Type": "application/json",
        "UCP-Agent": f'profile="urn:ucp:validator:smoke-test";'
        f' version="{version}"',
        "idempotency-key": str(uuid.uuid4()),
        "request-id": str(uuid.uuid4()),
        "request-signature": "test",
    }

    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.post(checkout_url, json=payload, headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        report.add(
            CheckResult(
                "smoke.checkout",
                "fail",
                f"POST {checkout_url} error: {e}",
                "Smoke Test",
            )
        )
        return

    # We expect either:
    #   201/200 = checkout created (ideal)
    #   400 = item not found (expected - smoke test uses fake item)
    #   4xx = some other validation error (still proves endpoint is alive)
    #
    # 5xx or connection errors = endpoint problem

    if resp.status_code in (200, 201):
        report.add(
            CheckResult(
                "smoke.checkout",
                "pass",
                f"Checkout created successfully "
                f"({resp.status_code}, {elapsed_ms}ms)",
                "Smoke Test",
            )
        )
        # Try to cancel it to clean up
        try:
            checkout_data = resp.json()
            checkout_id = checkout_data.get("id")
            if checkout_id:
                cancel_url = f"{checkout_url}/{checkout_id}/cancel"
                cancel_headers = {
                    **headers,
                    "idempotency-key": str(uuid.uuid4()),
                    "request-id": str(uuid.uuid4()),
                }
                client2 = httpx.Client(timeout=timeout)
                client2.post(cancel_url, headers=cancel_headers)
                client2.close()
        except Exception:
            pass
    elif resp.status_code == 400:
        report.add(
            CheckResult(
                "smoke.checkout",
                "pass",
                f"Endpoint alive - returned 400 for test item "
                f"({elapsed_ms}ms)",
                "Smoke Test",
            )
        )
        if verbose:
            try:
                detail = resp.json()
                report.add(
                    CheckResult(
                        "smoke.checkout.detail",
                        "info",
                        f"Response: {json.dumps(detail, indent=2)}",
                        "Smoke Test",
                    )
                )
            except Exception:
                pass
    elif 400 <= resp.status_code < 500:
        report.add(
            CheckResult(
                "smoke.checkout",
                "warn",
                f"Endpoint returned {resp.status_code} ({elapsed_ms}ms)"
                f" - alive but may need auth/config",
                "Smoke Test",
            )
        )
    else:
        report.add(
            CheckResult(
                "smoke.checkout",
                "fail",
                f"Endpoint returned {resp.status_code} ({elapsed_ms}ms)"
                f" - server error",
                "Smoke Test",
            )
        )


# -- Output ------------------------------------------------------------------


def print_terminal_report(report: ValidationReport) -> None:
    """Print colored terminal report."""
    print()
    print(bold("UCP Readiness Report"))
    print(bold("=" * 50))
    print(f"  Endpoint:  {report.endpoint}")
    print(f"  Timestamp: {report.timestamp}")
    if report.version:
        print(f"  Version:   {report.version}")
    print()

    # Group by section
    sections: dict[str, list[CheckResult]] = {}
    for check in report.checks:
        section = check.section or "Other"
        sections.setdefault(section, []).append(check)

    for section_name, checks in sections.items():
        print(bold(f"{section_name}"))
        for check in checks:
            badge = STATUS_BADGE.get(check.status, f"[{check.status.upper()}]")
            detail = check.detail
            # Indent multi-line details
            if "\n" in detail:
                lines = detail.split("\n")
                detail = lines[0] + "\n" + "\n".join(
                    "          " + line for line in lines[1:]
                )
            print(f"  {badge} {detail}")
        print()

    # Summary
    s = report.summary
    report.overall_status = report.compute_status()
    summary_parts = []
    if s["pass"]:
        summary_parts.append(green(f"{s['pass']} PASS"))
    if s["warn"]:
        summary_parts.append(yellow(f"{s['warn']} WARN"))
    if s["fail"]:
        summary_parts.append(red(f"{s['fail']} FAIL"))
    if s["skip"]:
        summary_parts.append(dim(f"{s['skip']} SKIP"))
    if s["info"]:
        summary_parts.append(cyan(f"{s['info']} INFO"))

    print(bold("Summary: ") + " | ".join(summary_parts))

    status_str = report.overall_status
    if status_str == "READY":
        print(bold("Status:  ") + green("READY"))
    else:
        print(bold("Status:  ") + red("NOT_READY"))
    print()


def print_json_report(report: ValidationReport) -> None:
    """Print structured JSON report."""
    report.overall_status = report.compute_status()
    output = {
        "endpoint": report.endpoint,
        "timestamp": report.timestamp,
        "version": report.version,
        "status": report.overall_status.lower(),
        "summary": report.summary,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "detail": c.detail,
                "section": c.section,
            }
            for c in report.checks
        ],
    }
    print(json.dumps(output, indent=2))


# -- Main --------------------------------------------------------------------


def main() -> int:
    """CLI entry point for UCP readiness validation."""
    parser = argparse.ArgumentParser(
        description="UCP Readiness Validator - validate merchant "
        "UCP endpoint readiness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run ucp_validate.py https://merchant.example.com\n"
            "  uv run ucp_validate.py http://localhost:8182 --smoke\n"
            "  uv run ucp_validate.py https://target.com --json\n"
            "  uv run ucp_validate.py http://localhost:8182 "
            "--skip-urls --verbose\n"
        ),
    )
    parser.add_argument("url", help="Merchant base URL")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run live checkout smoke test",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP request timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--skip-urls",
        action="store_true",
        help="Skip URL reachability checks",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed error info",
    )

    args = parser.parse_args()
    base_url = args.url.rstrip("/")

    report = ValidationReport(
        endpoint=base_url,
        timestamp=datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
    )

    # 1. Discovery
    profile = validate_discovery(
        base_url, args.timeout, report, verbose=args.verbose
    )

    if profile:
        # 2. Version
        validate_version(profile, report)

        # 3. Capabilities
        validate_capabilities(profile, report)

        # 4. Payment handlers
        validate_handlers(profile, report)

        # 5. URL reachability
        if args.skip_urls:
            report.add(
                CheckResult(
                    "urls.reachable",
                    "skip",
                    "Skipped (--skip-urls)",
                    "Spec/Schema URLs",
                )
            )
        else:
            validate_urls(
                profile, report, args.timeout, verbose=args.verbose
            )

        # 6. Signing keys
        validate_signing_keys(profile, report)

        # 7. Smoke test
        if args.smoke:
            validate_smoke(
                profile, report, args.timeout, verbose=args.verbose
            )
        else:
            report.add(
                CheckResult(
                    "smoke.checkout",
                    "skip",
                    "Use --smoke to enable",
                    "Smoke Test",
                )
            )

    # Output
    if args.json_output:
        print_json_report(report)
    else:
        print_terminal_report(report)

    # Exit code
    report.overall_status = report.compute_status()
    return 0 if report.overall_status == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
