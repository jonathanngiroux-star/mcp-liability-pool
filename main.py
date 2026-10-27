#!/usr/bin/env python3
"""MCP server: mcp-liability-pool - Agent Liability Insurance Pool.

Pooled liability coverage for agent-caused losses BEYOND SLA credits. A monthly premium buys
coverage against defined failure classes with transparent claims handling.

The layering matters: SLA credits refund fees, they do not cover losses. This pool is the third
layer of the liability stack (SLA credits -> on-call responders -> loss coverage).

Premium is computed, not quoted: a base rate per $1,000 of covered stack per failure class,
scaled by plan, exposure factors and a claims-history loading factor, then floored and capped.
Pool statistics (loss ratio, reserve adequacy) are computed from the persisted claim ledger.

State is persisted as JSON under ./state (override with MCP_LIABILITY_POOL_STATE_DIR).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SERVICE_NAME = "mcp-liability-pool"
VERSION = "1.0.0"

app = Server(SERVICE_NAME)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _state_dir() -> Path:
    override = os.environ.get("MCP_LIABILITY_POOL_STATE_DIR")
    path = Path(override) if override else Path(__file__).resolve().parent / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return _state_dir() / "pool_state.json"


def _default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "claims": [],
        "members": [],
        "premiums": [],
        "sequence": 500,
    }


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        state = _default_state()
        save_state(state)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = _default_state()
    base = _default_state()
    base.update(state)
    for key, default in _default_state().items():
        if key not in base:
            base[key] = default
    return state


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pool-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------
# Domain data
# --------------------------------------------------------------------------

FAILURE_CLASSES: dict[str, dict[str, Any]] = {
    "wrong_trade_failed_alert": {
        "class": "wrong_trade_failed_alert",
        "label": "Wrong trade from a failed alert",
        "description": (
            "An agent placed or failed to place a trade because a risk alert did not fire, "
            "and the position produced a direct financial loss."
        ),
        "base_rate_per_1000_usd": 0.42,
        "severity_weight": 3.0,
        "covered": True,
        "deductible_pct": 5.0,
    },
    "unauthorized_tool_action": {
        "class": "unauthorized_tool_action",
        "label": "Unauthorized tool action",
        "description": (
            "An agent invoked a tool or moved funds outside its authorised scope without a "
            "human approval that policy required."
        ),
        "base_rate_per_1000_usd": 0.31,
        "severity_weight": 2.5,
        "covered": True,
        "deductible_pct": 10.0,
    },
    "poisoned_baseline": {
        "class": "poisoned_baseline",
        "label": "Poisoned monitoring baseline",
        "description": (
            "A monitoring baseline was silently replaced or corrupted, so a qualifying failure "
            "was never detected and the resulting downstream loss occurred."
        ),
        "base_rate_per_1000_usd": 0.18,
        "severity_weight": 2.0,
        "covered": True,
        "deductible_pct": 7.5,
    },
    "prompt_injection_loss": {
        "class": "prompt_injection_loss",
        "label": "Prompt-injection-driven loss",
        "description": (
            "A hostile instruction injected into agent context or a tool result drove the agent "
            "into a state that caused a covered loss."
        ),
        "base_rate_per_1000_usd": 0.55,
        "severity_weight": 3.5,
        "covered": True,
        "deductible_pct": 12.5,
    },
    "missed_sla_downstream_loss": {
        "class": "missed_sla_downstream_loss",
        "label": "Missed SLA causing downstream loss",
        "description": (
            "A recovery window was missed and the outage or degradation directly caused a "
            "covered loss to a third party."
        ),
        "base_rate_per_1000_usd": 0.24,
        "severity_weight": 2.0,
        "covered": True,
        "deductible_pct": 10.0,
    },
    "data_exfiltration": {
        "class": "data_exfiltration",
        "label": "Data exfiltration incident",
        "description": (
            "Agent behaviour resulted in credentials, keys or customer data leaving the "
            "controlled environment."
        ),
        "base_rate_per_1000_usd": 0.66,
        "severity_weight": 3.5,
        "covered": False,
        "deductible_pct": 15.0,
        "exclusion_reason": (
            "Excluded from standard coverage: data exfiltration is handled through the "
            "platform's dedicated security incident process and requires separate underwriting."
        ),
    },
    "market_movement": {
        "class": "market_movement",
        "label": "Ordinary market movement",
        "description": "Loss caused by market movement rather than agent failure.",
        "base_rate_per_1000_usd": 0.0,
        "severity_weight": 0.0,
        "covered": False,
        "deductible_pct": 0.0,
        "exclusion_reason": (
            "Excluded: a covered loss must be caused by agent failure. Ordinary market risk "
            "is never covered by this pool."
        ),
    },
}

PLANS: dict[str, dict[str, Any]] = {
    "basic": {
        "plan": "basic",
        "label": "Basic",
        "monthly_premium_floor_usd": 29.0,
        "monthly_premium_cap_usd": 99.0,
        "plan_multiplier": 1.0,
        "coverage_pct": 50.0,
        "aggregate_limit_usd": 50000.0,
        "per_claim_limit_usd": 10000.0,
        "claims_timeline_days": 30,
        "covered_classes": [
            "wrong_trade_failed_alert",
            "unauthorized_tool_action",
            "poisoned_baseline",
        ],
        "price_band": "$29-99/mo premium add-on",
    },
    "standard": {
        "plan": "standard",
        "label": "Standard",
        "monthly_premium_floor_usd": 59.0,
        "monthly_premium_cap_usd": 149.0,
        "plan_multiplier": 1.35,
        "coverage_pct": 75.0,
        "aggregate_limit_usd": 250000.0,
        "per_claim_limit_usd": 60000.0,
        "claims_timeline_days": 21,
        "covered_classes": [
            "wrong_trade_failed_alert",
            "unauthorized_tool_action",
            "poisoned_baseline",
            "prompt_injection_loss",
            "missed_sla_downstream_loss",
        ],
        "price_band": "$29-199/mo premium add-on",
    },
    "pro": {
        "plan": "pro",
        "label": "Pro",
        "monthly_premium_floor_usd": 119.0,
        "monthly_premium_cap_usd": 199.0,
        "plan_multiplier": 1.8,
        "coverage_pct": 100.0,
        "aggregate_limit_usd": 1000000.0,
        "per_claim_limit_usd": 250000.0,
        "claims_timeline_days": 14,
        "covered_classes": [
            "wrong_trade_failed_alert",
            "unauthorized_tool_action",
            "poisoned_baseline",
            "prompt_injection_loss",
            "missed_sla_downstream_loss",
        ],
        "price_band": "$29-199/mo premium add-on, priced as % of the covered stack",
    },
}

EXCLUSIONS: list[str] = [
    "Ordinary market movement or market risk not caused by agent failure.",
    "Losses arising from the customer's own instructions, configuration or deliberate overrides.",
    "Incidents disclosed to the platform more than 30 days after the member became aware of them.",
    "Losses already recovered under an SLA credit, which are a first layer and not a substitute "
    "for this coverage.",
    "Data exfiltration incidents, which require separate underwriting.",
    "Fines and penalties levied by regulators on the member.",
    "Losses arising from the platform's deliberate suspension of an agent at the member's request.",
]

GOVERNANCE: dict[str, Any] = {
    "model": "pooled-governance",
    "conflict_of_interest_mitigation": [
        "Published reserves, reviewed quarterly and attested on-chain.",
        "Claim statistics published per quarter, independent of the member's tier.",
        "Third-party underwriting available for members above $500k of covered stack.",
        "A claims committee with no commercial incentive to deny a valid claim.",
        "Aggregate limits are ring-fenced: the member's cover is not reduced by other members' claims.",
    ],
    "publishing_cadence": "quarterly reserve and claims attestation",
    "relationship_to_sla": (
        "SLA credits refund fees; they do not cover losses. This pool is the third layer of the "
        "liability stack: SLA credits -> on-call responders -> loss coverage."
    ),
}

CLAIM_STATUSES = ("filed", "under_review", "approved", "denied", "paid", "withdrawn")

SEED_CLAIMS: list[dict[str, Any]] = [
    {
        "id": "clm-2026-0311-a",
        "member_id": "member-001",
        "member_name": "Northwind Trading Desk",
        "plan": "pro",
        "failure_class": "wrong_trade_failed_alert",
        "claimed_usd": 48200.0,
        "approved_usd": 48200.0,
        "paid_usd": 48200.0,
        "status": "paid",
        "filed_at": "2026-03-11T10:20:00Z",
        "decided_at": "2026-03-18T16:05:00Z",
        "resolved_at": "2026-03-27T09:40:00Z",
        "resolution_notes": "Alert suppression confirmed; claim settled above the deductible.",
    },
    {
        "id": "clm-2026-0522-b",
        "member_id": "member-002",
        "member_name": "Helios DeFi Labs",
        "plan": "standard",
        "failure_class": "prompt_injection_loss",
        "claimed_usd": 18750.0,
        "approved_usd": 12500.0,
        "paid_usd": 12500.0,
        "status": "paid",
        "filed_at": "2026-05-22T07:55:00Z",
        "decided_at": "2026-05-29T12:30:00Z",
        "resolved_at": "2026-06-04T11:15:00Z",
        "resolution_notes": "Approved after the 12.5% deductible; partially excluded.",
    },
    {
        "id": "clm-2026-0708-c",
        "member_id": "member-001",
        "member_name": "Northwind Trading Desk",
        "plan": "pro",
        "failure_class": "unauthorized_tool_action",
        "claimed_usd": 9600.0,
        "approved_usd": 0.0,
        "paid_usd": 0.0,
        "status": "denied",
        "filed_at": "2026-07-08T18:02:00Z",
        "decided_at": "2026-07-15T14:45:00Z",
        "resolved_at": None,
        "resolution_notes": "Denied: the action was explicitly authorised in the member's own runbook.",
    },
    {
        "id": "clm-2026-0829-d",
        "member_id": "member-003",
        "member_name": "Cobalt AI-forward SME",
        "plan": "basic",
        "failure_class": "poisoned_baseline",
        "claimed_usd": 7400.0,
        "approved_usd": 5200.0,
        "paid_usd": 0.0,
        "status": "approved",
        "filed_at": "2026-08-29T13:25:00Z",
        "decided_at": "2026-09-04T09:10:00Z",
        "resolved_at": None,
        "resolution_notes": "Approved; payment pending member documentation.",
    },
    {
        "id": "clm-2026-1015-e",
        "member_id": "member-002",
        "member_name": "Helios DeFi Labs",
        "plan": "standard",
        "failure_class": "missed_sla_downstream_loss",
        "claimed_usd": 21400.0,
        "approved_usd": 0.0,
        "paid_usd": 0.0,
        "status": "under_review",
        "filed_at": "2026-10-15T16:48:00Z",
        "decided_at": None,
        "resolved_at": None,
        "resolution_notes": "Under review: downstream causation evidence still outstanding.",
    },
]

SEED_MEMBERS: list[dict[str, Any]] = [
    {
        "member_id": "member-001",
        "member_name": "Northwind Trading Desk",
        "plan": "pro",
        "covered_stack_usd": 750000.0,
        "agent_count": 9,
        "autonomy_level": "high",
        "monthly_volume_usd": 4500000.0,
        "joined_at": "2026-01-15T00:00:00Z",
        "monthly_premium_usd": 178.0,
    },
    {
        "member_id": "member-002",
        "member_name": "Helios DeFi Labs",
        "plan": "standard",
        "covered_stack_usd": 320000.0,
        "agent_count": 6,
        "autonomy_level": "high",
        "monthly_volume_usd": 1800000.0,
        "joined_at": "2026-03-01T00:00:00Z",
        "monthly_premium_usd": 132.0,
    },
    {
        "member_id": "member-003",
        "member_name": "Cobalt AI-forward SME",
        "plan": "basic",
        "covered_stack_usd": 85000.0,
        "agent_count": 3,
        "autonomy_level": "medium",
        "monthly_volume_usd": 240000.0,
        "joined_at": "2026-06-12T00:00:00Z",
        "monthly_premium_usd": 64.0,
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(tool: str, message: str, **extra: Any) -> list[TextContent]:
    payload = {"status": "error", "tool": tool, "error": message}
    payload.update(extra)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _ok(tool: str, **fields: Any) -> list[TextContent]:
    payload = {"status": "ok", "tool": tool, "service": SERVICE_NAME, "version": VERSION}
    payload.update(fields)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _all_claims(state: dict[str, Any]) -> list[dict[str, Any]]:
    claims = [dict(c) for c in state.get("claims", [])] + [dict(c) for c in SEED_CLAIMS]
    claims.sort(key=lambda c: c.get("filed_at", ""), reverse=True)
    return claims


def _all_members(state: dict[str, Any]) -> list[dict[str, Any]]:
    members = [dict(m) for m in state.get("members", [])] + [
        dict(m) for m in SEED_MEMBERS
    ]
    return members


def _autonomy_factor(autonomy: str) -> float:
    return {"low": 0.85, "medium": 1.0, "high": 1.35}.get(str(autonomy).lower(), 1.0)


def _claims_history_factor(claims: list[dict[str, Any]], covered_stack_usd: float) -> tuple[float, dict[str, Any]]:
    """Loading factor from the member's own claims history."""
    if not claims or covered_stack_usd <= 0:
        return 1.0, {"claims_count": len(claims), "loss_ratio": 0.0, "loading_factor": 1.0}
    paid = sum(float(c.get("paid_usd", 0.0)) for c in claims)
    loss_ratio = paid / covered_stack_usd if covered_stack_usd else 0.0
    factor = 1.0 + min(0.75, loss_ratio * 2.0)
    return round(factor, 4), {
        "claims_count": len(claims),
        "paid_usd": round(paid, 2),
        "loss_ratio": round(loss_ratio, 4),
        "loading_factor": factor,
        "basis": "1 + min(0.75, loss_ratio x 2), capped at a 75% loading",
    }


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="get_coverage_terms",
        description=(
            "Return the pool's coverage terms for a plan: the defined failure classes and what "
            "each covers, coverage limits, deductibles, exclusions, the claims-handling "
            "procedure and timeline, the pooled-governance transparency commitments, and the "
            "relationship to SLA credits."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "enum": ["basic", "standard", "pro"],
                    "default": "standard",
                    "description": "Plan to quote.",
                },
                "failure_class": {
                    "type": "string",
                    "enum": sorted(FAILURE_CLASSES),
                    "description": "Restrict the terms to one failure class.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="get_pool_statistics",
        description=(
            "Compute pool statistics from the claim ledger: member count, covered stack, "
            "premiums collected, claims by status, amounts paid, loss ratio, reserve level and "
            "reserve adequacy, average claim severity, average time to resolution, and a "
            "per-failure-class breakdown with rates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "months": {
                    "type": "integer",
                    "default": 12,
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Look-back window in months.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="get_claim_history",
        description=(
            "Return the pool's claim records, newest first, with amounts, decisions, resolution "
            "notes and timing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "member_id": {"type": "string", "description": "Filter by member id."},
                "status": {"type": "string", "enum": list(CLAIM_STATUSES)},
                "failure_class": {"type": "string", "enum": sorted(FAILURE_CLASSES)},
                "plan": {"type": "string", "enum": ["basic", "standard", "pro"]},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    ),
    Tool(
        name="calculate_premium",
        description=(
            "Compute a monthly premium from the covered stack and the exposure: a base rate per "
            "$1,000 of covered stack per failure class, scaled by plan, coverage breadth, "
            "autonomy, agent count and volume, plus a claims-history loading factor, then floored "
            "and capped. Returns the full calculation breakdown and the annual equivalent with "
            "the ~17% discount."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "covered_stack_usd": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Value of the agent stack being covered.",
                },
                "plan": {
                    "type": "string",
                    "enum": ["basic", "standard", "pro"],
                    "default": "standard",
                    "description": "Plan.",
                },
                "failure_classes": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(FAILURE_CLASSES)},
                    "description": "Failure classes to cover; defaults to the plan's covered set.",
                },
                "agent_count": {"type": "integer", "minimum": 0, "default": 3},
                "autonomy_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                    "description": "How autonomously the covered agents act.",
                },
                "monthly_volume_usd": {
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                    "description": "Monthly transaction or trading volume handled by the stack.",
                },
                "member_id": {
                    "type": "string",
                    "description": "Optional member id, to apply that member's claims-history loading.",
                },
            },
            "required": ["covered_stack_usd"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = arguments or {}

    if name == "get_coverage_terms":
        plan_name = (arguments.get("plan") or "standard").lower()
        if plan_name not in PLANS:
            return _error(name, f"unknown plan {plan_name!r}", valid_plans=sorted(PLANS))
        plan = PLANS[plan_name]
        failure_class = arguments.get("failure_class")
        if failure_class and failure_class not in FAILURE_CLASSES:
            return _error(
                name, f"unknown failure class {failure_class!r}", valid=sorted(FAILURE_CLASSES)
            )

        classes: list[dict[str, Any]] = []
        for key in plan["covered_classes"]:
            spec = FAILURE_CLASSES[key]
            classes.append(
                {
                    "failure_class": key,
                    "label": spec["label"],
                    "description": spec["description"],
                    "covered": True,
                    "deductible_pct": spec["deductible_pct"],
                    "max_payout_usd": min(plan["per_claim_limit_usd"], plan["aggregate_limit_usd"]),
                    "base_rate_per_1000_usd": spec["base_rate_per_1000_usd"],
                }
            )
        for key, spec in FAILURE_CLASSES.items():
            if key in plan["covered_classes"] or not spec.get("exclusion_reason"):
                continue
            classes.append(
                {
                    "failure_class": key,
                    "label": spec["label"],
                    "description": spec["description"],
                    "covered": False,
                    "exclusion_reason": spec["exclusion_reason"],
                }
            )

        if failure_class:
            classes = [c for c in classes if c["failure_class"] == failure_class]
            if not classes:
                return _error(
                    name,
                    f"failure class {failure_class!r} is not referenced by the {plan_name} plan",
                    plan_covered_classes=plan["covered_classes"],
                )

        return _ok(
            name,
            plan={
                "plan": plan["plan"],
                "label": plan["label"],
                "coverage_pct": plan["coverage_pct"],
                "aggregate_limit_usd": plan["aggregate_limit_usd"],
                "per_claim_limit_usd": plan["per_claim_limit_usd"],
                "monthly_premium_floor_usd": plan["monthly_premium_floor_usd"],
                "monthly_premium_cap_usd": plan["monthly_premium_cap_usd"],
                "price_band": plan["price_band"],
            },
            failure_classes=classes,
            covered_class_count=len([c for c in classes if c["covered"]]),
            exclusions=EXCLUSIONS,
            claims_handling={
                "filing_window_days": 30,
                "decision_timeline_days": plan["claims_timeline_days"],
                "payment_timeline_days": 21,
                "procedure": [
                    "Member files with the incident reference and the evidence pack.",
                    "Claims committee reviews against the failure-class definition and the "
                    "attested SLA artifact pack.",
                    "Decision issued within the plan's decision timeline.",
                    "Payment made within 21 days of approval, net of the deductible.",
                    "Every decision, including denials, is published in the quarterly "
                    "claims attestation.",
                ],
                "required_evidence": [
                    "attested SLA artifact pack",
                    "on-chain attestation reference",
                    "raw agent run log",
                    "incident post-mortem",
                ],
            },
            governance=GOVERNANCE,
        )

    if name == "get_pool_statistics":
        months = int(arguments.get("months", 12))
        state = load_state()
        claims = _all_claims(state)
        members = _all_members(state)
        cutoff = _now() - timedelta(days=months * 30)
        window_claims = []
        for claim in claims:
            try:
                filed = _parse_ts(claim["filed_at"])
            except (ValueError, KeyError):
                continue
            if filed >= cutoff:
                window_claims.append(claim)
        if not window_claims and months >= 12:
            window_claims = claims

        by_status = {status: 0 for status in CLAIM_STATUSES}
        by_class: dict[str, dict[str, Any]] = {}
        for claim in window_claims:
            status = claim.get("status", "filed")
            by_status[status] = by_status.get(status, 0) + 1
            key = claim.get("failure_class", "unknown")
            entry = by_class.setdefault(
                key, {"claims": 0, "claimed_usd": 0.0, "approved_usd": 0.0, "paid_usd": 0.0}
            )
            entry["claims"] += 1
            entry["claimed_usd"] += float(claim.get("claimed_usd", 0.0))
            entry["approved_usd"] += float(claim.get("approved_usd", 0.0))
            entry["paid_usd"] += float(claim.get("paid_usd", 0.0))

        total_claimed = round(sum(float(c.get("claimed_usd", 0.0)) for c in window_claims), 2)
        total_approved = round(sum(float(c.get("approved_usd", 0.0)) for c in window_claims), 2)
        total_paid = round(sum(float(c.get("paid_usd", 0.0)) for c in window_claims), 2)
        premiums_collected = round(sum(float(m.get("monthly_premium_usd", 0.0)) for m in members) * months, 2)
        covered_stack = round(sum(float(m.get("covered_stack_usd", 0.0)) for m in members), 2)
        loss_ratio = round(total_paid / premiums_collected, 4) if premiums_collected else None

        resolution_days: list[float] = []
        for claim in window_claims:
            if claim.get("resolved_at") and claim.get("filed_at"):
                resolution_days.append(
                    round(
                        (_parse_ts(claim["resolved_at"]) - _parse_ts(claim["filed_at"])).days
                        + (
                            (_parse_ts(claim["resolved_at"]) - _parse_ts(claim["filed_at"])).seconds
                            / 3600.0
                            / 24.0
                        ),
                        2,
                    )
                )
        severities = [float(c.get("claimed_usd", 0.0)) for c in window_claims]

        reserve_target = round(max(premiums_collected * 0.35, total_approved * 0.2), 2)
        reserve_actual = round(premiums_collected * 0.42, 2)
        reserve_adequacy = (
            round(reserve_actual / reserve_target, 4) if reserve_target else None
        )

        return _ok(
            name,
            window_months=months,
            member_count=len(members),
            total_covered_stack_usd=covered_stack,
            premiums_collected_usd=premiums_collected,
            claims={
                "total": len(window_claims),
                "by_status": by_status,
                "filed": by_status.get("filed", 0),
                "approved_count": by_status.get("approved", 0),
                "denied_count": by_status.get("denied", 0),
                "pending_count": by_status.get("under_review", 0),
                "paid_count": by_status.get("paid", 0),
                "total_claimed_usd": total_claimed,
                "total_approved_usd": total_approved,
                "total_paid_usd": total_paid,
                "claim_frequency_per_member_per_year": round(
                    len(window_claims) / max(1, len(members)) * (12 / max(1, months)), 4
                ),
                "approval_rate_pct": round(
                    (len([c for c in window_claims if c.get("approved_usd", 0) > 0])
                     / len(window_claims)) * 100.0, 2
                )
                if window_claims
                else None,
            },
            loss_ratio=loss_ratio,
            reserve={
                "reserve_target_usd": reserve_target,
                "reserve_actual_usd": reserve_actual,
                "adequacy_ratio": reserve_adequacy,
                "adequacy_verdict": "adequate" if (reserve_adequacy or 0) >= 1 else "under-reserved",
                "basis": "42% of premiums collected held against a 35%-of-premiums target",
            },
            average_claim_severity_usd=round(statistics.mean(severities), 2) if severities else None,
            median_claim_severity_usd=round(statistics.median(severities), 2) if severities else None,
            average_time_to_resolution_days=round(statistics.mean(resolution_days), 2)
            if resolution_days
            else None,
            by_failure_class={
                key: {
                    "claims": value["claims"],
                    "claimed_usd": round(value["claimed_usd"], 2),
                    "approved_usd": round(value["approved_usd"], 2),
                    "paid_usd": round(value["paid_usd"], 2),
                    "claim_rate_pct": round(value["claims"] / len(window_claims) * 100.0, 2)
                    if window_claims
                    else 0.0,
                }
                for key, value in sorted(by_class.items())
            },
            governance=GOVERNANCE,
        )

    if name == "get_claim_history":
        state = load_state()
        claims = _all_claims(state)
        member_id = arguments.get("member_id")
        status = arguments.get("status")
        failure_class = arguments.get("failure_class")
        plan = arguments.get("plan")
        if member_id:
            claims = [c for c in claims if c.get("member_id") == member_id]
        if status:
            claims = [c for c in claims if c.get("status") == status]
        if failure_class:
            claims = [c for c in claims if c.get("failure_class") == failure_class]
        if plan:
            claims = [c for c in claims if c.get("plan") == plan]
        limit = int(arguments.get("limit", 50))
        total = len(claims)
        claims = claims[:limit]
        for claim in claims:
            if claim.get("filed_at") and claim.get("resolved_at"):
                delta = _parse_ts(claim["resolved_at"]) - _parse_ts(claim["filed_at"])
                claim["days_to_resolution"] = round(delta.total_seconds() / 86400.0, 2)
            else:
                claim["days_to_resolution"] = None
            spec = FAILURE_CLASSES.get(claim.get("failure_class", ""), {})
            claim["failure_class_label"] = spec.get("label", claim.get("failure_class"))
        return _ok(
            name,
            count=len(claims),
            total_matching=total,
            filters={
                "member_id": member_id,
                "status": status,
                "failure_class": failure_class,
                "plan": plan,
            },
            total_claimed_usd=round(sum(float(c.get("claimed_usd", 0.0)) for c in claims), 2),
            total_paid_usd=round(sum(float(c.get("paid_usd", 0.0)) for c in claims), 2),
            claims=claims,
        )

    if name == "calculate_premium":
        if "covered_stack_usd" not in arguments:
            return _error(name, "covered_stack_usd is required")
        try:
            covered_stack = float(arguments["covered_stack_usd"])
        except (TypeError, ValueError):
            return _error(name, "covered_stack_usd must be a number")
        if covered_stack <= 0:
            return _error(name, "covered_stack_usd must be greater than zero")
        plan_name = (arguments.get("plan") or "standard").lower()
        if plan_name not in PLANS:
            return _error(name, f"unknown plan {plan_name!r}", valid_plans=sorted(PLANS))
        plan = PLANS[plan_name]

        requested = arguments.get("failure_classes")
        if requested is None:
            classes = list(plan["covered_classes"])
        else:
            if not isinstance(requested, list) or not requested:
                return _error(name, "failure_classes must be a non-empty array when provided")
            unknown = [c for c in requested if c not in FAILURE_CLASSES]
            if unknown:
                return _error(
                    name, f"unknown failure classes {unknown}", valid=sorted(FAILURE_CLASSES)
                )
            classes = list(requested)

        uncovered = [c for c in classes if not FAILURE_CLASSES[c]["covered"]]
        if uncovered:
            return _error(
                name,
                f"failure classes {uncovered} are excluded from coverage and cannot be priced",
                exclusion_reason=FAILURE_CLASSES[uncovered[0]].get("exclusion_reason"),
            )

        # Step 1: base rate per $1,000 of covered stack, summed over the classes.
        base_rate = 0.0
        rate_breakdown = []
        for key in classes:
            spec = FAILURE_CLASSES[key]
            units = covered_stack / 1000.0
            contribution = spec["base_rate_per_1000_usd"] * units
            base_rate += contribution
            rate_breakdown.append(
                {
                    "failure_class": key,
                    "base_rate_per_1000_usd": spec["base_rate_per_1000_usd"],
                    "covered_stack_units_1000": round(units, 4),
                    "contribution_usd": round(contribution, 4),
                }
            )

        # Step 2: plan multiplier.
        after_plan = base_rate * plan["plan_multiplier"]
        plan_factor = round(base_rate * plan["plan_multiplier"], 4)

        # Step 3: breadth factor - covering more classes raises the exposure.
        breadth = 1.0 + 0.06 * max(0, len(classes) - 1)
        after_breadth = after_plan * breadth

        # Step 4: exposure factors - autonomy, agent count, volume.
        autonomy = str(arguments.get("autonomy_level") or "medium").lower()
        if autonomy not in ("low", "medium", "high"):
            return _error(name, f"unknown autonomy_level {autonomy!r}", valid=["low", "medium", "high"])
        autonomy_factor = _autonomy_factor(autonomy)
        agent_count = int(arguments.get("agent_count", 3))
        if agent_count < 0:
            return _error(name, "agent_count must be >= 0")
        agent_factor = 1.0 + min(0.5, agent_count * 0.04)
        monthly_volume = float(arguments.get("monthly_volume_usd", 0) or 0)
        if monthly_volume < 0:
            return _error(name, "monthly_volume_usd must be >= 0")
        volume_factor = 1.0 + min(0.35, (monthly_volume / max(covered_stack, 1.0)) * 0.12)
        exposure_multiplier = round(autonomy_factor * agent_factor * volume_factor, 4)

        # Step 5: claims-history loading.
        state = load_state()
        member_id = arguments.get("member_id")
        member_claims: list[dict[str, Any]] = []
        member_name = None
        if member_id:
            member_claims = [c for c in _all_claims(state) if c.get("member_id") == member_id]
            member = next(
                (m for m in _all_members(state) if m.get("member_id") == member_id), None
            )
            if member is None:
                return _error(
                    name,
                    f"unknown member_id {member_id!r}",
                    valid_members=[m["member_id"] for m in _all_members(state)],
                )
            member_name = member.get("member_name")
        loading_factor, loading_detail = _claims_history_factor(member_claims, covered_stack)

        uncapped = after_breadth * exposure_multiplier * loading_factor
        floor = plan["monthly_premium_floor_usd"]
        cap = plan["monthly_premium_cap_usd"]
        monthly = round(min(max(uncapped, floor), cap), 2)
        annual_gross = round(monthly * 10, 2)
        annual_discount_pct = 17.0
        annual = round(annual_gross * (1 - annual_discount_pct / 100.0), 2)

        return _ok(
            name,
            plan=plan_name,
            covered_stack_usd=covered_stack,
            covered_failure_classes=classes,
            member_id=member_id,
            member_name=member_name,
            monthly_premium_usd=monthly,
            annual_premium_usd=annual,
            annual_premium_gross_usd=annual_gross,
            annual_discount_pct=annual_discount_pct,
            calculation={
                "step_1_base_rate": {
                    "value_usd": round(base_rate, 4),
                    "formula": "sum(base_rate_per_1000_usd x covered_stack / 1000) over covered classes",
                    "breakdown": rate_breakdown,
                },
                "step_2_plan_multiplier": {
                    "value_usd": plan_factor,
                    "factor": plan["plan_multiplier"],
                },
                "step_3_breadth_factor": {
                    "value_usd": round(after_breadth, 4),
                    "factor": round(breadth, 4),
                    "formula": "1 + 0.06 x (number of covered classes - 1)",
                },
                "step_4_exposure": {
                    "value_usd": round(after_breadth * exposure_multiplier, 4),
                    "multiplier": exposure_multiplier,
                    "autonomy_level": autonomy,
                    "autonomy_factor": autonomy_factor,
                    "agent_count": agent_count,
                    "agent_factor": round(agent_factor, 4),
                    "monthly_volume_usd": monthly_volume,
                    "volume_factor": round(volume_factor, 4),
                },
                "step_5_claims_loading": {
                    "value_usd": round(after_breadth * exposure_multiplier * loading_factor, 4),
                    "factor": loading_factor,
                    **loading_detail,
                },
                "step_6_uncapped_premium_usd": round(uncapped, 4),
                "step_7_floor_and_cap": {
                    "floor_usd": floor,
                    "cap_usd": cap,
                    "applied": "floor" if uncapped < floor else ("cap" if uncapped > cap else "none"),
                    "final_monthly_usd": monthly,
                },
            },
            coverage={
                "coverage_pct": plan["coverage_pct"],
                "aggregate_limit_usd": plan["aggregate_limit_usd"],
                "per_claim_limit_usd": plan["per_claim_limit_usd"],
            },
            price_band=plan["price_band"],
        )

    return _error(name, f"unknown tool {name!r}", available=[t.name for t in TOOLS])


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
