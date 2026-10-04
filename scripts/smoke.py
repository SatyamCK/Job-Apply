"""Phase 1 "done" check against the running stack.

1. POST /api/diagnostics/ping (emits started/completed events inside a traced request)
2. assert the events rows with that trace_id are in Postgres
3. assert Jaeger has the trace
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
import psycopg


def fail(msg: str) -> None:
    print(f"SMOKE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api-url", default=os.environ.get("SMOKE_API_URL", "http://localhost:8000")
    )
    parser.add_argument(
        "--jaeger-url", default=os.environ.get("SMOKE_JAEGER_URL", "http://localhost:16686")
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if not args.database_url:
        fail("DATABASE_URL is not set (see .env.example)")

    resp = httpx.post(f"{args.api_url}/api/diagnostics/ping", timeout=10)
    if resp.status_code != 200:
        fail(f"ping returned {resp.status_code}: {resp.text}")
    trace_id = resp.json()["trace_id"]
    if not trace_id:
        fail("ping returned no trace_id")
    print(f"ping ok, trace_id={trace_id}")

    with psycopg.connect(args.database_url) as conn:
        rows = conn.execute(
            "SELECT event_type, status FROM events WHERE trace_id = %s ORDER BY created_at",
            (trace_id,),
        ).fetchall()
    if [r[0] for r in rows] != ["started", "completed"]:
        fail(f"expected started+completed events for trace, got {rows}")
    print(f"events ok: {rows}")

    deadline = time.monotonic() + args.timeout
    while True:
        r = httpx.get(f"{args.jaeger_url}/api/traces/{trace_id}", timeout=5)
        if r.status_code == 200 and r.json().get("data"):
            spans = r.json()["data"][0]["spans"]
            names = sorted({s["operationName"] for s in spans})
            print(f"jaeger ok: {len(spans)} spans {names}")
            print(f"view: {args.jaeger_url}/trace/{trace_id}")
            break
        if time.monotonic() > deadline:
            fail(f"trace {trace_id} not found in Jaeger after {args.timeout}s")
        time.sleep(1)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
