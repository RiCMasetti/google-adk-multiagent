"""
Hetzner action tools.

Two distinct execution paths:

  1. Direct Hetzner API actions: soft reboot and hard power cycle.
     Used for ad-hoc actions on individual servers identified by Hetzner
     labels (cluster=, service=). Fast, no Ansible involved. Tracks
     fired action_ids in session state for status polling.

  2. Ansible jobs via GitLab pipeline: trigger a parameterized pipeline
     in the Ansible repo that runs the matching playbook tag. Used for
     anything that's "more than a reboot" — currently OS upgrades, with
     room to grow as the Ansible team adds jobs. Reads catalog from
     job.yml in the repo root.

Approval flow (pending_approval -> request_approval -> confirmed=True)
applies to all mutating actions in both paths.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from google.adk.tools import ToolContext


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config: Hetzner
# ---------------------------------------------------------------------------

_HETZNER_API_BASE = "https://api.hetzner.cloud/v1"
_HETZNER_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _hetzner_token() -> str:
    tok = os.environ.get("HETZNER_TOKEN")
    if not tok:
        raise RuntimeError("HETZNER_TOKEN not set")
    return tok


_hetzner_http: Optional[httpx.Client] = None


def _hetzner_client() -> httpx.Client:
    global _hetzner_http
    if _hetzner_http is None:
        _hetzner_http = httpx.Client(
            base_url=_HETZNER_API_BASE,
            headers={
                "Authorization": f"Bearer {_hetzner_token()}",
                "Content-Type": "application/json",
            },
            timeout=_HETZNER_TIMEOUT,
        )
    return _hetzner_http


# ---------------------------------------------------------------------------
# Config: GitLab (for Ansible pipeline triggering)
# ---------------------------------------------------------------------------

# The Ansible repo that contains job.yml + .gitlab-ci.ops-ai-agent.yml
_ANSIBLE_PROJECT_PATH = os.environ.get(
    "ANSIBLE_PROJECT_PATH", "bullfinch-capital/ops/ansible"
)
# URL-encoded form for GitLab API
_ANSIBLE_PROJECT_ID_OR_PATH = _ANSIBLE_PROJECT_PATH.replace("/", "%2F")

# Default branch where job.yml and .gitlab-ci.ops-ai-agent.yml live
_ANSIBLE_REF = os.environ.get("ANSIBLE_REPO_REF", "main")

# Path to the catalog file inside the repo
_JOB_CATALOG_PATH = os.environ.get("ANSIBLE_JOB_CATALOG_PATH", "job.yml")

_GITLAB_BASE = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/")
_GITLAB_API = f"{_GITLAB_BASE}/api/v4"


def _gitlab_token() -> str:
    tok = os.environ.get("GITLAB_TOKEN")
    if not tok:
        raise RuntimeError("GITLAB_TOKEN not set")
    return tok


_gitlab_http: Optional[httpx.Client] = None


def _gitlab_client() -> httpx.Client:
    global _gitlab_http
    if _gitlab_http is None:
        _gitlab_http = httpx.Client(
            base_url=_GITLAB_API,
            headers={
                "PRIVATE-TOKEN": _gitlab_token(),
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return _gitlab_http


# ---------------------------------------------------------------------------
# Cluster classification (which clusters need typed confirmation)
# ---------------------------------------------------------------------------

def _management_cluster_names() -> set[str]:
    raw = os.environ.get("KUBE_MANAGEMENT_CLUSTERS", "rancher-dev,rancher-prod")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _requires_typed_confirmation(cluster: Optional[str]) -> bool:
    if not cluster:
        return False
    return cluster in _management_cluster_names()


# ---------------------------------------------------------------------------
# Audit log (append-only JSONL)
# ---------------------------------------------------------------------------

_LOG_DIR = Path(os.environ.get("ACTION_LOG_DIR", "/app/action_logs"))


def _audit(event: dict) -> None:
    """Append-only JSONL audit log. Best-effort; failure doesn't block actions."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / f"hetzner_actions_{datetime.now(timezone.utc):%Y-%m}.jsonl"
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Audit log write failed: %s", e)


# ===========================================================================
# PART A — Direct Hetzner API actions (reboot / power cycle of single servers)
# ===========================================================================
#
# These tools operate on Hetzner servers identified by labels (cluster=,
# service=). Used for quick reboots that don't need an Ansible playbook.
# For broader OS-level operations (upgrade, multi-step maintenance), use
# the Ansible pipeline tools in PART B.
# ===========================================================================


def _list_servers(label_selector: Optional[str] = None) -> list[dict]:
    """Page through Hetzner /servers with an optional label selector."""
    out: list[dict] = []
    page = 1
    while True:
        params = {"per_page": 50, "page": page}
        if label_selector:
            params["label_selector"] = label_selector
        r = _hetzner_client().get("/servers", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"Hetzner /servers returned {r.status_code}: {r.text[:300]}")
        data = r.json()
        out.extend(data.get("servers") or [])
        nxt = (data.get("meta") or {}).get("pagination", {}).get("next_page")
        if not nxt:
            return out
        page = nxt


def _resolve_targets(
    cluster: Optional[str],
    server_names: Optional[list[str]],
    role: Optional[str],
    service: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Resolve the user-facing target spec into a concrete list of Hetzner servers.

    Selectors (at least one required, cluster XOR service):
      - cluster: matches label cluster=<value> (K8s nodes)
      - service: matches label service=<value> (non-K8s standalone servers,
                 e.g. nat-gateway-dev, gitlab-runner-generic-1)
      - server_names: explicit list, can combine with cluster/service to narrow
      - role: matches label role=<value> (master/worker), only with cluster

    Identification policy: each resolved server MUST carry EITHER cluster=
    or service=. Servers without either label are refused.
    """
    if not cluster and not server_names and not service:
        return [], (
            "Specify at least one of: cluster (e.g. 'helios-dev'), "
            "service (e.g. 'gitlab-runner-generic-1'), or server_names."
        )

    if cluster and service:
        return [], "cluster and service are mutually exclusive selectors. Pick one."

    label_parts = []
    if cluster:
        label_parts.append(f"cluster={cluster}")
    if service:
        label_parts.append(f"service={service}")
    if role:
        if not cluster:
            return [], "role= filter requires cluster= selector (it's a K8s concept)."
        label_parts.append(f"role={role}")
    label_selector = ",".join(label_parts) if label_parts else None

    candidates = _list_servers(label_selector=label_selector)

    if server_names:
        wanted = set(server_names)
        candidates = [s for s in candidates if s.get("name") in wanted]
        if not cluster and not service:
            extra = _list_servers()
            for s in extra:
                if s.get("name") in wanted and s not in candidates:
                    candidates.append(s)
        missing = wanted - {s.get("name") for s in candidates}
        if missing:
            return [], f"Servers not found: {sorted(missing)}"

    if not candidates:
        return [], (
            f"No servers matched the selector "
            f"(cluster={cluster}, service={service}, role={role}, names={server_names})."
        )

    def _has_identity(s: dict) -> bool:
        labels = s.get("labels") or {}
        return bool(labels.get("cluster") or labels.get("service"))

    unlabelled = [s["name"] for s in candidates if not _has_identity(s)]
    if unlabelled:
        return [], (
            f"Refusing to operate on servers without 'cluster=' or 'service=' "
            f"label: {unlabelled}. Label them via Terraform first."
        )

    return candidates, None


def _summarize(server: dict) -> dict:
    """Compact server representation for approval blocks and audit logs."""
    labels = server.get("labels") or {}
    return {
        "id": server.get("id"),
        "name": server.get("name"),
        "status": server.get("status"),
        "cluster": labels.get("cluster"),
        "service": labels.get("service"),
        "role": labels.get("role"),
        "datacenter": (server.get("datacenter") or {}).get("name"),
    }


# ---------------------------------------------------------------------------
# Tool A1: reboot_servers (soft, ACPI)
# ---------------------------------------------------------------------------

def reboot_servers(
    reason: str,
    cluster: Optional[str] = None,
    service: Optional[str] = None,
    server_names: Optional[list[str]] = None,
    role: Optional[str] = None,
    sequential: bool = True,
    confirmed: bool = False,
    confirmed_cluster_name: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Soft reboot (ACPI shutdown + restart) one or more Hetzner servers via
    the Hetzner API directly (no Ansible pipeline). Use for quick targeted
    reboots; for cluster-wide planned reboots prefer the Ansible job
    `reboot_nodes_*` via run_ansible_job.

    Targets: at least one of cluster / service / server_names (+role).

    Args:
        reason: Mandatory rationale for the reboot.
        cluster: K8s cluster label selector.
        service: Standalone service label selector.
        server_names: Explicit list of names.
        role: Optional role filter (with cluster).
        sequential: If True (default), brief status poll between API calls
            so the next reboot fires only after the previous left "running".
        confirmed: True after user approval.
        confirmed_cluster_name: For management clusters, exact cluster name.

    Returns:
        Pending approval payload (first call) or executed result with action_ids.
    """
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    try:
        servers, err = _resolve_targets(cluster, server_names, role, service=service)
    except RuntimeError as e:
        return {"error": str(e)}
    if err:
        return {"error": err}

    targets = [_summarize(s) for s in servers]
    needs_typed = _requires_typed_confirmation(cluster)

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "reboot_servers",
                "reason": reason,
                "cluster": cluster,
                "service": service,
                "server_names": server_names,
                "role": role,
                "sequential": sequential,
                "targets": targets,
            }
        return {
            "status": "pending_approval",
            "action": "reboot_servers",
            "reason": reason,
            "intended_action": {
                "cluster": cluster, "service": service, "role": role,
                "server_names": server_names, "sequential": sequential,
            },
            "targets": targets,
            "target_count": len(targets),
            "extra_confirmation_required": "type_cluster_name" if needs_typed else None,
            "extra_confirmation_value_expected": cluster if needs_typed else None,
            "approval_message": (
                f"Reboot {len(targets)} server(s) "
                f"({'cluster=' + cluster if cluster else 'service=' + service if service else 'multiple'}). "
                f"Reason: {reason}."
                + (f" MANAGEMENT cluster — type '{cluster}' to confirm." if needs_typed else "")
            ),
        }

    if needs_typed:
        if not confirmed_cluster_name or confirmed_cluster_name.strip().lower() != (cluster or "").lower():
            return {
                "error": (
                    f"Reboot of management cluster '{cluster}' requires the "
                    f"user to type the cluster name. Got: {confirmed_cluster_name!r}."
                )
            }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    return _execute_reboots(servers, reason, sequential, mode="soft", tool_context=tool_context)


# ---------------------------------------------------------------------------
# Tool A2: power_cycle_servers (hard reset)
# ---------------------------------------------------------------------------

def power_cycle_servers(
    reason: str,
    cluster: Optional[str] = None,
    service: Optional[str] = None,
    server_names: Optional[list[str]] = None,
    role: Optional[str] = None,
    sequential: bool = True,
    confirmed: bool = False,
    confirmed_cluster_name: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Hard power cycle (poweroff + poweron) of one or more Hetzner servers.

    Use only when soft reboot has failed or the server is unresponsive to
    ACPI. For cluster-wide planned hard cycles prefer the Ansible job
    `power_off_on_*_node_*` via run_ansible_job.
    """
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    try:
        servers, err = _resolve_targets(cluster, server_names, role, service=service)
    except RuntimeError as e:
        return {"error": str(e)}
    if err:
        return {"error": err}

    targets = [_summarize(s) for s in servers]
    needs_typed = _requires_typed_confirmation(cluster)

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "power_cycle_servers",
                "reason": reason,
                "cluster": cluster, "service": service,
                "server_names": server_names, "role": role,
                "sequential": sequential, "targets": targets,
            }
        return {
            "status": "pending_approval",
            "action": "power_cycle_servers",
            "reason": reason,
            "intended_action": {
                "cluster": cluster, "service": service, "role": role,
                "server_names": server_names, "sequential": sequential,
            },
            "targets": targets,
            "target_count": len(targets),
            "extra_confirmation_required": "type_cluster_name" if needs_typed else None,
            "extra_confirmation_value_expected": cluster if needs_typed else None,
            "approval_message": (
                f"⚠️ HARD POWER CYCLE of {len(targets)} server(s) "
                f"({'cluster=' + cluster if cluster else 'service=' + service if service else 'multiple'}). "
                f"Reason: {reason}. Running processes killed without graceful shutdown."
                + (f" MANAGEMENT cluster — type '{cluster}' to confirm." if needs_typed else "")
            ),
        }

    if needs_typed:
        if not confirmed_cluster_name or confirmed_cluster_name.strip().lower() != (cluster or "").lower():
            return {
                "error": (
                    f"Power cycle of management cluster '{cluster}' requires "
                    f"the user to type the cluster name. Got: {confirmed_cluster_name!r}."
                )
            }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    return _execute_reboots(servers, reason, sequential, mode="hard", tool_context=tool_context)


# ---------------------------------------------------------------------------
# Reboot execution helpers (Hetzner API direct, no SSH)
# ---------------------------------------------------------------------------

# How long we wait inside a single tool invocation for an action to leave
# 'running' before firing the next one in sequential mode. Cap'd to stay
# under SSE proxy idle timeouts.
_SEQUENTIAL_POLL_CAP_SECONDS = 45
_SEQUENTIAL_POLL_INTERVAL_SECONDS = 5


def _execute_reboots(
    servers: list[dict],
    reason: str,
    sequential: bool,
    mode: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Issue Hetzner API reboot/power-cycle calls, track action_ids."""
    results: list[dict] = []
    fired_action_ids: list[int] = []

    for idx, srv in enumerate(servers):
        result = _action_for(srv, mode, reason)
        results.append(result)

        for k in ("action_id", "poweroff_action_id", "poweron_action_id"):
            aid = result.get(k)
            if aid:
                fired_action_ids.append(aid)

        is_last = idx == len(servers) - 1
        if sequential and not is_last and result.get("ok"):
            wait_id = result.get("poweron_action_id") or result.get("action_id")
            if wait_id:
                _poll_action_until_done(wait_id, _SEQUENTIAL_POLL_CAP_SECONDS, _SEQUENTIAL_POLL_INTERVAL_SECONDS)

    success = sum(1 for r in results if r.get("ok"))
    failures = [r for r in results if not r.get("ok")]

    if tool_context is not None and fired_action_ids:
        recent = tool_context.state.get("recent_hetzner_actions") or []
        recent.append({
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode, "reason": reason,
            "action_ids": fired_action_ids,
            "server_names": [s.get("name") for s in servers],
        })
        tool_context.state["recent_hetzner_actions"] = recent[-20:]

    return {
        "status": "executed",
        "mode": mode, "reason": reason, "sequential": sequential,
        "total": len(results), "succeeded": success, "failed": len(failures),
        "results": results, "action_ids": fired_action_ids,
        "summary": (
            f"{mode.upper()} reboot {'commands sent' if not sequential else 'issued sequentially'} "
            f"to {success}/{len(results)} servers. Reason: {reason}. "
            f"Use check_recent_reboot_actions to verify completion."
        ),
        "next_step_hint": (
            "Reboots have been triggered at the Hetzner API level. The actual "
            "VM-level restart takes 30-90s per server. Call "
            "check_recent_reboot_actions in a moment to see whether the "
            "actions have finished."
        ),
    }


def _poll_action_until_done(action_id: int, cap_seconds: int, interval_seconds: int) -> dict:
    deadline = time.monotonic() + max(0, cap_seconds)
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            r = _hetzner_client().get(f"/actions/{action_id}")
            if r.status_code == 200:
                last = (r.json() or {}).get("action") or {}
                if last.get("status") != "running":
                    return last
        except httpx.HTTPError:
            pass
        time.sleep(interval_seconds)
    return last


def _action_for(server: dict, mode: str, reason: str) -> dict:
    sid = server.get("id")
    sname = server.get("name")
    cluster = (server.get("labels") or {}).get("cluster")
    base = {"server_id": sid, "server_name": sname, "cluster": cluster, "mode": mode}

    try:
        if mode == "soft":
            r = _hetzner_client().post(f"/servers/{sid}/actions/reboot")
            if r.status_code not in (200, 201):
                base.update(ok=False, error=f"reboot HTTP {r.status_code}: {r.text[:200]}")
            else:
                action = (r.json() or {}).get("action") or {}
                base.update(ok=True, action_id=action.get("id"), action_status=action.get("status"))
        elif mode == "hard":
            r1 = _hetzner_client().post(f"/servers/{sid}/actions/poweroff")
            if r1.status_code not in (200, 201):
                base.update(ok=False, error=f"poweroff HTTP {r1.status_code}: {r1.text[:200]}")
            else:
                time.sleep(5)
                r2 = _hetzner_client().post(f"/servers/{sid}/actions/poweron")
                if r2.status_code not in (200, 201):
                    base.update(
                        ok=False,
                        error=f"poweron HTTP {r2.status_code}: {r2.text[:200]} — SERVER MAY BE LEFT POWERED OFF",
                    )
                else:
                    a1 = (r1.json() or {}).get("action") or {}
                    a2 = (r2.json() or {}).get("action") or {}
                    base.update(
                        ok=True,
                        poweroff_action_id=a1.get("id"),
                        poweron_action_id=a2.get("id"),
                    )
        else:
            base.update(ok=False, error=f"unknown mode '{mode}'")
    except httpx.HTTPError as e:
        base.update(ok=False, error=f"HTTP error: {e}")
    except Exception as e:
        base.update(ok=False, error=f"Unexpected error: {e}")

    _audit({**base, "reason": reason})
    return base


# ---------------------------------------------------------------------------
# Tool A3: check_recent_reboot_actions
# ---------------------------------------------------------------------------

def check_recent_reboot_actions(
    action_ids: Optional[list[int]] = None,
    server_names: Optional[list[str]] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Check status of recently fired Hetzner reboot/power-cycle action_ids.
    Recovers from chat disconnects: action_ids stored in session state.
    """
    target_ids: list[int] = []
    metadata_lookup: dict[int, dict] = {}

    if tool_context is not None:
        recent: list[dict] = tool_context.state.get("recent_hetzner_actions") or []
        for entry in recent:
            for aid in entry.get("action_ids") or []:
                metadata_lookup.setdefault(aid, {
                    "fired_at": entry.get("fired_at"),
                    "mode": entry.get("mode"),
                    "reason": entry.get("reason"),
                    "server_names_in_batch": entry.get("server_names"),
                })

    if action_ids:
        target_ids = list(action_ids)
    elif server_names:
        wanted = set(server_names)
        if tool_context is not None:
            recent = tool_context.state.get("recent_hetzner_actions") or []
            for entry in recent:
                if any(n in wanted for n in (entry.get("server_names") or [])):
                    target_ids.extend(entry.get("action_ids") or [])
        if not target_ids:
            return {
                "error": (
                    f"No recorded actions found for servers {sorted(wanted)} "
                    f"in this session."
                )
            }
    else:
        if tool_context is None or not metadata_lookup:
            return {
                "error": (
                    "No action_ids provided and no recent actions in session "
                    "state. Pass action_ids or server_names."
                )
            }
        target_ids = sorted(metadata_lookup.keys(), reverse=True)[:20]

    statuses = []
    for aid in target_ids:
        try:
            r = _hetzner_client().get(f"/actions/{aid}")
            if r.status_code == 200:
                act = (r.json() or {}).get("action") or {}
                meta = metadata_lookup.get(aid, {})
                statuses.append({
                    "action_id": aid,
                    "command": act.get("command"),
                    "status": act.get("status"),
                    "progress": act.get("progress"),
                    "started": act.get("started"),
                    "finished": act.get("finished"),
                    "error": act.get("error"),
                    "resources": [
                        {"id": r.get("id"), "type": r.get("type")}
                        for r in (act.get("resources") or [])
                    ],
                    "tracked_mode": meta.get("mode"),
                    "tracked_reason": meta.get("reason"),
                    "tracked_fired_at": meta.get("fired_at"),
                })
            else:
                statuses.append({
                    "action_id": aid,
                    "error": f"Hetzner /actions/{aid} returned {r.status_code}",
                })
        except httpx.HTTPError as e:
            statuses.append({"action_id": aid, "error": f"HTTP error: {e}"})

    by_status: dict[str, int] = {}
    for s in statuses:
        st = s.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1

    return {
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "total_checked": len(statuses),
        "by_status": by_status,
        "actions": statuses,
        "interpretation_hint": (
            "status='success' = action completed at Hetzner level. "
            "status='running' = in progress. status='error' = failed."
        ),
    }


# ===========================================================================
# PART B — Ansible jobs via GitLab pipeline
# ===========================================================================
#
# These tools talk to the GitLab API for the Ansible repo. The catalog of
# available jobs lives in job.yml in the repo root. The agent reads it on
# demand (cached in session state for the conversation lifetime).
# ===========================================================================


_CATALOG_CACHE_KEY = "ansible_job_catalog"


def _fetch_catalog() -> dict:
    """
    Fetch job.yml from the Ansible repo via GitLab API.
    Returns the parsed YAML as a dict.
    """
    file_path_url = _JOB_CATALOG_PATH.replace("/", "%2F")
    url = (
        f"/projects/{_ANSIBLE_PROJECT_ID_OR_PATH}"
        f"/repository/files/{file_path_url}?ref={_ANSIBLE_REF}"
    )
    r = _gitlab_client().get(url)
    if r.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch {_JOB_CATALOG_PATH} from {_ANSIBLE_PROJECT_PATH}: "
            f"HTTP {r.status_code} {r.text[:300]}"
        )
    payload = r.json()
    encoding = payload.get("encoding", "base64")
    content_b64 = payload.get("content", "")
    if encoding != "base64":
        raise RuntimeError(f"Unexpected encoding from GitLab API: {encoding}")
    raw = base64.b64decode(content_b64).decode("utf-8")
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"job.yml did not parse to a dict: {type(parsed)}")
    return parsed


def _get_catalog(tool_context: Optional[ToolContext] = None) -> dict:
    """Get catalog from session cache or fetch fresh."""
    if tool_context is not None:
        cached = tool_context.state.get(_CATALOG_CACHE_KEY)
        if cached:
            return cached
    catalog = _fetch_catalog()
    if tool_context is not None:
        tool_context.state[_CATALOG_CACHE_KEY] = catalog
    return catalog


def _find_job(catalog: dict, job_name: str) -> Optional[dict]:
    for job in catalog.get("jobs") or []:
        if job.get("name") == job_name:
            return job
    return None


def _validate_targets(
    catalog: dict,
    job_def: dict,
    nodes_input: str,
) -> tuple[Optional[list[str]], Optional[str]]:
    """
    Validate a comma-separated NODES_AI_AGENT string against the job's
    allowed_target_groups.

    Returns (valid_node_list, error). On error, valid_node_list is None.
    """
    raw = [n.strip() for n in (nodes_input or "").split(",") if n.strip()]
    if not raw:
        return None, "NODES_AI_AGENT cannot be empty."

    allowed_groups = job_def.get("allowed_target_groups") or []
    if not allowed_groups:
        return None, (
            f"Job '{job_def.get('name')}' has no allowed_target_groups but "
            f"NODES_AI_AGENT was provided. This job either takes no targets "
            f"or its catalog entry is misconfigured."
        )

    host_groups = (catalog.get("host_groups") or {})

    # Build the union of valid_values across allowed groups
    valid_set: set[str] = set()
    for g in allowed_groups:
        group_def = host_groups.get(g) or {}
        for h in (group_def.get("valid_values") or []):
            valid_set.add(h)

    # Refuse gitlab-runner targets always (self-execution risk)
    runner_targets = [n for n in raw if n.startswith("gitlab-runner-")]
    if runner_targets:
        return None, (
            f"GitLab runners {runner_targets} cannot be targeted by Ansible "
            f"jobs (self-execution risk: a job running on a runner cannot "
            f"safely upgrade or restart that runner). Manage runners manually."
        )

    invalid = [n for n in raw if n not in valid_set]
    if invalid:
        return None, (
            f"Invalid target(s) for job '{job_def.get('name')}': {invalid}. "
            f"Allowed values from {allowed_groups}: {sorted(valid_set)}"
        )

    return raw, None


def _job_requires_typed_confirmation(catalog: dict, job_def: dict) -> tuple[bool, Optional[str]]:
    """
    A job requires typed confirmation if either:
      - the job declares it directly, OR
      - any of its allowed_target_groups is is_management=true
    Returns (required, expected_value).
    """
    if job_def.get("requires_typed_confirmation"):
        return True, job_def.get("typed_confirmation_value")

    host_groups = catalog.get("host_groups") or {}
    for g in job_def.get("allowed_target_groups") or []:
        gdef = host_groups.get(g) or {}
        if gdef.get("is_management"):
            return True, gdef.get("cluster")

    return False, None


# ---------------------------------------------------------------------------
# Tool B1: list_ansible_jobs
# ---------------------------------------------------------------------------

def list_ansible_jobs(
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Read the catalog of Ansible jobs available via GitLab pipeline.
    Read-only; no approval needed. Use to answer "what can the agent do
    via Ansible?", or to discover the right job for a user's request.

    Returns:
        dict with `host_groups` and `jobs` from job.yml in the Ansible repo.
    """
    try:
        catalog = _get_catalog(tool_context)
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {"error": f"GitLab API error fetching catalog: {e}"}

    jobs = catalog.get("jobs") or []
    host_groups = catalog.get("host_groups") or {}

    # Compact summary for the model — full catalog is large
    job_summaries = []
    for j in jobs:
        job_summaries.append({
            "name": j.get("name"),
            "description": j.get("description"),
            "allowed_target_groups": j.get("allowed_target_groups") or [],
            "parameters": [p.get("name") for p in (j.get("parameters") or [])],
            "destructive": j.get("destructive", False),
            "requires_typed_confirmation": j.get("requires_typed_confirmation", False),
            "estimated_minutes": j.get("estimated_minutes"),
        })

    group_summaries = {}
    for gname, gdef in host_groups.items():
        group_summaries[gname] = {
            "cluster": gdef.get("cluster"),
            "description": gdef.get("description"),
            "is_management": gdef.get("is_management", False),
            "valid_values": gdef.get("valid_values") or [],
        }

    return {
        "schema_version": catalog.get("schema_version"),
        "ansible_project": _ANSIBLE_PROJECT_PATH,
        "catalog_ref": _ANSIBLE_REF,
        "host_groups": group_summaries,
        "jobs": job_summaries,
        "job_count": len(jobs),
    }


# ---------------------------------------------------------------------------
# Tool B2: run_ansible_job
# ---------------------------------------------------------------------------

def run_ansible_job(
    job_name: str,
    reason: str,
    nodes: Optional[str] = None,
    confirmed: bool = False,
    confirmed_value: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Trigger a parameterized GitLab pipeline that runs the named Ansible job.

    The catalog (job.yml) is the source of truth for what `job_name` values
    are valid, what targets each job accepts, and which jobs require typed
    confirmation. Use list_ansible_jobs to discover available jobs.

    Args:
        job_name: Catalog name (e.g. "reboot_nodes_dev", "upgrade_nodes_prod").
            Mapped 1:1 to OPS_AI_AGENT pipeline variable.
        reason: Mandatory reason for the action. Logged to audit trail.
        nodes: Comma-separated Ansible host names. Required iff the job
            declares a NODES_AI_AGENT parameter. Validated against the
            job's allowed_target_groups.valid_values.
        confirmed: True only after user approval.
        confirmed_value: For jobs requiring typed confirmation, the exact
            string the user typed (typically cluster name).

    Returns:
        Pending approval payload (first call) or executed result with
        pipeline_id and web_url.
    """
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    try:
        catalog = _get_catalog(tool_context)
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {"error": f"GitLab API error fetching catalog: {e}"}

    job_def = _find_job(catalog, job_name)
    if not job_def:
        available = [j.get("name") for j in catalog.get("jobs") or []]
        return {
            "error": (
                f"Unknown Ansible job '{job_name}'. Available jobs: {available}. "
                f"Use list_ansible_jobs to see catalog details."
            )
        }

    # Parameter validation
    declared_params = {p.get("name"): p for p in (job_def.get("parameters") or [])}
    validated_targets: Optional[list[str]] = None

    if "NODES_AI_AGENT" in declared_params:
        if nodes is None or not nodes.strip():
            return {
                "error": (
                    f"Job '{job_name}' requires 'nodes' (NODES_AI_AGENT). "
                    f"Allowed groups: {job_def.get('allowed_target_groups')}. "
                    f"Use list_ansible_jobs to see valid host names."
                )
            }
        validated_targets, err = _validate_targets(catalog, job_def, nodes)
        if err:
            return {"error": err}
    else:
        if nodes is not None and nodes.strip():
            return {
                "error": (
                    f"Job '{job_name}' does not accept 'nodes' (the pipeline "
                    f"job has hardcoded targets). Pass nodes=None or leave empty."
                )
            }

    # Typed confirmation requirement
    needs_typed, typed_expected = _job_requires_typed_confirmation(catalog, job_def)

    # Approval flow
    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "run_ansible_job",
                "job_name": job_name,
                "reason": reason,
                "nodes": validated_targets,
                "needs_typed": needs_typed,
                "typed_expected": typed_expected,
            }

        approval_msg = (
            f"Run Ansible job '{job_name}'. {job_def.get('description', '')} "
            f"Reason: {reason}."
            + (f" Targets: {validated_targets}." if validated_targets else "")
            + (f" Estimated duration: {job_def.get('estimated_minutes')} minutes." if job_def.get('estimated_minutes') else "")
            + (f" REQUIRES typed confirmation — type '{typed_expected}' to confirm." if needs_typed else "")
        )

        return {
            "status": "pending_approval",
            "action": "run_ansible_job",
            "reason": reason,
            "intended_action": {
                "job_name": job_name,
                "nodes": validated_targets,
                "ansible_project": _ANSIBLE_PROJECT_PATH,
            },
            "job_description": job_def.get("description"),
            "destructive": job_def.get("destructive", False),
            "estimated_minutes": job_def.get("estimated_minutes"),
            "extra_confirmation_required": "type_value" if needs_typed else None,
            "extra_confirmation_value_expected": typed_expected if needs_typed else None,
            "approval_message": approval_msg,
        }

    # Confirmed path
    if needs_typed:
        if not confirmed_value or confirmed_value.strip().lower() != (typed_expected or "").lower():
            return {
                "error": (
                    f"Job '{job_name}' requires the user to type "
                    f"'{typed_expected}'. Got: {confirmed_value!r}. Refusing."
                )
            }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    # Trigger the pipeline
    return _trigger_pipeline(
        job_name=job_name,
        reason=reason,
        validated_targets=validated_targets,
        tool_context=tool_context,
    )


def _trigger_pipeline(
    job_name: str,
    reason: str,
    validated_targets: Optional[list[str]],
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """POST to GitLab /pipeline endpoint with OPS_AI_AGENT (+ NODES_AI_AGENT)."""
    variables = [{"key": "OPS_AI_AGENT", "value": job_name}]
    if validated_targets:
        variables.append({
            "key": "NODES_AI_AGENT",
            "value": ",".join(validated_targets),
        })

    payload = {"ref": _ANSIBLE_REF, "variables": variables}
    url = f"/projects/{_ANSIBLE_PROJECT_ID_OR_PATH}/pipeline"

    try:
        r = _gitlab_client().post(url, json=payload)
    except httpx.HTTPError as e:
        return {"error": f"GitLab API error triggering pipeline: {e}"}

    if r.status_code not in (200, 201):
        return {
            "error": (
                f"Pipeline trigger failed: HTTP {r.status_code} {r.text[:300]}"
            )
        }

    pipeline = r.json() or {}
    pipeline_id = pipeline.get("id")
    web_url = pipeline.get("web_url")

    # Track in session state for status checks later
    if tool_context is not None and pipeline_id:
        recent = tool_context.state.get("recent_ansible_pipelines") or []
        recent.append({
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "job_name": job_name,
            "reason": reason,
            "nodes": validated_targets,
            "pipeline_id": pipeline_id,
            "web_url": web_url,
        })
        tool_context.state["recent_ansible_pipelines"] = recent[-20:]

    _audit({
        "type": "run_ansible_job",
        "job_name": job_name,
        "reason": reason,
        "nodes": validated_targets,
        "pipeline_id": pipeline_id,
        "web_url": web_url,
        "ok": True,
    })

    return {
        "status": "triggered",
        "job_name": job_name,
        "pipeline_id": pipeline_id,
        "web_url": web_url,
        "ref": pipeline.get("ref"),
        "nodes": validated_targets,
        "reason": reason,
        "next_step_hint": (
            f"Pipeline {pipeline_id} has been triggered. Use "
            f"check_ansible_job_status to see progress and logs."
        ),
    }


# ---------------------------------------------------------------------------
# Tool B3: check_ansible_job_status
# ---------------------------------------------------------------------------

def check_ansible_job_status(
    pipeline_id: Optional[int] = None,
    include_logs: bool = True,
    log_tail_lines: int = 200,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Check status of a triggered Ansible pipeline. Returns pipeline status,
    per-job status (a pipeline may run multiple jobs but for OPS_AI_AGENT
    only one matches the rule), and the trace tail of the matching job.

    If pipeline_id is omitted, returns the most recent pipeline triggered
    in this session.

    Args:
        pipeline_id: Specific pipeline to query. If omitted, uses the most
            recent one from session state.
        include_logs: If True (default), fetches the trace and returns its tail.
        log_tail_lines: How many trailing lines of the trace to include.
    """
    target_id = pipeline_id

    if target_id is None:
        if tool_context is None:
            return {"error": "pipeline_id required (no session state available)."}
        recent: list[dict] = tool_context.state.get("recent_ansible_pipelines") or []
        if not recent:
            return {"error": "No recent Ansible pipelines found in session state."}
        target_id = recent[-1].get("pipeline_id")
        if not target_id:
            return {"error": "Could not resolve a pipeline_id from session state."}

    # Pipeline-level status
    try:
        r = _gitlab_client().get(
            f"/projects/{_ANSIBLE_PROJECT_ID_OR_PATH}/pipelines/{target_id}"
        )
    except httpx.HTTPError as e:
        return {"error": f"GitLab API error: {e}"}
    if r.status_code != 200:
        return {"error": f"Pipeline {target_id} fetch HTTP {r.status_code}: {r.text[:300]}"}
    pipeline = r.json() or {}

    # Jobs list — find the one that actually ran (the one matching OPS_AI_AGENT rule)
    try:
        rj = _gitlab_client().get(
            f"/projects/{_ANSIBLE_PROJECT_ID_OR_PATH}/pipelines/{target_id}/jobs"
        )
    except httpx.HTTPError as e:
        return {"error": f"GitLab API error (jobs): {e}"}

    jobs_summary = []
    matching_job_id: Optional[int] = None
    if rj.status_code == 200:
        for j in (rj.json() or []):
            jobs_summary.append({
                "id": j.get("id"),
                "name": j.get("name"),
                "stage": j.get("stage"),
                "status": j.get("status"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "duration_seconds": j.get("duration"),
            })
            # Pick the job that actually ran (skipped jobs are not what we want)
            if j.get("status") not in ("skipped", "manual") and matching_job_id is None:
                matching_job_id = j.get("id")

    log_tail: Optional[str] = None
    if include_logs and matching_job_id:
        try:
            rl = _gitlab_client().get(
                f"/projects/{_ANSIBLE_PROJECT_ID_OR_PATH}/jobs/{matching_job_id}/trace"
            )
            if rl.status_code == 200:
                full_trace = rl.text or ""
                lines = full_trace.splitlines()
                if len(lines) > log_tail_lines:
                    log_tail = "...[truncated]...\n" + "\n".join(lines[-log_tail_lines:])
                else:
                    log_tail = full_trace
            else:
                log_tail = f"[failed to fetch trace: HTTP {rl.status_code}]"
        except httpx.HTTPError as e:
            log_tail = f"[trace fetch error: {e}]"

    return {
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_id": target_id,
        "pipeline_status": pipeline.get("status"),
        "pipeline_web_url": pipeline.get("web_url"),
        "pipeline_created_at": pipeline.get("created_at"),
        "pipeline_started_at": pipeline.get("started_at"),
        "pipeline_finished_at": pipeline.get("finished_at"),
        "pipeline_duration_seconds": pipeline.get("duration"),
        "jobs": jobs_summary,
        "active_job_id": matching_job_id,
        "log_tail": log_tail,
        "interpretation_hint": (
            "pipeline_status='success' = all jobs completed; 'failed' = at least "
            "one failed; 'running' = in progress; 'pending' = queued waiting for "
            "runner. The Ansible playbook output is in the active job's log_tail."
        ),
    }


# ===========================================================================
# Tool C: request_approval (sentinel — same pattern as gitlab_agent)
# ===========================================================================

def request_approval(action: str, params: dict, reason: str) -> dict:
    """
    Sentinel tool. The agent emits this function call when a tool returned
    `pending_approval`. The Pipeline intercepts it and renders an approval
    block in the chat UI.

    For actions requiring typed confirmation, params should include:
      extra_confirmation_required: "type_value" or "type_cluster_name"
      extra_confirmation_value_expected: "<value-the-user-must-type>"
    """
    return {
        "approval_pending": True,
        "action": action,
        "params": params,
        "reason": reason,
        "instruction_for_user": (
            "Reply 'approve' to proceed, or 'cancel' to abort. If the action "
            "requires typed confirmation (management cluster), type the "
            "expected value to confirm."
        ),
    }
