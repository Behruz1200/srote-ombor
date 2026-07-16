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
    code = (code or '').strip().replace(' ', '')
    if not secret_b32 or not code.isdigit():
        return False
    now = time.time()
    return any(_totp_at(secret_b32, now + w * 30) == code
               for w in range(-window, window + 1))


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
    """Return n human-friendly one-time codes (plaintext, shown once)."""
    return ['-'.join(secrets.token_hex(2) for _ in range(2)) for _ in range(n)]


def hash_code(code):
    return hashlib.sha256((code or '').strip().lower().encode('utf-8')).hexdigest()


def use_recovery_code(user, code):
    """If `code` matches an unused recovery hash, consume it and return True."""
    h = hash_code(code)
    codes = list(user.recovery_codes or [])
    if h in codes:
        codes.remove(h)
        user.recovery_codes = codes
        user.save(update_fields=['recovery_codes'])
        return True
    return False
