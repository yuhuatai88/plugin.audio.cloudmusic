# -*- coding: utf-8 -*-
"""极简二维码生成器（纯 Python 标准库）

只支持 Byte 模式 + 纠错等级 L 的 1~5 版，够用来编码一条登录 URL
（最长 106 字节）。Kodi 环境里没有 qrcode / PIL，所以自己实现。

用法:
    from qr import make_png
    png_bytes = make_png('https://music.163.com/login?codekey=xxx')
"""
import struct
import zlib

# ---------------- GF(256) ----------------

def _gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_gen_poly(degree):
    """生成多项式，系数高次在前"""
    g = [1]
    for i in range(degree):
        a = _EXP[i]
        new = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            new[j] ^= c                 # c * x
            new[j + 1] ^= _gf_mul(c, a)  # c * a^i
        g = new
    return g


def _rs_remainder(data, ec_len):
    gen = _rs_gen_poly(ec_len)
    res = [0] * ec_len
    for d in data:
        factor = d ^ res[0]
        res = res[1:] + [0]
        for i in range(ec_len):
            res[i] ^= _gf_mul(gen[i + 1], factor)
    return res


# ---------------- 版本参数（EC level L，全部单块） ----------------
# version -> (数据码字数, 纠错码字数, 可容纳字节数)
_SPEC = {
    1: (19, 7, 17),
    2: (34, 10, 32),
    3: (55, 15, 53),
    4: (80, 20, 78),
    5: (108, 26, 106),
}

# 校准图案中心坐标
_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
}

# 版本 1~5 的纠错块都是 1 块，交织时可忽略分块逻辑


def _pick_version(data_len):
    for v in (1, 2, 3, 4, 5):
        if data_len <= _SPEC[v][2]:
            return v
    return None


def _encode_codewords(data, version):
    data_len, ec_len, _ = _SPEC[version]
    bits = []

    def push(value, n):
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)          # Byte 模式
    push(len(data), 8)       # 字符计数（版本 1~9 用 8 bit）
    for b in data:
        push(b, 8)
    # 结束符
    for _ in range(min(4, data_len * 8 - len(bits))):
        bits.append(0)
    # 补齐到字节
    while len(bits) % 8:
        bits.append(0)
    # 填充码字
    pads = (0xEC, 0x11)
    i = 0
    while len(bits) < data_len * 8:
        push(pads[i % 2], 8)
        i += 1
    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        codewords.append(v)
    return codewords + _rs_remainder(codewords, ec_len)


# ---------------- 矩阵构造 ----------------

def _new_matrix(size):
    return [[False] * size for _ in range(size)]


def _place_function(m, func, version):
    size = len(m)
    # 定位图案 + 分隔符
    for (r, c) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    func[rr][cc] = True
                    on = (0 <= dr <= 6 and (dc in (0, 6))) or \
                         (0 <= dc <= 6 and (dr in (0, 6))) or \
                         (2 <= dr <= 4 and 2 <= dc <= 4)
                    m[rr][cc] = bool(on)
    # 定时图案
    for i in range(size):
        if not func[6][i]:
            func[6][i] = True
            m[6][i] = (i % 2 == 0)
        if not func[i][6]:
            func[i][6] = True
            m[i][6] = (i % 2 == 0)
    # 校准图案
    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r, c) in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    func[rr][cc] = True
                    m[rr][cc] = (max(abs(dr), abs(dc)) != 1)
    # 格式信息区
    for i in range(9):
        if not func[8][i]:
            func[8][i] = True
        if not func[i][8]:
            func[i][8] = True
    for i in range(8):
        func[8][size - 1 - i] = True
        func[size - 1 - i][8] = True
    # 固定黑点
    func[size - 8][8] = True
    m[size - 8][8] = True


def _place_data(m, func, codewords):
    size = len(m)
    idx = 0
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    right = size - 1
    while right >= 1:
        if right == 6:          # 跳过定时图案所在的第 6 列
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = (size - 1 - vert) if upward else vert
                if func[y][x] or idx >= len(bits):
                    continue
                m[y][x] = bool(bits[idx])
                idx += 1
        right -= 2


def _mask_fn(k):
    def f(y, x):
        if k == 0:
            return (y + x) % 2 == 0
        if k == 1:
            return y % 2 == 0
        if k == 2:
            return x % 3 == 0
        if k == 3:
            return (y + x) % 3 == 0
        if k == 4:
            return (y // 2 + x // 3) % 2 == 0
        if k == 5:
            return (y * x) % 2 + (y * x) % 3 == 0
        if k == 6:
            return ((y * x) % 2 + (y * x) % 3) % 2 == 0
        return ((y + x) % 2 + (y * x) % 3) % 2 == 0
    return f


def _penalty(m):
    size = len(m)
    score = 0
    # 规则 1：同色连续 >= 5
    def scan_line(get):
        s = 0
        for i in range(size):
            run = 1
            for j in range(1, size):
                if get(i, j) == get(i, j - 1):
                    run += 1
                else:
                    if run >= 5:
                        s += 3 + (run - 5)
                    run = 1
            if run >= 5:
                s += 3 + (run - 5)
        return s

    score += scan_line(lambda i, j: m[i][j])
    score += scan_line(lambda i, j: m[j][i])
    # 规则 2：2x2 同色
    for y in range(size - 1):
        for x in range(size - 1):
            v = m[y][x]
            if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    # 规则 3：1:1:3:1:1 图形
    pat1 = (True, False, True, True, True, False, True)
    pat2 = tuple(reversed(pat1))

    def has_pat(seq):
        n = 0
        for i in range(len(seq) - 6):
            t = tuple(seq[i:i + 7])
            if t == pat1 or t == pat2:
                n += 1
        return n

    for i in range(size):
        score += 40 * has_pat([m[i][j] for j in range(size)])
        score += 40 * has_pat([m[j][i] for j in range(size)])
    # 规则 4：黑白比例
    dark = sum(1 for row in m for v in row if v)
    ratio = dark * 100.0 / (size * size)
    prev = int(abs(ratio - 50) / 5)
    score += prev * 10
    return score


def _format_bits(mask):
    # EC level L = 01
    data = (0b01 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (((rem >> 9) & 1) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _place_format(m, func, mask):
    size = len(m)
    bits = _format_bits(mask)
    for i in range(15):
        b = (bits >> i) & 1
        # 第一份：第 8 列上半 + 第 8 行左半
        if i < 6:
            m[i][8] = bool(b)
        elif i == 6:
            m[7][8] = bool(b)
        elif i == 7:
            m[8][8] = bool(b)
        elif i == 8:
            m[8][7] = bool(b)
        else:
            m[8][14 - i] = bool(b)
        # 第二份：第 8 行右半 + 第 8 列下半
        if i < 8:
            m[8][size - 1 - i] = bool(b)
        else:
            m[size - 15 + i][8] = bool(b)
    _ = func


def build_matrix(text, force_mask=None):
    """返回布尔矩阵，编码失败返回 None

    force_mask 给定时跳过 mask 评分（用于测试对照）。
    """
    data = text.encode('utf-8')
    version = _pick_version(len(data))
    if version is None:
        return None
    size = 17 + 4 * version
    m = _new_matrix(size)
    func = _new_matrix(size)
    codewords = _encode_codewords(data, version)
    _place_function(m, func, version)
    _place_data(m, func, codewords)

    if force_mask is not None:
        k = force_mask
        cand = [row[:] for row in m]
        fn = _mask_fn(k)
        for y in range(size):
            for x in range(size):
                if not func[y][x] and fn(y, x):
                    cand[y][x] = not cand[y][x]
        _place_format(cand, func, k)
        return cand

    best, best_score = None, None
    for k in range(8):
        cand = [row[:] for row in m]
        fn = _mask_fn(k)
        for y in range(size):
            for x in range(size):
                if not func[y][x] and fn(y, x):
                    cand[y][x] = not cand[y][x]
        _place_format(cand, func, k)
        score = _penalty(cand)
        if best_score is None or score < best_score:
            best, best_score = cand, score
    return best


# ---------------- PNG 输出 ----------------

def _chunk(tag, payload):
    return (struct.pack('>I', len(payload)) + tag + payload +
            struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))


def to_png(matrix, scale=8, border=4, dark=(0, 0, 0), light=(255, 255, 255),
           side=None):
    """matrix -> PNG 字节

    side 给定时把图补白（对称）到 side×side。模块本身仍是整数倍缩放，
    所以图形永远是正方形、不会被显示器拉伸变形。
    """
    if not matrix:
        return b''
    n = len(matrix)
    body = (n + border * 2) * scale
    if side is None:
        side = body
    side = max(int(side), body)
    extra = side - body
    pad_l = extra // 2
    pad_r = extra - pad_l

    light_b = bytes(bytearray(light))
    dark_b = bytes(bytearray(dark))
    edge = border * scale

    rows = []
    blank = b'\x00' + light_b * side
    for _ in range(edge + pad_l):
        rows.append(blank)
    left = light_b * (edge + pad_l)
    right = light_b * (edge + pad_r)
    for y in range(n):
        line = bytearray()
        line.append(0)                      # PNG filter type
        line += left
        row = matrix[y]
        for x in range(n):
            line += (dark_b if row[x] else light_b) * scale
        line += right
        one = bytes(line)
        for _ in range(scale):
            rows.append(one)
    for _ in range(edge + pad_r):
        rows.append(blank)
    raw = b''.join(rows)

    ihdr = struct.pack('>IIBBBBB', side, side, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) +
            _chunk(b'IDAT', zlib.compress(raw, 9)) + _chunk(b'IEND', b''))


def make_png(text, scale=None, border=4, target=None, dark=(0, 0, 0),
             light=(255, 255, 255)):
    """text -> 二维码 PNG 字节

    target 指定期望边长(像素)：取最大的整数倍模块尺寸，再补白到 target。
    这样图片尺寸正好等于屏幕上的显示尺寸，缩放比例是整数，最清晰。
    """
    m = build_matrix(text)
    if not m:
        return b''
    total = len(m) + border * 2
    if target:
        target = int(target)
        scale = max(1, target // total)
        return to_png(m, scale=scale, border=border, dark=dark, light=light,
                      side=target)
    return to_png(m, scale=scale or 8, border=border, dark=dark, light=light)


def solid_png(w=32, h=32, rgba=(0, 0, 0, 255)):
    """纯色 PNG（可带透明通道），用来当窗口背景 / 白色卡片

    只有几百字节，Kodi 里拉伸纯色不会有任何画质损失。
    """
    r, g, b, a = rgba
    px = bytes(bytearray((r, g, b, a)))
    raw = (b'\x00' + px * w) * h
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) +
            _chunk(b'IDAT', zlib.compress(raw, 9)) + _chunk(b'IEND', b''))


def to_text(matrix, dark='##', light='  '):
    if not matrix:
        return ''
    pad = light * 4
    body = [pad + ''.join(dark if v else light for v in row) + pad
            for row in matrix]
    blank = light * (len(matrix) + 8)
    return '\n'.join([blank] + body + [blank])


if __name__ == '__main__':
    m = build_matrix('https://music.163.com/login?codekey=abcdef123456')
    print(to_text(m))
    png = to_png(m, scale=4)
    print('png bytes', len(png))
