"""Syntax-check every inline <script> on the key pages with `node --check`.

Fails with exit 1 if any inline JavaScript has a syntax error, so a broken
template-literal (e.g. bad quote-escaping) can never reach production. Wired
into deploy.sh. Skips gracefully with a warning if node isn't installed.
"""
import os
import re
import shutil
import subprocess
import tempfile
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

SCRIPT_RE = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.S | re.I)
TYPE_RE = re.compile(r'type\s*=\s*["\']([^"\']+)["\']', re.I)
JS_TYPES = ('text/javascript', 'module', 'application/javascript')


def _extract(html):
    out = []
    for m in SCRIPT_RE.finditer(html):
        attrs, body = m.group(1), m.group(2)
        if 'src=' in attrs.lower():
            continue
        tm = TYPE_RE.search(attrs)
        if tm and tm.group(1).lower() not in JS_TYPES:
            continue
        if body.strip():
            out.append(body)
    return out


class Command(BaseCommand):
    help = "node --check every inline <script> on the key pages."

    def handle(self, *args, **opts):
        node = shutil.which('node') or shutil.which('nodejs')
        if not node:
            self.stdout.write(self.style.WARNING(
                "node not found — skipping JS syntax check."))
            return

        from inventory.models import User, Branch, Shift, Product, ProductVariant
        from inventory.views import POS_BRANCH_SESSION_KEY

        made = {'user': False, 'branch': False, 'shift': False}
        u = (User.objects.filter(is_superuser=True).first()
             or User.objects.filter(role='admin').first())
        if not u:
            u = User.objects.create_superuser('jscheck_tmp', '', 'x'); made['user'] = True
        b = Branch.objects.filter(is_active=True).first()
        if not b:
            b = Branch.objects.create(name='JSCHECK_TMP', is_active=True); made['branch'] = True
        u.branch = b; u.save()
        sh = Shift.objects.filter(branch=b, status=Shift.Status.OPEN).first()
        if not sh:
            sh = Shift.objects.create(branch=b, opened_by=u, opening_cash=0,
                                      status=Shift.Status.OPEN); made['shift'] = True

        c = Client(); c.force_login(u); c.raise_request_exception = False
        sess = c.session; sess[POS_BRANCH_SESSION_KEY] = b.id; sess.save()
        anon = Client(); anon.raise_request_exception = False

        p = Product.objects.exclude(is_open_price=True).first() or Product.objects.first()
        code = p.code if p else 'PRD-0001'
        v = ProductVariant.objects.first(); vid = v.pk if v else 1

        pages = [
            '/lookup/', '/requests/', '/pos/', '/pos/display/', '/dashboard/', '/products/', '/products/new/',
            f'/products/{code}/', f'/products/{code}/edit/', f'/products/{code}/variants/edit/',
            f'/products/{code}/intake/', '/intake/', '/intake/variants/', '/intake/clothes/',
            '/intake/import/', '/intake/quick/', '/intake/suppliers/', '/reorder/', '/sales/', '/cart/',
            '/categories/', '/branches/', '/branches/new/', '/users/', '/users/new/',
            '/customers/', '/reports/?report_type=sales&period=month', '/insights/', '/audit/',
            '/labels/', f'/labels/variants/?ids={vid}&copies=1', '/security/2fa/',
            '/payment-qrs/', '/transfers/', '/transfers/new/', '/stocktake/', '/stocktake/new/', '/shifts/',
        ]
        anon_pages = ['/login/']

        errors = []
        n_blocks = 0
        with tempfile.TemporaryDirectory() as tmp:
            def check(client, url):
                nonlocal n_blocks
                r = client.get(url)
                if r.status_code != 200:
                    return
                for i, body in enumerate(_extract(r.content.decode('utf-8', 'ignore'))):
                    n_blocks += 1
                    fn = os.path.join(tmp, f"b_{abs(hash(url))}_{i}.js")
                    with open(fn, 'w') as f:
                        f.write(body)
                    res = subprocess.run([node, '--check', fn], capture_output=True, text=True)
                    if res.returncode != 0:
                        line = next((l for l in res.stderr.splitlines()
                                     if 'Error' in l), res.stderr.strip()[:100])
                        errors.append(f"  {url} [script #{i}]: {line.strip()}")
            for url in pages:
                check(c, url)
            for url in anon_pages:
                check(anon, url)

        if made['shift']:
            sh.delete()
        if made['branch']:
            try: b.delete()
            except Exception: pass
        if made['user']:
            u.delete()

        if errors:
            self.stderr.write(self.style.ERROR(f"JS SYNTAX ERRORS ({len(errors)}):"))
            for e in errors:
                self.stderr.write(e)
            raise CommandError("Inline JavaScript has syntax errors — deploy blocked.")
        self.stdout.write(self.style.SUCCESS(
            f"JS syntax OK — {n_blocks} inline scripts across {len(pages) + len(anon_pages)} pages."))
