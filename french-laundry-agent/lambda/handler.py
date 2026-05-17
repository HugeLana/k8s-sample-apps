"""French Laundry reservation watcher.

Two run modes, selected by the EventBridge event payload:

  mode=edge     Check only `today + DAYS_AHEAD` (the date newly entering
                the rolling window). Wired to an hourly schedule so the
                fill-curve of a fresh release is visible in CloudWatch.

  mode=rolling  Check `today+1 .. today+DAYS_AHEAD-1` (already-open
                dates) once a day to pick up cancellation drops.

Notification policy:
  - SMS fires on every scan that finds availability.
  - Voice call fires only once `TARGET_DATE` has entered the rolling
    window (i.e. `today >= TARGET_DATE - DAYS_AHEAD`). Until then SMS
    only — useful for observing release behavior without being woken at
    midnight.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import boto3
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

http = urllib3.PoolManager()
sms_voice = boto3.client("pinpoint-sms-voice-v2")
ddb = boto3.client("dynamodb")

TOCK_BUSINESS = os.environ.get("TOCK_BUSINESS", "tfl")
PARTY_SIZE = int(os.environ.get("PARTY_SIZE", "2"))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60"))
ORIGINATION_NUMBER = os.environ["ORIGINATION_NUMBER"]
DESTINATION_NUMBER = os.environ["DESTINATION_NUMBER"]
VOICE_ID = os.environ.get("VOICE_ID", "Joanna")
TARGET_DATE = date.fromisoformat(os.environ["TARGET_DATE"])
DEDUP_TABLE = os.environ["DEDUP_TABLE"]
DEDUP_TTL_HOURS = int(os.environ.get("DEDUP_TTL_HOURS", "24"))

# Tock's public search endpoint hit by the in-page widget. The exact path
# and response shape change occasionally; if logs show 404/403 or zero
# slots after a known release, open exploretock.com/tfl in DevTools,
# inspect the XHR to consumer/business or consumer/search, and update
# TOCK_SEARCH_URL + extract_slots accordingly.
TOCK_SEARCH_URL = "https://www.exploretock.com/api/consumer/business/{slug}/search"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.exploretock.com/{slug}",
}


def search_tock(target_date: date) -> list[dict[str, Any]]:
    url = TOCK_SEARCH_URL.format(slug=TOCK_BUSINESS)
    params = {"date": target_date.isoformat(), "size": PARTY_SIZE}
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = headers["Referer"].format(slug=TOCK_BUSINESS)

    resp = http.request("GET", f"{url}?{urlencode(params)}", headers=headers, timeout=10.0)
    if resp.status != 200:
        logger.warning("Tock %s for %s: %s", resp.status, target_date, resp.data[:200])
        return []
    try:
        data = json.loads(resp.data.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning("Non-JSON response for %s: %s", target_date, resp.data[:200])
        return []
    return extract_slots(data, target_date)


def extract_slots(data: dict[str, Any], target_date: date) -> list[dict[str, Any]]:
    """Walk Tock's response for available time slots.

    Tock's shape is not officially documented. The walker below tries
    the common keys; verify against a live response and adjust if needed.
    """
    slots: list[dict[str, Any]] = []
    experiences = data.get("experiences") or data.get("results") or []
    for exp in experiences:
        name = exp.get("name") or exp.get("title")
        times = exp.get("times") or exp.get("availability") or exp.get("timeSlots") or []
        for t in times:
            is_available = t.get("available") or t.get("isAvailable") or t.get("status") == "AVAILABLE"
            if not is_available:
                continue
            slots.append({
                "date": target_date.isoformat(),
                "time": t.get("time") or t.get("startTime") or t.get("displayTime"),
                "experience": name,
            })
    return slots


def format_summary(slots: list[dict[str, Any]], max_dates: int = 5) -> str:
    by_date: dict[str, list[str]] = {}
    for s in slots:
        by_date.setdefault(s["date"], []).append(str(s["time"]))
    parts = []
    for d, times in sorted(by_date.items())[:max_dates]:
        parts.append(f"{d} at {', '.join(times[:3])}")
    return "; ".join(parts)


def notify_voice(summary: str) -> None:
    body = (
        '<speak>'
        '<prosody rate="medium">'
        'French Laundry availability detected. '
        f'{summary}. '
        'Open Tock dot com to book.'
        '</prosody>'
        '</speak>'
    )
    sms_voice.send_voice_message(
        DestinationPhoneNumber=DESTINATION_NUMBER,
        OriginationIdentity=ORIGINATION_NUMBER,
        MessageBody=body,
        MessageBodyTextType="SSML",
        VoiceId=VOICE_ID,
    )


def notify_sms(summary: str, mode: str) -> None:
    link = f"https://www.exploretock.com/{TOCK_BUSINESS}"
    body = f"[FL/{mode}] {summary}\n{link}"
    sms_voice.send_text_message(
        DestinationPhoneNumber=DESTINATION_NUMBER,
        OriginationIdentity=ORIGINATION_NUMBER,
        MessageBody=body[:1500],
        MessageType="TRANSACTIONAL",
    )


def voice_enabled(today: date) -> bool:
    return today >= TARGET_DATE - timedelta(days=DAYS_AHEAD)


def slot_key(slot: dict[str, Any]) -> str:
    return f"{slot['date']}|{slot['time']}|{slot.get('experience') or ''}"


def filter_new_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically dedupe via DynamoDB conditional PutItem.

    A slot survives if we have not put its key in the last
    DEDUP_TTL_HOURS hours. After TTL expiry the key is removed and the
    slot becomes notifiable again (covers cancellation re-appearance and
    daily re-alerts for persistent slots).
    """
    now = int(time.time())
    expires_at = now + DEDUP_TTL_HOURS * 3600
    new_slots: list[dict[str, Any]] = []
    for s in slots:
        key = slot_key(s)
        try:
            ddb.put_item(
                TableName=DEDUP_TABLE,
                Item={
                    "slot_key": {"S": key},
                    "expires_at": {"N": str(expires_at)},
                    "first_seen": {"N": str(now)},
                },
                ConditionExpression="attribute_not_exists(slot_key)",
            )
            new_slots.append(s)
        except ddb.exceptions.ConditionalCheckFailedException:
            continue
    return new_slots


def dates_for_mode(today: date, mode: str) -> list[date]:
    if mode == "edge":
        return [today + timedelta(days=DAYS_AHEAD)]
    if mode == "rolling":
        return [today + timedelta(days=d) for d in range(1, DAYS_AHEAD)]
    raise ValueError(f"unknown mode: {mode}")


def handler(event, context):
    mode = (event or {}).get("mode", "rolling")
    today = date.today()
    dates = dates_for_mode(today, mode)

    found: list[dict[str, Any]] = []
    for d in dates:
        try:
            slots = search_tock(d)
        except Exception:
            logger.exception("Tock query failed for %s", d)
            continue
        logger.info(json.dumps({
            "event": "scan",
            "mode": mode,
            "date": d.isoformat(),
            "slot_count": len(slots),
            "target_date": TARGET_DATE.isoformat(),
        }))
        found.extend(slots)

    if not found:
        return {"mode": mode, "available": 0, "new": 0}

    new_slots = filter_new_slots(found)
    if not new_slots:
        logger.info(json.dumps({
            "event": "notify_skipped",
            "mode": mode,
            "slot_count": len(found),
            "reason": "all_deduped",
        }))
        return {"mode": mode, "available": len(found), "new": 0}

    summary = format_summary(new_slots)
    notify_sms(summary, mode)
    called = False
    if voice_enabled(today):
        notify_voice(summary)
        called = True
    logger.info(json.dumps({
        "event": "notify",
        "mode": mode,
        "slot_count": len(found),
        "new_slot_count": len(new_slots),
        "sms": True,
        "voice": called,
    }))
    return {
        "mode": mode,
        "available": len(found),
        "new": len(new_slots),
        "voice_called": called,
    }
