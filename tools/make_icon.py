"""生成程序图标（assets/gamelens.ico）。"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def rounded(draw: ImageDraw.ImageDraw, box, r, fill):
    draw.rounded_rectangle(box, radius=r, fill=fill)


def build(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(size * 0.06)
    rounded(d, (pad, pad, size - pad, size - pad), int(size * 0.22), (16, 19, 26, 255))
    # 中文"译"
    font = None
    for name in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                 "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(name, int(size * 0.50))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "译"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - size * 0.035),
           text, font=font, fill=(232, 236, 243, 255))
    # 底部强调条 + 右边小圆点
    bar_y = size - pad - int(size * 0.20)
    rounded(d, (pad + int(size * 0.16), bar_y, size - pad - int(size * 0.16),
                bar_y + int(size * 0.035)), int(size * 0.02), (76, 141, 255, 255))
    return img


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/gamelens.ico")
    out.parent.mkdir(parents=True, exist_ok=True)
    base = build(512)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    base.save(out, format="ICO", sizes=sizes)
    base.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
    print("saved", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
