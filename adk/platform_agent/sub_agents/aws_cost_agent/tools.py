"""
Native AWS Cost Explorer tools for the platform agent.

Why native instead of wrapping the official AWS Cost MCP server:
  - Surface area is small (4 Cost Explorer API methods cover everything we need).
  - Output formats (CSV reports, alias-rewritten responses) are awkward via MCP.
  - IAM scoping is naturally minimal: only the Cost Explorer read actions.
  - One less moving part in the deployment.

Authentication:
  Uses the standard boto3 credential chain. For local Compose, `~/.aws` and
  the optional certs directory are mounted into the container. boto3 picks up
  profiles, environment credentials, or role-based credentials transparently;
  no custom credential code is needed here.

Multi-account:
  When credentials belong to an AWS Organizations management account, Cost
  Explorer can return consolidated data for linked accounts. Linked accounts
  come back as 12-digit IDs; optional aliases rewrite those IDs before
  returning results to the LLM.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError


# ---------------------------------------------------------------------------
# Account alias map
# ---------------------------------------------------------------------------
#
# Optional lookup table: 12-digit account ID -> human-readable alias.
# Configure via AWS_ACCOUNT_ALIASES, format:
#     "111111111111=management,222222222222=production,..."
# Cost Explorer returns any unmapped account ID unchanged.


def _load_account_aliases() -> dict[str, str]:
    raw = os.environ.get("AWS_ACCOUNT_ALIASES", "").strip()
    if not raw:
        return {}
    aliases: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            aliases[k] = v
    return aliases


_ACCOUNT_ALIASES = _load_account_aliases()


def _alias(account_id: str) -> str:
    """Return alias if known, else the raw ID (so anomalies stay visible)."""
    return _ACCOUNT_ALIASES.get(account_id, account_id)


# ---------------------------------------------------------------------------
# boto3 client
# ---------------------------------------------------------------------------

_ce_client = None


def _client():
    """
    Cost Explorer is a global service but its endpoint lives in us-east-1.
    boto3 handles this automatically when region is set, but being explicit
    avoids surprises when AWS_DEFAULT_REGION points elsewhere (e.g. eu-west-1
    for the rest of the deployment).
    """
    global _ce_client
    if _ce_client is None:
        _ce_client = boto3.client("ce", region_name="us-east-1")
    return _ce_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_GRANULARITY = {"DAILY", "MONTHLY", "HOURLY"}
_GROUP_BY_DIMENSIONS = {"SERVICE", "LINKED_ACCOUNT", "REGION", "USAGE_TYPE", "INSTANCE_TYPE"}


def _validate_dates(start_date: str, end_date: str) -> Optional[str]:
    """Return None if valid, else an error message. Cost Explorer wants YYYY-MM-DD,
    end_date is exclusive."""
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return "Dates must be in YYYY-MM-DD format."
    if e <= s:
        return "end_date must be strictly after start_date (Cost Explorer treats end_date as exclusive)."
    if s > date.today():
        return "start_date is in the future."
    return None


def _parse_group_by(group_by: Optional[str]) -> Optional[list[dict]]:
    """
    Convert a friendly group_by string into the Cost Explorer GroupBy structure.
    Accepts:
      - None or "" -> no grouping
      - dimension name (SERVICE, LINKED_ACCOUNT, REGION, USAGE_TYPE, INSTANCE_TYPE)
      - "TAG:<key>" for cost allocation tags (must be enabled in Billing console)
    """
    if not group_by:
        return None
    g = group_by.strip().upper()
    if g.startswith("TAG:"):
        return [{"Type": "TAG", "Key": group_by.split(":", 1)[1].strip()}]
    if g in _GROUP_BY_DIMENSIONS:
        return [{"Type": "DIMENSION", "Key": g}]
    return None  # caller checks for None and reports an error


def _extract_amount(metric_obj: dict) -> tuple[float, str]:
    """Extract numeric amount + currency from a Cost Explorer metric block."""
    return float(metric_obj.get("Amount", 0)), metric_obj.get("Unit", "USD")


def _rewrite_group_key(dim: str, key: str) -> str:
    """Apply alias rewriting where applicable (currently LINKED_ACCOUNT only)."""
    if dim == "LINKED_ACCOUNT":
        return _alias(key)
    return key


# ---------------------------------------------------------------------------
# Tool: cost summary
# ---------------------------------------------------------------------------

def get_cost_summary(
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
    group_by: Optional[str] = None,
    metric: str = "UnblendedCost",
) -> dict:
    """
    Return AWS cost for a period, optionally grouped by a dimension or tag.

    Args:
        start_date: First day of the window, format YYYY-MM-DD (inclusive).
        end_date: Day after the last day of the window, format YYYY-MM-DD
                  (exclusive — Cost Explorer convention). E.g. for "March 2026"
                  use start=2026-03-01, end=2026-04-01.
        granularity: "DAILY", "MONTHLY", or "HOURLY". Default "MONTHLY".
                     Note: HOURLY only available for the last 14 days and costs more.
        group_by: Optional. Dimension name ("SERVICE", "LINKED_ACCOUNT",
                  "REGION", "USAGE_TYPE", "INSTANCE_TYPE") or "TAG:<key>"
                  for a cost allocation tag.
        metric: Which cost metric. "UnblendedCost" (default — what each
                account paid before discounts), "AmortizedCost" (RIs/SP
                spread over their term), "BlendedCost", "NetUnblendedCost".

    Returns:
        dict with the cost breakdown, or {"error": "..."} on failure.
    """
    err = _validate_dates(start_date, end_date)
    if err:
        return {"error": err}
    g = granularity.upper()
    if g not in _VALID_GRANULARITY:
        return {"error": f"granularity must be one of {sorted(_VALID_GRANULARITY)}"}

    group_by_struct = None
    if group_by:
        group_by_struct = _parse_group_by(group_by)
        if group_by_struct is None:
            return {
                "error": (
                    f"Unsupported group_by '{group_by}'. Use one of "
                    f"{sorted(_GROUP_BY_DIMENSIONS)} or 'TAG:<key>'."
                )
            }

    kwargs = {
        "TimePeriod": {"Start": start_date, "End": end_date},
        "Granularity": g,
        "Metrics": [metric],
    }
    if group_by_struct:
        kwargs["GroupBy"] = group_by_struct

    try:
        resp = _client().get_cost_and_usage(**kwargs)
    except (ClientError, BotoCoreError) as e:
        return {"error": f"AWS Cost Explorer error: {e}"}

    periods = []
    currency = "USD"
    for r in resp.get("ResultsByTime", []):
        period = {
            "start": r["TimePeriod"]["Start"],
            "end": r["TimePeriod"]["End"],
            "estimated": r.get("Estimated", False),
        }
        groups = r.get("Groups") or []
        if groups:
            entries = []
            dim = group_by_struct[0]["Key"] if group_by_struct[0]["Type"] == "DIMENSION" else "TAG"
            for grp in groups:
                amount, currency = _extract_amount(grp["Metrics"][metric])
                key_raw = grp["Keys"][0] if grp.get("Keys") else "(empty)"
                entries.append(
                    {
                        "key": _rewrite_group_key(dim, key_raw),
                        "amount": round(amount, 2),
                    }
                )
            entries.sort(key=lambda x: x["amount"], reverse=True)
            period["breakdown"] = entries
            period["total"] = round(sum(e["amount"] for e in entries), 2)
        else:
            total = r.get("Total", {}).get(metric, {})
            amount, currency = _extract_amount(total) if total else (0.0, "USD")
            period["total"] = round(amount, 2)
        periods.append(period)

    return {
        "metric": metric,
        "granularity": g,
        "group_by": group_by,
        "currency": currency,
        "periods": periods,
        "grand_total": round(sum(p["total"] for p in periods), 2),
    }


# ---------------------------------------------------------------------------
# Tool: compare two periods
# ---------------------------------------------------------------------------

def compare_periods(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    group_by: Optional[str] = None,
    metric: str = "UnblendedCost",
) -> dict:
    """
    Compare AWS cost between two arbitrary periods (period A vs period B).

    Typical pattern: A = previous month, B = current month, group_by=SERVICE
    -> "where did spend grow MoM and where did it shrink?"

    Both periods use the same convention as `get_cost_summary` (end is
    exclusive). Periods do not need to be the same length — but if they are
    not, the comparison is reported with a warning since absolute deltas
    aren't directly meaningful.

    Args:
        period_a_start, period_a_end: "Baseline" period (e.g. previous month).
        period_b_start, period_b_end: "Current" period (e.g. this month).
        group_by: Optional dimension to break down the comparison by.
        metric: Cost metric (see get_cost_summary).

    Returns:
        dict with totals for both periods, delta absolute and percentage,
        and per-key breakdown if group_by was provided.
    """
    for label, s, e in [("A", period_a_start, period_a_end), ("B", period_b_start, period_b_end)]:
        err = _validate_dates(s, e)
        if err:
            return {"error": f"period {label}: {err}"}

    a = get_cost_summary(period_a_start, period_a_end, "MONTHLY", group_by, metric)
    if "error" in a:
        return {"error": f"Period A: {a['error']}"}
    b = get_cost_summary(period_b_start, period_b_end, "MONTHLY", group_by, metric)
    if "error" in b:
        return {"error": f"Period B: {b['error']}"}

    total_a = a["grand_total"]
    total_b = b["grand_total"]
    delta_abs = round(total_b - total_a, 2)
    delta_pct = round(((total_b - total_a) / total_a) * 100, 2) if total_a else None

    # Same-length warning
    days_a = (datetime.strptime(period_a_end, "%Y-%m-%d") - datetime.strptime(period_a_start, "%Y-%m-%d")).days
    days_b = (datetime.strptime(period_b_end, "%Y-%m-%d") - datetime.strptime(period_b_start, "%Y-%m-%d")).days
    warning = None
    if days_a != days_b:
        warning = (
            f"Period A is {days_a} days, period B is {days_b} days — "
            "absolute deltas may be misleading. Consider equal-length windows."
        )

    result = {
        "metric": metric,
        "currency": a["currency"],
        "period_a": {"start": period_a_start, "end": period_a_end, "total": total_a},
        "period_b": {"start": period_b_start, "end": period_b_end, "total": total_b},
        "delta_absolute": delta_abs,
        "delta_percent": delta_pct,
    }
    if warning:
        result["warning"] = warning

    # Per-key delta when grouped
    if group_by:
        a_breakdown = {e["key"]: e["amount"] for p in a["periods"] for e in p.get("breakdown", [])}
        b_breakdown = {e["key"]: e["amount"] for p in b["periods"] for e in p.get("breakdown", [])}
        all_keys = set(a_breakdown) | set(b_breakdown)
        rows = []
        for k in all_keys:
            va = a_breakdown.get(k, 0.0)
            vb = b_breakdown.get(k, 0.0)
            d_abs = round(vb - va, 2)
            d_pct = round((d_abs / va) * 100, 2) if va else None
            rows.append(
                {
                    "key": k,
                    "period_a": va,
                    "period_b": vb,
                    "delta_absolute": d_abs,
                    "delta_percent": d_pct,
                }
            )
        rows.sort(key=lambda r: abs(r["delta_absolute"]), reverse=True)
        result["breakdown"] = rows

    return result


# ---------------------------------------------------------------------------
# Tool: top cost drivers
# ---------------------------------------------------------------------------

def get_top_cost_drivers(
    start_date: str,
    end_date: str,
    group_by: str = "SERVICE",
    top_n: int = 10,
    metric: str = "UnblendedCost",
) -> dict:
    """
    Top N cost contributors in a period, with their share of total spend.

    Args:
        start_date, end_date: window (end exclusive).
        group_by: dimension to rank by. Default "SERVICE". Also useful:
                  "LINKED_ACCOUNT" (which account spends most),
                  "USAGE_TYPE" (which kind of usage drives cost).
        top_n: how many top entries to return (default 10, max 50).
        metric: cost metric.

    Returns:
        dict with ranked list including absolute amount and percentage of total.
    """
    top_n = max(1, min(int(top_n), 50))
    base = get_cost_summary(start_date, end_date, "MONTHLY", group_by, metric)
    if "error" in base:
        return base

    # Aggregate across periods (in case multiple months)
    agg: dict[str, float] = {}
    for p in base["periods"]:
        for e in p.get("breakdown", []):
            agg[e["key"]] = agg.get(e["key"], 0.0) + e["amount"]

    total = sum(agg.values())
    ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    rows = [
        {
            "rank": i + 1,
            "key": k,
            "amount": round(v, 2),
            "percent_of_total": round((v / total) * 100, 2) if total else 0.0,
        }
        for i, (k, v) in enumerate(ranked)
    ]
    return {
        "metric": metric,
        "currency": base["currency"],
        "group_by": group_by,
        "period": {"start": start_date, "end": end_date},
        "total": round(total, 2),
        "top": rows,
    }


# ---------------------------------------------------------------------------
# Tool: forecast
# ---------------------------------------------------------------------------

def forecast_costs(
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
    metric: str = "UNBLENDED_COST",
) -> dict:
    """
    Forecast future AWS spend using Cost Explorer's built-in model.

    Note: Cost Explorer's forecast metric names use underscores
    ("UNBLENDED_COST" — not "UnblendedCost" as in get_cost_and_usage).
    The other valid forecast metrics are AMORTIZED_COST, BLENDED_COST,
    NET_UNBLENDED_COST, NET_AMORTIZED_COST, USAGE_QUANTITY, NORMALIZED_USAGE_AMOUNT.

    Args:
        start_date: first day of the forecast window (must be in the future
                    or today). Format YYYY-MM-DD.
        end_date: end of forecast window (exclusive).
        granularity: "DAILY" or "MONTHLY".
        metric: see note above.

    Returns:
        dict with forecasted amount, prediction interval, and per-period details.
    """
    err = _validate_dates(start_date, end_date)
    if err:
        return {"error": err}
    today = date.today()
    if datetime.strptime(start_date, "%Y-%m-%d").date() < today:
        return {"error": "Forecast start_date must be today or in the future."}

    g = granularity.upper()
    if g not in {"DAILY", "MONTHLY"}:
        return {"error": "Forecast granularity must be DAILY or MONTHLY."}

    try:
        resp = _client().get_cost_forecast(
            TimePeriod={"Start": start_date, "End": end_date},
            Metric=metric,
            Granularity=g,
            PredictionIntervalLevel=80,
        )
    except (ClientError, BotoCoreError) as e:
        return {"error": f"AWS Cost Explorer forecast error: {e}"}

    total = resp.get("Total", {})
    return {
        "metric": metric,
        "granularity": g,
        "period": {"start": start_date, "end": end_date},
        "forecast_total": round(float(total.get("Amount", 0)), 2),
        "currency": total.get("Unit", "USD"),
        "prediction_interval_80pct": [
            {
                "start": p["TimePeriod"]["Start"],
                "end": p["TimePeriod"]["End"],
                "mean": round(float(p["MeanValue"]), 2),
                "lower_bound": round(float(p["PredictionIntervalLowerBound"]), 2),
                "upper_bound": round(float(p["PredictionIntervalUpperBound"]), 2),
            }
            for p in resp.get("ForecastResultsByTime", [])
        ],
    }


# ---------------------------------------------------------------------------
# Tool: export CSV report
# ---------------------------------------------------------------------------

_REPORTS_DIR = Path(os.environ.get("AGENT_REPORTS_DIR", "/tmp/reports"))


def export_cost_report_csv(
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
    group_by: Optional[str] = "SERVICE",
    metric: str = "UnblendedCost",
    filename: Optional[str] = None,
) -> dict:
    """
    Generate a CSV cost report and save it to the reports directory.

    The output directory is configurable via AGENT_REPORTS_DIR (default
    /tmp/reports). Mount this path as a volume in docker-compose so the
    user can retrieve the file. The tool returns the absolute path and
    a sample of the contents (first 20 rows) so the LLM can preview
    and reference it without needing to read the full file.

    Args:
        start_date, end_date: window (end exclusive).
        granularity: DAILY/MONTHLY.
        group_by: required for a meaningful report. Default SERVICE.
        metric: cost metric.
        filename: optional filename. If omitted, generated from parameters.

    Returns:
        dict with file path, row count, and a preview, or {"error": ...}.
    """
    summary = get_cost_summary(start_date, end_date, granularity, group_by, metric)
    if "error" in summary:
        return summary

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        gb = (group_by or "total").lower().replace(":", "-")
        filename = f"cost-report_{start_date}_{end_date}_{gb}_{ts}.csv"
    if not filename.endswith(".csv"):
        filename += ".csv"
    out_path = _REPORTS_DIR / filename

    # Write CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    if group_by:
        writer.writerow(["period_start", "period_end", "key", "amount", "currency", "estimated"])
        for p in summary["periods"]:
            for e in p.get("breakdown", []):
                writer.writerow(
                    [p["start"], p["end"], e["key"], e["amount"], summary["currency"], p["estimated"]]
                )
    else:
        writer.writerow(["period_start", "period_end", "amount", "currency", "estimated"])
        for p in summary["periods"]:
            writer.writerow([p["start"], p["end"], p["total"], summary["currency"], p["estimated"]])

    csv_text = buf.getvalue()
    out_path.write_text(csv_text, encoding="utf-8")

    # Preview: first 20 lines
    lines = csv_text.splitlines()
    preview = "\n".join(lines[: min(20, len(lines))])
    row_count = max(0, len(lines) - 1)  # minus header

    return {
        "file_path": str(out_path),
        "filename": filename,
        "row_count": row_count,
        "currency": summary["currency"],
        "grand_total": summary["grand_total"],
        "preview": preview,
        "note": "File saved on the agent host. Mount the reports directory as a volume to expose it to users.",
    }
