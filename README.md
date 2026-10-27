# mcp-liability-pool

MCP server for the **Agent Liability Insurance Pool** - pooled liability coverage for
agent-caused losses beyond SLA credits. A monthly premium buys coverage against defined failure
classes with transparent claims handling.

Credits refund fees; they do not cover losses. This pool completes the liability layer:
SLA credits -> on-call responders -> loss coverage. Pricing is $29-199/mo as a percentage of the
covered stack. The "insurance from the platform operator is conflicted" objection is answered
with pooled-governance transparency (published reserves, claim statistics via on-chain
attestations) and third-party underwriting.

## Tools

### `get_coverage_terms`
Return the coverage terms for a plan.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `plan` | string enum `basic`/`standard`/`pro` | `standard` | Plan to quote |
| `failure_class` | string enum | - | Restrict the terms to one failure class |

Returns the defined failure classes (wrong trade from a failed alert, unauthorized tool action,
poisoned baseline, prompt-injection loss, missed SLA downstream loss) with what each covers, per
class deductibles and payout caps; the excluded classes with their exclusion reasons; the global
exclusion list; the claims-handling procedure and timeline with the required evidence; the
pooled-governance transparency commitments; and the relationship to SLA credits.

### `get_pool_statistics`
Pool statistics computed from the claim ledger.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `months` | integer | 12 | Look-back window in months (1-60) |

Returns member count, total covered stack, premiums collected, claims by status, counts filed /
approved / denied / pending / paid, total claimed / approved / paid, claim frequency per member
per year, approval rate, loss ratio, reserve target / actual / adequacy ratio and verdict, average
and median claim severity, average time to resolution, and a per-failure-class breakdown with
claim rates.

### `get_claim_history`
Claim records, newest first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `member_id` | string | - | Filter by member id |
| `status` | string enum `filed`/`under_review`/`approved`/`denied`/`paid`/`withdrawn` | - | Filter by status |
| `failure_class` | string enum | - | Filter by failure class |
| `plan` | string enum | - | Filter by plan |
| `limit` | integer | 50 | Maximum entries (1-500) |

Each claim carries its amounts, decision, resolution notes and days-to-resolution.

### `calculate_premium`
Compute a monthly premium with the full breakdown.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `covered_stack_usd` | number | **required** | Value of the agent stack being covered (must be > 0) |
| `plan` | string enum | `standard` | Plan |
| `failure_classes` | array of string | plan's covered set | Failure classes to cover |
| `agent_count` | integer | 3 | Number of covered agents |
| `autonomy_level` | string enum `low`/`medium`/`high` | `medium` | How autonomously the agents act |
| `monthly_volume_usd` | number | 0 | Monthly transaction or trading volume |
| `member_id` | string | - | Member id, to apply that member's claims-history loading |

The calculation runs in seven auditable steps:

1. base rate = `sum(base_rate_per_1000_usd x covered_stack / 1000)` over the covered classes;
2. plan multiplier;
3. breadth factor = `1 + 0.06 x (classes - 1)`;
4. exposure = autonomy x agent-count x volume factors;
5. claims-history loading = `1 + min(0.75, loss_ratio x 2)`;
6. uncapped premium;
7. plan floor and cap.

Returns the monthly premium, the annual equivalent (10x monthly less the 17% discount) and every
intermediate value. Pricing an excluded failure class is rejected with `status: "error"`.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

State is persisted as JSON in `./state/pool_state.json`. Override the location with
`MCP_LIABILITY_POOL_STATE_DIR`.

## Connecting to an MCP client

```json
{
  "mcpServers": {
    "liability-pool": {
      "command": "python3",
      "args": ["/tmp/v3-services/mcp-liability-pool/main.py"]
    }
  }
}
```

## License

MIT
