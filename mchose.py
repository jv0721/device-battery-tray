# -*- coding: utf-8 -*-
"""mchose.py —— 迈从 G87 V2 键盘电量（有线 / 2.4G 两种接法）

同一个键盘会以两个产品号出现
----------------------------
    0x3033  「MCHOSE G87 V2 2.4G」  接收器（键盘走无线时靠它转发）
    0x2015  「MCHOSE G87 V2」       键盘本体（插线时直接连它）

官方网页驱动也是按 PID 判断有线/无线的（`LKt()` 里那张表命中就标 wired），
所以这里不猜协议，直接看谁在。两个都插着时按有线算 —— 那才是真正在用的那条。

有线时读不到电量
----------------
实测这块固件插线后**不提供电量**：
* `0x65`（官方驱动 D8t() 用的那条「电量+充电状态」查询）回 `AA 65 … "unknw"`；
* `0x05` 还能回，但返回的是静态配置块（灯效/档位那份），第 40 字节不是电量，
  连续 4 分半一个字节都没变，也不存在被动播报。
所以有线时 `query_battery()` 给 `(None, None)`，由界面显示「USB 供电」。

2.4G 时怎么读
-------------
**主动查询（主用）**：发 `55 05 00 38 38 00 …`（读配置块，只读命令），
   应答 `AA 05 …` 的第 40 字节是电量，第 41 字节是充电标志。

       +28  30 00 02 06 00 01 01 00
            │  └─ 41 = 充电
            └──── 40 = 电量（0x30 = 48）

   这条命令是官方网页驱动每次连上设备时都会发的，本机实测 0~100 合理、充电位对得上。

**被动播报（备用）**：设备有时会主动推一帧（报文编号 0，64 字节）：

       00 FA FB 07 3A 00 00 …
          │  │  │  └── 电量百分比（0x3A = 58）
          │  │  └───── 类型 07 = 电量
          │  └──────── 帧头
          └─────────── 报文编号 0

   实测序列 58 → 56 → 55 → 54 → 52 → 50，间隔约 36 秒。不保证随时都有，
   所以只能当兜底。

重要
----
**本模块只发 cmd 0x05（读配置块），绝不向设备写任何东西。**
之前的写入式试探曾误伤键盘键位（0x01 是写键位、0x06/0x0C 碰到回报率档位），
所以 `m55.build()` 的只读白名单闸门必须一直关着。

关键经验（踩过的坑）
--------------------
* Windows 的 WriteFile 必须写满 OutputReportByteLength（本机 65 字节）。
* 打开句柄时要 HidD_SetNumInputBuffers(64)（WebHID 的 open() 会做），
  否则接收器不推送。这一招对雷柏是毒药，只给迈从用。
* 同步句柄上不能一边阻塞读一边写（会死锁），所以读、写各用一个句柄。
* 读到超时**不能** CancelIoEx + 立即 CloseHandle，反复几百次会让读取队列失效，
  读线程静默退出 —— 这是前面几轮「零回包」的真凶。
* 应答会从后台监听线程那个读句柄上来，所以主动查询**复用**监听句柄读，
  另开一个写句柄发命令，不新开读句柄。
"""

from __future__ import annotations

import ctypes
import re
import threading
import time

import hid_win as hw
import m55

VID = 0x3837

# 同一个键盘会以两个不同的产品号出现，插不插线是两台「设备」：
#   0x3033  「MCHOSE G87 V2 2.4G」 —— 接收器（键盘走无线时用它转发）
#   0x2015  「MCHOSE G87 V2」      —— 键盘本体（插线时直接连它）
# 官方网页驱动也是这么分的：它按 PID 查表，命中的直接标成 wired。
# 所以「有线 / 2.4G」不用问协议，看谁在就行。
PID_DONGLE = 0x3033
PID_WIRED = 0x2015
PIDS = (PID_WIRED, PID_DONGLE)      # 顺序即优先级：插着线就优先走有线那条

PID = PID_DONGLE                    # 兼容老代码里的引用

MAGIC = b"\xFA\xFB"
TYPE_BATTERY = 0x07
TYPE_STATUS = 0x06

READ_LEN = 65          # 报文编号 1 字节 + 64 字节
PUSH_INTERVAL = 36.0   # 实测播报间隔（秒）

# ---- 主动查询：cmd 0x05「读配置块」 ----
CMD_INFO = 0x03        # 「读设备信息」，应答里带固件日期和接收器名
DATA_OFF = 8
CFG_CMD = 0x05
CFG_OFFSET = 0x0000
CFG_LENGTH = 0x38      # 56 字节
BATT_IDX = 40          # AA 05 帧内偏移
CHG_IDX = 41
QUERY_WAIT = 2.0       # 发命令后等应答的秒数

INFO_WAIT = 1.8

_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

_LATEST = {"battery": None, "status": None, "ts": 0.0, "raw": None,
           "charging": None, "query_ts": 0.0,
           "info": None, "info_ts": 0.0}
_LOCK = threading.Lock()
_THREAD = None
_ON_VALUE = None
_STOP = threading.Event()
# 读句柄就绪信号：主动查询必须等它先挂上，否则应答会丢
_READY = threading.Event()


def _vendor_path(pid):
    """这个产品号下面的厂商数据接口（usage page 0x0001 / usage 0x0000，即 MI_01）。"""
    for it in hw.enumerate_hid({(VID, pid)}):
        if it.get("usage_page") == 1 and it.get("usage") == 0:
            return it["path"]
    return None


def find_path():
    """当前该找谁说话。有线优先 —— 插着线时接收器还挂在机器上，

    但它已经连不上键盘了（发 0x05 一声不吭，只有 0x03 从缓存里回个旧信息），
    所以只要键盘本体在，就一律走键盘本体。
    """
    for pid in PIDS:
        path = _vendor_path(pid)
        if path:
            return path
    return None


def connection():
    """'有线' / '2.4G' / None（没插）。

    判据和官方驱动一致：看设备号。0x2015 在就是有线，
    只有 0x3033 在就是走接收器的 2.4G。
    """
    if _vendor_path(PID_WIRED):
        return "有线"
    if _vendor_path(PID_DONGLE):
        return "2.4G"
    return None


def _parse_info(data):
    """解析 AA 03 的设备信息。

    原始数据形如：
        24 01 30 01 FF FF 2C "Jan 12 2026,20:41:47" "2.4G Dongle"
    能稳定拿到的是固件编译日期 / 时间，以及「是不是接收器」。
    """
    if not data:
        return {}
    txt = bytes(x if 32 <= x < 127 else 32 for x in data).decode("ascii", "ignore")
    out = {"raw": " ".join(txt.split())}
    m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})", txt)
    if m and m.group(1) in _MONTHS:
        out["fw_date"] = "%s-%02d-%02d" % (m.group(3), _MONTHS[m.group(1)],
                                           int(m.group(2)))
    m = re.search(r"(\d{2}):(\d{2}):(\d{2})", txt)
    if m:
        out["fw_time"] = "%s:%s" % (m.group(1), m.group(2))
    if "Dongle" in txt or "dongle" in txt:
        out["receiver"] = "2.4G 接收器"
    return out


def decode(frame):
    """从一帧原始数据解析。返回 dict 或 None。

    两类帧：
      * 主动查询应答  AA 05 …        → kind="cfg"，第 40 字节是电量
      * 设备播报      00 FA FB 07 xx → kind="battery"
    frame 可能带前导的报文编号 0，也可能不带，所以先对齐。
    """
    if not frame or len(frame) < 8:
        return None
    b = bytes(frame)

    # ---- 设备信息应答 AA 03：对齐到 AA ----
    pos = b.find(b"\xAA", 0, 3)
    if pos >= 0 and len(b) - pos >= 16 and b[pos + 1] == CMD_INFO:
        body = b[pos:]
        length = body[4]
        data = body[DATA_OFF:DATA_OFF + max(length, 1)]
        return {"kind": "info", "battery": None, "status": None,
                "charging": None, "raw": body[:12],
                "info": _parse_info(data)}

    # ---- 主动查询应答：对齐到 AA ----
    pos = b.find(b"\xAA", 0, 3)
    if pos >= 0 and len(b) - pos >= BATT_IDX + 2 and b[pos + 1] == CFG_CMD:
        body = b[pos:]
        level = body[BATT_IDX]
        charging = body[CHG_IDX]
        if 0 <= level <= 100:
            return {"kind": "cfg", "battery": level, "status": None,
                    "charging": bool(charging), "raw": body[:12]}

    # ---- 设备主动播报：对齐到 FA FB ----
    pos = b.find(MAGIC, 0, 5)
    if pos < 0:
        return None
    body = b[pos:]
    if len(body) < 4:
        return None
    kind = body[2]
    value = body[3]
    if kind == TYPE_BATTERY:
        return {"kind": "battery", "battery": value, "status": None,
                "charging": None, "raw": body[:8]}
    if kind == TYPE_STATUS:
        return {"kind": "status", "battery": None, "status": value,
                "charging": None, "raw": body[:8]}
    return {"kind": "other", "battery": None, "status": None,
            "charging": None, "raw": body[:8]}


def _worker():
    while not _STOP.is_set():
        path = find_path()
        if not path:
            _STOP.wait(3.0)
            continue
        h, err = hw.open_device(path, read=True, write=False,
                                overlapped=False, input_buffers=64)
        if not h:
            _READY.clear()
            _STOP.wait(3.0)
            continue
        buf = (ctypes.c_ubyte * READ_LEN)()
        _READY.set()
        try:
            while not _STOP.is_set():
                got = ctypes.wintypes.DWORD(0)
                ok = ctypes.windll.kernel32.ReadFile(h, buf, READ_LEN,
                                                     ctypes.byref(got), None)
                if not ok or got.value == 0:
                    break
                info = decode(bytes(bytearray(buf[:got.value])))
                if not info:
                    continue
                with _LOCK:
                    _LATEST["raw"] = info["raw"]
                    _LATEST["ts"] = time.time()
                    if info["kind"] in ("battery", "cfg") and 0 <= info["battery"] <= 100:
                        _LATEST["battery"] = info["battery"]
                        if info["kind"] == "cfg":
                            _LATEST["charging"] = info["charging"]
                            _LATEST["query_ts"] = time.time()
                        cb = _ON_VALUE
                    elif info["kind"] == "status":
                        _LATEST["status"] = info["status"]
                        cb = None
                    elif info["kind"] == "info":
                        _LATEST["info"] = info.get("info") or {}
                        _LATEST["info_ts"] = time.time()
                        cb = None
                    else:
                        cb = None
                if cb:
                    try:
                        cb(info["battery"])
                    except Exception:
                        pass
        finally:
            _READY.clear()
            hw.close(h)
        _STOP.wait(2.0)


def start_watcher(on_value=None):
    """启动后台监听线程（只读）。重复调用无副作用。"""
    global _THREAD, _ON_VALUE
    if on_value is not None:
        _ON_VALUE = on_value
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=_worker, name="mchose-watcher", daemon=True)
    _THREAD.start()
    return _THREAD


def stop_watcher():
    _STOP.set()


def latest(max_age=0.0):
    """取最近一次电量。max_age > 0 时超龄返回 None。"""
    with _LOCK:
        val = _LATEST["battery"]
        ts = _LATEST["ts"]
    if val is None:
        return None
    if max_age and (time.time() - ts) > max_age:
        return None
    return val


def _write_frame(path, frame):
    """另开一个写句柄发一帧（不读），发完立刻关。返回 (ok, err)。

    读和写必须是两个句柄：同一个同步句柄上一边阻塞读一边写会死锁。
    """
    h, err = hw.open_device(path, read=False, write=True, overlapped=False)
    if not h:
        return False, err
    try:
        payload = b"\x00" + frame          # 报文编号 0 + 64 字节
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        written = ctypes.wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.WriteFile(h, buf, len(payload),
                                             ctypes.byref(written), None)
        return bool(ok), (0 if ok else ctypes.get_last_error())
    finally:
        hw.close(h)


def query_battery(timeout=QUERY_WAIT):
    """主动读一次电量。返回 (电量, 是否充电)，失败给 (None, None)。

    只发 cmd 0x05（读配置块）—— `m55.build()` 的白名单会拦住其它命令。
    应答由后台监听线程的读句柄接收，所以这里只开写句柄。
    """
    path = find_path()
    if not path:
        return None, None
    start_watcher()
    _READY.wait(2.5)
    with _LOCK:
        before = _LATEST["query_ts"]
    try:
        frame = m55.build(CFG_CMD, CFG_OFFSET, CFG_LENGTH)
    except Exception:
        return None, None
    ok, _err = _write_frame(path, frame)
    if not ok:
        return None, None
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            if _LATEST["query_ts"] > before:
                return _LATEST["battery"], _LATEST["charging"]
        time.sleep(0.05)
    return None, None


def is_charging():
    with _LOCK:
        return _LATEST["charging"]


def query_info(timeout=INFO_WAIT):
    """主动读一次设备信息（cmd 0x03，只读）。拿不到就返回 {}。

    返回 {'fw_date': '2026-01-12', 'fw_time': '20:41', 'receiver': '2.4G 接收器'}
    """
    path = find_path()
    if not path:
        return {}
    start_watcher()
    _READY.wait(2.5)
    with _LOCK:
        before = _LATEST["info_ts"]
    try:
        frame = m55.build(CMD_INFO, 0, m55.DATA_MAX)
    except Exception:
        return {}
    ok, _err = _write_frame(path, frame)
    if not ok:
        return {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            if _LATEST["info_ts"] > before:
                return dict(_LATEST["info"] or {})
        time.sleep(0.05)
    return {}


def read_status(timeout=40.0, verbose=False):
    """读电量，返回 (电量, 来源说明) 或 (None, 原因)。

    顺序：主动查询 → 近 90 秒内的缓存 → 等设备播报（约 36 秒一帧）。
    """
    lvl, _chg = query_battery()
    if lvl is not None:
        return lvl, "查询"

    cached = latest(max_age=90)
    if cached is not None:
        return cached, "缓存"

    with _LOCK:
        before = _LATEST["ts"]
    start_watcher()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            val = _LATEST["battery"]
            ts = _LATEST["ts"]
        if val is not None and ts > before:
            return val, "播报"
        time.sleep(0.2)
    if verbose:
        print("   (等待 %.0f 秒仍未收到播报)" % timeout)
    return None, "未收到播报"


if __name__ == "__main__":
    import os
    import sys

    print("键盘:", "已连接" if find_path() else "未找到")
    hits = []

    def show(v):
        hits.append(v)
        print("  收到电量播报: %d%%" % v)

    start_watcher(show)
    t0 = time.time()
    while time.time() - t0 < 80 and len(hits) < 3:
        time.sleep(0.5)
    print("共收到 %d 次播报" % len(hits))
    sys.stdout.flush()
    os._exit(0)
