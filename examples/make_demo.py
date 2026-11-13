#!/usr/bin/env python3
"""Build the synthetic demo clip (no copyrighted footage): four mock ad cards rendered with Pillow,
animated with ffmpeg, so `desk.py ingest` has something to chew on.

  python3 examples/make_demo.py [out.mp4]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 720, 1280
FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",     # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Debian / Ubuntu
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",               # Arch
    "C:/Windows/Fonts/arialbd.ttf",                           # Windows
)
FONTS_PLAIN = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in (FONTS if bold else FONTS_PLAIN):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def bottle(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float, body=(238, 214, 176), cap=(70, 60, 52)):
    w, h = int(190 * scale), int(420 * scale)
    draw.rounded_rectangle([cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2], radius=int(46 * scale), fill=body)
    draw.rounded_rectangle([cx - w // 3, cy - h // 2 + int(20 * scale), cx + w // 3, cy - h // 2 + int(120 * scale)],
                           radius=int(18 * scale), fill=(255, 252, 245))
    draw.rounded_rectangle([cx - w // 4, cy - h // 2 - int(52 * scale), cx + w // 4, cy - h // 2 + int(26 * scale)],
                           radius=int(14 * scale), fill=cap)
    label = font(int(34 * scale))
    draw.text((cx, cy + int(30 * scale)), "GLOW", font=label, fill=(90, 74, 58), anchor="mm")
    draw.text((cx, cy + int(80 * scale)), "cleansing balm", font=font(int(20 * scale), False), fill=(120, 104, 88), anchor="mm")


def scene_product(path: Path) -> None:
    img = gradient((236, 226, 210), (206, 190, 168))
    draw = ImageDraw.Draw(img)
    draw.ellipse([90, 780, 630, 940], fill=(196, 178, 156))
    bottle(draw, 360, 640, 1.0)
    draw.text((60, 120), "Melts makeup", font=font(64), fill=(58, 48, 40))
    draw.text((60, 200), "in one gentle wash", font=font(48, False), fill=(96, 82, 68))
    img.save(path)


def scene_texture(path: Path) -> None:
    img = gradient((250, 246, 240), (226, 216, 206))
    draw = ImageDraw.Draw(img)
    for i, colour in enumerate([(238, 214, 176), (246, 232, 208), (214, 190, 158)]):
        draw.ellipse([90 + i * 190, 520, 300 + i * 190, 730], fill=colour)
    draw.text((70, 300), "Balm to milk", font=font(70), fill=(52, 44, 38))
    draw.text((70, 400), "no tugging", font=font(56, False), fill=(104, 90, 76))
    img.save(path)


def scene_compare(path: Path) -> None:
    img = gradient((242, 236, 228), (222, 212, 200))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 640, W, 660], fill=(255, 255, 255))
    draw.text((60, 120), "Before", font=font(54), fill=(120, 96, 80))
    draw.text((430, 120), "After", font=font(54), fill=(96, 120, 100))
    for cx, skin in ((180, (226, 190, 172)), (540, (238, 214, 196))):
        draw.ellipse([cx - 130, 700, cx + 130, 1040], fill=skin)
        draw.ellipse([cx - 60, 760, cx - 30, 790], fill=(80, 64, 58))
        draw.ellipse([cx + 30, 760, cx + 60, 790], fill=(80, 64, 58))
    img.save(path)


def scene_end(path: Path) -> None:
    img = gradient((44, 52, 66), (24, 30, 40))
    draw = ImageDraw.Draw(img)
    bottle(draw, 360, 560, 0.8, body=(255, 246, 230), cap=(210, 202, 190))
    draw.text((360, 860), "GLOW", font=font(96), fill=(255, 250, 240), anchor="mm")
    draw.text((360, 950), "Shop the set", font=font(44, False), fill=(226, 218, 206), anchor="mm")
    img.save(path)


def ken_burns(src: Path, out: Path, seconds: float, zoom: str = "in") -> None:
    frames = int(seconds * 30)
    expr = (f"zoom+0.0009" if zoom == "in" else f"max(1.0,1.06-0.0009*on)")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(src),
                    "-vf", f"scale={W * 2}:{H * 2},zoompan=z='{expr}':d={frames}:x='iw/2-(iw/zoom/2)':"
                           f"y='ih/2-(ih/zoom/2)':s={W}x{H},fps=30,format=yuv420p",
                    "-frames:v", str(frames), "-c:v", "libx264", "-crf", "20", str(out)], check=True)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "examples/demo/clip.mp4").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    cards = {name: tmp / f"{name}.png" for name in ("product", "texture", "compare", "end")}
    scene_product(cards["product"])
    scene_texture(cards["texture"])
    scene_compare(cards["compare"])
    scene_end(cards["end"])
    parts = []
    for name, seconds, zoom in (("product", 5.5, "in"), ("texture", 5.0, "out"), ("compare", 5.5, "in"), ("end", 4.5, "in")):
        part = tmp / f"{name}.mp4"
        ken_burns(cards[name], part, seconds, zoom)
        parts.append(part)
    listing = tmp / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", str(out)], check=True)
    print(out)


if __name__ == "__main__":
    main()
