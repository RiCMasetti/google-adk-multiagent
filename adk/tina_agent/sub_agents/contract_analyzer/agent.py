"""
Contract analyzer sub-agent.

Wraps the Tina contract MCP server (HTTP transport). The MCP domain owns the
RAG retrieval and contract search logic; this agent translates user questions
into precise MCP tool calls and formats the returned facts.
"""
from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from common.runtime_context import SUB_AGENT_MODEL, inject_date

from .cognito_auth import tina_authorization_header_provider


MODEL = SUB_AGENT_MODEL


_PUBLIC_MCP_URLS = {
    "canary": "https://mcp-off.test.bullfinch.com/tina/mcp",
    "live": "https://mcp.test.bullfinch.com/tina/mcp",
}

_INTERNAL_MCP_URLS = {
    "canary": "http://tina-mcp-service.tina-mcp-test-canary.svc.cluster.local",
    "live": "http://tina-mcp-service.tina-mcp-test-live.svc.cluster.local",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _tina_mcp_url() -> str:
    explicit = os.environ.get("TINA_MCP_URL")
    if explicit:
        return explicit

    environment = os.environ.get("TINA_MCP_ENV", "canary").strip().lower()
    if environment not in _PUBLIC_MCP_URLS:
        raise RuntimeError(
            "TINA_MCP_ENV must be either 'canary' or 'live' when TINA_MCP_URL "
            "is not set."
        )

    urls = (
        _INTERNAL_MCP_URLS
        if _env_bool("TINA_MCP_INTERNAL", False)
        else _PUBLIC_MCP_URLS
    )
    return urls[environment]


tina_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=_tina_mcp_url(),
        timeout=_env_float("TINA_MCP_TIMEOUT_SECONDS", 30.0),
        sse_read_timeout=_env_float("TINA_MCP_SSE_READ_TIMEOUT_SECONDS", 300.0),
    ),
    header_provider=tina_authorization_header_provider,
)


CONTRACT_ANALYZER_INSTRUCTION = """
You are a contract-analysis specialist for Tina contracts.

The contract facts live behind the Tina MCP server. The MCP domain owns RAG
retrieval over contract documents and exposes tools with their own parameter
schemas and descriptions. Use those tools to answer; do not answer from
general knowledge or assumptions.

# Available MCP tools

Use only the tools exposed by the Tina MCP server. Current tool names include:
`ask_contract_documents`, `chat_with_documents`, `search_documents`,
`resolve_contract_ids`, `list_contract_document_types`, `list_document_types`,
and `clear_conversation`.

Do not invent function names such as `get_contract_details`. If a tool is not
available, say that the Tina MCP toolset did not expose that capability.

# How to handle requests

1. Identify the contract identifier from the user request when present.
   Contract IDs are usually UUIDs such as
   `487d345b-87c6-4d72-a1e6-f124042a11ca`.
2. Read the MCP tool descriptions and choose the tool whose parameters best
   match the request.
3. Build a precise query for the MCP tool. Preserve the exact contract ID.
   For field lookups, ask directly for the requested field; for broader
   requests, ask for the relevant grouped details.
4. Use MCP results as the only source of truth.

# Expected user intents

- "what is the email address for this contract <uuid>?"
  Retrieve the email address associated with that exact contract.
- "does this contract <uuid> have the battery?"
  Check whether the contract includes a battery/product battery component.
- "show me all the user details about this contract <uuid>"
  Retrieve customer/user details for that exact contract.

# Output rules

- Be concise and factual.
- If the tool returns confidence, source snippets, document references, or
  retrieval metadata, include the useful parts briefly.
- If the answer is not present in the returned contract data, say that it was
  not found in the available contract context.
- If the user asks about one contract but does not provide a contract ID, ask
  for the contract ID.
- Do not expose raw authentication tokens, MCP headers, or internal endpoint
  details.
""".strip()


contract_analyzer = LlmAgent(
    name="contract_analyzer",
    model=MODEL,
    description=(
        "READ-ONLY contract-analysis agent backed by the Tina HTTP MCP "
        "server and its contract RAG retrieval tools. Answers questions "
        "about contract facts, extracted contract fields, user/customer "
        "details, email addresses, products, battery presence, clauses, "
        "status, and metadata for specific contract UUIDs. Builds precise "
        "queries and tool parameters for the Tina MCP tools. Does not modify "
        "contracts and does not answer from memory."
    ),
    instruction=CONTRACT_ANALYZER_INSTRUCTION,
    before_model_callback=inject_date,
    tools=[tina_toolset],
)
