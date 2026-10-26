"""生成 assets/icon.png 与 assets/killapp.ico。

用法：python tools/make_icon.py
只需要在制作图标时运行一次，运行时和打包都不依赖 Pillow。
"""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

SIZE = 256
BG_TOP = (37, 99, 235)      # 蓝
BG_BOTTOM = (14, 34, 84)     # 深蓝
BAR = (236, 244, 255)
BAR_DIM = (147, 178, 232)
DANGER = (239, 68, 68)


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def make_png():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 背景：竖向渐变 + 圆角
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel(
            (0, y),
            tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    grad = grad.resize((SIZE, SIZE))
    img.paste(grad, (0, 0), rounded_mask(SIZE, 56))

    d = ImageDraw.Draw(img)

    # 标题栏
    d.rounded_rectangle((44, 44, 212, 74), radius=12, fill=BAR_DIM)
    d.ellipse((56, 52, 68, 64), fill=DANGER)
    d.ellipse((76, 52, 88, 64), fill=(250, 204, 21))
    d.ellipse((96, 52, 108, 64), fill=(34, 197, 94))

    # 进程列表：三根长度不一的条
    for i, (top, width) in enumerate(
        [(96, 150), (128, 120), (160, 138)]
    ):
        color = BAR if i == 0 else BAR_DIM
        d.rounded_rectangle((44, top, 44 + width, top + 18), radius=9, fill=color)

    # 右下角的「结束」标记
    d.ellipse((146, 152, 218, 224), fill=DANGER)
    d.line((168, 174, 196, 202), fill=(255, 255, 255), width=12)
    d.line((196, 174, 168, 202), fill=(255, 255, 255), width=12)

    return img


def main():
    os.makedirs(ASSETS, exist_ok=True)
    img = make_png()
    png_path = os.path.join(ASSETS, "icon.png")
    img.save(png_path)

    ico_path = os.path.join(ASSETS, "killapp.ico")
    img.save(
        ico_path,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("生成完成:")
    print(" ", png_path)
    print(" ", ico_path)


if __name__ == "__main__":
    main()
