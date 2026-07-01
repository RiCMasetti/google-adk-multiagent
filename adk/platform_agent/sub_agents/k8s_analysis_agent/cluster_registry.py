"""
Cluster registry and Kubernetes client factory.

Single source of truth for:
  - Which clusters exist
  - Where each cluster's kubeconfig lives
  - Whether each cluster is a 'workload' (actions allowed in the future)
    or 'management' (read-only, e.g. Rancher control planes)

This module is shared between the analysis agent (today) and the future
action agent: the analysis agent reads from all clusters indiscriminately,
while the action agent will refuse to act on management clusters by
checking `is_workload_cluster(name)`.

Configuration model
-------------------

Two env vars define the registry:

  KUBE_CLUSTERS
    Comma-separated list of "name=path" entries pointing each cluster
    name to a kubeconfig file inside the container.
    Example:
      "helios-dev=/etc/kube/helios-dev.yaml,bullfinch-mcp=/etc/kube/bullfinch-mcp.yaml,helios-prod=/etc/kube/helios-prod.yaml,rancher-dev=/etc/kube/rancher-dev.yaml,rancher-prod=/etc/kube/rancher-prod.yaml"

  KUBE_MANAGEMENT_CLUSTERS
    Comma-separated list of cluster names (matching KUBE_CLUSTERS keys)
    that are management/control-plane clusters. These are READ-ONLY for
    every agent; the action agent (when implemented) will refuse them.
    Example:
      "rancher-dev,rancher-prod"

Anything in KUBE_CLUSTERS that is NOT in KUBE_MANAGEMENT_CLUSTERS is
considered a workload cluster.

Auth
----

Each kubeconfig should embed a scoped Rancher API token, a ServiceAccount
token, or use exec/credential plugins for an identity with the read-only
permissions documented in the README. No long-lived admin tokens.

Mount the kubeconfigs as a Kubernetes Secret or read-only volume; never
bake them into the image.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClusterEntry:
    name: str
    kubeconfig_path: str
    is_workload: bool  # True = real workload cluster; False = management


def _parse_kv_list(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def _load_registry() -> dict[str, ClusterEntry]:
    raw_clusters = os.environ.get("KUBE_CLUSTERS", "")
    raw_mgmt = os.environ.get("KUBE_MANAGEMENT_CLUSTERS", "")
    paths = _parse_kv_list(raw_clusters)
    mgmt = {n.strip() for n in raw_mgmt.split(",") if n.strip()}
    if not paths:
        # Sensible defaults matching the team's topology, useful for local dev.
        # In production KUBE_CLUSTERS should always be set explicitly.
        paths = {
            "helios-dev":     "/etc/kube/helios-dev.yaml",
            "bullfinch-mcp":  "/etc/kube/bullfinch-mcp.yaml",
            "helios-prod":    "/etc/kube/helios-prod.yaml",
            "rancher-dev":    "/etc/kube/rancher-dev.yaml",
            "rancher-prod":   "/etc/kube/rancher-prod.yaml",
        }
        mgmt = mgmt or {"rancher-dev", "rancher-prod"}
    return {
        name: ClusterEntry(name=name, kubeconfig_path=path, is_workload=name not in mgmt)
        for name, path in paths.items()
    }


_REGISTRY = _load_registry()


def list_known_clusters() -> list[str]:
    """Return every configured cluster name (workload + management)."""
    return list(_REGISTRY.keys())


def list_workload_clusters() -> list[str]:
    return [c.name for c in _REGISTRY.values() if c.is_workload]


def list_management_clusters() -> list[str]:
    return [c.name for c in _REGISTRY.values() if not c.is_workload]


def is_workload_cluster(name: str) -> bool:
    """True if the cluster exists and is a workload cluster (actions allowed)."""
    entry = _REGISTRY.get(name)
    return bool(entry and entry.is_workload)


def get_cluster(name: str) -> Optional[ClusterEntry]:
    return _REGISTRY.get(name)


def resolve_clusters(selector: Optional[str]) -> list[str]:
    """
    Resolve a selector string into the list of clusters to operate on.

      None or "all"        -> all known clusters (workload + management)
      "workload"           -> only workload clusters
      "management"         -> only management clusters
      "<concrete name>"    -> just that one (must exist)

    Raises ValueError on unknown selectors / cluster names.
    """
    if selector in (None, "all", ""):
        return list_known_clusters()
    if selector == "workload":
        return list_workload_clusters()
    if selector == "management":
        return list_management_clusters()
    if selector in _REGISTRY:
        return [selector]
    raise ValueError(
        f"Unknown cluster '{selector}'. Known: {list_known_clusters()}; "
        f"or use 'all' / 'workload' / 'management'."
    )


# ---------------------------------------------------------------------------
# k8s client factory (cached per cluster name)
# ---------------------------------------------------------------------------

# kubernetes-python keeps state in module-level configuration when you call
# load_kube_config(). To avoid clobbering, we build a fresh ApiClient per
# cluster from a Configuration object. Cached in a dict, protected by a
# lock so concurrent tool calls don't race.
_clients_lock = threading.Lock()
_clients_cache: dict[str, k8s_client.ApiClient] = {}


def _build_api_client(entry: ClusterEntry) -> k8s_client.ApiClient:
    cfg = k8s_client.Configuration()
    k8s_config.load_kube_config(
        config_file=entry.kubeconfig_path,
        client_configuration=cfg,
    )
    _normalize_bearer_token_auth(cfg)
    return k8s_client.ApiClient(configuration=cfg)


def _normalize_bearer_token_auth(cfg: k8s_client.Configuration) -> None:
    """
    Keep Rancher token kubeconfigs compatible with newer kubernetes-python.

    Rancher kubeconfigs commonly load into `api_key["authorization"]` as a
    full "Bearer <token>" header value. kubernetes-python generated API
    methods use the `BearerToken` auth key, so without this normalization SDK
    calls can be sent without the loaded token even though kubectl works.
    """
    authorization = cfg.api_key.get("authorization")
    if not authorization:
        return

    token = authorization
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1]

    cfg.api_key["BearerToken"] = token
    cfg.api_key_prefix["BearerToken"] = "Bearer"


def api_client_for(cluster: str) -> k8s_client.ApiClient:
    """Return a cached ApiClient for the given cluster name."""
    entry = _REGISTRY.get(cluster)
    if entry is None:
        raise ValueError(
            f"Unknown cluster '{cluster}'. Known: {list_known_clusters()}"
        )
    with _clients_lock:
        if cluster not in _clients_cache:
            _clients_cache[cluster] = _build_api_client(entry)
        return _clients_cache[cluster]


# Convenience accessors ------------------------------------------------------

def core_v1(cluster: str) -> k8s_client.CoreV1Api:
    return k8s_client.CoreV1Api(api_client_for(cluster))


def apps_v1(cluster: str) -> k8s_client.AppsV1Api:
    return k8s_client.AppsV1Api(api_client_for(cluster))


def batch_v1(cluster: str) -> k8s_client.BatchV1Api:
    return k8s_client.BatchV1Api(api_client_for(cluster))


def networking_v1(cluster: str) -> k8s_client.NetworkingV1Api:
    return k8s_client.NetworkingV1Api(api_client_for(cluster))


def custom_objects(cluster: str) -> k8s_client.CustomObjectsApi:
    """Used to read metrics.k8s.io for node/pod metrics."""
    return k8s_client.CustomObjectsApi(api_client_for(cluster))
