"""Generate PWA icons for yurit.

Produces:
- static/img/icon-192.png   (Android home screen)
- static/img/icon-512.png   (Android splash / install)
- static/img/icon-180.png   (Apple touch icon)
- static/img/maskable-512.png (Android adaptive — safe zone with padding)
- static/img/favicon.png    (32x32 browser tab)

Run once: ./venv/bin/python scripts/gen_pwa_icons.py
"""
import os
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / 'static' / 'img'
OUT.mkdir(parents=True, exist_ok=True)


def find_font(size):
    """Find a system font that supports Latin."""
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Black.ttf',
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/System/Library/Fonts/Avenir Next.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_icon(size, padding_ratio=0.0, output_name=None):
    """Create square icon. padding_ratio adds inner padding (for maskable)."""
    img = Image.new('RGB', (size, size), color='#0d6efd')
    draw = ImageDraw.Draw(img)
    # Background gradient simulation (solid blue with darker frame on maskable)
    if padding_ratio > 0:
        # Maskable: leave safe zone (inner 80%)
        inner = int(size * 0.8)
        pad = (size - inner) // 2
        # Inner darker background
        draw.rounded_rectangle(
            [pad, pad, size - pad, size - pad],
            radius=int(inner * 0.15),
            fill='#0a58ca',
        )
        text_box_size = inner
        offset = pad
    else:
        # Rounded corners visually (iOS will apply its own mask)
        draw.rounded_rectangle(
            [0, 0, size, size],
            radius=int(size * 0.22),
            fill='#0d6efd',
        )
        text_box_size = size
        offset = 0

    # "S" letter centered
    font_size = int(text_box_size * 0.6)
    font = find_font(font_size)
    text = 'S'
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = offset + (text_box_size - text_w) // 2 - bbox[0]
    y = offset + (text_box_size - text_h) // 2 - bbox[1]
    draw.text((x, y), text, fill='white', font=font)

    # Small "rote" tag below for larger icons
    if size >= 192 and padding_ratio == 0:
        small_size = int(size * 0.08)
        small_font = find_font(small_size)
        tag = 'rote'
        tbbox = draw.textbbox((0, 0), tag, font=small_font)
        tw = tbbox[2] - tbbox[0]
        tx = (size - tw) // 2 - tbbox[0]
        ty = y + text_h + int(size * 0.02)
        draw.text((tx, ty), tag, fill='white', font=small_font)

    out_path = OUT / (output_name or f'icon-{size}.png')
    img.save(out_path, 'PNG', optimize=True)
    print(f'  wrote {out_path.relative_to(BASE)}  ({size}x{size})')


make_icon(192)
make_icon(512)
make_icon(180)
make_icon(512, padding_ratio=0.2, output_name='maskable-512.png')
make_icon(32, output_name='favicon.png')
print('Done.')
