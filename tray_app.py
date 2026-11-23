# -*- coding: utf-8 -*-
"""tray_app.py —— 托盘电量监视器（纯 ctypes，无第三方依赖）

本机两台设备
------------
* 雷柏 VT7S Pro V2（2.4G，VID 24AE）：接收器每 3 秒被动播报一次，
  稳定可读，电量 / DPI / 充电状态都有。
* 迈从 G87 V2（2.4G，VID 3837）：电量靠设备**主动播报**
  `FA FB 07 <百分比>`（报文编号 0，64 字节），**只读、绝不发任何命令**。
  播报是「变化时推」，不是定时心跳，所以读不到就如实显示「等待播报」，
  绝不瞎猜数字。协议细节见 mchose.py 文件头。

界面约定
--------
* 托盘默认是**一根电池图标**：壳里上下两条，上条=键盘、下条=鼠标，
  各自按电量从左往右填充，颜色随电量变化：绿 ≥60 / 黄 30-59 / 红 <30。
  没读到的显示成空槽。鼠标悬停看两台的具体数字。
  （设置里可切回「两个图标 + 数字」的老样式。）
* **右键图标 → 打开面板**（面板里改设置：刷新间隔 / 开机自启 / 托盘样式）。
* 左键图标 → 立刻查一次（默认手动模式，平时不自动查）。
* 悬停显示 tooltip：键盘与鼠标的电量、连接方式、充电状态。

其它
----
* 开机自启用「启动」文件夹里的快捷方式（`电量托盘.lnk`，任务管理器可见）+ pythonw.exe。
* 低 CPU：默认**不挂定时器**，右键菜单里点「立即刷新」才读一次；想自动再改成 30 秒~30 分钟。
  读取在后台线程，绝不阻塞托盘消息循环。
* **不写日志文件**：只有 `--debug` 时才往控制台打印。
* 全 ctypes 调 shell32/user32，不依赖 pystray / Pillow。

启动：
    pythonw tray_app.py        # 静默驻留（开机自启用的就是这个）
    python  tray_app.py --debug   # 前台运行并打印过程信息（排查问题用）
    python  tray_app.py --status
    python  tray_app.py --quit
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes
from typing import Optional

import hid_win as hw
import mchose
import mchose_mouse
import rapoo
import win_icon
import tray_common as tc


BASE_DIR = tc.BASE_DIR
APP_NAME = tc.APP_NAME
MUTEX_NAME = tc.MUTEX_NAME
WND_CLASS = tc.WND_CLASS

USER32 = tc.USER32
KERNEL32 = tc.KERNEL32
SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)

# 右键菜单用到的几个函数，手工声明一遍参数类型，免得 ctypes 猜错把指针截断
USER32.CreatePopupMenu.restype = ctypes.c_void_p
USER32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                               ctypes.c_size_t, wintypes.LPCWSTR]
USER32.AppendMenuW.restype = wintypes.BOOL
USER32.TrackPopupMenuEx.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
USER32.TrackPopupMenuEx.restype = ctypes.c_int
USER32.DestroyMenu.argtypes = [ctypes.c_void_p]
USER32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
MSG_TRAY_CALLBACK = WM_APP + 1
MSG_REFRESH_DONE = WM_APP + 2
MSG_ICONS_DIRTY = WM_APP + 3   # 旁听线程有新值，只重画图标，不算一次刷新

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

SW_RESTORE = 9
SW_SHOW = 5

# 托盘右键菜单
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
MENU_OPEN, MENU_REFRESH, MENU_QUIT = 1001, 1002, 1003

# --------------------------------------------------------------------------
# Win32 结构体与原型
# --------------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", hw.GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", ctypes.c_void_p), ("message", wintypes.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


USER32.RegisterClassExW.restype = wintypes.WORD
USER32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
USER32.CreateWindowExW.restype = ctypes.c_void_p
USER32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
USER32.DefWindowProcW.restype = ctypes.c_ssize_t
USER32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
USER32.DestroyWindow.restype = wintypes.BOOL
USER32.DestroyWindow.argtypes = [ctypes.c_void_p]
USER32.SetTimer.restype = ctypes.c_size_t
USER32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
USER32.KillTimer.restype = wintypes.BOOL
USER32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
USER32.GetMessageW.restype = ctypes.c_int
USER32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                               wintypes.UINT, wintypes.UINT]
USER32.PostQuitMessage.restype = None
USER32.PostQuitMessage.argtypes = [ctypes.c_int]
USER32.TranslateMessage.restype = wintypes.BOOL
USER32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
USER32.DispatchMessageW.restype = ctypes.c_ssize_t
USER32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

SHELL32.Shell_NotifyIconW.restype = wintypes.BOOL
SHELL32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

KERNEL32.GetModuleHandleW.restype = ctypes.c_void_p
KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = ctypes.c_void_p
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]

def _taskbar_is_light() -> bool:
    """任务栏是不是浅色（白底）。判断逻辑放在 tray_common，面板也要用。"""
    return tc.system_is_light()


TRAY_ICONS = [
    # (uID, key, 提示名, 是否带底板)  —— 「两个图标」模式下用
    # 提示语只说「键盘 / 鼠标」，不写型号。左边一个叫「键盘」、右边一个却叫
    # 「雷柏 VT7 V2」，两个提示不对等，读起来别扭；型号留给面板里的卡片去交代。
    (1, "mouse", "鼠标", True),
    (2, "keyboard", "键盘", True),
]


def mouse_slot() -> str:
    """鼠标位当前放哪只：a7=迈从 A7 V3 Ultra+ / mouse=雷柏（VT7 V2 或 VT7S Pro V2）。"""
    return tc.load_config().get("mouse_slot", "a7")


def mouse_title() -> str:
    """鼠标位该叫什么名字。雷柏那格以实际插着的型号为准，别写死。"""
    slot = mouse_slot()
    if slot != "a7":
        try:
            pid = rapoo.detect()
            if pid:
                return rapoo.model_name(pid)
        except Exception:
            pass
    return tc.MOUSE_SLOTS.get(slot, "鼠标")

# 托盘样式（只留两种：一条电池样式的双电量条太挤，已经删掉了）
STYLE_DUAL_NUM = "dualnum"   # 一个图标：上下两行数字，上=键盘、下=鼠标（默认）
STYLE_SEPARATE = "separate"  # 两个图标，各一个大数字

# read_* 的第二个返回值：特殊错误码（普通错误是一句给人看的话）
ERR_WIRED = "\x00wired"      # 键盘插着线，这块固件不提供电量


# --------------------------------------------------------------------------
# 设备读取
# --------------------------------------------------------------------------
def read_mouse():
    """鼠标位：按配置决定读迈从 A7 还是雷柏。

    返回 (电量, 备注, 是否在充电)。
    """
    if mouse_slot() == "a7":
        try:
            info = mchose_mouse.query_info()
        except Exception as e:
            return None, f"读取异常：{e}", None
        if info:
            return info["battery"], "", bool(info["charging"])
        got = mchose_mouse.latest(max_age=1800)
        if got is not None:
            return got, "上次", mchose_mouse.is_charging()
        return None, "鼠标未响应（动一下它再试）", None

    t0 = time.time()
    try:
        res = rapoo.read_status(timeout=6.0)
    except Exception as e:
        return None, f"读取异常：{e}", None
    if res is None:
        return None, "没等到播报（确认鼠标开机、在 2.4G 模式、动一下它）", None
    if "error" in res:
        return None, res["error"], None
    return res["battery"], "", bool(res.get("charging"))


def read_keyboard():
    """迈从：先主动读一次（cmd 0x05 读配置块），失败再退回缓存/播报。

    主动查询走的是官方网页驱动每次连上设备都会发的那条读命令，
    应答第 40 字节就是电量，单次耗时几十毫秒，所以可以放心点就查。
    """
    lvl, chg = mchose.query_battery()
    if lvl is not None:
        return lvl, "", bool(chg)

    if mchose.connection() == "有线":
        # 插线时这块固件不报电量（0x65 回 unknw、0x05 只有静态配置块），
        # 就别再拿上次无线的数字冒充实时值了，图标上写 -- 更老实
        return None, ERR_WIRED, None

    got = mchose.latest(max_age=1800)
    if got is not None:
        return got, "上次", mchose.is_charging()
    return None, "键盘未响应（接收器可能休眠了，敲一下键再试）", None


# --------------------------------------------------------------------------
# 托盘程序
# --------------------------------------------------------------------------
class TrayApp:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.cfg = tc.load_config()
        self.hwnd: Optional[int] = None
        self.wndproc_ref = None
        self.busy = False
        self.levels = {k: (None, "正在读取…") for _, k, _, _ in TRAY_ICONS}
        self.charg = {k: None for _, k, _, _ in TRAY_ICONS}   # 是否在充电
        self.stamp = {k: "" for _, k, _, _ in TRAY_ICONS}   # 上次读到的时间
        self.pub_ts = {k: 0 for _, k, _, _ in TRAY_ICONS}   # 已采纳的「别处读数」时间戳
        self.style = self.cfg.get("tray_style", STYLE_DUAL_NUM)
        self.light_taskbar = _taskbar_is_light()
        self.icons_added = False
        # 键盘只在电量变化时播报，重启后可能很久都没有新帧。
        # 把上次收到的值写进配置，开机先显示它并标上时间，别一上来就是个 --。
        last = self.cfg.get("kb_last")
        if mchose.connection() == "有线":
            # 插着线：固件不给电量，开机别先摆一个拔线前的旧数字
            self.levels["keyboard"] = (None, "有线")
        elif isinstance(last, int) and 0 <= last <= 100:
            ts = self.cfg.get("kb_ts") or 0
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "上次"
            self.levels["keyboard"] = (last, when)
            self.stamp["keyboard"] = when
        # 鼠标位同理：先摆上次的读数，别一开就显示 -- 等后台线程回来
        got = tc.read_published(mouse_slot())
        if got:
            pct, chg, ts = got
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "上次"
            self.levels["mouse"] = (pct, when)
            self.stamp["mouse"] = when
            self.charg["mouse"] = chg
        self.taskbar_created_msg = USER32.RegisterWindowMessageW("TaskbarCreated")
        self.msg_quit = USER32.RegisterWindowMessageW(tc.MSG_QUIT)
        self.msg_reload = USER32.RegisterWindowMessageW(tc.MSG_RELOAD)
        self.log("启动，配置 = " + json.dumps(self.cfg, ensure_ascii=False))

    # ---- 输出（默认不落盘）---------------------------------------------
    def log(self, msg: str) -> None:
        """只在 --debug 时输出到控制台。

        默认**不写任何日志文件**（不占磁盘也不留痕）；需要排查问题时用
        `python tray_app.py --debug` 前台跑一次就能看到全部输出。
        """
        if self.debug:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

    # ---- 图标 ----------------------------------------------------------
    def _color(self, level: Optional[int]) -> tuple[int, int, int]:
        """电量颜色。任务栏是浅色（白底）时换一套深颜料。

        亮绿/亮黄在白底上几乎看不见，这不是审美问题而是对比度不够；
        深一档之后，白底和深底两种情况都读得清。
        """
        if level is None:
            return (90, 97, 107) if self.light_taskbar else (150, 155, 160)
        if self.light_taskbar:
            if level >= 60:
                return (12, 116, 60)
            if level >= 30:
                return (168, 91, 0)
            return (176, 26, 26)
        if level >= 60:
            return (93, 220, 138)
        if level >= 30:
            return (245, 196, 81)
        return (255, 107, 107)

    def _sync_theme(self) -> None:
        """决定图标走浅色还是深色。配置里的 theme 优先，auto 才问系统。

        每次画图标前都过一遍：用户在面板里改了主题、或者改了 Windows 主题，
        都不用重启程序。
        """
        mode = self.cfg.get("theme", "auto")
        light = _taskbar_is_light() if mode == "auto" else (mode == "light")
        if light != self.light_taskbar:
            self.light_taskbar = light
            self.log(f"图标配色 -> {'浅色' if light else '深色'}"
                     f"（theme={mode}）")

    def _nid(self, uID: int):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = uID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = MSG_TRAY_CALLBACK
        return nid

    def _apply_icon(self, action: int, uID: int, key: str, title: str,
                    plate: bool) -> bool:
        level, note = self.levels[key]
        text = str(level) if level is not None else "--"
        nid = self._nid(uID)
        nid.hIcon = win_icon.make_hicon(text, self._color(level), plate,
                                        dark=not self.light_taskbar)
        if level is None:
            nid.szTip = f"{title}：{note or '未知'}"
        elif note:                      # 旧值：把时间写清楚，别让人误以为是刚测的
            nid.szTip = f"{title}：{level}%（{note}）"
        else:
            nid.szTip = f"{title}：{level}%（{time.strftime('%H:%M:%S')} 更新）"
        return bool(SHELL32.Shell_NotifyIconW(action, ctypes.byref(nid)))

    # ---- 两行数字（默认样式）--------------------------------------------
    def _tip_part(self, key: str, name: str) -> str:
        level, note = self.levels[key]
        if level is None:
            if note == "有线":
                return f"{name} 有线（USB 供电）"
            return f"{name} 未读到"
        tail = ""
        if self.charg.get(key):
            tail = " 充电中"
        elif note:
            tail = f" {note}"
        return f"{name} {level}%{tail}"

    def _apply_dual_num(self, action: int) -> bool:
        """一个图标，上下两行数字：上排键盘、下排鼠标，颜色表示电量高低。

        透明底、不要底板 —— 图标一共才这么点地方，底板占掉一圈之后
        两行数字挤得又小又闷。数字颜色已经会跟着任务栏深浅换，
        透明底照样看得清。
        """
        kb_lvl = self.levels["keyboard"][0]
        ms_lvl = self.levels["mouse"][0]
        nid = self._nid(1)
        nid.hIcon = win_icon.make_hicon_two_num(
            (kb_lvl, self._color(kb_lvl)), (ms_lvl, self._color(ms_lvl)))
        nid.szTip = "｜".join((
            self._tip_part("keyboard", "键盘"),
            self._tip_part("mouse", "鼠标"),
        ))
        return bool(SHELL32.Shell_NotifyIconW(action, ctypes.byref(nid)))

    def add_icons(self) -> None:
        self._sync_theme()
        if self.style == STYLE_DUAL_NUM:
            ok = self._apply_dual_num(NIM_ADD)
            self.log(f"NIM_ADD 双行数字图标 -> {'ok' if ok else '失败'}")
        else:
            for uID, key, title, plate in TRAY_ICONS:
                ok = self._apply_icon(NIM_ADD, uID, key, title, plate)
                self.log(f"NIM_ADD {title} -> {'ok' if ok else '失败'}")
        self.icons_added = True

    def update_icons(self) -> None:
        self._sync_theme()
        if not self.icons_added:
            self.add_icons()
            return
        if self.style == STYLE_DUAL_NUM:
            self._apply_dual_num(NIM_MODIFY)
            return
        for uID, key, title, plate in TRAY_ICONS:
            self._apply_icon(NIM_MODIFY, uID, key, title, plate)

    def remove_icons(self) -> None:
        # 两种样式都清一遍，避免切换样式后旧图标留在任务栏里
        for uID in (1, 2):
            nid = self._nid(uID)
            nid.uFlags = 0
            SHELL32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.icons_added = False

    def rebuild_icons(self) -> None:
        """换了托盘样式（或任务栏重建）时，删掉旧的重新加。"""
        self.remove_icons()
        self.add_icons()

    def on_keyboard_frame(self, level: int) -> None:
        """旁听线程抓到一帧电量（在别的线程里调用，只能投消息，不能碰界面）。"""
        same = self.levels["keyboard"][0] == level and not self.levels["keyboard"][1]
        self.levels["keyboard"] = (level, "")
        if same:
            return          # 值没变，不用写配置也不用重画，省得每隔几十秒白忙一次
        self.log(f"旁听抓到迈从电量 {level}%")
        self.stamp["keyboard"] = time.strftime("%H:%M")
        # 记到配置里，下次启动直接显示，面板也能直接用上；
        # publish 会重新读一遍配置文件，不会把面板刚写进来的鼠标读数盖掉
        ts = tc.publish_reading("keyboard", level, bool(self.charg.get("keyboard")))
        self.pub_ts["keyboard"] = max(self.pub_ts.get("keyboard", 0), ts)
        if self.hwnd:
            USER32.PostMessageW(self.hwnd, MSG_ICONS_DIRTY, 0, 0)

    # ---- 定时器 / 刷新 --------------------------------------------------
    def apply_timer(self) -> None:
        """interval = 0 表示手动模式：一个定时器都不挂，只在右键菜单里点「立即刷新」时查。

        默认就是手动。电量变化慢，没必要隔一会儿就去敲设备——尤其是迈从
        被反复查询时反而更不肯开口。
        """
        if not self.hwnd:
            return
        USER32.KillTimer(self.hwnd, 1)
        secs = int(self.cfg.get("interval") or 0)
        if secs <= 0:
            self.log("手动模式：不设自动刷新，右键菜单可「立即刷新」")
            return
        USER32.SetTimer(self.hwnd, 1, secs * 1000, None)
        self.log(f"自动刷新：每 {secs} 秒")

    def apply_config(self) -> None:
        """同步界面里改过的设置。

        注意：开机自启这里**只读不改**——托盘绝不替用户动系统设置。
        （早先的版本会自动"纠正"，结果一次读到不一致就把用户的启动项删了。）
        """
        new = tc.load_config()
        changed = new["interval"] != self.cfg["interval"]
        style = new.get("tray_style", STYLE_DUAL_NUM)
        style_changed = style != self.style
        old_theme = self.cfg.get("theme", "auto")
        theme = new.get("theme", "auto")
        old_slot = self.cfg.get("mouse_slot", "a7")
        slot_changed = new.get("mouse_slot", "a7") != old_slot
        self.cfg = new
        self.style = style
        if slot_changed:
            # 右格换了另一只鼠标：旧值必须扔掉，否则图标上还挂着上一只的电量
            self.log(f"鼠标位换成 {mouse_title()}")
            self.levels["mouse"] = (None, "正在读取…")
            self.charg["mouse"] = None
            self.stamp["mouse"] = ""
            self.pub_ts["mouse"] = 0
            self.update_icons()
            self.refresh_now()
        if changed:
            self.log(f"刷新间隔 -> {new['interval']} 秒")
            self.apply_timer()
        if style_changed:
            self.log(f"托盘样式 -> {style}")
            self.rebuild_icons()
        elif theme != old_theme:
            # 只换了深浅，不用删图标重加，原地重画一张图就够了
            self.log(f"图标配色 -> {theme}")
            self.update_icons()

        actual = tc.is_autostart_enabled()
        if bool(self.cfg.get("autostart")) != actual:
            # 只把真实状态写回配置，让界面上的勾选和任务管理器保持一致
            self.cfg["autostart"] = actual
            tc.save_config(self.cfg)
            self.log(f"开机自启实际为 {actual}，已同步到配置（未改动系统设置）")

        self._adopt_published()

    def _adopt_published(self) -> None:
        """采纳面板那边读到的电量。

        面板是独立进程，它读到了会把结果写进配置再通知过来；这里直接取用，
        不再自己去敲设备——两边同时开 HID 句柄反而容易互相打断。
        """
        touched = False
        # 鼠标位在配置里可能是 a7 也可能是 mouse，托盘内部一律叫 "mouse"
        wired = mchose.connection() == "有线"
        pairs = [("keyboard", "keyboard"), ("mouse", mouse_slot())]
        for key, src in pairs:
            if src == "keyboard" and wired:
                # 键盘插着线时不报电量，配置里那条是拔线前的旧读数，
                # 别拿它把「有线」这个状态覆盖成 5%
                continue
            got = tc.read_published(src)
            if not got:
                continue
            pct, chg, ts = got
            if ts <= self.pub_ts.get(key, 0):
                continue
            self.pub_ts[key] = ts
            self.levels[key] = (pct, "")
            self.charg[key] = chg
            self.stamp[key] = time.strftime("%H:%M", time.localtime(ts))
            touched = True
            self.log(f"同步到电量：{src}={pct}%{'（充电中）' if chg else ''}")
        if touched:
            self.update_icons()

    def refresh_now(self) -> None:
        if self.busy or not self.hwnd:
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(self.hwnd,), daemon=True).start()

    def _merge(self, key: str, new) -> None:
        """读到新值就更新；没读到但以前读到过，就保留旧值并标上时间。

        new = (电量, 备注, 是否充电)。两台设备都会休眠，读不到就清零的话
        图标会来回闪，反而看不清，所以宁可显示「上次 20:09」。
        """
        pct, err, chg = new
        if pct is not None:
            self.stamp[key] = time.strftime("%H:%M")
            self.levels[key] = (pct, "")
            self.charg[key] = chg
            return
        if err == ERR_WIRED:
            # 插着线：不是「读失败」，而是这块固件根本没有有线电量
            self.levels[key] = (None, "有线")
            self.charg[key] = None
            return
        old_pct, _ = self.levels[key]
        if old_pct is not None:
            self.levels[key] = (old_pct, "上次 %s" % self.stamp[key])
            self.charg[key] = None
        else:
            self.levels[key] = (None, err)
            self.charg[key] = None

    def _worker(self, hwnd: int) -> None:
        got = {}
        try:
            got["mouse"] = read_mouse()
            self._merge("mouse", got["mouse"])
            got["keyboard"] = read_keyboard()
            self._merge("keyboard", got["keyboard"])
        except Exception:
            self.log("读取异常:\n" + traceback.format_exc())
        # 读到的值顺手写进配置，面板开着的话能直接取用，不用自己再抢一次设备
        # 鼠标位的实际键名跟着配置走（a7 / mouse），面板才能对上号
        mkey = mouse_slot()
        for key, val in got.items():
            if val[0] is not None:
                pub_key = mkey if key == "mouse" else key
                ts = tc.publish_reading(pub_key, val[0], bool(val[2]))
                self.pub_ts[key] = max(self.pub_ts.get(key, 0), ts)
        # 顺手记一行电量日志。只认**真读到的**：val[1] 有内容就说明是缓存里的
        # 「上次」值，把它当新样本记下来，耗电曲线就会被拉成一条假直线。
        items = []
        kb = got.get("keyboard")
        if kb and kb[0] is not None and not kb[1]:
            items.append(("键盘", "迈从 G87 V2", kb[0], bool(kb[2])))
        ms = got.get("mouse")
        if ms and ms[0] is not None and not ms[1]:
            items.append(("鼠标", mouse_title(), ms[0], bool(ms[2])))
        if items:
            try:
                tc.log_sample(items)
            except Exception:
                self.log("电量日志写入失败")
        USER32.PostMessageW(hwnd, MSG_REFRESH_DONE, 0, 0)

    def on_refresh_done(self) -> None:
        self.busy = False
        m, k = self.levels["mouse"], self.levels["keyboard"]
        self.log(f"刷新完成：鼠标={m[0]}（{m[1] or 'ok'}） 键盘={k[0]}（{k[1] or 'ok'}）")
        self.update_icons()

    # ---- 打开简洁界面 ---------------------------------------------------
    def open_ui(self) -> None:
        if tc.open_ui_window():
            self.log("简洁界面已在前台 / 已启动")
        else:
            self.log("打开界面失败")

    def show_menu(self) -> None:
        """右键：在鼠标位置弹一个小菜单。"""
        pt = wintypes.POINT()
        USER32.GetCursorPos(ctypes.byref(pt))

        hmenu = USER32.CreatePopupMenu()
        if not hmenu:
            self.log("建菜单失败，直接开界面")
            self.open_ui()
            return
        try:
            USER32.AppendMenuW(hmenu, MF_STRING, MENU_OPEN, "打开面板…")
            USER32.AppendMenuW(hmenu, MF_STRING, MENU_REFRESH, "立即刷新")
            USER32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            USER32.AppendMenuW(hmenu, MF_STRING, MENU_QUIT, "退出")
            # Windows 的老规矩：弹菜单前得把本窗口设成前台，
            # 否则点菜单以外的地方它不会消失，会一直挂在那儿。
            USER32.SetForegroundWindow(self.hwnd)
            cmd = USER32.TrackPopupMenuEx(
                hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pt.x, pt.y, self.hwnd, None)
            USER32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            USER32.DestroyMenu(hmenu)

        if cmd == MENU_OPEN:
            self.open_ui()
        elif cmd == MENU_REFRESH:
            self.refresh_now()
        elif cmd == MENU_QUIT:
            self.quit()

    def quit(self) -> None:
        self.log("退出")
        self.remove_icons()
        try:
            win_icon.destroy_all()
            win_icon.cleanup_files()
        except Exception:
            self.log("清理图标时出错（忽略）：\n" + traceback.format_exc())
        if self.hwnd:
            # DestroyWindow 会触发 WM_DESTROY -> PostQuitMessage，消息循环随之结束
            USER32.DestroyWindow(self.hwnd)

    # ---- 窗口过程 -------------------------------------------------------
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == MSG_TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.open_ui()                     # 左键：打开面板
            elif event == WM_RBUTTONUP:
                self.show_menu()                   # 右键：小菜单
            return 0
        if msg == MSG_REFRESH_DONE:
            self.on_refresh_done()
            return 0
        if msg == MSG_ICONS_DIRTY:
            self.update_icons()
            return 0
        if msg == WM_TIMER:
            self.apply_config()                    # 顺手同步一下界面里改的设置
            self.refresh_now()
            return 0
        if msg == self.msg_quit:
            self.quit()
            return 0
        if msg == self.msg_reload:
            self.apply_config()
            return 0
        if msg == self.taskbar_created_msg:
            self.log("任务栏重建，重新添加图标")
            self.add_icons()
            return 0
        if msg == WM_DESTROY:
            # 重要：销毁窗口后必须让消息循环退出，否则进程会残留成"僵尸"
            # ——它已经没有窗口/图标了，却还占着单实例锁，导致托盘再也起不来。
            USER32.PostQuitMessage(0)
            return 0
        return USER32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 启动 ----------------------------------------------------------
    def run(self) -> int:
        hinst = KERNEL32.GetModuleHandleW(None)
        self.wndproc_ref = WNDPROC(self.wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = WND_CLASS
        # 给这个隐藏窗口也挂上程序图标：不然任务管理器 / Alt-Tab 里
        # 万一露出它来，就是一张跟程序毫无关系的默认白纸
        icon_path = win_icon.app_icon_path()
        if icon_path:
            h = USER32.LoadImageW(None, icon_path, win_icon.IMAGE_ICON, 0, 0,
                                  win_icon.LR_LOADFROMFILE)
            if h:
                wc.hIcon = int(h)
                wc.hIconSm = int(h)
        if not USER32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):
                self.log(f"RegisterClassExW 失败 err={err}")
                return 1

        hwnd = USER32.CreateWindowExW(0, WND_CLASS, APP_NAME, 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            self.log(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
            return 1
        self.hwnd = hwnd

        self.add_icons()
        # 迈从常驻旁听：只读不写，抓到状态帧就更新图标，不需要用户配合时机
        mchose.start_watcher(self.on_keyboard_frame)
        self.apply_config()
        self.apply_timer()
        self.refresh_now()

        msg = MSG()
        while USER32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            USER32.TranslateMessage(ctypes.byref(msg))
            USER32.DispatchMessageW(ctypes.byref(msg))
        self.log("消息循环结束")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="托盘电量监视器")
    parser.add_argument("--debug", action="store_true", help="前台运行并输出日志")
    parser.add_argument("--status", action="store_true", help="打印当前状态")
    parser.add_argument("--quit", action="store_true", help="让正在运行的实例退出")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="开启 / 关闭开机自启（改的是启动文件夹里的快捷方式）")
    parser.add_argument("--install", action="store_true",
                        help="在桌面放一个指向本项目的快捷方式（不复制文件）")
    args = parser.parse_args()

    if args.autostart:
        want = args.autostart == "on"
        ok = tc.set_autostart(want)
        print(("已开启" if want else "已关闭") + "开机自启" if ok
              else "改动失败，可能被权限或系统策略挡住了")
        return 0 if ok else 1
    if args.install:
        res = tc.install_desktop_links()
        print("桌面快捷方式：", json.dumps(res, ensure_ascii=False))
        return 0

    if args.status:
        print("配置：", json.dumps(tc.load_config(), ensure_ascii=False))
        print("开机自启：", tc.is_autostart_enabled())
        print("启动命令：", tc.autostart_command())
        print("托盘是否在运行：", tc.is_tray_running())
        return 0
    if args.quit:
        ok = tc.post_to_tray(tc.MSG_QUIT)
        print("已通知托盘退出" if ok else "托盘没在运行")
        return 0

    # 单实例
    KERNEL32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:
        if tc.is_tray_running():
            # 真有一个正常实例在跑：让它重读配置
            tc.post_to_tray(tc.MSG_RELOAD)
            print("已经在运行了（任务栏那根电池图标就是它；点一下就能打开面板）")
            return 0
        # 有人占着锁但已经没有窗口 —— 上一版留下的僵尸进程，清掉后继续启动
        tc.cleanup_zombie()

    tc.write_pid()

    try:
        USER32.SetProcessDPIAware()            # 图标不糊
    except Exception:
        pass

    app = TrayApp(debug=args.debug)
    try:
        return app.run()
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        tc.remove_pid()


if __name__ == "__main__":
    sys.exit(main())
