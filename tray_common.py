# -*- coding: utf-8 -*-
"""tray_common.py —— 托盘程序与界面共用的配置 / 开机自启 / 进程间通信

托盘程序（tray_app.py）和简洁界面（my_devices.py）是两个进程，
共用这里的配置文件与窗口消息，界面里改设置后能立刻通知托盘生效。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from typing import Optional

import win_link
import win_shell

# 打包成 exe 之后，__file__ 会指向 PyInstaller 的临时解包目录（每次启动都换一个），
# 配置和 pid 放那儿就丢了，所以得分清两个目录：
#   程序目录 BASE_DIR —— exe 旁边，存配置 / pid，人也能找到
#   资源目录 RES_DIR  —— assets 这类只读素材，打包后藏在解包目录里
FROZEN = bool(getattr(sys, "frozen", False))


def _app_dir() -> str:
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _res_dir() -> str:
    if FROZEN:
        return getattr(sys, "_MEIPASS", _app_dir())
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_dir()
RES_DIR = _res_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "tray_config.json")
PID_FILE = os.path.join(BASE_DIR, "tray_pid.txt")

APP_NAME = "DeviceBatteryTray"
WND_CLASS = "DeviceBatteryTrayWnd"
MUTEX_NAME = "Global\\DeviceBatteryTray_single_instance"

# 界面标题（托盘用它来找到已打开的窗口）
UI_TITLE = "我的设备电量"

DEFAULT_CONFIG = {
    "interval": 0,         # 0 = 手动：只有你点图标 / 打开面板时才查一次
    "autostart": True,     # 开机自启
    "kb_last": None,       # 键盘上次收到的电量（设备只在电量变化时播报，
    "kb_ts": 0,            # 记下来免得重启后显示空白）
    "kb_chg": False,       # 键盘上次是不是在充电
    "ms_last": None,       # 鼠标上次读到的电量（同理，免得托盘和面板各说各话）
    "ms_ts": 0,
    "ms_chg": False,
    "a7_last": None,       # 迈从 A7 V3 Ultra+ 上次读到的电量
    "a7_ts": 0,
    "a7_chg": False,
    "tray_style": "dualnum",  # 托盘图标样式：dualnum=上下两行数字 / separate=两个图标
    "theme": "auto",          # 配色：auto=跟着系统走 / light=浅色 / dark=深色
    "mouse_slot": "a7",       # 鼠标位放哪只：a7=迈从 A7 V3 Ultra+ / mouse=雷柏 VT7 V2
}

# 鼠标位能放的设备：配置值 -> (显示名, 读取模块里的 key)
# 雷柏那格两款都认（VT7 V2 / VT7S Pro V2），显示名以实际插着的型号为准
MOUSE_SLOTS = {
    "a7": "迈从 A7 V3 Ultra+",
    "mouse": "雷柏 VT7 V2",
}

# 0 排在最前面：默认就是手动，想自动再加
# 最短 1 分钟：外设电量变化本来就慢，比这更频繁纯属白折腾
INTERVAL_CHOICES = [(0, "手动"), (60, "1 分钟"), (120, "2 分钟"), (300, "5 分钟"),
                    (600, "10 分钟"), (900, "15 分钟"), (1800, "30 分钟"),
                    (3600, "1 小时")]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

SW_RESTORE = 9

USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

# EnumWindows 的回调类型。Tk 会建一个和面板同名的隐藏窗口，找面板时得靠枚举筛掉它。
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

USER32.EnumWindows.restype = wintypes.BOOL
USER32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

USER32.FindWindowW.restype = ctypes.c_void_p
USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
USER32.PostMessageW.restype = wintypes.BOOL
USER32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                ctypes.c_size_t, ctypes.c_ssize_t]
USER32.RegisterWindowMessageW.restype = wintypes.UINT
USER32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [ctypes.c_void_p]


# --------------------------------------------------------------------------
# 系统主题
# --------------------------------------------------------------------------
def system_is_light() -> bool:
    """Windows 现在是不是浅色主题（任务栏也跟着这个走）。

    HKCU 里的 SystemUsesLightTheme：1 = 浅色，0 = 深色。
    读不到就当深色（Windows 默认就是深色任务栏）。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion"
                r"\Themes\Personalize") as key:
            val, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(int(val))
    except Exception:
        return False


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    # 老配置里可能还留着已经删掉的样式（比如双电量条），统一退回默认
    if cfg.get("tray_style") not in ("dualnum", "separate"):
        cfg["tray_style"] = DEFAULT_CONFIG["tray_style"]
    if cfg.get("theme") not in ("auto", "light", "dark"):
        cfg["theme"] = DEFAULT_CONFIG["theme"]
    if cfg.get("mouse_slot") not in MOUSE_SLOTS:
        cfg["mouse_slot"] = DEFAULT_CONFIG["mouse_slot"]
    # 老配置里可能有 15 / 30 秒这种档位，现在最短 1 分钟
    try:
        secs = int(cfg.get("interval") or 0)
    except (TypeError, ValueError):
        secs = 0
    if 0 < secs < 60:
        secs = 60
    cfg["interval"] = secs
    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# 配置里存「上次读到的电量」的字段：键名、电量、时间戳、充电位
READING_FIELDS = {
    "keyboard": ("kb_last", "kb_ts", "kb_chg"),
    "mouse": ("ms_last", "ms_ts", "ms_chg"),
    "a7": ("a7_last", "a7_ts", "a7_chg"),
}


def publish_reading(key: str, pct: int, charging: bool = False) -> int:
    """把刚读到的电量写进配置，让另一个进程直接取用。

    托盘和面板是两个进程，都去抢同一台设备的 HID 句柄容易互相打断，
    所以约定「谁读到了就写下来」：另外一边只取结果，不重复读一遍。
    返回写入的时间戳，调用方可以记下来，避免又把自己写的当成新消息。
    """
    import time as _t
    fields = READING_FIELDS.get(key)
    if not fields:
        return 0
    now = int(_t.time())
    cfg = load_config()
    cfg[fields[0]] = int(pct)
    cfg[fields[1]] = now
    cfg[fields[2]] = bool(charging)
    save_config(cfg)
    post_to_tray(MSG_RELOAD)
    return now


def read_published(key: str):
    """取另一个进程写下的电量，返回 (电量, 是否充电, 时间戳) 或 None。"""
    fields = READING_FIELDS.get(key)
    if not fields:
        return None
    cfg = load_config()
    pct = cfg.get(fields[0])
    if not isinstance(pct, int) or not (0 <= pct <= 100):
        return None
    return pct, bool(cfg.get(fields[2])), int(cfg.get(fields[1]) or 0)


# --------------------------------------------------------------------------
# 电量日志
# --------------------------------------------------------------------------
# 一次刷新记一条，一条里两台设备各占一行 —— 文件里是长格式（一行一台设备），
# 因为要拿 Excel 看单台设备的耗电趋势时，长格式才好按「设备」筛。
# 但**界面上一律按「一次刷新 = 一条记录」显示**：同一个时刻的两台设备是同一件事，
# 分成两行、各写一次时间，看着像两个不同时刻发生的。
#
# 「间隔」「变化」在写入时就算好，打开即人话，不用再拉透视表。
# 文件放 exe 旁边（BASE_DIR），跟 tray_config.json 作伴，拷走也能带着走。
LOG_NAME = "电量日志.csv"
LOG_HEADER = "时间,位置,设备,电量,充电,间隔,变化"
LOG_MAX_BYTES = 3 * 1024 * 1024     # 长到这个数就换一个文件，别让它大到打不开
LOG_DEDUPE_SECS = 8                 # 托盘和面板可能同时刷新，这么近的算同一轮
LOG_TAIL_BYTES = 48 * 1024
LOG_TAIL_ROWS = 600
# 老文件没有「位置」列时的兜底：这个产品名就是键盘，其它一律当鼠标
KB_NAMES = ("迈从 G87 V2",)


def log_path() -> str:
    return os.path.join(BASE_DIR, LOG_NAME)


def _fmt_gap(secs: float) -> str:
    """把「两条记录隔了多久」说成人话。"""
    secs = int(secs)
    if secs < 60:
        return "%d 秒" % secs
    mins = secs // 60
    if mins < 60:
        return "%d 分钟" % mins
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return "%d 小时 %d 分" % (hours, mins) if mins else "%d 小时" % hours
    days, hours = divmod(hours, 24)
    return "%d 天 %d 小时" % (days, hours) if hours else "%d 天" % days


def _parse_log_row(line: str):
    """解析一行。兼容两种宽度：
    7 列（现在）时间,位置,设备,电量,充电,间隔,变化
    6 列（旧文件）时间,设备,电量,充电,间隔,变化
    """
    parts = line.split(",")
    if len(parts) >= 7:
        when, slot, name = parts[0], parts[1], parts[2]
        rest = parts[3:]
    elif len(parts) >= 6:
        when, slot, name = parts[0], "", parts[1]
        rest = parts[2:]
    else:
        return None
    try:
        ts = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
        pct = int(rest[0])
    except (ValueError, OverflowError, IndexError):
        return None
    charging = rest[1] == "是" if len(rest) > 1 else False
    if not slot:                     # 旧文件没写位置，按名字猜
        slot = "键盘" if name in KB_NAMES else "鼠标"
    return {"when": when, "ts": ts, "slot": slot, "name": name, "pct": pct,
            "charging": charging,
            "gap": rest[2] if len(rest) > 2 else "",
            "delta": rest[3] if len(rest) > 3 else ""}


def _read_log_tail(limit_rows: int = LOG_TAIL_ROWS):
    """只读文件尾巴。日志会越来越长，没必要每次都从头解析。"""
    path = log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
                f.readline()            # 第一行可能被切了一半，丢掉
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8-sig", "ignore").splitlines()
    return lines[-limit_rows:]


def _rotate_log(path: str) -> None:
    try:
        if os.path.getsize(path) <= LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(path, os.path.join(BASE_DIR, "电量日志-%s.csv" % stamp))
    except OSError:
        pass


def log_sample(items) -> int:
    """把一轮刷新记进日志，返回实际写了几行。

    items = [(位置, 设备名, 电量, 是否充电), ...]，位置是「键盘」或「鼠标」。
    读不到、或者只是从缓存里翻出来的旧值**不要传进来** —— 把旧值当新样本
    记下来会把曲线拉平成一条直线，那比没有日志更容易误导人。
    """
    rows = []
    for it in items or ():
        if not it or len(it) < 3 or it[2] is None:
            continue
        slot = str(it[0] or "设备")
        name = str(it[1] or "设备").replace(",", "、").replace("\r", " ")
        name = name.replace("\n", " ")
        try:
            pct = max(0, min(100, int(it[2])))
        except (TypeError, ValueError):
            continue
        rows.append((slot, name, pct, bool(it[3]) if len(it) > 3 else False))
    if not rows:
        return 0

    now = time.time()
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    # 同一个设备的上一条（用来算间隔和变化）。设备换过型号也算两台，
    # 拿「位置」区分不了，所以键里带上名字。
    prev = {}
    for line in reversed(_read_log_tail()):
        r = _parse_log_row(line)
        if r and (r["slot"], r["name"]) not in prev:
            prev[(r["slot"], r["name"])] = r

    out = []
    for slot, name, pct, chg in rows:
        p = prev.get((slot, name))
        if (p and now - p["ts"] < LOG_DEDUPE_SECS
                and p["pct"] == pct and p["charging"] == chg):
            continue            # 另一进程刚记过同一轮，别再记一遍
        gap = _fmt_gap(now - p["ts"]) if p else ""
        delta = "%+d" % (pct - p["pct"]) if p else ""
        out.append("%s,%s,%s,%d,%s,%s,%s"
                   % (when, slot, name, pct, "是" if chg else "否", gap, delta))
    if not out:
        return 0

    path = log_path()
    _rotate_log(path)
    fresh = not os.path.exists(path)
    try:
        # 新建时带 BOM（utf-8-sig），Excel / WPS 双击打开才不会把中文读成乱码；
        # 追加时绝不能再写一次 BOM，否则文件中间会多出几个怪字符。
        with open(path, "a", encoding="utf-8-sig" if fresh else "utf-8",
                  newline="") as f:
            if fresh:
                f.write(LOG_HEADER + "\r\n")
            for line in out:
                f.write(line + "\r\n")
    except OSError:
        return 0            # 文件被别人占着（比如正开着 Excel），这轮就算了
    return len(out)


def read_log(limit: int = 400):
    """给界面用：把长格式按「时刻」合并成一条条记录，**新的在前**。

    返回 [{when, ts, gap, rows: [{slot, name, pct, charging, delta}, ...]}, ...]
    gap 是「距下一条（更早的）记录多久」，也就是这一次刷新和上一次刷新之间隔了多少。
    """
    flat = []
    for line in _read_log_tail(limit_rows=max(limit * 4, 400)):
        r = _parse_log_row(line)
        if r:
            flat.append(r)

    entries = []
    for r in flat:                       # 文件是追加写的，同一时刻的几行必然相邻
        if entries and entries[-1]["when"] == r["when"]:
            entries[-1]["rows"].append(r)
        else:
            entries.append({"when": r["when"], "ts": r["ts"], "rows": [r]})
    for e in entries:
        # 间隔取键盘那条自己的值。键盘是主动查询、每轮都在，它的间隔就是
        # 「这次刷新距上次刷新多久」；只有键盘缺席时才退回鼠标的。
        kb = next((r for r in e["rows"] if r["slot"] == "键盘"), None)
        e["gap"] = (kb or e["rows"][0]).get("gap", "")
    entries.reverse()
    return entries[:limit]


def delete_log_entry(when: str) -> bool:
    """删掉某个时刻的整条记录（两台设备一起删）。

    删完要把后面各条的「间隔 / 变化」重算一遍 —— 删掉中间一条之后，
    下一条的间隔应该跟**再上一条**比，不重算的话那几个数字就成了假的。
    文件被别的程序占着（比如正开着 Excel）时返回 False，界面会给提示。
    """
    path = log_path()
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig", "ignore")
    except OSError:
        return False

    lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
    keep = []
    hit = 0
    for line in lines:
        parts = line.split(",")
        if parts and parts[0] == when:
            hit += 1
            continue
        keep.append(line)
    if not hit:
        return False

    rows = [r for r in (_parse_log_row(ln) for ln in keep) if r]
    prev = {}
    out = []
    for r in rows:                        # 文件按时间先后追加，顺序直接能用
        key = (r["slot"], r["name"])
        p = prev.get(key)
        gap = _fmt_gap(r["ts"] - p["ts"]) if p else ""
        delta = "%+d" % (r["pct"] - p["pct"]) if p else ""
        prev[key] = r
        out.append("%s,%s,%s,%d,%s,%s,%s"
                   % (r["when"], r["slot"], r["name"], r["pct"],
                      "是" if r["charging"] else "否", gap, delta))

    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(LOG_HEADER + "\r\n")
            for line in out:
                f.write(line + "\r\n")
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 开机自启：启动文件夹里的快捷方式（.lnk），不再用注册表
# --------------------------------------------------------------------------
def pythonw_path() -> str:
    if FROZEN:
        return sys.executable          # 打包后自己就是那个 exe
    exe = sys.executable or ""
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def pythonw_with_tk() -> str:
    """找一个**带 tkinter** 的 pythonw，用来打开图形面板。

    托盘本身不需要 tkinter，但面板要。托管的那份 Python 偏偏没有 tkinter，
    所以这里按顺序探一遍，谁有就用谁；都没有就退回当前解释器（面板会
    自己提示缺 tkinter，不会崩）。

    打包成 exe 之后 tkinter 已经一起塞进去了，直接返回 exe 本身。
    """
    if FROZEN:
        return sys.executable
    here = pythonw_path()
    cands = [here]

    # pythonw.exe 旁边就是 python.exe，用它跑一次 import 探针最快最准
    def has_tk(exe: str) -> bool:
        py = exe.replace("pythonw.exe", "python.exe")
        if not os.path.exists(py):
            return False
        try:
            import subprocess
            r = subprocess.run([py, "-c", "import tkinter"],
                               capture_output=True, timeout=12)
            return r.returncode == 0
        except Exception:
            return False

    if has_tk(here):
        return here

    # 当前解释器没带 tkinter 时，再去常见安装位置碰运气。
    # 不写死任何一台机器的路径，只用标准安装位置 + 环境变量。
    for d in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
              os.path.expandvars(r"%PROGRAMFILES%\Python"),
              r"C:\Python313", r"C:\Python312", r"C:\Python311"):
        if not os.path.isdir(d):
            continue
        if d.endswith("Python"):
            try:
                subs = sorted(os.listdir(d), reverse=True)
            except OSError:
                continue
            cands.extend(os.path.join(d, s, "pythonw.exe") for s in subs)
        else:
            cands.append(os.path.join(d, "pythonw.exe"))

    for exe in cands:
        if exe != here and os.path.exists(exe) and has_tk(exe):
            return exe
    return here


def tray_script() -> str:
    return os.path.join(BASE_DIR, "tray_app.py")


def tray_command() -> list:
    """起托盘的完整命令行（源码跑 = pythonw + 脚本，exe = 自己 + --tray）。"""
    if FROZEN:
        return [sys.executable, "--tray"]
    return [pythonw_path(), tray_script()]


def ui_command() -> list:
    """起面板的完整命令行。

    排障用：环境里设了 PB_PANEL_PROBE=1，就叫面板跑一自身检（写 probe.txt）
    而不是正常显示，专门用来查「被托盘拉起的面板」和「手动开的面板」的差别。
    """
    if FROZEN:
        cmd = [sys.executable, "--panel"]
        if os.environ.get("PB_PANEL_PROBE") == "1":
            cmd.append("--probe")
        return cmd
    return [pythonw_with_tk(), os.path.join(BASE_DIR, "my_devices.py")]


def child_env():
    """拉起子进程时用的环境变量（源码模式返回 None = 照常继承）。

    坑在这里：打包成 onefile 后，PyInstaller 会让「自己拉起的自己」**复用父进程
    的临时解包目录**（靠 `_PYI_PARENT_PROCESS_LEVEL` 这类环境变量）。可是父进程
    （双击启动的那个启动器）拉起托盘/面板后马上就退出，退出时会把整个解包目录
    删掉 —— 子进程随后去读 assets 就扑空了。

    症状很有欺骗性：窗口、数值都正常，只有产品图变成「放一张产品图」的占位；
    偶发还会 `cannot import name '_imagingtk'` 直接崩，因为那是个懒加载的扩展，
    文件恰好被删掉时就 import 不到。

    所以这里把 `_PYI_*` 全部摘掉，让子进程自己解一份，父进程删什么都不影响它。
    """
    if not FROZEN:
        return None
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("_PYI_") or "_MEIPASS" in k.upper())}


def open_ui_window() -> bool:
    """把面板叫到前台；从来没开过就新起一个。

    返回是否成功把它弄出来了（不代表窗口已画完）。
    """
    hwnd = ui_hwnd()
    if hwnd:
        try:
            USER32.ShowWindow(hwnd, SW_RESTORE)
            USER32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return True
    import subprocess
    try:
        subprocess.Popen(ui_command(), cwd=BASE_DIR, creationflags=0x00000008,
                         env=child_env())
        return True
    except Exception:
        return False


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --tray'
    return f'"{pythonw_path()}" "{tray_script()}"'


STARTUP_LNK_NAME = "电量托盘.lnk"

# 任务管理器/系统用这个键记录启动项的"启用/禁用"状态
#   值名 = 启动文件夹里的文件名；数据 12 字节，首字节 02=启用、03=禁用
STARTUP_APPROVED_FOLDER = (r"Software\Microsoft\Windows\CurrentVersion"
                           r"\Explorer\StartupApproved\StartupFolder")


def startup_dir() -> str:
    return win_shell.startup_dir()


def startup_lnk() -> str:
    """启动文件夹里的快捷方式路径。"""
    return os.path.join(win_shell.startup_dir(), STARTUP_LNK_NAME)


# 以前版本写在注册表里的项，启动时顺手清掉（迁移用）
LEGACY_RUN_VALUE = APP_NAME


def legacy_autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, LEGACY_RUN_VALUE)
            return bool(val)
    except OSError:
        return False


def _clear_legacy_autostart() -> bool:
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, LEGACY_RUN_VALUE)
            return True
    except OSError:
        return False


def _startup_approved() -> Optional[bool]:
    """任务管理器里的启用状态：True 启用 / False 禁用 / None 没记录（默认启用）"""
    import winreg
    base = os.path.basename(startup_lnk())
    names = (base, os.path.splitext(base)[0])       # 两种命名都兼容
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                            0, winreg.KEY_READ) as k:
            for name in names:
                try:
                    data, _ = winreg.QueryValueEx(k, name)
                except OSError:
                    continue
                if isinstance(data, (bytes, bytearray)) and data:
                    return data[0] != 0x03          # 0x03 = 已禁用
    except OSError:
        pass
    return None


def _set_startup_approved(enabled: bool) -> None:
    """写启用/禁用状态——写完之后任务管理器里看到的开关状态是一致的。"""
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                                0, winreg.KEY_SET_VALUE) as k:
            data = bytes([0x02 if enabled else 0x03]) + bytes(11)
            winreg.SetValueEx(k, os.path.basename(startup_lnk()), 0,
                              winreg.REG_BINARY, data)
    except OSError:
        pass


def _clear_startup_approved() -> None:
    import winreg
    base = os.path.basename(startup_lnk())
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                                0, winreg.KEY_SET_VALUE) as k:
            for name in (base, os.path.splitext(base)[0]):
                try:
                    winreg.DeleteValue(k, name)
                except OSError:
                    pass
    except OSError:
        pass


def is_autostart_enabled() -> bool:
    """开机自启是否生效：快捷方式在、且没被任务管理器禁用。"""
    if win_shell.shortcut_exists(startup_lnk()):
        return _startup_approved() is not False
    return legacy_autostart_enabled()       # 兼容旧版注册表项


def set_autostart(enabled: bool) -> bool:
    """开关开机自启。

    实现 = 「启动」文件夹里的快捷方式，这样任务管理器「启动」标签里
    能直接看到它（名字就是 电量托盘），并且可以在那里直接启用/禁用。
    """
    if enabled:
        ok = win_shell.create_shortcut(
            startup_lnk(), pythonw_path(),
            arguments=" ".join(tray_command()[1:]),
            workdir=BASE_DIR,
            description="设备电量托盘：常驻显示雷柏鼠标 / 迈从键盘电量")
        _set_startup_approved(True)
    else:
        _clear_startup_approved()
        ok = win_shell.remove_shortcut(startup_lnk()) or \
            not win_shell.shortcut_exists(startup_lnk())
    _clear_legacy_autostart()               # 迁移：清掉旧的注册表项
    return ok


def autostart_status() -> dict:
    """给界面/命令行看的自启状态摘要。"""
    lnk = startup_lnk()
    approved = _startup_approved()
    return {
        "enabled": is_autostart_enabled(),
        "method": "启动文件夹快捷方式（任务管理器可见）",
        "shortcut": lnk,
        "shortcut_exists": win_shell.shortcut_exists(lnk),
        "taskmanager_state": ("已启用" if approved is not False else "已被任务管理器禁用")
                              if approved is not None else "未记录（默认启用）",
        "target": autostart_command(),
        "legacy_registry": legacy_autostart_enabled(),
    }


# --------------------------------------------------------------------------
# 桌面链接（软连接：目录 junction / 文件符号链接）
# --------------------------------------------------------------------------
DESKTOP_LINK_NAME = "设备电量查看器"          # 桌面上的项目文件夹链接名
LAUNCHER_NAMES = ["我的设备电量.bat", "电量托盘.bat"]


def desktop_dir() -> str:
    return win_shell.desktop_dir()


def desktop_link_status() -> dict:
    """桌面链接现状：{'folder': (路径, 类型), 'links': {名字: (路径, 类型)}}"""
    d = win_shell.desktop_dir()
    info = {
        "folder": (os.path.join(d, DESKTOP_LINK_NAME),
                   win_link.link_kind(os.path.join(d, DESKTOP_LINK_NAME))),
        "links": {},
    }
    for name in LAUNCHER_NAMES:
        p = os.path.join(d, name)
        info["links"][name] = (p, win_link.link_kind(p))
    return info


def install_desktop_links() -> dict:
    """建桌面链接：项目文件夹的目录链接 + 启动脚本的软链接。

    返回 {'folder': 类型, 'links': {名字: 类型}, 'errors': [消息]}
    """
    d = win_shell.desktop_dir()
    out: dict = {"folder": "", "links": {}, "errors": []}

    try:
        out["folder"] = win_link.create_dir_link(
            BASE_DIR, os.path.join(d, DESKTOP_LINK_NAME))
    except OSError as e:
        out["errors"].append(str(e))

    for name in LAUNCHER_NAMES:
        src = os.path.join(BASE_DIR, name)
        if not os.path.isfile(src):
            out["errors"].append(f"项目里没有 {name}，跳过")
            continue
        try:
            out["links"][name] = win_link.create_file_link(
                src, os.path.join(d, name))
        except OSError as e:
            out["errors"].append(str(e))
    return out


# --------------------------------------------------------------------------
# 进程间通信（窗口消息，双方都注册同名的 message）
# --------------------------------------------------------------------------
MSG_QUIT = "DeviceBatteryTray_Quit"       # 让托盘退出
MSG_RELOAD = "DeviceBatteryTray_Reload"   # 让托盘重读配置


def tray_hwnd() -> int:
    h = USER32.FindWindowW(WND_CLASS, None)
    return int(h) if h else 0


def is_tray_running() -> bool:
    return tray_hwnd() != 0


def post_to_tray(msg_name: str) -> bool:
    hwnd = tray_hwnd()
    if not hwnd:
        return False
    msg = USER32.RegisterWindowMessageW(msg_name)
    return bool(USER32.PostMessageW(hwnd, msg, 0, 0))


def ui_hwnd() -> int:
    """找面板那个窗口。

    不能直接 FindWindowW：Tk 除了可见的面板，还会建一个**同名**的隐藏窗口
    （客户区 0×0、宽高只有标题栏那么大、被扔到屏幕外 -32000）。Z 序一排前面，
    FindWindowW 就会抓到它 —— 于是「打开面板」变成对一个看不见的窗口
    ShowWindow，用户点了毫无反应。所以这里枚举所有同名顶层窗口，
    挑真正显示在屏幕上的那个。
    """
    hits = []

    def _cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        USER32.GetWindowTextW(hwnd, buf, 256)
        if buf.value != UI_TITLE or not USER32.IsWindowVisible(hwnd):
            return True
        rc = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(rc))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        # 隐藏窗口是 338×40 那一档，正常面板怎么也有 700×450 以上
        if w < 400 or h < 300 or rc.left <= -30000:
            return True
        hits.append(int(hwnd))
        return False
    try:
        USER32.EnumWindows(_ENUM_PROC(_cb), 0)
    except Exception:
        pass
    return hits[0] if hits else 0


def start_tray_app(wait_timeout: float = 6.0) -> bool:
    """启动托盘程序；返回托盘窗口是否真的起来了。

    启动前先清理"没有窗口却还占着单实例"的僵尸进程，
    启动后等托盘窗口出现（pythonw 是静默进程，只能靠窗口判断成败）。
    """
    import subprocess
    if is_tray_running():
        return True
    cleanup_zombie()
    try:
        subprocess.Popen(tray_command(), cwd=BASE_DIR, creationflags=0x00000008,
                         env=child_env())
    except Exception:
        return False
    return wait_for_tray(wait_timeout)


# --------------------------------------------------------------------------
# 进程辅助：用于识别 / 清理僵尸托盘进程
# --------------------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259

KERNEL32.OpenProcess.restype = ctypes.c_void_p
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
KERNEL32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.DWORD)]
KERNEL32.TerminateProcess.restype = wintypes.BOOL
KERNEL32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL
KERNEL32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

_NTDLL = ctypes.WinDLL("ntdll")
_NTDLL.NtQueryInformationProcess.restype = ctypes.c_long
_NTDLL.NtQueryInformationProcess.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG)]
_PROCESS_COMMAND_LINE_INFORMATION = 60


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


def process_cmdline(pid: int) -> str:
    """取进程命令行（用于确认它确实是我们的托盘程序）。"""
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = 4096
        buf = ctypes.create_string_buffer(size)
        need = wintypes.ULONG(0)
        st = _NTDLL.NtQueryInformationProcess(
            h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(need))
        if st < 0 and need.value:
            size = int(need.value) + 16
            buf = ctypes.create_string_buffer(size)
            st = _NTDLL.NtQueryInformationProcess(
                h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size,
                ctypes.byref(need))
        if st < 0:
            return ""
        us = _UNICODE_STRING.from_buffer(buf)
        if not us.Buffer or not us.Length:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return ""
    finally:
        KERNEL32.CloseHandle(h)


def write_pid(pid: Optional[int] = None) -> None:
    """把自己的 PID 写下来（进程意外卡死时，下一个实例能据此清掉它）。"""
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(pid if pid is not None else os.getpid()))
    except OSError:
        pass


def read_pid() -> int:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not KERNEL32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        KERNEL32.CloseHandle(h)


def process_image(pid: int) -> str:
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if KERNEL32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        KERNEL32.CloseHandle(h)


def kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    h = KERNEL32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(KERNEL32.TerminateProcess(h, 1))
    finally:
        KERNEL32.CloseHandle(h)


def cleanup_zombie(wait: float = 2.0) -> bool:
    """清理僵尸托盘进程：上一个实例还活着（占着单实例锁），但窗口已经销毁。

    双重确认后才动手，避免 PID 复用误杀无辜进程：
      1) 镜像名确实是我们的程序（打包后比对 exe 全路径，源码跑看 python + tray_app）；
      2) 命令行里确实有托盘相关的特征。
    任一不满足就只清掉自己的 pid 文件，绝不碰那个进程。
    """
    import time
    pid = read_pid()
    if pid in (0, os.getpid()):
        return False
    if not pid_alive(pid):
        remove_pid()
        return False
    image = os.path.basename(process_image(pid)).lower()
    cmd = process_cmdline(pid).lower()
    if FROZEN:
        mine = os.path.abspath(process_image(pid)).lower()
        same = bool(mine) and mine == os.path.abspath(sys.executable).lower()
    else:
        same = image.startswith("python") and "tray_app" in cmd
    if not same:
        remove_pid()
        return False
    if not kill_pid(pid):
        return False
    deadline = time.time() + max(wait, 0.2)
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.05)
    remove_pid()
    return True


def wait_for_tray(timeout: float = 6.0) -> bool:
    """等托盘窗口出现（启动是否成功只能靠它判断）。"""
    import time
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if is_tray_running():
            return True
        time.sleep(0.15)
    return is_tray_running()
