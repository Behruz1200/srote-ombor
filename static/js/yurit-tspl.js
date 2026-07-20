/* yurit — termal etiketka printeriga to'g'ridan-to'g'ri chop etish (WebUSB + TSPL).
 *
 * Har etiketka ALOHIDA topshiriq bo'lib yuboriladi — ya'ni bittalab bosiladi,
 * bitta varaqqa bir nechtasi tushmaydi. Printer WinUSB (Zadig) drayveriga
 * o'tkazilgan bo'lishi va sahifa HTTPS'da ochilishi shart.
 *
 * Ishlatish:
 *     const bmp = yuritTSPL.canvasToBitmap(canvas, 50, 30, invert);
 *     await yuritTSPL.printAll([bmp], { onStatus: (msg, isErr) => ... });
 */
(function (global) {
    'use strict';

    const KEY = 'yurit_label_usb';

    function saved() {
        try { return JSON.parse(localStorage.getItem(KEY) || 'null'); }
        catch (e) { return null; }
    }

    async function pickPrinter(forcePrompt) {
        if (!navigator.usb) throw new Error("WebUSB yo'q — Chrome (HTTPS) kerak");
        const s = saved();
        if (!forcePrompt && s) {
            const devs = await navigator.usb.getDevices();
            const m = devs.find(d => d.vendorId === s.vendorId &&
                                     d.productId === s.productId);
            if (m) return m;
        }
        const device = await navigator.usb.requestDevice({ filters: [] });
        localStorage.setItem(KEY, JSON.stringify({
            vendorId: device.vendorId, productId: device.productId,
        }));
        return device;
    }

    async function sendBytes(device, bytes) {
        if (!device.opened) await device.open();
        if (device.configuration === null) await device.selectConfiguration(1);
        let ifaceNum = null, epNum = null;
        for (const iface of device.configuration.interfaces) {
            const alt = iface.alternate || (iface.alternates && iface.alternates[0]);
            if (!alt) continue;
            const ep = alt.endpoints.find(e => e.direction === 'out' && e.type === 'bulk')
                    || alt.endpoints.find(e => e.direction === 'out');
            if (ep) { ifaceNum = iface.interfaceNumber; epNum = ep.endpointNumber; break; }
        }
        if (epNum === null) throw new Error('USB chiqish endpoint topilmadi');
        const iface = device.configuration.interfaces
            .find(i => i.interfaceNumber === ifaceNum);
        if (!iface.claimed) await device.claimInterface(ifaceNum);
        const CHUNK = 4096;
        for (let i = 0; i < bytes.length; i += CHUNK) {
            await device.transferOut(epNum,
                bytes.subarray(i, Math.min(i + CHUNK, bytes.length)));
        }
    }

    /** Canvas -> 1 bitli monoxrom bitmap (TSPL BITMAP uchun). */
    function canvasToBitmap(canvas, wmm, hmm, invert) {
        const W = canvas.width, H = canvas.height;
        const px = canvas.getContext('2d').getImageData(0, 0, W, H).data;
        const bpr = Math.ceil(W / 8);
        const data = new Uint8Array(bpr * H);
        data.fill(invert ? 0x00 : 0xFF);
        for (let y = 0; y < H; y++) {
            for (let x = 0; x < W; x++) {
                const i = (y * W + x) * 4;
                const lum = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
                if (px[i + 3] > 64 && lum < 128) {
                    const bi = y * bpr + (x >> 3);
                    const mask = 0x80 >> (x & 7);
                    if (invert) data[bi] |= mask; else data[bi] &= ~mask;
                }
            }
        }
        return { data: data, bpr: bpr, H: H, wmm: wmm, hmm: hmm };
    }

    function buildTSPL(b) {
        const enc = new TextEncoder();
        const head = enc.encode(
            'SIZE ' + b.wmm + ' mm,' + b.hmm + ' mm\r\n' +
            'GAP 2 mm,0 mm\r\nDIRECTION 1\r\nCLS\r\n' +
            'BITMAP 0,0,' + b.bpr + ',' + b.H + ',0,'
        );
        const tail = enc.encode('\r\nPRINT 1,1\r\n');
        const out = new Uint8Array(head.length + b.data.length + tail.length);
        out.set(head, 0);
        out.set(b.data, head.length);
        out.set(tail, head.length + b.data.length);
        return out;
    }

    /** Bitmaplarni BITTALAB yuboradi (har biri alohida PRINT topshirig'i). */
    async function printAll(bitmaps, opts) {
        opts = opts || {};
        const status = opts.onStatus || function () {};
        if (!bitmaps.length) { status("Yorliq yo'q", true); return false; }
        let device;
        try {
            status('Printerga ulanmoqda...', false);
            device = await pickPrinter(!!opts.forcePrompt);
        } catch (e) {
            status('Printer: ' + e.message, true);
            return false;
        }
        try {
            for (let i = 0; i < bitmaps.length; i++) {
                status('Chop etilmoqda ' + (i + 1) + '/' + bitmaps.length + '...', false);
                await sendBytes(device, buildTSPL(bitmaps[i]));
            }
            status('✓ Chop etildi (' + bitmaps.length + ' ta, USB)', false);
            return true;
        } catch (e) {
            status('USB xato: ' + e.message, true);
            return false;
        }
    }

    /** SVG elementni rasm sifatida yuklaydi (canvasga chizish uchun). */
    function svgToImage(svgEl, w, h) {
        return new Promise(function (resolve, reject) {
            const clone = svgEl.cloneNode(true);
            clone.setAttribute('width', w);
            clone.setAttribute('height', h);
            if (!clone.getAttribute('xmlns')) {
                clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            }
            const xml = new XMLSerializer().serializeToString(clone);
            const img = new Image();
            img.onload = function () { resolve(img); };
            img.onerror = reject;
            img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
        });
    }

    function loadImage(src) {
        return new Promise(function (resolve, reject) {
            const img = new Image();
            img.onload = function () { resolve(img); };
            img.onerror = reject;
            img.src = src;
        });
    }

    global.yuritTSPL = {
        pickPrinter: pickPrinter,
        sendBytes: sendBytes,
        canvasToBitmap: canvasToBitmap,
        buildTSPL: buildTSPL,
        printAll: printAll,
        svgToImage: svgToImage,
        loadImage: loadImage,
        DPMM: 8,
    };
})(window);
