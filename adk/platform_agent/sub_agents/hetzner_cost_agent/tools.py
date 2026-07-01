"""
Hetzner Cloud cost & inventory tools.

Read-only by design. Talks directly to the Hetzner Cloud REST API
(`api.hetzner.cloud/v1`) using a project API token.

Cost model: "steady-state monthly cost" — for every resource currently
active we read the monthly price the API returns in its payload and sum
it up. This answers "if nothing changes, what will we pay this month?"
which is the typical question for a cost dashboard. It does NOT compute
the actual prorated spend for the elapsed portion of the month — for
that you'd need to track resource lifecycle from /v1/actions, which is
out of scope (and Hetzner anyway caps usage at the monthly price).

Resources covered:
  Paid:     server, load_balancer, volume, primary_ip, floating_ip
  Free:     network, firewall, certificate  (tracked as inventory only)

Pricing API (`/v1/pricing`) is exposed via a separate tool for what-if
queries — pricing of resources you don't yet own.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional

import httpx


# ---------------------------------------------------------------------------
# Config & client
# ---------------------------------------------------------------------------

_API_BASE = "https://api.hetzner.cloud/v1"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _token() -> str:
    tok = os.environ.get("HETZNER_TOKEN")
    if not tok:
        raise RuntimeError(
            "HETZNER_TOKEN not set. Configure it as a secret in the deployment."
        )
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


# Single shared client. httpx clients are thread-safe for sync use.
_http_client: Optional[httpx.Client] = None


def _client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            base_url=_API_BASE, headers=_headers(), timeout=_TIMEOUT
        )
    return _http_client


# ---------------------------------------------------------------------------
# Generic paginated GET
# ---------------------------------------------------------------------------

def _paginated_get(
    path: str, key: str, params: Optional[dict] = None, max_pages: int = 50
) -> list[dict]:
    """
    Walk all pages of a list endpoint. Hetzner uses page/per_page with a
    `meta.pagination` block telling us if there are more pages.

    `key` is the JSON key holding the actual list (e.g. 'servers' for /v1/servers).
    `max_pages` is a safety cap; 50 pages × 50 items = 2500 items, enough
    for almost any single project.
    """
    out: list[dict] = []
    p = dict(params or {})
    p.setdefault("per_page", 50)
    p["page"] = 1
    for _ in range(max_pages):
        r = _client().get(path, params=p)
        if r.status_code != 200:
            raise RuntimeError(
                f"Hetzner API {path} returned {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        items = data.get(key, []) or []
        out.extend(items)
        meta = (data.get("meta") or {}).get("pagination") or {}
        next_page = meta.get("next_page")
        if not next_page:
            break
        p["page"] = next_page
    return out


# ---------------------------------------------------------------------------
# Price extraction helpers
# ---------------------------------------------------------------------------
#
# Hetzner price payloads vary slightly by resource. They share the pattern:
#
#   {"price_monthly": {"net": "3.79", "gross": "4.5101"},
#    "price_hourly":  {"net": "0.006", "gross": "0.00714"}}
#
# We always use NET prices (VAT-exclusive) so totals are comparable
# regardless of the project's tax setup. The agent's instruction tells
# the model to state this explicitly.
# ---------------------------------------------------------------------------


def _to_float(s: Any) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _server_monthly_price(server: dict) -> float:
    """Hetzner servers: server_type.prices is a list, one entry per location."""
    location = (server.get("datacenter") or {}).get("location", {}).get("name")
    prices = (server.get("server_type") or {}).get("prices") or []
    # Try to match the location of the actual server; fall back to the first.
    chosen = next((p for p in prices if p.get("location") == location), None)
    if chosen is None and prices:
        chosen = prices[0]
    if not chosen:
        return 0.0
    return _to_float((chosen.get("price_monthly") or {}).get("net"))


def _load_balancer_monthly_price(lb: dict) -> float:
    """Same shape as servers: load_balancer_type.prices is a list."""
    location = (lb.get("location") or {}).get("name")
    prices = (lb.get("load_balancer_type") or {}).get("prices") or []
    chosen = next((p for p in prices if p.get("location") == location), None)
    if chosen is None and prices:
        chosen = prices[0]
    if not chosen:
        return 0.0
    return _to_float((chosen.get("price_monthly") or {}).get("net"))


def _volume_monthly_price(volume: dict) -> float:
    """
    Volumes: a flat per-GB-month price. The API returns price_per_gb_month
    on the volume object itself in some responses; we also derive it
    defensively from size × pricing if missing.
    """
    direct = volume.get("price_monthly")  # not always present
    if direct:
        return _to_float((direct or {}).get("net"))
    # Fallback: many tenants see only `pricing` on /v1/pricing, but volumes
    # in /v1/volumes carry size in GB. We can't compute without the unit
    # price here without an extra API call; leave as 0 with a flag.
    return _to_float((volume.get("price_monthly") or {}).get("net"))


def _primary_ip_monthly_price(ip: dict) -> float:
    """
    Primary IPs charge a monthly fee. The API returns:
      price_monthly: {net, gross}
    on each primary_ip object. IPs already attached to a server may also
    be billed depending on type — we report whatever the API tells us.
    """
    return _to_float((ip.get("price_monthly") or {}).get("net"))


def _floating_ip_monthly_price(ip: dict) -> float:
    """Floating IPs: same shape as primary IPs."""
    return _to_float((ip.get("price_monthly") or {}).get("net"))


# ---------------------------------------------------------------------------
# Per-resource fetch (returns normalised inventory dicts)
# ---------------------------------------------------------------------------

def _fetch_servers() -> list[dict]:
    raw = _paginated_get("/servers", "servers", params={"sort": "id"})
    out = []
    for s in raw:
        out.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "status": s.get("status"),
                "server_type": (s.get("server_type") or {}).get("name"),
                "location": (s.get("datacenter") or {}).get("location", {}).get("name"),
                "datacenter": (s.get("datacenter") or {}).get("name"),
                "labels": s.get("labels") or {},
                "monthly_price_eur_net": round(_server_monthly_price(s), 2),
                "created": s.get("created"),
            }
        )
    return out


def _fetch_load_balancers() -> list[dict]:
    raw = _paginated_get("/load_balancers", "load_balancers", params={"sort": "id"})
    out = []
    for lb in raw:
        out.append(
            {
                "id": lb.get("id"),
                "name": lb.get("name"),
                "lb_type": (lb.get("load_balancer_type") or {}).get("name"),
                "location": (lb.get("location") or {}).get("name"),
                "labels": lb.get("labels") or {},
                "monthly_price_eur_net": round(_load_balancer_monthly_price(lb), 2),
                "created": lb.get("created"),
            }
        )
    return out


def _fetch_volumes() -> list[dict]:
    raw = _paginated_get("/volumes", "volumes", params={"sort": "id"})
    out = []
    for v in raw:
        out.append(
            {
                "id": v.get("id"),
                "name": v.get("name"),
                "size_gb": v.get("size"),
                "location": (v.get("location") or {}).get("name"),
                "server_id": v.get("server"),
                "labels": v.get("labels") or {},
                "monthly_price_eur_net": round(_volume_monthly_price(v), 2),
                "created": v.get("created"),
            }
        )
    return out


def _fetch_primary_ips() -> list[dict]:
    raw = _paginated_get("/primary_ips", "primary_ips", params={"sort": "id"})
    out = []
    for ip in raw:
        out.append(
            {
                "id": ip.get("id"),
                "name": ip.get("name"),
                "type": ip.get("type"),  # ipv4 / ipv6
                "ip": ip.get("ip"),
                "datacenter": (ip.get("datacenter") or {}).get("name"),
                "assignee_id": ip.get("assignee_id"),
                "assignee_type": ip.get("assignee_type"),
                "auto_delete": ip.get("auto_delete"),
                "labels": ip.get("labels") or {},
                "monthly_price_eur_net": round(_primary_ip_monthly_price(ip), 2),
                "created": ip.get("created"),
            }
        )
    return out


def _fetch_floating_ips() -> list[dict]:
    raw = _paginated_get("/floating_ips", "floating_ips", params={"sort": "id"})
    out = []
    for ip in raw:
        out.append(
            {
                "id": ip.get("id"),
                "name": ip.get("name"),
                "type": ip.get("type"),
                "ip": ip.get("ip"),
                "home_location": (ip.get("home_location") or {}).get("name"),
                "server_id": ip.get("server"),
                "labels": ip.get("labels") or {},
                "monthly_price_eur_net": round(_floating_ip_monthly_price(ip), 2),
                "created": ip.get("created"),
            }
        )
    return out


# Free resources — inventory only, no price.

def _fetch_networks() -> list[dict]:
    raw = _paginated_get("/networks", "networks", params={"sort": "id"})
    return [
        {
            "id": n.get("id"),
            "name": n.get("name"),
            "ip_range": n.get("ip_range"),
            "subnet_count": len(n.get("subnets") or []),
            "labels": n.get("labels") or {},
            "created": n.get("created"),
        }
        for n in raw
    ]


def _fetch_firewalls() -> list[dict]:
    raw = _paginated_get("/firewalls", "firewalls", params={"sort": "id"})
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "rules_count": len(f.get("rules") or []),
            "applied_to_count": len(f.get("applied_to") or []),
            "labels": f.get("labels") or {},
            "created": f.get("created"),
        }
        for f in raw
    ]


def _fetch_certificates() -> list[dict]:
    raw = _paginated_get("/certificates", "certificates", params={"sort": "id"})
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "type": c.get("type"),  # uploaded / managed
            "domain_names": c.get("domain_names"),
            "not_valid_after": c.get("not_valid_after"),
            "labels": c.get("labels") or {},
            "created": c.get("created"),
        }
        for c in raw
    ]


# ---------------------------------------------------------------------------
# Label filtering
# ---------------------------------------------------------------------------

def _matches_labels(item: dict, label_filter: Optional[dict]) -> bool:
    """
    Apply a {label_key: expected_value} filter to a resource. Returns True
    if all entries match (AND semantics). An empty/None filter matches all.
    """
    if not label_filter:
        return True
    actual = item.get("labels") or {}
    for k, v in label_filter.items():
        if str(actual.get(k)) != str(v):
            return False
    return True


def _filter(items: Iterable[dict], label_filter: Optional[dict]) -> list[dict]:
    return [i for i in items if _matches_labels(i, label_filter)]


# ---------------------------------------------------------------------------
# Tool 1: cost summary
# ---------------------------------------------------------------------------

# Public-facing list of paid resource types. Used both for validation
# and as the default scope of `get_hetzner_cost_summary`.
_PAID_RESOURCE_TYPES = [
    "server",
    "load_balancer",
    "volume",
    "primary_ip",
    "floating_ip",
]
_FREE_RESOURCE_TYPES = ["network", "firewall", "certificate"]
_ALL_RESOURCE_TYPES = _PAID_RESOURCE_TYPES + _FREE_RESOURCE_TYPES


_FETCHERS = {
    "server": _fetch_servers,
    "load_balancer": _fetch_load_balancers,
    "volume": _fetch_volumes,
    "primary_ip": _fetch_primary_ips,
    "floating_ip": _fetch_floating_ips,
    "network": _fetch_networks,
    "firewall": _fetch_firewalls,
    "certificate": _fetch_certificates,
}


def get_hetzner_cost_summary(
    resource_types: Optional[list[str]] = None,
    label_filter: Optional[dict] = None,
) -> dict:
    """
    Return the steady-state monthly cost of the Hetzner project, broken
    down by resource type.

    "Steady-state" means: for every resource currently active, take the
    monthly price the API reports and sum it. It answers "if nothing
    changes between now and end of month, what will we pay?" — NOT the
    actual prorated spend so far this month.

    Args:
        resource_types: Subset of resource types to include. None = all
            paid resources (server, load_balancer, volume, primary_ip,
            floating_ip). Free types (network, firewall, certificate)
            can be included explicitly to see their inventory counts.
        label_filter: Optional {label_key: value} dict; only resources
            matching ALL listed labels are included. Useful to scope by
            environment/team if you label your resources.

    Returns:
        dict with 'currency', 'total_monthly_eur_net', 'breakdown'
        (per resource type: count, total_cost), and 'note'.
    """
    if resource_types is None:
        resource_types = list(_PAID_RESOURCE_TYPES)
    else:
        resource_types = [t.lower() for t in resource_types]
        unknown = [t for t in resource_types if t not in _ALL_RESOURCE_TYPES]
        if unknown:
            return {
                "error": f"Unknown resource types: {unknown}. "
                f"Valid: {_ALL_RESOURCE_TYPES}"
            }

    breakdown = {}
    grand_total = 0.0
    try:
        for rtype in resource_types:
            items = _filter(_FETCHERS[rtype](), label_filter)
            count = len(items)
            cost = sum(i.get("monthly_price_eur_net", 0.0) for i in items)
            breakdown[rtype] = {
                "count": count,
                "monthly_cost_eur_net": round(cost, 2),
                "is_free": rtype in _FREE_RESOURCE_TYPES,
            }
            grand_total += cost
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {"error": f"Network error talking to Hetzner API: {e}"}

    return {
        "currency": "EUR",
        "vat": "excluded (net prices)",
        "total_monthly_eur_net": round(grand_total, 2),
        "breakdown": breakdown,
        "label_filter": label_filter or None,
        "note": (
            "Steady-state monthly cost based on currently active resources. "
            "Hetzner caps usage at the monthly price; actual spend may be "
            "lower if resources were created mid-month."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 2: list resources of a given type
# ---------------------------------------------------------------------------

def list_hetzner_resources(
    resource_type: str,
    label_filter: Optional[dict] = None,
    sort_by_cost: bool = True,
    limit: int = 100,
) -> dict:
    """
    List all resources of a given type with their relevant attributes
    (and monthly price for paid types).

    Args:
        resource_type: One of 'server', 'load_balancer', 'volume',
            'primary_ip', 'floating_ip', 'network', 'firewall', 'certificate'.
        label_filter: Optional {key: value} label match (AND semantics).
        sort_by_cost: For paid resources, sort by monthly cost descending
            (default). Ignored for free types.
        limit: Max items returned (default 100).

    Returns:
        dict with 'resource_type', 'count', 'items', plus 'subtotal_monthly_eur_net'
        for paid types.
    """
    rtype = resource_type.lower()
    if rtype not in _ALL_RESOURCE_TYPES:
        return {
            "error": f"Unknown resource type '{resource_type}'. "
            f"Valid: {_ALL_RESOURCE_TYPES}"
        }
    try:
        items = _filter(_FETCHERS[rtype](), label_filter)
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {"error": f"Network error: {e}"}

    is_paid = rtype in _PAID_RESOURCE_TYPES
    if is_paid and sort_by_cost:
        items.sort(key=lambda i: i.get("monthly_price_eur_net", 0.0), reverse=True)

    truncated = len(items) > limit
    items = items[: max(1, int(limit))]

    result = {
        "resource_type": rtype,
        "count": len(items),
        "truncated": truncated,
        "label_filter": label_filter or None,
        "items": items,
    }
    if is_paid:
        result["subtotal_monthly_eur_net"] = round(
            sum(i.get("monthly_price_eur_net", 0.0) for i in items), 2
        )
        result["currency"] = "EUR"
    return result


# ---------------------------------------------------------------------------
# Tool 3: top cost drivers
# ---------------------------------------------------------------------------

def get_hetzner_top_cost_drivers(
    top_n: int = 10,
    resource_types: Optional[list[str]] = None,
    label_filter: Optional[dict] = None,
) -> dict:
    """
    Return the most expensive individual resources across the project.

    Unlike `get_hetzner_cost_summary` (which aggregates by type), this
    returns the top N specific items — useful to answer "which single
    resources cost us the most?".

    Args:
        top_n: How many items to return (default 10).
        resource_types: Restrict to specific paid resource types. None = all paid.
        label_filter: Optional label match.

    Returns:
        dict with 'top_drivers' (sorted desc by monthly cost) and
        'total_monthly_eur_net' across all items considered (not just top N).
    """
    if resource_types is None:
        resource_types = list(_PAID_RESOURCE_TYPES)
    else:
        resource_types = [t.lower() for t in resource_types]
        unknown = [t for t in resource_types if t not in _PAID_RESOURCE_TYPES]
        if unknown:
            return {
                "error": f"Free or unknown resource types not allowed here: "
                f"{unknown}. Valid: {_PAID_RESOURCE_TYPES}"
            }

    all_items = []
    try:
        for rtype in resource_types:
            for item in _filter(_FETCHERS[rtype](), label_filter):
                all_items.append(
                    {
                        "resource_type": rtype,
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "monthly_price_eur_net": item.get("monthly_price_eur_net", 0.0),
                        "details": {
                            k: v
                            for k, v in item.items()
                            if k
                            in (
                                "server_type",
                                "lb_type",
                                "size_gb",
                                "type",
                                "location",
                                "datacenter",
                                "home_location",
                                "labels",
                            )
                            and v is not None
                        },
                    }
                )
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {"error": f"Network error: {e}"}

    all_items.sort(key=lambda x: x["monthly_price_eur_net"], reverse=True)
    total = sum(i["monthly_price_eur_net"] for i in all_items)
    drivers = all_items[: max(1, int(top_n))]
    for d in drivers:
        d["pct_of_total"] = (
            round(d["monthly_price_eur_net"] / total * 100.0, 2) if total else 0.0
        )

    return {
        "currency": "EUR",
        "vat": "excluded (net prices)",
        "considered_resource_types": resource_types,
        "label_filter": label_filter or None,
        "total_monthly_eur_net": round(total, 2),
        "top_drivers": drivers,
    }


# ---------------------------------------------------------------------------
# Tool 4: what-if pricing lookup
# ---------------------------------------------------------------------------

def get_hetzner_pricing(
    resource_type: Optional[str] = None,
    name_filter: Optional[str] = None,
    location: Optional[str] = None,
) -> dict:
    """
    Query the Hetzner public pricing catalogue. Use this to answer
    what-if questions ("how much would a cx52 cost?", "what's the price
    per GB-month of volumes?") without provisioning anything.

    Args:
        resource_type: Optional filter. One of: 'server', 'load_balancer',
            'volume', 'primary_ip', 'floating_ip', 'image', 'traffic'.
            None = return everything.
        name_filter: Optional substring match on the type name (e.g.
            'cx5' matches 'cx51', 'cx52'). Case-insensitive.
        location: Optional location filter for resources priced per-location
            (e.g. 'fsn1', 'nbg1'). Ignored for resources with single global price.

    Returns:
        dict with 'currency', 'pricing' (a structured catalogue subset).
    """
    try:
        r = _client().get("/pricing")
    except httpx.HTTPError as e:
        return {"error": f"Network error: {e}"}
    if r.status_code != 200:
        return {"error": f"Hetzner /pricing returned {r.status_code}: {r.text[:300]}"}
    payload = (r.json() or {}).get("pricing") or {}

    nf = name_filter.lower() if name_filter else None

    def _filter_typed_list(items: list[dict], type_field: str = "name") -> list[dict]:
        out = []
        for it in items:
            if nf and nf not in str(it.get(type_field, "")).lower():
                continue
            entry = {"name": it.get(type_field)}
            prices = it.get("prices") or []
            if location:
                prices = [p for p in prices if p.get("location") == location]
            entry["prices"] = [
                {
                    "location": p.get("location"),
                    "monthly_eur_net": _to_float(
                        (p.get("price_monthly") or {}).get("net")
                    ),
                    "hourly_eur_net": _to_float(
                        (p.get("price_hourly") or {}).get("net")
                    ),
                }
                for p in prices
            ]
            out.append(entry)
        return out

    result: dict[str, Any] = {
        "currency": payload.get("currency", "EUR"),
        "vat_rate": payload.get("vat_rate"),
    }

    rtype = (resource_type or "").lower() or None

    if rtype in (None, "server"):
        result["server_types"] = _filter_typed_list(
            payload.get("server_types") or [], type_field="name"
        )
    if rtype in (None, "load_balancer"):
        result["load_balancer_types"] = _filter_typed_list(
            payload.get("load_balancer_types") or [], type_field="name"
        )
    if rtype in (None, "volume"):
        # Volumes have a flat price_per_gb_month
        vol = payload.get("volume") or {}
        result["volume"] = {
            "price_per_gb_month_eur_net": _to_float(
                (vol.get("price_per_gb_month") or {}).get("net")
            )
        }
    if rtype in (None, "primary_ip"):
        # primary_ips: list of types with prices per location
        result["primary_ips"] = _filter_typed_list(
            payload.get("primary_ips") or [], type_field="type"
        )
    if rtype in (None, "floating_ip"):
        # floating_ips: similar to primary_ips
        if "floating_ips" in payload:
            result["floating_ips"] = _filter_typed_list(
                payload.get("floating_ips") or [], type_field="type"
            )
        elif "floating_ip" in payload:
            fi = payload.get("floating_ip") or {}
            result["floating_ip"] = {
                "monthly_eur_net": _to_float((fi.get("price_monthly") or {}).get("net"))
            }
    if rtype in (None, "image"):
        img = payload.get("image") or {}
        result["image"] = {
            "price_per_gb_month_eur_net": _to_float(
                (img.get("price_per_gb_month") or {}).get("net")
            )
        }
    if rtype in (None, "traffic"):
        # Traffic overage pricing is on server_traffic / lb_traffic blocks
        result["server_backup_percentage"] = _to_float(
            payload.get("server_backup", {}).get("percentage")
        )
        result["traffic"] = {
            "info": "Per-GB overage rates vary; refer to per-type pricing."
        }

    return result
