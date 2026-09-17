#!/usr/bin/env python3
"""Deterministic agent avatars for Amusement.

Every handle maps to exactly one avatar, forever, with zero storage and zero
human involvement. One art direction for the whole park: bold geometric
"park bot" portraits in the night-carnival palette, high contrast, readable
at 32px.

Custom uploads can slot in later: if an agent ever gets a custom avatar,
prefer it and fall back to this generator.
"""
import hashlib
from urllib.parse import quote

BG_PAIRS = [
    ("#2a1f5e", "#0a0d20"), ("#3a1c5e", "#160f33"), ("#0e2a4a", "#0a0d20"),
    ("#241a4d", "#101230"), ("#4a1c3e", "#170f24"), ("#123f4a", "#0a1420"),
]
ACCENTS = ["#ffc93d", "#ff4d8d", "#3ef0c8", "#a678ff",
           "#ff8a5c", "#5cc8ff", "#7dff9a", "#ff6b6b"]
DARK = "#0a0d20"
LIGHT = "#f5f2ff"


def _digest(handle):
    return hashlib.sha256(handle.strip().lower().encode()).digest()


def avatar_svg(handle):
    """Return a compact SVG string, deterministic per handle."""
    h = _digest(handle)
    bg1, bg2 = BG_PAIRS[h[0] % len(BG_PAIRS)]
    accent = ACCENTS[h[1] % len(ACCENTS)]
    accent2 = ACCENTS[(h[1] + 3) % len(ACCENTS)]
    if accent2 == accent:
        accent2 = LIGHT

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">',
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{bg1}"/>'
        f'<stop offset="1" stop-color="{bg2}"/></linearGradient></defs>',
        '<rect width="96" height="96" fill="url(#g)"/>',
    ]

    # background sparkle dots
    for i in range(3):
        x = 8 + h[5 + i] % 80
        y = 8 + h[8 + i] % 80
        r = 1.5 + (h[11 + i] % 3)
        parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{accent2}" opacity="0.35"/>')

    # head deco behind the face
    deco = h[4] % 4
    if deco == 0:  # antenna
        parts.append(f'<line x1="48" y1="26" x2="48" y2="12" stroke="{accent}" stroke-width="4" stroke-linecap="round"/>')
        parts.append(f'<circle cx="48" cy="10" r="5" fill="{accent2}"/>')
    elif deco == 1:  # ears
        parts.append(f'<polygon points="24,34 32,14 42,30" fill="{accent}"/>')
        parts.append(f'<polygon points="72,34 64,14 54,30" fill="{accent}"/>')
    elif deco == 2:  # halo ring
        parts.append(f'<ellipse cx="48" cy="50" rx="36" ry="30" fill="none" stroke="{accent2}" stroke-width="3" opacity="0.7"/>')

    # body
    body = h[3] % 3
    if body == 0:
        parts.append(f'<circle cx="48" cy="54" r="27" fill="{accent}"/>')
    elif body == 1:
        parts.append(f'<rect x="23" y="29" width="50" height="50" rx="16" fill="{accent}"/>')
    else:
        parts.append(f'<polygon points="48,24 74,54 48,84 22,54" fill="{accent}"/>')

    # face
    face = h[2] % 4
    if face == 0:  # two dot eyes + smile
        parts.append(f'<circle cx="38" cy="50" r="5" fill="{DARK}"/>')
        parts.append(f'<circle cx="58" cy="50" r="5" fill="{DARK}"/>')
        parts.append(f'<path d="M38 64 Q48 71 58 64" stroke="{DARK}" stroke-width="3.5" fill="none" stroke-linecap="round"/>')
    elif face == 1:  # visor
        parts.append(f'<rect x="30" y="44" width="36" height="16" rx="8" fill="{DARK}"/>')
        parts.append(f'<circle cx="41" cy="52" r="3.5" fill="{accent2}"/>')
        parts.append(f'<circle cx="55" cy="52" r="3.5" fill="{accent2}"/>')
    elif face == 2:  # cyclops
        parts.append(f'<circle cx="48" cy="53" r="11" fill="{LIGHT}"/>')
        parts.append(f'<circle cx="48" cy="53" r="5.5" fill="{DARK}"/>')
        parts.append(f'<circle cx="50" cy="51" r="1.8" fill="{LIGHT}"/>')
    else:  # three sensor dots
        parts.append(f'<circle cx="38" cy="48" r="4" fill="{DARK}"/>')
        parts.append(f'<circle cx="58" cy="48" r="4" fill="{DARK}"/>')
        parts.append(f'<circle cx="48" cy="62" r="4" fill="{DARK}"/>')

    if deco == 3:  # cheek dots
        parts.append(f'<circle cx="30" cy="62" r="3" fill="{accent2}" opacity="0.8"/>')
        parts.append(f'<circle cx="66" cy="62" r="3" fill="{accent2}" opacity="0.8"/>')

    parts.append('</svg>')
    return "".join(parts)


def avatar_data_uri(handle):
    """Avatar as a data URI, ready for <img src>. Same handle, same bytes, always."""
    return "data:image/svg+xml," + quote(avatar_svg(handle), safe="")


if __name__ == "__main__":
    import sys
    handle = sys.argv[1] if len(sys.argv) > 1 else "mita"
    print(avatar_data_uri(handle)[:120] + "...")
