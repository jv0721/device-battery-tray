# -*- coding: utf-8 -*-
"""mchose_mouse.py —— 迈从 A7 V3 Ultra+ 无线鼠标电量

和键盘（G87 V2）完全是两套体系，别混用
---------------------------------------
键盘走 `55/AA` 帧、报文号 0、电量在第 40 字节；
鼠标走 `4D` 帧、报文号 **0x4D**、命令字 `0x0900`、电量在数据区第 12 字节。
两者只有「都是迈从」这一层关系，协议字节一个都不通用。

报文格式（官方设备中心驱动 `i1e()` / `mu()`）
--------------------------------------------
一次收发是一段 64 字节，**报文号 0x4D 单独算第一字节**，后面 63 字节是数据：

    4D | 01 | flags | len | cmdLo | cmdHi | biz | seq | data… | 校验

    ├ 0x4D        报文号（固定）
    ├ 0x01        固定
    ├ flags       固定 1
    ├ len         后面 data 的字节数
    ├ cmdLo/cmdHi 命令字小端（0x0900 → 00 09）
    ├ biz, seq    一般 0
    └ 校验        从 flags 到 data 末尾逐字节异或

查询「设备信息（含电量）」= 命令字 `0x0900`，数据区为空：

    请求  4D 01 01 00 00 09 00 00 08  00 ×55
    应答  4D 01 01 10 00 09 00 00 │ 37 38 26 40 04 00 00 00
                                  00 10 02 00 60 00 08 E4

应答数据区（从第 8 字节起）逐字节：

    [0..1] 厂商号 0x3837（迈从）
    [2..3] 产品号（无线模式下报 0x4026）
    [4]    最大配置数      [5] 宏保存类型
    [6..7] 共享宏区大小    [8..9] 宏单元大小
    [10]   连接状态        0=未连接，2=2.4G
    [11]   充电状态        0=未充电，非 0=充电中
    [12]   ★ 电量 0~100
    [13]   游戏模式

本机实测连读 6 次全部 96%、未充电，和官方网页驱动显示的一致。

来源
----
`https://www.mchose.com.cn/assets/purify.es-CxLZ1Htw.js`（官方设备中心驱动，未混淆）
里的 `i1e`（构造报文）、`mu`（发送）、`hgt`（读设备信息）、`AK`（解析）。
只借鉴协议事实，未复制代码。

接哪条接口
----------
`用途页 == 0xFF01`，本机是 `MI_02 COL_02`，输入/输出/属性报文都是 64 字节。
同一个设备的 `MI_00`（真正的鼠标接口）被系统鼠标驱动独占，打不开也没必要开。

安全
----
**只发 `0x0900` 一条只读命令。** `build()` 里带白名单闸门，
其它命令字一律抛异常，防止手滑把配置写坏。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time

import hid_win as hw

VID = 0x3837
PID = 0x1018

RID = 0x4D              # 报文号，也是帧的第一字节
USAGE_PAGE = 0xFF01     # 控制接口的用途页
PACKET = 64             # 帧总长（含报文号）

CMD_INFO = 0x0900       # 读设备信息（含电量）
READ_ONLY_CMDS = {CMD_INFO}

REPLY_TIMEOUT = 1.2     # 单次等应答的秒数（鼠标睡着时接收器要约 1 秒才回话）
RETRIES = 2
CACHE_SEC = 3.0         # 这么短时间内重复调用直接给缓存

DATA_OFF = 8            # 数据区在帧里的起点
BATT_IDX = 12
CHG_IDX = 11
LINK_IDX = 10

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_k32 = ctypes.windll.kernel32

_LATEST = {"battery": None, "charging": None, "link": None,
           "ts": 0.0, "raw": None,
           "online": None, "online_ts": 0.0}
_LOCK = threading.Lock()


def find_path():
    """定位控制接口：用途页 0xFF01（MI_02 COL_02，64 字节报文）。"""
    for it in hw.enumerate_hid({(VID, PID)}):
        if (it.get("usage_page") or 0) == USAGE_PAGE and (it.get("out_len") or 0) >= 2:
            return it["path"]
    return None


def _open(path, access):
    h = _k32.CreateFileW(path, access, 0x1 | 0x2, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        return None, ctypes.get_last_error()
    return h, 0


def _xor(buf, start, end):
    v = 0
    for i in range(start, end + 1):
        v ^= buf[i]
    return v & 0xFF


def build(cmd, data=b"", flags=1, biz=0, seq=0):
    """按官方规则拼一帧。非白名单命令直接拒绝。"""
    if cmd not in READ_ONLY_CMDS:
        raise RuntimeError("拒绝发送命令 0x%04X：不在只读白名单内" % cmd)
    if len(data) > PACKET - DATA_OFF - 1:
        raise ValueError("数据太长")

    pkt = bytearray(PACKET)
    pkt[0] = RID
    pkt[1] = 0x01
    pkt[2] = flags
    pkt[3] = len(data)
    pkt[4] = cmd & 0xFF
    pkt[5] = (cmd >> 8) & 0xFF
    pkt[6] = biz
    pkt[7] = seq
    for i, b in enumerate(data):
        pkt[DATA_OFF + i] = b
    if flags == 1:
        pkt[DATA_OFF + len(data)] = _xor(pkt, 2, DATA_OFF - 1 + len(data))
    return bytes(pkt)


def parse(reply):
    """解析 0x0900 的应答。不像正常应答就给空 dict。

    要区分两种应答：

        正常 4D 01 01 10 00 09 00 00 | 37 38 26 40 …   长度字段 = 0x10
        睡觉 4D 01 01 01 00 09 00 00 | FF              长度字段 = 0x01

    鼠标睡着时接收器照样立刻回话，但数据区只有一个 0xFF 错误码。
    以前没看长度字段，这种应答被当成「电量 0%、未连接」写进了配置，
    面板上就出现一只 0% 的鼠标 —— 比显示「上次读数」糟得多。
    """
    if not reply or len(reply) < DATA_OFF + 14:
        return {}
    b = bytes(reply)
    if b[0] != RID or b[4] != (CMD_INFO & 0xFF) or b[5] != (CMD_INFO >> 8):
        return {}
    if b[3] < 14:               # 长度字段不够，是错误应答，不是设备信息
        return {}
    d = b[DATA_OFF:]
    return {
        "vid": d[0] | (d[1] << 8),
        "pid": d[2] | (d[3] << 8),
        "max_config": d[4],
        "link": d[LINK_IDX],
        "charging": bool(d[CHG_IDX]),
        "battery": d[BATT_IDX],
        "game_mode": d[13],
        "raw": b[:DATA_OFF + 16],
    }


def _transact(path, pkt, timeout=REPLY_TIMEOUT):
    """发一帧、收一条 0x4D 应答。失败返回 (None, 原因)。"""
    hr, er = _open(path, GENERIC_READ)
    if not hr:
        return None, "打不开读句柄 err=%s" % er
    hwd, ew = _open(path, GENERIC_READ | GENERIC_WRITE)
    if not hwd:
        _k32.CloseHandle(hr)
        return None, "打不开写句柄 err=%s" % ew

    got = []
    stop = [False]

    def reader():
        buf = (ctypes.c_ubyte * PACKET)()
        while not stop[0]:
            n = ctypes.wintypes.DWORD(0)
            if not _k32.ReadFile(hr, buf, PACKET, ctypes.byref(n), None) or n.value == 0:
                return
            got.append(bytes(bytearray(buf[:n.value])))

    threading.Thread(target=reader, daemon=True).start()
    try:
        time.sleep(0.05)

        payload = (ctypes.c_ubyte * PACKET).from_buffer_copy(pkt)
        written = ctypes.wintypes.DWORD(0)
        if not _k32.WriteFile(hwd, payload, PACKET, ctypes.byref(written), None):
            return None, "写失败 err=%s" % ctypes.get_last_error()

        deadline = time.time() + timeout
        while time.time() < deadline and not stop[0]:
            for r in got:
                if r and r[0] == RID and len(r) >= DATA_OFF + 14:
                    return r, None
            if got:
                del got[:]
            time.sleep(0.015)
        return None, "无应答"
    finally:
        stop[0] = True
        _k32.CloseHandle(hwd)
        _k32.CancelIoEx(hr, None)
        _k32.CloseHandle(hr)


def _offline_reply(reply):
    """是不是「接收器在、鼠标不在」那句回话。"""
    if not reply or len(reply) < DATA_OFF:
        return False
    b = bytes(reply)
    return (b[0] == RID and b[3] < 14
            and b[4] == (CMD_INFO & 0xFF) and b[5] == (CMD_INFO >> 8))


def query_info():
    """主动读一次设备信息。返回 dict，读不到给 {}。

    鼠标睡着时接收器要等射频超时（约 1 秒）才回「连不上」，
    这一秒躲不掉；但既然它已经把话说清楚了，就没必要再重试三遍白等 3 秒。
    """
    path = find_path()
    if not path:
        return {}
    pkt = build(CMD_INFO)
    for _ in range(RETRIES):
        reply, _why = _transact(path, pkt)
        if reply:
            info = parse(reply)
            if info:
                if not info["link"]:
                    # 连上了接收器但鼠标没醒：这不是电量读数，一个字都别记
                    with _LOCK:
                        _LATEST["online"] = False
                        _LATEST["online_ts"] = time.time()
                    return {}
                with _LOCK:
                    _LATEST["battery"] = info["battery"]
                    _LATEST["charging"] = info["charging"]
                    _LATEST["link"] = info["link"]
                    _LATEST["ts"] = time.time()
                    _LATEST["raw"] = info["raw"]
                    _LATEST["online"] = True
                    _LATEST["online_ts"] = time.time()
                return info
            if _offline_reply(reply):
                with _LOCK:
                    _LATEST["online"] = False
                    _LATEST["online_ts"] = time.time()
                return {}
        time.sleep(0.06)
    return {}


def query_battery():
    """读一次电量。返回 (电量, 是否充电)，失败 (None, None)。"""
    info = query_info()
    if not info:
        return None, None
    return info["battery"], info["charging"]


def latest(max_age=0.0):
    with _LOCK:
        val = _LATEST["battery"]
        ts = _LATEST["ts"]
    if val is None:
        return None
    if max_age and (time.time() - ts) > max_age:
        return None
    return val


def is_charging():
    with _LOCK:
        chg = _LATEST["charging"]
        ts = _LATEST["ts"]
    if time.time() - ts > 30:
        return None
    return chg


def is_online():
    """在线 = 最近一次探测说连上了（2 表示 2.4G 无线）。"""
    with _LOCK:
        online = _LATEST["online"]
        ts = _LATEST["online_ts"]
    if time.time() - ts > 30:
        return None
    return bool(online)


def read_status(verbose=False):
    """读电量，返回 (电量, 来源)。来源为「查询」「缓存」「无应答」。"""
    with _LOCK:
        val = _LATEST["battery"]
        ts = _LATEST["ts"]
    if val is not None and (time.time() - ts) <= CACHE_SEC:
        return val, "缓存"

    lvl, _chg = query_battery()
    if lvl is not None:
        return lvl, "查询"
    if val is not None:
        if verbose:
            print("   (本次没读回来，用上次的 %d%%)" % val)
        return val, "缓存"
    return None, "无应答"


# 面板和托盘用统一接口，鼠标是主动查询型，没有后台监听，给空壳保持一致
def start_watcher(on_value=None):
    return None


def stop_watcher():
    return None


if __name__ == "__main__":
    import os
    import sys

    print("=" * 62)
    print("迈从 A7 V3 Ultra+ 电量")
    print("=" * 62)
    p = find_path()
    if not p:
        print("✗ 未找到设备（0x%04X:0x%04X）" % (VID, PID))
        sys.stdout.flush()
        os._exit(1)
    print("接口:", p.split("#")[1] if "#" in p else p)
    print()
    for i in range(3):
        t0 = time.time()
        info = query_info()
        ms = (time.time() - t0) * 1000
        if not info:
            online = is_online()
            print("%d）%5.0fms   读不到%s" % (
                i + 1, ms,
                "：鼠标睡着了（动一下它或按个键）" if online is False else ""))
            sys.stdout.flush()
            continue
        print("%d）%5.0fms   电量 %d%%   %s   %s" % (
            i + 1, ms, info["battery"],
            "充电中" if info["charging"] else "未充电",
            "2.4G" if info["link"] == 2 else "连接状态=%d" % info["link"]))
        sys.stdout.flush()
    print()
    print("原始帧:", " ".join("%02X" % x for x in (_LATEST["raw"] or b"")))
    sys.stdout.flush()
    os._exit(0)
