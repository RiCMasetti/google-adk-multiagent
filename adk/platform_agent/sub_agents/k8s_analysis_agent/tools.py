"""
Read-only Kubernetes analysis tools.

Design rules:
  - NO Secret reads anywhere. The ServiceAccount RBAC also forbids it.
  - ConfigMap values are NEVER returned. Only metadata (name, namespace,
    keys, sizes). The agent's instruction reinforces this rule.
  - Tools are coarse-grained "compound" operations (cluster overview,
    workload deep-dive, find failing) rather than thin API wrappers.
  - Cross-cluster operations parallelise via threadpool to avoid serial
    latency over 5 clusters.

These tools do not perform any mutating operation. The future action
agent (separate) will live in a different module with its own RBAC.
"""
from __future__ import annotations

import concurrent.futures
import re
from datetime import datetime, timezone
from typing import Any, Optional

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from .cluster_registry import (
    apps_v1,
    batch_v1,
    core_v1,
    custom_objects,
    is_workload_cluster,
    list_known_clusters,
    networking_v1,
    resolve_clusters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Used by callers to decorate any "concerning" condition.
_NORMAL_POD_PHASES = {"Running", "Succeeded"}
_HEALTHY_NODE_CONDITIONS = {"Ready"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int((_now_utc() - ts).total_seconds())


def _safe_call(fn, *args, **kwargs):
    """
    Run a Kubernetes API call and convert exceptions into a structured
    error dict so a partial cross-cluster result never blows up the whole
    response.
    """
    try:
        return fn(*args, **kwargs), None
    except ApiException as e:
        return None, f"K8s API error {e.status}: {e.reason}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


def _parallel_per_cluster(clusters: list[str], fn) -> dict[str, Any]:
    """Run `fn(cluster_name)` in parallel for each cluster, collect into dict."""
    out: dict[str, Any] = {}
    if not clusters:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(clusters))) as ex:
        futures = {ex.submit(fn, c): c for c in clusters}
        for fut in concurrent.futures.as_completed(futures):
            c = futures[fut]
            try:
                out[c] = fut.result()
            except Exception as e:
                out[c] = {"error": f"{type(e).__name__}: {e}"}
    return out


# ConfigMap key redaction patterns. Even though we never return values,
# we use these to FLAG keys in the metadata response so the user knows
# which keys would be sensitive if read out-of-band.
_SENSITIVE_KEY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"token",
        r"secret",
        r"password",
        r"pwd",
        r"credential",
        r"apikey",
        r"api[_-]?key",
        r"private[_-]?key",
        r"access[_-]?key",
        r"auth",
        r"jwt",
        r"bearer",
    )
]


def _is_sensitive_key(key: str) -> bool:
    return any(p.search(key) for p in _SENSITIVE_KEY_PATTERNS)


# ---------------------------------------------------------------------------
# Tool 1: cluster_overview
# ---------------------------------------------------------------------------

def cluster_overview(cluster: Optional[str] = None) -> dict:
    """
    High-level overview of one or all clusters.

    For each cluster reports: K8s server version, node count and status
    breakdown, namespace count, total pod count by phase, top-level health
    summary (any nodes NotReady, any pods CrashLoopBackOff, etc.).

    Args:
        cluster: A specific cluster name, or 'all'/'workload'/'management',
                 or None (= all known clusters). Use this as the entry
                 point when the user asks "what's the state of our
                 clusters?".

    Returns:
        dict with one entry per cluster, each containing summary fields.
    """
    try:
        targets = resolve_clusters(cluster)
    except ValueError as e:
        return {"error": str(e)}

    def _per_cluster(name: str) -> dict:
        v1 = core_v1(name)

        nodes_resp, err = _safe_call(v1.list_node, _request_timeout=20)
        if err:
            return {"error": err}
        nodes = nodes_resp.items
        node_summary = {"ready": 0, "not_ready": 0, "schedulable": 0, "unschedulable": 0}
        node_warnings: list[str] = []
        for n in nodes:
            cond = {c.type: c.status for c in (n.status.conditions or [])}
            if cond.get("Ready") == "True":
                node_summary["ready"] += 1
            else:
                node_summary["not_ready"] += 1
                node_warnings.append(
                    f"Node {n.metadata.name} not Ready (conditions: {cond})"
                )
            if (n.spec.unschedulable or False):
                node_summary["unschedulable"] += 1
            else:
                node_summary["schedulable"] += 1

        ns_resp, err = _safe_call(v1.list_namespace, _request_timeout=20)
        if err:
            return {"error": err}
        ns_count = len(ns_resp.items)

        pods_resp, err = _safe_call(
            v1.list_pod_for_all_namespaces, _request_timeout=30
        )
        if err:
            return {"error": err}
        pods = pods_resp.items
        phase_breakdown: dict[str, int] = {}
        crashlooping = 0
        for p in pods:
            phase = p.status.phase or "Unknown"
            phase_breakdown[phase] = phase_breakdown.get(phase, 0) + 1
            for cs in p.status.container_statuses or []:
                waiting = cs.state and cs.state.waiting
                if waiting and waiting.reason == "CrashLoopBackOff":
                    crashlooping += 1
                    break

        # Server version
        version_resp, _ = _safe_call(
            lambda: k8s_client.VersionApi(v1.api_client).get_code(_request_timeout=20)
        )
        server_version = getattr(version_resp, "git_version", None) if version_resp else None

        return {
            "is_workload_cluster": is_workload_cluster(name),
            "server_version": server_version,
            "namespaces": ns_count,
            "nodes": {
                "total": len(nodes),
                **node_summary,
                "warnings": node_warnings[:5],  # cap noise
            },
            "pods": {
                "total": len(pods),
                "by_phase": phase_breakdown,
                "crashlooping": crashlooping,
            },
            "concerns": [
                *(["nodes_not_ready"] if node_summary["not_ready"] else []),
                *(["pods_crashlooping"] if crashlooping else []),
            ],
        }

    return {
        "clusters": _parallel_per_cluster(targets, _per_cluster),
        "queried_at": _now_utc().isoformat(),
    }


# ---------------------------------------------------------------------------
# Tool 2: analyze_node_health
# ---------------------------------------------------------------------------

def analyze_node_health(
    cluster: str,
    node_name: Optional[str] = None,
) -> dict:
    """
    Detailed health analysis of one or all nodes in a cluster.

    Reports per node: conditions (Ready/MemoryPressure/DiskPressure/PIDPressure),
    capacity vs allocatable, current resource requests/limits across pods,
    kubelet version, kernel/OS info. If metrics-server is available, adds
    actual CPU/memory usage.

    Args:
        cluster: Cluster name (required; this is a per-cluster deep-dive).
        node_name: Specific node, or None for all nodes in the cluster.

    Returns:
        dict with cluster, queried_at, and 'nodes' list.
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    v1 = core_v1(cluster)

    if node_name:
        node_resp, err = _safe_call(v1.read_node, name=node_name, _request_timeout=20)
        if err:
            return {"error": err}
        nodes = [node_resp]
    else:
        nodes_resp, err = _safe_call(v1.list_node, _request_timeout=20)
        if err:
            return {"error": err}
        nodes = nodes_resp.items

    # Pull live pod requests/limits per node, in one shot.
    pods_resp, err = _safe_call(
        v1.list_pod_for_all_namespaces, _request_timeout=30
    )
    if err:
        return {"error": err}
    requests_by_node: dict[str, dict[str, float]] = {}
    for p in pods_resp.items:
        node = p.spec.node_name
        if not node:
            continue
        bucket = requests_by_node.setdefault(node, {"cpu_m": 0.0, "memory_bytes": 0.0})
        for c in p.spec.containers or []:
            req = (c.resources.requests if c.resources else None) or {}
            bucket["cpu_m"] += _parse_cpu_to_millicores(req.get("cpu"))
            bucket["memory_bytes"] += _parse_memory_to_bytes(req.get("memory"))

    # Try metrics-server for actual usage. Best-effort.
    co = custom_objects(cluster)
    metrics_by_node: dict[str, dict[str, float]] = {}
    metrics_payload, _ = _safe_call(
        co.list_cluster_custom_object,
        group="metrics.k8s.io",
        version="v1beta1",
        plural="nodes",
        _request_timeout=15,
    )
    if metrics_payload:
        for item in (metrics_payload.get("items") or []):
            usage = item.get("usage") or {}
            metrics_by_node[item.get("metadata", {}).get("name", "")] = {
                "cpu_m": _parse_cpu_to_millicores(usage.get("cpu")),
                "memory_bytes": _parse_memory_to_bytes(usage.get("memory")),
            }

    out = []
    for n in nodes:
        name = n.metadata.name
        cap = n.status.capacity or {}
        alloc = n.status.allocatable or {}
        cap_cpu = _parse_cpu_to_millicores(cap.get("cpu"))
        alloc_cpu = _parse_cpu_to_millicores(alloc.get("cpu"))
        cap_mem = _parse_memory_to_bytes(cap.get("memory"))
        alloc_mem = _parse_memory_to_bytes(alloc.get("memory"))
        req = requests_by_node.get(name, {"cpu_m": 0.0, "memory_bytes": 0.0})
        usage = metrics_by_node.get(name)

        conditions = {c.type: c.status for c in (n.status.conditions or [])}
        node_info = n.status.node_info

        out.append(
            {
                "name": name,
                "ready": conditions.get("Ready") == "True",
                "schedulable": not (n.spec.unschedulable or False),
                "conditions": conditions,
                "kubelet_version": node_info.kubelet_version if node_info else None,
                "os": (node_info.os_image if node_info else None),
                "kernel": (node_info.kernel_version if node_info else None),
                "container_runtime": (
                    node_info.container_runtime_version if node_info else None
                ),
                "capacity": {
                    "cpu_m": cap_cpu,
                    "memory_bytes": cap_mem,
                    "pods": int(cap.get("pods", 0)),
                },
                "allocatable": {
                    "cpu_m": alloc_cpu,
                    "memory_bytes": alloc_mem,
                    "pods": int(alloc.get("pods", 0)),
                },
                "requested": {
                    "cpu_m": round(req["cpu_m"], 2),
                    "memory_bytes": int(req["memory_bytes"]),
                    "cpu_pct_of_allocatable": (
                        round(req["cpu_m"] / alloc_cpu * 100.0, 1) if alloc_cpu else None
                    ),
                    "memory_pct_of_allocatable": (
                        round(req["memory_bytes"] / alloc_mem * 100.0, 1)
                        if alloc_mem
                        else None
                    ),
                },
                "current_usage": (
                    {
                        "cpu_m": round(usage["cpu_m"], 1),
                        "memory_bytes": int(usage["memory_bytes"]),
                        "cpu_pct_of_allocatable": (
                            round(usage["cpu_m"] / alloc_cpu * 100.0, 1)
                            if alloc_cpu
                            else None
                        ),
                        "memory_pct_of_allocatable": (
                            round(usage["memory_bytes"] / alloc_mem * 100.0, 1)
                            if alloc_mem
                            else None
                        ),
                    }
                    if usage
                    else None
                ),
            }
        )

    return {
        "cluster": cluster,
        "is_workload_cluster": is_workload_cluster(cluster),
        "queried_at": _now_utc().isoformat(),
        "metrics_server_available": bool(metrics_by_node),
        "nodes": out,
    }


# ---------------------------------------------------------------------------
# Resource string parsers (k8s quantities)
# ---------------------------------------------------------------------------

def _parse_cpu_to_millicores(raw: Any) -> float:
    if raw is None:
        return 0.0
    s = str(raw)
    if s.endswith("n"):  # nanocores
        return float(s[:-1]) / 1_000_000.0
    if s.endswith("u"):  # microcores
        return float(s[:-1]) / 1_000.0
    if s.endswith("m"):  # millicores
        return float(s[:-1])
    # Plain integer = whole cores
    try:
        return float(s) * 1000.0
    except ValueError:
        return 0.0


_MEMORY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def _parse_memory_to_bytes(raw: Any) -> float:
    if raw is None:
        return 0.0
    s = str(raw).strip()
    for unit in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T"):
        if s.endswith(unit):
            try:
                return float(s[: -len(unit)]) * _MEMORY_UNITS[unit]
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Tool 3: analyze_namespace
# ---------------------------------------------------------------------------

def analyze_namespace(cluster: str, namespace: str) -> dict:
    """
    Snapshot of a namespace: workloads, pods (status), recent events,
    PVCs, configmap NAMES (no values), services and ingresses.

    The output is shaped to fit in one LLM context comfortably: we cap
    list lengths and include only the fields that matter for triage.

    Args:
        cluster: Cluster name.
        namespace: Namespace to inspect.

    Returns:
        dict with sections: pods, deployments, statefulsets, daemonsets,
        jobs, cronjobs, pvcs, configmaps, services, ingresses, events.
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    v1 = core_v1(cluster)
    apps = apps_v1(cluster)
    batch = batch_v1(cluster)
    net = networking_v1(cluster)

    def _list(fn, *a, **kw):
        resp, err = _safe_call(fn, *a, **kw)
        return (resp.items if resp else []), err

    pods, err_pods = _list(v1.list_namespaced_pod, namespace, _request_timeout=20)
    deps, _ = _list(apps.list_namespaced_deployment, namespace, _request_timeout=20)
    sts, _ = _list(apps.list_namespaced_stateful_set, namespace, _request_timeout=20)
    ds, _ = _list(apps.list_namespaced_daemon_set, namespace, _request_timeout=20)
    jobs, _ = _list(batch.list_namespaced_job, namespace, _request_timeout=20)
    crons, _ = _list(batch.list_namespaced_cron_job, namespace, _request_timeout=20)
    pvcs, _ = _list(v1.list_namespaced_persistent_volume_claim, namespace, _request_timeout=20)
    cms, _ = _list(v1.list_namespaced_config_map, namespace, _request_timeout=20)
    svcs, _ = _list(v1.list_namespaced_service, namespace, _request_timeout=20)
    ings, _ = _list(net.list_namespaced_ingress, namespace, _request_timeout=20)
    events, _ = _list(v1.list_namespaced_event, namespace, _request_timeout=20)

    if err_pods:
        return {"error": err_pods}

    return {
        "cluster": cluster,
        "namespace": namespace,
        "is_workload_cluster": is_workload_cluster(cluster),
        "queried_at": _now_utc().isoformat(),
        "pods": [_summarize_pod(p) for p in pods],
        "deployments": [_summarize_deployment(d) for d in deps],
        "statefulsets": [_summarize_statefulset(s) for s in sts],
        "daemonsets": [_summarize_daemonset(d) for d in ds],
        "jobs": [_summarize_job(j) for j in jobs],
        "cronjobs": [_summarize_cronjob(c) for c in crons],
        "pvcs": [_summarize_pvc(p) for p in pvcs],
        "configmaps": [_summarize_configmap(c) for c in cms],
        "services": [
            {
                "name": s.metadata.name,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [
                    {"port": p.port, "protocol": p.protocol, "target": p.target_port}
                    for p in (s.spec.ports or [])
                ],
            }
            for s in svcs
        ],
        "ingresses": [
            {
                "name": i.metadata.name,
                "hosts": [r.host for r in (i.spec.rules or []) if r.host],
                "class": (i.spec.ingress_class_name or None),
            }
            for i in ings
        ],
        "recent_events": _recent_events(events, limit=20),
    }


def _summarize_pod(p) -> dict:
    statuses = p.status.container_statuses or []
    waiting_reasons = []
    restart_total = 0
    for cs in statuses:
        restart_total += cs.restart_count or 0
        w = cs.state and cs.state.waiting
        if w and w.reason:
            waiting_reasons.append(f"{cs.name}: {w.reason}")
    return {
        "name": p.metadata.name,
        "phase": p.status.phase,
        "node": p.spec.node_name,
        "containers": len(p.spec.containers or []),
        "restarts": restart_total,
        "waiting_reasons": waiting_reasons,
        "age_seconds": _age_seconds(p.metadata.creation_timestamp),
        "ready": all(cs.ready for cs in statuses) if statuses else False,
    }


def _summarize_deployment(d) -> dict:
    return {
        "name": d.metadata.name,
        "replicas_desired": (d.spec.replicas or 0),
        "replicas_available": (d.status.available_replicas or 0),
        "replicas_ready": (d.status.ready_replicas or 0),
        "strategy": (d.spec.strategy.type if d.spec.strategy else None),
        "age_seconds": _age_seconds(d.metadata.creation_timestamp),
        "healthy": (d.spec.replicas or 0) == (d.status.available_replicas or 0),
    }


def _summarize_statefulset(s) -> dict:
    return {
        "name": s.metadata.name,
        "replicas_desired": (s.spec.replicas or 0),
        "replicas_ready": (s.status.ready_replicas or 0),
        "age_seconds": _age_seconds(s.metadata.creation_timestamp),
    }


def _summarize_daemonset(d) -> dict:
    return {
        "name": d.metadata.name,
        "desired": (d.status.desired_number_scheduled or 0),
        "ready": (d.status.number_ready or 0),
        "available": (d.status.number_available or 0),
        "age_seconds": _age_seconds(d.metadata.creation_timestamp),
    }


def _summarize_job(j) -> dict:
    return {
        "name": j.metadata.name,
        "completions": j.spec.completions,
        "succeeded": (j.status.succeeded or 0),
        "failed": (j.status.failed or 0),
        "active": (j.status.active or 0),
        "completion_time": (
            j.status.completion_time.isoformat() if j.status.completion_time else None
        ),
        "age_seconds": _age_seconds(j.metadata.creation_timestamp),
    }


def _summarize_cronjob(c) -> dict:
    last = c.status.last_schedule_time
    last_success = c.status.last_successful_time
    return {
        "name": c.metadata.name,
        "schedule": c.spec.schedule,
        "suspend": bool(c.spec.suspend),
        "last_schedule_time": last.isoformat() if last else None,
        "last_successful_time": last_success.isoformat() if last_success else None,
        "active_jobs": len(c.status.active or []),
    }


def _summarize_pvc(p) -> dict:
    requested = (p.spec.resources.requests or {}).get("storage") if p.spec.resources else None
    return {
        "name": p.metadata.name,
        "phase": p.status.phase,
        "storage_class": p.spec.storage_class_name,
        "volume_name": p.spec.volume_name,
        "requested_storage": requested,
        "access_modes": p.spec.access_modes,
    }


def _summarize_configmap(c) -> dict:
    """
    Returns metadata + sensitivity flags. We NEVER include data values.
    Sizes let the user judge whether a key looks like a small flag vs.
    a chunky payload, without exposing content.
    """
    keys = list((c.data or {}).keys()) + list((c.binary_data or {}).keys())
    sizes = {}
    for k, v in (c.data or {}).items():
        sizes[k] = len(v) if v is not None else 0
    for k, v in (c.binary_data or {}).items():
        sizes[k] = len(v) if v is not None else 0
    return {
        "name": c.metadata.name,
        "key_count": len(keys),
        "keys": [
            {
                "name": k,
                "size_bytes": sizes.get(k, 0),
                "looks_sensitive": _is_sensitive_key(k),
            }
            for k in keys
        ],
        "age_seconds": _age_seconds(c.metadata.creation_timestamp),
    }


def _recent_events(events, limit: int = 20, since_seconds: Optional[int] = None) -> list[dict]:
    out = []
    cutoff = None
    if since_seconds:
        cutoff = _now_utc().timestamp() - since_seconds
    for e in events:
        ts = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
        if cutoff and ts and ts.timestamp() < cutoff:
            continue
        out.append(
            {
                "type": e.type,  # Normal / Warning
                "reason": e.reason,
                "message": e.message,
                "involved_object": (
                    f"{e.involved_object.kind}/{e.involved_object.name}"
                    if e.involved_object
                    else None
                ),
                "namespace": e.metadata.namespace,
                "count": e.count,
                "first_seen": e.first_timestamp.isoformat() if e.first_timestamp else None,
                "last_seen": ts.isoformat() if ts else None,
            }
        )
    # Sort warnings first, then by recency
    out.sort(key=lambda x: (x["type"] != "Warning", x["last_seen"] or ""), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Tool 4: analyze_workload
# ---------------------------------------------------------------------------

_WORKLOAD_KINDS = {"deployment", "statefulset", "daemonset", "job", "cronjob"}


def analyze_workload(
    cluster: str,
    namespace: str,
    kind: str,
    name: str,
) -> dict:
    """
    Deep-dive on a single workload: spec, replica/run state, owned pods,
    container status, restart counts, recent events for the workload and
    its pods. The first place to look when the user says "X is broken".

    Args:
        cluster: Cluster name.
        namespace: Namespace.
        kind: 'deployment' | 'statefulset' | 'daemonset' | 'job' | 'cronjob'
              (case-insensitive).
        name: Workload name.

    Returns:
        dict with workload spec, status, pods, events.
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    kind_l = kind.lower()
    if kind_l not in _WORKLOAD_KINDS:
        return {
            "error": f"Unsupported kind '{kind}'. Use one of {sorted(_WORKLOAD_KINDS)}."
        }

    v1 = core_v1(cluster)
    apps = apps_v1(cluster)
    batch = batch_v1(cluster)

    workload: dict = {}
    selector_str: Optional[str] = None

    if kind_l == "deployment":
        obj, err = _safe_call(apps.read_namespaced_deployment, name, namespace)
        if err:
            return {"error": err}
        workload = _summarize_deployment(obj)
        selector_str = _format_selector(obj.spec.selector)
    elif kind_l == "statefulset":
        obj, err = _safe_call(apps.read_namespaced_stateful_set, name, namespace)
        if err:
            return {"error": err}
        workload = _summarize_statefulset(obj)
        selector_str = _format_selector(obj.spec.selector)
    elif kind_l == "daemonset":
        obj, err = _safe_call(apps.read_namespaced_daemon_set, name, namespace)
        if err:
            return {"error": err}
        workload = _summarize_daemonset(obj)
        selector_str = _format_selector(obj.spec.selector)
    elif kind_l == "job":
        obj, err = _safe_call(batch.read_namespaced_job, name, namespace)
        if err:
            return {"error": err}
        workload = _summarize_job(obj)
        selector_str = _format_selector(obj.spec.selector)
    elif kind_l == "cronjob":
        obj, err = _safe_call(batch.read_namespaced_cron_job, name, namespace)
        if err:
            return {"error": err}
        workload = _summarize_cronjob(obj)
        selector_str = None  # cronjob doesn't have a label selector itself

    # Find owned pods (best effort via label selector)
    owned_pods: list[dict] = []
    if selector_str:
        pods_resp, err = _safe_call(
            v1.list_namespaced_pod,
            namespace=namespace,
            label_selector=selector_str,
            _request_timeout=20,
        )
        if not err and pods_resp:
            owned_pods = [_summarize_pod(p) for p in pods_resp.items]

    # Pull events scoped to this workload + its pods
    events_resp, _ = _safe_call(
        v1.list_namespaced_event,
        namespace=namespace,
        field_selector=f"involvedObject.name={name}",
        _request_timeout=15,
    )
    workload_events = _recent_events(events_resp.items if events_resp else [], limit=15)

    return {
        "cluster": cluster,
        "namespace": namespace,
        "kind": kind_l,
        "name": name,
        "is_workload_cluster": is_workload_cluster(cluster),
        "queried_at": _now_utc().isoformat(),
        "workload": workload,
        "pods": owned_pods,
        "events": workload_events,
    }


def _format_selector(sel) -> Optional[str]:
    if sel is None:
        return None
    parts = []
    for k, v in (sel.match_labels or {}).items():
        parts.append(f"{k}={v}")
    return ",".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Tool 5: find_failing_workloads
# ---------------------------------------------------------------------------

def find_failing_workloads(
    cluster: Optional[str] = None,
    namespace: Optional[str] = None,
) -> dict:
    """
    Hunt for anything that's not healthy across one or all clusters.

    Identifies: pods not Ready / CrashLoopBackOff / ImagePullBackOff,
    deployments where available != desired, jobs with failed > 0, cronjobs
    with no successful run in too long. Best entry point when the user
    asks "what's broken?".

    Args:
        cluster: Cluster name, or 'all' / 'workload' / 'management', or None (= all).
        namespace: Optional namespace filter applied to every cluster.

    Returns:
        dict with failures grouped by cluster.
    """
    try:
        targets = resolve_clusters(cluster)
    except ValueError as e:
        return {"error": str(e)}

    def _per_cluster(name: str) -> dict:
        v1 = core_v1(name)
        apps = apps_v1(name)
        batch = batch_v1(name)

        problems: dict[str, list[Any]] = {
            "pods_not_ready": [],
            "pods_crashlooping": [],
            "pods_image_pull_failure": [],
            "deployments_unhealthy": [],
            "jobs_failed": [],
            "cronjobs_stale": [],
        }

        # Pods
        if namespace:
            pods_resp, err = _safe_call(
                v1.list_namespaced_pod, namespace, _request_timeout=30
            )
        else:
            pods_resp, err = _safe_call(
                v1.list_pod_for_all_namespaces, _request_timeout=30
            )
        if err:
            return {"error": err}
        for p in (pods_resp.items if pods_resp else []):
            for cs in p.status.container_statuses or []:
                w = cs.state and cs.state.waiting
                if w and w.reason == "CrashLoopBackOff":
                    problems["pods_crashlooping"].append(
                        {
                            "namespace": p.metadata.namespace,
                            "name": p.metadata.name,
                            "container": cs.name,
                            "restarts": cs.restart_count,
                            "message": (w.message or "")[:200],
                        }
                    )
                elif w and w.reason in ("ImagePullBackOff", "ErrImagePull"):
                    problems["pods_image_pull_failure"].append(
                        {
                            "namespace": p.metadata.namespace,
                            "name": p.metadata.name,
                            "container": cs.name,
                            "reason": w.reason,
                            "message": (w.message or "")[:200],
                        }
                    )
            phase = p.status.phase
            if phase not in _NORMAL_POD_PHASES:
                problems["pods_not_ready"].append(
                    {
                        "namespace": p.metadata.namespace,
                        "name": p.metadata.name,
                        "phase": phase,
                        "node": p.spec.node_name,
                    }
                )

        # Deployments
        if namespace:
            deps_resp, _ = _safe_call(
                apps.list_namespaced_deployment, namespace, _request_timeout=20
            )
        else:
            deps_resp, _ = _safe_call(
                apps.list_deployment_for_all_namespaces, _request_timeout=20
            )
        for d in (deps_resp.items if deps_resp else []):
            desired = d.spec.replicas or 0
            available = d.status.available_replicas or 0
            if desired and available < desired:
                problems["deployments_unhealthy"].append(
                    {
                        "namespace": d.metadata.namespace,
                        "name": d.metadata.name,
                        "desired": desired,
                        "available": available,
                    }
                )

        # Failed jobs
        if namespace:
            jobs_resp, _ = _safe_call(
                batch.list_namespaced_job, namespace, _request_timeout=20
            )
        else:
            jobs_resp, _ = _safe_call(
                batch.list_job_for_all_namespaces, _request_timeout=20
            )
        for j in (jobs_resp.items if jobs_resp else []):
            failed = j.status.failed or 0
            if failed:
                problems["jobs_failed"].append(
                    {
                        "namespace": j.metadata.namespace,
                        "name": j.metadata.name,
                        "failed": failed,
                        "succeeded": j.status.succeeded or 0,
                    }
                )

        # Cronjobs with stale last-success (> 7 days or never)
        if namespace:
            cron_resp, _ = _safe_call(
                batch.list_namespaced_cron_job, namespace, _request_timeout=20
            )
        else:
            cron_resp, _ = _safe_call(
                batch.list_cron_job_for_all_namespaces, _request_timeout=20
            )
        STALE_SECS = 7 * 24 * 3600
        for c in (cron_resp.items if cron_resp else []):
            if c.spec.suspend:
                continue  # suspended on purpose; don't flag
            last_ok = c.status.last_successful_time
            age = _age_seconds(last_ok) if last_ok else None
            if age is None or age > STALE_SECS:
                problems["cronjobs_stale"].append(
                    {
                        "namespace": c.metadata.namespace,
                        "name": c.metadata.name,
                        "schedule": c.spec.schedule,
                        "last_successful_time": (
                            last_ok.isoformat() if last_ok else None
                        ),
                        "age_seconds": age,
                    }
                )

        return {"is_workload_cluster": is_workload_cluster(name), "problems": problems}

    return {
        "queried_at": _now_utc().isoformat(),
        "namespace_filter": namespace,
        "clusters": _parallel_per_cluster(targets, _per_cluster),
    }


# ---------------------------------------------------------------------------
# Tool 6: get_recent_events
# ---------------------------------------------------------------------------

def get_recent_events(
    cluster: str,
    namespace: Optional[str] = None,
    since_minutes: int = 60,
    warnings_only: bool = True,
    limit: int = 50,
) -> dict:
    """
    Fetch recent Kubernetes events from a cluster, optionally namespace-scoped.
    Defaults to warnings-only and the last 60 minutes.

    Args:
        cluster: Cluster name.
        namespace: Optional namespace filter.
        since_minutes: Window length in minutes (default 60).
        warnings_only: If True, filter to type=Warning.
        limit: Max events to return (default 50).
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    v1 = core_v1(cluster)
    if namespace:
        resp, err = _safe_call(v1.list_namespaced_event, namespace, _request_timeout=30)
    else:
        resp, err = _safe_call(v1.list_event_for_all_namespaces, _request_timeout=30)
    if err:
        return {"error": err}

    items = resp.items if resp else []
    if warnings_only:
        items = [e for e in items if e.type == "Warning"]

    rendered = _recent_events(items, limit=limit, since_seconds=since_minutes * 60)
    return {
        "cluster": cluster,
        "namespace": namespace,
        "since_minutes": since_minutes,
        "warnings_only": warnings_only,
        "queried_at": _now_utc().isoformat(),
        "count": len(rendered),
        "events": rendered,
    }


# ---------------------------------------------------------------------------
# Tool 7: inspect_configmap
# ---------------------------------------------------------------------------

def inspect_configmap(cluster: str, namespace: str, name: str) -> dict:
    """
    Inspect a ConfigMap's METADATA only — names, keys, sizes, and per-key
    sensitivity flags. Values are NEVER returned by this tool, regardless
    of any parameter. There is no override path.

    If you need to read a value, do it from the cluster directly with kubectl
    after verifying the key is not sensitive.

    Args:
        cluster: Cluster name.
        namespace: Namespace.
        name: ConfigMap name.

    Returns:
        dict with name, namespace, key list (each with size and sensitivity),
        labels, annotations.
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    v1 = core_v1(cluster)
    cm, err = _safe_call(
        v1.read_namespaced_config_map, name=name, namespace=namespace, _request_timeout=15
    )
    if err:
        return {"error": err}

    summary = _summarize_configmap(cm)
    return {
        "cluster": cluster,
        "namespace": namespace,
        "queried_at": _now_utc().isoformat(),
        "name": cm.metadata.name,
        "labels": cm.metadata.labels or {},
        "annotations": cm.metadata.annotations or {},
        "key_count": summary["key_count"],
        "keys": summary["keys"],
        "policy_note": (
            "ConfigMap data values are deliberately NOT exposed by this tool. "
            "Keys flagged 'looks_sensitive' may also be sensitive in non-flagged "
            "form — treat as a hint, not a guarantee."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 8 (bonus): list_persistent_volumes
# ---------------------------------------------------------------------------

# Listed in the README as part of the analysis surface. Implemented even
# though the original list said 7 tools — PV listing is so cheap and so
# commonly needed that omitting it would force the agent to use the
# heavier `analyze_namespace` for simple PV questions.

def list_persistent_volumes(
    cluster: str,
    namespace: Optional[str] = None,
) -> dict:
    """
    List PersistentVolumes (cluster-scoped) and PersistentVolumeClaims
    (namespace-scoped) with status, capacity, and binding info.

    Args:
        cluster: Cluster name.
        namespace: Optional namespace filter for PVCs. PVs are always
                   listed cluster-wide (they are cluster-scoped).

    Returns:
        dict with 'pvs' and 'pvcs'.
    """
    if cluster not in list_known_clusters():
        return {"error": f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"}

    v1 = core_v1(cluster)
    pvs_resp, err = _safe_call(v1.list_persistent_volume, _request_timeout=30)
    if err:
        return {"error": err}

    if namespace:
        pvcs_resp, err = _safe_call(
            v1.list_namespaced_persistent_volume_claim, namespace, _request_timeout=30
        )
    else:
        pvcs_resp, err = _safe_call(
            v1.list_persistent_volume_claim_for_all_namespaces, _request_timeout=30
        )
    if err:
        return {"error": err}

    return {
        "cluster": cluster,
        "namespace_filter": namespace,
        "queried_at": _now_utc().isoformat(),
        "pvs": [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "capacity": (p.spec.capacity or {}).get("storage"),
                "storage_class": p.spec.storage_class_name,
                "access_modes": p.spec.access_modes,
                "reclaim_policy": p.spec.persistent_volume_reclaim_policy,
                "claim": (
                    f"{p.spec.claim_ref.namespace}/{p.spec.claim_ref.name}"
                    if p.spec.claim_ref
                    else None
                ),
            }
            for p in (pvs_resp.items if pvs_resp else [])
        ],
        "pvcs": [_summarize_pvc(p) for p in (pvcs_resp.items if pvcs_resp else [])],
    }


# ===========================================================================
# Observability + topology tools
# ===========================================================================
#
# These tools resolve "user-friendly" service references into the actual
# Kubernetes objects that run them, and surface the observability metadata
# (Datadog tags) declared on Deployment labels.
#
# Use cases driving these:
#   - Datadog query construction: user says "errors on the API service",
#     agent needs to know that maps to `service:bf-api env:test` (the actual
#     tags on the Deployment) before constructing a Datadog query.
#   - Hostname-to-workload reverse lookup: user pastes "api.test.bullfinch.com"
#     from an alert, agent needs to find which Deployment serves that host.
#   - Cluster topology discovery: user asks "what runs on helios-prod",
#     agent enumerates Deployments grouped by namespace/env.
#
# All four tools are READ-ONLY. They never modify cluster state.
# ---------------------------------------------------------------------------

# Datadog tag labels (unified service tagging convention)
_DD_LABEL_SERVICE = "tags.datadoghq.com/service"
_DD_LABEL_ENV = "tags.datadoghq.com/env"
_DD_LABEL_VERSION = "tags.datadoghq.com/version"

# Traefik CRD coordinates (confirmed uniform on this team's clusters)
_TRAEFIK_GROUP = "traefik.io"
_TRAEFIK_VERSION = "v1alpha1"
_TRAEFIK_INGRESSROUTE_PLURAL = "ingressroutes"
_SERVICE_NAMESPACE_ENV_SUFFIXES = {
    "dev",
    "development",
    "test",
    "sandbox",
    "staging",
    "stage",
    "prod",
    "production",
}
_NON_SERVICE_NAMESPACE_PREFIXES = (
    "kube-",
    "flux-",
    "cert-manager",
    "datadog",
    "traefik",
    "rancher-",
    "cattle-",
    "ingress-",
    "monitoring",
)


def _extract_dd_tags(labels: dict) -> dict:
    """Pull Datadog tags off a labels map. Returns only the keys present."""
    if not labels:
        return {}
    out = {}
    if labels.get(_DD_LABEL_SERVICE):
        out["service"] = labels[_DD_LABEL_SERVICE]
    if labels.get(_DD_LABEL_ENV):
        out["env"] = labels[_DD_LABEL_ENV]
    if labels.get(_DD_LABEL_VERSION):
        out["version"] = labels[_DD_LABEL_VERSION]
    return out


def _pod_template_labels(d) -> dict:
    """Return Deployment pod-template labels, or an empty dict if absent."""
    try:
        return d.spec.template.metadata.labels or {}
    except AttributeError:
        return {}


def _deployment_metadata_labels(d) -> dict:
    """Return Deployment metadata labels, or an empty dict if absent."""
    try:
        return d.metadata.labels or {}
    except AttributeError:
        return {}


def _effective_labels(d) -> dict:
    """
    Labels to use for service discovery and observability.

    Kubernetes Deployments can carry labels at two different levels:
      - metadata.labels: labels on the Deployment object itself
      - spec.template.metadata.labels: labels stamped onto Pods

    Datadog unified service tags must be present on the Pods to be useful.
    Helios deployments currently duplicate them at both levels, but cqrs-*
    and heliosmq-* deployments only expose them on the pod template. Prefer
    pod-template labels and use Deployment metadata labels as fallback.
    """
    metadata_labels = _deployment_metadata_labels(d)
    template_labels = _pod_template_labels(d)
    return {**metadata_labels, **template_labels}


def _infer_namespace_context(namespace: Optional[str]) -> dict:
    """
    Infer service/env hints from service-specific namespaces.

    Examples:
      cqrs-test        -> service_family=cqrs, env=test
      heliosmq-sandbox -> service_family=heliosmq, env=sandbox

    This is fallback context only. It must never override explicit
    Datadog labels from the pod template.
    """
    if not namespace or "-" not in namespace:
        return {}
    if namespace == "default" or any(
        namespace.startswith(prefix) for prefix in _NON_SERVICE_NAMESPACE_PREFIXES
    ):
        return {}
    service_family, env_name = namespace.rsplit("-", 1)
    if not service_family or not env_name:
        return {}
    if env_name not in _SERVICE_NAMESPACE_ENV_SUFFIXES:
        return {}
    return {
        "service_family": service_family,
        "env": env_name,
    }


def _effective_dd_tags(d) -> dict:
    """
    Datadog tags for a Deployment.

    Real Datadog labels win. Namespace-derived env is only used when the
    Deployment genuinely does not expose tags.datadoghq.com/env.
    """
    dd_tags = _extract_dd_tags(_effective_labels(d))
    ns_context = _infer_namespace_context(getattr(d.metadata, "namespace", None))
    if not dd_tags.get("env") and ns_context.get("env"):
        dd_tags["env"] = ns_context["env"]
    return dd_tags


def _build_dd_filter(dd_tags: dict) -> Optional[str]:
    """Turn a dd_tags dict into a Datadog query filter string."""
    parts = []
    if dd_tags.get("service"):
        parts.append(f"service:{dd_tags['service']}")
    if dd_tags.get("env"):
        parts.append(f"env:{dd_tags['env']}")
    return " ".join(parts) if parts else None


def _deployment_summary(d, cluster: str) -> dict:
    """Compact view of a Deployment for these tools' output."""
    metadata_labels = _deployment_metadata_labels(d)
    template_labels = _pod_template_labels(d)
    labels = _effective_labels(d)
    dd_tags = _effective_dd_tags(d)
    ns_context = _infer_namespace_context(d.metadata.namespace)
    additional = {
        k: v for k, v in labels.items()
        if not k.startswith("tags.datadoghq.com/")
        and k not in ("kustomize.toolkit.fluxcd.io/name", "kustomize.toolkit.fluxcd.io/namespace")
    }
    # Container image — first container only, just for context. Truncate
    # the full repo path to keep output token-light.
    image = None
    try:
        containers = d.spec.template.spec.containers or []
        if containers:
            image = containers[0].image
    except AttributeError:
        pass

    return {
        "deployment_name": d.metadata.name,
        "namespace": d.metadata.namespace,
        "cluster": cluster,
        "datadog_tags": dd_tags,
        "datadog_query_filter": _build_dd_filter(dd_tags),
        "additional_labels": additional,
        "namespace_context": ns_context,
        "label_sources": {
            "deployment_metadata_has_dd_tags": bool(_extract_dd_tags(metadata_labels)),
            "pod_template_has_dd_tags": bool(_extract_dd_tags(template_labels)),
        },
        "container_image": image,
        "replicas_status": (
            f"{(d.status.ready_replicas or 0)}/{d.spec.replicas or 0} ready"
        ),
    }


# ---------------------------------------------------------------------------
# Tool: list_cluster_namespaces
# ---------------------------------------------------------------------------

def list_cluster_namespaces(
    cluster: str,
    group_by_env: bool = True,
) -> dict:
    """
    Enumerate namespaces of a cluster, structured by env/dep label when
    available. Use this for discovery questions ("which environments
    exist on helios-prod?", "where do canary services run?").

    Args:
        cluster: Cluster name from KUBE_CLUSTERS.
        group_by_env: If True, group by env+dep labels of the namespace
            itself. If False, return a flat list.

    Returns:
        Structured dict with namespaces grouped, plus a flat listing.
    """
    from .cluster_registry import core_v1
    api = core_v1(cluster)
    resp, err = _safe_call(api.list_namespace)
    if resp is None:
        return {"error": f"Could not list namespaces on {cluster}: {err}"}

    flat = []
    for ns in resp.items:
        labels = ns.metadata.labels or {}
        ns_context = _infer_namespace_context(ns.metadata.name)
        flat.append({
            "name": ns.metadata.name,
            "env": labels.get("env"),
            "dep": labels.get("dep"),
            "namespace_context": ns_context,
            "labels": {k: v for k, v in labels.items()
                       if k in ("env", "dep")},
        })

    out = {
        "cluster": cluster,
        "namespace_count": len(flat),
        "queried_at": _now_utc().isoformat(),
    }

    if group_by_env:
        # Three buckets: env-tagged (matrix env x dep), system, service-specific.
        # System detection: well-known K8s/operator namespaces.
        system_prefixes = (
            "kube-", "flux-", "cert-manager", "datadog", "traefik",
            "rancher-", "cattle-", "ingress-", "monitoring", "default",
        )
        by_env: dict[str, dict] = {}
        system: list[str] = []
        service_specific: list[str] = []
        service_specific_context: list[dict] = []

        for ns in flat:
            n = ns["name"]
            env = ns.get("env")
            dep = ns.get("dep")
            ns_context = ns.get("namespace_context") or {}
            if env:
                by_env.setdefault(env, {}).setdefault(dep or "unspecified", []).append(n)
            elif any(n.startswith(p) for p in system_prefixes) or n in ("default",):
                system.append(n)
            else:
                # Service-specific: namespace named after a service
                # (cqrs-test, heliosmq-sandbox, etc.)
                service_specific.append(n)
                if ns_context:
                    service_specific_context.append({
                        "name": n,
                        **ns_context,
                    })

        out["by_env"] = by_env
        out["system"] = sorted(system)
        out["service_specific"] = sorted(service_specific)
        out["service_specific_context"] = sorted(
            service_specific_context,
            key=lambda item: item["name"],
        )
    else:
        out["namespaces"] = flat

    return out


# ---------------------------------------------------------------------------
# Tool: list_workloads_in_cluster
# ---------------------------------------------------------------------------

def list_workloads_in_cluster(
    cluster: str,
    env: Optional[str] = None,
    dep: Optional[str] = None,
    namespace_filter: Optional[str] = None,
) -> dict:
    """
    Discovery: list all Deployments in a cluster with their Datadog tags.
    Use this for open-ended questions ("what runs on helios-prod?", "which
    services are in test canary?").

    Args:
        cluster: Cluster name from KUBE_CLUSTERS.
        env: Optional filter — only Deployments with `tags.datadoghq.com/env` matching.
            E.g. "test", "sandbox", "prod".
        dep: Optional filter — only Deployments with `dep` label matching.
            E.g. "live", "canary".
        namespace_filter: Optional substring; only namespaces containing
            this string are included.

    Returns:
        Compact list of workloads with Datadog tags. Also surfaces
        aggregate "envs_seen" / "services_seen" so the model can suggest
        follow-up filters.
    """
    from .cluster_registry import apps_v1
    api = apps_v1(cluster)
    resp, err = _safe_call(api.list_deployment_for_all_namespaces)
    if resp is None:
        return {"error": f"Could not list deployments on {cluster}: {err}"}

    workloads = []
    envs_seen: set[str] = set()
    services_seen: set[str] = set()
    namespaces_seen: set[str] = set()

    for d in resp.items:
        labels = _effective_labels(d)
        dd = _effective_dd_tags(d)
        ns_context = _infer_namespace_context(d.metadata.namespace)

        if env is not None and dd.get("env") != env:
            continue
        if dep is not None and labels.get("dep") != dep:
            continue
        if namespace_filter and namespace_filter not in d.metadata.namespace:
            continue

        workloads.append({
            "name": d.metadata.name,
            "namespace": d.metadata.namespace,
            "kind": "Deployment",
            "dd_service": dd.get("service"),
            "dd_env": dd.get("env"),
            "dep": labels.get("dep"),
            "namespace_context": ns_context,
            "replicas": f"{(d.status.ready_replicas or 0)}/{d.spec.replicas or 0}",
        })
        if dd.get("env"):
            envs_seen.add(dd["env"])
        if dd.get("service"):
            services_seen.add(dd["service"])
        namespaces_seen.add(d.metadata.namespace)

    return {
        "cluster": cluster,
        "filter": {"env": env, "dep": dep, "namespace_filter": namespace_filter},
        "workload_count": len(workloads),
        "workloads": workloads,
        "envs_seen": sorted(envs_seen),
        "services_seen": sorted(services_seen),
        "namespaces_seen": sorted(namespaces_seen),
        "queried_at": _now_utc().isoformat(),
    }


# ---------------------------------------------------------------------------
# Tool: get_workload_observability_tags
# ---------------------------------------------------------------------------

def get_workload_observability_tags(
    cluster: str,
    workload_hint: str,
    namespace: Optional[str] = None,
    env: Optional[str] = None,
    dep: Optional[str] = None,
) -> dict:
    """
    Resolve a user-friendly service reference into one or more matching
    Deployments, returning the Datadog tags declared on them.

    Use this BEFORE constructing a Datadog query when the user mentions
    a service by name. The Datadog `service:` and `env:` tags come from
    the cluster, not from naming conventions.

    Matching strategies, tried in order (returns first non-empty):
      1. Exact match on `tags.datadoghq.com/service` label
      2. Exact match on `metadata.name` of Deployment
      3. Partial (substring) match on `tags.datadoghq.com/service`
      4. Partial match on `metadata.name`

    All matches from the winning strategy are returned. The model/user
    decides which to use.

    Args:
        cluster: Cluster name.
        workload_hint: User's reference — could be "api", "bf-api", "cqrs",
            "heliosmq", etc.
        namespace: Optional — restrict search to this namespace.
        env: Optional — only return matches with `tags.datadoghq.com/env=<value>`.
        dep: Optional — only return matches with label `dep=<value>` (live|canary).

    Returns:
        Matches with full observability metadata, plus the strategy that
        produced them.
    """
    from .cluster_registry import apps_v1
    api = apps_v1(cluster)
    if namespace:
        resp, err = _safe_call(api.list_namespaced_deployment, namespace=namespace)
    else:
        resp, err = _safe_call(api.list_deployment_for_all_namespaces)
    if resp is None:
        return {"error": f"Could not list deployments on {cluster}: {err}"}

    deployments = list(resp.items)

    # Filter by env / dep up front
    if env is not None:
        deployments = [
            d for d in deployments
            if _effective_dd_tags(d).get("env") == env
        ]
    if dep is not None:
        deployments = [
            d for d in deployments
            if _effective_labels(d).get("dep") == dep
        ]

    hint = workload_hint.strip()
    hint_lower = hint.lower()

    # Strategy 1: exact match on dd service label
    s1 = [d for d in deployments
          if _effective_dd_tags(d).get("service") == hint]
    # Strategy 2: exact match on Deployment name
    s2 = [d for d in deployments if d.metadata.name == hint]
    # Strategy 3: partial on dd service
    s3 = [d for d in deployments
          if hint_lower in (_effective_dd_tags(d).get("service") or "").lower()]
    # Strategy 4: partial on name
    s4 = [d for d in deployments if hint_lower in d.metadata.name.lower()]
    # Strategy 5: exact service-family match from service-specific namespace
    s5 = [d for d in deployments
          if _infer_namespace_context(d.metadata.namespace).get("service_family") == hint_lower]
    # Strategy 6: partial service-family match from service-specific namespace
    s6 = [d for d in deployments
          if hint_lower in (_infer_namespace_context(d.metadata.namespace).get("service_family") or "").lower()]

    strategy_used = None
    matches = []
    for label, group in (
        ("exact_dd_service", s1),
        ("exact_deployment_name", s2),
        ("partial_dd_service", s3),
        ("partial_deployment_name", s4),
        ("exact_namespace_service_family", s5),
        ("partial_namespace_service_family", s6),
    ):
        if group:
            strategy_used = label
            matches = group
            break

    if not matches:
        # Suggest available services so the model/user can pick.
        all_services = sorted({
            _effective_dd_tags(d).get("service")
            for d in deployments
            if _effective_dd_tags(d).get("service")
        })
        namespace_service_families = sorted({
            ctx["service_family"]
            for d in deployments
            for ctx in [_infer_namespace_context(d.metadata.namespace)]
            if ctx.get("service_family")
        })
        return {
            "error": (
                f"No Deployment matched '{hint}' on cluster '{cluster}' "
                f"(filters: env={env}, dep={dep}, namespace={namespace})."
            ),
            "available_dd_services": all_services,
            "available_namespace_service_families": namespace_service_families,
            "hint": (
                "Try one of the available services above, or use "
                "list_workloads_in_cluster for a full inventory."
            ),
        }

    return {
        "cluster": cluster,
        "workload_hint": hint,
        "match_strategy": strategy_used,
        "match_count": len(matches),
        "matches": [_deployment_summary(d, cluster) for d in matches],
        "queried_at": _now_utc().isoformat(),
    }


# ---------------------------------------------------------------------------
# Tool: resolve_hostname_to_workload
# ---------------------------------------------------------------------------

def resolve_hostname_to_workload(
    cluster: str,
    hostname: str,
) -> dict:
    """
    Reverse lookup: from a public hostname (e.g. from an alert URL) find
    the Service and Deployment that serves it, plus its Datadog tags.

    Walks Traefik IngressRoute custom resources, parses each `match` rule,
    finds those that contain `Host(`<hostname>`)`, follows the route to
    its target Service, and resolves the Service to the underlying
    Deployment via Service.spec.selector.

    Args:
        cluster: Cluster name.
        hostname: Public hostname (e.g. "api.test.bullfinch.com"). Match
            is exact on the Host(`...`) literal in IngressRoute rules.

    Returns:
        Match details with deployment name, namespace, and Datadog tags.
        On no match, lists hostnames found in the cluster as suggestions.
    """
    from .cluster_registry import custom_objects, core_v1, apps_v1

    co = custom_objects(cluster)

    # 1. Fetch all IngressRoute across all namespaces
    try:
        routes_resp = co.list_cluster_custom_object(
            group=_TRAEFIK_GROUP,
            version=_TRAEFIK_VERSION,
            plural=_TRAEFIK_INGRESSROUTE_PLURAL,
        )
    except Exception as e:
        return {
            "error": (
                f"Could not list IngressRoute on {cluster}: {e}. "
                f"Verify the Traefik CRD is installed at "
                f"{_TRAEFIK_GROUP}/{_TRAEFIK_VERSION}."
            )
        }

    routes = routes_resp.get("items", [])

    # 2. Find IngressRoute with a route matching the hostname
    matching_routes = []
    all_hostnames: set[str] = set()
    for route in routes:
        ns = route.get("metadata", {}).get("namespace")
        rname = route.get("metadata", {}).get("name")
        spec = route.get("spec", {}) or {}
        for r in spec.get("routes", []) or []:
            match_str = r.get("match", "") or ""
            # Extract all Host(`...`) literals from the match string
            hosts_in_rule = _extract_host_literals(match_str)
            for h in hosts_in_rule:
                all_hostnames.add(h)
                if h == hostname:
                    # This rule matches our hostname.
                    services = r.get("services") or []
                    matching_routes.append({
                        "ingress_route_name": rname,
                        "namespace": ns,
                        "match_rule": match_str.strip()[:300],
                        "services": services,
                    })

    if not matching_routes:
        return {
            "error": (
                f"No IngressRoute on cluster '{cluster}' matches host "
                f"'{hostname}'."
            ),
            "available_hostnames": sorted(all_hostnames),
            "hint": (
                "Pass one of the hostnames above, or strip the path "
                "(IngressRoute matches on Host() literals only)."
            ),
        }

    # 3. For each matching route, resolve services to deployments
    core = core_v1(cluster)
    apps = apps_v1(cluster)

    resolved = []
    for mr in matching_routes:
        for svc_ref in mr["services"]:
            svc_name = svc_ref.get("name")
            svc_ns = svc_ref.get("namespace") or mr["namespace"]
            if not svc_name:
                continue

            # Lookup the Service
            try:
                svc = core.read_namespaced_service(name=svc_name, namespace=svc_ns)
            except Exception as e:
                resolved.append({
                    "ingress_route": mr["ingress_route_name"],
                    "ingress_namespace": mr["namespace"],
                    "service_name": svc_name,
                    "service_namespace": svc_ns,
                    "error": f"Could not read Service: {e}",
                })
                continue

            selector = svc.spec.selector or {}
            if not selector:
                resolved.append({
                    "ingress_route": mr["ingress_route_name"],
                    "service_name": svc_name,
                    "service_namespace": svc_ns,
                    "error": "Service has no selector (headless or ExternalName?).",
                })
                continue

            # Find Deployment(s) in the service's namespace whose pod template
            # labels are a superset of the Service selector. We do this by
            # listing Deployments in the namespace and matching the selector.
            try:
                deps_resp = apps.list_namespaced_deployment(namespace=svc_ns)
            except Exception as e:
                resolved.append({
                    "ingress_route": mr["ingress_route_name"],
                    "service_name": svc_name,
                    "service_namespace": svc_ns,
                    "error": f"Could not list deployments: {e}",
                })
                continue

            matching_deps = []
            for d in deps_resp.items:
                d_labels = (d.spec.template.metadata.labels or {})
                if all(d_labels.get(k) == v for k, v in selector.items()):
                    matching_deps.append(d)

            if not matching_deps:
                resolved.append({
                    "ingress_route": mr["ingress_route_name"],
                    "service_name": svc_name,
                    "service_namespace": svc_ns,
                    "service_selector": selector,
                    "error": (
                        "No Deployment matches the Service selector. "
                        "May be served by a StatefulSet, DaemonSet, or "
                        "bare pods."
                    ),
                })
                continue

            for d in matching_deps:
                resolved.append({
                    "ingress_route": mr["ingress_route_name"],
                    "ingress_namespace": mr["namespace"],
                    "service_name": svc_name,
                    "service_namespace": svc_ns,
                    "deployment": _deployment_summary(d, cluster),
                })

    return {
        "cluster": cluster,
        "hostname": hostname,
        "match_count": len(resolved),
        "resolutions": resolved,
        "queried_at": _now_utc().isoformat(),
    }


# Regex precompiled for Host() literal extraction.
import re as _re
_HOST_LITERAL_RE = _re.compile(r"Host\(\s*`([^`]+)`\s*\)")


def _extract_host_literals(match_string: str) -> list[str]:
    """Extract all Host(`...`) hostnames from a Traefik match expression."""
    if not match_string:
        return []
    return _HOST_LITERAL_RE.findall(match_string)
