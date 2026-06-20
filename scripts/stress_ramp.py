"""
Maximum-load stress test: realistic mixed workload, ramped concurrency.

Stages: 10 → 25 → 50 → 100 → 200 → 400 → 600 → 800 concurrent virtual users
Each stage runs DURATION_SEC seconds, then we summarise. We STOP the next
stage if the previous one already showed:
  - error rate > 10%       (system is degrading)
  - p99 latency > 10000 ms (users would abandon)
  - any worker crash detected in logs

Operation mix (per virtual user):
  50% pos_lookup       — GET /pos/lookup/?q=PROD-XXXX     (POS scan)
  30% pos_checkout     — POST /pos/checkout/              (sell)
  15% admin_dashboard  — GET /dashboard/                  (HQ refresh)
   5% sales_list       — GET /sales/                      (admin browse)

Output: per-stage table with RPS / p50 / p95 / p99 / err%, then a
verdict on where the ceiling lies.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import sys
import threading
import time
import urllib.parse as urlparse
from collections import Counter, defaultdict
from http.cookiejar import CookieJar
from statistics import median
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

BASE = os.environ.get('STRESS_BASE', 'http://127.0.0.1:8001')
LOGIN_URL = f'{BASE}/login/'
CHECKOUT_URL = f'{BASE}/pos/checkout/'
LOOKUP_URL = f'{BASE}/pos/lookup/'
DASHBOARD_URL = f'{BASE}/dashboard/'
SALES_URL = f'{BASE}/sales/'

ADMIN_USER = os.environ.get('STRESS_ADMIN', 'admin')
ADMIN_PASS = os.environ.get('STRESS_ADMIN_PASS', 'admin123')

DURATION_SEC = int(os.environ.get('STRESS_DURATION', '20'))
STAGES = [int(s) for s in os.environ.get(
    'STRESS_STAGES', '10,25,50,100,200,400,600,800'
).split(',')]

ERROR_THRESHOLD = 0.10
LATENCY_THRESHOLD_MS = 10_000

OP_MIX = [
    ('pos_lookup', 50),
    ('pos_checkout', 30),
    ('admin_dashboard', 15),
    ('sales_list', 5),
]

with open('/tmp/stress_config.json') as f:
    CONFIG = json.load(f)
SELLERS = CONFIG['sellers']
CODES = CONFIG['product_codes']


def make_session():
    jar = CookieJar()
    return urlrequest.build_opener(urlrequest.HTTPCookieProcessor(jar)), jar


def csrf_of(opener, url):
    req = urlrequest.Request(url, headers={'User-Agent': 'stress/1.0'})
    with opener.open(req, timeout=15) as resp:
        body = resp.read().decode('utf-8', errors='replace')
    marker = 'name="csrfmiddlewaretoken" value="'
    i = body.find(marker)
    if i == -1:
        raise RuntimeError(f'csrf not found at {url}')
    j = body.find('"', i + len(marker))
    return body[i + len(marker):j]


def login_session(username, password):
    opener, jar = make_session()
    csrf = csrf_of(opener, LOGIN_URL)
    payload = urlparse.urlencode({
        'csrfmiddlewaretoken': csrf, 'username': username, 'password': password,
    }).encode()
    req = urlrequest.Request(LOGIN_URL, data=payload, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': LOGIN_URL,
        'User-Agent': 'stress/1.0',
    })
    with opener.open(req, timeout=15) as resp:
        _ = resp.read()
    cookie_csrf = next((c.value for c in jar if c.name == 'csrftoken'), None)
    return opener, cookie_csrf


def http_call(opener, method, url, headers=None, body=None, timeout=30):
    req = urlrequest.Request(url, data=body, headers=headers or {}, method=method)
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000, None
    except HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return e.code, (time.perf_counter() - t0) * 1000, 'http_err'
    except (URLError, TimeoutError) as e:
        return 0, (time.perf_counter() - t0) * 1000, f'{type(e).__name__}'
    except Exception as e:
        return 0, (time.perf_counter() - t0) * 1000, f'{type(e).__name__}'


def op_pos_lookup(session, _rng):
    opener, _, _ = session
    q = _rng.choice(CODES)
    return http_call(opener, 'GET', f'{LOOKUP_URL}?q={q}', headers={
        'User-Agent': 'stress/1.0',
        'Accept': 'text/html',
    })


def op_pos_checkout(session, rng):
    opener, csrf, stocks = session
    line = rng.choice(stocks)
    body = json.dumps({
        'lines': [{'stock_id': line['id'], 'qty': 1, 'sale_price': line['price']}],
        'payment_method': 'cash',
    }).encode()
    return http_call(opener, 'POST', CHECKOUT_URL, headers={
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf,
        'Referer': f'{BASE}/pos/',
        'User-Agent': 'stress/1.0',
    }, body=body)


def op_admin_dashboard(session, _rng):
    opener, _, _ = session
    return http_call(opener, 'GET', DASHBOARD_URL, headers={
        'User-Agent': 'stress/1.0',
        'Accept': 'text/html',
    })


def op_sales_list(session, _rng):
    opener, _, _ = session
    return http_call(opener, 'GET', SALES_URL, headers={
        'User-Agent': 'stress/1.0',
        'Accept': 'text/html',
    })


OPS = {
    'pos_lookup': op_pos_lookup,
    'pos_checkout': op_pos_checkout,
    'admin_dashboard': op_admin_dashboard,
    'sales_list': op_sales_list,
}


def weighted_choice(rng):
    r = rng.random() * sum(w for _, w in OP_MIX)
    acc = 0
    for name, w in OP_MIX:
        acc += w
        if r <= acc:
            return name
    return OP_MIX[-1][0]


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


print(f'target: {BASE}')
print(f'workload mix: {OP_MIX}')
print(f'stages: {STAGES} concurrent | duration each: {DURATION_SEC}s')
print(f'available sellers: {len(SELLERS)} | product codes: {len(CODES)}')

# Pre-establish ALL sessions (max stage count + 1 admin)
max_users = max(STAGES)
print(f'\n[setup] logging in {max_users} virtual users ...')
setup_start = time.perf_counter()


def build_one_seller(idx):
    seller = SELLERS[idx % len(SELLERS)]
    opener, csrf = login_session(seller['username'], 'sotuvchi123')
    return opener, csrf, seller['stocks']


def build_admin():
    opener, csrf = login_session(ADMIN_USER, ADMIN_PASS)
    return opener, csrf, []


# 80% sellers, 20% admin/HQ accounts (all hit admin pages)
admin_share = int(max_users * 0.20)
seller_share = max_users - admin_share

session_pool = []
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    sellers_futs = [ex.submit(build_one_seller, i) for i in range(seller_share)]
    admin_futs = [ex.submit(build_admin) for _ in range(admin_share)]
    for f in cf.as_completed(sellers_futs + admin_futs):
        try:
            session_pool.append(f.result())
        except Exception as e:
            print(f'  session setup failed: {e}', file=sys.stderr)

print(f'  ok ({len(session_pool)} ready, {time.perf_counter() - setup_start:.1f}s)')

# Track all stage results
stage_summaries = []


def worker(session, op_filter_for_admin, rng_seed, stop_at, sink):
    rng = random.Random(rng_seed)
    is_admin = len(session[2]) == 0
    # Simulate human pacing — a kassir averages ~1 op/sec at peak (item scan
    # + cart manipulation), ~1 every 5-10s in idle moments. We jitter to
    # 0.2-2.0s between ops which represents busy-checkout pace.
    while time.perf_counter() < stop_at:
        op_name = weighted_choice(rng)
        if op_name == 'pos_checkout' and is_admin:
            op_name = 'admin_dashboard'
        elif op_name in ('admin_dashboard', 'sales_list') and not is_admin:
            op_name = 'pos_lookup'

        status, ms, err = OPS[op_name](session, rng)
        ok = status in (200, 201, 302) and err is None
        sink.append((op_name, ok, status, ms, err))
        time.sleep(0.2 + rng.random() * 1.8)


def run_stage(concurrency):
    print(f'\n=== stage: {concurrency} concurrent users for {DURATION_SEC}s ===')
    results = []
    lock = threading.Lock()

    class Sink(list):
        def append(self, item):
            with lock:
                super().append(item)
    sink = Sink()

    stop_at = time.perf_counter() + DURATION_SEC
    # Slice the pool so workers reuse sessions across stages
    sessions_in_use = session_pool[:concurrency]

    t_wall = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [
            ex.submit(worker, sessions_in_use[i], None, i * 9999, stop_at, sink)
            for i in range(concurrency)
        ]
        for f in cf.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f'  worker died: {type(e).__name__}: {e}')
    elapsed = time.perf_counter() - t_wall

    total = len(sink)
    if total == 0:
        return {'concurrency': concurrency, 'total': 0, 'rps': 0, 'err_pct': 100,
                'p50': 0, 'p95': 0, 'p99': 0, 'by_op': {}}

    ok_count = sum(1 for r in sink if r[1])
    failed = total - ok_count
    err_pct = failed / total * 100

    ok_times = sorted(r[3] for r in sink if r[1])
    p50 = percentile(ok_times, 0.5)
    p95 = percentile(ok_times, 0.95)
    p99 = percentile(ok_times, 0.99)

    by_op = defaultdict(lambda: {'count': 0, 'ok': 0, 'times': [], 'errors': Counter()})
    for op_name, ok, status, ms, err in sink:
        b = by_op[op_name]
        b['count'] += 1
        if ok:
            b['ok'] += 1
            b['times'].append(ms)
        else:
            b['errors'][f'{status}/{err}'] += 1

    op_table = {}
    for op_name, b in by_op.items():
        t = sorted(b['times'])
        op_table[op_name] = {
            'count': b['count'],
            'ok_pct': b['ok'] / b['count'] * 100,
            'p50': percentile(t, 0.5),
            'p95': percentile(t, 0.95),
            'p99': percentile(t, 0.99),
            'top_errors': b['errors'].most_common(3),
        }

    summary = {
        'concurrency': concurrency,
        'elapsed_s': elapsed,
        'total': total,
        'rps': total / elapsed,
        'err_pct': err_pct,
        'p50': p50, 'p95': p95, 'p99': p99,
        'by_op': op_table,
    }
    print(f"  total: {total}  rps: {summary['rps']:.1f}  err: {err_pct:.1f}%  "
          f"p50: {p50:.0f}ms  p95: {p95:.0f}ms  p99: {p99:.0f}ms")
    print(f"  by op:")
    for op_name, b in op_table.items():
        err_pct_op = 100 - b['ok_pct']
        print(f"    {op_name:<18} n={b['count']:>5} ok={b['ok_pct']:>5.1f}%  "
              f"p50={b['p50']:>5.0f}  p95={b['p95']:>6.0f}  p99={b['p99']:>6.0f}")
        if b['top_errors']:
            print(f"      err: {b['top_errors'][:3]}")
    return summary


for c in STAGES:
    if c > len(session_pool):
        print(f'  ! stage {c} > available sessions {len(session_pool)}, capping')
        c = len(session_pool)
    s = run_stage(c)
    stage_summaries.append(s)
    if s['err_pct'] > ERROR_THRESHOLD * 100 or s['p99'] > LATENCY_THRESHOLD_MS:
        print(f'\n  >>> STOP threshold hit at {c} users: '
              f'err={s["err_pct"]:.1f}% p99={s["p99"]:.0f}ms')
        break

print(f'\n{"="*72}')
print(f'STRESS RAMP SUMMARY')
print(f'{"="*72}')
print(f'{"Users":>6} {"RPS":>7} {"err%":>6} {"p50":>7} {"p95":>7} {"p99":>7}')
print(f'{"-"*72}')
for s in stage_summaries:
    print(f"{s['concurrency']:>6} {s['rps']:>7.1f} {s['err_pct']:>5.1f}% "
          f"{s['p50']:>6.0f}ms {s['p95']:>6.0f}ms {s['p99']:>6.0f}ms")

# Identify ceiling
healthy = [s for s in stage_summaries if s['err_pct'] < 5 and s['p99'] < 5000]
if healthy:
    top = healthy[-1]
    print(f'\nVerdict: sustained healthy ceiling = {top["concurrency"]} users, '
          f'{top["rps"]:.0f} RPS, p95 {top["p95"]:.0f}ms')
else:
    print('\nVerdict: even minimum stage degraded — system is overloaded')
