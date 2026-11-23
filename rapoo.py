# -*- coding: utf-8 -*-
"""雷柏 2.4G 鼠标电量读取（VT7 V2 / VT7S Pro V2 通用）。

原理：接收器在 COL_09 这个厂商通道上，大约每 3 秒主动播报一条 19 字节的状态帧，
不需要我们发任何命令，纯监听即可。

两款鼠标帧格式完全一样，只有产品号不同（都是 VID 24AE）：
    0x1460 → VT7 V2       （2026-11 在用的黑色版）
    0x14A8 → VT7S Pro V2  （老款）

实测到的帧样例（VT7 V2，连续三条，只有最后一位在动）：
    07 20 02 40 06 40 06 01 51 00 03 84 00 60 46 01 02 02 BB
    07 20 02 40 06 40 06 01 51 00 03 84 00 60 46 01 02 02 BA
老款 VT7S Pro V2 的帧：
    07 40 02 40 06 40 06 01 53 00 03 84 00 A8 46 10 02 02 C0

字段解读（⚑ = 已用真实数据核对过）：
    [0]     0x07  报文编号
    [1:3]   20 02 / 40 02          含义未知（两款不同，与设置无关，先不管）
    [3:5] ⚑ 40 06 → 0x0640 = 1600  DPI（小端存储）
    [5:7]   40 06 → 0x0640 = 1600  同上，疑似 Y 轴 DPI
    [7]     0x01  状态标志
    [8] ⚑   0x51  电量百分比（VT7 V2 实测 81%，旧款 0x53=83%，两代都在这一位）
    [9]     0x00  充电标志（非 0 = 正在充电）
    [10:12] 03 84 → 900           含义未知（不是 DPI）
    [13:15] 60 46 / A8 46         含义未知（两款不同）
    [18]    C0/C1/BB/BA           滚动计数或校验位
"""

import time

import hid_win as hw

VID = 0x24AE

# 产品号 → 显示名。顺序 = 探测优先级（在用的排前面）
MODELS = {
    0x1460: "雷柏 VT7 V2",
    0x14A8: "雷柏 VT7S Pro V2",
}
PIDS = tuple(MODELS)

PID = 0x1460            # 兼容老引用（默认当在用的那款）
REPORT_ID = 0x07
STATUS_LEN = 19

BATTERY_INDEX = 8       # ⚑ 已核对：0x51 = 81%（VT7 V2）、0x53 = 83%（旧款）
CHARGING_INDEX = 9
DPI_RANGE = (3, 5)      # ⚑ 已核对：40 06 小端 = 1600


def find_channels(vid=VID, pid=None):
    """找出 19 字节的播报通道。pid 不传就两款一起找。"""
    want = {(vid, p) for p in (PIDS if pid is None else (pid,))}
    return [it for it in hw.enumerate_hid(want) if it.get("in_len") == STATUS_LEN]


def detect():
    """当前插着哪款雷柏？返回产品号，没插返回 None。"""
    chans = find_channels()
    return chans[0].get("pid") if chans else None


def model_name(pid=None):
    return MODELS.get(pid if pid is not None else detect(), "雷柏鼠标")


def _channel():
    chans = find_channels()
    if not chans:
        return None
    # 优先选实测会播报的那个，按路径里的 COL 号兜底
    for it in chans:
        if "COL_09" in it["short"].upper():
            return it
    return chans[0]


def read_status(timeout=8.0):
    """监听一条状态帧。返回 dict，读不到返回 None。

    timeout 到点还没等到播报就放弃 —— 接收器每 3 秒一条，默认给 8 秒足够。
    """
    ch = _channel()
    if not ch:
        return None
    pid = ch.get("pid")

    h, err = hw.open_device(ch["path"], read=True, write=False, overlapped=True)
    if not h:
        return {"error": "打不开通道（错误码 %s）" % err}

    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            data, e = hw._read_overlapped(h, STATUS_LEN, timeout_ms=500)
            if not data:
                continue
            if len(data) >= STATUS_LEN and data[0] == REPORT_ID:
                st = decode(data)
                st["pid"] = pid
                st["model"] = MODELS.get(pid, "雷柏鼠标")
                return st
    finally:
        hw.close(h)
    return None


def decode(frame):
    battery = frame[BATTERY_INDEX] if len(frame) > BATTERY_INDEX else None
    charging = None
    if len(frame) > CHARGING_INDEX:
        charging = frame[CHARGING_INDEX] != 0
    dpi = None
    a, b = DPI_RANGE
    if len(frame) >= b:
        dpi = frame[a] | (frame[a + 1] << 8)  # 小端：40 06 -> 1600
    return {
        "raw": frame,
        "battery": battery,
        "charging": charging,
        "dpi": dpi,
    }


def main():
    st = read_status()
    if st is None:
        print("没等到播报。确认接收器插好、鼠标已开机并处于 2.4G 模式。")
        return 1
    if "error" in st:
        print(st["error"])
        return 1

    print("型号  : %s" % st.get("model"))
    print("原始帧: %s" % hw.hexs(st["raw"]))
    print("电量  : %s%%" % st["battery"])
    print("充电  : %s" % ("是" if st["charging"] else "否"))
    print("DPI   : %s" % st["dpi"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
