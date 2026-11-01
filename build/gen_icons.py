#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 SPK 与 DSM 桌面图标（需要 Pillow；仅在开发/构建机使用）。
运行：python build/gen_icons.py
输出：spk/PACKAGE_ICON.PNG, spk/PACKAGE_ICON_256.PNG, spk/ui/images/icon_{size}.png
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024
TEAL_TOP = (18, 104, 164, 255)
TEAL_BOTTOM = (10, 160, 148, 255)
WHITE = (255, 255, 255, 255)
PALE = (221, 245, 244, 255)
INK = (12, 94, 122, 255)
AMBER = (255, 196, 88, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))


def make_master():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景圆角方块 + 纵向渐变
    radius = int(S * 0.22)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=radius, fill=255)
    grad = Image.new("RGBA", (1, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.point((0, y), fill=lerp(TEAL_TOP, TEAL_BOTTOM, y / max(1, S - 1)))
    grad = grad.resize((S, S))
    img.paste(grad, (0, 0), mask)

    # 后层文件（倾斜一点的白色卡片）
    back = [(250, 300), (610, 300), (760, 452), (760, 760), (250, 760)]
    d.polygon(back, fill=(224, 246, 246, 255), outline=(255, 255, 255, 200))
    # 前层文件
    front = [(330, 380), (650, 380), (790, 520), (790, 830), (330, 830)]
    d.polygon(front, fill=WHITE, outline=WHITE)
    # 折角
    d.polygon([(650, 380), (790, 520), (650, 520)], fill=(206, 238, 238, 255))
    # 文本行
    for i, y in enumerate((590, 645, 700)):
        w = (770 - 410) if i < 2 else 250
        d.rounded_rectangle((410, y, 410 + w, y + 24), radius=12, fill=PALE if i == 2 else (150, 205, 214, 255))
    # 放大镜（重复检测）
    cx, cy, r = 680, 700, 92
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=AMBER, width=34)
    d.line((cx + 66, cy + 66, cx + 148, cy + 148), fill=AMBER, width=40)
    # 保留勾选徽标
    d.ellipse((500, 430, 650, 580), fill=(22, 163, 106, 255), outline=WHITE, width=12)
    d.line(((538, 505), (575, 542), (622, 470)), fill=WHITE, width=26, joint="curve")
    # 闪光
    for (x, y, l) in ((390, 950, 0), (940, 330, 1), (860, 930, 2)):
        pass
    return img


def main():
    master = make_master()
    out_dirs = [ROOT / "spk" / "ui" / "images"]
    for p in out_dirs:
        p.mkdir(parents=True, exist_ok=True)
    # 桌面图标模板尺寸
    for size in (16, 24, 32, 48, 64, 72, 256):
        icon = master.resize((size, size), Image.LANCZOS)
        icon.save(ROOT / "spk" / "ui" / "images" / ("icon_%d.png" % size), "PNG", optimize=True)
    # 包中心图标
    master.resize((256, 256), Image.LANCZOS).save(ROOT / "spk" / "PACKAGE_ICON_256.PNG", "PNG", optimize=True)
    master.resize((64, 64), Image.LANCZOS).save(ROOT / "spk" / "PACKAGE_ICON.PNG", "PNG", optimize=True)
    print("icons generated")


if __name__ == "__main__":
    main()
