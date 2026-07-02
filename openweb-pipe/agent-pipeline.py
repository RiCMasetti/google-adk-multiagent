"""
title: Platform Agent Pipeline
author: Riccardo Masetti
description: Adapter between Open WebUI and ADK runtime. It forwards messages to the ADK orchestrator and streams events back to Open WebUI in an OpenAI-compatible format.
required_open_webui_version: 0.5.0
requirements: httpx>=0.27.0
version: 0.1.0
license: MIT
"""

from typing import List, Union, Generator, Iterator, Optional
from pydantic import BaseModel, Field
import json
import logging
import threading
import httpx

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_TOOL_ACTIVITY_LABELS = {
    # ADK delegation
    "transfer_to_agent": "Routing to a specialist agent",
    # Cost analysis
    "get_cost_summary": "Querying AWS Cost Explorer",
    "compare_periods": "Comparing AWS cost periods",
    "get_top_cost_drivers": "Finding AWS cost drivers",
    "forecast_costs": "Forecasting AWS costs",
    "get_hetzner_cost_summary": "Querying Hetzner Cloud costs",
    "list_hetzner_resources": "Listing Hetzner Cloud resources",
    "get_hetzner_top_cost_drivers": "Finding Hetzner cost drivers",
    "get_hetzner_pricing": "Looking up Hetzner pricing",
}


class Pipeline:
    """
    Pipeline that translates between Open WebUI (OpenAI-compatible protocol)
    and ADK runtime (HTTP/SSE).

    For Open WebUI, this "is" a selectable model.
    Internally, it delegates all reasoning to the ADK orchestrator.
    """

    class Valves(BaseModel):
        """Configurations exposed in the Open WebUI admin UI."""

        ADK_BASE_URL: str = Field(
            default="http://platform-agent:8000",
            description="Base URL of the ADK runtime service (adk api_server).",
        )
        ADK_APP_NAME: str = Field(
            default="platform_agent",
            description="Name of the ADK app registered on the runtime (must match the ADK app directory name).",
        )
        REQUEST_TIMEOUT_SECONDS: float = Field(
            default=600.0,
            description="Total timeout for an ADK request. High because tools can be long-running.",
        )
        SHOW_TOOL_ACTIVITY: bool = Field(
            default=True,
            description="Shows an indication in the chat when a tool is invoked.",
        )
        SHOW_REQUEST_PROGRESS: bool = Field(
            default=True,
            description="Shows lightweight request progress while waiting for non-streaming model responses.",
        )
        DEBUG: bool = Field(
            default=False,
            description="Logs received ADK events verbosely.",
        )
        IGNORE_OPENWEBUI_FOLLOWUP_TASKS: bool = Field(
            default=True,
            description="Do not forward OpenWebUI's automatic follow-up suggestion prompts to ADK.",
        )

    def __init__(self):
        # Name shown as "model" in Open WebUI
        self.name = "Platform Agent"
        self.valves = self.Valves()
        self._client: Optional[httpx.Client] = None
        self._client_config: Optional[tuple[str, float]] = None
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()

    async def on_startup(self):
        logger.info("Platform Agent Pipeline starting up")
        client = self._ensure_client()
        # Verify connectivity with ADK at boot, non-blocking
        try:
            r = client.get("/list-apps")
            if r.status_code == 200:
                apps = r.json()
                if self.valves.ADK_APP_NAME not in apps:
                    logger.warning(
                        "ADK app '%s' not found. Available apps: %s",
                        self.valves.ADK_APP_NAME,
                        apps,
                    )
        except Exception as e:
            logger.warning("Could not contact ADK at boot: %s", e)

    async def on_shutdown(self):
        if self._client:
            self._client.close()

    def _ensure_client(self) -> httpx.Client:
        """Create or refresh the ADK HTTP client when valves change."""
        base_url = self.valves.ADK_BASE_URL.rstrip("/")
        timeout_seconds = float(self.valves.REQUEST_TIMEOUT_SECONDS)
        config = (base_url, timeout_seconds)

        if self._client is not None and self._client_config == config:
            return self._client

        if self._client is not None:
            self._client.close()

        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            trust_env=False,
        )
        self._client_config = config
        logger.info("Configured Platform ADK client: base_url=%s", base_url)
        return self._client

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _build_session_id(body: dict) -> str:
        """
        Deterministic mapping between (Open WebUI user, chat) and ADK session.
        Same chat = same ADK session = context continuity.
        """
        user = body.get("user") or {}
        user_id = user.get("id") or user.get("email") or "anonymous"
        chat_id = body.get("chat_id") or body.get("metadata", {}).get("chat_id") or "default"
        # ADK accepts alphanumeric/underscore session_ids
        safe_user = str(user_id).replace("@", "_at_").replace(".", "_")
        return f"owui_{safe_user}_{chat_id}"

    def _ensure_session(self, user_id: str, session_id: str) -> None:
        """Creates the ADK session if it does not exist. Idempotent."""
        url = f"/apps/{self.valves.ADK_APP_NAME}/users/{user_id}/sessions/{session_id}"
        # GET first, if 404 then create
        r = self._client.get(url)
        if r.status_code == 404:
            create = self._client.post(url, json={"state": {}})
            if create.status_code not in (200, 201):
                raise RuntimeError(
                    f"Could not create ADK session: {create.status_code} {create.text}"
                )
        elif r.status_code != 200:
            raise RuntimeError(
                f"Error looking up ADK session: {r.status_code} {r.text}"
            )

    def _lock_for_session(self, session_id: str) -> threading.Lock:
        """
        Return a process-local lock for one ADK session.

        ADK's database session service uses optimistic concurrency. If two
        requests append events to the same session at the same time, one of
        them fails with a stale-session error. OpenWebUI can issue automatic
        background requests (for example follow-up suggestions) while the
        user's turn is still being finalized, so serialize per chat/session.
        """
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    @staticmethod
    def _is_openwebui_followup_task(user_message: str) -> bool:
        """
        Detect OpenWebUI's automatic follow-up-suggestion prompt.

        This is not a real user turn. Forwarding it to ADK pollutes the
        platform-agent session and can race with the real user response.
        """
        msg = (user_message or "").lstrip()
        return (
            msg.startswith("### Task:")
            and "Suggest 3-5 relevant follow-up questions" in msg
        )

    @staticmethod
    def _format_tool_activity(event: dict) -> Optional[str]:
        """Extract a user-facing progress message from an ADK tool call event."""
        content = event.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            fc = part.get("functionCall") or part.get("function_call")
            if fc:
                name = fc.get("name", "tool")
                args = fc.get("args") or {}
                label = _TOOL_ACTIVITY_LABELS.get(name, f"Invoking `{name}`")
                target_agent = args.get("agent_name") or args.get("name")
                if name == "transfer_to_agent" and target_agent:
                    label = f"Routing to `{target_agent}`"
                return f"\n\n_🔧 {label}..._\n\n"
        return None

    @staticmethod
    def _extract_text(event: dict, streamed_partials: bool) -> str:
        """
        Extracts the response text from an ADK event.

        ADK emits for the same turn both `partial: True` events (progressive
        streaming deltas) and a final event with `partial` absent/False containing the
        COMPLETE aggregated text. If we stream both, the text appears twice.

        Strategy:
          - If we see incoming partial events, we yield their deltas and SKIP
            the final aggregated event of the same turn.
          - If we haven't seen partials (model/runtime in non-streaming mode),
            we yield the final event.

        `streamed_partials` indicates whether we have already
        streamed deltas during the current turn. The caller manages and resets it at the end of the turn.
        """
        content = event.get("content") or {}
        parts = content.get("parts") or []
        is_partial = bool(event.get("partial"))

        chunks = []
        for part in parts:
            text = part.get("text")
            if text:
                chunks.append(text)
        text = "".join(chunks)

        if not text:
            return ""

        # Caso 1: delta partial -> sempre da yield-are
        if is_partial:
            return text

        # Caso 2: evento finale aggregato
        # Se abbiamo gi streammato i partial, questo  duplicato -> skip
        if streamed_partials and not is_partial: # Only skip if it's a final event and partials were streamed
            return ""

        # Altrimenti (no streaming dal modello)  l'unico testo che riceviamo
        return text # If it's a final event and no partials were streamed, return it

    @staticmethod
    def _is_turn_complete(event: dict) -> bool:
        """Recognizes the end of an agent's turn. ADK marks the final event
        of the turn with turn_complete=True or with partial absent/False
        and finishReason set (e.g., 'STOP').
        """
        if event.get("turn_complete") is True:
            return True
        # Fallback: evento non-partial con finishReason
        if not event.get("partial"):
            content = event.get("content") or {}
            # Heuristic: se c' contenuto testuale e non  partial, considera
            # il turno chiuso DOPO averlo elaborato. Il chiamante se ne occupa.
            return bool(content.get("text")) # If there's textual content and it's not partial, consider the turn closed after processing.
        return False

    @staticmethod
    def _is_approval_request(event: dict) -> bool:
        """
        Heuristic to recognize an approval request issued by the agent.
        Convention: the agent emits a function call with the name 'request_approval'
        or sets state['pending_approval'].
        """
        content = event.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            fc = part.get("functionCall") or part.get("function_call")
            if fc and fc.get("name") == "request_approval":
                return True
        return False

    @staticmethod
    def _format_approval_block(event: dict) -> str:
        """Renders an approval request as a readable markdown block."""
        content = event.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            fc = part.get("functionCall") or part.get("function_call")
            if fc and fc.get("name") == "request_approval":
                args = fc.get("args") or {}
                action = args.get("action", "unknown")
                params = args.get("params", {})
                reason = args.get("reason", "")
                params_md = json.dumps(params, indent=2, ensure_ascii=False)
                return (
                    "\n\n"
                    "> ⚠️  **Approval required**\n"
                    f"> \n"
                    f"> **Action**: `{action}`\n"
                    f"> \n"
                    f"> **Reason**: {reason}\n"
                    f"> \n"
                    f"> **Parameters**:\n"
                    f"> ```json\n"
                    + "\n".join(f"> {ln}" for ln in params_md.splitlines())
                    + "\n> ```\n"
                    "> \n"
                    "> Reply with `approve` to proceed or `cancel` to abort.\n\n"
                )
        return ""

    # -------------------------------------------------------------------
    # Main entry point called by Open WebUI
    # -------------------------------------------------------------------

    def pipe(
        self,
        user_message: str,
        model_id: str, # Unused, but part of the Open WebUI pipeline signature
        messages: List[dict], # Unused, but part of the Open WebUI pipeline signature
        body: dict, # Contains user and chat_id
    ) -> Union[str, Generator, Iterator]:
        """
        Forwards the user message to the ADK orchestrator and streams back.
        """ # The original comment was already translated in the previous turn, but the docstring was not.
        self._ensure_client()

        if (
            self.valves.IGNORE_OPENWEBUI_FOLLOWUP_TASKS
            and self._is_openwebui_followup_task(user_message)
        ):
            if self.valves.DEBUG:
                logger.info("Ignoring OpenWebUI follow-up suggestion task")
            return ""

        user = body.get("user") or {}
        user_id = str(user.get("id") or user.get("email") or "anonymous").replace("@", "_at_").replace(".", "_")
        session_id = self._build_session_id(body)

        if self.valves.DEBUG:
            logger.info("pipe() user=%s session=%s msg=%r", user_id, session_id, user_message[:120])

        try:
            self._ensure_session(user_id, session_id)
        except Exception as e:
            return f"❌ ADK session error: {e}"

        payload = {
            "appName": self.valves.ADK_APP_NAME,
            "userId": user_id,
            "sessionId": session_id,
            "newMessage": {
                "role": "user",
                "parts": [{"text": user_message}],
            },
            "streaming": True,
        }

        return self._stream_adk(payload, session_id=session_id)

    # -------------------------------------------------------------------
    # Streaming generator
    # -------------------------------------------------------------------

    def _stream_adk(self, payload: dict, session_id: str) -> Generator[str, None, None]:
        """
        Generator that calls ADK's /run_sse and yields text chunks for Open WebUI.
        Open WebUI accepts yielded strings as streaming deltas.
        """ # The original comment was already translated in the previous turn, but the docstring was not.
        url = "/run_sse"
        headers = {"Accept": "text/event-stream"}
        session_lock = self._lock_for_session(session_id)

        try:
            with session_lock:
                yield from self._stream_adk_locked(url, payload, headers)

        except httpx.ReadTimeout:
            yield "\n\n⏱️ Timeout waiting for a response from ADK. The task might still be running on the backend.\n"
        except httpx.HTTPError as e:
            yield f"\n\n❌ Network error connecting to ADK: {e}\n"
        except Exception as e:
            logger.exception("Unhandled error during ADK streaming")
            yield f"\n\n❌ Unexpected error: {e}\n"

    def _stream_adk_locked(
        self,
        url: str,
        payload: dict,
        headers: dict,
    ) -> Generator[str, None, None]:
        """Call ADK while the caller holds the per-session lock."""
        if self.valves.SHOW_REQUEST_PROGRESS:
            yield "\n\n_Routing request..._\n\n"

        with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", errors="replace")
                yield f"\n❌ ADK returned {resp.status_code}: {body[:500]}\n"
                return

            # ADK SSE: ogni evento è una riga "data: {...}"
            # We track if we have already streamed partials in this "turn":
            # in that case, the final aggregated event should be skipped to avoid duplicates.
            streamed_partials = False

            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    if self.valves.DEBUG:
                        logger.warning("Unparsable ADK event: %s", data_str[:200])
                    continue

                if self.valves.DEBUG:
                    logger.info("ADK event: %s", json.dumps(event)[:300])

                # 1. Approval request: special, high-priority rendering
                if self._is_approval_request(event):
                    block = self._format_approval_block(event)
                    if block:
                        yield block
                    continue

                # 2. Tool call: activity indication. A tool call
                #    closes the current textual turn -> reset flag.
                if self.valves.SHOW_TOOL_ACTIVITY:
                    activity = self._format_tool_activity(event)
                    if activity:
                        streamed_partials = False
                        yield activity
                        continue

                # 3. Agent text
                text = self._extract_text(event, streamed_partials)
                is_partial = bool(event.get("partial"))

                if text:
                    yield text
                    if is_partial:
                        streamed_partials = True
                    else:
                        # Final event yielded (we didn't have partials):
                        # turn closed, reset for the next potential turn
                        streamed_partials = False
                else:
                    # Final event skipped because it's a duplicate of partials:
                    # still closes the turn -> reset
                    if not is_partial and streamed_partials:
                        streamed_partials = False
