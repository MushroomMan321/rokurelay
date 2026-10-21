"""Generate the Roku channel's icon and splash PNGs (needs Pillow).

    python tools/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "roku-channel" / "images"
BG = (11, 15, 20)
ACCENT = (63, 167, 255)
TEXT = (230, 237, 243)
MUTED = (138, 160, 180)


def font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def draw_mark(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float) -> None:
    """A TV with cast waves in the lower-left corner."""
    w, h = 1.6 * s, 1.0 * s
    lw = max(2, int(s * 0.07))
    x0, y0 = cx - w / 2, cy - h / 2
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=s * 0.1, outline=TEXT, width=lw)
    d.line([cx - s * 0.3, y0 + h + s * 0.14, cx + s * 0.3, y0 + h + s * 0.14], fill=TEXT, width=lw)
    ox, oy = x0 + s * 0.18, y0 + h - s * 0.16
    d.ellipse([ox - s * 0.06, oy - s * 0.06, ox + s * 0.06, oy + s * 0.06], fill=ACCENT)
    for r in (0.28, 0.5):
        d.arc([ox - s * r, oy - s * r, ox + s * r, oy + s * r], start=270, end=360, fill=ACCENT, width=lw)


def icon(size: tuple[int, int], name: str) -> None:
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    s = h * 0.36
    draw_mark(d, w / 2, h * 0.40, s)
    f = font(int(h * 0.12))
    label = "RokuRelay"
    tw = d.textlength(label, font=f)
    d.text(((w - tw) / 2, h * 0.76), label, font=f, fill=TEXT)
    img.save(OUT / name)


def splash(size: tuple[int, int], name: str) -> None:
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    draw_mark(d, w / 2, h * 0.42, h * 0.16)
    f, small = font(int(h * 0.05)), font(int(h * 0.025))
    for text, fnt, y, color in (("RokuRelay", f, 0.62, TEXT), ("Cast from your phone", small, 0.70, MUTED)):
        tw = d.textlength(text, font=fnt)
        d.text(((w - tw) / 2, h * y), text, font=fnt, fill=color)
    img.save(OUT / name)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    icon((540, 405), "icon_focus_fhd.png")
    icon((290, 218), "icon_focus_hd.png")
    icon((246, 140), "icon_focus_sd.png")
    splash((1920, 1080), "splash_fhd.png")
    splash((1280, 720), "splash_hd.png")
    splash((720, 480), "splash_sd.png")
    print("wrote", ", ".join(p.name for p in sorted(OUT.glob("*.png"))))
