"""CORE-6 — brauzerda tekshiruv: umumiy JS yordamchilari ishlaydimi.

Har bir muhim sahifani HAQIQIY brauzerda ochadi va:
  * JS xatosi bor-yo'qligini,
  * window.Y yuklanganini,
  * Y.num / Y.money / Y.fmtShort / Y.csrf to'g'ri javob berishini,
  * eski nusxalar (num, shortSom) hamon ishlashini tekshiradi.

    DEBUG=1 python scripts/browser_check_core.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')
os.environ['DEBUG'] = '1'
os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

import django                                       # noqa: E402
django.setup()

from django.contrib.staticfiles.handlers import StaticFilesHandler   # noqa: E402
from django.test.testcases import LiveServerThread                   # noqa: E402
from django.test.utils import setup_test_environment, teardown_test_environment  # noqa: E402
from django.test.runner import DiscoverRunner       # noqa: E402

PAGES = ['/', '/sales/', '/prices/', '/products/', '/audit/', '/insights/',
         '/warehouse/', '/customers/', '/branches/', '/reports/',
         '/intake/variants/', '/pos/']

NBSP = '\u00a0'      # `som` filtri ham aynan shuni ishlatadi
CHECKS = [
    ("Y yuklandi", "typeof Y === 'object'", True),
    ("Y.num('12 345,50')", "Y.num('12 345,50')", 12345.5),
    ("Y.num('50,5')", "Y.num('50,5')", 50.5),
    ("Y.num('')", "Y.num('')", None),
    ("Y.num('abc')", "Y.num('abc')", None),
    ("Y.money(1234567)", "Y.money(1234567)", f'1{NBSP}234{NBSP}567'),
    ("Y.fmtShort(2412000)", "Y.fmtShort(2412000)", '2.4mln'),
    ("Y.fmtShort(0)", "Y.fmtShort(0)", '0'),
    ("Y.csrf() bo'sh emas", "Y.csrf().length > 0", True),
]


def main():
    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old = runner.setup_databases()
    thread = None
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'scripts'))
        from render_snapshot import build_world
        world = build_world()

        thread = LiveServerThread('127.0.0.1', StaticFilesHandler)
        thread.daemon = True
        thread.start()
        thread.is_ready.wait()
        base = f'http://127.0.0.1:{thread.port}'

        from playwright.sync_api import sync_playwright
        bad = 0
        with sync_playwright() as pw:
            # Playwright versiyasi kutgan yo'l bo'lmasa — o'rnatilganini
            # to'g'ridan-to'g'ri ko'rsatamiz (konteynerda oldindan bor).
            import glob as _glob
            _exe = (_glob.glob('/opt/pw-browsers/chromium-*/chrome-linux/chrome')
                    + ['/opt/pw-browsers/chromium/chrome-linux/chrome'])
            _exe = next((p for p in _exe if os.path.exists(p)), None)
            br = pw.chromium.launch(executable_path=_exe) if _exe \
                else pw.chromium.launch()
            ctx = br.new_context()
            page = ctx.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))

            # login
            page.goto(base + '/login/')
            page.fill('input[name=username]', world['admin'].username)
            page.fill('input[name=password]', 'x')
            page.click('button[type=submit]')
            page.wait_for_load_state('networkidle')

            for url in PAGES:
                errors.clear()
                page.goto(base + url, wait_until='networkidle')
                if errors:
                    print(f'  XATO {url}: {errors[0][:110]}')
                    bad += 1
                    continue
                has_y = page.evaluate("typeof window.Y")
                if has_y != 'object':
                    print(f'  XATO {url}: window.Y yo\'q')
                    bad += 1
                    continue
                print(f'  OK   {url}')

            print('\n  --- Y.* funksiyalari ---')
            page.goto(base + '/prices/', wait_until='networkidle')
            for label, expr, want in CHECKS:
                got = page.evaluate(f'() => {expr}')
                ok = got == want
                bad += not ok
                print(f'  {"OK  " if ok else "XATO"} {label:26s} -> {got!r}'
                      + ('' if ok else f'  (kutilgan {want!r})'))

            # umumiy yordamchiga bog'langan eski nomlar ishlaydimi
            print('\n  --- umumiy funksiyaga bog\'langan nomlar ---')
            for url, expr, want in (
                    ('/', "typeof srtChart.shortSom === 'function'", True),
                    ('/', "srtChart.shortSom(2412000)", '2.4mln')):
                page.goto(base + url, wait_until='networkidle')
                got = page.evaluate(f'() => {expr}')
                bad += got != want
                print(f'  {"OK  " if got == want else "XATO"} {url:6s} {expr}'
                      f' -> {got!r}')
            br.close()
        print('\n' + '=' * 60)
        print(f'  XATO: {bad}')
        print('=' * 60)
        return 1 if bad else 0
    finally:
        if thread:
            thread.terminate()
        runner.teardown_databases(old)
        teardown_test_environment()


if __name__ == '__main__':
    sys.exit(main())
