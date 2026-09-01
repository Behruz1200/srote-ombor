/* yurit — jadval qatorlariga RAQAM qo'yadi.
 *
 * NEGA KERAK. Xato xabarlari qator raqami bilan gapiradi:
 *
 *     "5-qator: manfiy qiymat kiritilmaydi."
 *     "3-qator: shtrix-kod jadvalda takror."
 *
 * lekin jadvalda raqam yo'q edi — foydalanuvchi barmoq bilan sanashi
 * kerak bo'lardi. 79 turli mahsulotda bu jiddiy vaqt. Uzun ro'yxatlarda
 * ham "nechanchi qatordaman" degan savol doim bor.
 *
 * ISHLATISH: jadvalga `yrt-numbered` sinfini qo'ying, tamom:
 *
 *     <table class="table yrt-numbered">
 *
 * Sarlavhaga "#" katak, har qatorga raqam katagi O'ZI qo'shiladi.
 * Shablonlarni qo'lda o'zgartirish shart emas.
 *
 * DINAMIK QATORLAR. Qator qo'shilsa yoki o'chirilsa, raqamlar o'zi
 * qayta chiziladi (MutationObserver). Shu bois "yangi tur qo'shish"
 * tugmasi bosilganda ham raqamlar to'g'ri qoladi — bu muhim, chunki
 * xato xabari aynan o'sha raqamga ishora qiladi.
 *
 * BO'SH HOLAT QATORI. "Hozircha yozuv yo'q" kabi bitta colspan'li
 * qatorga raqam berilmaydi — uning o'rniga colspan bittaga oshiriladi,
 * aks holda jadval siljib ketardi.
 */
(function () {
    'use strict';

    var CLS = 'yrt-numbered';
    var CELL = 'yrt-rownum';

    function isPlaceholder(tr) {
        // Bitta katak + colspan = "bo'sh" qatori, ma'lumot emas.
        var tds = tr.children;
        if (tds.length !== 1) return false;
        var c = tds[0];
        return c.hasAttribute('colspan') || c.colSpan > 1;
    }

    function head(table) {
        var thead = table.tHead;
        if (!thead || !thead.rows.length) return;
        for (var i = 0; i < thead.rows.length; i++) {
            var hr = thead.rows[i];
            if (hr.querySelector('.' + CELL)) continue;
            var th = document.createElement('th');
            th.className = CELL;
            // Faqat BIRINCHI sarlavha qatorida "#" yozamiz (ikki qavatli
            // sarlavhalarda pastkisi bo'sh qolsin).
            th.textContent = i === 0 ? '#' : '';
            hr.insertBefore(th, hr.firstChild);
        }
    }

    function body(table) {
        var tbodies = table.tBodies;
        var n = 0;
        for (var b = 0; b < tbodies.length; b++) {
            var rows = tbodies[b].rows;
            for (var i = 0; i < rows.length; i++) {
                var tr = rows[i];
                var cell = tr.querySelector(':scope > .' + CELL);
                if (isPlaceholder(tr)) {
                    if (cell) cell.remove();
                    var only = tr.children[0];
                    if (only && !only.dataset.yrtBumped) {
                        only.colSpan = (only.colSpan || 1) + 1;
                        only.dataset.yrtBumped = '1';
                    }
                    continue;
                }
                n += 1;
                if (!cell) {
                    cell = document.createElement('td');
                    cell.className = CELL;
                    tr.insertBefore(cell, tr.firstChild);
                }
                if (cell.textContent !== String(n)) cell.textContent = n;
            }
        }
    }

    function usable(table) {
        // Sarlavhasiz jadvalga raqam qo'shib bo'lmaydi: katak qo'shilsayu
        // sarlavha qo'shilmasa, ustunlar bir-biriga nisbatan siljib
        // ketadi. Bunday jadval jimgina chetlab o'tiladi.
        return !!(table.tHead && table.tHead.rows.length);
    }

    function apply(table) {
        try {
            if (!usable(table)) return;
            head(table);
            body(table);
        } catch (e) { /* raqam — bezak; xatosi sahifani buzmasin */ }
    }

    function applyAll(root) {
        var tables = (root || document).querySelectorAll('table.' + CLS);
        for (var i = 0; i < tables.length; i++) apply(tables[i]);
    }

    function watch() {
        if (!window.MutationObserver) return;
        var pending = false;
        var obs = new MutationObserver(function () {
            if (pending) return;            // bir kadrda bir marta
            pending = true;
            requestAnimationFrame(function () {
                pending = false;
                var tables = document.querySelectorAll('table.' + CLS);
                for (var i = 0; i < tables.length; i++) {
                    if (usable(tables[i])) body(tables[i]);
                }
            });
        });
        var tables = document.querySelectorAll('table.' + CLS);
        for (var i = 0; i < tables.length; i++) {
            for (var b = 0; b < tables[i].tBodies.length; b++) {
                obs.observe(tables[i].tBodies[b], {childList: true});
            }
        }
    }

    function init() { applyAll(); watch(); }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.yuritRowNum = {apply: apply, applyAll: applyAll};
})();
