"""Dashboard for the French Laundry watcher.

Invoked through a Lambda Function URL. Queries the scans table and
renders a single HTML page with:

  * Phase banner (silent collection vs. SMS+voice alerting).
  * Line chart of slot counts over time for the edge date and the
    rolling-window aggregate.
  * Recent-scan table.

If DASHBOARD_KEY is set the page requires `?key=<value>`.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from string import Template

import boto3

ddb = boto3.client("dynamodb")

SCANS_TABLE = os.environ["SCANS_TABLE"]
TARGET_DATE = date.fromisoformat(os.environ["TARGET_DATE"])
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60"))
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")

# Display only. Pacific is UTC-8 / UTC-7 depending on DST; this is a
# label, not a scheduling concern (EventBridge handles the real TZ).
PT = timezone(timedelta(hours=-8))


def handler(event, context):
    qs = (event or {}).get("queryStringParameters") or {}

    if DASHBOARD_KEY and qs.get("key") != DASHBOARD_KEY:
        return {"statusCode": 403, "headers": {"Content-Type": "text/plain"}, "body": "forbidden"}

    try:
        days = max(1, min(30, int(qs.get("days", "7"))))
    except ValueError:
        days = 7

    now = int(time.time())
    start = now - days * 86400
    scans = query_scans(start, now)
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": render(scans, days),
    }


def query_scans(start_ts: int, end_ts: int) -> list[dict]:
    items = []
    last_key = None
    while True:
        kw = {
            "TableName": SCANS_TABLE,
            "KeyConditionExpression": "pk = :p AND sk BETWEEN :s AND :e",
            "ExpressionAttributeValues": {
                ":p": {"S": "scan"},
                ":s": {"S": f"{start_ts:010d}#"},
                ":e": {"S": f"{end_ts:010d}#~"},
            },
            "ScanIndexForward": True,
        }
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        resp = ddb.query(**kw)
        items.extend(resp["Items"])
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return [
        {
            "ts": int(it["timestamp"]["N"]),
            "mode": it["mode"]["S"],
            "date": it["date_checked"]["S"],
            "count": int(it["slot_count"]["N"]),
        }
        for it in items
    ]


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=PT).strftime("%Y-%m-%d %H:%M")


TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>French Laundry watcher</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #1f2937; }
  h1 { font-weight: 500; margin-bottom: 0.2em; }
  h2 { font-weight: 500; margin-top: 2em; color: #374151; }
  .meta { color: #6b7280; font-size: 14px; margin-bottom: 1.5em; }
  .banner { padding: 14px 18px; border-radius: 8px; margin-bottom: 1.5em; line-height: 1.6; }
  .phase1 { background: #f3f4f6; }
  .phase2 { background: #fef3c7; border-left: 4px solid #f59e0b; }
  table { border-collapse: collapse; width: 100%; margin-top: 1em; font-size: 14px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #e5e7eb; }
  th { background: #f9fafb; font-weight: 500; color: #374151; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; background: #e5e7eb; font-variant-numeric: tabular-nums; }
  .pill.has { background: #d1fae5; color: #065f46; font-weight: 600; }
  canvas { max-height: 360px; }
  code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
  .filters { margin-top: 1em; font-size: 14px; }
  .filters a { margin-right: 0.7em; color: #2563eb; text-decoration: none; }
</style>
</head>
<body>
<h1>French Laundry watcher</h1>
<div class="meta">Window: last $days days &middot; Target: <code>$target_date</code> &middot; Alerts activate: <code>$voice_start</code> ($voice_msg)</div>

<div class="banner $phase_class">
<b>$phase_label</b><br>
$phase_detail
</div>

<h2>Slot counts over time (Pacific)</h2>
<canvas id="chart"></canvas>

<h2>Most recent scans</h2>
<table>
<thead><tr><th>Time (PT)</th><th>Mode</th><th>Date checked</th><th>Slots</th></tr></thead>
<tbody>$rows</tbody>
</table>

<div class="filters">Window:
  <a href="?days=1$keyq">1d</a>
  <a href="?days=7$keyq">7d</a>
  <a href="?days=14$keyq">14d</a>
  <a href="?days=30$keyq">30d</a>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
const d = $data_json;
new Chart(document.getElementById('chart'), {
  type: 'line',
  data: {
    datasets: [
      { label: 'edge (today+60)', data: d.edge, borderColor: '#dc2626', backgroundColor: '#dc262633', pointRadius: 2, tension: 0.1 },
      { label: 'rolling (sum across window)', data: d.rolling, borderColor: '#2563eb', backgroundColor: '#2563eb33', pointRadius: 2, tension: 0.1 }
    ]
  },
  options: {
    parsing: false,
    scales: {
      x: { type: 'category', ticks: { maxTicksLimit: 12 } },
      y: { beginAtZero: true, title: { display: true, text: 'slots available' } }
    },
    plugins: { legend: { position: 'bottom' } }
  }
});
</script>
</body>
</html>
""")


def render(scans: list[dict], days: int) -> str:
    today = date.today()
    voice_start_date = TARGET_DATE - timedelta(days=DAYS_AHEAD)
    phase2 = today >= voice_start_date
    delta = (voice_start_date - today).days

    if phase2:
        voice_msg = f"{-delta} day{'s' if abs(delta) != 1 else ''} ago"
        phase_label = "Phase 2 active &mdash; SMS + voice on every detection"
        phase_detail = (
            f"Reservations for {TARGET_DATE.isoformat()} are inside the "
            f"{DAYS_AHEAD}-day window. Every hourly scan that finds availability "
            f"fires SMS and a phone call."
        )
        phase_class = "phase2"
    else:
        voice_msg = f"in {delta} day{'s' if delta != 1 else ''}"
        phase_label = "Phase 1 &mdash; silent collection"
        phase_detail = (
            f"Reservations for {TARGET_DATE.isoformat()} are not yet bookable. "
            f"Dashboard records every scan; no SMS or calls are sent."
        )
        phase_class = "phase1"

    edge_points: list[dict] = []
    rolling_by_ts: dict[int, int] = {}
    for s in scans:
        if s["mode"] == "edge":
            edge_points.append({"x": fmt_ts(s["ts"]), "y": s["count"]})
        else:
            rolling_by_ts[s["ts"]] = rolling_by_ts.get(s["ts"], 0) + s["count"]
    rolling_points = [{"x": fmt_ts(ts), "y": c} for ts, c in sorted(rolling_by_ts.items())]

    rows_html: list[str] = []
    for s in sorted(scans, key=lambda x: x["ts"], reverse=True)[:80]:
        pill_cls = "pill has" if s["count"] > 0 else "pill"
        rows_html.append(
            f"<tr><td>{fmt_ts(s['ts'])}</td><td>{s['mode']}</td>"
            f"<td>{s['date']}</td>"
            f"<td><span class='{pill_cls}'>{s['count']}</span></td></tr>"
        )
    rows = "\n".join(rows_html) or "<tr><td colspan='4'>No scans recorded in this window yet.</td></tr>"

    keyq = f"&amp;key={DASHBOARD_KEY}" if DASHBOARD_KEY else ""

    return TEMPLATE.substitute(
        days=days,
        target_date=TARGET_DATE.isoformat(),
        voice_start=voice_start_date.isoformat(),
        voice_msg=voice_msg,
        phase_label=phase_label,
        phase_detail=phase_detail,
        phase_class=phase_class,
        rows=rows,
        data_json=json.dumps({"edge": edge_points, "rolling": rolling_points}),
        keyq=keyq,
    )
