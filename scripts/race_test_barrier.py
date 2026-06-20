"""
Maximum-pressure race condition reproducer.

Unlike race_test.py, this:
  1. Pre-establishes ALL sessions (login + csrf) BEFORE timing starts.
  2. Uses threading.Barrier so every checkout POST is fired at the SAME
     instant — eliminates login-latency-induced serialization.

Run against either SQLite or Postgres. Postgres exposes the race
condition far more readily because MVCC does not serialize naive reads.

Usage:
  ./venv/bin/python scripts/race_test_barrier.py
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import threading
import time
from http.cookiejar import CookieJar
from urllib import request as urlrequest
from urllib import parse as urlparse
from urllib.error import HTTPError

BASE = os.environ.get("RACE_BASE", "http://localhost:8000")
LOGIN_URL = f"{BASE}/login/"
CHECKOUT_URL = f"{BASE}/pos/checkout/"
POS_URL = f"{BASE}/pos/"

USERNAME = os.environ.get("RACE_USER", "sotuvchi1")
PASSWORD = os.environ.get("RACE_PASS", "sotuvchi123")
STOCK_ID = int(os.environ.get("RACE_STOCK", "562"))
SALE_PRICE = float(os.environ.get("RACE_PRICE", "450000"))

CONCURRENT_REQUESTS = int(os.environ.get("RACE_N", "10"))
QTY_PER_REQUEST = int(os.environ.get("RACE_QTY", "20"))


def build_session():
    jar = CookieJar()
    return urlrequest.build_opener(urlrequest.HTTPCookieProcessor(jar)), jar


def csrf_from_page(opener, url: str) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "race-barrier/1.0"})
    with opener.open(req) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    marker = 'name="csrfmiddlewaretoken" value="'
    i = body.find(marker)
    if i == -1:
        raise RuntimeError(f"csrf not found at {url}")
    j = body.find('"', i + len(marker))
    return body[i + len(marker) : j]


def prepare_session(worker_id: int):
    opener, jar = build_session()
    csrf = csrf_from_page(opener, LOGIN_URL)
    payload = urlparse.urlencode(
        {"csrfmiddlewaretoken": csrf, "username": USERNAME, "password": PASSWORD}
    ).encode()
    req = urlrequest.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
            "User-Agent": "race-barrier/1.0",
        },
    )
    with opener.open(req) as resp:
        _ = resp.read()
    cookie_csrf = next((c.value for c in jar if c.name == "csrftoken"), None)
    if not cookie_csrf:
        raise RuntimeError(f"worker {worker_id}: no csrftoken cookie after login")
    return opener, cookie_csrf


def fire(worker_id: int, session_pack, barrier: threading.Barrier) -> dict:
    opener, csrf = session_pack
    body = json.dumps(
        {
            "lines": [{"stock_id": STOCK_ID, "qty": QTY_PER_REQUEST, "sale_price": SALE_PRICE}],
            "payment_method": "cash",
        }
    ).encode()
    req = urlrequest.Request(
        CHECKOUT_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-CSRFToken": csrf,
            "Referer": POS_URL,
            "User-Agent": "race-barrier/1.0",
        },
    )

    # Wait for every other worker to be ready, then fire together.
    barrier.wait(timeout=30)

    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {
                "worker": worker_id,
                "ok": bool(data.get("ok")),
                "status": resp.status,
                "txn_id": data.get("txn_id"),
                "error": data.get("error"),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body[:120]}
        return {
            "worker": worker_id,
            "ok": False,
            "status": e.code,
            "error": data.get("error") or data.get("raw") or str(e),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as e:
        return {
            "worker": worker_id,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def main():
    print(
        f"target: {BASE} | stock_id={STOCK_ID} | qty={QTY_PER_REQUEST} | "
        f"workers={CONCURRENT_REQUESTS}"
    )

    print("Pre-logging in all sessions ...")
    t_login = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
        sessions = list(ex.map(prepare_session, range(CONCURRENT_REQUESTS)))
    print(f"  ok ({(time.perf_counter() - t_login):.2f}s)")

    barrier = threading.Barrier(CONCURRENT_REQUESTS)
    print(f"Firing {CONCURRENT_REQUESTS} simultaneous checkouts ...")
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
        futures = [
            ex.submit(fire, i, sessions[i], barrier) for i in range(CONCURRENT_REQUESTS)
        ]
        results = [f.result() for f in cf.as_completed(futures)]
    elapsed = time.perf_counter() - t0

    success = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    print(f"\nResults ({len(results)} total, {elapsed:.2f}s wall):")
    print(f"  ✅ succeeded: {len(success)}")
    print(f"  ❌ failed:    {len(failed)}")
    print(f"  📦 total units claimed sold: {len(success) * QTY_PER_REQUEST}")

    if success:
        times = sorted(r["ms"] for r in success if r.get("ms"))
        if times:
            p50 = times[len(times) // 2]
            p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
            print(f"  latency: p50={p50}ms p95={p95}ms min={times[0]}ms max={times[-1]}ms")

    if failed:
        from collections import Counter

        counter = Counter()
        for r in failed:
            counter[(r.get("status"), (r.get("error") or "")[:80])] += 1
        print("  failure breakdown:")
        for (status, err), count in counter.most_common():
            print(f"    [{status}] x{count}: {err}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
