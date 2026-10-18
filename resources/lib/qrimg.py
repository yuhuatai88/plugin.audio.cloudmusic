# -*- coding: utf-8 -*-
"""把官方二维码 PNG 解码成布尔矩阵，再整数倍放大后重新编码。

用途：QQ 的 ptqrshow 返回的二维码是 **1-bit 灰度 PNG（111x111）**。
直接交给 Kodi 拉伸到 500px 会被插值糊掉，手机容易扫不出来。
这里把它解码成模块矩阵，用最近邻整数倍放大重新编码成 PNG，
保证边缘锐利、和网易云插件那版一样清楚。

零第三方依赖；只支持非隔行（interlace=0）——腾讯返回的都是这种。
Python 3.8 兼容（Kodi 21 用 3.8）。
"""
import struct
import zlib

QR_MARK = b'\x89PNG\r\n\x1a\n'


def _chunks(data):
    """遍历 PNG chunk -> (tag, payload)"""
    if data[:8] != QR_MARK:
        raise ValueError('不是 PNG')
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + ln]
        yield tag, payload
        pos += 12 + ln


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, height, stride, bpp):
    """还原 PNG 行过滤器，返回 list[bytearray]"""
    rows = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = prev[i]
                ul = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, up, ul)) & 0xFF
        elif ft != 0:
            raise ValueError('未知过滤器 %d' % ft)
        rows.append(line)
        prev = line
    return rows


def decode(data):
    """PNG 字节 -> (w, h, matrix)，matrix[y][x] 为 True 表示深色（黑）"""
    ihdr = None
    idat = b''
    for tag, payload in _chunks(data):
        if tag == b'IHDR':
            ihdr = payload
        elif tag == b'IDAT':
            idat += payload
        elif tag == b'IEND':
            break
    if not ihdr or not idat:
        raise ValueError('PNG 缺少 IHDR/IDAT')
    w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(
        '>IIBBBBB', ihdr[:13])
    if interlace:
        raise ValueError('不支持隔行 PNG')
    raw = zlib.decompress(idat)

    def rows_for(stride, bpp):
        return _unfilter(raw, h, stride, bpp)

    if depth == 1 and ctype == 0:
        stride = (w + 7) // 8
        rows = rows_for(stride, 1)
        m = []
        for line in rows:
            bits = []
            for x in range(w):
                byte = line[x >> 3]
                bit = (byte >> (7 - (x & 7))) & 1
                bits.append(bit == 0)   # 灰度 0 = 黑
            m.append(bits)
        return w, h, m

    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 1)
    if depth != 8:
        raise ValueError('暂不支持 bitdepth=%d colortype=%d' % (depth, ctype))
    stride = w * bpp
    rows = rows_for(stride, bpp)
    m = []
    for line in rows:
        bits = []
        for x in range(w):
            off = x * bpp
            if ctype in (0, 4):
                v = line[off]
            else:
                v = (line[off] + line[off + 1] + line[off + 2]) // 3
            bits.append(v < 128)
        m.append(bits)
    return w, h, m


def upscale(m, factor):
    """最近邻整数倍放大"""
    if factor <= 1:
        return m
    out = []
    for line in m:
        wide = []
        for bit in line:
            wide.extend([bit] * factor)
        for _ in range(factor):
            out.append(list(wide))
    return out


def content_box(m):
    """内容（非白）区域的边界 -> (top, bottom, left, right)"""
    h = len(m)
    w = len(m[0]) if h else 0
    top = 0
    while top < h and not any(m[top]):
        top += 1
    if top >= h:
        return (0, 0, 0, 0)
    bottom = h
    while bottom > top and not any(m[bottom - 1]):
        bottom -= 1
    left = 0
    while left < w and not any(r[left] for r in m):
        left += 1
    right = w
    while right > left and not any(r[right - 1] for r in m):
        right -= 1
    return (top, bottom, left, right)


QR_MODS = (21, 25, 29, 33, 37, 41, 45)


def grid_bad(m, top, left, unit, mods):
    """逐模块统计「内部颜色不一致」的数量，全 0 说明网格对齐正确"""
    bad = 0
    for gy in range(mods):
        for gx in range(mods):
            vals = set()
            for dy in range(unit):
                for dx in range(unit):
                    vals.add(m[top + gy * unit + dy][left + gx * unit + dx])
            if len(vals) != 1:
                bad += 1
    return bad


def analyze(m):
    """推断 (模块像素 unit, 模块数 mods)。

    不能靠「量第一条黑线」——那是定位方块的整条边（7 个模块宽）。
    改用 QR 规范的合法模块数去整除内容宽，再用网格一致性投票，
    只有真正的 unit 能让每个模块内部颜色完全一致。
    """
    top, bottom, left, right = content_box(m)
    cw, ch = right - left, bottom - top
    if cw <= 0 or ch <= 0:
        return 1, cw
    for mods in QR_MODS:
        if cw % mods or ch % mods:
            continue
        unit = cw // mods
        if unit < 1 or (ch // mods) != unit:
            continue
        if grid_bad(m, top, left, unit, mods) == 0:
            return unit, mods
    return 1, cw


def module_size(m):
    """模块边长（像素）。见 analyze 里的说明"""
    unit, _mods = analyze(m)
    return unit


def crop_quiet(m, keep=4):
    """裁掉多余静默区，只留 keep 个模块白边（让二维码在屏幕上尽量大）"""
    top, bottom, left, right = content_box(m)
    if bottom <= top:
        return m
    h = len(m)
    w = len(m[0]) if h else 0
    unit = module_size(m)
    pad = unit * keep
    y0 = max(0, top - pad)
    y1 = min(h, bottom + pad)
    x0 = max(0, left - pad)
    x1 = min(w, right + pad)
    return [r[x0:x1] for r in m[y0:y1]]


def fit(m, target, keep=4):
    """把矩阵放大/裁剪到接近 target 像素；返回 (matrix, 实际边长)"""
    m = crop_quiet(m, keep)
    n = len(m)
    if not n:
        return m, 0
    factor = max(1, target // n)
    return upscale(m, factor), n * factor


def to_text(m, limit=48):
    """转 ASCII，调试/自测用"""
    step = max(1, len(m) // limit) if m else 1
    lines = []
    for y in range(0, len(m), step):
        row = ''
        for x in range(0, len(m[0]), step):
            row += '##' if m[y][x] else '  '
        lines.append(row)
    return '\n'.join(lines)
