# -*- coding: utf-8 -*-
"""my_devices.py —— 外设电量面板（tkinter，零第三方依赖）

    python my_devices.py            打开面板
    python my_devices.py --once     命令行打印一次就退出

界面怎么做的
------------
整个窗口是一块 Canvas 手工画的，没有用 tk 的默认控件 —— 只有这样才能做出
圆角卡片、环形电量表、渐变底这些现代监控软件（A Hub / M Hub 那一类）的样子。

绘制统一按 780x520 的「设计稿坐标」写，真正落笔时乘一个缩放比 s
（s = 系统 DPI / 96），字体也按像素指定（tk 里负数尺寸就是像素），
所以在 100% / 150% / 200% 缩放下版式完全一致，字也不会糊。

设备怎么读
----------
* 迈从 G87 V2：主动发 `cmd 0x05` 读配置块，应答第 40 字节是电量、41 是充电位；
  再用 `cmd 0x03` 读一次设备信息拿固件日期和接收器名。全程只读。
* 迈从 A7 V3 Ultra+：主动发 `4D` 帧的 `0x0900` 读设备信息，数据区第 12 字节是电量。
* 雷柏 VT7 V2 / VT7S Pro V2：接收器每 3 秒被动播报一帧，等一帧就够（两款帧格式相同）。

左格固定是键盘，右格是「鼠标位」：默认放迈从 A7，卡片右下角可以一键换成雷柏。

设备图放在 assets/ 里，换图直接覆盖同名文件即可；文件不在就退回占位框。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import io
import math
import os
import queue
import tempfile
import threading
import time
import webbrowser

import mchose
import mchose_mouse
import rapoo
import tray_common as tc

# 缩图 / 抗锯齿都靠 Pillow（有就用，没有就退回 tk 自带的画法）。
# Pillow 只影响画面精细度，不影响能不能跑，所以留在可选的兜底位置上。
#
# 这里**故意不导入 PIL.ImageTk**：它内部要靠 `_imagingtk` 这个 C 扩展把像素
# 塞进 Tk，而打包成 exe 之后这个扩展时不时加载失败（报 cannot import name
# '_imagingtk'），一失败就是整个程序崩在画图那一步。
# 所以走自己的路：PIL 缩好图 → 存成 PNG 字节 → base64 → tk.PhotoImage(data=)。
# 效果一样（alpha 照样保留），但链路里没有任何需要额外加载的东西。
try:
    from PIL import Image, ImageDraw
    _PIL = True
except Exception:
    _PIL = False

if _PIL:
    # 新老 Pillow 的重采样常量位置不一样，两边都认
    _RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
else:
    _RESAMPLE = 1


LAST_ERR = ""          # 最近一次 pil_photo 失败的原因（排障用，正常时是空串）


def pil_photo(img):
    """PIL 图 → 能直接塞进 Canvas 的 tk 图片。失败返回 None（调用方自行退路）。"""
    global LAST_ERR
    import tkinter as tk
    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        import traceback
        LAST_ERR = traceback.format_exc()
        return None

# --------------------------------------------------------------------------
# 配色：两套完整调色板，切换时直接改模块级变量
#
# 绘制代码一律在**运行时**读这些名字（不放进默认参数），
# 所以 use_palette() 一调，下一次 redraw 就是新配色。
# --------------------------------------------------------------------------
PALETTES = {
    "dark": {
        "BG_TOP": "#0C0E12", "BG_BOT": "#080A0D",
        "CARD_TOP": "#171B23", "CARD_BOT": "#14181F", "CARD_LINE": "#252C39",
        "PANEL_TOP": "#171C24", "PANEL_BOT": "#11151B",
        "TEXT": "#EDF0F5", "SUB": "#8B94A4", "FAINT": "#5B6372",
        "TRACK": "#242B37",
        "GREEN": "#3DDC97", "AMBER": "#FFC24B", "RED": "#FF6B6B",
        "GREY": "#7A828F",
        "PILL_BG": "#1F2531", "PILL_BG2": "#20262F", "PILL_LINE": "#2E3746",
        "CHIP_BG": "#1C222C", "BOX_OFF": "#1A202A", "BOX_LINE": "#39424F",
        "DRIVER_BG": "#1B2436", "DRIVER_FG": "#9DBAFF",
        "DRIVER_LINE": "#2C3A55", "TIP_INK": "#0B0D11",
    },
    "light": {
        "BG_TOP": "#F6F8FC", "BG_BOT": "#E9EEF6",
        "CARD_TOP": "#FFFFFF", "CARD_BOT": "#FAFBFE", "CARD_LINE": "#E2E7F0",
        "PANEL_TOP": "#FFFFFF", "PANEL_BOT": "#F6F9FD",
        "TEXT": "#161B24", "SUB": "#5B6575", "FAINT": "#8B95A5",
        "TRACK": "#DDE4EF",
        "GREEN": "#0E9A5A", "AMBER": "#C17100", "RED": "#D62B2B",
        "GREY": "#98A1AF",
        "PILL_BG": "#EFF2F7", "PILL_BG2": "#F2F5FA", "PILL_LINE": "#DBE1EB",
        "CHIP_BG": "#EEF2F8", "BOX_OFF": "#FFFFFF", "BOX_LINE": "#C9D2DF",
        "DRIVER_BG": "#E9F0FF", "DRIVER_FG": "#2F5BD0",
        "DRIVER_LINE": "#C6D6FF", "TIP_INK": "#FFFFFF",
    },
}

ACCENT = "#5B8CFF"
ACCENT_DIM = "#2B3A5C"

# 这几个先给个初值（深色），启动时再按配置覆盖
BG_TOP = PALETTES["dark"]["BG_TOP"]
BG_BOT = PALETTES["dark"]["BG_BOT"]
CARD_TOP = PALETTES["dark"]["CARD_TOP"]
CARD_BOT = PALETTES["dark"]["CARD_BOT"]
CARD_LINE = PALETTES["dark"]["CARD_LINE"]
PANEL_TOP = PALETTES["dark"]["PANEL_TOP"]
PANEL_BOT = PALETTES["dark"]["PANEL_BOT"]
TEXT = PALETTES["dark"]["TEXT"]
SUB = PALETTES["dark"]["SUB"]
FAINT = PALETTES["dark"]["FAINT"]
TRACK = PALETTES["dark"]["TRACK"]
GREEN = PALETTES["dark"]["GREEN"]
AMBER = PALETTES["dark"]["AMBER"]
RED = PALETTES["dark"]["RED"]
GREY = PALETTES["dark"]["GREY"]
PILL_BG = PALETTES["dark"]["PILL_BG"]
PILL_BG2 = PALETTES["dark"]["PILL_BG2"]
PILL_LINE = PALETTES["dark"]["PILL_LINE"]
CHIP_BG = PALETTES["dark"]["CHIP_BG"]
BOX_OFF = PALETTES["dark"]["BOX_OFF"]
BOX_LINE = PALETTES["dark"]["BOX_LINE"]
DRIVER_BG = PALETTES["dark"]["DRIVER_BG"]
DRIVER_FG = PALETTES["dark"]["DRIVER_FG"]
DRIVER_LINE = PALETTES["dark"]["DRIVER_LINE"]
TIP_INK = PALETTES["dark"]["TIP_INK"]


def use_palette(name: str) -> None:
    """把某一套配色的颜色值灌进模块级变量。"""
    for key, val in PALETTES.get(name, PALETTES["dark"]).items():
        globals()[key] = val

FONT = "Microsoft YaHei UI"

# 左键盘区、右鼠标区。鼠标位由配置里的 mouse_slot 决定显示哪只
# 26 + 356 + 16 + 356 + 26 = 780
DESIGN_W, DESIGN_H = 780, 520
MARGIN = 26
GAP = 16
HEADER_H = 62
CARD_H = 330
CARD_W = (DESIGN_W - MARGIN * 2 - GAP) / 2.0
BAR_H = 58

# 最短 1 分钟：电量变化本来就慢，比这更频繁纯属白折腾
# tc.INTERVAL_CHOICES 里存的是 (秒数, 文字)，这里统一翻成 (文字, 秒数)，
# 跟 THEMES / TRAY_STYLES 一个形状 —— 漏了这一步，菜单里就会拿数字去拼字符串。
INTERVALS = [(label, secs) for secs, label in tc.INTERVAL_CHOICES]

# 掉电残影（游戏里血条的延迟扣减）：
#   只有手动点「立即刷新」才播，自动刷新不播 —— 自动刷新本来就不该有动效开销。
#   先整整 2 秒停住不动（让你看清「上次多少、掉了多少」），最后 1 秒才回缩。
#   别一上来就动：眼睛还没来得及读数字，动画就演完了。
GHOST_HOLD = 2.0          # 静止多久（秒）
GHOST_ANIM = 1.0          # 回缩多久（秒）
GHOST_MS = (GHOST_HOLD + GHOST_ANIM) * 1000
GHOST_STEP = 33           # 约 27 帧，够顺滑又不至于闪着费 CPU
TRAY_STYLES = [("两行数字", "dualnum"),
               ("两个图标", "separate")]
THEMES = [("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")]

ASSETS = os.path.join(tc.RES_DIR, "assets")

# 设备键 → 图片文件名。换图直接覆盖 assets 里的同名文件即可。
ASSET_FILES = {"keyboard": "keyboard.png",
               "mouse": "mouse.png",
               "a7": "a7.png"}

# 日志页一次装多少条。文件本身能一直长，这里只是「翻得到多远」的上限，
# 400 条按一天刷 20 次算够看三周，再多就该去开 CSV 了。
LOG_VIEW_MAX = 400

# 每台设备的网页版驱动（都只做配置，不改电量通道，点开是浏览器自己的事）
#   迈从：官网首页就是 M HUB 网页版入口，官方手册里写的就是这个地址
#   雷柏：RAPOO HUB 网页驱动，VT 二代系列都支持
DRIVER_URL = {
    "keyboard": "https://www.mchose.com.cn",
    "mouse": "https://hub.rapoo.cn/home",
    "a7": "https://www.mchose.com.cn",
}


def _rgb(c):
    """'#rrggbb' 或 '#rgb' → (r, g, b)。"""
    h = c.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgba(c, a=255):
    r, g, b = _rgb(c)
    return (r, g, b, a)


def level_color(pct):
    if pct is None:
        return GREY
    if pct >= 60:
        return GREEN
    if pct >= 30:
        return AMBER
    return RED


def _mix(c1, c2, t):
    """两个 #rrggbb 之间插值；t 会被夹到 0..1。"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t))
                                   for i in range(3))


# --------------------------------------------------------------------------
# 读设备
# --------------------------------------------------------------------------
def _fallback(key, conn):
    """直连读不到时，用另一个进程（托盘）记下的上次读数兜底。

    两台设备都会休眠，休眠时怎么问都不开口。既然托盘已经读到过，
    就没必要让面板显示成「未响应」——把上次的值和读到的时间摆出来更管用。
    """
    pub = tc.read_published(key)
    if not pub:
        return None
    pct, chg, ts = pub
    if time.time() - ts > 7 * 86400:        # 一周前的数就别拿来充数了
        return None
    when = time.strftime("%m-%d %H:%M", time.localtime(ts))
    return pct, chg, when


def read_keyboard():
    """迈从：主动查询电量 + 设备信息。返回统一结构的 dict。"""
    conn = mchose.connection() or "2.4G"
    out = {"name": "迈从 G87 V2", "kind": "keyboard", "ok": False,
           "battery": None, "charging": None, "rows": [],
           "conn": conn}
    t0 = time.time()
    try:
        lvl, chg = mchose.query_battery()
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        out["rows"] = [("状态", "读取异常：%s" % e)]
        return out

    if lvl is None:
        if conn == "有线":
            # 插线时这块固件不报电量：0x65 状态查询回 "unknw"，
            # 0x05 回的是静态配置块（第 40 字节不是电量，几分钟纹丝不动）。
            # 与其把上次无线的 5% 当成实时值吓人，不如老实说「USB 供电」。
            out["ok"] = True
            out["no_battery"] = True
            out["ring_label"] = "USB 供电"
            rows = [("通道", "有线"), ("供电", "USB 直连")]
            fb = _fallback("keyboard", conn)
            if fb:
                rows.append(("上次无线", "%d%% · %s" % (fb[0], fb[2])))
            out["rows"] = rows
            return out
        cached = mchose.latest(max_age=3600)
        if cached is None:
            fb = _fallback("keyboard", out["conn"])
            if fb:
                out.update(ok=True, battery=fb[0], charging=fb[1],
                           stale=fb[2])
                out["rows"] = [("充电", "充电中" if fb[1] else "未充电"),
                               ("通道", conn),
                               ("数据", "上次读到 %s" % fb[2])]
                return out
            out["rows"] = [("通道", conn),
                           ("状态", "接收器可能休眠了"),
                           ("建议", "敲一下键盘再刷新")]
            return out
        lvl, chg, ms = cached, mchose.is_charging(), None
        out["stale"] = "上次"

    out["ok"] = True
    out["battery"] = lvl
    out["charging"] = bool(chg) if chg is not None else None
    # 写进配置，托盘那边直接取用，省得两个进程各自去抢同一个 HID 句柄
    tc.publish_reading("keyboard", lvl, out["charging"])

    info = {}
    try:
        info = mchose.query_info() or {}
    except Exception:
        pass

    rows = [("充电", "充电中" if out["charging"] else "未充电"),
            ("通道", out["conn"])]
    if info.get("fw_date"):
        fw = info["fw_date"] + (" " + info["fw_time"] if info.get("fw_time") else "")
        rows.append(("固件", fw))
    if ms is not None:
        rows.append(("响应", "%d ms" % ms))
    out["rows"] = rows
    return out


def read_mouse():
    """雷柏（VT7 V2 / VT7S Pro V2）：等一帧被动播报。返回统一结构的 dict。"""
    out = {"name": rapoo.model_name(), "kind": "mouse", "ok": False,
           "battery": None, "charging": None, "rows": [],
           "conn": "2.4G"}
    try:
        res = rapoo.read_status(timeout=5.0)
    except Exception as e:
        out["rows"] = [("状态", "读取异常：%s" % e)]
        return out

    if not res or "error" in res:
        fb = _fallback("mouse", out["conn"])
        if fb:
            out.update(ok=True, battery=fb[0], charging=fb[1], stale=fb[2])
            out["rows"] = [("充电", "充电中" if fb[1] else "未充电"),
                           ("通道", out["conn"]),
                           ("数据", "上次读到 %s" % fb[2])]
            return out
        out["rows"] = [("状态", "没等到播报"),
                       ("建议", (res or {}).get("error")
                        or "确认鼠标开机、动一下它")]
        return out

    out["ok"] = True
    out["name"] = res.get("model") or out["name"]
    out["battery"] = res["battery"]
    out["charging"] = res["charging"]
    tc.publish_reading("mouse", res["battery"], bool(res.get("charging")))

    # 参数行只放设备本身的东西。「多久取到这一帧」是排查用的，
    # 摆给用户看只是噪音，「是不是上次的读数」也已经由环下面那行「上次读数」交代了。
    rows = [("充电", "充电中" if res.get("charging") else "未充电")]
    if res.get("dpi"):
        rows.append(("DPI", "%d" % res["dpi"]))
    rows.append(("通道", out["conn"]))
    out["rows"] = rows
    return out


def read_a7():
    """迈从 A7 V3 Ultra+：主动查询（报文号 0x4D，命令字 0x0900）。"""
    out = {"name": "迈从 A7 V3 Ultra+", "kind": "a7", "ok": False,
           "battery": None, "charging": None, "rows": [],
           "conn": "2.4G"}
    t0 = time.time()
    try:
        info = mchose_mouse.query_info()
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        out["rows"] = [("状态", "读取异常：%s" % e)]
        return out

    if not info:
        cached = mchose_mouse.latest(max_age=3600)
        if cached is None:
            fb = _fallback("a7", out["conn"])
            if fb:
                out.update(ok=True, battery=fb[0], charging=fb[1], stale=fb[2])
                out["rows"] = [("充电", "充电中" if fb[1] else "未充电"),
                               ("数据", "上次读到 %s" % fb[2])]
                return out
            if mchose_mouse.is_online() is False:
                out["rows"] = [("状态", "鼠标睡着了"),
                               ("建议", "动一下鼠标再刷新")]
            else:
                out["rows"] = [("状态", "接收器没应答"),
                               ("建议", "确认鼠标开着、接收器插好")]
            return out
        out.update(ok=True, battery=cached, charging=mchose_mouse.is_charging())
        out["stale"] = "上次"
        out["rows"] = [("充电", "充电中" if out["charging"] else "未充电"),
                       ("数据", "上次读数")]
        return out

    out["ok"] = True
    out["battery"] = info["battery"]
    out["charging"] = bool(info["charging"])
    tc.publish_reading("a7", out["battery"], out["charging"])

    link = info.get("link")
    if link == 2:
        out["conn"] = "2.4G"
    elif link == 1:
        out["conn"] = "有线"
    elif link:
        out["conn"] = "连接 %d" % link
    else:
        out["conn"] = "未连接"

    rows = [("充电", "充电中" if out["charging"] else "未充电"),
            ("通道", out["conn"])]
    if ms:
        rows.append(("响应", "%d ms" % ms))
    out["rows"] = rows
    return out


def read_all():
    """键盘区 + 鼠标区。鼠标区读哪只由配置 mouse_slot 决定。"""
    return read_keyboard(), read_mouse_slot()


def mouse_slot():
    return tc.load_config().get("mouse_slot", "a7")


def read_mouse_slot():
    return read_a7() if mouse_slot() == "a7" else read_mouse()


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 窗口外观：深色标题栏 + 自己的图标
# --------------------------------------------------------------------------
def _apply_app_icon(win):
    """把窗口和任务栏的图标换成程序图标（蓝底电池）。

    不换的话标题栏和任务栏会挂一张 tk 默认的「白纸」，跟深色界面格格不入。

    只调 iconbitmap() 的话，图标挂在**窗口类**上，标题栏认，
    但任务栏按钮经常不理它 —— 所以还得再补一发 WM_SETICON，
    大小两枚图标都按当前 DPI 现载，任务栏立刻就换。
    """
    try:
        import win_icon
        path = win_icon.app_icon_path()
        if not path:
            return False
        win.iconbitmap(default=path)

        user32 = ctypes.windll.user32
        WM_SETICON, ICON_BIG, ICON_SMALL = 0x0080, 1, 0
        dpi = 96
        try:
            dpi = user32.GetDpiForWindow(win.winfo_id()) or 96
        except Exception:
            pass
        scale = dpi / 96.0
        # 顶层窗口是 tk 内部控件的父窗口（跟 _dark_titlebar 同一套找法）
        targets = []
        inner = win.winfo_id()
        if inner:
            parent = user32.GetParent(inner)
            targets.append(parent or inner)
            targets.append(inner)
        for size, flag in ((32, ICON_BIG), (16, ICON_SMALL)):
            px = max(1, int(round(size * scale)))
            h = user32.LoadImageW(None, path, win_icon.IMAGE_ICON, px, px,
                                  win_icon.LR_LOADFROMFILE)
            if not h:
                continue
            for hwnd in targets:
                if hwnd:
                    user32.SendMessageW(hwnd, WM_SETICON, flag,
                                        ctypes.c_void_p(h))
        return True
    except Exception:
        return False


def _dark_titlebar(win, enable=True):
    """把系统标题栏刷成深色（enable=False 时刷回浅色）。

    Win10 1809 以后 DWM 才认识这个属性：新版编号 20，1809~1909 是 19。
    老系统上调用直接失败，静默跳过 —— 大不了标题栏还是浅的，不影响用。
    """
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception:
        return False
    hwnds = []
    try:
        hwnds.append(ctypes.windll.user32.GetParent(win.winfo_id()))
    except Exception:
        pass
    hwnds.append(win.winfo_id())
    val = ctypes.c_int(1 if enable else 0)
    for hwnd in hwnds:
        if not hwnd:
            continue
        for attr in (20, 19):
            try:
                hr = dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr,
                                                 ctypes.byref(val),
                                                 ctypes.sizeof(val))
            except Exception:
                hr = -1
            if hr == 0:
                return True
    return False


def run_gui(autoclose_ms=0):
    import tkinter as tk
    from tkinter import font as tkfont

    # 先声明本进程是 DPI 感知的，否则 Windows 会把整个窗口当位图放大，字会糊
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    class Dashboard(tk.Tk):
        def __init__(self):
            tk.Tk.__init__(self)
            self.cfg = tc.load_config()
            self.theme = self.cfg.get("theme", "auto")
            use_palette(self.effective_theme())     # 先定配色，再建窗口
            self.title(tc.UI_TITLE)
            self.configure(bg=BG_BOT)
            self.update_idletasks()
            # 设计稿坐标 → 实际像素的缩放比
            try:
                self.s = max(1.0, self.winfo_fpixels("1i") / 96.0)
            except Exception:
                self.s = 1.0
            w, h = self.S(DESIGN_W), self.S(DESIGN_H)
            self.geometry("%dx%d" % (w, h))
            # 先把「请求尺寸」落到窗口上，再上锁。
            # 顺序反了的话，个别情况下 Tk 会把当时的实际尺寸当成上限，
            # 窗口就会被 Windows 压成只剩标题栏三个按钮那么大。
            self.update_idletasks()
            self.minsize(w, h)
            self.resizable(False, False)
            _apply_app_icon(self)
            dark = self.effective_theme() == "dark"
            _dark_titlebar(self, dark)
            self.update_idletasks()
            # 有些系统要等窗口真正画出来才吃深色属性，稍后再刷一次
            self.after(250, lambda: _dark_titlebar(self, dark))

            self.cv = tk.Canvas(self, bg=BG_BOT, highlightthickness=0, bd=0)
            self.cv.pack(fill="both", expand=True)
            self.cv.bind("<Button-1>", self._on_click)
            self.cv.bind("<Motion>", self._on_motion)
            # 日志页要能往回翻。Windows 的滚轮是 <MouseWheel>，delta 按 120 一格。
            self.cv.bind("<MouseWheel>", self._on_wheel)

            self._font_cache = {}
            self._pic_cache = {}
            self._img_cache = {}
            self._keep = []          # 每帧临时生成的图片，防被回收
            self._hits = []
            self._hover = None
            self._busy = False
            self._job = None
            self.q = queue.Queue()
            self.pub_ts = {"keyboard": 0, "mouse": 0, "a7": 0}
            self.data = None

            self.interval = int(self.cfg.get("interval") or 0)
            self.style = self.cfg.get("tray_style", "dualnum")
            # 直接问系统「启动文件夹里到底有没有那个快捷方式」，
            # 不读配置里的意向值 —— 那个值可能是别的进程还没同步过来的旧状态。
            try:
                self.autostart = tc.is_autostart_enabled()
            except Exception:
                self.autostart = bool(self.cfg.get("autostart"))
            self.prev_pct = {}          # 上次显示过的电量，用来算「掉了多少」
            self._base = {}             # 点「立即刷新」那一刻的基准，防被别处刷新掉
            self._ghost = {}            # 正在播放的掉血残影：kind -> {prev, t0}
            self._ghost_job = None
            self._manual_pending = False
            # 页面切换：main=设备卡片 / log=电量记录
            self.view = "main"
            self.log_rows = []          # 日志页当前显示的数据（新的在前）
            self.log_off = 0            # 日志页从第几条开始显示
            self.log_hover = None       # 鼠标停在第几条上（停哪儿哪条才显示删除）
            self.log_pending = None     # 等确认删除的那条（时刻字符串）
            self._log_job = None        # 确认状态的超时定时器
            self._log_note = None       # 页脚临时提示，如「已删除」
            self._load_images()

            self.after(60, self.pump)
            self.after(120, self._poll_published)
            # 窗口显示后校正几次尺寸：万一被系统压成标题栏那么一点，自己长回来
            for _ms in (200, 800, 2000):
                self.after(_ms, self._enforce_size)
            self.bind("<Configure>", self._on_configure)
            self.refresh()
            self._reschedule()

        # ---- 窗口尺寸自愈 ----
        def _enforce_size(self):
            """把窗口拉回设计稿尺寸。不是简单粗暴地每次都设一遍：

            尺寸本来就对的时候直接返回，免得无谓地闪。
            """
            want = (self.S(DESIGN_W), self.S(DESIGN_H))
            if (self.winfo_width(), self.winfo_height()) == want:
                return
            self.resizable(True, True)          # 先解锁，否则可能被上限卡住
            self.minsize(*want)
            self.geometry("%dx%d" % want)
            self.update_idletasks()
            self.resizable(False, False)
            if self.data:
                self.redraw()

        def _on_configure(self, ev):
            """窗口大小被外面动了（切缩放、插拔屏）就补一次，防抖 400ms。"""
            if ev.widget is not self:
                return
            if getattr(self, "_fix_job", None):
                try:
                    self.after_cancel(self._fix_job)
                except Exception:
                    pass
            self._fix_job = self.after(400, self._enforce_size)

        # ---- 主题 ----
        def effective_theme(self):
            """配置写的 auto 就按系统走，其余两种情况照配置来。"""
            if self.theme in ("light", "dark"):
                return self.theme
            return "light" if tc.system_is_light() else "dark"

        # ---- 尺寸 / 字体 ----
        def S(self, v):
            return int(round(v * self.s))

        def F(self, px, bold=False):
            key = (int(px), bool(bold))
            if key not in self._font_cache:
                self._font_cache[key] = tkfont.Font(
                    family=FONT, size=-max(7, self.S(px)),
                    weight="bold" if bold else "normal")
            return self._font_cache[key]

        def _tw(self, px, text, bold=False):
            """量一段文字占多宽（设计稿单位）。

            注意要除以缩放比：字体是按物理像素建的，measure() 给的也是物理像素，
            而布局用的全是设计稿坐标。不除的话 150% 缩放下量出来的宽度会大 1.5 倍，
            按它算出来的胶囊能把设备名挤扁。
            """
            return self.F(px, bold).measure(text) / self.s

        # ---- 图形基元 ----
        # tk 的 Canvas 没有抗锯齿：create_arc 画出来的圆是一格一格点亮的，
        # 边缘那圈台阶就是肉眼看到的「毛刺」。圆角和圆弧一律交给 Pillow——
        # 在 4 倍尺寸上画，再 LANCZOS 缩回实际像素，边缘就有了半透明过渡。
        _SS = 4

        def _aa(self, key, w, h, painter):
            """画一张抗锯齿小图并缓存。w / h 是物理像素，painter(img, d, ss)。"""
            if not _PIL or w <= 0 or h <= 0:
                return None
            ck = (key, w, h)
            hit = self._img_cache.get(ck)
            if hit is not None:
                return hit
            big = Image.new("RGBA", (w * self._SS, h * self._SS), (0, 0, 0, 0))
            painter(big, ImageDraw.Draw(big), self._SS)
            im = pil_photo(big.resize((w, h), _RESAMPLE))
            if len(self._img_cache) > 160:      # 样式来回切时别把内存吃满
                self._img_cache.clear()
            self._img_cache[ck] = im
            return im

        def _round_pts(self, x0, y0, x1, y1, r, n=7):
            """没有 Pillow 时的退路：用多边形拼圆角（会有锯齿，但能用）。"""
            pts = []
            for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
                for i in range(n + 1):
                    a = math.radians(a0 + 90.0 * i / n)
                    pts.append(self.S(cx + r * math.cos(a)))
                    pts.append(self.S(cy + r * math.sin(a)))
            return pts

        def card(self, x0, y0, x1, y1, r=14, top=None, bot=None, line=None):
            """画一块带竖向渐变的圆角卡片。

            top / bot / line 用 None 表示「取当前配色」—— 不能写成默认参数，
            默认值是函数定义时求值的，换主题就永远停在原色上了。
            """
            top = top or CARD_TOP
            bot = bot or CARD_BOT
            line = CARD_LINE if line is None else line
            cv = self.cv
            w, h = self.S(x1) - self.S(x0), self.S(y1) - self.S(y0)
            rp = self.S(r)

            def paint(img, d, ss):
                t, b = _rgb(top), _rgb(bot)
                for i in range(h * ss):
                    f = i / max(1, h * ss - 1)
                    d.line([(0, i), (w * ss - 1, i)],
                           fill=(int(round(t[0] + (b[0] - t[0]) * f)),
                                 int(round(t[1] + (b[1] - t[1]) * f)),
                                 int(round(t[2] + (b[2] - t[2]) * f)), 255))
                mask = Image.new("L", (w * ss, h * ss), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0, 0, w * ss - 1, h * ss - 1], radius=rp * ss, fill=255)
                img.putalpha(mask)
                if line:
                    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1],
                                        radius=rp * ss, outline=_rgba(line),
                                        width=ss)

            im = self._aa(("card", rp, top, bot, line), w, h, paint)
            if im:
                cv.create_image(self.S(x0), self.S(y0), image=im, anchor="nw")
                return

            # ---- 退路：多边形打底 + 横线铺渐变 ----
            pts = self._round_pts(x0, y0, x1, y1, r)
            cv.create_polygon(pts, fill=bot, outline="")
            y0p, y1p = self.S(y0), self.S(y1)
            steps = max(12, min(140, (y1p - y0p) // 4))
            x0p, x1p = self.S(x0), self.S(x1)
            for i in range(steps):
                ya = y0p + (y1p - y0p) * i // steps
                yb = y0p + (y1p - y0p) * (i + 1) // steps
                col = _mix(top, bot, i / (steps - 1.0) if steps > 1 else 0)
                if ya >= y0p + rp and yb <= y1p - rp:
                    mid = (ya + yb) / 2.0
                    cv.create_line(x0p, mid, x1p, mid, fill=col,
                                   width=max(1, yb - ya))
                    continue
                for y in range(ya, yb):
                    dy = max(0.0, (y0p + rp) - y)
                    dy2 = max(0.0, y - (y1p - rp))
                    inset = max(rp - math.sqrt(max(0.0, rp * rp - dy * dy)),
                                rp - math.sqrt(max(0.0, rp * rp - dy2 * dy2)))
                    cv.create_line(x0p + inset, y, x1p - inset, y, fill=col)
            if line:
                cv.create_polygon(pts, fill="", outline=line,
                                  width=max(1, self.S(1)))

        def pill(self, x, y, w, h, text, px=10, fill=None, fg=None,
                 border=None, bold=False, cb=None, key=None):
            """胶囊按钮 / 标签。返回它的右边界（设计稿单位）。"""
            fill = fill or PILL_BG
            fg = fg or TEXT
            cv = self.cv
            wpx, hpx = self.S(w), self.S(h)
            rp = hpx // 2

            def paint(img, d, ss):
                box = [0, 0, wpx * ss - 1, hpx * ss - 1]
                d.rounded_rectangle(box, radius=rp * ss, fill=_rgba(fill))
                if border:
                    d.rounded_rectangle(box, radius=rp * ss,
                                        outline=_rgba(border), width=ss)

            im = self._aa(("pill", fill, border, rp), wpx, hpx, paint)
            if im:
                cv.create_image(self.S(x), self.S(y), image=im, anchor="nw")
            else:
                pts = self._round_pts(x, y, x + w, y + h, h / 2.0, n=6)
                cv.create_polygon(pts, fill=fill, outline=border or "",
                                  width=max(1, self.S(1)) if border else 0)
            if text:
                cv.create_text(self.S(x + w / 2.0), self.S(y + h / 2.0),
                               text=text, fill=fg, font=self.F(px, bold))
            if cb:
                self._hits.append((x, y, x + w, y + h, cb, key))
            return x + w

        def ring_dims(self, r, w):
            """环形表的全部尺寸（物理像素）：图边长、环半径、环宽、缝、三角长。

            画环、画三角、画残影三处都得用同一套数，所以只留这一份算法。
            Pillow 的 arc(width=) 是从**外沿向内**填的，所以 rp 本身就是外半径。
            """
            wpx = max(2, self.S(w))
            rp = self.S(r)
            gap = wpx * 0.18                     # 尖头和环外沿之间的缝
            tip = wpx * 0.62                     # 三角本身有多长
            dpx = 2 * int(round(rp + wpx / 2.0 + gap + tip)) + 2
            return dpx, rp, wpx, gap, tip

        def ring_geom(self, r, w):
            """残影要对齐主环，只需要前三个尺寸。"""
            dpx, rp, wpx, _, _ = self.ring_dims(r, w)
            return dpx, rp, wpx

        def arc_img(self, dpx, rp, wpx, p0, p1, color, alpha=95):
            """单独画一段百分比弧（p1 > p0），给掉血残影用。

            不能进 _aa 缓存 —— 每帧的角度都不一样，缓存只会白占内存。
            """
            if not _PIL:
                return None
            ss = self._SS
            big = Image.new("RGBA", (dpx * ss, dpx * ss), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            half = dpx * ss / 2.0
            rad = (rp + wpx / 2.0) * ss
            box = [half - rad, half - rad, half + rad, half + rad]
            d.arc(box, -90 + 3.6 * p0, -90 + 3.6 * p1,
                  fill=_rgba(color, alpha), width=wpx * ss)
            return pil_photo(big.resize((dpx, dpx), _RESAMPLE))

        def ring(self, cx, cy, r, w, pct, color):
            """环形电量表。12 点起顺时针。

            起点什么都不加 —— 圆弧本来就该从刻度上平切进来，多一个圆点像关节。
            终点在环外侧放一个小三角，**尖头朝里指着环**，但留一点缝：
            贴死了像粘上去的疙瘩，悬着一点才像「指着这里」。
            """
            cv = self.cv
            dpx, rp, wpx, gap, tip = self.ring_dims(r, w)
            p = 0 if pct is None else max(0, min(100, pct))
            end = -90 + 360.0 * p / 100.0

            def tip_pts(ccx, ccy, rad, wid, ang, out, gap):
                """终点小三角的三个点：尖头朝里，正对着圆环的外沿。

                三角站在环外面，尖头指向环、离环外沿留 gap 那么一点缝 ——
                贴死了会像粘上去的疙瘩，留一点才像「指着这里」。
                """
                a = math.radians(ang)
                ca, sa = math.cos(a), math.sin(a)
                tx, ty = -sa, ca                # 切线方向，用来摊开底边
                bh = wid * 0.55
                ro = rad + gap + out            # 底边（在外圈）
                ra = rad + gap                  # 尖头（对着环，但不碰）
                return [(ccx + ro * ca + tx * bh, ccy + ro * sa + ty * bh),
                        (ccx + ro * ca - tx * bh, ccy + ro * sa - ty * bh),
                        (ccx + ra * ca, ccy + ra * sa)]

            def paint(img, d, ss):
                half = dpx * ss / 2.0
                rad = (rp + wpx / 2.0) * ss
                box = [half - rad, half - rad, half + rad, half + rad]
                d.arc(box, 0, 360, fill=_rgba(TRACK), width=wpx * ss)
                if p <= 0:
                    return
                d.arc(box, -90, end, fill=_rgba(color), width=wpx * ss)
                if p < 99.5:                     # 满电时两端重合，就不画尖头了
                    d.polygon(tip_pts(half, half, rad, wpx * ss, end,
                                      tip * ss, gap * ss), fill=_rgba(color))

            im = self._aa(("ring2", wpx, p, color), dpx, dpx, paint)
            if im:
                cv.create_image(self.S(cx), self.S(cy), image=im,
                                anchor="center")
                return

            # ---- 退路：tk 的 arc 没有抗锯齿，也画不了尖头，只能凑合 ----
            box = (self.S(cx - r), self.S(cy - r), self.S(cx + r),
                   self.S(cy + r))
            cv.create_arc(*box, start=90, extent=-359.9, style="arc",
                          outline=TRACK, width=wpx)
            if p:
                cv.create_arc(*box, start=90, extent=-359.9 * p / 100.0,
                              style="arc", outline=color, width=wpx)

        def dot(self, x, y, r, color):
            self.cv.create_oval(self.S(x - r), self.S(y - r), self.S(x + r),
                                self.S(y + r), fill=color, outline="")

        # ---- 图片 ----
        def _load_images(self):
            self.imgs = {}
            self.img_err = {}           # 哪一步失败、为什么（排障用）
            for key, name in ASSET_FILES.items():
                path = os.path.join(ASSETS, name)
                img = None
                if _PIL:
                    try:
                        img = Image.open(path).convert("RGBA")
                    except Exception as e:
                        self.img_err[key + "/pil"] = repr(e)
                if img is None:
                    try:
                        img = tk.PhotoImage(file=path)
                    except Exception as e:
                        self.img_err[key + "/tk"] = repr(e)
                self.imgs[key] = img
                if img is None:
                    self.img_err[key + "/path"] = path

        def pic(self, key, max_w, max_h):
            """把产品图缩到刚好放进 (max_w, max_h) —— 按物理像素算，1:1 上屏。"""
            src = self.imgs.get(key)
            if src is None or max_w <= 2 or max_h <= 2:
                return None
            ck = (key, int(max_w), int(max_h))
            if ck in self._pic_cache:
                return self._pic_cache[ck]

            im = None
            if _PIL and isinstance(src, Image.Image):
                k = min(max_w / float(src.width), max_h / float(src.height))
                tw = max(1, int(round(src.width * k)))
                th = max(1, int(round(src.height * k)))
                im = pil_photo(src.resize((tw, th), _RESAMPLE))
                if im is None:
                    # 缩图这条路断了，退回让 tk 直接读原始 png：尺寸会偏大，但能看
                    try:
                        im = tk.PhotoImage(
                            file=os.path.join(ASSETS, ASSET_FILES.get(key, "")))
                    except Exception:
                        im = None
            else:
                # 没有 Pillow：tk 只能整数倍缩放，挑一个最接近且不超框的组合
                need = min(max_w / float(src.width()),
                           max_h / float(src.height()))
                best = None
                for z in range(1, 7):
                    for d in range(1, 7):
                        ratio = z / float(d)
                        if (src.width() * ratio > max_w * 1.02
                                or src.height() * ratio > max_h * 1.02):
                            continue
                        err = abs(ratio - need)
                        if best is None or err < best[0]:
                            best = (err, z, d)
                if best is None:
                    return None
                _, z, d = best
                im = src
                if d > 1:
                    im = im.subsample(d)
                if z > 1:
                    im = im.zoom(z)
            self._pic_cache[ck] = im
            return im

        # ---- 整屏绘制 ----
        def redraw(self):
            cv = self.cv
            cv.delete("all")
            self._keep = []
            self._hits = []
            self._bg()
            if self.view == "log":
                self._draw_log()        # 日志页自己画标题，不再画设备卡片
                return
            self._header()
            kb, ms = self.data if self.data else (None, None)
            top = HEADER_H + 22
            self._card(MARGIN, top, CARD_W, CARD_H, kb or
                       {"name": "迈从 G87 V2", "kind": "keyboard", "rows": []})
            self._card(MARGIN + CARD_W + GAP, top, CARD_W, CARD_H,
                       ms or {"name": tc.MOUSE_SLOTS.get(mouse_slot(), "鼠标"),
                              "kind": mouse_slot(), "rows": []})
            self._bar(MARGIN, top + CARD_H + 18, DESIGN_W - MARGIN * 2, BAR_H)

        def _bg(self):
            cv = self.cv
            h = self.S(DESIGN_H)
            steps = 40
            for i in range(steps):
                ya = h * i // steps
                yb = h * (i + 1) // steps
                col = _mix(BG_TOP, BG_BOT, i / (steps - 1.0))
                cv.create_rectangle(0, ya, self.S(DESIGN_W), yb + 1,
                                    fill=col, outline=col)

        def _header(self):
            cv = self.cv
            cv.create_text(self.S(MARGIN), self.S(14), text="外设电量",
                           anchor="nw", fill=TEXT, font=self.F(24, True))
            # 副标题跟着实际接口走：键盘可能插线，也可能走 2.4G，别写死
            conns = []
            for d in (self.data or ()):
                c = d.get("conn")
                if c and c not in conns:
                    conns.append(c)
            link = " + ".join(conns) if conns else "2.4G"
            n = len(self.data) if self.data else 2
            sub = "%s · %d 台设备" % (link, n)
            if self.data and any(not d.get("ok") for d in self.data):
                sub = "%s · 有设备未响应" % link
            cv.create_text(self.S(MARGIN), self.S(51), text=sub, anchor="nw",
                           fill=FAINT, font=self.F(12))

            # 右上角：更新时间 + 「电量记录」入口。
            # 入口放这儿是没得选 —— 底部那条工具栏已经有三个下拉、开机自启和
            # 立即刷新，再塞一个就挤成一根线了；标题行右边本来就是空的。
            label = "电量记录"
            pw = self._tw(12, label) + 34
            px = DESIGN_W - MARGIN - pw
            self.pill(px, 19, pw, 28, label, px=12,
                      fill=PILL_BG2, border=PILL_LINE,
                      cb=self.open_log, key="log")
            stamp = self._stamp or "正在读取…"
            cv.create_text(self.S(px - 14), self.S(33), text=stamp,
                           anchor="e", fill=SUB, font=self.F(12))

        def _card(self, x, y, w, h, d):
            cv = self.cv
            self.card(x, y, x + w, y + h)
            band = 152
            kind = d.get("kind")

            pic = self.pic(kind, self.S(w - 40), self.S(band - 12))
            if pic:
                cv.create_image(self.S(x + w / 2.0), self.S(y + band / 2.0 + 2),
                                image=pic, anchor="center")
            else:
                cv.create_text(self.S(x + w / 2.0), self.S(y + band / 2.0 - 8),
                               text="放一张产品图到", fill=FAINT,
                               font=self.F(11))
                cv.create_text(self.S(x + w / 2.0), self.S(y + band / 2.0 + 12),
                               text="assets/%s.png" % (kind or ""),
                               fill=SUB, font=self.F(11, True))

            name = d.get("name", "设备")
            # 卡片从 356 缩到 320，长名字（迈从 A7 V3 Ultra+）按 17 号会顶到右边
            # 的胶囊上，所以量一下宽度，放不下就一级一级往下缩
            npx = 17
            while npx > 12 and self._tw(npx, name, True) > (w - 20 - 18 - 150):
                npx -= 1
            cv.create_text(self.S(x + 20), self.S(y + band + 2), text=name,
                           anchor="nw", fill=TEXT, font=self.F(npx, True))

            # 右上角：网页驱动入口 + 连接方式
            ok = d.get("ok")
            conn = d.get("conn", "—")
            cw2 = self._tw(11, conn) + 30
            cx2 = x + w - 18 - cw2
            pill_y = y + band + 2
            self.pill(cx2, pill_y, cw2, 24, "", fill=CHIP_BG)
            self.dot(cx2 + 12, pill_y + 12, 3.2, GREEN if ok else GREY)
            cv.create_text(self.S(cx2 + 21), self.S(pill_y + 12), text=conn,
                           anchor="w", fill=SUB, font=self.F(11))

            url = DRIVER_URL.get(kind or "")
            if url:
                dw = self._tw(11, "驱动") + 26
                dx = cx2 - 8 - dw
                self.pill(dx, pill_y, dw, 24, "驱动 ↗", px=11,
                          fill=DRIVER_BG, fg=DRIVER_FG, border=DRIVER_LINE,
                          cb=lambda e, u=url: webbrowser.open(u), key="driver")

            # 环形电量表
            cy = y + band + 104
            pct = d.get("battery")
            color = level_color(pct)
            self.ring(x + 78, cy, 44, 10, pct, color)

            # 手动刷新后掉电了：先留一段回缩的残影
            # （就是游戏里血条的延迟扣减，一眼能看出这轮掉了多少）
            ghost = self._ghost.get(kind)
            dropping = bool(ghost and pct is not None and ghost["prev"] > pct)
            if dropping:
                # 前 GHOST_HOLD 秒静止不动，之后才按 ease 回缩
                t = (time.time() - ghost["t0"]) * 1000.0
                a = max(0.0, min(1.0, (t - GHOST_HOLD * 1000.0)
                                 / (GHOST_ANIM * 1000.0)))
                ease = 1.0 - (1.0 - a) ** 3     # 先快后慢，像血条缩回去
                hi = ghost["prev"] - (ghost["prev"] - pct) * ease
                if hi > pct + 0.2:
                    dpx, rp, wpx = self.ring_geom(44, 10)
                    im = self.arc_img(dpx, rp, wpx, pct, hi, color, 95)
                    if im:
                        self._keep.append(im)      # 不握住引用会被回收
                        cv.create_image(self.S(x + 78), self.S(cy),
                                        image=im, anchor="center")
                cv.create_text(self.S(x + 78), self.S(cy - 31),
                               text="-%d" % (ghost["prev"] - pct), fill=RED,
                               font=self.F(12, True))
            if pct is None:
                cv.create_text(self.S(x + 78), self.S(cy), text="--",
                               fill=FAINT, font=self.F(25, True))
            else:
                # 「48%」当成一个整体：先量总宽，从圆心往两边摊开，
                # 两位数和三位数（100%）都能真正居中，不会像以前那样偏一边
                num = str(pct)
                f1, f2 = self.F(27, True), self.F(13, True)
                nw = f1.measure(num)
                pw = f2.measure("%")
                gap = self.S(2)
                # 量出来的宽度和画布坐标都得是物理像素，别一个用设计稿一个用像素
                cxp = self.S(x + 78)
                x0 = cxp - (nw + gap + pw) / 2.0
                base = self.S(cy) + self.S(9)      # 数字和百分号共用一条基线
                cv.create_text(x0, base + f1.metrics("descent"), text=num,
                               anchor="sw", fill=color, font=f1)
                cv.create_text(x0 + nw + gap, base + f2.metrics("descent"),
                               text="%", anchor="sw", fill=color, font=f2)
            if dropping:
                # 环里那个「-40」已经说清掉了多少，这里只交代上次是多少
                label = "上次 %d%%" % ghost["prev"]
                lcolor = AMBER
            elif d.get("charging"):
                label, lcolor = "充电中", GREEN
            elif d.get("stale"):
                label, lcolor = "上次读数", FAINT
            else:
                label, lcolor = "电量", FAINT
            if d.get("ring_label"):
                label, lcolor = d["ring_label"], FAINT
            cv.create_text(self.S(x + 78), self.S(cy + 24), text=label,
                           fill=lcolor, font=self.F(10))

            # 右侧参数行
            lx = x + 140
            ry = y + band + 50
            for k, v in (d.get("rows") or [])[:4]:
                cv.create_text(self.S(lx), self.S(ry), text=k, anchor="nw",
                               fill=FAINT, font=self.F(12))
                cv.create_text(self.S(lx + 50), self.S(ry), text=str(v),
                               anchor="nw", fill=SUB, font=self.F(12))
                ry += 27

            # 鼠标区多一行：换另一只鼠标。
            # 不是「设备列表」——位置就两个（键盘位、鼠标位），换的只是右格放谁。
            # 摆在卡片右下角当脚注，不挤在参数行下面，两行/三行参数都不会打架。
            if kind in ("mouse", "a7"):
                other = "mouse" if kind == "a7" else "a7"
                label = "⇄ 换成" + ("雷柏" if other == "mouse" else "迈从")
                pw = self._tw(11, label) + 24
                self.pill(x + w - 18 - pw, y + h - 40, pw, 24, label, px=11,
                          fill=PILL_BG2, border=PILL_LINE,
                          cb=self._pick_mouse_slot, key="slot")

        def _bar(self, x, y, w, h):
            cv = self.cv
            self.card(x, y, x + w, y + h, r=12, top=PANEL_TOP, bot=PANEL_BOT)

            cy = y + h / 2.0
            gap = 18
            cur = x + 20

            def labeled(title, value, key, pick, minw):
                nonlocal cur
                cv.create_text(self.S(cur), self.S(cy), text=title, anchor="w",
                               fill=FAINT, font=self.F(12))
                cur += self._tw(12, title) + 10
                pw = max(minw, self._tw(12, value) + 34)
                self.pill(cur, cy - 14, pw, 28, value + "  ▾", px=12,
                          fill=PILL_BG2, border=PILL_LINE, cb=pick, key=key)
                cur += pw + gap

            labeled("刷新间隔", self._interval_label(), "interval",
                    self._pick_interval, 78)
            labeled("主题", self._theme_label(), "theme", self._pick_theme, 84)
            labeled("托盘图标", self._style_label(), "style",
                    self._pick_style, 96)

            # 右侧：立即刷新 + 开机自启
            rx = x + w - 20
            bw = self._tw(12, "立即刷新", True) + 38
            rx -= bw
            self.pill(rx, cy - 15, bw, 30, "立即刷新", px=12, fill=ACCENT,
                      fg="#FFFFFF", bold=True, cb=self._manual_refresh,
                      key="refresh")

            lw = self._tw(12, "开机自启")
            box = 20
            rx -= 26 + lw + 9 + box
            by = cy - box / 2.0
            if self.autostart:
                self.pill(rx, by, box, box, "", fill=ACCENT, cb=self.toggle_auto,
                          key="auto")
                cv.create_line(self.S(rx + 5), self.S(by + 10), self.S(rx + 8.5),
                               self.S(by + 13.5), self.S(rx + 15), self.S(by + 5.5),
                               fill="#FFFFFF", width=max(2, self.S(2)),
                               capstyle="round", joinstyle="round")
            else:
                self.pill(rx, by, box, box, "", fill=BOX_OFF,
                          border=BOX_LINE, cb=self.toggle_auto, key="auto")
            cv.create_text(self.S(rx + box + 9), self.S(cy), text="开机自启",
                           anchor="w", fill=SUB, font=self.F(12))

        # ---- 日志页 ----
        # 一次刷新＝一条记录，两台设备并排摆两栏。
        # 早先是一台设备一行、各写一次时间，看着像两个不同时刻发生的事，
        # 所以改成「一个时刻一条」，时间只出现一次。
        # 下面这些坐标都相对「卡片左边 + 20」。
        #
        # 每组是 (组中心, 组名, 电量右边缘, 变化右边缘)：
        # 设备名用 anchor="center" 挂在**组中心**上，于是「电量」落在它左下、
        # 「变化」落在它右下。早先是组名左对齐，两个子列全跑到名字右边去了。
        LOG_DEV = ((202, "键盘", 180, 250),
                   (402, "鼠标", 380, 450))
        LOG_GAP_X = 560           # 间隔（右对齐）
        LOG_DEL_X = 688           # 删除控件（右对齐）
        LOG_ROW_H = 30
        LOG_TOP = 88
        LOG_BOT = DESIGN_H - 22
        LOG_H1 = 24               # 表头第一行：设备名（组标题）
        LOG_H2 = 44               # 表头第二行：电量 / 变化
        LOG_RULE = 58             # 表头下的横线
        LOG_FIRST = 78            # 第一条记录的中心线

        def open_log(self, e=None):
            self.log_off = 0
            self.log_hover = None
            self.log_pending = None
            self.log_rows = tc.read_log(LOG_VIEW_MAX)
            self.view = "log"
            self.redraw()

        def close_log(self, e=None):
            self.view = "main"
            self.log_hover = None
            self.log_pending = None
            self.redraw()

        def _log_visible(self):
            # 底部留 62 给页脚（分隔线 + 行号），再多一行就贴到线上了
            return max(1, int((self.LOG_BOT - 62
                               - (self.LOG_TOP + self.LOG_FIRST))
                              // self.LOG_ROW_H) + 1)

        def _clamp_log(self, vis=None):
            """别让滚动越界。列表会变长变短，每次画之前都校一下。"""
            if vis is None:
                vis = self._log_visible()
            top = max(0, len(self.log_rows) - vis)
            self.log_off = max(0, min(self.log_off, top))

        def _on_wheel(self, e):
            if self.view != "log" or not self.log_rows:
                return
            old = self.log_off
            self.log_off += -3 if e.delta > 0 else 3
            self._clamp_log()
            if self.log_off != old:
                self.redraw()

        def _log_row_at(self, px, py):
            """鼠标停在第几条上。不在任何一行上就返回 None。"""
            dx, dy = px / self.s, py / self.s
            if not (MARGIN <= dx <= DESIGN_W - MARGIN):
                return None
            first = self.LOG_TOP + self.LOG_FIRST
            if dy < first - self.LOG_ROW_H / 2.0:
                return None
            i = int((dy - first + self.LOG_ROW_H / 2.0) // self.LOG_ROW_H)
            if i < 0 or i >= self._log_visible():
                return None
            idx = self.log_off + i
            return idx if idx < len(self.log_rows) else None

        def _draw_log(self):
            cv = self.cv
            lx = MARGIN + 20
            top, bot = self.LOG_TOP, self.LOG_BOT
            rows = self.log_rows
            vis = self._log_visible()
            self._clamp_log(vis)

            cv.create_text(self.S(MARGIN), self.S(14), text="电量记录",
                           anchor="nw", fill=TEXT, font=self.F(24, True))
            if rows:
                sub = "最近 %d 条 · 每次刷新记一条 · 存在 %s" % (
                    len(rows), tc.LOG_NAME)
            else:
                sub = "每次刷新记一条 · 存在 %s" % tc.LOG_NAME
            cv.create_text(self.S(MARGIN), self.S(51), text=sub, anchor="nw",
                           fill=FAINT, font=self.F(12))

            label = "‹  返回"
            pw = self._tw(13, label) + 36
            self.pill(DESIGN_W - MARGIN - pw, 19, pw, 28, label, px=13,
                      fill=PILL_BG2, border=PILL_LINE,
                      cb=self.close_log, key="back")

            self.card(MARGIN, top, DESIGN_W - MARGIN, bot, r=14)

            # 表头两排：上排是设备名（挂在各自那两列的正中间），
            # 下排是子列「电量 / 变化」。「时间」「间隔」这种只有一列的东西
            # 跟「电量 / 变化」排在同一水平线上 —— 摆在两排正中间的话，
            # 会浮在字缝里，看着比下面那排高出一截，像是没对齐。
            y1 = top + self.LOG_H1
            y2 = top + self.LOG_H2
            cv.create_text(self.S(lx), self.S(y2), text="时间", anchor="w",
                           fill=FAINT, font=self.F(11))
            for gx, gname, cx, cdx in self.LOG_DEV:
                cv.create_text(self.S(lx + gx), self.S(y1),
                               text=gname, anchor="center", fill=SUB,
                               font=self.F(11, True))
                cv.create_text(self.S(lx + cx), self.S(y2),
                               text="电量", anchor="e", fill=FAINT,
                               font=self.F(11))
                cv.create_text(self.S(lx + cdx), self.S(y2),
                               text="变化", anchor="e", fill=FAINT,
                               font=self.F(11))
            cv.create_text(self.S(lx + self.LOG_GAP_X), self.S(y2), text="间隔",
                           anchor="e", fill=FAINT, font=self.F(11))
            cv.create_line(self.S(lx), self.S(top + self.LOG_RULE),
                           self.S(lx + self.LOG_DEL_X), self.S(top + self.LOG_RULE),
                           fill=CARD_LINE, width=max(1, self.S(1)))

            if not rows:
                cv.create_text(self.S(MARGIN + (DESIGN_W - MARGIN * 2) / 2.0),
                               self.S(top + 150), text="还没有记录",
                               fill=SUB, font=self.F(15, True))
                cv.create_text(self.S(MARGIN + (DESIGN_W - MARGIN * 2) / 2.0),
                               self.S(top + 178),
                               text="回首页点「立即刷新」，这里就会出现第一条",
                               fill=FAINT, font=self.F(12))
                self._log_footer(bot, 0, 0, 0)
                return

            first_y = top + self.LOG_FIRST
            for i in range(vis):
                idx = self.log_off + i
                if idx >= len(rows):
                    break
                e = rows[idx]
                y = first_y + i * self.LOG_ROW_H
                hovered = (idx == self.log_hover)
                pending = (self.log_pending == e["when"])
                if hovered or pending:
                    cv.create_rectangle(
                        self.S(lx - 10), self.S(y - self.LOG_ROW_H / 2.0 + 2),
                        self.S(lx + self.LOG_DEL_X), self.S(y + self.LOG_ROW_H / 2.0 - 2),
                        fill=PILL_BG2, outline="")
                elif i:
                    cv.create_line(self.S(lx), self.S(y - self.LOG_ROW_H / 2.0),
                                   self.S(lx + self.LOG_DEL_X),
                                   self.S(y - self.LOG_ROW_H / 2.0),
                                   fill=CARD_LINE, width=max(1, self.S(1)))

                # 时间只到分钟：秒在这一页没意义，还挤占别的地方
                cv.create_text(self.S(lx), self.S(y), text=e["when"][5:16],
                               anchor="w", fill=SUB, font=self.F(12))

                by_slot = {r["slot"]: r for r in e["rows"]}
                for gx, gname, cx, cdx in self.LOG_DEV:
                    r = by_slot.get(gname)
                    if not r:
                        # 这一轮没读到它（睡着了），留空比编一个数好
                        cv.create_text(self.S(lx + cx), self.S(y), text="—",
                                       anchor="e", fill=FAINT,
                                       font=self.F(13, True))
                        cv.create_text(self.S(lx + cdx), self.S(y), text="",
                                       anchor="e", fill=FAINT,
                                       font=self.F(13, True))
                        continue
                    cv.create_text(self.S(lx + cx), self.S(y),
                                   text="%d%%" % r["pct"], anchor="e",
                                   fill=GREEN if r["charging"] else TEXT,
                                   font=self.F(13, True))
                    delta, dcolor = self._delta_text(r["delta"])
                    cv.create_text(self.S(lx + cdx), self.S(y), text=delta,
                                   anchor="e", fill=dcolor, font=self.F(12, True))

                cv.create_text(self.S(lx + self.LOG_GAP_X), self.S(y),
                               text=e["gap"] or "—", anchor="e", fill=SUB,
                               font=self.F(12))

                # 只在停着（或正等确认）的那一行上挂删除控件，
                # 不然整片区域都是隐形的删除按钮，点到就删，太吓人。
                if hovered or pending:
                    self._log_delete(lx + self.LOG_DEL_X, y, e)

            self._log_footer(bot, self.log_off,
                             min(len(rows), self.log_off + vis), len(rows))

        def _delta_text(self, raw):
            """「+0」说成「持平」—— 写成 +0% 看着像涨了，其实没动。"""
            if not raw:
                return "—", FAINT
            if raw in ("+0", "0"):
                return "持平", FAINT
            if raw.startswith("+"):
                return raw + "%", GREEN
            return raw.replace("-", "−") + "%", TEXT

        def _log_delete(self, right, y, entry):
            """行尾的删除控件。

            平时不显示，鼠标停到这一行才冒出来一个淡灰的「×」——
            删除是很偶尔才用一次的操作，不该一直杵在那儿抢视线。
            点一下只是「上膛」（变成红色的「确认删除」），再点一下才真删，
            3 秒不动就自己取消，免得手滑删掉一条正常记录。
            """
            cv = self.cv
            pending = (self.log_pending == entry["when"])
            text = "确认删除" if pending else "×"
            color = RED if pending else FAINT
            w = self._tw(11, text) + 16
            cv.create_text(self.S(right), self.S(y), text=text, anchor="e",
                           fill=color, font=self.F(11, pending))
            self._hits.append((right - w, y - self.LOG_ROW_H / 2.0,
                               right, y + self.LOG_ROW_H / 2.0,
                               lambda ev, wh=entry["when"]: self._delete_entry(wh),
                               "del"))

        def _delete_entry(self, when):
            if self.log_pending != when:
                self.log_pending = when
                if self._log_job:
                    self.after_cancel(self._log_job)
                self._log_job = self.after(3500, self._clear_pending)
                self.redraw()
                return

            if self._log_job:
                self.after_cancel(self._log_job)
                self._log_job = None
            ok = tc.delete_log_entry(when)
            self.log_pending = None
            self.log_hover = None
            self.log_rows = tc.read_log(LOG_VIEW_MAX)
            self._clamp_log()
            self._log_note = ("已删除" if ok else "删不掉：文件可能正被别的程序打开",
                              GREEN if ok else RED)
            self.redraw()
            self.after(2200, self._clear_note)

        def _clear_pending(self):
            self._log_job = None
            if self.log_pending:
                self.log_pending = None
                self.redraw()

        def _clear_note(self):
            if self._log_note:
                self._log_note = None
                self.redraw()

        def _log_footer(self, bot, first, last, total):
            cv = self.cv
            lx = MARGIN + 20
            # 跟列表之间拉一条分隔线：不加的话最后一行的数字会和「第几条」挤成一片
            cv.create_line(self.S(lx), self.S(bot - 44),
                           self.S(lx + self.LOG_DEL_X), self.S(bot - 44),
                           fill=CARD_LINE, width=max(1, self.S(1)))
            y = bot - 22
            if total:
                txt = "第 %d–%d 条 / 共 %d 条" % (first + 1, last, total)
            else:
                txt = "暂无记录"
            cv.create_text(self.S(lx), self.S(y), text=txt, anchor="w",
                           fill=FAINT, font=self.F(11))
            if self._log_note:
                note, color = self._log_note
                cv.create_text(self.S(lx + self.LOG_DEL_X), self.S(y),
                               text=note, anchor="e", fill=color,
                               font=self.F(11, True))
            elif total > self._log_visible():
                cv.create_text(self.S(lx + self.LOG_DEL_X), self.S(y),
                               text="滚轮翻看 · 停在某条上可以删掉它",
                               anchor="e", fill=FAINT, font=self.F(11))

        # ---- 设置 ----
        def _interval_label(self):
            for label, secs in INTERVALS:
                if secs == self.interval:
                    return label
            return "手动"

        def _style_label(self):
            for label, val in TRAY_STYLES:
                if val == self.style:
                    return label
            return TRAY_STYLES[0][0]

        def _theme_label(self):
            for label, val in THEMES:
                if val == self.theme:
                    return label
            return THEMES[0][0]

        def _save(self, **kw):
            cfg = tc.load_config()
            cfg.update(kw)
            tc.save_config(cfg)
            tc.post_to_tray(tc.MSG_RELOAD)

        def _menu(self, event, items, current, pick):
            import tkinter as tk
            m = tk.Menu(self, tearoff=0, bg=PILL_BG2, fg=TEXT, bd=0,
                        activebackground=ACCENT, activeforeground="#FFFFFF",
                        font=self.F(10))
            for label, val in items:
                m.add_command(label=("●  " if val == current else "     ") + label,
                              command=lambda v=val: pick(v))
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                m.grab_release()

        def _pick_interval(self, e):
            self._menu(e, INTERVALS, self.interval, self.set_interval)

        def _pick_style(self, e):
            self._menu(e, TRAY_STYLES, self.style, self.set_style)

        def _pick_theme(self, e):
            self._menu(e, THEMES, self.theme, self.set_theme)

        def _pick_mouse_slot(self, e):
            items = [(name, key) for key, name in tc.MOUSE_SLOTS.items()]
            self._menu(e, items, mouse_slot(), self.set_mouse_slot)

        def set_mouse_slot(self, val):
            """换鼠标位放哪只。切换后重新读一次，不然格子里还是上一只的数。"""
            self._save(mouse_slot=val)
            self.prev_pct.pop("a7", None)
            self.prev_pct.pop("mouse", None)
            self._ghost.pop("a7", None)
            self._ghost.pop("mouse", None)
            self.refresh()

        def set_interval(self, secs):
            self.interval = secs
            self._save(interval=secs)
            self._reschedule()
            self.redraw()

        def set_style(self, val):
            self.style = val
            self._save(tray_style=val)
            self.redraw()

        def set_theme(self, val):
            """切配色。面板和托盘共用这一个配置项。

            换主题等于换了一整套颜色，所以缓存必须整个倒掉 ——
            _img_cache 里存的都是按旧颜色画好的位图，不清会拿旧图来贴。
            清空是安全的：下一次 redraw 会按新颜色重新画、重新缓存。
            """
            self.theme = val
            use_palette(self.effective_theme())
            self._img_cache.clear()
            self._pic_cache.clear()
            self.configure(bg=BG_BOT)
            self.cv.config(bg=BG_BOT)
            _dark_titlebar(self, self.effective_theme() == "dark")
            self._save(theme=val)      # 顺便通知托盘换图标配色
            self.redraw()

        def toggle_auto(self, e=None):
            want = not self.autostart
            tc.set_autostart(want)
            actual = tc.is_autostart_enabled()     # 以系统真实状态为准，别骗人
            self.autostart = bool(actual)
            self._save(autostart=actual)
            self.redraw()

        def _reschedule(self):
            if self._job:
                try:
                    self.after_cancel(self._job)
                except Exception:
                    pass
                self._job = None
            if self.interval > 0:
                self._job = self.after(self.interval * 1000, self._tick)

        def _tick(self):
            self.refresh()
            self._job = self.after(self.interval * 1000, self._tick)

        # ---- 交互 ----
        def _hit_test(self, px, py):
            for x0, y0, x1, y1, cb, key in self._hits:
                if (self.S(x0) <= px <= self.S(x1)
                        and self.S(y0) <= py <= self.S(y1)):
                    return cb, key
            return None, None

        def _on_click(self, e):
            cb, _ = self._hit_test(e.x, e.y)
            if cb:
                cb(e)

        def _on_motion(self, e):
            if self.view == "log":
                # 行尾那个删除控件只在「正停着的那一行」上出现，
                # 所以鼠标挪到另一行才重画，不是每动一个像素就重画。
                idx = self._log_row_at(e.x, e.y)
                if idx != self.log_hover:
                    self.log_hover = idx
                    self.redraw()
            cb, key = self._hit_test(e.x, e.y)
            if key != self._hover:
                self._hover = key
                self.cv.config(cursor="hand2" if cb else "")

        # ---- 数据 ----
        _stamp = ""

        def refresh(self, e=None):
            if self._busy:
                return
            self._busy = True
            self._stamp = "正在读取…"
            self.redraw()
            threading.Thread(target=lambda: self.q.put(read_all()),
                             daemon=True).start()

        def _manual_refresh(self, e=None):
            """点「立即刷新」：这一轮读完要播掉电残影。

            基准电量在**点击这一刻**就抄一份存起来。不能等到读数回来再比 ——
            读键盘的那条线程会顺手把新电量写进配置，而 _poll_published
            每秒都会去读配置，很可能抢先一步把 prev_pct 改成新值，
            等 pump 回来时就没得比了（深色那张键盘卡就是这么被吃掉的）。
            """
            if self._busy:
                return
            self._base = dict(self.prev_pct)
            self._manual_pending = True
            self.refresh()

        def _ghost_tick(self):
            """推进掉电残影。全部播完就停表，不留后台任务空转。"""
            self._ghost_job = None
            now = time.time()
            live = {k: g for k, g in self._ghost.items()
                    if now - g["t0"] < GHOST_MS / 1000.0}
            self._ghost = live
            self.redraw()
            if live:
                self._ghost_job = self.after(GHOST_STEP, self._ghost_tick)

        def _start_ghosts(self):
            if self._ghost_job is None and self._ghost:
                self._ghost_job = self.after(GHOST_STEP, self._ghost_tick)

        def _note_reading(self, data, manual):
            """把这一轮读数记进「上次电量」，并决定要不要播残影。"""
            base = self._base if manual else {}
            if manual:
                self._base = {}
            for d in data:
                kind = d.get("kind")
                pct = d.get("battery")
                if kind is None or pct is None:
                    continue
                prev = base.get(kind) if manual else self.prev_pct.get(kind)
                # 只有「手动刷新 + 确实掉了」才播。充电涨回去、持平都不播。
                if manual and prev is not None and pct < prev:
                    self._ghost[kind] = {"prev": prev, "t0": time.time()}
                self.prev_pct[kind] = pct

        def pump(self):
            try:
                while True:
                    new = self.q.get_nowait()
                    manual, self._manual_pending = self._manual_pending, False
                    self._note_reading(new, manual)
                    self.data = new
                    self._busy = False
                    self._stamp = "更新于 " + time.strftime("%H:%M:%S")
                    self._log_round(new)
                    self.redraw()
                    self._start_ghosts()
            except queue.Empty:
                pass
            self.after(140, self.pump)

        def _log_round(self, data):
            """这一轮读到的电量记进日志。

            只记**真读到的**：`stale` 是从缓存/另一个进程翻出来的旧值，
            把它当新样本写进去，曲线就会变成一条假的水平线。
            """
            items = []
            for d in data or ():
                if d.get("battery") is None or d.get("stale") or d.get("no_battery"):
                    continue
                slot = "键盘" if d.get("kind") == "keyboard" else "鼠标"
                items.append((slot, d.get("name") or "设备", d["battery"],
                              bool(d.get("charging"))))
            if not items:
                return
            try:
                tc.log_sample(items)
            except Exception:
                return              # 日志写不进去（文件被占/没权限）不该影响读数
            if self.view == "log":
                self.log_rows = tc.read_log(LOG_VIEW_MAX)
                self._clamp_log()

        def _poll_published(self):
            """看托盘有没有读到新值。托盘常驻，它读到了这里直接显示。"""
            got_new = False
            for i, key in enumerate(("keyboard", mouse_slot())):
                # 有线键盘不报电量，就别让配置里那条旧的无线读数再把它填回去
                if self.data and i < len(self.data) and self.data[i].get("no_battery"):
                    continue
                got = tc.read_published(key)
                if not got:
                    continue
                pct, chg, ts = got
                if ts <= self.pub_ts.get(key, 0):
                    continue
                self.pub_ts[key] = ts
                self.prev_pct[key] = pct        # 基准跟着走，下次手动刷新才比得准
                if self.data:
                    d = list(self.data)
                    d[i] = dict(d[i])
                    if d[i].get("battery") != pct or bool(d[i].get("charging")) != chg:
                        d[i].update(battery=pct, charging=chg, ok=True)
                        self.data = tuple(d)
                        got_new = True
            if got_new:
                self.redraw()
            self.after(1000, self._poll_published)

    app = Dashboard()
    app.redraw()
    if autoclose_ms:
        app.after(autoclose_ms, app.destroy)
    app.mainloop()
    return 0


# --------------------------------------------------------------------------
def run_once():
    for d in read_all():
        if d.get("no_battery"):
            print("%-16s %s" % (d["name"], d.get("ring_label") or "无电量"))
        elif d["ok"] and d["battery"] is not None:
            print("%-16s %3d%%  %s%s" % (d["name"], d["battery"],
                                         "充电中" if d["charging"] else "未充电",
                                         "（上次读数）" if d.get("stale") else ""))
        else:
            print("%-16s 读不到" % d["name"])
        for k, v in d["rows"]:
            print("    %-8s %s" % (k, v))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="打印一次就退出")
    args = ap.parse_args()

    if args.once:
        return run_once()

    try:
        mchose.start_watcher()
    except Exception:
        pass

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("当前 Python 没有 tkinter，打不开面板。")
        print("可以先用 python my_devices.py --once 在命令行看数值。")
        return 2
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
