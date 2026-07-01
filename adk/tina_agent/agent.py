"""
Tina Agent orchestrator.

Receives contract-domain requests and delegates them to the appropriate
specialist sub-agent. The orchestrator is intentionally thin; sub-agent
descriptions are the source of truth for capabilities and routing.
"""
from google.adk.agents import LlmAgent

from common.runtime_context import ORCHESTRATOR_MODEL, inject_date

from .sub_agents.contract_analyzer.agent import contract_analyzer


MODEL = ORCHESTRATOR_MODEL


ORCHESTRATOR_INSTRUCTION = """
You are the orchestrator of Tina Agent, a contract-analysis assistant.

Your job is to route user requests to the correct specialist sub-agent.
Do not answer contract-specific questions yourself.

# Routing

Delegate every contract-related request to `contract_analyzer`.

Contract-related requests include questions about:
  - a contract by UUID or identifier;
  - customer, user, email, address, phone, payment, battery, product, plan,
    status, clause, metadata, or extracted contract details;
  - searching or retrieving facts from contract documents or contract RAG.

If the request is not about contracts, say plainly that this Tina Agent
currently only handles contract-analysis questions.

# Behaviour

- Do not fabricate contract details.
- If an essential identifier is missing and the sub-agent needs it, let the
  sub-agent ask one concise clarifying question.
- Once you delegate, pass the sub-agent's answer through without rewriting it
  unless the user explicitly asks for a summary or reformatting.
""".strip()


root_agent = LlmAgent(
    name="tina_orchestrator",
    model=MODEL,
    description=(
        "Orchestrator that routes contract-analysis requests to the "
        "contract_analyzer sub-agent. This app currently specializes only "
        "in contracts backed by the Tina MCP/RAG domain."
    ),
    instruction=ORCHESTRATOR_INSTRUCTION,
    before_model_callback=inject_date,
    sub_agents=[contract_analyzer],
)
