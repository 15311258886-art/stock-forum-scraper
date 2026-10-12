"""PNG 取色（纯标准库，零依赖）——用于核验截图是否真的换过主题。

为什么需要它：本机踩过一个坑——切主题后页面的 DOM 读数明明是深色
（`#theme-link` 指向 dark-dashboard.css、`getComputedStyle(body).backgroundColor`
= rgb(11,14,20)），但 `Page.captureScreenshot` 返回的仍是上一帧浅色画面，
产出一张与浅色版**逐字节相同**的假图。只看 DOM 或只看"文件已生成"都会漏掉，
必须**解码像素**才能证伪。

用法：
    python tools/png_pixel.py <图片> [x] [y] [x2 y2 ...]
    python tools/png_pixel.py a.png 20 500 40 520
    python tools/png_pixel.py --judge dark a.png 20 500     # 判定该点是否为深底
    python tools/png_pixel.py --compare a.png b.png         # 两图是否逐字节相同

输出：每个采样点的 RGB 与相对亮度（0=纯黑，255=纯白）。
"""
from __future__ import annotations

import hashlib
import struct
import sys
import zlib


def read_png(path: str):
    """解码 PNG → (宽, 高, 取样函数)。支持 8bit 的灰度/RGB/RGBA/调色板，逐行反滤波。"""
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos, idat, pal, trns = 8, bytearray(), None, None
    w = h = bitd = ctype = None
    while pos < len(raw):
        ln = struct.unpack(">I", raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bitd, ctype = struct.unpack(">IIBB", data[:10])
        elif typ == b"PLTE":
            pal = data
        elif typ == b"tRNS":
            trns = data
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if bitd != 8:
        raise ValueError(f"仅支持 8bit/通道，实际 {bitd}（截图通常为 8）")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    buf = zlib.decompress(bytes(idat))
    stride = w * ch
    out = bytearray(h * stride)          # 反滤波结果按行紧密排列
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = buf[p]; p += 1
        line = bytearray(buf[p:p + stride]); p += stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line

    def px(x: int, y: int):
        o = y * stride + x * ch
        s = out[o:o + ch]
        if ctype == 3:
            i = s[0] * 3
            s = pal[i:i + 3]
        if len(s) == 1:
            s = bytes([s[0]] * 3)
        elif len(s) == 2:
            s = bytes([s[0]] * 3)
        return tuple(s[:3])

    return w, h, px


def lum(rgb) -> float:
    """相对亮度（0=纯黑，255=纯白）。"""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "--compare":
        if len(a) < 3:
            print("用法: --compare a.png b.png")
            return 2
        ha = hashlib.sha256(open(a[1], "rb").read()).hexdigest()
        hb = hashlib.sha256(open(a[2], "rb").read()).hexdigest()
        same = ha == hb
        print(f"{a[1]}  sha256={ha[:16]}")
        print(f"{a[2]}  sha256={hb[:16]}")
        print("结论:", "逐字节相同 —— 很可能是没生效的假图" if same else "不同 —— 确实是两张不同的渲染")
        return 1 if same else 0
    if a[0] == "--judge":
        if len(a) < 4:
            print("用法: --judge dark|light <图片> <x> <y>")
            return 2
        want, path, x, y = a[1], a[2], int(a[3]), int(a[4])
        w, h, px = read_png(path)
        rgb = px(x, y)
        L = lum(rgb)
        got = "dark" if L < 128 else "light"
        ok = got == want
        print(f"{path} @({x},{y}) rgb={rgb} 亮度={L:.0f} → {got}"
              f"（期望 {want}）{'✅' if ok else '❌'}")
        return 0 if ok else 1

    path = a[0]
    pts = [(int(a[i]), int(a[i + 1])) for i in range(1, len(a) - 1, 2)]
    w, h, px = read_png(path)
    print(f"{path}  {w}x{h}")
    for (x, y) in pts:
        rgb = px(x, y)
        print(f"  ({x:>5},{y:>5}) rgb={rgb} 亮度={lum(rgb):.0f} "
              f"{'深底' if lum(rgb) < 128 else '浅底'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
