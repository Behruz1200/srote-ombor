/* yurit — jadval ustunlari kengligini sichqoncha bilan sozlash.
 *
 * Sahifadagi barcha jadvallarga o'zi ulanadi. Ustun chegarasini tortib
 * kenglikni o'zgartirasiz; o'lchov brauzerda saqlanadi va keyingi safar
 * o'sha holda ochiladi.
 *
 *   - Chegarani TORTING       -> kenglik o'zgaradi
 *   - Chegarani IKKI MARTA bosing -> shu jadval o'lchovlari tiklanadi
 *
 * Jadvalga tegmaslik uchun: <table data-no-resize> yoki class="no-col-resize".
 * Barqaror kalit berish uchun: <table data-colkey="mahsulotlar">.
 */
(function () {
    'use strict';

    var PREFIX = 'yurit_colw:';
    var MIN_W = 44;          // px — ustun bundan tor bo'lmasin
    var HANDLE = 8;          // px — tortish zonasi kengligi

    function hash(str) {
        var h = 5381;
        for (var i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
        return Math.abs(h).toString(36);
    }

    /* Kalit: bir xil turdagi jadval turli sahifalarda ham bir xil qolsin
       (masalan har mahsulot sahifasidagi "Turlar ro'yxati"). Shuning uchun
       manzil emas, sarlavhalar matni asos qilinadi. */
    function keyFor(table, ths) {
        if (table.dataset.colkey) return PREFIX + table.dataset.colkey;
        if (table.id) return PREFIX + table.id;
        var labels = ths.map(function (th) {
            return (th.textContent || '').trim().slice(0, 20);
        }).join('|');
        return PREFIX + hash(labels) + '.' + ths.length;
    }

    function load(key) {
        try {
            var v = JSON.parse(localStorage.getItem(key) || 'null');
            return (v && v.length) ? v : null;
        } catch (e) { return null; }
    }

    function save(key, widths) {
        try { localStorage.setItem(key, JSON.stringify(widths)); } catch (e) {}
    }

    function clear(key) {
        try { localStorage.removeItem(key); } catch (e) {}
    }

    /* Kenglikni boshqarish uchun jadval "fixed" bo'lishi kerak, aks holda
       brauzer o'zi qayta hisoblab yuboradi. Avval hozirgi kengliklarni
       muzlatamiz — shunda bitta ustunni tortganda qolganlari sakramaydi. */
    function freeze(table, ths) {
        if (table.dataset.yrtFrozen === '1') return;
        var ws = ths.map(function (th) { return th.getBoundingClientRect().width; });
        ths.forEach(function (th, i) {
            th.style.width = Math.round(ws[i]) + 'px';
        });
        table.style.tableLayout = 'fixed';
        table.dataset.yrtFrozen = '1';
    }

    function widthsOf(ths) {
        return ths.map(function (th) { return Math.round(th.getBoundingClientRect().width); });
    }

    function attach(table) {
        if (table.dataset.yrtResize === '1') return;
        if (table.hasAttribute('data-no-resize') ||
            table.classList.contains('no-col-resize')) return;

        var head = table.tHead;
        if (!head || !head.rows.length) return;
        // Faqat oxirgi sarlavha qatori (ustma-ust sarlavhalarda pastkisi)
        var row = head.rows[head.rows.length - 1];
        var ths = Array.prototype.slice.call(row.cells);
        if (ths.length < 2) return;
        // Birlashtirilgan kataklar bo'lsa — tegmaymiz, xato hisoblanadi
        for (var i = 0; i < ths.length; i++) {
            if (ths[i].colSpan > 1) return;
        }

        table.dataset.yrtResize = '1';
        var key = keyFor(table, ths);

        var saved = load(key);
        if (saved && saved.length === ths.length) {
            ths.forEach(function (th, i) { th.style.width = saved[i] + 'px'; });
            table.style.tableLayout = 'fixed';
            table.dataset.yrtFrozen = '1';
        }

        ths.forEach(function (th, idx) {
            if (idx === ths.length - 1) return;   // oxirgisining o'ng chekkasi kerak emas
            if (getComputedStyle(th).position === 'static') th.style.position = 'relative';

            var h = document.createElement('span');
            h.className = 'yrt-col-handle';
            h.title = "Kenglikni o'zgartirish uchun torting · tiklash uchun ikki marta bosing";
            th.appendChild(h);

            var startX = 0, startW = 0, dragging = false;

            h.addEventListener('pointerdown', function (e) {
                e.preventDefault();
                e.stopPropagation();          // saralanadigan sarlavhalar bosilib ketmasin
                freeze(table, ths);
                dragging = true;
                startX = e.clientX;
                startW = th.getBoundingClientRect().width;
                h.setPointerCapture(e.pointerId);
                document.body.classList.add('yrt-col-resizing');
            });

            h.addEventListener('pointermove', function (e) {
                if (!dragging) return;
                var w = Math.max(MIN_W, Math.round(startW + (e.clientX - startX)));
                th.style.width = w + 'px';
            });

            function stop(e) {
                if (!dragging) return;
                dragging = false;
                try { h.releasePointerCapture(e.pointerId); } catch (err) {}
                document.body.classList.remove('yrt-col-resizing');
                save(key, widthsOf(ths));
            }
            h.addEventListener('pointerup', stop);
            h.addEventListener('pointercancel', stop);

            // Ikki marta bosish — shu jadval o'lchovlarini tiklaydi
            h.addEventListener('dblclick', function (e) {
                e.preventDefault();
                e.stopPropagation();
                clear(key);
                ths.forEach(function (t) { t.style.width = ''; });
                table.style.tableLayout = '';
                delete table.dataset.yrtFrozen;
            });

            h.addEventListener('click', function (e) { e.stopPropagation(); });
        });
    }

    function scan(root) {
        var tables = (root || document).querySelectorAll('table');
        Array.prototype.forEach.call(tables, function (t) {
            try { attach(t); } catch (e) { /* bitta jadval sababli sahifa buzilmasin */ }
        });
    }

    function init() {
        scan(document);
        // Keyin qo'shilgan jadvallar (modal, AJAX) ham qamrab olinsin
        if (window.MutationObserver) {
            var mo = new MutationObserver(function (muts) {
                for (var i = 0; i < muts.length; i++) {
                    var added = muts[i].addedNodes;
                    for (var j = 0; j < added.length; j++) {
                        var n = added[j];
                        if (n.nodeType !== 1) continue;
                        if (n.tagName === 'TABLE') { try { attach(n); } catch (e) {} }
                        else if (n.querySelectorAll) scan(n);
                    }
                }
            });
            mo.observe(document.body, { childList: true, subtree: true });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.yuritTableResize = { scan: scan, attach: attach };
})();
