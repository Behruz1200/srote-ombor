/* CORE-6 — sahifalarda TAKRORLANGAN JS yordamchilari bitta joyda.
 *
 * Nima takrorlangan edi:
 *   * `function num(s)` — 5 faylda AYNAN bir xil;
 *   * qisqa son formati (1.2mln / 3k) — 3 faylda aynan bir xil;
 *   * CSRF tokenni olish — 45 faylda, UCH XIL usulda, ulardan
 *     IKKITASI NOTO'G'RI (pastga qarang);
 *   * `fetch(...)` + sarlavhalar + JSON tahlili — 42 marta.
 *
 * CSRF haqida (SW-4). Sahifa xizmat ishchisi (service worker) tomonidan
 * KESHLANGAN bo'lishi mumkin. Shunda sahifadagi `{{ csrf_token }}` va
 * formadagi yashirin katak ESKI tokenni saqlaydi. Umumiy kassada A
 * kassir chiqib, B kirsa — B ning so'rovi 403 bo'lardi. To'g'ri manba
 * — JONLI `csrftoken` cookie'si. Forma katagi faqat zaxira.
 *
 * `window.Y` global — sahifalardagi inline skriptlar undan foydalanadi.
 */
(function (global) {
    'use strict';

    function csrf() {
        var m = document.cookie.match(/(?:^|;)\s*csrftoken\s*=\s*([^;]+)/);
        if (m) return decodeURIComponent(m[1]);
        var el = document.querySelector('input[name=csrfmiddlewaretoken]');
        return el ? el.value : '';
    }

    /* Matn -> son. Bo'sh yoki noto'g'ri bo'lsa null ("kiritilmagan"),
     * 0 emas: aks holda tozalangan katak narxni NOLGA tushirardi.
     *
     * VERGUL — O'NLIK AJRATGICH (server bilan bir xil).  Eski nusxalar
     * vergulni shunchaki O'CHIRIB tashlardi: brauzer "50,5" ni 505 deb
     * ko'rsatar, server esa (money.parse_money) 50.5 deb saqlardi —
     * ya'ni kassir ko'rgan narx bilan bazadagi narx BOSHQA edi. */
    function num(s) {
        s = (s === null || s === undefined ? '' : s).toString()
            .replace(/,/g, '.')
            .replace(/[^0-9.\-]/g, '');
        var dot = s.indexOf('.');
        if (dot !== -1) {              // faqat BIRINCHI nuqta ajratgich
            s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '');
        }
        if (s === '' || s === '-' || s === '.') return null;
        var v = parseFloat(s);
        return isNaN(v) ? null : v;
    }

    /* 1 234 567 — uch xonali guruhlar, uzilmas probel bilan. */
    function money(v) {
        if (v === null || v === undefined || v === '') return '';
        var n = typeof v === 'number' ? v : num(v);
        if (n === null) return '';
        return Math.round(n).toString()
            .replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }

    /* Diagramma o'qlari uchun qisqa shakl: 1.2mlrd / 3.4mln / 12k */
    function fmtShort(v) {
        if (v == null || v === 0) return v === 0 ? '0' : '';
        var a = Math.abs(v);
        if (a >= 1e9) return (v / 1e9).toFixed(1).replace('.0', '') + 'mlrd';
        if (a >= 1e6) return (v / 1e6).toFixed(1).replace('.0', '') + 'mln';
        if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1)
            .replace('.0', '') + 'k';
        return String(Math.round(v));
    }

    /* JSON POST — CSRF, sarlavhalar va xato tahlili bitta joyda.
     * Har doim {ok: ...} shaklidagi obyekt qaytaradi (tarmoq uzilsa ham),
     * shuning uchun chaqiruvchi try/catch yozishi shart emas. */
    function post(url, data, opts) {
        opts = opts || {};
        var headers = {'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest'};
        var body;
        if (data instanceof FormData) {
            body = data;
        } else {
            headers['Content-Type'] = 'application/json';
            body = JSON.stringify(data || {});
        }
        Object.keys(opts.headers || {}).forEach(function (k) {
            headers[k] = opts.headers[k];
        });
        return fetch(url, {
            method: 'POST', headers: headers, body: body,
            credentials: 'same-origin'
        }).then(function (r) {
            return r.json().catch(function () { return {}; })
                .then(function (j) {
                    if (typeof j.ok === 'undefined') j.ok = r.ok;
                    j.status = r.status;
                    return j;
                });
        }).catch(function (e) {
            return {ok: false, error: (e && e.message) || 'tarmoq xatosi',
                    status: 0};
        });
    }

    function get(url) {
        return fetch(url, {credentials: 'same-origin',
                           headers: {'X-Requested-With': 'XMLHttpRequest'}})
            .then(function (r) {
                return r.json().catch(function () { return {}; })
                    .then(function (j) { j.status = r.status; return j; });
            })
            .catch(function (e) {
                return {ok: false, error: (e && e.message) || 'tarmoq xatosi',
                        status: 0};
            });
    }

    global.Y = global.Y || {};
    global.Y.csrf = csrf;
    global.Y.num = num;
    global.Y.money = money;
    global.Y.fmtShort = fmtShort;
    global.Y.post = post;
    global.Y.get = get;
})(window);
