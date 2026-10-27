# mcp-liability-pool

MCP server for Agent Liability Insurance Pool - coverage terms, pool statistics, claim history, premium calculator.

## Tools

### get_coverage_terms
Get coverage terms for a tier (basic, professional, enterprise).

**Parameters:**
- `tier` (string, optional): "basic", "professional", or "enterprise"

### get_pool_statistics
Get insurance pool statistics (capital, policies, claims, loss ratio).

**Parameters:** None

### get_claim_history
Get claim history with filters.

**Parameters:**
- `agent_id` (string, optional): Filter by agent ID
- `status` (string, optional): "pending", "approved", "paid", "denied", or "all" (default: "all")
- `limit` (integer, optional): Max entries (default: 50)

### calculate_premium
Calculate premium for agent(s).

**Parameters:**
- `agent_count` (integer, required): Number of agents
- `tier` (string, required): "basic", "professional", or "enterprise"
- `chains` (array, optional): Chains agents operate on
- `monthly_volume_usd` (number, optional): Estimated monthly trading volume

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

## Connecting to Claude Desktop

```json
{
  "mcpServers": {
    "liability-pool": {
      "command": "python",
      "args": ["/path/to/mcp-liability-pool/main.py"],
      "cwd": "/path/to/mcp-liability-pool"
    }
  }
}
```

## License

MIT