"""
title: Tina Agent Pipeline
author: platform-team
description: Adapter between Open WebUI and the Tina ADK runtime app. It forwards contract-analysis messages to the Tina orchestrator and streams events back to Open WebUI in an OpenAI-compatible format.
required_open_webui_version: 0.5.0
requirements: httpx>=0.27.0
version: 0.1.0
license: MIT
"""

from typing import Generator, Iterator, List, Optional, Union
import json
import logging
import threading
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


_TOOL_ACTIVITY_LABELS = {
    "transfer_to_agent": "Routing to a specialist agent",
    "ask_contract_documents": "Querying contract documents",
    "chat_with_documents": "Querying contract context",
    "search_documents": "Searching contract documents",
    "resolve_contract_ids": "Resolving contract identifiers",
    "list_contract_document_types": "Listing contract document types",
    "list_document_types": "Listing document types",
    "clear_conversation": "Clearing contract conversation context",
    "request_approval": "Preparing approval request",
}


class Pipeline:
    """
    Pipeline that translates between Open WebUI (OpenAI-compatible protocol)
    and ADK runtime (HTTP/SSE).

    For Open WebUI, this is a selectable model. Internally, it delegates
    reasoning to the Tina ADK orchestrator.
    """

    class Valves(BaseModel):
        """Configurations exposed in the Open WebUI admin UI."""

        ADK_BASE_URL: str = Field(
            default="http://platform-agent:8000",
            description=(
                "Base URL of the ADK runtime service (adk api_server). "
                "For local Docker Compose use http://platform-agent:8000. "
                "For Kubernetes use the ADK service DNS name."
            ),
        )
        ADK_APP_NAME: str = Field(
            default="tina_agent",
            description="Name of the ADK app registered on the runtime.",
        )
        REQUEST_TIMEOUT_SECONDS: float = Field(
            default=600.0,
            description="Total timeout for an ADK request.",
        )
        SHOW_TOOL_ACTIVITY: bool = Field(
            default=True,
            description="Shows an indication in the chat when a tool is invoked.",
        )
        SHOW_REQUEST_PROGRESS: bool = Field(
            default=True,
            description="Shows lightweight request progress while waiting for responses.",
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
        self.name = "Tina Agent"
        self.valves = self.Valves()
        self._client: Optional[httpx.Client] = None
        self._client_config: Optional[tuple[str, float]] = None
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()

    async def on_startup(self):
        logger.info("Tina Agent Pipeline starting up")
        client = self._ensure_client()
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
        logger.info("Configured Tina ADK client: base_url=%s", base_url)
        return self._client

    @staticmethod
    def _build_session_id(body: dict) -> str:
        """
        Deterministic mapping between (Open WebUI user, chat) and ADK session.
        Same chat = same Tina ADK session = context continuity.
        """
        user = body.get("user") or {}
        user_id = user.get("id") or user.get("email") or "anonymous"
        chat_id = body.get("chat_id") or body.get("metadata", {}).get("chat_id") or "default"
        safe_user = str(user_id).replace("@", "_at_").replace(".", "_")
        return f"tina_owui_{safe_user}_{chat_id}"

    def _ensure_session(self, user_id: str, session_id: str) -> None:
        """Creates the ADK session if it does not exist. Idempotent."""
        url = f"/apps/{self.valves.ADK_APP_NAME}/users/{user_id}/sessions/{session_id}"
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
        """Return a process-local lock for one ADK session."""
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    @staticmethod
    def _is_openwebui_followup_task(user_message: str) -> bool:
        """Detect OpenWebUI's automatic follow-up-suggestion prompt."""
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
        Extract response text from an ADK event while avoiding duplicate final
        text when partial streaming has already emitted the same content.
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
        if is_partial:
            return text
        if streamed_partials:
            return ""
        return text

    @staticmethod
    def _is_approval_request(event: dict) -> bool:
        """Recognize an approval request emitted as a function call."""
        content = event.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            fc = part.get("functionCall") or part.get("function_call")
            if fc and fc.get("name") == "request_approval":
                return True
        return False

    @staticmethod
    def _format_approval_block(event: dict) -> str:
        """Render an approval request as a readable markdown block."""
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

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Union[str, Generator, Iterator]:
        """Forward the user message to the Tina ADK orchestrator."""
        self._ensure_client()

        if (
            self.valves.IGNORE_OPENWEBUI_FOLLOWUP_TASKS
            and self._is_openwebui_followup_task(user_message)
        ):
            if self.valves.DEBUG:
                logger.info("Ignoring OpenWebUI follow-up suggestion task")
            return ""

        user = body.get("user") or {}
        user_id = str(
            user.get("id") or user.get("email") or "anonymous"
        ).replace("@", "_at_").replace(".", "_")
        session_id = self._build_session_id(body)

        if self.valves.DEBUG:
            logger.info(
                "pipe() user=%s session=%s msg=%r",
                user_id,
                session_id,
                user_message[:120],
            )

        try:
            self._ensure_session(user_id, session_id)
        except Exception as e:
            return self._format_adk_session_error(e)

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

    def _format_adk_session_error(self, error: Exception) -> str:
        """Add an actionable hint for common Docker/Kubernetes DNS mixups."""
        message = str(error)
        hint = ""
        dns_error = (
            "No address associated with hostname" in message
            or "Name or service not known" in message
            or "Temporary failure in name resolution" in message
            or "[Errno -5]" in message
            or "[Errno -2]" in message
        )
        if dns_error:
            hostname = urlparse(self.valves.ADK_BASE_URL).hostname or ""
            if hostname.endswith(".svc.cluster.local"):
                details = (
                    "That is a Kubernetes cluster DNS name. It resolves only "
                    "from pods running inside the same Kubernetes cluster. If "
                    "this Pipelines runtime is also in Kubernetes, check the "
                    "service name, namespace, NetworkPolicy, and that the ADK "
                    "service exposes the API server port. If Pipelines is "
                    "running locally in Docker Compose, use "
                    "`http://platform-agent:8000` or expose the Kubernetes "
                    "service through an ingress/port-forward."
                )
            else:
                details = (
                    "The Pipelines container cannot resolve that hostname. In "
                    "local Docker Compose, use `http://platform-agent:8000`. "
                    "For Kubernetes, use a service DNS name reachable from the "
                    "Pipelines pod or expose ADK through an ingress."
                )
            hint = (
                "\n\nConfigured ADK_BASE_URL: "
                f"`{self.valves.ADK_BASE_URL}`\n\n"
                f"{details}"
            )
        return f"❌ ADK session error: {message}{hint}"

    def _stream_adk(self, payload: dict, session_id: str) -> Generator[str, None, None]:
        """Call ADK's /run_sse and yield text chunks for Open WebUI."""
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

                if self._is_approval_request(event):
                    block = self._format_approval_block(event)
                    if block:
                        yield block
                    continue

                if self.valves.SHOW_TOOL_ACTIVITY:
                    activity = self._format_tool_activity(event)
                    if activity:
                        streamed_partials = False
                        yield activity
                        continue

                text = self._extract_text(event, streamed_partials)
                is_partial = bool(event.get("partial"))

                if text:
                    yield text
                    streamed_partials = bool(is_partial)
                elif not is_partial and streamed_partials:
                    streamed_partials = False
