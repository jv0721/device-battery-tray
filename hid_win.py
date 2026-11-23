# -*- coding: utf-8 -*-
"""Windows HID 底层访问 —— 纯 ctypes，零第三方依赖。

只做三件事：
  1. 枚举本机所有 HID 接口，拿到 usage page / 报文长度 / 是否可读写；
  2. 读 Feature Report（设备主动提供的属性块）；
  3. 异步收发（Overlapped）—— 绝不在同一个句柄上并发「阻塞读 + 写」，
     那个写法实测会让进程直接消失，连异常栈都没有。
"""

import ctypes
import re
import time
from ctypes import (
    byref,
    sizeof,
    create_unicode_buffer,
    addressof,
    wstring_at,
    wintypes,
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid_dll = ctypes.WinDLL("hid", use_last_error=True)

# ---------------------------------------------------------------- 常量
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


# ---------------------------------------------------------------- 结构体
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self):
        return "{%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}" % (
            self.Data1, self.Data2, self.Data3,
            self.Data4[0], self.Data4[1], self.Data4[2], self.Data4[3],
            self.Data4[4], self.Data4[5], self.Data4[6], self.Data4[7],
        )


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("DevicePath", wintypes.WCHAR * 1),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# ---------------------------------------------------------------- 函数签名
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                          wintypes.HWND, wintypes.DWORD]

setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
    wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]

setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

hid_dll.HidD_GetHidGuid.restype = None
hid_dll.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]

hid_dll.HidD_GetAttributes.restype = wintypes.BOOLEAN
hid_dll.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

hid_dll.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
hid_dll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]

hid_dll.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
hid_dll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]

hid_dll.HidP_GetCaps.restype = wintypes.LONG
hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

hid_dll.HidD_GetProductString.restype = wintypes.BOOLEAN
hid_dll.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid_dll.HidD_GetManufacturerString.restype = wintypes.BOOLEAN
hid_dll.HidD_GetManufacturerString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid_dll.HidD_GetSerialNumberString.restype = wintypes.BOOLEAN
hid_dll.HidD_GetSerialNumberString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]

hid_dll.HidD_GetFeature.restype = wintypes.BOOLEAN
hid_dll.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid_dll.HidD_SetFeature.restype = wintypes.BOOLEAN
hid_dll.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]


# ---------------------------------------------------------------- 枚举
def _hid_guid():
    g = GUID()
    hid_dll.HidD_GetHidGuid(byref(g))
    return g


_PATH_RE = re.compile(
    r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})(?:&mi_([0-9a-f]{2}))?(?:&col([0-9a-f]{2}))?",
    re.I,
)


def _short_path(path):
    """把 100 多字符的设备路径压成 vid:pid MI_xx COL_xx 这种好读的形式。"""
    m = _PATH_RE.search(path)
    if not m:
        return path
    vid, pid, mi, col = m.groups()
    tag = "VID_%s/PID_%s" % (vid.upper(), pid.upper())
    if mi:
        tag += " MI_%s" % mi.upper()
    if col:
        tag += " COL_%s" % col.upper()
    return tag


def _get_string(handle, fn, size=256):
    buf = create_unicode_buffer(size)
    if fn(handle, buf, sizeof(buf)):
        return buf.value
    return ""


def open_device(path, read=True, write=True, overlapped=True, input_buffers=0):
    """打开设备，返回 (handle, err)。

    input_buffers: 打开后顺手设一下输入缓冲区大小（HidD_SetNumInputBuffers）。
    网页的 WebHID 在 open() 里会做这一步，裸 CreateFile 不做；实测迈从的接收器
    要靠这一步才会开始推送数据。

    ⚠ 默认 0（不设）：雷柏的通道一设就收不到播报了，所以只给需要的设备显式开启。
    """
    access = 0
    if read:
        access |= GENERIC_READ
    if write:
        access |= GENERIC_WRITE
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    h = kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             None, OPEN_EXISTING, flags, None)
    if h == INVALID_HANDLE_VALUE or h is None:
        return None, ctypes.get_last_error()
    if input_buffers:
        try:
            hid_dll.HidD_SetNumInputBuffers(h, int(input_buffers))
        except Exception:
            pass
    return h, 0


def close(h):
    if h:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def enumerate_hid(vid_pid_filter=None, with_caps=True):
    """列出所有 HID 接口。

    vid_pid_filter: 可选的集合，元素形如 (0x3837, 0x3033)，只要这些设备。
    返回 list[dict]，每项含 path / vid / pid / mi / col / usage_page / usage /
    三种报文长度 / can_write 等。
    """
    guid = _hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(byref(guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE_VALUE or not hdev:
        raise OSError("SetupDiGetClassDevsW 失败，错误码 %d" % ctypes.get_last_error())

    results = []
    idx = 0
    try:
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = sizeof(did)
            ok = setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid), idx, byref(did))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_NO_MORE_ITEMS:
                    break
                break
            idx += 1

            need = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(did), None, 0, byref(need), None)
            if need.value == 0:
                continue
            raw = ctypes.create_string_buffer(need.value)
            detail = ctypes.cast(raw, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
            detail.contents.cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, byref(did), detail, need.value, byref(need), None):
                continue
            # DevicePath 紧跟在 cbSize(DWORD) 后面，偏移 4；64 位下 cbSize 要填 8
            path = ctypes.cast(addressof(raw) + 4, ctypes.c_wchar_p).value

            m = _PATH_RE.search(path)
            if not m:
                continue
            vid = int(m.group(1), 16)
            pid = int(m.group(2), 16)
            if vid_pid_filter and (vid, pid) not in vid_pid_filter:
                continue

            item = {
                "path": path,
                "short": _short_path(path),
                "vid": vid,
                "pid": pid,
                "mi": m.group(3),
                "col": m.group(4),
            }

            h, err = open_device(path, read=False, write=False, overlapped=False)
            if not h:
                item["error"] = "无法打开(0 权限)，错误码 %d" % err
                results.append(item)
                continue

            try:
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = sizeof(attrs)
                if hid_dll.HidD_GetAttributes(h, byref(attrs)):
                    item["vid"] = attrs.VendorID
                    item["pid"] = attrs.ProductID
                    item["version"] = attrs.VersionNumber
                item["product"] = _get_string(h, hid_dll.HidD_GetProductString)
                item["manufacturer"] = _get_string(h, hid_dll.HidD_GetManufacturerString)
                item["serial"] = _get_string(h, hid_dll.HidD_GetSerialNumberString)

                if with_caps:
                    preparsed = ctypes.c_void_p()
                    if hid_dll.HidD_GetPreparsedData(h, byref(preparsed)):
                        caps = HIDP_CAPS()
                        rc = hid_dll.HidP_GetCaps(preparsed, byref(caps))
                        hid_dll.HidD_FreePreparsedData(preparsed)
                        if rc >= 0 and caps.InputReportByteLength:
                            item.update({
                                "usage_page": caps.UsagePage,
                                "usage": caps.Usage,
                                "in_len": caps.InputReportByteLength,
                                "out_len": caps.OutputReportByteLength,
                                "feat_len": caps.FeatureReportByteLength,
                                "n_collections": caps.NumberLinkCollectionNodes,
                                "n_in_value": caps.NumberInputValueCaps,
                            })
            finally:
                close(h)

            # 尝试用读写权限打开，判断能不能发指令
            h2, err2 = open_device(path, read=True, write=True, overlapped=True)
            if h2:
                item["can_write"] = True
                close(h2)
            else:
                item["can_write"] = False
                item["write_err"] = err2

            results.append(item)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)

    return results


# ---------------------------------------------------------------- Feature Report
def get_feature(path, report_id=0, length=0):
    """读一次 Feature Report。length=0 时按设备 caps 自动取。"""
    h, err = open_device(path, read=True, write=False, overlapped=False)
    if not h:
        return None, err
    try:
        if not length:
            preparsed = ctypes.c_void_p()
            if hid_dll.HidD_GetPreparsedData(h, byref(preparsed)):
                caps = HIDP_CAPS()
                hid_dll.HidP_GetCaps(preparsed, byref(caps))
                hid_dll.HidD_FreePreparsedData(preparsed)
                length = caps.FeatureReportByteLength or 65
        buf = (ctypes.c_ubyte * max(length, 2))()
        buf[0] = report_id
        if hid_dll.HidD_GetFeature(h, buf, length):
            return bytes(bytearray(buf[:length])), 0
        return None, ctypes.get_last_error()
    finally:
        close(h)


def set_feature(path, data):
    h, err = open_device(path, read=True, write=True, overlapped=False)
    if not h:
        return False, err
    try:
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        if hid_dll.HidD_SetFeature(h, buf, len(data)):
            return True, 0
        return False, ctypes.get_last_error()
    finally:
        close(h)


# ---------------------------------------------------------------- 异步收发
def _wait_io(handle, ov, buf, nbytes):
    written = wintypes.DWORD(0)
    ok = kernel32.WriteFile(handle, buf, nbytes, byref(written), byref(ov))
    if not ok:
        err = ctypes.get_last_error()
        if err != ERROR_IO_PENDING:
            return False, err, 0
    r = kernel32.WaitForSingleObject(ov.hEvent, 400)
    if r != WAIT_OBJECT_0:
        kernel32.CancelIoEx(handle, byref(ov))
        return False, -1, 0
    got = wintypes.DWORD(0)
    ok = kernel32.GetOverlappedResult(handle, byref(ov), byref(got), False)
    return bool(ok), 0, got.value


def _write_overlapped(handle, data, timeout_ms=400):
    ev = kernel32.CreateEventW(None, True, False, None)
    if not ev:
        return False, -2, 0
    ov = OVERLAPPED()
    ov.hEvent = ev
    try:
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(handle, buf, len(data), byref(written), byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                return False, err, 0
        r = kernel32.WaitForSingleObject(ev, timeout_ms)
        if r != WAIT_OBJECT_0:
            kernel32.CancelIoEx(handle, byref(ov))
            return False, WAIT_TIMEOUT, 0
        got = wintypes.DWORD(0)
        ok = kernel32.GetOverlappedResult(handle, byref(ov), byref(got), False)
        return bool(ok), 0, got.value
    finally:
        kernel32.CloseHandle(ev)


def _read_overlapped(handle, length, timeout_ms=400):
    ev = kernel32.CreateEventW(None, True, False, None)
    if not ev:
        return None, -2
    ov = OVERLAPPED()
    ov.hEvent = ev
    buf = (ctypes.c_ubyte * length)()
    try:
        got = wintypes.DWORD(0)
        ok = kernel32.ReadFile(handle, buf, length, byref(got), byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                return None, err
        r = kernel32.WaitForSingleObject(ev, timeout_ms)
        if r != WAIT_OBJECT_0:
            kernel32.CancelIoEx(handle, byref(ov))
            return None, WAIT_TIMEOUT
        ok = kernel32.GetOverlappedResult(handle, byref(ov), byref(got), False)
        if not ok:
            return None, ctypes.get_last_error()
        return bytes(bytearray(buf[:got.value])), 0
    finally:
        kernel32.CloseHandle(ev)


def transact(path, payload, in_len=64, timeout_ms=600, drain=True):
    """发一条报文，然后收一段时间内的所有回复。

    返回 list[(耗时毫秒, bytes)]。为了安全，整个收发都用 Overlapped，
    不会出现「一边阻塞读一边写」那种把进程搞死的写法。
    """
    h, err = open_device(path, read=True, write=True, overlapped=True)
    if not h:
        return None, err

    replies = []
    t0 = time.time()
    try:
        if drain:
            # 先把缓存里的旧报文读干净，免得把历史数据当成本次回复
            for _ in range(8):
                old, e = _read_overlapped(h, in_len, timeout_ms=30)
                if old is None:
                    break

        ok, werr, _n = _write_overlapped(h, payload, timeout_ms=timeout_ms)
        if not ok:
            return None, werr if werr else ctypes.get_last_error()

        deadline = t0 + timeout_ms / 1000.0
        while time.time() < deadline:
            left = max(20, int((deadline - time.time()) * 1000))
            data, e = _read_overlapped(h, in_len, timeout_ms=min(left, 300))
            if data:
                replies.append((int((time.time() - t0) * 1000), data))
            elif e == WAIT_TIMEOUT:
                continue
            else:
                break
        return replies, 0
    finally:
        close(h)


def sniff(path, in_len=64, seconds=20, quiet_gap_ms=1500, on_report=None):
    """只读监听：不发任何命令，纯粹收设备自己吐出来的报文。"""
    h, err = open_device(path, read=True, write=False, overlapped=True)
    if not h:
        return None, err
    out = []
    t0 = time.time()
    deadline = t0 + seconds
    try:
        while time.time() < deadline:
            data, e = _read_overlapped(h, in_len, timeout_ms=400)
            if data:
                t = int((time.time() - t0) * 1000)
                out.append((t, data))
                if on_report:
                    on_report(t, data)
            elif e == WAIT_TIMEOUT:
                continue
            elif e == ERROR_IO_PENDING:
                continue
            else:
                # 句柄读失败（有些通道只能用 0 权限打开），直接结束
                break
        return out, 0
    finally:
        close(h)


def hexs(data, limit=None):
    if data is None:
        return "<无>"
    d = data if limit is None else data[:limit]
    return " ".join("%02X" % b for b in d)


# ---------------------------------------------------------------- 报文描述符
IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0000

kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]


def get_report_descriptor(path, size=8192):
    """读设备的报文描述符（HID Report Descriptor）。

    这是设备自带的一份"说明书"，里面写清了每条报文的编号、长度、
    以及每个字段是干什么用的 —— 比盲试报文高效得多。

    优先走注册表（Windows 会把描述符缓存一份，最可靠），
    拿不到再退回 IOCTL。只读，不改任何东西。
    """
    data = _descriptor_from_registry(path)
    if data:
        return data, 0

    last_err = 0
    for mode in ((True, True), (True, False), (False, False)):
        h, err = open_device(path, read=mode[0], write=mode[1], overlapped=False)
        if not h:
            last_err = err
            continue
        try:
            buf = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(h, IOCTL_HID_GET_REPORT_DESCRIPTOR,
                                          None, 0, buf, size, byref(returned), None)
            if ok and returned.value > 4:
                return buf.raw[:returned.value], 0
            last_err = ctypes.get_last_error()
        finally:
            close(h)
    return None, last_err


def _descriptor_from_registry(interface_path):
    """从注册表取描述符。路径形如
    \\\\?\\hid#vid_3837&pid_3033&mi_02#7&2b4f911bd&0&0000#{接口GUID}
    对应的键是
    HKLM\\SYSTEM\\CurrentControlSet\\Enum\\HID\\VID_3837&PID_3033&MI_02\\7&2b4f911bd&0&0000\\Device Parameters
    """
    import winreg

    body = interface_path
    if body.startswith("\\\\?\\"):
        body = body[4:]
    parts = body.split("#")
    if len(parts) < 3:
        return None
    hardware_id = parts[1].upper()
    instance = parts[2].upper()
    candidates = [
        r"SYSTEM\CurrentControlSet\Enum\HID\%s\%s\Device Parameters" % (hardware_id, instance),
        r"SYSTEM\CurrentControlSet\Enum\HID\%s\%s\Device Parameters" % (
            hardware_id.split("&COL")[0], instance),
    ]
    for key_path in candidates:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                data, _type = winreg.QueryValueEx(k, "ReportDescriptor")
                if data:
                    return bytes(data)
        except OSError:
            continue
    return None




def parse_report_descriptor(data):
    """把描述符解析成 [(报文编号, 类型, 位数, [用途...])]。

    只做够用程度的解析：全局项（用途页、报文大小/数量、报文编号）
    + 主项（Input / Output / Feature）+ 局部项（用途）。
    """
    reports = []
    usage_page = 0
    report_id = 0
    report_size = 0
    report_count = 0
    usages = []
    pending_usage_min = None
    i = 0
    n = len(data)
    while i < n:
        prefix = data[i]
        if prefix == 0xFE:  # 长项目，基本没人用，跳过
            if i + 2 >= n:
                break
            size = data[i + 1]
            i += 3 + size
            continue
        bsize = prefix & 0x03
        if bsize == 3:
            bsize = 4
        btype = (prefix >> 2) & 0x03
        btag = (prefix >> 4) & 0x0F
        val = 0
        for k in range(bsize):
            if i + 1 + k < n:
                val |= data[i + 1 + k] << (8 * k)
        i += 1 + bsize

        if btype == 1:  # 全局项
            if btag == 0x0:
                usage_page = val
            elif btag == 0x7:
                report_size = val
            elif btag == 0x8:
                report_id = val
            elif btag == 0x9:
                report_count = val
            elif btag == 0x1:
                pending_usage_min = val
        elif btype == 2:  # 局部项
            if btag == 0x0:
                usages.append((usage_page, val))
        elif btype == 0:  # 主项
            if btag in (0x8, 0x9, 0xB):  # Input / Output / Feature
                kind = {0x8: "输入", 0x9: "输出", 0xB: "属性"}[btag]
                bits = report_size * report_count
                reports.append((report_id, kind, bits, list(usages), bool(val & 1)))
                usages = []
            elif btag == 0xA:  # Collection
                usages = []
            elif btag == 0xC:  # EndCollection
                usages = []
    return reports

