#!/usr/bin/env python3
"""MCP server for liability-pool - Agent Liability Insurance Pool"""

import json
import os
from datetime import datetime
from mcp.server import Server
from mcp.types import Tool, TextContent

app = Server("mcp-liability-pool")

COVERAGE_TIERS = {
    "basic": {
        "name": "Basic Coverage",
        "monthly_premium_per_agent": 49,
        "coverage_limit_usd": 100000,
        "covered_events": ["agent_downtime", "data_loss", "execution_error"],
        "exclusions": ["market_loss", "regulatory_action", "intentional_misconduct"],
        "deductible_usd": 5000,
        "claims_process_days": 14
    },
    "professional": {
        "name": "Professional Coverage",
        "monthly_premium_per_agent": 149,
        "coverage_limit_usd": 1000000,
        "covered_events": ["agent_downtime", "data_loss", "execution_error", "security_breach", "oracle_failure"],
        "exclusions": ["market_loss", "regulatory_action", "intentional_misconduct"],
        "deductible_usd": 2500,
        "claims_process_days": 7
    },
    "enterprise": {
        "name": "Enterprise Coverage",
        "monthly_premium_per_agent": 499,
        "coverage_limit_usd": 10000000,
        "covered_events": ["agent_downtime", "data_loss", "execution_error", "security_breach", "oracle_failure", "smart_contract_bug", "mev_exploit", "governance_attack"],
        "exclusions": ["market_loss", "regulatory_action", "intentional_misconduct", "war_terrorism"],
        "deductible_usd": 1000,
        "claims_process_days": 3
    }
}

POOL_STATS = {
    "total_capital_usd": 50000000,
    "active_policies": 247,
    "total_agents_covered": 892,
    "premiums_collected_30d": 1250000,
    "claims_paid_30d": 180000,
    "loss_ratio_30d": 0.144,
    "reserve_ratio": 2.5,
    "tier_distribution": {"basic": 120, "professional": 85, "enterprise": 42},
    "chains_covered": ["ethereum", "polygon", "arbitrum", "optimism", "base", "avalanche"]
}

CLAIMS = {
    "claim-001": {
        "id": "claim-001",
        "policy_id": "pol-001",
        "agent_id": "agent-001",
        "event_type": "execution_error",
        "description": "Agent executed duplicate order due to race condition",
        "amount_claimed_usd": 25000,
        "amount_approved_usd": 22000,
        "status": "paid",
        "filed_at": "2024-11-15T10:00:00",
        "processed_at": "2024-11-22T10:00:00",
        "tier": "professional"
    },
    "claim-002": {
        "id": "claim-002",
        "policy_id": "pol-002",
        "agent_id": "agent-002",
        "event_type": "oracle_failure",
        "description": "Price oracle returned stale data causing bad trades",
        "amount_claimed_usd": 150000,
        "amount_approved_usd": 125000,
        "status": "paid",
        "filed_at": "2024-12-01T10:00:00",
        "processed_at": "2024-12-08T10:00:00",
        "tier": "enterprise"
    },
    "claim-003": {
        "id": "claim-003",
        "policy_id": "pol-003",
        "agent_id": "agent-003",
        "event_type": "security_breach",
        "description": "API key compromise led to unauthorized trades",
        "amount_claimed_usd": 50000,
        "amount_approved_usd": 0,
        "status": "denied",
        "filed_at": "2024-12-10T10:00:00",
        "processed_at": "2024-12-17T10:00:00",
        "denial_reason": "Excluded - customer credential management failure",
        "tier": "basic"
    }
}

app = Server("mcp-liability-pool")

TOOLS = [
    Tool(
        name="get_coverage_terms",
        description="Get coverage terms for a tier",
        inputSchema={
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": ["basic", "professional", "enterprise"], "description": "Coverage tier"}
            }
        }
    ),
    Tool(
        name="get_pool_statistics",
        description="Get insurance pool statistics",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="get_claim_history",
        description="Get claim history",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Filter by agent ID (optional)"},
                "status": {"type": "string", "enum": ["pending", "approved", "paid", "denied", "all"], "default": "all", "description": "Filter by status"},
                "limit": {"type": "integer", "default": 50, "description": "Max entries"}
            }
        }
    ),
    Tool(
        name="calculate_premium",
        description="Calculate premium for agent(s)",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_count": {"type": "integer", "description": "Number of agents"},
                "tier": {"type": "string", "enum": ["basic", "professional", "enterprise"], "description": "Coverage tier"},
                "chains": {"type": "array", "items": {"type": "string"}, "description": "Chains agents operate on"},
                "monthly_volume_usd": {"type": "number", "description": "Estimated monthly trading volume"}
            },
            "required": ["agent_count", "tier"]
        }
    )
]

@app.list_tools()
async def list_tools():
    return TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_coverage_terms":
        tier = arguments.get("tier")
        
        if tier:
            coverage = COVERAGE_TIERS.get(tier)
            if not coverage:
                return [TextContent(type="text", text=json.dumps({"error": f"Tier not found: {tier}"}))]
            result = {"status": "ok", "tool": "get_coverage_terms", "tier": tier, "coverage": coverage}
        else:
            result = {"status": "ok", "tool": "get_coverage_terms", "tiers": COVERAGE_TIERS}
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "get_pool_statistics":
        result = {"status": "ok", "tool": "get_pool_statistics", "statistics": POOL_STATS, "as_of": datetime.now().isoformat()}
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "get_claim_history":
        agent_id = arguments.get("agent_id")
        status = arguments.get("status", "all")
        limit = arguments.get("limit", 50)
        
        claims = list(CLAIMS.values())
        if agent_id:
            claims = [c for c in claims if c["agent_id"] == agent_id]
        if status != "all":
            claims = [c for c in claims if c["status"] == status]
        
        claims.sort(key=lambda x: x["filed_at"], reverse=True)
        
        total_claimed = sum(c["amount_claimed_usd"] for c in claims)
        total_approved = sum(c["amount_approved_usd"] for c in claims)
        approval_rate = total_approved / total_claimed if total_claimed > 0 else 0
        
        result = {
            "status": "ok",
            "tool": "get_claim_history",
            "count": len(claims),
            "total_claimed_usd": total_claimed,
            "total_approved_usd": total_approved,
            "approval_rate": round(approval_rate * 100, 1),
            "claims": claims[:limit]
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "calculate_premium":
        agent_count = arguments.get("agent_count")
        tier = arguments.get("tier")
        chains = arguments.get("chains", [])
        monthly_volume_usd = arguments.get("monthly_volume_usd", 0)
        
        coverage = COVERAGE_TIERS.get(tier)
        if not coverage:
            return [TextContent(type="text", text=json.dumps({"error": f"Tier not found: {tier}"}))]
        
        base_premium = coverage["monthly_premium_per_agent"] * agent_count
        
        # Volume surcharge
        volume_surcharge = 0
        if monthly_volume_usd > 10000000:  # >$10M/month
            volume_surcharge = base_premium * 0.15
        elif monthly_volume_usd > 5000000:  # >$5M/month
            volume_surcharge = base_premium * 0.10
        elif monthly_volume_usd > 1000000:  # >$1M/month
            volume_surcharge = base_premium * 0.05
        
        # Multi-chain surcharge
        chain_surcharge = 0
        if len(chains) > 1:
            chain_surcharge = base_premium * 0.05 * (len(chains) - 1)
        
        # Annual discount
        annual_premium = (base_premium + volume_surcharge + chain_surcharge) * 12
        annual_discount = annual_premium * 0.10  # 10% annual discount
        
        result = {
            "status": "ok",
            "tool": "calculate_premium",
            "agent_count": agent_count,
            "tier": tier,
            "chains": chains,
            "monthly_volume_usd": monthly_volume_usd,
            "base_monthly_premium": base_premium,
            "volume_surcharge": round(volume_surcharge, 2),
            "chain_surcharge": round(chain_surcharge, 2),
            "total_monthly_premium": round(base_premium + volume_surcharge + chain_surcharge, 2),
            "annual_premium": round(annual_premium, 2),
            "annual_discount": round(annual_discount, 2),
            "annual_after_discount": round(annual_premium - annual_discount, 2),
            "coverage_limit_per_agent_usd": coverage["coverage_limit_usd"],
            "deductible_usd": coverage["deductible_usd"]
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

if __name__ == "__main__":
    import asyncio
    from mcp.server.stdio import stdio_server
    
    async def serve():
        reader, writer = await stdio_server()
        await app.run(reader, writer)
    
    asyncio.run(serve())