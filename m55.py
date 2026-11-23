# -*- coding: utf-8 -*-
"""迈从 2.4G 的 55/AA 帧协议 —— 从官方网页的真实收发日志反解。

帧结构（固定 64 字节，reportId = 0）：

    下标  0     1      2     3        4      5       6       7     8..63
         55/AA  cmd    00    校验     长度   offL    offH    00    数据(56B)

校验 = (长度 + offL + offH) & 0xFF

依据（全部来自官方页面的真实收发）：
    55 03 00 38 | 38 00 00 00   -> 38 = 0x38+0+0
    55 05 00 78 | 38 40 00 00   -> 78 = 0x38+0x40+0
    55 05 00 80 | 08 78 00 00   -> 80 = 0x08+0x78+0
    55 F1 00 89 | 38 50 01 00   -> 89 = 0x38+0x50+0x01
    55 F1 00 C1 | 38 88 01 00   -> C1 = 0x38+0x88+0x01
    55 F1 00 87 | 38 48 07 00   -> 87 = 0x38+0x48+0x07
"""

import ctypes
import os
import threading
import sys
import time

import hid_win as H

VID, PID = 0x3837, 0x3033
REPORT_ID = 0
FRAME_LEN = 64
HEAD_REQ = 0x55
HEAD_RSP = 0xAA
DATA_OFF = 8
DATA_MAX = 56          # 64 - 8

# 已知命令码
CMD_INFO = 0x03        # 设备信息（返回 "…,Jan 12 2026,20:41:472.4G Dongle"）
CMD_LIGHT = 0x04       # 灯效
CMD_CFG = 0x05         # 配置块（按 offset 读）
CMD_READ = 0xF1        # 长数据分片读
CMD_WRITE = 0xF2       # 分片写


def checksum(length, offset):
    return (length + (offset & 0xFF) + ((offset >> 8) & 0xFF)) & 0xFF


# ------------------------------------------------------------------ 安全闸门
# 血的教训：0x01 这条命令在固件里是「写键位」。曾经带着一字节全零载荷发出去，
# 结果把键盘每行左侧三个键写成了「无功能」，只能靠网页恢复默认才救回来。
#
# 从此本模块只放行经过确认的只读命令。任何写操作必须先在这里显式登记，
# 否则直接抛异常 —— 宁可功能做不出来，也不能再把用户的键盘弄坏。
READ_ONLY_CMDS = {
    CMD_INFO,    # 0x03 设备信息
    CMD_LIGHT,   # 0x04 灯效（读取）
    CMD_CFG,     # 0x05 配置块（按 offset 读）
    CMD_READ,    # 0xF1 分片读
}

ALLOW_WRITES = False          # 永不轻易打开


def build(cmd, offset=0, length=DATA_MAX, data=b""):
    """构造一条 64 字节请求帧。只允许只读命令。"""
    if not ALLOW_WRITES and cmd not in READ_ONLY_CMDS:
        raise RuntimeError(
            "拒绝发送 0x%02X：该命令未经只读白名单确认，可能写入设备" % (cmd & 0xFF))
    if data and not ALLOW_WRITES:
        raise RuntimeError("拒绝发送带数据的帧：本模块只做只读访问")
    if length > DATA_MAX:
        raise ValueError("单帧最多 %d 字节" % DATA_MAX)
    body = bytearray(FRAME_LEN)
    body[0] = HEAD_REQ
    body[1] = cmd & 0xFF
    body[2] = 0x00
    body[3] = checksum(length, offset)
    body[4] = length
    body[5] = offset & 0xFF
    body[6] = (offset >> 8) & 0xFF
    body[7] = 0x00
    if data:
        n = min(len(data), length, DATA_MAX)
        body[DATA_OFF:DATA_OFF + n] = data[:n]
    return bytes(body)


def parse(frame):
    """解析一条应答帧，返回 dict；不是应答帧则返回 None。"""
    if not frame or len(frame) < DATA_OFF:
        return None
    # 有的通道会把 reportId 一起读回来，先对齐到 55/AA
    b = bytes(frame)
    if b[0] != HEAD_RSP:
        idx = b.find(bytes([HEAD_RSP]))
        if idx < 0:
            return None
        b = b[idx:]
    if len(b) < FRAME_LEN:
        b = b + b"\x00" * (FRAME_LEN - len(b))
    length = b[4]
    offset = b[5] | (b[6] << 8)
    payload = b[DATA_OFF:DATA_OFF + min(max(length, 1), DATA_MAX)]
    return {
        "cmd": b[1],
        "ck": b[3],
        "length": length,
        "offset": offset,
        "data": payload,
        "raw": b[:FRAME_LEN],
    }


def find_path():
    for item in H.enumerate_hid({(VID, PID)}):
        if item.get("usage_page") == 1 and item.get("usage") == 0:
            return item["path"], item
    return None, None


def transact(cmd, offset=0, length=DATA_MAX, data=b"", wait=1.2, path=None):
    """发一条帧，收集 wait 秒内的所有应答。返回 list[dict]。"""
    if path is None:
        path, _info = find_path()
    if not path:
        return [], "not-found"

    hr, e1 = H.open_device(path, read=True, write=False, overlapped=False,
                           input_buffers=64)
    if not hr:
        return [], "open-read-%d" % e1
    hw, e2 = H.open_device(path, read=False, write=True, overlapped=False)
    if not hw:
        H.close(hr)
        return [], "open-write-%d" % e2

    got = []

    def reader():
        buf = (ctypes.c_ubyte * (FRAME_LEN + 1))()
        while True:
            n = ctypes.wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.ReadFile(hr, buf, FRAME_LEN + 1,
                                                 ctypes.byref(n), None)
            if not ok or n.value == 0:
                return
            got.append(bytes(bytearray(buf[:n.value])))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.25)

    frame = build(cmd, offset, length, data)
    payload = b"\x00" + frame          # reportId 0 + 64 字节
    buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    written = ctypes.wintypes.DWORD(0)
    ok = ctypes.windll.kernel32.WriteFile(hw, buf, len(payload),
                                          ctypes.byref(written), None)
    err = ctypes.get_last_error() if not ok else 0

    time.sleep(wait)
    H.close(hw)
    # hr 故意不关：它还挂着阻塞读
    out = []
    for g in got:
        p = parse(g)
        if p:
            out.append(p)
    return out, (0 if ok else err)


def hexs(b, n=64):
    return " ".join("%02X" % x for x in b[:n])


def as_text(b):
    try:
        s = bytes(b).decode("utf-8", "replace")
    except Exception:
        return ""
    return "".join(ch if 32 <= ord(ch) < 127 or ord(ch) > 127 else "." for ch in s)


if __name__ == "__main__":
    p, info = find_path()
    print("设备:", (info or {}).get("short"), (info or {}).get("product"))
    print()

    tests = [
        ("设备信息 03", CMD_INFO, 0, DATA_MAX, b""),
        ("灯效 04", CMD_LIGHT, 0, DATA_MAX, b""),
        ("配置 05 off=0x40", CMD_CFG, 0x40, DATA_MAX, b""),
        ("配置 05 off=0x78 len=8", CMD_CFG, 0x78, 8, b""),
        ("长读 F1 off=0x150", CMD_READ, 0x150, DATA_MAX, b""),
    ]
    for name, cmd, off, ln, data in tests:
        f = build(cmd, off, ln, data)
        print("→", name, "|", hexs(f, 9), "...")
        reps, err = transact(cmd, off, ln, data, wait=1.5, path=p)
        if err:
            print("   写入失败 err=%s" % err)
        if not reps:
            print("   ✗ 无应答")
        for r in reps:
            print("   ← cmd=%02X off=0x%04X len=%d | %s" %
                  (r["cmd"], r["offset"], r["length"], hexs(r["data"], 24)))
            txt = as_text(r["data"]).rstrip(".")
            if txt:
                print("      文本: %s" % txt[:60])
        print()
        time.sleep(0.3)

    print("完成")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
