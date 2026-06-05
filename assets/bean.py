"""Generate every countbeans avatar asset.

Pure pixel art: a 40x40 grid is drawn procedurally, then upscaled with
nearest-neighbour so the pixels stay hard-edged at any size. Run with:

    python bean.py

All PNGs are written next to this script.
"""

import os

import numpy as np
from PIL import Image, ImageDraw

W = H = 40
SCALE = 18  # base render = 40 * 18 = 720px

OUT = os.path.dirname(os.path.abspath(__file__))  # write assets beside this file


def path(name):
    return os.path.join(OUT, name)


# warm / earthy palette
PAL = {
    "bg": (242, 226, 196),  # warm cream
    "bg2": (233, 213, 178),  # cream shade (vignette)
    "frame": (120, 86, 54),  # border frame
    "body": (168, 94, 59),  # bean reddish-brown
    "bodyhi": (201, 130, 84),  # bean highlight
    "bodylo": (122, 64, 38),  # bean shadow
    "out": (74, 41, 24),  # dark outline
    "coin": (232, 184, 75),  # gold
    "coinhi": (245, 214, 130),  # gold highlight
    "coinlo": (196, 144, 47),  # gold shade
    "coinout": (110, 74, 18),  # coin outline
    "eye": (44, 28, 20),
    "white": (255, 248, 235),
    "blush": (226, 142, 120),
}

img = np.zeros((H, W, 4), dtype=np.uint8)


def put(x, y, c, a=255):
    if 0 <= x < W and 0 <= y < H:
        r, g, b = PAL[c]
        img[y, x] = (r, g, b, a)


def disc(cx, cy, r):
    pts = set()
    rr = r * r
    for y in range(H):
        for x in range(W):
            if (x - cx) ** 2 + (y - cy) ** 2 <= rr:
                pts.add((x, y))
    return pts


def ellipse(cx, cy, rx, ry):
    pts = set()
    for y in range(H):
        for x in range(W):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                pts.add((x, y))
    return pts


# ---------- background: cream with soft round vignette + frame ----------
for y in range(H):
    for x in range(W):
        put(x, y, "bg")
# subtle darker corners (vignette toward edges)
cx, cy = (W - 1) / 2, (H - 1) / 2
maxd = ((cx) ** 2 + (cy) ** 2) ** 0.5
for y in range(H):
    for x in range(W):
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if d > maxd * 0.74:
            put(x, y, "bg2")
# thin frame border
for i in range(W):
    put(i, 0, "frame")
    put(i, H - 1, "frame")
    put(0, i, "frame")
    put(W - 1, i, "frame")
    put(i, 1, "frame")
    put(i, H - 2, "frame")
    put(1, i, "frame")
    put(W - 2, i, "frame")

# ---------- bean body (kidney shape = rounded ellipse with soft side dents) ----------
body = ellipse(20, 20, 11.5, 12.0)
# soft symmetric kidney concaves on the sides, rounded top (no pointy bits)
notch = disc(34, 19, 6.0)
notch |= disc(6, 19, 6.0)
body = body - notch
body = {p for p in body if p[1] >= 9}  # flatten the rounded apex

# legs (little stubs)
legs = set()
for lx in (16, 17, 23, 24):
    for ly in (32, 33, 34):
        legs.add((lx, ly))
body |= legs

# arms reaching out to coins (lower so they don't collide with the cheeks)
arm_l = {(11, 27), (10, 28), (9, 28)}
arm_r = {(29, 27), (30, 28), (31, 28)}
body |= arm_l | arm_r

# draw body fill
for x, y in body:
    put(x, y, "body")

# shading: soft top highlight, symmetric bottom shadow band
for x, y in body:
    if (x - 20) ** 2 + (y - 11) ** 2 <= 5**2:
        put(x, y, "bodyhi")
    if y >= 27:
        put(x, y, "bodylo")

# outline: any body pixel adjacent to non-body
bset = set(body)
for x, y in list(bset):
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        if (x + dx, y + dy) not in bset:
            # only outline against background, keep inner shape
            pass
# build outline ring by expanding
outline = set()
for x, y in bset:
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            n = (x + dx, y + dy)
            if n not in bset:
                outline.add(n)
for x, y in outline:
    put(x, y, "out")

# ---------- coins ----------
# clean hand-tuned round coin mask (relative to centre), radius ~4.5
COIN = set()
for y in range(-5, 6):
    for x in range(-5, 6):
        if x * x + y * y <= 16:  # rounder, slightly smaller coin
            COIN.add((x, y))


def draw_coin(cx, cy):
    c = {(cx + dx, cy + dy) for (dx, dy) in COIN}
    ring = set()
    for x, y in c:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (x + dx, y + dy)
                if n not in c:
                    ring.add(n)
    for x, y in ring:
        put(x, y, "coinout")
    for x, y in c:
        put(x, y, "coin")
    # top-left highlight, bottom-right shade
    for dx, dy in COIN:
        if (dx + 2) ** 2 + (dy + 2) ** 2 <= 4:
            put(cx + dx, cy + dy, "coinhi")
        if (dx - 2) ** 2 + (dy - 2) ** 2 <= 4:
            put(cx + dx, cy + dy, "coinlo")
    # '$' mark
    put(cx, cy - 2, "coinlo")
    put(cx, cy - 1, "coinlo")
    put(cx, cy, "coinlo")
    put(cx, cy + 1, "coinlo")
    put(cx, cy + 2, "coinlo")
    put(cx - 1, cy - 1, "coinlo")
    put(cx + 1, cy + 1, "coinlo")


draw_coin(8, 30)
draw_coin(32, 30)

# ---------- face ----------
# eyes
for ex in (16, 24):
    for dx in (0, 1):
        for dy in (0, 1):
            put(ex + dx, 19 + dy, "eye")
    put(ex, 19, "white")  # sparkle
# blush
for bx in (13, 27):
    put(bx, 23, "blush")
    put(bx + 1, 23, "blush")
    put(bx, 24, "blush")
    put(bx + 1, 24, "blush")
# smile (small arc)
smile = [(18, 24), (19, 25), (20, 25), (21, 25), (22, 24)]
for x, y in smile:
    put(x, y, "eye")

# ---------- upscale nearest neighbor ----------
pim = Image.fromarray(img, "RGBA")
big = pim.resize((W * SCALE, H * SCALE), Image.NEAREST)
big.save(path("countbeans_pfp.png"))

# transparent version (no bg/frame): rebuild without background
img2 = img.copy()
# make any bg/bg2/frame pixel transparent
for y in range(H):
    for x in range(W):
        px = tuple(img2[y, x][:3])
        if px in (PAL["bg"], PAL["bg2"], PAL["frame"]):
            img2[y, x] = (0, 0, 0, 0)
trans = Image.fromarray(img2, "RGBA").resize((W * SCALE, H * SCALE), Image.NEAREST)
trans.save(path("countbeans_pfp_transparent.png"))

# ---------- extra exports ----------
# 512px square (framed) + 512px transparent
big.resize((512, 512), Image.NEAREST).save(path("countbeans_pfp_512.png"))
trans.resize((512, 512), Image.NEAREST).save(path("countbeans_pfp_transparent_512.png"))


# ---------- circular avatar (Telegram round mask) ----------
def make_circle(size):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    pad = max(2, size // 90)
    # cream disc
    d.ellipse([pad, pad, size - 1 - pad, size - 1 - pad], fill=PAL["bg"] + (255,))
    # paste the bean (transparent sprite), scaled to ~82% and centred a touch high
    bw = int(size * 0.82) // SCALE * SCALE  # keep an integer pixel multiple → crisp
    sprite = Image.fromarray(img2, "RGBA").resize((bw, bw), Image.NEAREST)
    off = ((size - bw) // 2, (size - bw) // 2 - size // 40)
    canvas.alpha_composite(sprite, off)
    # circular frame ring on top
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    w = max(3, size // 22)
    rd.ellipse(
        [pad, pad, size - 1 - pad, size - 1 - pad],
        outline=PAL["frame"] + (255,),
        width=w,
    )
    canvas.alpha_composite(ring)
    # hard circular clip for a clean edge
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([pad, pad, size - 1 - pad, size - 1 - pad], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


make_circle(720).save(path("countbeans_pfp_circle.png"))
make_circle(512).save(path("countbeans_pfp_circle_512.png"))
print("Wrote 6 PNGs to", OUT)
