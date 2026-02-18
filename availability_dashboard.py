#!/usr/bin/env python3
"""
Availability & Queue Time SLA Dashboard
========================================
Queries Grafana/InfluxDB for model availability and P99 queue times,
flags SLA violations, and generates an interactive HTML dashboard
with diagnosis suggestions.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from jinja2 import Template

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

GRAFANA_TOKEN = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN")
GRAFANA_URL = os.getenv("GRAFANA_WORKSPACE_URL", "").rstrip("/")
INFLUX_DATASOURCE_UID = "edvph6t110hz4a"

# Time window for SLA evaluation (default: last 24 hours)
LOOKBACK = os.getenv("SLA_LOOKBACK", "24h")

# SLA thresholds
SLA_AVAILABILITY_PCT = 99.9
SLA_P99_QUEUE_TIME = {
    1: 0.5,    # 500 ms
    2: 1.5,    # 1.5 s
    3: 2.0,    # 2 s
    4: 5.0,    # 5 s
}

# How many models to query in parallel
MAX_WORKERS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grafana query helper
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {GRAFANA_TOKEN}",
    "Content-Type": "application/json",
})

QUERY_ENDPOINT = f"{GRAFANA_URL}/api/ds/query"


def grafana_query(flux_query: str, time_from: str = f"now-{LOOKBACK}",
                  time_to: str = "now", timeout: int = 180) -> dict:
    """Execute a Flux query via the Grafana datasource proxy and return raw JSON."""
    payload = {
        "queries": [{
            "refId": "A",
            "datasource": {"type": "influxdb", "uid": INFLUX_DATASOURCE_UID},
            "query": flux_query,
            "maxDataPoints": 1000,
        }],
        "from": time_from,
        "to": time_to,
    }
    resp = SESSION.post(QUERY_ENDPOINT, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data‑fetching functions
# ---------------------------------------------------------------------------

def fetch_model_list() -> list[str]:
    """Return a sorted list of all model names."""
    flux = """
import "influxdata/influxdb/schema"

schema.tagValues(
  bucket: "inference",
  tag: "model",
  start: v.timeRangeStart,
  predicate: (r) => r._measurement == "api_server_prompt",
)
"""
    data = grafana_query(flux, time_from=f"now-{LOOKBACK}")
    models = []
    for frame in data.get("results", {}).get("A", {}).get("frames", []):
        for values in frame.get("data", {}).get("values", []):
            models.extend([v for v in values if isinstance(v, str)])
    return sorted(set(models))


def fetch_availability(model: str) -> float | None:
    """
    Return availability percentage for *model* over the lookback window.
    Mirrors the exact Flux logic from Grafana panel 1 of the SLO dashboard.
    """
    flux = f"""
import "join"
import "array"

total = from(bucket: "inference_slo")
|> range(start: v.timeRangeStart, stop: v.timeRangeStop)
|> filter(fn: (r) =>
  r["_measurement"] == "api_gateway" and
  r["_field"] == "request_id" and
  r["model"] == "{model}"
)
|> group()
|> aggregateWindow(every: 1m, fn: sum, createEmpty: false)
|> rename(columns: {{_value: "Requests"}})

errors_q = from(bucket: "inference_slo")
|> range(start: v.timeRangeStart, stop: v.timeRangeStop)
|> filter(fn: (r) =>
  r["_measurement"] == "api_gateway_exception" and
  r["_field"] == "status_code" and
  r["model"] == "{model}"
)
|> group()
|> aggregateWindow(every: 1m, fn: sum, createEmpty: false)
|> rename(columns: {{_value: "Errors"}})

errors_t = array.from(rows:[{{_time: v.timeRangeStop, Errors:0}}])
errors = union(tables:[errors_t, errors_q])

joined =
join.left(
    left: errors,
    right: total,
    on: (l,r) => l._time == r._time,
    as: (l,r) => ({{
      l with
      Requests: if exists r.Requests then r.Requests else 0,
    }})
  )
|> keep(columns: ["Errors", "Requests", "_time"])

window_seconds = int(v: v.timeRangeStop) / 1000000000 - int(v: v.timeRangeStart) / 1000000000
total_minutes = window_seconds / 60

j = joined
|> reduce(
    identity: {{total_downtime_minutes: 0}},
    fn: (r, accumulator) => ({{
      total_downtime_minutes: accumulator.total_downtime_minutes + (
          if ( (r.Requests+r.Errors) > 5 ) and ( float(v: r.Errors) / float(v: r.Requests+r.Errors) >= 0.05 ) then
              1
          else
              0
          ),
    }}))

j
|> map(fn: (r) => ({{
      _time: now(),
      uptime: float(v: total_minutes - r.total_downtime_minutes) / float(v: total_minutes) * 100.0
  }}))
"""
    try:
        data = grafana_query(flux)
        for frame in data.get("results", {}).get("A", {}).get("frames", []):
            vals = frame.get("data", {}).get("values", [])
            if len(vals) >= 2 and vals[1]:
                return vals[1][0]
    except Exception as exc:
        log.warning("Availability query failed for %s: %s", model, exc)
    return None


def fetch_p99_queue_times(model: str) -> dict[int, float]:
    """
    Return {priority_tier: p99_queue_time_seconds} for *model*.
    Mirrors Grafana panel 59 of the Request Prioritization dashboard.
    """
    flux = f"""
before_quantile = from(bucket: "inference")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) =>
    r["_measurement"] == "api_server_prompt" and
    r["model"] == "{model}" and
    (r["_field"] == "queue_time" or r["_field"] == "priority_tier"))
  |> group(columns: ["model","hostname", "_field"])
  |> pivot(
      rowKey:["_time"],
      columnKey: ["_field"],
      valueColumn: "_value"
  )
  |> filter(fn: (r) => r.priority_tier > -1, onEmpty: "drop")
  |> group(columns: ["priority_tier"])
  |> rename(columns: {{ queue_time: "_value"}})
  |> aggregateWindow(every: 1m, fn: mean)
  |> keep(columns: ["priority_tier", "_value"])

percentile_99 = before_quantile
  |> quantile(q: 0.99)
  |> rename(columns: {{_value: "p99_queue_time"}})
  |> yield()
"""
    result: dict[int, float] = {}
    try:
        data = grafana_query(flux, time_from=f"now-{LOOKBACK}")
        for frame in data.get("results", {}).get("A", {}).get("frames", []):
            schema = frame.get("schema", {})
            vals = frame.get("data", {}).get("values", [])
            # Extract priority_tier from field labels
            for field in schema.get("fields", []):
                labels = field.get("labels", {})
                if "priority_tier" in labels and vals:
                    tier = int(labels["priority_tier"])
                    # The p99 value is in the last values array
                    for v_list in vals:
                        if v_list and isinstance(v_list[0], (int, float)):
                            result[tier] = v_list[0]
    except Exception as exc:
        log.warning("P99 queue time query failed for %s: %s", model, exc)
    return result


# ---------------------------------------------------------------------------
# Additional diagnostic data fetching
# ---------------------------------------------------------------------------

def fetch_error_rate_timeseries(model: str) -> list[dict]:
    """Return minute-by-minute error counts for diagnosis context."""
    flux = f"""
from(bucket: "inference_slo")
|> range(start: v.timeRangeStart, stop: v.timeRangeStop)
|> filter(fn: (r) =>
  r["_measurement"] == "api_gateway_exception" and
  r["_field"] == "status_code" and
  r["model"] == "{model}"
)
|> group()
|> aggregateWindow(every: 10m, fn: sum, createEmpty: false)
|> keep(columns: ["_time", "_value"])
|> sort(columns: ["_time"])
"""
    try:
        data = grafana_query(flux)
        rows = []
        for frame in data.get("results", {}).get("A", {}).get("frames", []):
            vals = frame.get("data", {}).get("values", [])
            if len(vals) >= 2:
                for t, v in zip(vals[0], vals[1]):
                    rows.append({"time": t, "errors": v})
        return rows
    except Exception:
        return []


def fetch_request_volume(model: str) -> list[dict]:
    """Return minute-by-minute request counts."""
    flux = f"""
from(bucket: "inference_slo")
|> range(start: v.timeRangeStart, stop: v.timeRangeStop)
|> filter(fn: (r) =>
  r["_measurement"] == "api_gateway" and
  r["_field"] == "request_id" and
  r["model"] == "{model}"
)
|> group()
|> aggregateWindow(every: 10m, fn: sum, createEmpty: false)
|> keep(columns: ["_time", "_value"])
|> sort(columns: ["_time"])
"""
    try:
        data = grafana_query(flux)
        rows = []
        for frame in data.get("results", {}).get("A", {}).get("frames", []):
            vals = frame.get("data", {}).get("values", [])
            if len(vals) >= 2:
                for t, v in zip(vals[0], vals[1]):
                    rows.append({"time": t, "requests": v})
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Diagnosis engine
# ---------------------------------------------------------------------------

def diagnose_violations(model: str, availability: float | None,
                        p99_times: dict[int, float],
                        error_ts: list[dict],
                        request_ts: list[dict]) -> list[dict]:
    """
    Generate possible root‑cause diagnoses for each SLA violation.
    Returns a list of {category, description, severity} dicts.
    """
    diagnoses: list[dict] = []

    # --- Availability violations ---
    if availability is not None and availability < SLA_AVAILABILITY_PCT:
        gap = SLA_AVAILABILITY_PCT - availability

        # Check if errors are bursty (concentrated) or spread out
        if error_ts:
            error_vals = [e["errors"] for e in error_ts]
            max_err = max(error_vals)
            avg_err = sum(error_vals) / len(error_vals) if error_vals else 0
            burstiness = max_err / avg_err if avg_err > 0 else 0

            if burstiness > 5:
                diagnoses.append({
                    "category": "Availability – Bursty Errors",
                    "description": (
                        f"Errors are highly concentrated (peak={max_err:.0f}, "
                        f"avg={avg_err:.1f}). This pattern suggests a transient "
                        "incident (deployment, node failure, upstream outage) "
                        "rather than a chronic issue."
                    ),
                    "severity": "high" if gap > 0.5 else "medium",
                })
            else:
                diagnoses.append({
                    "category": "Availability – Sustained Error Rate",
                    "description": (
                        f"Errors are relatively steady (peak={max_err:.0f}, "
                        f"avg={avg_err:.1f}). This may indicate a persistent "
                        "configuration issue, resource constraint, or a bug "
                        "in the model serving path."
                    ),
                    "severity": "high" if gap > 0.5 else "medium",
                })

        # Check traffic patterns
        if request_ts:
            req_vals = [r["requests"] for r in request_ts]
            max_req = max(req_vals)
            avg_req = sum(req_vals) / len(req_vals) if req_vals else 0
            if max_req > 3 * avg_req:
                diagnoses.append({
                    "category": "Availability – Traffic Spike",
                    "description": (
                        f"Traffic peaked at {max_req:.0f} reqs/10min vs "
                        f"avg {avg_req:.0f}. A sudden spike may overwhelm "
                        "capacity, causing 5xx errors and downtime minutes."
                    ),
                    "severity": "medium",
                })

        if gap > 1.0:
            diagnoses.append({
                "category": "Availability – Major Gap",
                "description": (
                    f"Availability is {gap:.2f}% below SLA ({availability:.2f}% "
                    f"vs {SLA_AVAILABILITY_PCT}%). Recommend reviewing incident "
                    "reports, recent deployments, and cluster health."
                ),
                "severity": "critical",
            })
        elif availability is not None and availability < SLA_AVAILABILITY_PCT:
            diagnoses.append({
                "category": "Availability – Below SLA",
                "description": (
                    f"Availability is {gap:.3f}% below SLA ({availability:.3f}% "
                    f"vs {SLA_AVAILABILITY_PCT}%). Check for intermittent "
                    "5xx errors or short-lived node issues."
                ),
                "severity": "medium",
            })

    # --- P99 queue time violations ---
    for tier, sla_limit in SLA_P99_QUEUE_TIME.items():
        actual = p99_times.get(tier)
        if actual is not None and actual > sla_limit:
            ratio = actual / sla_limit
            over_by = actual - sla_limit

            diag = {
                "category": f"P99 Queue Time – Priority Tier {tier}",
                "severity": "critical" if ratio > 2 else ("high" if ratio > 1.5 else "medium"),
                "description": "",
            }

            reasons = []
            if ratio > 3:
                reasons.append(
                    f"P99 queue time is {ratio:.1f}× the SLA limit "
                    f"({actual:.3f}s vs {sla_limit}s). "
                    "This suggests severe resource starvation or scheduling "
                    "delays — investigate cluster GPU utilization, pending "
                    "request queue depth, and whether lower‑priority traffic "
                    "is crowding out tier {tier}."
                )
            elif ratio > 1.5:
                reasons.append(
                    f"P99 queue time exceeds SLA by {over_by:.3f}s "
                    f"({actual:.3f}s vs {sla_limit}s). "
                    "Possible causes: capacity is borderline for current "
                    "request volume, batch scheduling is sub‑optimal, or "
                    "a few long‑running requests are blocking the queue."
                )
            else:
                reasons.append(
                    f"P99 queue time is slightly over SLA by {over_by:.3f}s "
                    f"({actual:.3f}s vs {sla_limit}s). "
                    "Monitor closely — may be caused by occasional traffic "
                    "micro‑bursts or warm‑up effects."
                )

            # Cross‑correlate with availability
            if availability is not None and availability < SLA_AVAILABILITY_PCT:
                reasons.append(
                    "Note: this model also has availability violations, "
                    "which may share the same root cause (e.g., overloaded "
                    "cluster, failing nodes)."
                )

            diag["description"] = " ".join(reasons)
            diagnoses.append(diag)

    return diagnoses


# ---------------------------------------------------------------------------
# Main data collection
# ---------------------------------------------------------------------------

def collect_all_model_data() -> list[dict]:
    """Fetch data for every model and return a list of model report dicts."""
    log.info("Fetching model list …")
    models = fetch_model_list()
    log.info("Found %d models", len(models))

    results: list[dict] = []

    def process_model(model: str) -> dict | None:
        log.info("  → querying %s", model)
        avail = fetch_availability(model)
        p99 = fetch_p99_queue_times(model)

        # Determine whether any SLA is violated
        avail_violated = avail is not None and avail < SLA_AVAILABILITY_PCT
        queue_violated = any(
            p99.get(tier, 0) > limit
            for tier, limit in SLA_P99_QUEUE_TIME.items()
        )

        if not avail_violated and not queue_violated:
            return None  # No violations — skip

        # Fetch extra diagnostic data for violated models
        error_ts = fetch_error_rate_timeseries(model)
        request_ts = fetch_request_volume(model)
        diagnoses = diagnose_violations(model, avail, p99, error_ts, request_ts)

        violations = []
        if avail_violated:
            violations.append({
                "metric": "Availability",
                "actual": f"{avail:.4f}%",
                "expected": f"≥ {SLA_AVAILABILITY_PCT}%",
                "delta": f"-{SLA_AVAILABILITY_PCT - avail:.4f}%",
            })
        for tier, limit in sorted(SLA_P99_QUEUE_TIME.items()):
            actual_val = p99.get(tier)
            if actual_val is not None and actual_val > limit:
                violations.append({
                    "metric": f"P99 Queue Time (Tier {tier})",
                    "actual": f"{actual_val:.4f} s" if actual_val < 10 else f"{actual_val:.2f} s",
                    "expected": f"< {limit} s",
                    "delta": f"+{actual_val - limit:.4f} s",
                })

        return {
            "model": model,
            "availability": avail,
            "p99_queue_times": {str(k): v for k, v in p99.items()},
            "violations": violations,
            "diagnoses": diagnoses,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_model, m): m for m in models}
        for future in as_completed(futures):
            model_name = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as exc:
                log.error("Error processing %s: %s", model_name, exc)

    # Sort by highest criticality first, then by violation count, then alphabetically
    severity_order = {"critical": 0, "high": 1, "medium": 2}
    def sort_key(r):
        worst = min((severity_order.get(d["severity"], 99) for d in r["diagnoses"]), default=99)
        return (worst, -len(r["violations"]), r["model"])
    results.sort(key=sort_key)
    return results


# ---------------------------------------------------------------------------
# HTML dashboard generation
# ---------------------------------------------------------------------------

HTML_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Availability &amp; Queue Time SLA Dashboard</title>
<style>
  :root {
    --bg: #0e1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --red: #f85149;
    --orange: #d29922;
    --green: #3fb950;
    --critical-bg: rgba(248,81,73,0.12);
    --high-bg: rgba(210,153,34,0.12);
    --medium-bg: rgba(56,139,253,0.08);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
                 Ubuntu, Cantarell, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
  }
  h1 {
    font-size: 1.8rem;
    margin-bottom: 0.25rem;
    color: #fff;
  }
  .subtitle {
    color: var(--text-muted);
    margin-bottom: 2rem;
    font-size: 0.95rem;
  }
  .summary-bar {
    display: flex;
    gap: 1.5rem;
    margin-bottom: 2rem;
    flex-wrap: wrap;
  }
  .summary-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem 1.5rem;
    min-width: 180px;
  }
  .summary-card .label { color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .summary-card .value { font-size: 1.8rem; font-weight: 700; }
  .summary-card .value.bad { color: var(--red); }
  .summary-card .value.ok { color: var(--green); }

  .model-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 1.5rem;
    overflow: hidden;
  }
  .model-header {
    padding: 1rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    user-select: none;
  }
  .model-header:hover { background: rgba(255,255,255,0.03); }
  .model-name {
    font-size: 1.15rem;
    font-weight: 600;
    color: #fff;
  }
  .badge {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-left: 0.5rem;
  }
  .badge.critical { background: var(--red); color: #fff; }
  .badge.high { background: var(--orange); color: #000; }
  .badge.medium { background: var(--accent); color: #000; }
  .violation-count {
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .chevron {
    transition: transform 0.2s;
    color: var(--text-muted);
  }
  .model-panel.open .chevron { transform: rotate(180deg); }

  .model-body {
    display: none;
    padding: 0 1.5rem 1.5rem;
  }
  .model-panel.open .model-body { display: block; }

  /* Violations table */
  table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 1.5rem;
    font-size: 0.9rem;
  }
  th {
    text-align: left;
    padding: 0.6rem 0.8rem;
    border-bottom: 2px solid var(--border);
    color: var(--text-muted);
    font-weight: 600;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  td {
    padding: 0.6rem 0.8rem;
    border-bottom: 1px solid var(--border);
  }
  td.actual { color: var(--red); font-weight: 600; }
  td.expected { color: var(--green); }
  td.delta { color: var(--orange); font-weight: 600; }

  /* Diagnosis cards */
  .diag-section-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.75rem;
  }
  .diag-card {
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-bottom: 0.75rem;
    border-left: 4px solid;
  }
  .diag-card.critical { background: var(--critical-bg); border-color: var(--red); }
  .diag-card.high { background: var(--high-bg); border-color: var(--orange); }
  .diag-card.medium { background: var(--medium-bg); border-color: var(--accent); }
  .diag-card .diag-title {
    font-weight: 600;
    font-size: 0.95rem;
    margin-bottom: 0.35rem;
  }
  .diag-card .diag-desc {
    color: var(--text-muted);
    font-size: 0.88rem;
    line-height: 1.55;
  }

  .no-violations {
    text-align: center;
    padding: 4rem 2rem;
    color: var(--green);
    font-size: 1.2rem;
  }
  .no-violations svg { margin-bottom: 1rem; }

  .footer {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
  }

  @media (max-width: 600px) {
    body { padding: 1rem; }
    .summary-bar { flex-direction: column; }
  }
</style>
</head>
<body>

<h1>🚨 Availability &amp; Queue Time SLA Dashboard</h1>
<p class="subtitle">
  Window: last {{ lookback }} &nbsp;|&nbsp; Generated: {{ generated_at }} UTC
  &nbsp;|&nbsp; Models scanned: {{ total_models }}
</p>

<!-- Summary -->
<div class="summary-bar">
  <div class="summary-card">
    <div class="label">Models Violating SLA</div>
    <div class="value {% if violating_count > 0 %}bad{% else %}ok{% endif %}">
      {{ violating_count }}
    </div>
  </div>
  <div class="summary-card">
    <div class="label">Total Violations</div>
    <div class="value {% if total_violations > 0 %}bad{% else %}ok{% endif %}">
      {{ total_violations }}
    </div>
  </div>
  <div class="summary-card">
    <div class="label">Critical Diagnoses</div>
    <div class="value {% if critical_count > 0 %}bad{% else %}ok{% endif %}">
      {{ critical_count }}
    </div>
  </div>
  <div class="summary-card">
    <div class="label">Models Within SLA</div>
    <div class="value ok">{{ total_models - violating_count }}</div>
  </div>
</div>

{% if models %}
{% for m in models %}
<div class="model-panel" id="panel-{{ loop.index }}">
  <div class="model-header" onclick="this.parentElement.classList.toggle('open')">
    <div>
      <span class="model-name">{{ m.model }}</span>
      {% set max_sev = m.diagnoses | map(attribute='severity') | list %}
      {% if 'critical' in max_sev %}
        <span class="badge critical">CRITICAL</span>
      {% elif 'high' in max_sev %}
        <span class="badge high">HIGH</span>
      {% else %}
        <span class="badge medium">MEDIUM</span>
      {% endif %}
      <span class="violation-count">&nbsp;{{ m.violations | length }} violation{{ 's' if m.violations | length != 1 else '' }}</span>
    </div>
    <span class="chevron">▼</span>
  </div>
  <div class="model-body">
    <!-- Violations table -->
    <table>
      <thead>
        <tr>
          <th>Metric</th>
          <th>Actual</th>
          <th>SLA Requirement</th>
          <th>Delta</th>
        </tr>
      </thead>
      <tbody>
        {% for v in m.violations %}
        <tr>
          <td>{{ v.metric }}</td>
          <td class="actual">{{ v.actual }}</td>
          <td class="expected">{{ v.expected }}</td>
          <td class="delta">{{ v.delta }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>

    <!-- Diagnosis -->
    {% if m.diagnoses %}
    <div class="diag-section-title">Possible Diagnosis / Root Causes</div>
    {% for d in m.diagnoses %}
    <div class="diag-card {{ d.severity }}">
      <div class="diag-title">{{ d.category }}</div>
      <div class="diag-desc">{{ d.description }}</div>
    </div>
    {% endfor %}
    {% endif %}
  </div>
</div>
{% endfor %}
{% else %}
<div class="no-violations">
  <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
  <div>All models are within SLA — no violations detected.</div>
</div>
{% endif %}

<div class="footer">
  Data sourced from Grafana (InfluxDB) &nbsp;|&nbsp;
  SLA: Availability ≥ {{ sla_availability }}% &nbsp;|&nbsp;
  P99 Queue: T1 &lt; 500ms, T2 &lt; 1.5s, T3 &lt; 2s, T4 &lt; 5s
</div>

<script>
// Auto-open first panel
document.addEventListener('DOMContentLoaded', () => {
  const first = document.querySelector('.model-panel');
  if (first) first.classList.add('open');
});
</script>
</body>
</html>
""")


def generate_html(model_reports: list[dict], total_models: int) -> str:
    """Render the HTML dashboard from collected data."""
    total_violations = sum(len(m["violations"]) for m in model_reports)
    critical_count = sum(
        1 for m in model_reports
        for d in m["diagnoses"]
        if d["severity"] == "critical"
    )
    return HTML_TEMPLATE.render(
        models=model_reports,
        lookback=LOOKBACK,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        total_models=total_models,
        violating_count=len(model_reports),
        total_violations=total_violations,
        critical_count=critical_count,
        sla_availability=SLA_AVAILABILITY_PCT,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not GRAFANA_TOKEN:
        log.error("GRAFANA_SERVICE_ACCOUNT_TOKEN is not set in .env")
        sys.exit(1)
    if not GRAFANA_URL:
        log.error("GRAFANA_WORKSPACE_URL is not set in .env")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Availability & Queue Time SLA Dashboard")
    log.info("Lookback window: %s", LOOKBACK)
    log.info("=" * 60)

    # 1. Fetch all model list first
    all_models = fetch_model_list()
    total_models = len(all_models)
    log.info("Total models discovered: %d", total_models)

    # 2. Process each model (parallel)
    model_reports: list[dict] = []

    def process_model(model: str) -> dict | None:
        log.info("  → querying %s …", model)
        avail = fetch_availability(model)
        p99 = fetch_p99_queue_times(model)

        avail_violated = avail is not None and avail < SLA_AVAILABILITY_PCT
        queue_violated = any(
            p99.get(tier, 0) > limit
            for tier, limit in SLA_P99_QUEUE_TIME.items()
        )

        if not avail_violated and not queue_violated:
            log.info("    ✓ %s within SLA (avail=%.3f%%)", model,
                     avail if avail else 0)
            return None

        log.info("    ✗ %s has violations — fetching diagnostics …", model)

        error_ts = fetch_error_rate_timeseries(model)
        request_ts = fetch_request_volume(model)
        diagnoses = diagnose_violations(model, avail, p99, error_ts, request_ts)

        violations = []
        if avail_violated:
            violations.append({
                "metric": "Availability",
                "actual": f"{avail:.4f}%",
                "expected": f"≥ {SLA_AVAILABILITY_PCT}%",
                "delta": f"-{SLA_AVAILABILITY_PCT - avail:.4f}%",
            })
        for tier, limit in sorted(SLA_P99_QUEUE_TIME.items()):
            actual_val = p99.get(tier)
            if actual_val is not None and actual_val > limit:
                violations.append({
                    "metric": f"P99 Queue Time (Tier {tier})",
                    "actual": f"{actual_val:.4f} s" if actual_val < 10 else f"{actual_val:.2f} s",
                    "expected": f"< {limit} s",
                    "delta": f"+{actual_val - limit:.4f} s",
                })

        return {
            "model": model,
            "availability": avail,
            "p99_queue_times": {str(k): v for k, v in p99.items()},
            "violations": violations,
            "diagnoses": diagnoses,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_model, m): m for m in all_models}
        for future in as_completed(futures):
            model_name = futures[future]
            try:
                result = future.result()
                if result:
                    model_reports.append(result)
            except Exception as exc:
                log.error("Error processing %s: %s", model_name, exc)

    severity_order = {"critical": 0, "high": 1, "medium": 2}
    def sort_key(r):
        worst = min((severity_order.get(d["severity"], 99) for d in r["diagnoses"]), default=99)
        return (worst, -len(r["violations"]), r["model"])
    model_reports.sort(key=sort_key)

    # 3. Generate HTML
    log.info("Generating dashboard …")
    html = generate_html(model_reports, total_models)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "dashboard.html")
    with open(out_path, "w") as f:
        f.write(html)

    # Also dump raw JSON for programmatic use
    json_path = os.path.join(out_dir, "dashboard_data.json")
    with open(json_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback": LOOKBACK,
            "total_models": total_models,
            "violating_models": len(model_reports),
            "models": model_reports,
        }, f, indent=2)

    log.info("=" * 60)
    log.info("Dashboard written to: %s", out_path)
    log.info("Raw data written to:  %s", json_path)
    log.info("Models with violations: %d / %d", len(model_reports), total_models)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
