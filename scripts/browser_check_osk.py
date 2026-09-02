"""KBD-4 — ekran klaviaturasini KASSIR kabi sinab ko'radi.

Har bir tekshiruv haqiqiy brauzerda, haqiqiy bosish bilan bajariladi:
ochish, katak nomi, yozish, tozalash, kataklar bo'ylab yurish, rejim
almashinuvi, Esc bilan yopish va boshqa sahifada ishlashi.

    DEBUG=1 python scripts/browser_check_osk.py
"""
import os
import sys
import glob
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT); sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','store_management.settings')
os.environ['DEBUG']='1'; os.environ['DJANGO_ALLOW_ASYNC_UNSAFE']='true'
import django; django.setup()
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.test.testcases import LiveServerThread
from django.test.utils import setup_test_environment, teardown_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); runner=DiscoverRunner(verbosity=0,interactive=False)
old=runner.setup_databases(); th=None; BAD=[]
def chk(ok,label,detail=''):
    print(f'  {"OK  " if ok else "XATO"} {label:46s} {detail}')
    if not ok: BAD.append(label)
try:
    from render_snapshot import build_world
    w=build_world()
    th=LiveServerThread('127.0.0.1',StaticFilesHandler); th.daemon=True
    th.start(); th.is_ready.wait(); base=f'http://127.0.0.1:{th.port}'
    from playwright.sync_api import sync_playwright
    exe=next((p for p in glob.glob('/opt/pw-browsers/chromium-*/chrome-linux/chrome') if os.path.exists(p)),None)
    with sync_playwright() as pw:
        br=pw.chromium.launch(executable_path=exe)
        ctx=br.new_context(viewport={'width':1440,'height':900})
        page=ctx.new_page(); errs=[]
        page.on('pageerror', lambda e: errs.append(str(e)))
        page.goto(base+'/login/'); page.fill('input[name=username]',w['admin'].username)
        page.fill('input[name=password]','x'); page.click('button[type=submit]')
        page.wait_for_load_state('networkidle')
        page.evaluate("() => { localStorage.removeItem('yurit_osk_mode'); localStorage.removeItem('yurit_osk_open'); }")
        page.goto(base+'/pos/', wait_until='networkidle')

        print('\n--- 1. Ochilish ---')
        page.click('#oskToggle'); page.wait_for_timeout(350)
        chk(page.is_visible('#osk'),'ochildi')
        chk(not errs,'JS xatosi yo\'q',errs[:1])
        chk(page.inner_text('#oskTarget').strip()=='Yozilyapti: Skaner / qidiruv',
            'katak NOMI ko\'rinadi (misol emas)', repr(page.inner_text('#oskTarget')))

        print('\n--- 2. Yopish tugmasi bosiladimi (ilgari klaviatura ostida qolardi) ---')
        vis = page.evaluate("""() => {
            const t=document.getElementById('oskToggle'), r=t.getBoundingClientRect();
            const el=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
            return {ustida: !!(el && (el===t || t.contains(el))), bottom: Math.round(r.bottom)};
        }""")
        chk(vis['ustida'],'dumaloq tugma klaviatura USTIDA',vis)
        chk(page.is_visible('#oskClose'),'bosh qismda ✕ tugmasi bor')

        print('\n--- 3. Yozish + jonli ko\'rinish ---')
        page.click('#scanInput'); page.wait_for_timeout(150)
        for ch in ('a','b','c'):
            page.click(f'#osk .osk-key >> text="{ch}"'); page.wait_for_timeout(50)
        chk(page.input_value('#scanInput')=='abc','harf katakka tushdi',repr(page.input_value('#scanInput')))
        chk(page.inner_text('#oskValue').strip()=='abc','tepada jonli qiymat ko\'rinadi',repr(page.inner_text('#oskValue')))

        print('\n--- 4. Tozalash + keyingi katak ---')
        page.click('#osk .osk-key >> text="Tozalash"'); page.wait_for_timeout(120)
        chk(page.input_value('#scanInput')=='','Tozalash ishladi')
        before = page.evaluate("() => document.activeElement.id")
        page.click('#osk .osk-key >> text="⇥ Keyingi"'); page.wait_for_timeout(250)
        after = page.evaluate("() => document.activeElement.id")
        chk(before!=after,'⇥ keyingi katakka o\'tdi',f'{before} -> {after}')
        # AUTO rejimi raqamli katakda 123 ga o'tadi — ⇤ o'sha yerda ham bor
        page.click('#osk .osk-key[data-kind="prev"]:visible'); page.wait_for_timeout(250)
        chk(page.evaluate("() => document.activeElement.id")==before,'⇤ orqaga qaytdi',
            page.evaluate("() => document.activeElement.id"))

        print('\n--- 5. AUTO rejim: raqamli katakda 123 ---')
        page.click('#osk .osk-mode button[data-osk-mode="auto"]'); page.wait_for_timeout(150)
        page.click('#orderDiscount'); page.wait_for_timeout(250)
        chk(page.evaluate("() => osk.classList.contains('osk-num')"),'raqamli katakda 123 ochildi')
        chk(page.inner_text('#oskTarget').strip()=='Yozilyapti: Chek chegirmasi','nomi to\'g\'ri',repr(page.inner_text('#oskTarget')))
        page.click('#scanInput'); page.wait_for_timeout(250)
        chk(page.evaluate("() => osk.classList.contains('osk-abc')"),'matnli katakda ABC qaytdi')

        print('\n--- 6. Vergul va o\'zbekcha tutuq ---')
        keys = page.evaluate("() => [...document.querySelectorAll('#osk .osk-key')].map(b=>b.textContent.trim())")
        chk('ʻ' in keys,'ʻ (tutuq) tugmasi bor')
        page.click('#osk .osk-mode button[data-osk-mode="123"]'); page.wait_for_timeout(200)
        keys = page.evaluate("() => [...document.querySelectorAll('#osk .osk-key')].map(b=>b.textContent.trim())")
        chk(',' in keys,'123 rejimida vergul bor')
        chk(any('⇥' in k for k in keys),'123 rejimida ⇥ bor')
        chk(any(k=='⇤' for k in keys),'123 rejimida ⇤ bor')

        print('\n--- 7. Esc bilan yopish ---')
        page.keyboard.press('Escape'); page.wait_for_timeout(300)
        chk(not page.is_visible('#osk'),'Esc yopdi')
        pb = page.evaluate("() => document.body.style.paddingBottom")
        chk(pb in ('','0px'),'pastki bo\'shliq tozalandi',repr(pb))

        print('\n--- 8. Boshqa sahifada ham ishlaydi ---')
        page.goto(base+'/customers/', wait_until='networkidle')
        errs.clear(); page.click('#oskToggle'); page.wait_for_timeout(350)
        chk(not errs,'JS xatosi yo\'q',errs[:1])
        t = page.inner_text('#oskTarget').strip()
        chk('Avval' not in t,'katak O\'ZI tanlandi',repr(t))
        chk(t=='Yozilyapti: Qidiruv','umumiy qidiruv katagi to\'g\'ri nomlandi',repr(t))
        br.close()
    print('\n'+'='*64); print(f'  MUAMMO: {len(BAD)}')
    for b in BAD: print('   -', b)
    print('='*64)
finally:
    if th: th.terminate()
    runner.teardown_databases(old); teardown_test_environment()

sys.exit(1 if BAD else 0)
