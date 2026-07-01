"""
Kubernetes action tools.

Mutating operations on workload clusters only. Management clusters
(rancher-dev, rancher-prod) are explicitly refused — the tools check
`is_workload_cluster()` before any API call.

Operations:
  - list_triggerable_jobs: list CronJobs available for manual trigger (read-only)
  - trigger_job:           create a one-off Job from a CronJob's job template
  - delete_job:            delete a Kubernetes Job resource
  - suspend_cronjob:       set spec.suspend=true on a CronJob
  - unsuspend_cronjob:     set spec.suspend=false on a CronJob
  - delete_pod:            delete (terminate) a pod; controller reschedules it
  - request_approval:      approval-gate sentinel

All mutating tools follow the pending_approval -> request_approval ->
confirmed=True flow from hetzner_action_agent.

RBAC requirement: a dedicated ServiceAccount with:
  - delete on pods
  - delete/create on jobs
  - patch on cronjobs
The read-only analysis SA is insufficient — use a separate kubeconfig.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from google.adk.tools import ToolContext

from ..k8s_analysis_agent.cluster_registry import (
    batch_v1,
    core_v1,
    is_workload_cluster,
    list_known_clusters,
    list_workload_clusters,
)


logger = logging.getLogger(__name__)

_LOG_DIR = Path(os.environ.get("ACTION_LOG_DIR", "/app/action_logs"))


def _audit(event: dict) -> None:
    """Append-only JSONL audit log. Best-effort; failure doesn't block actions."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / f"k8s_actions_{datetime.now(timezone.utc):%Y-%m}.jsonl"
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Audit log write failed: %s", e)


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except ApiException as e:
        return None, f"K8s API error {e.status}: {e.reason}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


def _check_workload_cluster(cluster: str) -> Optional[dict]:
    """Return an error dict if the cluster is unknown or is a management cluster."""
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}
    if not is_workload_cluster(cluster):
        return {
            "error": (
                f"Cluster '{cluster}' is a management cluster. "
                f"Mutating actions are restricted to workload clusters: "
                f"{list_workload_clusters()}."
            )
        }
    return None


# ---------------------------------------------------------------------------
# Tool 1: list_triggerable_jobs (READ-ONLY, no approval needed)
# ---------------------------------------------------------------------------

def list_triggerable_jobs(
    cluster: Optional[str] = None,
    namespace: Optional[str] = None,
) -> dict:
    """
    List CronJobs available for manual one-off triggering across workload
    clusters. Read-only; no approval required.

    Args:
        cluster: Specific workload cluster name, or None for all workload clusters.
        namespace: Optional namespace filter.

    Returns:
        dict with CronJobs grouped by cluster, including schedule and suspend state.
    """
    if cluster is not None:
        err = _check_workload_cluster(cluster)
        if err:
            return err
        targets = [cluster]
    else:
        targets = list_workload_clusters()

    results: dict = {}
    for c in targets:
        batch = batch_v1(c)
        if namespace:
            resp, err = _safe_call(batch.list_namespaced_cron_job, namespace, _request_timeout=20)
        else:
            resp, err = _safe_call(batch.list_cron_job_for_all_namespaces, _request_timeout=20)
        if err:
            results[c] = {"error": err}
            continue
        crons = resp.items if resp else []
        results[c] = {
            "count": len(crons),
            "cronjobs": [
                {
                    "name": cj.metadata.name,
                    "namespace": cj.metadata.namespace,
                    "schedule": cj.spec.schedule,
                    "suspended": bool(cj.spec.suspend),
                    "active_jobs": len(cj.status.active or []),
                    "last_schedule_time": (
                        cj.status.last_schedule_time.isoformat()
                        if cj.status.last_schedule_time else None
                    ),
                    "last_successful_time": (
                        cj.status.last_successful_time.isoformat()
                        if cj.status.last_successful_time else None
                    ),
                }
                for cj in crons
            ],
        }

    return {
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "namespace_filter": namespace,
        "clusters": results,
    }


# ---------------------------------------------------------------------------
# Tool 2: trigger_job (create Job from CronJob template)
# ---------------------------------------------------------------------------

def trigger_job(
    cluster: str,
    namespace: str,
    cronjob_name: str,
    reason: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Manually trigger a one-off Job from an existing CronJob's job template.
    Equivalent to `kubectl create job --from=cronjob/<name>`.

    Args:
        cluster: Workload cluster name.
        namespace: Namespace of the CronJob.
        cronjob_name: Name of the CronJob to trigger.
        reason: Mandatory reason for the action (logged to audit trail).
        confirmed: True only after user approval.

    Returns:
        Pending approval payload (first call) or the created Job details.
    """
    err = _check_workload_cluster(cluster)
    if err:
        return err
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    # Read the CronJob upfront (even before confirmation) to validate it exists.
    batch = batch_v1(cluster)
    cj, api_err = _safe_call(
        batch.read_namespaced_cron_job, cronjob_name, namespace, _request_timeout=15
    )
    if api_err:
        return {"error": api_err}

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "trigger_job",
                "cluster": cluster,
                "namespace": namespace,
                "cronjob_name": cronjob_name,
                "reason": reason,
            }
        return {
            "status": "pending_approval",
            "action": "trigger_job",
            "reason": reason,
            "intended_action": {
                "cluster": cluster,
                "namespace": namespace,
                "cronjob_name": cronjob_name,
                "schedule": cj.spec.schedule,
            },
            "approval_message": (
                f"Trigger a one-off Job from CronJob '{cronjob_name}' "
                f"in namespace '{namespace}' on cluster '{cluster}'. "
                f"Schedule: {cj.spec.schedule}. Reason: {reason}."
            ),
        }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    # Build Job name: cronjob-<name>-manual-<timestamp>, capped at 63 chars.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base = f"{cronjob_name}-manual-{ts}"
    job_name = base[:63]

    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            annotations={
                "triggered-by": "k8s-action-agent",
                "reason": reason,
            },
            owner_references=[
                k8s_client.V1OwnerReference(
                    api_version="batch/v1",
                    kind="CronJob",
                    name=cj.metadata.name,
                    uid=cj.metadata.uid,
                    controller=False,
                    block_owner_deletion=False,
                )
            ],
        ),
        spec=cj.spec.job_template.spec,
    )

    created, api_err = _safe_call(
        batch.create_namespaced_job, namespace, job, _request_timeout=15
    )
    if api_err:
        return {"error": f"Failed to create Job: {api_err}"}

    _audit({
        "type": "trigger_job",
        "cluster": cluster,
        "namespace": namespace,
        "cronjob_name": cronjob_name,
        "job_name": created.metadata.name,
        "reason": reason,
    })

    return {
        "status": "triggered",
        "cluster": cluster,
        "namespace": namespace,
        "cronjob_name": cronjob_name,
        "job_name": created.metadata.name,
        "reason": reason,
        "next_step_hint": (
            f"Job '{created.metadata.name}' created in '{namespace}'. "
            f"Use k8s_analysis_agent to monitor its status."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 3: delete_job
# ---------------------------------------------------------------------------

def delete_job(
    cluster: str,
    namespace: str,
    name: str,
    reason: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Delete a Kubernetes Job resource. Pods owned by the Job are cleaned up
    by Kubernetes background garbage collection.

    Args:
        cluster: Workload cluster name.
        namespace: Namespace of the Job.
        name: Job name.
        reason: Mandatory reason for the action.
        confirmed: True only after user approval.
    """
    err = _check_workload_cluster(cluster)
    if err:
        return err
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "delete_job",
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "reason": reason,
            }
        return {
            "status": "pending_approval",
            "action": "delete_job",
            "reason": reason,
            "intended_action": {
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
            },
            "approval_message": (
                f"Delete Job '{name}' in namespace '{namespace}' "
                f"on cluster '{cluster}'. Owned pods will be garbage-collected. "
                f"Reason: {reason}."
            ),
        }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    batch = batch_v1(cluster)
    _, api_err = _safe_call(
        batch.delete_namespaced_job,
        name=name,
        namespace=namespace,
        body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
        _request_timeout=15,
    )
    if api_err:
        return {"error": f"Failed to delete Job: {api_err}"}

    _audit({
        "type": "delete_job",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
    })

    return {
        "status": "deleted",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
        "note": "Job and its pods will be cleaned up by Kubernetes garbage collection.",
    }


# ---------------------------------------------------------------------------
# Tool 4: suspend_cronjob
# ---------------------------------------------------------------------------

def suspend_cronjob(
    cluster: str,
    namespace: str,
    name: str,
    reason: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Suspend a CronJob by setting spec.suspend=true. No new Jobs will be
    created from this CronJob until it is unsuspended.

    Args:
        cluster: Workload cluster name.
        namespace: Namespace of the CronJob.
        name: CronJob name.
        reason: Mandatory reason for the action.
        confirmed: True only after user approval.
    """
    err = _check_workload_cluster(cluster)
    if err:
        return err
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "suspend_cronjob",
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "reason": reason,
            }
        return {
            "status": "pending_approval",
            "action": "suspend_cronjob",
            "reason": reason,
            "intended_action": {
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
            },
            "approval_message": (
                f"Suspend CronJob '{name}' in namespace '{namespace}' "
                f"on cluster '{cluster}'. No new Jobs will be scheduled "
                f"until unsuspended. Reason: {reason}."
            ),
        }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    batch = batch_v1(cluster)
    _, api_err = _safe_call(
        batch.patch_namespaced_cron_job,
        name=name,
        namespace=namespace,
        body={"spec": {"suspend": True}},
        _request_timeout=15,
    )
    if api_err:
        return {"error": f"Failed to suspend CronJob: {api_err}"}

    _audit({
        "type": "suspend_cronjob",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
    })

    return {
        "status": "suspended",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
        "note": "CronJob is now suspended. Use unsuspend_cronjob to resume scheduling.",
    }


# ---------------------------------------------------------------------------
# Tool 5: unsuspend_cronjob
# ---------------------------------------------------------------------------

def unsuspend_cronjob(
    cluster: str,
    namespace: str,
    name: str,
    reason: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Resume a suspended CronJob by setting spec.suspend=false.

    Args:
        cluster: Workload cluster name.
        namespace: Namespace of the CronJob.
        name: CronJob name.
        reason: Mandatory reason for the action.
        confirmed: True only after user approval.
    """
    err = _check_workload_cluster(cluster)
    if err:
        return err
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "unsuspend_cronjob",
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "reason": reason,
            }
        return {
            "status": "pending_approval",
            "action": "unsuspend_cronjob",
            "reason": reason,
            "intended_action": {
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
            },
            "approval_message": (
                f"Unsuspend CronJob '{name}' in namespace '{namespace}' "
                f"on cluster '{cluster}'. Normal scheduling will resume. "
                f"Reason: {reason}."
            ),
        }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    batch = batch_v1(cluster)
    _, api_err = _safe_call(
        batch.patch_namespaced_cron_job,
        name=name,
        namespace=namespace,
        body={"spec": {"suspend": False}},
        _request_timeout=15,
    )
    if api_err:
        return {"error": f"Failed to unsuspend CronJob: {api_err}"}

    _audit({
        "type": "unsuspend_cronjob",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
    })

    return {
        "status": "unsuspended",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "reason": reason,
        "note": "CronJob is now active. New Jobs will be scheduled as per the cron schedule.",
    }


# ---------------------------------------------------------------------------
# Tool 6: delete_pod
# ---------------------------------------------------------------------------

def delete_pod(
    cluster: str,
    namespace: str,
    name: str,
    reason: str,
    confirmed: bool = False,
    grace_period_seconds: Optional[int] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Delete (terminate) a pod. If the pod is owned by a Deployment,
    StatefulSet, or DaemonSet, Kubernetes schedules a replacement
    automatically. This is the correct way to "restart" a misbehaving pod.

    Args:
        cluster: Workload cluster name.
        namespace: Namespace of the pod.
        name: Pod name.
        reason: Mandatory reason for the action.
        confirmed: True only after user approval.
        grace_period_seconds: Override the pod's graceful termination period.
            None = use the pod's own terminationGracePeriodSeconds.
            0 = immediate forced termination (SIGKILL). Use only for
            completely unresponsive pods.
    """
    err = _check_workload_cluster(cluster)
    if err:
        return err
    if not reason or not reason.strip():
        return {"error": "A non-empty 'reason' is mandatory."}

    force_note = (
        " IMMEDIATE termination (grace_period=0 — SIGKILL)."
        if grace_period_seconds == 0
        else ""
    )

    if not confirmed:
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "delete_pod",
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "grace_period_seconds": grace_period_seconds,
                "reason": reason,
            }
        return {
            "status": "pending_approval",
            "action": "delete_pod",
            "reason": reason,
            "intended_action": {
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "grace_period_seconds": grace_period_seconds,
            },
            "approval_message": (
                f"Delete pod '{name}' in namespace '{namespace}' "
                f"on cluster '{cluster}'.{force_note} "
                f"If owned by a controller, a replacement will be scheduled. "
                f"Reason: {reason}."
            ),
        }

    if tool_context is not None and tool_context.state.get("pending_action") is not None:
        tool_context.state["pending_action"] = None

    v1 = core_v1(cluster)
    delete_opts = k8s_client.V1DeleteOptions()
    if grace_period_seconds is not None:
        delete_opts.grace_period_seconds = grace_period_seconds

    _, api_err = _safe_call(
        v1.delete_namespaced_pod,
        name=name,
        namespace=namespace,
        body=delete_opts,
        _request_timeout=15,
    )
    if api_err:
        return {"error": f"Failed to delete pod: {api_err}"}

    _audit({
        "type": "delete_pod",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "grace_period_seconds": grace_period_seconds,
        "reason": reason,
    })

    return {
        "status": "deleted",
        "cluster": cluster,
        "namespace": namespace,
        "name": name,
        "grace_period_seconds": grace_period_seconds,
        "reason": reason,
        "note": (
            "Pod deletion initiated. If managed by a Deployment/StatefulSet/"
            "DaemonSet, a replacement will be scheduled automatically. "
            "Use k8s_analysis_agent to verify the new pod reaches Running state."
        ),
    }


# ---------------------------------------------------------------------------
# Sentinel: request_approval
# ---------------------------------------------------------------------------

def request_approval(action: str, params: dict, reason: str) -> dict:
    """
    Sentinel tool. Emitted when a mutating tool returned `pending_approval`.
    The Pipeline intercepts it and renders an approval block in the chat UI.
    """
    return {
        "approval_pending": True,
        "action": action,
        "params": params,
        "reason": reason,
        "instruction_for_user": (
            "Reply 'approve' to proceed, or 'cancel' to abort."
        ),
    }
