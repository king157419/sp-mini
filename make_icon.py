"""画周历的图标（纸墨风格）：暖白纸色的圆角方块，顶上一条朱砂红，中间一个思源宋体的「历」。
运行：python make_icon.py  → 生成 icon.ico（多尺寸）和 icon.png（预览）。"""
from PIL import Image, ImageDraw, ImageFont

PAPER, LINE, RED, INK = "#FBFAF7", "#E2DDD3", "#B8452F", "#1D1C1A"
FONT = "C:/Windows/Fonts/NotoSerifSC-VF.ttf"


def draw(S=1024):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m, r = int(S * .06), int(S * .2)
    box = (m, m, S - m, S - m)
    d.rounded_rectangle(box, r, fill=PAPER, outline=LINE, width=max(2, S // 96))
    # 顶上一条朱砂：先画整块红色圆角，再用纸色盖住下半截
    band = int(S * .30)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, r, fill=255)
    red = Image.new("RGBA", (S, S), RED)
    cut = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cut).rectangle((0, 0, S, m + band), fill=255)
    from PIL import ImageChops
    im.paste(red, (0, 0), ImageChops.multiply(mask, cut))
    # 两个装订孔
    for cx in (.34, .66):
        x, y, rr = int(S * cx), int(m + band * .5), int(S * .035)
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=PAPER)
    # 「历」
    f = ImageFont.truetype(FONT, int(S * .52))
    try:
        f.set_variation_by_name("Bold")
    except Exception:
        pass
    cy = (m + band + S - m) / 2 + S * .01
    d.text((S / 2, cy), "历", font=f, fill=INK, anchor="mm")
    return im


if __name__ == "__main__":
    big = draw()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save("icon.ico", sizes=[(s, s) for s in sizes])
    big.resize((256, 256), Image.LANCZOS).save("icon.png")
    print("ok")
