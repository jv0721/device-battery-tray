# -*- coding: utf-8 -*-
"""win_icon.py —— 用纯 Python 画托盘图标（不依赖 Pillow / 任何第三方库）

做法：内置一套 3x5 点阵数字，按电量数值渲染成 RGBA 位图，
再手写 ICO 文件格式，最后交给 LoadImageW 变成 HICON。
支持 16/24/32 三种尺寸，Windows 会按 DPI 自动挑合适的那张。
"""

from __future__ import annotations

import ctypes
import math
import os
import struct
import tempfile
from ctypes import wintypes
from typing import Optional

# ---- 3x5 点阵数字（每行 3 位，"1" 点亮）----
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "%": ("101", "001", "010", "100", "101"),
}

GLYPH_W, GLYPH_H = 3, 5


PLATE_COLOR = (222, 227, 233, 255)   # 浅灰底板：深色任务栏上不刺眼，白色任务栏上也看得出块


def _ink(color):
    """把电量的亮色压深一点，压在浅色底板上才看得清。

    直接乘个固定系数会把颜色搞脏，这里按最大通道归一到 165，
    色相基本保留（绿还是绿、红还是红），只是整体压暗。
    """
    m = max(color[0], color[1], color[2], 1)
    f = min(1.0, 165.0 / m)          # 已经够暗就别再压了，免得越压越淡
    return tuple(max(0, min(255, int(round(c * f)))) for c in color)


def render_rgba(text: str, size: int, color: tuple[int, int, int],
                plate: bool = False,
                plate_color: tuple = PLATE_COLOR) -> bytes:
    """把文字渲染成 size×size 的 RGBA 像素（返回 size*size*4 字节，行优先）。"""
    n = len(text)
    if plate:
        color = _ink(color)
    # 选一个能放下的最大缩放倍数（有底板时多留 2 像素，别顶到板边）
    limit = size - (4 if plate else 2)
    gap = 1
    scale = 1
    while True:
        w = n * GLYPH_W * (scale + 1) + (n - 1) * gap
        h = GLYPH_H * (scale + 1)
        if w > limit or h > limit:
            break
        scale += 1

    gw, gh = GLYPH_W * scale, GLYPH_H * scale
    total_w = n * gw + (n - 1) * gap
    total_h = gh
    ox = (size - total_w) // 2
    oy = (size - total_h) // 2

    px = bytearray(size * size * 4)

    def put(x: int, y: int, rgba):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            px[i:i + 4] = bytes(rgba)

    def get(x: int, y: int):
        i = (y * size + x) * 4
        return (px[i], px[i + 1], px[i + 2], px[i + 3])

    # 底衬（「两个图标」样式用，让两颗数看起来是一对 App 图标）
    #
    # 注意四周要留 1 像素：底板贴边的话，下面那圈描边就没地方画，
    # 只能钻进四个圆角缺口，结果看起来像凭空多了四条小黑线。
    if plate:
        r = max(2, int(round(size / 5.0)))
        for y in range(1, size - 1):
            for x in range(1, size - 1):
                cx = min(x - 1, size - 2 - x)
                cy = min(y - 1, size - 2 - y)
                if cx + cy < r:
                    continue
                put(x, y, plate_color)

    # 逐字画
    for k, ch in enumerate(text):
        glyph = DIGITS.get(ch)
        if not glyph:
            continue
        gx = ox + k * (gw + gap)
        for gy, row in enumerate(glyph):
            for gxx, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        put(gx + gxx * scale + dx, oy + gy * scale + dy,
                            (*color, 255))

    # 描一圈深色轮廓，保证在浅色/深色任务栏上都看得清
    outline = (16, 17, 19, 190)
    edge = []
    for y in range(size):
        for x in range(size):
            if get(x, y)[3] == 0:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < size and 0 <= ny < size and get(nx, ny)[3] == 0:
                    edge.append((nx, ny))
    for x, y in edge:
        if get(x, y)[3] == 0:
            put(x, y, outline)
    return bytes(px)


# --------------------------------------------------------------------------
# 两行数字图标：上排=键盘、下排=鼠标（默认样式）
#
# 为什么不再带形状符号：16 像素见方里符号要占掉 5 个格子，数字就只剩 10 个。
# 既然固定只有两台设备，**位置本身**就是最可靠的标记 —— 上排永远是键盘、
# 下排永远是鼠标，把整块面积都让给数字，才做得出 5×7 点阵这么粗的笔画。
# 电量高低交给颜色：绿 = 充足，橙 = 偏低，红 = 告急。
# --------------------------------------------------------------------------
DASH = ("000", "000", "111", "000", "000")

# 5 宽 7 高点阵数字：16 格高度里正好排两行（7 + 空 2 + 7）
DIGITS7 = {
    "0": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "01110", "00000", "00000", "00000"),
}

_ROW_H = 7           # 一行的格高（两行加起来 7+空2+7 = 16 格）


# --------------------------------------------------------------------------
# 超采样：先在 size×4 的高分辨率网格上画，再按 4×4 块平均缩回 size。
#
# 为什么要这么绕：任务栏图标不是只有 16 像素 —— 150% 缩放给 24、200% 给 32，
# 16 格的布局乘 1.5 之后，一个格点正好压 1.5 个像素。直接取整的话，
# 同一笔竖线有的 1 像素有的 2 像素，笔画看起来一格一格在抖。
# 画 4 倍再平均，边缘自然有了半透明过渡（这就是抗锯齿），
# 相邻格子的接缝也能正确合并成满色。
# --------------------------------------------------------------------------
_SS = 4


def _canvas(size):
    """开一张 size×_SS 的画布。返回 (px, hi, put, get)，坐标仍是 16 格基准。"""
    hi = size * _SS
    px = bytearray(hi * hi * 4)

    def put(x, y, rgba):
        if 0 <= x < hi and 0 <= y < hi:
            i = (y * hi + x) * 4
            px[i:i + 4] = bytes(rgba)

    def get(x, y):
        i = (y * hi + x) * 4
        return (px[i], px[i + 1], px[i + 2], px[i + 3])

    return px, hi, put, get


def _downsample(px, hi, size):
    """4×4 块平均缩回 size。颜色按 alpha 加权，免得边缘发灰。"""
    ss = hi // size
    n = ss * ss
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(ss):
                base = ((y * ss + dy) * hi + x * ss) * 4
                for dx in range(ss):
                    i = base + dx * 4
                    av = px[i + 3]
                    if av:
                        r += px[i] * av
                        g += px[i + 1] * av
                        b += px[i + 2] * av
                        a += av
            if a:
                o = (y * size + x) * 4
                out[o] = r // a
                out[o + 1] = g // a
                out[o + 2] = b // a
                out[o + 3] = a // n
    return bytes(out)


def _blit_dots(put, pattern, px, py, kx, ky, rgba):
    """把 1=点亮 的点阵画上去。

    px / py 是这块点阵左上角的**像素**坐标，kx / ky 是单格占多少像素。
    宽高分开传：100% 那种三位数要横向拉宽一点才够高，
    但又不能连高度一起拉，不然那一行会比其他行高出一截。
    """
    for gy, row in enumerate(pattern):
        for gx, bit in enumerate(row):
            if bit != "1":
                continue
            x0 = int(round(px + gx * kx))
            x1 = int(round(px + (gx + 1) * kx))
            y0 = int(round(py + gy * ky))
            y1 = int(round(py + (gy + 1) * ky))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    put(x, y, rgba)


def _num_line(put, text, glyphs, gw, gap, gh, hi, sx, sy, y_px, rgba):
    """把一串数字横向居中画进 16 格宽里，内容上边落在 y_px（像素）。

    sx / sy 是缩放系数：有底板的时候要留白，全尺寸顶到板边会显得局促。
    """
    k = hi / 16.0
    kw, kh = k * sx, k * sy
    total = len(text) * gw * sx + (len(text) - 1) * gap * sx      # 单位：格
    px0 = (hi - total * k) / 2.0
    for i, ch in enumerate(text):
        pat = glyphs.get(ch) or (DASH if gh == 5 else DIGITS7["-"])
        _blit_dots(put, pat, px0 + i * (gw + gap) * sx * k, y_px, kw, kh, rgba)


def _pick_glyphs(val, sc):
    """按位数挑字模。返回 (文字, 点阵, 字宽, 字距, 字高, 横向缩放)。

    三位数「100」在 16 格里排不下 5×7，退回 3×5 窄字再横向拉宽，
    拉 1.35 倍之后高度看起来跟旁边的 5×7 齐平。
    """
    if val is None:
        return "--", DIGITS7, 5, 2, 7, sc
    n = max(0, min(100, int(val)))
    if n >= 100:
        return "100", DIGITS, 3, 1, 5, sc * 1.35
    return str(n), DIGITS7, 5, 2, 7, sc


def render_two_num(size: int, top, bottom) -> bytes:
    """两行纯数字：上=键盘、下=鼠标。top / bottom = (电量或 None, RGB)。

    不加底板：16 像素的图标里，底板本身就占掉一圈，两行数字被挤得又小又闷，
    不如把全部面积让给数字 —— 数字本身就是这个程序的标志。
    颜色会跟着任务栏深浅换深浅，所以透明底也不会看不清。
    """
    px, hi, put, get = _canvas(size)
    k = hi / 16.0
    sc, rgap = 1.0, 2              # 铺满 16 格：7 + 空 2 + 7

    row_h = _ROW_H * sc
    block = row_h * 2 + rgap * sc
    y0 = (hi - block * k) / 2.0
    y1 = y0 + (row_h + rgap * sc) * k

    for item, y_px in ((top, y0), (bottom, y1)):
        val = item[0] if item else None
        rgb = item[1] if item else (150, 155, 160)
        color = (150, 155, 160, 255) if val is None else (rgb[0], rgb[1],
                                                         rgb[2], 255)
        text, glyphs, gw, gap, gh, sx = _pick_glyphs(val, sc)
        # 窄字（100）只有 5 格高，得在行内自己垂直居中
        yy = y_px + (_ROW_H * sc - gh * sc) * k / 2.0
        _num_line(put, text, glyphs, gw, gap, gh, hi, sx, sc, yy, color)

    return _downsample(px, hi, size)


# --------------------------------------------------------------------------
# 圆角底板：两种样式共用
#
# 底板四周缩进 1 格，描边才有地方画（贴边的话描边只能钻进四个圆角缺口，
# 看起来就像凭空多了四条小黑线）。
# --------------------------------------------------------------------------
PLATE_EDGE = (150, 156, 166, 255)    # 浅色底板的边
PLATE_DARK = (47, 51, 59, 255)       # 深色底板的底
PLATE_DARK_EDGE = (86, 92, 104, 255)  # 深色底板的边


def _fill_round(put, x0, y0, x1, y1, r, k, rgba):
    """在 16 格网格里填一个圆角矩形（坐标含端点，k = 单格像素）。"""
    for gy in range(int(y0), int(y1) + 1):
        for gx in range(int(x0), int(x1) + 1):
            cx = min(gx - x0, x1 - gx)
            cy = min(gy - y0, y1 - gy)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                continue
            _blit_dots(put, ("1",), gx * k, gy * k, k, k, rgba)


def _plate(put, hi, dark=False):
    """铺一块 16×16 的圆角底板：先描边，再铺底色，四周各留 1 格。"""
    k = hi / 16.0
    _fill_round(put, 0, 0, 15, 15, 3.6, k,
                PLATE_DARK_EDGE if dark else PLATE_EDGE)
    _fill_round(put, 1, 1, 14, 14, 2.8, k,
                PLATE_DARK if dark else PLATE_COLOR)


def render_one_num(size: int, val, color, dark: bool = False) -> bytes:
    """一个数字压在圆角底板上。val = 电量或 None，color = RGB。

    dark=True 用深底板配亮数字 —— 任务栏是黑的就得反过来，
    白底板压黑背景上会像个「贴纸」，反过来的对比才够。

    这里**按整数像素排数字**，不像两行数字那样按 16 格网格铺：
    任务栏图标常见尺寸是 16/24/32，一个格点分别是 1 / 1.5 / 2 像素，
    24 时正好是半个像素，笔画会被平均成半透明 —— 看着就是「糊」。
    先算出能容纳的最大整数像素，笔画落在整像素上才锐利。
    """
    px, hi, put, get = _canvas(size)
    _plate(put, hi, dark)
    ss = _SS                                   # 画布放大的倍数

    if val is None:
        ink = (150, 155, 160, 255)
    elif dark:
        ink = (*color, 255)                    # 深底板上直接用亮色，别再压深
    else:
        ink = (*_ink(color), 255)              # 浅底板上要压深，不然看不出

    text, glyphs, gw, gap, gh, _sx = _pick_glyphs(val, 1.0)

    inset = max(1, int(round(size / 16.0)))    # 底板自己缩进去的那圈
    room = max(1, int(round(size / 16.0)))     # 数字和板边之间再留一点
    avail = size - 2 * (inset + room)
    need = len(text) * gw + (len(text) - 1) * gap
    unit = max(1, avail // need)               # 一格占几个像素（整数！）

    w = need * unit
    x0 = (size - w) / 2.0
    y0 = (size - gh * unit) / 2.0
    u_px = unit * ss
    for i, ch in enumerate(text):
        pat = glyphs.get(ch) or (DASH if gh == 5 else DIGITS7["-"])
        _blit_dots(put, pat, (x0 + i * (gw + gap) * unit) * ss, y0 * ss,
                   u_px, u_px, ink)

    return _downsample(px, hi, size)


# --------------------------------------------------------------------------
# 程序图标：蓝色圆角方块 + 电池。
#
# 窗口、任务栏、托盘隐藏窗口（Alt-Tab / 任务管理器里那格）都用这一张 ——
# 不给的话那些地方就是一张默认的「白纸」，跟整个程序毫无关系。
# --------------------------------------------------------------------------
TILE = (91, 140, 255, 255)        # 面板强调色，界面里的按钮就是这个蓝
TILE_EDGE = (58, 96, 196, 255)
ICON_WHITE = (245, 248, 255, 255)
ICON_GREEN = (61, 220, 151, 255)  # 跟「电量充足」一个绿


def render_app_icon(size: int) -> bytes:
    px, hi, put, get = _canvas(size)
    k = hi / 16.0
    _fill_round(put, 0, 0, 15, 15, 3.8, k, TILE_EDGE)      # 深一圈的边
    _fill_round(put, 0.6, 0.6, 14.4, 14.4, 3.2, k, TILE)   # 亮面
    # 电池：白色外框 → 抠出肚皮 → 正极凸头（跟机身连着，断开就像镜头了）
    # → 一格绿色电量。整体 2.0~13.8，居中。
    _fill_round(put, 2.0, 4.5, 12.0, 11.5, 1.5, k, ICON_WHITE)
    _fill_round(put, 3.0, 5.5, 11.0, 10.5, 1.0, k, TILE)
    _fill_round(put, 12.0, 6.6, 13.8, 9.4, 0.6, k, ICON_WHITE)
    _fill_round(put, 4.2, 6.5, 8.6, 9.5, 0.6, k, ICON_GREEN)
    return _downsample(px, hi, size)


_APP_SIZES = (16, 24, 32, 48, 64)
_app_icon_path_cache = ""


def app_icon_path() -> str:
    """把程序图标写成 ico 文件，返回路径（重复调用走缓存）。失败返回空串。"""
    global _app_icon_path_cache
    if _app_icon_path_cache:
        return _app_icon_path_cache
    try:
        ico = build_ico([(s, render_app_icon(s)) for s in _APP_SIZES])
        path = os.path.join(_ensure_tmp(), "peripheral_battery_app.ico")
        with open(path, "wb") as f:
            f.write(ico)
        _app_icon_path_cache = path
        return path
    except Exception:
        return ""


def build_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """把多张 RGBA 位图打包成 ICO 文件内容。"""
    entries = []
    blobs = []
    offset = 6 + 16 * len(pngs)
    for size, rgba in pngs:
        # BITMAPINFOHEADER
        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          size * size * 4, 0, 0, 0, 0)
        # XOR 位图：32bpp BGRA，自下而上
        xor = bytearray()
        for y in range(size - 1, -1, -1):
            row = rgba[y * size * 4:(y + 1) * size * 4]
            for x in range(size):
                r, g, b, a = row[x * 4:x * 4 + 4]
                # 预乘不必要，Windows 直接用 alpha
                xor += bytes((b, g, r, a))
        # AND 掩码：1bpp，每行按 4 字节对齐，全 0（靠 alpha 通道）
        row_bytes = ((size + 31) // 32) * 4
        mask = b"\x00" * (row_bytes * size)
        blob = bih + bytes(xor) + mask
        blobs.append(blob)
        entries.append(struct.pack("<BBBBHHII", size if size < 256 else 0,
                                   size if size < 256 else 0, 0, 0, 1, 32,
                                   len(blob), offset))
        offset += len(blob)
    return (struct.pack("<HHH", 0, 1, len(pngs)) + b"".join(entries)
            + b"".join(blobs))


# ---------------- HICON 缓存 ----------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [ctypes.c_void_p]

SIZES = (16, 24, 32)
_icon_cache: dict[tuple, int] = {}
_tmp_dir: Optional[str] = None


def _ensure_tmp() -> str:
    global _tmp_dir
    if _tmp_dir is None:
        _tmp_dir = os.path.join(tempfile.gettempdir(), "device_battery_icons")
        os.makedirs(_tmp_dir, exist_ok=True)
    return _tmp_dir


def make_hicon(text: str, color: tuple[int, int, int], plate: bool = False,
               dark: bool = False) -> int:
    """按文字+颜色生成（带缓存的）HICON；失败返回 0。"""
    key = (text, color, plate, dark)
    if key in _icon_cache:
        return _icon_cache[key]
    val = None if text == "--" else int(text)
    images = [(s, render_one_num(s, val, color, dark)) for s in SIZES]
    ico = build_ico(images)
    path = os.path.join(_ensure_tmp(), f"ico_{text}_{color[0]:02x}{color[1]:02x}"
                                       f"{color[2]:02x}_{int(plate)}"
                                       f"{int(dark)}.ico")
    try:
        with open(path, "wb") as f:
            f.write(ico)
    except OSError:
        return 0
    h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    hicon = int(h) if h else 0
    _icon_cache[key] = hicon
    return hicon


def _from_renderer(key, name: str, renderer, top, bottom) -> int:
    """把某个 renderer 的结果做成（带缓存的）HICON。"""
    if key in _icon_cache:
        return _icon_cache[key]
    ico = build_ico([(s, renderer(s, top, bottom)) for s in SIZES])
    path = os.path.join(_ensure_tmp(), name)
    try:
        with open(path, "wb") as f:
            f.write(ico)
    except OSError:
        return 0
    h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    hicon = int(h) if h else 0
    _icon_cache[key] = hicon
    return hicon


def make_hicon_two_num(top, bottom) -> int:
    """两行数字托盘图标（默认样式）：透明底，只留两排数字。"""
    def tag(t):
        return "x" if not t or t[0] is None else int(t[0])

    key = ("twonum", top, bottom)
    # 文件名带上颜色：任务栏浅/深切换时数字颜色会变，同名文件会被覆盖得莫名其妙
    name = "tn_%s%02x%02x%02x_%s%02x%02x%02x.ico" % (
        tag(top), top[1][0], top[1][1], top[1][2],
        tag(bottom), bottom[1][0], bottom[1][1], bottom[1][2])
    return _from_renderer(key, name, render_two_num, top, bottom)


def destroy_all() -> None:
    for h in _icon_cache.values():
        if h:
            try:
                user32.DestroyIcon(ctypes.c_void_p(h))
            except Exception:
                pass
    _icon_cache.clear()


def cleanup_files() -> None:
    """清掉临时目录里的 ico 文件。"""
    global _tmp_dir
    if _tmp_dir and os.path.isdir(_tmp_dir):
        for name in os.listdir(_tmp_dir):
            try:
                os.remove(os.path.join(_tmp_dir, name))
            except OSError:
                pass


if __name__ == "__main__":
    # 生成几个样例，方便肉眼检查
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_icon_preview")
    os.makedirs(out, exist_ok=True)
    for text, color, plate, name in (
        ("82", (93, 220, 138), False, "mouse82"),
        ("70", (93, 220, 138), True, "plate70"),
        ("9", (255, 107, 107), True, "plate9"),
        ("100", (245, 196, 81), True, "plate100"),
        ("25", (245, 196, 81), False, "mouse25"),
        ("48/81", (245, 196, 81), False, "twonum"),
    ):
        if text == "48/81":
            imgs = [(s, render_two_num(s, (48, (245, 196, 81)),
                                      (81, (93, 220, 138)))) for s in SIZES]
        else:
            imgs = [(s, render_rgba(text, s, color, plate)) for s in SIZES]
        p = os.path.join(out, name + ".ico")
        with open(p, "wb") as f:
            f.write(build_ico(imgs))
        print("生成", p)
