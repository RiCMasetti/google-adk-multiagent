# Cost Multi-Agent Design Spec

## Goal

Build a small ADK multi-agent application focused on cloud cost analysis.
The initial system keeps only read-only cost agents and uses an orchestrator
to coordinate them.

## Agents

- `platform_orchestrator`
  - Routes user requests to the right cost specialist.
  - Can call both cost specialists for comparative or blended questions.
  - Synthesizes a final response when more than one specialist is needed.
  - Does not call cloud APIs directly.

- `aws_cost_agent`
  - Read-only AWS Cost Explorer specialist.
  - Answers AWS spend, cost-driver, comparison, forecast, and CSV report
    questions.

- `hetzner_cost_agent`
  - Read-only Hetzner Cloud cost and inventory specialist.
  - Answers current steady-state spend, inventory, cost-driver, and SKU
    pricing questions.

## Collaboration Model

Sub-agent collaboration is orchestrator-mediated:

1. The orchestrator decides which specialist or specialists are needed.
2. A specialist may recommend that the other cost specialist should be
   consulted when the question spans providers.
3. The orchestrator performs the second delegation and combines the result.

Peer-to-peer free-form agent loops are intentionally out of scope for the
initial version.

## Model Configuration

Default provider is Bedrock:

- `ORCHESTRATOR_MODEL`: Claude Sonnet 4.6 through Bedrock.
- `SUB_AGENT_MODEL`: Claude Haiku 4.5 through Bedrock.

The existing provider switch must remain:

- `LLM_PROVIDER=bedrock` uses Bedrock model IDs.
- `LLM_PROVIDER=vertex_ai` uses Gemini model names and regional fallback.

