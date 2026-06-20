"""
Measure dashboard view: query count + wall time end-to-end.

Uses Django test client to hit /dashboard/ as admin and measures
the number of DB queries and total response time.
"""
import os
import time

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')

# debug mode required for connection.queries to record
from django.conf import settings
settings.DEBUG = True

from django.test import Client
from django.db import connection, reset_queries

c = Client()
ok = c.login(username='admin', password='admin123')
print(f"admin login: {ok}")

# Warm up — first request loads templates, middleware caches
c.get('/dashboard/')

print("\n--- dashboard hits (3 runs) ---")
for i in range(3):
    reset_queries()
    t0 = time.perf_counter()
    resp = c.get('/dashboard/')
    elapsed = (time.perf_counter() - t0) * 1000
    q = len(connection.queries)
    # Sum SQL execution time reported by Django (excludes Python time)
    sql_ms = sum(float(q['time']) * 1000 for q in connection.queries)
    print(f"  run {i+1}: status={resp.status_code} | queries={q} | sql={sql_ms:.1f}ms | total={elapsed:.1f}ms")

# Print top 10 slowest queries from last run
print("\n--- slowest 5 queries (from last run) ---")
qs_sorted = sorted(connection.queries, key=lambda q: float(q['time']), reverse=True)
for q in qs_sorted[:5]:
    snippet = q['sql'][:140].replace('\n', ' ')
    print(f"  {float(q['time'])*1000:>6.1f}ms  {snippet}...")
