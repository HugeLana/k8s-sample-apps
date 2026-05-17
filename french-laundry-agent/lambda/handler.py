"""French Laundry reservation watcher.

Runs on a schedule (EventBridge), queries Tock for availability across a
rolling window, and — if any slot is found — places an outbound voice
call and sends an SMS via AWS End User Messaging (pinpoint-sms-voice-v2).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import boto3
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

http = urllib3.PoolManager()
sms_voice = boto3.client("pinpoint-sms-voice-v2")

TOCK_BUSINESS = os.environ.get("TOCK_BUSINESS", "tfl")
PARTY_SIZE = int(os.environ.get("PARTY_SIZE", "2"))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60"))
ORIGINATION_NUMBER = os.environ["ORIGINATION_NUMBER"]
DESTINATION_NUMBER = os.environ["DESTINATION_NUMBER"]
VOICE_ID = os.environ.get("VOICE_ID", "Joanna")

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


def notify_sms(summary: str) -> None:
    link = f"https://www.exploretock.com/{TOCK_BUSINESS}"
    body = f"French Laundry availability:\n{summary}\nBook: {link}"
    sms_voice.send_text_message(
        DestinationPhoneNumber=DESTINATION_NUMBER,
        OriginationIdentity=ORIGINATION_NUMBER,
        MessageBody=body[:1500],
        MessageType="TRANSACTIONAL",
    )


def handler(event, context):
    today = date.today()
    found: list[dict[str, Any]] = []
    for offset in range(DAYS_AHEAD + 1):
        target = today + timedelta(days=offset)
        try:
            found.extend(search_tock(target))
        except Exception:
            logger.exception("Tock query failed for %s", target)

    logger.info("Total available slots found: %d", len(found))
    if not found:
        return {"available": 0}

    summary = format_summary(found)
    notify_voice(summary)
    notify_sms(summary)
    return {"available": len(found), "slots": found[:20]}
