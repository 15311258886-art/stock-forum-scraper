# -*- coding: utf-8 -*-
"""把一张图片加工成多尺寸 Windows 图标（.ico），用于桌面快捷方式。

用法:
    python make_icon.py <src_image> <out_dir>

产出（out_dir 下）:
    stock_viewer.ico            徽章版（金边·主推）：圆角金色渐变底 + 抠图人物
    stock_viewer_blue.ico       徽章版（旧蓝边·备份）：圆角深蓝渐变底 + 抠图人物
    stock_viewer_prev_blue.ico  首次改造前的原始 .ico 自动备份（仅备份一次）
    stock_viewer_outlook.ico    A 暗角版：保留原画面，四周虚化压暗、中心聚光
    stock_viewer_clear.ico      C 透明版：抠图人物 + 透明底
    preview_compare.png         对比预览（上排 256px 大图 / 下排深色桌面实景模拟）

抠图算法（纯 Pillow，无第三方依赖）——两步法：
  ① 严格天空色 + 从画面四边洪泛 → 得到「与外边界连通的天空」；
  ② 取①的补集（人物候选，含被头发围住的封闭天空碎片），从画面中下部一点洪泛，
     只保留与主体连通的那块 → 封闭天空碎片被自动剔除，而人物身上的深蓝内衬
     （不连通到边界）被保留。
  之后 1px 腐蚀（去掉抗锯齿渗色）+ 高斯羽化。

配色切换（外框/底/描边）:
  * version_badge(sq, PALETTE_GOLD)  金边（默认，主推）
  * version_badge(sq, PALETTE_BLUE)  蓝边（旧版）
  只改色板字典即可，不改动抠图与排版逻辑。

调参提示（换图后按需改）:
  * SQ_SEED 主体种子点比例；若人物不在画面中下部，调它。
  * is_sky() 的天空色判据；偏灰/偏紫的天空放宽 (b-r) 阈值。
"""
import io
import os
import shutil
import sys
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

SQ = 480          # 从原图裁出的正方形边长
OUT = 512         # 最终画布边长
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
SQ_SEED = (0.50, 0.74)      # 主体种子点（相对 SQ 的比例，须落在人物身上）
RES = Image.Resampling.LANCZOS

# ---------------------------------------------------------------- 徽章版色板
# grad_top/grad_bot: 竖向渐变的上/下颜色；lift: 中心提亮的 RGB 倍率；
# glow: 人物轮廓柔光色；edge: 圆角描边 RGBA；edge_w: 描边宽度（512 基准）
PALETTE_GOLD = dict(              # 主推：明亮香槟金
    grad_top=(210, 174, 70),
    grad_bot=(148, 112, 34),
    lift=(1.12, 1.00, 0.62),
    glow=(242, 212, 132),
    edge=(255, 248, 216, 215),
    edge_w=3,
    fade=42,
)
PALETTE_GOLD_DARK = dict(         # 备选：暗金 / 古铜
    grad_top=(128, 96, 30),
    grad_bot=(66, 48, 16),
    lift=(1.25, 1.00, 0.42),
    glow=(220, 178, 92),
    edge=(252, 228, 172, 205),
    edge_w=3,
    fade=42,
)
PALETTE_BLUE = dict(
    grad_top=(17, 40, 72),        # 深蓝
    grad_bot=(4, 11, 22),
    lift=(0.60, 1.00, 1.50),
    glow=(54, 128, 204),
    edge=(158, 208, 248, 92),
    edge_w=1,
)


# ---------------------------------------------------------------- 基础工具
def load_square(src):
    """居中裁出正方形，竖直方向顶部对齐（保留发梢，人物脸偏上）。"""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    s = min(w, h)
    left = (w - s) // 2
    top = 0
    if h < s:
        s = h
        left = 0
    box = (left, top, left + s, top + s)
    im = im.crop(box)
    if im.size != (SQ, SQ):
        im = im.resize((SQ, SQ), RES)
    return im


def rounded_mask(size, radius, ss=4):
    m = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size * ss - 1, size * ss - 1), radius=radius * ss, fill=255)
    return m.resize((size, size), RES)


def vertical_gradient(size, top_rgb, bot_rgb):
    grad = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(int(top_rgb[i] + (bot_rgb[i] - top_rgb[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c)
    return grad


# ---------------------------------------------------------------- 抠图
def _is_sky(c):
    # 判据：天空 = 偏蓝 **且够亮**。人物身上的深蓝衣物同样偏蓝，但亮度低得多。
    #   天空 (71,152,199) luma≈133 / 浅蓝天 (74,146,194)≈130
    #   衣物 (61,77,113)  luma≈76   ← 靠亮度区分，不会误剔
    #   红马甲 (167,58,53)≈90、肤色 (222,180,170)≈191 但 b-r 为负 → 自然排除
    r, g, b = c[0], c[1], c[2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    return luma > 105 and (b - r) > 25


def _flood(ok, size, seeds):
    """在 ok(x,y)->bool 的谓词上，从 seeds 洪泛，返回 L 模式 mask。"""
    w, h = size
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    dq = deque()
    for (x, y) in seeds:
        if 0 <= x < w and 0 <= y < h and mp[x, y] == 0 and ok(x, y):
            mp[x, y] = 255
            dq.append((x, y))
    while dq:
        x, y = dq.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and mp[nx, ny] == 0 and ok(nx, ny):
                mp[nx, ny] = 255
                dq.append((nx, ny))
    return mask


def subject_alpha(sq):
    """返回人物 alpha（255=人物）。两步洪泛：边界天空 → 主体连通域。"""
    w, h = sq.size
    px = sq.load()
    sky_ok = lambda x, y: _is_sky(px[x, y])

    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] + \
             [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    bg = _flood(sky_ok, (w, h), border)

    cand = ImageChops.invert(bg)                 # 人物候选（含封闭天空碎片）
    cp = cand.load()
    keep_ok = lambda x, y: cp[x, y] > 128

    sx, sy = int(w * SQ_SEED[0]), int(h * SQ_SEED[1])
    if not keep_ok(sx, sy):                      # 种子落在背景上时就近找一点
        found = False
        for r in range(1, 90):
            for dx in range(-r, r + 1):
                for dy in (-r, r):
                    nx, ny = sx + dx, sy + dy
                    if 0 <= nx < w and 0 <= ny < h and keep_ok(nx, ny):
                        sx, sy, found = nx, ny, True
                        break
                if found:
                    break
            if found:
                break
    keep = _flood(keep_ok, (w, h), [(sx, sy)])

    keep = keep.filter(ImageFilter.MinFilter(3))   # 腐蚀 1px：去抗锯齿渗色
    keep = keep.filter(ImageFilter.GaussianBlur(1.1))
    return keep


def cut_subject(sq):
    """抠出人物并按 alpha 紧裁，返回 RGBA。"""
    sub = sq.convert("RGBA")
    sub.putalpha(subject_alpha(sq))
    bbox = sub.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if bbox:
        sub = sub.crop(bbox)
    return sub


def fade_square(alpha, px):
    """把 alpha 的四周向内渐隐 px 像素；用圆角矩形（同徽章比例），
    让人物边缘以圆角形状融入底色，避免露出直角暗边。"""
    w, h = alpha.size
    fm = Image.new("L", (w, h), 0)
    ImageDraw.Draw(fm).rounded_rectangle((px, px, w - px - 1, h - px - 1),
                                         radius=int(min(w, h) * 0.16), fill=255)
    fm = fm.filter(ImageFilter.GaussianBlur(px * 0.6))
    return ImageChops.multiply(alpha, fm)


def paste_subject(sub, canvas, scale, dy_ratio=-0.008, fade=0):
    """把 sub 等比缩放到 canvas*scale 并居中贴上（略微上移），返回 (贴图, ox, oy)。"""
    box = canvas.width
    target = int(box * scale)
    w, h = sub.size
    k = target / max(w, h)
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    s = sub.resize((nw, nh), RES)
    if fade:
        s = s.copy()
        s.putalpha(fade_square(s.getchannel("A"), fade))
    ox = (box - nw) // 2
    oy = (box - nh) // 2 + int(box * dy_ratio)
    oy = max(0, min(oy, box - nh))
    canvas.alpha_composite(s, (ox, oy))
    return s, ox, oy


# ---------------------------------------------------------------- A 暗角聚焦
def version_outlook(sq):
    base = sq.convert("RGB")
    blur = base.filter(ImageFilter.GaussianBlur(10))
    m = Image.new("L", base.size, 0)
    ImageDraw.Draw(m).ellipse((-55, -35, base.width + 55, base.height + 35), fill=255)
    m = m.filter(ImageFilter.GaussianBlur(44))
    out = Image.composite(base, blur, m)                    # 中心原图 / 四周虚化
    vig = m.point(lambda v: 58 + int(197 * (v / 255.0)))     # 暗角：中心 255 → 边缘 58
    out = ImageChops.multiply(out, Image.merge("RGB", (vig, vig, vig)))
    out = ImageEnhance.Sharpness(out).enhance(1.22)
    out = out.resize((OUT, OUT), RES).convert("RGBA")
    out.putalpha(rounded_mask(OUT, int(OUT * 0.175)))
    return out


# ---------------------------------------------------------------- B 徽章版
def version_badge(sq, pal=PALETTE_GOLD):
    sub = cut_subject(sq)
    # 底色：竖向渐变（金/蓝由色板决定），只留极轻的中心提亮，避免出现"亮块"
    base = vertical_gradient(OUT, pal["grad_top"], pal["grad_bot"]).convert("RGB")
    rad = Image.radial_gradient("L").resize((OUT, OUT), RES)      # 中心0 边缘255
    lift = rad.point(lambda v: int(20 * (1 - v / 255.0)))
    kr, kg, kb = pal["lift"]
    base = ImageChops.add(base, Image.merge("RGB", (
        lift.point(lambda v: min(255, int(v * kr))),
        lift.point(lambda v: min(255, int(v * kg))),
        lift.point(lambda v: min(255, int(v * kb)))))).convert("RGBA")

    # 人物轮廓外的极柔光晕（先铺，随后被人物本体覆盖）
    s, ox, oy = paste_subject(sub, Image.new("RGBA", (OUT, OUT), (0, 0, 0, 0)), 0.86,
                              fade=pal.get("fade", 16))
    gmask = Image.new("L", (OUT, OUT), 0)
    gmask.paste(s.getchannel("A").filter(ImageFilter.GaussianBlur(14))
                .point(lambda v: int(v * 0.22)), (ox, oy))
    base.paste(Image.new("RGB", (OUT, OUT), pal["glow"]), (0, 0), gmask)

    base.alpha_composite(s, (ox, oy))
    base.putalpha(rounded_mask(OUT, int(OUT * 0.175)))
    edge = Image.new("RGBA", (OUT, OUT), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((1, 1, OUT - 2, OUT - 2),
                                           radius=int(OUT * 0.175),
                                           outline=pal["edge"],
                                           width=pal.get("edge_w", 1))
    base.alpha_composite(edge)
    return base


# ---------------------------------------------------------------- C 透明版
def version_clear(sq):
    sub = cut_subject(sq)
    canvas = Image.new("RGBA", (OUT, OUT), (0, 0, 0, 0))
    paste_subject(sub, canvas, 0.88, fade=10)
    return canvas


# ---------------------------------------------------------------- ICO 容器
def save_ico(img, path, sizes=ICO_SIZES):
    """自写 ICO 容器：每档尺寸单独 LANCZOS 缩放，小尺寸加锐化。"""
    prepared = []
    for s in sizes:
        im = img.resize((s, s), RES)
        if s <= 32:
            im = im.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=2))
        prepared.append(im)

    pngs = []
    for im in prepared:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        pngs.append(buf.getvalue())

    n = len(prepared)
    header = bytearray()
    header += (0).to_bytes(2, "little")
    header += (1).to_bytes(2, "little")
    header += n.to_bytes(2, "little")
    offset = 6 + 16 * n
    entries = bytearray()
    for im, data in zip(prepared, pngs):
        w = im.width if im.width < 256 else 0
        h = im.height if im.height < 256 else 0
        entries += bytes([w, h, 0, 0])
        entries += (1).to_bytes(2, "little")
        entries += (32).to_bytes(2, "little")
        entries += len(data).to_bytes(4, "little")
        entries += offset.to_bytes(4, "little")
        offset += len(data)
    with open(path, "wb") as f:
        f.write(bytes(header) + bytes(entries) + b"".join(pngs))
    return path


# ---------------------------------------------------------------- 预览
def build_preview(versions, path, labels=("旧版：蓝色外边", "新版：金色外边（已应用）", "C 透明版")):
    """上排 256px 大图（透明底用棋盘格），下排模拟深色桌面实景（96px + 图标名）。"""
    from PIL import ImageDraw as D
    FONT = "C:/Windows/Fonts/msyhbd.ttc"
    try:
        f_lab = ImageFont.truetype(FONT, 17, index=0)
        f_desk = ImageFont.truetype(FONT, 14, index=0)
        f_small = ImageFont.truetype(FONT, 12, index=0)
    except Exception:
        f_lab = f_desk = f_small = ImageFont.load_default()

    pad, gap, cell = 26, 22, 256
    desk_h = 168
    W = pad * 2 + cell * 3 + gap * 2
    H = pad + 24 + cell + 46 + 20 + desk_h + pad
    canvas = Image.new("RGB", (W, H), (28, 30, 36))
    d = D.Draw(canvas)

    def checker(x0, y0, s):
        for i in range(0, s, 8):
            for j in range(0, s, 8):
                c = (62, 64, 72) if ((i // 8 + j // 8) % 2 == 0) else (50, 52, 60)
                d.rectangle((x0 + i, y0 + j, x0 + i + 7, y0 + j + 7), fill=c)

    for i, v in enumerate(versions):
        x0 = pad + i * (cell + gap)
        y0 = pad + 24
        checker(x0, y0, cell)
        big = v.resize((cell, cell), RES)
        canvas.paste(big, (x0, y0), big)
        d.text((x0 + 2, pad + 2), labels[i], fill=(232, 234, 240), font=f_lab)

    ys = pad + 24 + cell + 46
    d.text((pad, ys - 22), "深色桌面实景模拟（96px 图标 + 图标名）",
           fill=(170, 174, 186), font=f_small)
    for i, v in enumerate(versions):
        x0 = pad + i * (cell + gap)
        tile = Image.new("RGB", (cell, desk_h), (0, 0, 0))
        td = D.Draw(tile)
        for y in range(desk_h):
            t = y / (desk_h - 1)
            td.line([(0, y), (cell, y)],
                    fill=(int(24 + 18 * t), int(31 + 22 * t), int(48 + 26 * t)))
        ic = v.resize((96, 96), RES)
        tile.paste(ic, ((cell - 96) // 2, 12), ic)
        for li, seg in enumerate(["股票论坛可视", "化查看器"]):
            bb = td.textbbox((0, 0), seg, font=f_desk)
            td.text(((cell - (bb[2] - bb[0])) // 2, 118 + li * 20), seg, font=f_desk,
                    fill=(245, 246, 250), stroke_width=2, stroke_fill=(10, 12, 18))
        canvas.paste(tile, (x0, ys))

    canvas.save(path)
    return path


# ---------------------------------------------------------------- main
def main():
    src = sys.argv[1]
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)

    # 首次改造前自动备份原始 .ico（只备份一次，避免覆盖）
    main_ico = os.path.join(outdir, "stock_viewer.ico")
    bak_ico = os.path.join(outdir, "stock_viewer_prev_blue.ico")
    if os.path.exists(main_ico) and not os.path.exists(bak_ico):
        shutil.copy2(main_ico, bak_ico)
        print("[备份] 原始 stock_viewer.ico ->", bak_ico)

    sq = load_square(src)
    a = version_outlook(sq)
    gold = version_badge(sq, PALETTE_GOLD)            # 亮金（主推）
    gold_dark = version_badge(sq, PALETTE_GOLD_DARK)  # 暗金（备选）
    blue = version_badge(sq, PALETTE_BLUE)
    c = version_clear(sq)

    save_ico(gold, os.path.join(outdir, "stock_viewer.ico"))          # 主用：亮金边
    # 【重要】快捷方式真正指向的"活文件"，按配色命名。
    # 原因：Windows 资源管理器 / 酷呆桌面等桌面整理软件是**按图标文件路径**缓存位图的，
    # 同名文件只换内容时桌面往往不刷新（改了像没改）。换成按色命名后，
    # 每次改色 = 文件路径变化 = 缓存必然失效，桌面立刻生效。
    save_ico(gold, os.path.join(outdir, "stock_viewer_gold.ico"))
    save_ico(gold_dark, os.path.join(outdir, "stock_viewer_gold_dark.ico"))
    save_ico(blue, os.path.join(outdir, "stock_viewer_blue.ico"))     # 备份：旧蓝边
    save_ico(a, os.path.join(outdir, "stock_viewer_outlook.ico"))
    save_ico(c, os.path.join(outdir, "stock_viewer_clear.ico"))
    for name, im in (("stock_viewer.png", gold),
                     ("stock_viewer_gold_dark.png", gold_dark),
                     ("stock_viewer_blue.png", blue),
                     ("stock_viewer_outlook.png", a),
                     ("stock_viewer_clear.png", c)):
        im.resize((OUT, OUT), RES).save(os.path.join(outdir, name))
    build_preview([blue, gold, gold_dark], os.path.join(outdir, "preview_compare.png"),
                  labels=("旧版：蓝色外边", "新版：金色外边（已应用）", "备选：暗金外边"))

    # 自检（数值兜底，便于无人看图时判断抠图是否合理）
    alpha_src = subject_alpha(sq)
    tot = sq.width * sq.height
    fg = sum(v for i, v in enumerate(alpha_src.histogram()) if i > 8)
    for tag, im in (("A", a), ("GOLD", gold), ("GOLD_DARK", gold_dark),
                    ("BLUE", blue), ("C", c)):
        al = im.getchannel("A")
        cov = sum(v for i, v in enumerate(al.histogram()) if i > 8) / (im.width * im.height)
        print(f"[{tag}] size={im.size} alpha_cov={cov:.3f}")
    print(f"[抠图] 人物占比={fg/tot:.1%}  背景已剔除={1-fg/tot:.1%}")
    print("OUT_DIR:", outdir)
    for fn in sorted(os.listdir(outdir)):
        print("  ", fn, os.path.getsize(os.path.join(outdir, fn)))


if __name__ == "__main__":
    main()
