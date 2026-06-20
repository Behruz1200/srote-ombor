"""
Throughput test: 20 virtual sellers, real POS workflow.

Each worker:
  1. Logs in once.
  2. Repeatedly POSTs /pos/checkout/ against a randomly chosen stock from
     a pre-seeded pool (so contention is realistic, not pathological).
  3. Runs for DURATION_SECONDS.

Reports: total ok / failed, RPS, p50/p95/p99 latency, error breakdown.

Env vars:
  RACE_BASE  (default http://localhost:8000)
  RACE_USER, RACE_PASS  (default sotuvchi1 / sotuvchi123)
  RACE_N        concurrent workers (default 20)
  RACE_SECONDS  duration (default 30)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
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
CONCURRENT = int(os.environ.get("RACE_N", "20"))
DURATION = int(os.environ.get("RACE_SECONDS", "30"))

STOCK_POOL_JSON = os.environ.get("RACE_POOL", "")


def build_session():
    jar = CookieJar()
    return urlrequest.build_opener(urlrequest.HTTPCookieProcessor(jar)), jar


def csrf_of(opener, url: str) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "throughput/1.0"})
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
    csrf = csrf_of(opener, LOGIN_URL)
    payload = urlparse.urlencode(
        {"csrfmiddlewaretoken": csrf, "username": USERNAME, "password": PASSWORD}
    ).encode()
    req = urlrequest.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
            "User-Agent": "throughput/1.0",
        },
    )
    with opener.open(req) as resp:
        _ = resp.read()
    cookie_csrf = next((c.value for c in jar if c.name == "csrftoken"), None)
    if not cookie_csrf:
        raise RuntimeError(f"worker {worker_id}: no csrftoken cookie")
    return opener, cookie_csrf


def post_checkout(opener, csrf, stock_id: int, price: float) -> dict:
    body = json.dumps(
        {
            "lines": [{"stock_id": stock_id, "qty": 1, "sale_price": price}],
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
            "User-Agent": "throughput/1.0",
        },
    )
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {
                "ok": bool(data.get("ok")),
                "status": resp.status,
                "error": data.get("error"),
                "ms": (time.perf_counter() - t0) * 1000,
            }
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body[:80]}
        return {
            "ok": False,
            "status": e.code,
            "error": data.get("error") or data.get("raw") or "http_err",
            "ms": (time.perf_counter() - t0) * 1000,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "error": f"{type(e).__name__}",
            "ms": (time.perf_counter() - t0) * 1000,
        }


def worker(worker_id: int, pool: list[dict], stop_at: float, results: list) -> None:
    opener, csrf = prepare_session(worker_id)
    local_rng = random.Random(worker_id * 1009)
    while time.perf_counter() < stop_at:
        stock = local_rng.choice(pool)
        r = post_checkout(opener, csrf, stock["id"], float(stock["sale_price"]))
        results.append(r)


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


def main():
    if not STOCK_POOL_JSON:
        print("ERROR: set RACE_POOL=<json list of {id, sale_price}> env var")
        return 1
    pool = json.loads(STOCK_POOL_JSON)
    print(f"target: {BASE}")
    print(f"workers: {CONCURRENT} | duration: {DURATION}s | pool size: {len(pool)}")

    results: list[dict] = []
    lock = threading.Lock()

    class Sink(list):
        def append(self, item):
            with lock:
                super().append(item)

    sink = Sink()

    stop_at = time.perf_counter() + DURATION
    print("Starting ...")
    t_real = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=CONCURRENT) as ex:
        futures = [ex.submit(worker, i, pool, stop_at, sink) for i in range(CONCURRENT)]
        for f in cf.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  worker died: {type(e).__name__}: {e}")
    elapsed = time.perf_counter() - t_real

    total = len(sink)
    ok = [r for r in sink if r["ok"]]
    failed = [r for r in sink if not r["ok"]]

    print(f"\n--- results ({elapsed:.1f}s wall) ---")
    print(f"  total requests: {total}")
    print(f"  ✅ success:     {len(ok)}")
    print(f"  ❌ failed:      {len(failed)}")
    print(f"  RPS:            {total / elapsed:.1f}")
    print(f"  success RPS:    {len(ok) / elapsed:.1f}")

    if ok:
        times = sorted(r["ms"] for r in ok)
        print(
            f"  success latency: p50={percentile(times, 0.5):.0f}ms "
            f"p95={percentile(times, 0.95):.0f}ms "
            f"p99={percentile(times, 0.99):.0f}ms "
            f"max={times[-1]:.0f}ms"
        )

    if failed:
        from collections import Counter

        counter = Counter()
        for r in failed:
            err = r.get("error") or ""
            if isinstance(err, str) and len(err) > 60:
                err = err[:60] + "..."
            counter[(r.get("status"), err)] += 1
        print("  failure breakdown (top 6):")
        for (status, err), count in counter.most_common(6):
            print(f"    [{status}] x{count}: {err}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
