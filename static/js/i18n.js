/* yurit — multi-language transliteration
 * - UZ Latin ↔ UZ Cyrillic: deterministic transliteration on page load.
 * - DOM walk via TreeWalker, with MutationObserver for dynamic content.
 * - Skips <code>, <pre>, <script>, <style>, <input>, and elements with
 *   class .no-i18n / data-no-i18n attribute.
 */
(function() {
    'use strict';

    // ---- Transliteration tables ----
    // Order matters: longer (digraph) patterns first.
    const LAT_TO_CYR = [
        ["O'", "Ў"], ["o'", "ў"], ["O’", "Ў"], ["o’", "ў"],
        ["G'", "Ғ"], ["g'", "ғ"], ["G’", "Ғ"], ["g’", "ғ"],
        ["Ch", "Ч"], ["CH", "Ч"], ["ch", "ч"],
        ["Sh", "Ш"], ["SH", "Ш"], ["sh", "ш"],
        ["Yo", "Ё"], ["YO", "Ё"], ["yo", "ё"],
        ["Yu", "Ю"], ["YU", "Ю"], ["yu", "ю"],
        ["Ya", "Я"], ["YA", "Я"], ["ya", "я"],
        ["a", "а"], ["A", "А"],
        ["b", "б"], ["B", "Б"],
        ["d", "д"], ["D", "Д"],
        ["e", "е"], ["E", "Е"],
        ["f", "ф"], ["F", "Ф"],
        ["g", "г"], ["G", "Г"],
        ["h", "ҳ"], ["H", "Ҳ"],
        ["i", "и"], ["I", "И"],
        ["j", "ж"], ["J", "Ж"],
        ["k", "к"], ["K", "К"],
        ["l", "л"], ["L", "Л"],
        ["m", "м"], ["M", "М"],
        ["n", "н"], ["N", "Н"],
        ["o", "о"], ["O", "О"],
        ["p", "п"], ["P", "П"],
        ["q", "қ"], ["Q", "Қ"],
        ["r", "р"], ["R", "Р"],
        ["s", "с"], ["S", "С"],
        ["t", "т"], ["T", "Т"],
        ["u", "у"], ["U", "У"],
        ["v", "в"], ["V", "В"],
        ["x", "х"], ["X", "Х"],
        ["y", "й"], ["Y", "Й"],
        ["z", "з"], ["Z", "З"],
    ];

    // Reverse (Cyr → Lat) — used only when toggling back from Cyrillic
    const CYR_TO_LAT = [
        ["Ў", "O'"], ["ў", "o'"],
        ["Ғ", "G'"], ["ғ", "g'"],
        ["Ч", "Ch"], ["ч", "ch"],
        ["Ш", "Sh"], ["ш", "sh"],
        ["Ё", "Yo"], ["ё", "yo"],
        ["Ю", "Yu"], ["ю", "yu"],
        ["Я", "Ya"], ["я", "ya"],
        ["а", "a"], ["А", "A"],
        ["б", "b"], ["Б", "B"],
        ["д", "d"], ["Д", "D"],
        ["е", "e"], ["Е", "E"],
        ["ф", "f"], ["Ф", "F"],
        ["г", "g"], ["Г", "G"],
        ["ҳ", "h"], ["Ҳ", "H"],
        ["и", "i"], ["И", "I"],
        ["ж", "j"], ["Ж", "J"],
        ["к", "k"], ["К", "K"],
        ["л", "l"], ["Л", "L"],
        ["м", "m"], ["М", "M"],
        ["н", "n"], ["Н", "N"],
        ["о", "o"], ["О", "O"],
        ["п", "p"], ["П", "P"],
        ["қ", "q"], ["Қ", "Q"],
        ["р", "r"], ["Р", "R"],
        ["с", "s"], ["С", "S"],
        ["т", "t"], ["Т", "T"],
        ["у", "u"], ["У", "U"],
        ["в", "v"], ["В", "V"],
        ["х", "x"], ["Х", "X"],
        ["й", "y"], ["Й", "Y"],
        ["з", "z"], ["З", "Z"],
        ["ъ", "'"], ["ь", ""],  // soft/hard signs
    ];

    function transliterate(text, table) {
        let result = '';
        let i = 0;
        const n = text.length;
        while (i < n) {
            let matched = false;
            for (const [src, dst] of table) {
                const sl = src.length;
                if (i + sl <= n && text.substr(i, sl) === src) {
                    result += dst;
                    i += sl;
                    matched = true;
                    break;
                }
            }
            if (!matched) {
                result += text[i];
                i++;
            }
        }
        return result;
    }

    function latToCyr(s) { return transliterate(s, LAT_TO_CYR); }
    function cyrToLat(s) { return transliterate(s, CYR_TO_LAT); }

    // ---- DOM walker ----
    // Skip elements that shouldn't be transliterated.
    const SKIP_TAGS = new Set([
        'SCRIPT', 'STYLE', 'CODE', 'PRE', 'KBD', 'SAMP',
        'INPUT', 'TEXTAREA', 'SELECT', 'OPTION',
    ]);
    function shouldSkip(el) {
        if (!el || el.nodeType !== 1) return false;
        if (SKIP_TAGS.has(el.tagName)) return true;
        if (el.classList && el.classList.contains('no-i18n')) return true;
        if (el.hasAttribute && el.hasAttribute('data-no-i18n')) return true;
        return false;
    }

    function transliterateNode(root, mode) {
        const fn = mode === 'cyr' ? latToCyr : cyrToLat;
        // 1. Text nodes (visible content)
        const walker = document.createTreeWalker(
            root, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    let p = node.parentNode;
                    while (p && p !== root.parentNode) {
                        if (shouldSkip(p)) return NodeFilter.FILTER_REJECT;
                        p = p.parentNode;
                    }
                    return node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                }
            }
        );
        const nodes = [];
        let n;
        while ((n = walker.nextNode())) nodes.push(n);
        for (const node of nodes) {
            const before = node.nodeValue;
            const after = fn(before);
            if (after !== before) node.nodeValue = after;
        }

        // 2. Element attributes that contain translatable text (M7 fix):
        // placeholder, title, aria-label. Apply only when parent isn't skipped.
        // input/textarea ARE in skip list for content, but their placeholder
        // attribute is presentational text — translate it.
        const ATTRS = ['placeholder', 'title', 'aria-label'];
        const els = (root.nodeType === 1 && root.matches('input,textarea,select,button,a,*'))
            ? [root]
            : [];
        const all = (root.querySelectorAll
                     ? Array.from(root.querySelectorAll('[placeholder],[title],[aria-label]'))
                     : []);
        for (const el of [...els, ...all]) {
            if (!el || el.nodeType !== 1) continue;
            // Respect no-i18n on the element itself
            if (el.classList && el.classList.contains('no-i18n')) continue;
            if (el.hasAttribute && el.hasAttribute('data-no-i18n')) continue;
            for (const attr of ATTRS) {
                if (el.hasAttribute && el.hasAttribute(attr)) {
                    const before = el.getAttribute(attr);
                    if (before && before.trim()) {
                        const after = fn(before);
                        if (after !== before) el.setAttribute(attr, after);
                    }
                }
            }
        }
    }

    function applyLanguage(lang) {
        // 'lat' = native (no change), 'cyr' = transliterate to cyrillic
        if (lang === 'cyr') {
            transliterateNode(document.body, 'cyr');
        }
        // For 'lat', we don't re-transliterate (page already in Latin source)
        document.documentElement.setAttribute('lang', lang === 'cyr' ? 'uz-Cyrl' : 'uz-Latn');
    }

    // Initial application based on saved preference
    function getLang() {
        return localStorage.getItem('yurit_lang') || 'lat';
    }

    function setLang(lang) {
        localStorage.setItem('yurit_lang', lang);
        if (lang === 'ru') {
            alert('Ruscha tarjima keyingi yangilanishda qo\'shiladi.\n\n' +
                  'Russian translation coming soon.');
            localStorage.setItem('yurit_lang', 'lat');
            return;
        }
        // Reload page to apply cleanly (avoids re-translit edge cases)
        location.reload();
    }

    // On DOM ready, transliterate if needed
    function onReady(fn) {
        if (document.readyState !== 'loading') fn();
        else document.addEventListener('DOMContentLoaded', fn);
    }

    onReady(() => {
        const lang = getLang();
        if (lang === 'cyr') {
            applyLanguage('cyr');
            // Mutation observer for dynamic content (POS cart updates, modals)
            const observer = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    for (const node of m.addedNodes) {
                        if (node.nodeType === 1) transliterateNode(node, 'cyr');
                        else if (node.nodeType === 3 && node.nodeValue.trim()) {
                            node.nodeValue = latToCyr(node.nodeValue);
                        }
                    }
                }
            });
            observer.observe(document.body, {childList: true, subtree: true});
        }
    });

    // Expose API
    window.yuritI18n = { setLang, getLang, latToCyr, cyrToLat };
})();
