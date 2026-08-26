"""Minimal TOTP (RFC 6238) + recovery codes — no external dependency.

Opt-in per user: login is unchanged until a user enables 2FA from the
Security page. Recovery codes are stored hashed; the plaintext is shown once.
"""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time


def gen_secret():
    """New base32 TOTP secret (compatible with Google Authenticator etc.)."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _totp_at(secret_b32, when, step=30, digits=6):
    pad = '=' * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    counter = int(when // step)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(val % (10 ** digits)).zfill(digits)


def verify_totp(secret_b32, code, window=1):
    """True if `code` matches within +/- `window` 30s steps (clock drift)."""
    return verify_totp_step(secret_b32, code, window) is not None


def verify_totp_step(secret_b32, code, window=1, step=30):
    """AUTH-3: mos kelган vaqt-qadamини (butun son) qaytaradi, yoki None.

    Chaqiruvchi bu qadamни saqlab, o'sha kodни QAYTA ishlatishни bloklaydi.
    """
    code = (code or '').strip().replace(' ', '')
    if not secret_b32 or not code.isdigit():
        return None
    now = time.time()
    for w in range(-window, window + 1):
        t = now + w * step
        if _totp_at(secret_b32, t, step=step) == code:
            return int(t // step)
    return None


def otpauth_uri(secret_b32, username, issuer='yurit'):
    return (f"otpauth://totp/{issuer}:{username}"
            f"?secret={secret_b32}&issuer={issuer}&digits=6&period=30")


def qr_datauri(text):
    """otpauth URI -> PNG data URI (uses the bundled qrcode lib)."""
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=5, border=2)
    qr.add_data(text); qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


# ----- recovery codes -----

def gen_recovery_codes(n=8):
    """Return n human-friendly one-time codes (plaintext, shown once).

    SEC-17: har kod ~72 bit entropiya (ilgari 32 bit edi — GPUда tez topilardi).
    """
    return ['-'.join(secrets.token_hex(3) for _ in range(3)) for _ in range(n)]


def hash_code(code):
    """SEC-17: tuzlangan, ko'p-raundli PBKDF2 hash (Django hasher) — bir raundli
    tuzsiz SHA-256 emas. Bazа sizib chiqса ham kodlar oson topilmaydi."""
    from django.contrib.auth.hashers import make_password
    return make_password((code or '').strip().lower())


def _legacy_sha256(code):
    return hashlib.sha256((code or '').strip().lower().encode('utf-8')).hexdigest()


def use_recovery_code(user, code):
    """If `code` matches an unused recovery hash, consume it and return True.
    Yangi (PBKDF2) va eski (SHA-256) formatларни ham qo'llab-quvvatlaydi."""
    from django.contrib.auth.hashers import check_password
    norm = (code or '').strip().lower()
    codes = list(user.recovery_codes or [])
    for stored in codes:
        # Eski format = 64 belgili sof hex; yangisi = 'pbkdf2_sha256$...'
        if len(stored) == 64 and all(c in '0123456789abcdef' for c in stored):
            ok = (stored == _legacy_sha256(code))
        else:
            ok = check_password(norm, stored)
        if ok:
            codes.remove(stored)
            user.recovery_codes = codes
            user.save(update_fields=['recovery_codes'])
            return True
    return False
