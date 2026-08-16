#!/usr/bin/env python3
"""LiteLLM daily log pusher.

Runs on a LiteLLM server. Two modes:

  Daemon mode (default, what `docker compose up -d` runs):
      Sleeps until PUSH_TIME (UTC, default 00:05), pushes yesterday's
      /spend/logs to the dashboard, repeats every day.

  One-shot mode (manual test / backfill):
      python push_logs.py 2026-08-10     # push that date once, then exit

Configuration via environment variables (in .env next to docker-compose.yml):

  LITELLM_BASE_URL  Local LiteLLM URL, e.g. http://127.0.0.1:4001
  LITELLM_API_KEY   Master/admin key for the local instance
  DASHBOARD_URL     Dashboard backend URL, e.g. https://dash.example.com
  INGEST_TOKEN      Shared secret matching the dashboard's INGEST_TOKEN
  INSTANCE_NAME     "public" or "private"
  PUSH_TIME         Optional, "HH:MM" UTC (default "00:05")

Always posts, even when the day has zero logs — the push itself is the
signal that this instance has reported for the date. Re-pushing the same
date is safe: the dashboard overwrites and reprocesses.
"""
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"[push_logs] ERROR: environment variable {name} is required", file=sys.stderr)
        sys.exit(2)
    return value


def fetch_logs(base_url: str, api_key: str, date: str, next_day: str) -> list[dict]:
    resp = requests.get(
        f"{base_url}/spend/logs",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"start_date": date, "end_date": next_day, "summarize": "false"},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        data = [data] if data else []
    return data


# Split a day's records into chunks of ~20MB serialized size. Each chunk is
# a separate request: small uploads survive slow/flaky links that stall on
# one big transfer, and memory stays bounded on both sides.
CHUNK_BYTES = 20 * 1024 * 1024


def _chunks(logs: list[dict]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for rec in logs:
        rec_size = len(json.dumps(rec))
        if current and size + rec_size > CHUNK_BYTES:
            chunks.append(current)
            current, size = [], 0
        current.append(rec)
        size += rec_size
    chunks.append(current)  # last chunk; [[]] when the day has zero logs
    return chunks


def push(dashboard_url: str, token: str, instance: str, date: str, logs: list[dict]) -> dict:
    # gzip the body: raw logs are text and compress ~10x. The dashboard
    # decompresses when Content-Encoding: gzip is set (plain JSON still works).
    # First chunk overwrites the date's data for this instance; continuation
    # chunks append; the final chunk marks the instance as reported.
    chunks = _chunks(logs)
    result: dict = {}
    for i, chunk in enumerate(chunks):
        payload = {
            "instance": instance,
            "logs": chunk,
            "append": i > 0,
            "final": i == len(chunks) - 1,
            "total_records": len(logs),
        }
        body = gzip.compress(json.dumps(payload).encode())
        resp = requests.post(
            f"{dashboard_url}/api/ingest/{date}",
            headers={
                "X-Ingest-Token": token,
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            data=body,
            timeout=(30, 300),  # 30s connect, 5min read — small chunks only
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[push_logs] Chunk {i + 1}/{len(chunks)} accepted ({len(chunk)} records)", flush=True)
    return result


def push_once(cfg: dict, date: str) -> int:
    """Fetch + push one date with retries. Returns exit code."""
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[push_logs] {datetime.now(timezone.utc).isoformat()} — instance={cfg['instance']} date={date}", flush=True)
    for attempt in range(1, 4):
        try:
            logs = fetch_logs(cfg["base_url"], cfg["api_key"], date, next_day)
            print(f"[push_logs] Fetched {len(logs)} record(s) from local LiteLLM", flush=True)
            result = push(cfg["dashboard_url"], cfg["token"], cfg["instance"], date, logs)
            print(f"[push_logs] Dashboard accepted: {result}", flush=True)
            return 0
        except Exception as e:
            print(f"[push_logs] Attempt {attempt}/3 failed: {e}", file=sys.stderr, flush=True)
            if attempt < 3:
                time.sleep(30 * attempt)
    return 1


def _seconds_until(push_time: str) -> float:
    hour, minute = (int(p) for p in push_time.split(":"))
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main() -> int:
    cfg = {
        "base_url": _require_env("LITELLM_BASE_URL").rstrip("/"),
        "api_key": _require_env("LITELLM_API_KEY"),
        "dashboard_url": _require_env("DASHBOARD_URL").rstrip("/"),
        "token": _require_env("INGEST_TOKEN"),
        "instance": _require_env("INSTANCE_NAME"),
    }
    if cfg["instance"] not in ("public", "private"):
        print('[push_logs] ERROR: INSTANCE_NAME must be "public" or "private"', file=sys.stderr)
        return 2
    push_time = os.getenv("PUSH_TIME", "00:05")

    # One-shot mode: push the given date and exit.
    if len(sys.argv) > 1:
        date = sys.argv[1]
        datetime.strptime(date, "%Y-%m-%d")  # validates format, exits on bad input
        return push_once(cfg, date)

    # Daemon mode: push yesterday (UTC) every day at PUSH_TIME.
    print(f"[push_logs] Daemon started — daily push at {push_time} UTC", flush=True)
    while True:
        time.sleep(_seconds_until(push_time))
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        push_once(cfg, yesterday)


if __name__ == "__main__":
    sys.exit(main())
