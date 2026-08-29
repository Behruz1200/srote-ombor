/* yurit — rasmni kattalashtirib ko'rish (umumiy oyna).
 *
 * Faktura (nakladnoy) sahifasidagi ko'rish oynasining o'zi: zum, burish,
 * sudrash, barmoq bilan pinch, Esc bilan yopish.
 *
 * Ishlatish:
 *     yuritZoom.open('/media/products/foo.jpg');
 * yoki HTML'da:
 *     <img data-zoom-src="/media/...">   (o'zi bog'lanadi)
 *
 * Bootstrap KERAK EMAS — u sahifa mazmunidan keyin yuklanadi, shuning uchun
 * bu mustaqil overlay sifatida ishlaydi.
 */
(function (global) {
    'use strict';

    var el = null, pic = null, pct = null;
    var zs = 1, zx = 0, zy = 0, zrot = 0, zfit = 1;

    function build() {
        if (el) return;
        el = document.createElement('div');
        el.className = 'yrt-zoom';
        el.id = 'yuritZoom';
        el.setAttribute('aria-hidden', 'true');
        el.innerHTML =
            '<div class="yrt-zoom__bar">' +
              '<button type="button" class="btn btn-sm btn-light" data-z="out" title="Kichraytirish"><i class="bi bi-zoom-out"></i></button>' +
              '<span class="yrt-zoom__pct">100%</span>' +
              '<button type="button" class="btn btn-sm btn-light" data-z="in" title="Kattalashtirish"><i class="bi bi-zoom-in"></i></button>' +
              '<button type="button" class="btn btn-sm btn-light" data-z="fit" title="Ekranga moslash"><i class="bi bi-arrows-angle-contract"></i></button>' +
              '<button type="button" class="btn btn-sm btn-light" data-z="rot" title="Burish"><i class="bi bi-arrow-clockwise"></i></button>' +
              '<button type="button" class="btn btn-sm btn-danger" data-z="close" title="Yopish (Esc)"><i class="bi bi-x-lg"></i></button>' +
            '</div>' +
            '<img alt="Rasm" draggable="false">' +
            '<div class="yrt-zoom__hint small">Ikki marta bosing — kattalashadi · sudrab suring · g\'ildirak bilan zum</div>';
        document.body.appendChild(el);
        pic = el.querySelector('img');
        pct = el.querySelector('.yrt-zoom__pct');
        wire();
    }

    function apply() {
        pic.style.transform =
            'translate(calc(-50% + ' + zx + 'px), calc(-50% + ' + zy + 'px)) ' +
            'scale(' + zs + ') rotate(' + zrot + 'deg)';
        pct.textContent = Math.round(zs / zfit * 100) + '%';
    }

    function fit() {
        var nw = pic.naturalWidth || 1, nh = pic.naturalHeight || 1;
        var rot = (zrot % 180) !== 0;
        var w = rot ? nh : nw, h = rot ? nw : nh;
        zfit = Math.min((window.innerWidth - 24) / w, (window.innerHeight - 96) / h);
        if (!isFinite(zfit) || zfit <= 0) zfit = 1;
        zs = zfit; zx = 0; zy = 0;
        apply();
    }

    function set(next, cx, cy) {
        next = Math.max(zfit * 0.5, Math.min(zfit * 12, next));
        if (cx !== undefined) {
            var k = next / zs;              // kursor ostidagi nuqta joyida qolsin
            zx = cx - (cx - zx) * k;
            zy = cy - (cy - zy) * k;
        }
        zs = next;
        apply();
    }

    function open(src) {
        if (!src) return;
        build();
        pic.src = src;
        el.classList.add('is-open');
        el.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        zrot = 0;
        if (pic.complete && pic.naturalWidth) fit();
        else pic.onload = fit;
    }

    function close() {
        if (!el) return;
        el.classList.remove('is-open');
        el.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
    }

    function dist(t) {
        var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY;
        return Math.hypot(dx, dy);
    }

    function wire() {
        el.querySelectorAll('[data-z]').forEach(function (b) {
            if (b.dataset.z === 'tab') return;
            b.addEventListener('click', function () {
                var a = b.dataset.z;
                if (a === 'in') set(zs * 1.4);
                else if (a === 'out') set(zs / 1.4);
                else if (a === 'fit') { zrot = 0; fit(); }
                else if (a === 'rot') { zrot = (zrot + 90) % 360; fit(); }
                else if (a === 'close') close();
            });
        });
        el.addEventListener('click', function (e) {
            if (e.target === el) close();        // fon bosilsa — yopamiz
        });
        pic.addEventListener('dblclick', function (e) {
            set(zs > zfit * 1.2 ? zfit : zfit * 3, e.clientX, e.clientY);
        });
        el.addEventListener('wheel', function (e) {
            e.preventDefault();
            set(zs * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY);
        }, { passive: false });
        document.addEventListener('keydown', function (e) {
            if (!el.classList.contains('is-open')) return;
            if (e.key === 'Escape') close();
            else if (e.key === '+' || e.key === '=') set(zs * 1.4);
            else if (e.key === '-') set(zs / 1.4);
            else if (e.key === '0') { zrot = 0; fit(); }
        });

        // sudrash (sichqoncha) + surish/pinch (barmoq)
        var pan = null, pinch = null;
        pic.addEventListener('pointerdown', function (e) {
            if (e.pointerType === 'touch') return;
            pan = { x: e.clientX - zx, y: e.clientY - zy };
            pic.classList.add('is-dragging');
            pic.setPointerCapture(e.pointerId);
        });
        pic.addEventListener('pointermove', function (e) {
            if (!pan || e.pointerType === 'touch') return;
            zx = e.clientX - pan.x; zy = e.clientY - pan.y;
            apply();
        });
        ['pointerup', 'pointercancel'].forEach(function (ev) {
            pic.addEventListener(ev, function () {
                pan = null; pic.classList.remove('is-dragging');
            });
        });
        el.addEventListener('touchstart', function (e) {
            if (e.touches.length === 2) pinch = { d: dist(e.touches), s: zs };
            else if (e.touches.length === 1)
                pan = { x: e.touches[0].clientX - zx, y: e.touches[0].clientY - zy };
        }, { passive: true });
        el.addEventListener('touchmove', function (e) {
            if (pinch && e.touches.length === 2) {
                e.preventDefault();
                set(pinch.s * (dist(e.touches) / pinch.d));
            } else if (pan && e.touches.length === 1) {
                e.preventDefault();
                zx = e.touches[0].clientX - pan.x;
                zy = e.touches[0].clientY - pan.y;
                apply();
            }
        }, { passive: false });
        el.addEventListener('touchend', function (e) {
            if (e.touches.length === 0) { pan = null; pinch = null; }
        }, { passive: true });
        window.addEventListener('resize', function () {
            if (el.classList.contains('is-open')) fit();
        });
    }

    // data-zoom-src bo'lgan rasmlar o'zi bog'lanadi (keyin qo'shilganlari ham)
    document.addEventListener('click', function (e) {
        var t = e.target.closest && e.target.closest('[data-zoom-src]');
        if (!t) return;
        e.preventDefault();
        open(t.getAttribute('data-zoom-src') || t.getAttribute('src'));
    });

    global.yuritZoom = { open: open, close: close };
})(window);
