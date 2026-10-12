"""纯标准库 PNG 裁剪 + 放大工具（本机无 PIL，用于放大查看截图里的 UI 细节）。

用法：
    python tools/png_crop.py <in.png> <out.png> <x> <y> <w> <h> [scale]

例：把左上角 600x120 的区域放大 3 倍
    python tools/png_crop.py shot.png top.png 0 0 600 120 3

只支持 8 位 RGB/RGBA 非隔行 PNG（截图与 CDP 输出均属此类）。
"""
import struct
import sys
import zlib


def read_png(path):
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos, idat, hdr = 8, [], None
    while pos < len(raw):
        ln = struct.unpack(">I", raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", data)
            if depth != 8 or inter != 0 or color not in (2, 6):
                raise ValueError(f"仅支持 8 位 RGB/RGBA 非隔行（收到 depth={depth} color={color} interlace={inter}）")
            hdr = (w, h, 4 if color == 6 else 3)
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break
        pos += 12 + ln
    w, h, bpp = hdr
    stream = zlib.decompress(b"".join(idat))
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = stream[p]
        p += 1
        line = bytearray(stream[p:p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, bpp, out


def write_png(path, w, h, bpp, buf):
    color = 6 if bpp == 4 else 2
    stride = w * bpp
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += buf[y * stride:(y + 1) * stride]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    out = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    open(path, "wb").write(out)


def crop_scale(src, dst, x, y, w, h, scale=1):
    sw, sh, bpp, buf = read_png(src)
    x = max(0, min(x, sw - 1))
    y = max(0, min(y, sh - 1))
    w = max(1, min(w, sw - x))
    h = max(1, min(h, sh - y))
    sstride = sw * bpp
    out = bytearray(w * scale * h * scale * bpp)
    ostride = w * scale * bpp
    for oy in range(h * scale):
        sy = y + oy // scale
        srow = sy * sstride
        orow = oy * ostride
        for ox in range(w * scale):
            sx = x + ox // scale
            si = srow + sx * bpp
            di = orow + ox * bpp
            out[di:di + bpp] = buf[si:si + bpp]
    write_png(dst, w * scale, h * scale, bpp, out)
    return w * scale, h * scale


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 6:
        print(__doc__)
        sys.exit(1)
    nw, nh = crop_scale(a[0], a[1], int(a[2]), int(a[3]), int(a[4]), int(a[5]),
                        int(a[6]) if len(a) > 6 else 1)
    print(f"written {a[1]}  {nw}x{nh}")
