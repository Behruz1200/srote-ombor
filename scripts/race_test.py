"""
POS checkout race condition reproducer.

Spawns N concurrent checkout requests for the SAME stock item.
If select_for_update is missing, more requests will succeed than the
stock allows -> stock_count goes negative.

Setup expected:
- Dev server running on http://localhost:8000
- stock_id=562, branch=1 (Chilonzor), stock_count=20 (reset before run)
- open shift for branch 1
- user 'sotuvchi1' / 'sotuvchi123' attached to branch 1

Test design:
- 25 concurrent sellers, each tries to sell 1 unit
- Safe behavior:   exactly 20 succeed, 5 fail (insufficient stock)
- Race condition:  >20 succeed OR final stock_count < 0
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
from http.cookiejar import CookieJar
from urllib import request as urlrequest
from urllib import parse as urlparse
from urllib.error import HTTPError

BASE = "http://localhost:8000"
LOGIN_URL = f"{BASE}/login/"
CHECKOUT_URL = f"{BASE}/pos/checkout/"
POS_URL = f"{BASE}/pos/"

USERNAME = "sotuvchi1"
PASSWORD = "sotuvchi123"
STOCK_ID = 562
SALE_PRICE = 450000.0

CONCURRENT_REQUESTS = 10
QTY_PER_REQUEST = 20


def build_opener():
    jar = CookieJar()
    return urlrequest.build_opener(urlrequest.HTTPCookieProcessor(jar)), jar


def get_csrf(opener, url: str) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "race-test/1.0"})
    with opener.open(req) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    # Token sits in a hidden input
    marker = 'name="csrfmiddlewaretoken" value="'
    i = body.find(marker)
    if i == -1:
        raise RuntimeError(f"csrf token not found at {url}")
    j = body.find('"', i + len(marker))
    return body[i + len(marker) : j]


def login(opener, jar) -> str:
    """Returns csrftoken cookie value usable on subsequent POSTs."""
    csrf = get_csrf(opener, LOGIN_URL)
    payload = urlparse.urlencode(
        {"csrfmiddlewaretoken": csrf, "username": USERNAME, "password": PASSWORD}
    ).encode()
    req = urlrequest.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
            "User-Agent": "race-test/1.0",
        },
    )
    with opener.open(req) as resp:
        _ = resp.read()
    for c in jar:
        if c.name == "csrftoken":
            return c.value
    raise RuntimeError("login did not yield csrftoken")


def attempt_sale(worker_id: int) -> dict:
    opener, jar = build_opener()
    try:
        login(opener, jar)
    except Exception as e:
        return {"worker": worker_id, "phase": "login", "ok": False, "error": str(e)}

    csrf = None
    for c in jar:
        if c.name == "csrftoken":
            csrf = c.value
            break
    if not csrf:
        return {"worker": worker_id, "phase": "csrf", "ok": False, "error": "no csrftoken cookie"}

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
            "User-Agent": "race-test/1.0",
        },
    )

    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {
                "worker": worker_id,
                "phase": "checkout",
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
            data = {"raw": body[:200]}
        return {
            "worker": worker_id,
            "phase": "checkout",
            "ok": False,
            "status": e.code,
            "error": data.get("error") or data.get("raw") or str(e),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as e:
        return {
            "worker": worker_id,
            "phase": "checkout",
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def main():
    print(f"Firing {CONCURRENT_REQUESTS} concurrent checkouts at stock_id={STOCK_ID}, qty={QTY_PER_REQUEST} each")
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
        futures = [ex.submit(attempt_sale, i) for i in range(CONCURRENT_REQUESTS)]
        results = [f.result() for f in cf.as_completed(futures)]
    elapsed = time.perf_counter() - t0

    success = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    print(f"\nResults ({len(results)} total, {elapsed:.2f}s elapsed):")
    print(f"  ✅ succeeded: {len(success)}")
    print(f"  ❌ failed:    {len(failed)}")

    if success:
        times = [r["ms"] for r in success if r.get("ms")]
        if times:
            times.sort()
            p50 = times[len(times) // 2]
            p95 = times[int(len(times) * 0.95)]
            print(f"  latency: p50={p50}ms p95={p95}ms min={times[0]}ms max={times[-1]}ms")

    if failed:
        from collections import Counter

        err_counter = Counter()
        for r in failed:
            key = (r.get("phase"), r.get("status"), (r.get("error") or "")[:60])
            err_counter[key] += 1
        print("  failure breakdown:")
        for (phase, status, err), count in err_counter.most_common():
            print(f"    [{phase}|{status}] x{count}: {err}")

    print("\nReturn code 0 = test finished (interpret stock_count separately).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
