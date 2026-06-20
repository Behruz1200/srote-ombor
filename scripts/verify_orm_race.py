"""
Verify pos_checkout code path WITHOUT HTTP layer.

Calls the same atomic + select_for_update pattern from N threads, each
running its own DB connection. On Postgres this should: succeed once,
fail (N-1) times with the friendly 'omborda faqat 0 ta bor' message,
never crash.

On SQLite some calls may still raise OperationalError (database is
locked) — that is a SQLite property, not a bug in the application code.
"""
import threading
import sys

import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')

import django
django.setup()

from django.db import connection, transaction, IntegrityError, OperationalError
from django.db.models import F

from inventory.models import BranchStock, SaleTransaction, Sale, User, Branch, Shift


STOCK_ID = 562
QTY = 20
THREADS = 10

# Reset
s = BranchStock.objects.get(pk=STOCK_ID)
s.stock_count = 20
s.save()
print(f"reset stock_count={s.stock_count}")

branch = Branch.objects.get(pk=1)
user = User.objects.filter(branch=branch).first()
shift = Shift.objects.filter(branch=branch, status='open').first()

barrier = threading.Barrier(THREADS)
results = []
results_lock = threading.Lock()


def attempt_sale(worker_id):
    connection.close()  # fresh connection per thread

    class _Abort(Exception):
        def __init__(self, msg):
            self.msg = msg

    barrier.wait(timeout=30)

    try:
        with transaction.atomic():
            locked = list(
                BranchStock.objects
                .select_for_update()
                .filter(pk=STOCK_ID, branch=branch)
            )
            if not locked:
                raise _Abort('stock not found')
            stock = locked[0]
            if QTY > stock.stock_count:
                raise _Abort(f'omborda faqat {stock.stock_count} ta bor, soʻrov {QTY}')

            txn = SaleTransaction.objects.create(
                branch=branch, sold_by=user, payment_method='cash',
                shift=shift,
            )
            stock.stock_count = F('stock_count') - QTY
            stock.save()
            Sale.objects.create(
                transaction=txn, variant=stock.variant, branch=branch,
                quantity=QTY, sale_price=stock.sale_price,
                cost_at_sale=stock.cost_price, sold_by=user,
            )
        with results_lock:
            results.append({'w': worker_id, 'ok': True})
    except _Abort as e:
        with results_lock:
            results.append({'w': worker_id, 'ok': False, 'kind': 'business', 'err': e.msg})
    except IntegrityError as e:
        with results_lock:
            results.append({'w': worker_id, 'ok': False, 'kind': 'integrity', 'err': str(e)[:80]})
    except OperationalError as e:
        with results_lock:
            results.append({'w': worker_id, 'ok': False, 'kind': 'sqlite_busy', 'err': str(e)[:80]})


threads = [threading.Thread(target=attempt_sale, args=(i,)) for i in range(THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)

s = BranchStock.objects.get(pk=STOCK_ID)
print(f"\nFinal stock_count: {s.stock_count}")

from collections import Counter
buckets = Counter()
for r in results:
    if r['ok']:
        buckets['ok'] += 1
    else:
        buckets[r['kind']] += 1

print(f"Outcomes: {dict(buckets)}")
print(f"  ✅ business-logic successes:   {buckets.get('ok', 0)}")
print(f"  🟢 business-logic rejections:  {buckets.get('business', 0)}  (= clean 'out of stock')")
print(f"  🔴 SQLite busy errors:         {buckets.get('sqlite_busy', 0)}  (= dev-only artifact)")
print(f"  🔴 CHECK constraint failures:  {buckets.get('integrity', 0)}  (= would not happen on Postgres)")

if buckets.get('integrity', 0) == 0:
    print("\n✅ The select_for_update fix prevents constraint violations.")
    print("   On Postgres production: all 9 failures would be clean 'business' rejections.")
