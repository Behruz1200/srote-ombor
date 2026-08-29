/* yurit — chek chop etishda sahifani chek bo'yiga moslash.
 *
 * MUAMMO. 14 qatorli chek XP-80 termal printerda IKKITA alohida chek
 * bo'lib chiqardi: ikkinchi varaqda ustun sarlavhalari va "JAMI" qaytadan
 * bosilardi, ya'ni mijozga ikkita yarim chek berilardi.
 *
 * SABAB (brauzerda o'lchab aniqlandi, taxmin emas). Chek CSS'ida shunday
 * yozilgan edi:
 *
 *     @media print { @page { size: 80mm auto; margin: 0; } }
 *
 * Bu to'g'ridek ko'rinadi, lekin ikkita xato bor:
 *
 *   1. Chrome `@media print` ICHIGA yozilgan `@page` qoidasini butunlay
 *      E'TIBORSIZ qoldiradi. `@page` YUQORI DARAJADA turishi kerak.
 *      Shu sababli bu qoida hech qachon ishlamagan.
 *   2. Chrome balandlik uchun `auto` ni ham qo'llab-quvvatlamaydi. Hatto
 *      to'g'ri joyga yozilganda ham u standart qog'oz balandligini olardi
 *      (Letter = 279mm) va uzun chek bo'linib ketardi.
 *
 * YECHIM. Chop etishdan oldin chekning haqiqiy balandligini o'lchab,
 * YUQORI DARAJADAGI `@page` ga aniq son yozamiz. Bu naqsh loyihada
 * allaqachon bor — variant_labels.html yorliq o'lchamini shunday beradi.
 *
 * IKKI NOZIK JOY — ikkalasi ham o'lchov bilan tekshirilgan:
 *
 *   a) Ekrandagi balandlik chop etishdagi balandlik EMAS (shrift boshqa,
 *      kenglik 80mm, navbar yashiringan). Shuning uchun sahifaning O'Z
 *      `@media print` bloklarini vaqtincha `@media all` ga aylantirib
 *      qo'yamiz. Nusxalar BODY OXIRIGA qo'shiladi: chek uslublari ham
 *      body ichidagi <style> da turadi, head'ga qo'shilsa ular ustun
 *      kelib, o'lchov 15% xato bo'lardi.
 *
 *   b) `documentElement.scrollHeight` ISHLATILMAYDI — u hech qachon
 *      oynadan kichik bo'lmaydi (720px = 190mm), shuning uchun kalta
 *      chek uchun 84mm ORTIQCHA qog'oz chiqarardi. Kuniga 148 chekda bu
 *      12 metr behuda lenta. Buning o'rniga chekning o'z pastki chekkasi
 *      olinadi.
 *
 * CSS ikki marta yozilmaydi: manba bitta bo'lib qoladi, chek uslubi
 * kelajakda o'zgarsa o'lchov o'zi moslashadi.
 */
(function (global) {
    'use strict';

    var PX_PER_MM = 96 / 25.4;      // CSS: 1in = 96px = 25.4mm
    var TAIL_MM = 3;                // oxirgi qator kesilmasin
    var MIN_MM = 30;

    function printStyleSources() {
        var out = [];
        var nodes = document.querySelectorAll('style');
        for (var i = 0; i < nodes.length; i++) {
            var css = nodes[i].textContent || '';
            if (css.indexOf('@media print') !== -1) out.push(css);
        }
        return out;
    }

    /** Chop etish maketidagi chekning PASTKI CHEKKASI, pikselda. */
    function measure(selector) {
        var node = document.querySelector(selector || '#receipt');
        if (!node) return 0;
        var clones = [];
        var sources = printStyleSources();
        for (var i = 0; i < sources.length; i++) {
            var st = document.createElement('style');
            // FAQAT media nomi almashadi — qoidalarga tegilmaydi.
            st.textContent = sources[i].split('@media print').join('@media all');
            st.setAttribute('data-rcpt-measure', '1');
            document.body.appendChild(st);      // body oxiriga — (a) izohiga qarang
            clones.push(st);
        }
        var px = 0;
        try {
            px = node.getBoundingClientRect().bottom;   // (b) izohiga qarang
        } finally {
            for (var j = 0; j < clones.length; j++) {
                if (clones[j].parentNode) clones[j].parentNode.removeChild(clones[j]);
            }
        }
        return px > 0 ? px : 0;
    }

    /**
     * @page ni chek bo'yiga moslaydi.
     * Qaytaradi: belgilangan balandlik (mm); o'lchab bo'lmasa 0.
     */
    function fit(selector, widthMm) {
        var px;
        try {
            px = measure(selector);
        } catch (e) {
            return 0;               // o'lchov yiqilsa ham chop etish ketaversin
        }
        if (!px) return 0;
        var mm = Math.ceil(px / PX_PER_MM) + TAIL_MM;
        if (mm < MIN_MM) mm = MIN_MM;
        var st = document.getElementById('rcptPageSize');
        if (!st) {
            st = document.createElement('style');
            st.id = 'rcptPageSize';
        }
        // YUQORI DARAJADA — @media print ichida emas (yuqoridagi 1-sabab).
        st.textContent = '@page { size: ' + (widthMm || 80) + 'mm ' + mm +
                         'mm; margin: 0; }';
        document.body.appendChild(st);          // eng oxirida tursin
        return mm;
    }

    global.yuritReceiptPrint = { fit: fit, measure: measure };
})(window);
