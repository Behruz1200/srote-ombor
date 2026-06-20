"""
Benchmark hot queries against the current schema.

Captures both:
  - Wall time (multiple runs, median)
  - SQLite query plan (EXPLAIN QUERY PLAN) — shows SEARCH vs SCAN

Usage:
  ./venv/bin/python manage.py shell < scripts/bench_queries.py
"""
import time
from datetime import timedelta
from statistics import median

from django.db import connection, reset_queries
from django.db.models import Sum, Count, F, DecimalField, ExpressionWrapper
from django.utils import timezone

from inventory.models import (
    Branch, BranchStock, Sale, SaleTransaction, Shift, Transfer,
)

RUNS = 5
results = []


def time_call(name, fn):
    """Run fn() RUNS times, return median ms + sample plan."""
    samples = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return {'name': name, 'median_ms': median(samples), 'min_ms': samples[0], 'max_ms': samples[-1]}


def explain_qs(name, qs):
    with connection.cursor() as cur:
        sql, params = qs.query.sql_with_params()
        cur.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        plan_rows = cur.fetchall()
    plan = '\n'.join('  ' + ' | '.join(str(c) for c in row) for row in plan_rows)
    return plan


today = timezone.localdate()
today_start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
today_end = today_start + timedelta(days=1)
seven_days_ago = today_start - timedelta(days=7)
branches = list(Branch.objects.filter(is_active=True))

print(f"\n{'='*72}")
print(f"BASELINE BENCHMARK — {RUNS} runs each, median ms")
print(f"Sale rows: {Sale.objects.count():,}  |  SaleTransaction: {SaleTransaction.objects.count():,}")
print(f"{'='*72}\n")


# ============== Q1: Dashboard 7-day trend ==============
def q1():
    out = []
    for i in range(7):
        ds = today_start - timedelta(days=i)
        de = ds + timedelta(days=1)
        out.append(
            Sale.objects.filter(sold_at__gte=ds, sold_at__lt=de).aggregate(
                rev=Sum(
                    ExpressionWrapper(
                        F('sale_price') * F('quantity') - F('line_discount'),
                        output_field=DecimalField(),
                    )
                ),
                qty=Sum('quantity'),
            )
        )
    return out


r = time_call('Q1 dashboard 7-day trend (7 aggregates)', q1)
results.append(r)
plan = explain_qs(
    'Q1 sample',
    Sale.objects.filter(sold_at__gte=seven_days_ago, sold_at__lt=today_end),
)
print(f"[Q1] dashboard 7-day trend (loop x7):")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}")
print(f"     plan:\n{plan}\n")


# ============== Q2: Dashboard per-branch today ==============
def q2():
    out = []
    for br in branches:
        a = Sale.objects.filter(
            branch=br, sold_at__gte=today_start, sold_at__lt=today_end
        ).aggregate(
            rev=Sum(
                ExpressionWrapper(
                    F('sale_price') * F('quantity') - F('line_discount'),
                    output_field=DecimalField(),
                )
            ),
            qty=Sum('quantity'),
        )
        out.append(a)
    return out


r = time_call(f'Q2 dashboard per-branch today (loop x{len(branches)})', q2)
results.append(r)
plan = explain_qs(
    'Q2 sample',
    Sale.objects.filter(branch=branches[0], sold_at__gte=today_start, sold_at__lt=today_end),
)
print(f"[Q2] dashboard per-branch today (loop x{len(branches)}):")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}")
print(f"     plan:\n{plan}\n")


# ============== Q3: Sales list filter by date range + branch ==============
def q3():
    return list(
        Sale.objects.filter(
            branch=branches[0],
            sold_at__gte=seven_days_ago,
            sold_at__lt=today_end,
        )
        .select_related('variant__product', 'transaction', 'sold_by')
        .order_by('-sold_at')[:300]
    )


r = time_call('Q3 sales_list 7d + branch + 300 rows', q3)
results.append(r)
plan = explain_qs(
    'Q3 sample',
    Sale.objects.filter(branch=branches[0], sold_at__gte=seven_days_ago, sold_at__lt=today_end).order_by('-sold_at')[:300],
)
print(f"[Q3] sales_list (7-day, branch filter, 300 rows):")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}")
print(f"     plan:\n{plan}\n")


# ============== Q4: Reports pivot — 30 days ==============
def q4():
    thirty = today_start - timedelta(days=30)
    return list(
        SaleTransaction.objects.filter(sold_at__gte=thirty, sold_at__lt=today_end)
        .values('branch__name', 'payment_method')
        .annotate(cnt=Count('id'), revenue=Sum('order_discount'))
        .order_by('branch__name')
    )


r = time_call('Q4 reports pivot 30d (branch x payment_method)', q4)
results.append(r)
print(f"[Q4] reports pivot 30 days:")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}\n")


# ============== Q5: Shift.objects.filter(branch, status='open') ==============
def q5():
    return [
        Shift.objects.filter(branch=br, status='open').first()
        for br in branches
    ]


r = time_call(f'Q5 open shift lookup (loop x{len(branches)})', q5)
results.append(r)
plan = explain_qs(
    'Q5 sample',
    Shift.objects.filter(branch=branches[0], status='open'),
)
print(f"[Q5] open shift lookup:")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}")
print(f"     plan:\n{plan}\n")


# ============== Q6: Transfer list — IN_TRANSIT, sorted ==============
def q6():
    cutoff = timezone.now() - timedelta(hours=4)
    return list(
        Transfer.objects.filter(status='in_transit', dispatched_at__lt=cutoff)
        .select_related('from_branch', 'to_branch')
        .order_by('dispatched_at')[:5]
    )


r = time_call('Q6 overdue transfer scan', q6)
results.append(r)
print(f"[Q6] overdue transfers (status filter):")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}\n")


# ============== Q7: pos_checkout-equivalent stock lookup ==============
sample_stock = BranchStock.objects.filter(stock_count__gte=1).first()


def q7():
    return (
        BranchStock.objects.select_related('variant__product', 'branch')
        .filter(pk=sample_stock.pk, branch=sample_stock.branch)
        .first()
    )


r = time_call('Q7 pos_checkout stock lookup (single row)', q7)
results.append(r)
print(f"[Q7] pos_checkout stock lookup:")
print(f"     median={r['median_ms']:.1f}ms  min={r['min_ms']:.1f}  max={r['max_ms']:.1f}\n")


# ============== Summary ==============
print(f"\n{'='*72}")
print(f"BASELINE SUMMARY")
print(f"{'='*72}")
print(f"{'Query':<48} {'median':>10} {'min':>8} {'max':>8}")
print(f"{'-'*72}")
for r in results:
    print(f"{r['name']:<48} {r['median_ms']:>8.1f}ms {r['min_ms']:>6.1f}ms {r['max_ms']:>6.1f}ms")
total_median = sum(r['median_ms'] for r in results)
print(f"{'-'*72}")
print(f"{'TOTAL (typical dashboard page hit)':<48} {total_median:>8.1f}ms")
print(f"\n(SCAN in plan = full table scan, SEARCH = index lookup — we want SEARCH)")
