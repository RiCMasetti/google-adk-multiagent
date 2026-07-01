"""
Runtime context helpers shared across all ADK apps.

Two responsibilities:

1. **Date injection** (inject_date callback): keep the model's notion
   of "today" aligned with reality. Gemini 2.5 Pro has a strong
   internalised belief about the current date anchored to its training
   cutoff. Without explicit injection, queries like "this month's
   costs" get interpreted against a year-old reference and produce
   wrong results.

2. **Model factory** (make_model): build the configured LiteLlm backend.
   `LLM_PROVIDER=bedrock` uses Amazon Bedrock through LiteLLM.
   `LLM_PROVIDER=vertex_ai` keeps the Vertex AI Gemini regional fallback.
   Agents import `ORCHESTRATOR_MODEL` or `SUB_AGENT_MODEL`, so provider
   switches do not require per-agent refactors.

The `inject_date` callback should be wired as `before_model_callback`
on every LlmAgent that does temporal reasoning (basically all of them).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model-request context management
# ---------------------------------------------------------------------------

_DEFAULT_MAX_HISTORY_CONTENTS = 32
_DEFAULT_MAX_HISTORY_CHARS = 120_000


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using %s", name, raw, default)
        return default


def _env_first(names: list[str], default: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def _parts(content: Any) -> list[Any]:
    if isinstance(content, dict):
        return content.get("parts") or []
    return getattr(content, "parts", None) or []


def _part_value(part: Any, snake_name: str, camel_name: str) -> Any:
    if isinstance(part, dict):
        return part.get(snake_name) or part.get(camel_name)
    return getattr(part, snake_name, None) or getattr(part, camel_name, None)


def _part_text(part: Any) -> str:
    if isinstance(part, dict):
        return part.get("text") or ""
    return getattr(part, "text", None) or ""


def _content_estimated_chars(content: Any) -> int:
    """
    Estimate the prompt cost of one ADK/GenAI content item.

    Text is counted exactly. Function calls/responses are counted by their
    serialized representation because those objects can carry large tool
    payloads even when they are not plain text.
    """
    total = 0
    for part in _parts(content):
        text = _part_text(part)
        if text:
            total += len(text)
            continue
        function_call = _part_value(part, "function_call", "functionCall")
        function_response = _part_value(part, "function_response", "functionResponse")
        if function_call is not None:
            total += len(repr(function_call))
        if function_response is not None:
            total += len(repr(function_response))
    return total


def _content_has_tool_part(content: Any) -> bool:
    for part in _parts(content):
        if _part_value(part, "function_call", "functionCall") is not None:
            return True
        if _part_value(part, "function_response", "functionResponse") is not None:
            return True
    return False


def _compact_contents(llm_request: LlmRequest) -> Optional[str]:
    """
    Trim old conversation contents before a model call.

    This is intentionally not an LLM summarizer. It is a deterministic guard
    against long-running OpenWebUI chats resending days of old tool-heavy
    history. The ADK session in storage remains intact; only the contents
    sent to this model call are compacted.
    """
    if not _env_bool("ENABLE_HISTORY_COMPACTION", True):
        return None

    contents = getattr(llm_request, "contents", None)
    if not contents:
        return None

    contents = list(contents)
    max_contents = _env_int("MAX_LLM_HISTORY_CONTENTS", _DEFAULT_MAX_HISTORY_CONTENTS)
    max_chars = _env_int("MAX_LLM_HISTORY_CHARS", _DEFAULT_MAX_HISTORY_CHARS)

    total_chars = sum(_content_estimated_chars(c) for c in contents)
    if len(contents) <= max_contents and total_chars <= max_chars:
        return None

    kept_reversed: list[Any] = []
    kept_chars = 0
    for content in reversed(contents):
        content_chars = _content_estimated_chars(content)
        if kept_reversed and len(kept_reversed) >= max_contents:
            break
        if kept_reversed and kept_chars + content_chars > max_chars:
            break
        kept_reversed.append(content)
        kept_chars += content_chars

    kept = list(reversed(kept_reversed))

    # Avoid starting the retained history with a dangling tool call/result.
    # Function call/result pairs are only meaningful with their neighboring
    # model/tool events; if the trim boundary splits them, drop the leading
    # tool event rather than sending malformed history.
    while kept and _content_has_tool_part(kept[0]):
        kept_chars -= _content_estimated_chars(kept[0])
        kept.pop(0)

    if not kept:
        kept = contents[-1:]
        kept_chars = sum(_content_estimated_chars(c) for c in kept)

    omitted_count = len(contents) - len(kept)
    omitted_chars = max(total_chars - kept_chars, 0)
    if omitted_count <= 0:
        return None

    llm_request.contents = kept
    return (
        "Conversation history compaction is active. "
        f"{omitted_count} older content items (~{omitted_chars} chars) were "
        "omitted from this model call to keep the prompt within context. "
        "The persistent ADK session still contains the full chat history; "
        "ask the user to restate details if an omitted old result is needed."
    )

def inject_date(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
):
    """
    Inject runtime context before every model invocation.

    Responsibilities:
      - compact old chat history before it blows up the prompt;
      - prepend an authoritative current-date statement.

    Returning None means "don't short-circuit, proceed with the
    (now modified) request normally". Returning an LlmResponse here
    would skip the model call entirely — we don't want that.
    """
    now = datetime.now(timezone.utc)
    compaction_note = _compact_contents(llm_request)

    context_blocks = [
        f"AUTHORITATIVE CURRENT DATE: {now.strftime('%A, %B %d, %Y')} "
        f"({now.strftime('%Y-%m-%d')} UTC).\n"
        "This is the actual current date — ignore any internal beliefs "
        "about today's date that may come from your training data. When "
        "the user says 'today', 'this month', 'last week', etc., use "
        "this date as the reference point.\n"
    ]
    if compaction_note:
        context_blocks.append(compaction_note + "\n")
    context_block = "\n".join(context_blocks)

    cfg = getattr(llm_request, "config", None)
    if cfg is None:
        return None

    existing = getattr(cfg, "system_instruction", None) or ""
    cfg.system_instruction = f"{context_block}\n{existing}" if existing else context_block

    return None


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

# Ordered list of EU/Europe regions to try, in priority order.
# The first region is the "primary" — most calls land there.
# Fallbacks fire in order on RESOURCE_EXHAUSTED (429).
#
# Override via env var FALLBACK_REGIONS (comma-separated) if you want
# to change order or restrict to fewer regions.
_DEFAULT_FALLBACK_REGIONS = ["europe-west4", "europe-west9", "europe-west3"]


class LiteLlmRouterClient(LiteLLMClient):
    """
    Adapter that lets ADK's LiteLlm use a LiteLLM Router.

    ADK calls `llm_client.acompletion(model=..., messages=..., tools=...)`.
    The Router has the same high-level method, so this adapter only handles
    lazy Router construction and keeps the model aliases explicit.
    """

    def __init__(
        self,
        *,
        model_list: list[dict[str, Any]],
        fallbacks: list[dict[str, list[str]]],
        num_retries: int,
    ):
        self._model_list = model_list
        self._fallbacks = fallbacks
        self._num_retries = num_retries
        self._router = None

    def _get_router(self):
        if self._router is None:
            import litellm

            self._router = litellm.Router(
                model_list=self._model_list,
                fallbacks=self._fallbacks,
                num_retries=self._num_retries,
            )
        return self._router

    async def acompletion(self, model, messages, tools, **kwargs):
        return await self._get_router().acompletion(
            model=model,
            messages=messages,
            tools=tools,
            **kwargs,
        )


def _region_alias(model_name: str, region: str) -> str:
    """
    Build a LiteLLM Router model alias for one Vertex region.

    Keep the `vertex_ai/` prefix so ADK still treats the alias as a
    Vertex/Gemini model for request shaping, headers, and response schema.
    """
    return f"vertex_ai/{model_name}@{region}"


def _get_fallback_regions() -> list[str]:
    """Get fallback region list from env, with sensible default."""
    raw = os.environ.get("FALLBACK_REGIONS")
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(_DEFAULT_FALLBACK_REGIONS)


def _get_primary_region() -> str:
    """Primary region — the first fallback by default, override via env."""
    return os.environ.get("GOOGLE_CLOUD_LOCATION") or _get_fallback_regions()[0]


def make_model(
    model_env_var: str | None = None,
    default_model: str = "gemini-2.5-pro",
    num_retries: int = 2,
) -> LiteLlm:
    """Build the configured LLM backend."""
    provider = os.environ.get("LLM_PROVIDER", "vertex_ai").strip().lower()
    if provider in ("bedrock", "aws_bedrock"):
        return make_bedrock_model(
            model_env_var=model_env_var or "BEDROCK_MODEL_ID",
        )
    if provider in ("vertex", "vertex_ai", "gemini"):
        return make_vertex_model(
            model_env_var=model_env_var or "GEMINI_MODEL",
            default_model=default_model,
            num_retries=num_retries,
        )
    raise ValueError(
        "Unsupported LLM_PROVIDER="
        f"{provider!r}. Expected 'vertex_ai' or 'bedrock'."
    )


def make_bedrock_model(
    model_env_var: str = "BEDROCK_MODEL_ID",
    default_model: str = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
) -> LiteLlm:
    """
    Build a Bedrock Claude model through LiteLLM.

    Authentication options:
      - Bedrock API key: set AWS_BEARER_TOKEN_BEDROCK.
      - Standard boto3 credentials: AWS_ACCESS_KEY_ID /
        AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN, profile, or IAM role.

    Region is controlled by AWS_REGION_NAME. For EU residency, use an EU
    source region such as eu-central-1 and the `eu.` inference profile.
    """
    model_id = _env_first([model_env_var, "BEDROCK_MODEL_ID"], default_model)
    aws_region = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_DEFAULT_REGION")
    api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")

    kwargs: dict[str, Any] = {}
    if aws_region:
        kwargs["aws_region_name"] = aws_region
    if api_key:
        kwargs["api_key"] = api_key

    logger.info(
        "Configured LiteLLM Bedrock model: model=bedrock/%s region=%s auth=%s",
        model_id,
        aws_region,
        "api_key" if api_key else "boto3",
    )

    return LiteLlm(
        model=f"bedrock/{model_id}",
        **kwargs,
    )


def make_vertex_model(
    model_env_var: str = "GEMINI_MODEL",
    default_model: str = "gemini-2.5-pro",
    num_retries: int = 2,
) -> LiteLlm:
    """
    Build a LiteLlm instance with EU-only regional fallback on 429
    RESOURCE_EXHAUSTED, using LiteLLM Router model aliases.

    The primary region is GOOGLE_CLOUD_LOCATION (or the first entry of
    FALLBACK_REGIONS if not set). On rate-limit error, LiteLLM Router
    falls through to the same model in the next regional alias.

    Token streaming must stay disabled at the ADK request layer (the
    OpenWebUI pipeline sends `"streaming": false`). Streaming failures are
    not safely replayable to another region.

    Args:
        model_env_var: Env var that holds the model name (default: GEMINI_MODEL).
            Allows different sub-agents to use different models (e.g. flash
            for cheap ones, pro for orchestrator) by passing different env
            var names.
        default_model: Fallback if the env var is unset (default: gemini-2.5-pro).
        num_retries: Retries within the SAME region before failing over to
            the next. 2 is a good middle ground — covers transient blips
            without delaying region failover too much.

    Returns:
        A LiteLlm configured with a Router-backed primary regional alias
        and EU-only fallback aliases.
    """
    model_name = _env_first([model_env_var, "GEMINI_MODEL"], default_model)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    fallback_regions = _get_fallback_regions()
    primary = _get_primary_region()
    ordered_regions = [primary] + [r for r in fallback_regions if r != primary]
    primary_alias = _region_alias(model_name, primary)
    fallback_aliases = [
        _region_alias(model_name, region)
        for region in ordered_regions
        if region != primary
    ]

    model_list = [
        {
            "model_name": _region_alias(model_name, region),
            "litellm_params": {
                "model": f"vertex_ai/{model_name}",
                "vertex_project": project,
                "vertex_location": region,
            },
        }
        for region in ordered_regions
    ]
    router_fallbacks = [{primary_alias: fallback_aliases}] if fallback_aliases else []

    logger.info(
        "Configured LiteLLM Router model aliases: primary=%s fallbacks=%s",
        primary_alias,
        fallback_aliases,
    )

    return LiteLlm(
        model=primary_alias,
        llm_client=LiteLlmRouterClient(
            model_list=model_list,
            fallbacks=router_fallbacks,
            num_retries=num_retries,
        ),
    )


# Pre-built role models. If ORCHESTRATOR_MODEL / SUB_AGENT_MODEL are unset,
# they fall back to the provider-specific defaults:
#   - Vertex AI: GEMINI_MODEL
#   - Bedrock: BEDROCK_MODEL_ID
ORCHESTRATOR_MODEL = make_model(model_env_var="ORCHESTRATOR_MODEL")
SUB_AGENT_MODEL = make_model(model_env_var="SUB_AGENT_MODEL")

# Backward-compatible alias for older imports.
DEFAULT_MODEL = SUB_AGENT_MODEL
