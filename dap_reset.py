"""通过 CMSIS-DAP v2（WinUSB）给目标发 SYSRESETREQ —— 绕过 uVision 的自动复位。

用法：
    python tools/dap_reset.py            # 复位并运行
    python tools/dap_reset.py --halt     # 复位后停住（调试用）
    python tools/dap_reset.py --info     # 只打印探头信息

依赖：Windows 自带 winusb.dll / setupapi.dll，无需安装任何包。
注意：该 WinUSB 接口必须用 FILE_FLAG_OVERLAPPED 打开，否则 WinUsb_Initialize 报 err=6。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import sys

# ---------------------------------------------------------------- 结构体
class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", w.DWORD), ("Reserved", ctypes.POINTER(w.ULONG))]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_ubyte), ("bDescriptorType", ctypes.c_ubyte),
                ("bInterfaceNumber", ctypes.c_ubyte), ("bAlternateSetting", ctypes.c_ubyte),
                ("bNumEndpoints", ctypes.c_ubyte), ("bInterfaceClass", ctypes.c_ubyte),
                ("bInterfaceSubClass", ctypes.c_ubyte), ("bInterfaceProtocol", ctypes.c_ubyte),
                ("iInterface", ctypes.c_ubyte)]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [("PipeType", ctypes.c_int), ("PipeId", ctypes.c_ubyte),
                ("MaximumPacketSize", w.USHORT), ("Interval", ctypes.c_ubyte)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


CMSIS_DAP_V2_GUID = GUID(0xCDB3B5AD, 0x293B, 0x4663,
                         (ctypes.c_ubyte * 8)(0xAA, 0x36, 0x1A, 0xAE, 0x46, 0x46, 0x37, 0x76))
GENERIC_RW = 0x80000000 | 0x40000000
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
PIPE_TRANSFER_TIMEOUT = 0x03

setupapi = ctypes.WinDLL("setupapi.dll", use_last_error=True)
winusb = ctypes.WinDLL("winusb.dll", use_last_error=True)
k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.POINTER(GUID), w.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                     ctypes.c_void_p, w.DWORD,
                                                     ctypes.POINTER(w.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                            w.DWORD, w.DWORD, ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.CreateEventW.restype = w.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, w.LPCWSTR]
k32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
                                    ctypes.POINTER(w.DWORD), ctypes.c_int]

winusb.WinUsb_Initialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
winusb.WinUsb_Free.argtypes = [ctypes.c_void_p]
winusb.WinUsb_QueryInterfaceSettings.argtypes = [ctypes.c_void_p, ctypes.c_ubyte,
                                                 ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)]
winusb.WinUsb_QueryPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                    ctypes.POINTER(WINUSB_PIPE_INFORMATION)]
winusb.WinUsb_SetPipePolicy.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, w.DWORD, w.DWORD,
                                        ctypes.c_void_p]
winusb.WinUsb_WritePipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
                                    w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
                                   w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]


def open_dap():
    """返回 (dev_handle, usb_handle, out_pipe, in_pipe, path)。"""
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(CMSIS_DAP_V2_GUID), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev:
        raise RuntimeError("找不到 CMSIS-DAP v2 设备")
    try:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(did)
        if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None,
                                                    ctypes.byref(CMSIS_DAP_V2_GUID), 0,
                                                    ctypes.byref(did)):
            raise RuntimeError("没有 CMSIS-DAP v2 接口实例")
        need = w.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0,
                                                  ctypes.byref(need), None)
        buf = ctypes.create_string_buffer(need.value)
        ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8      # x64 下 cbSize=8
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf,
                                                         need.value, None, None):
            raise RuntimeError("取设备路径失败")
        path = ctypes.wstring_at(ctypes.addressof(buf) + ctypes.sizeof(w.DWORD))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)

    h = k32.CreateFileW(path, GENERIC_RW, 0x3, None, 3, FILE_FLAG_OVERLAPPED, None)
    if not h or h == ctypes.c_void_p(-1).value:
        raise RuntimeError(f"打开设备失败 err={ctypes.get_last_error()}：{path}")

    iface = ctypes.c_void_p()
    if not winusb.WinUsb_Initialize(ctypes.c_void_p(h), ctypes.byref(iface)):
        err = ctypes.get_last_error()
        k32.CloseHandle(ctypes.c_void_p(h))
        raise RuntimeError(f"WinUsb_Initialize 失败 err={err}")

    desc = USB_INTERFACE_DESCRIPTOR()
    winusb.WinUsb_QueryInterfaceSettings(iface, 0, ctypes.byref(desc))
    out_pipe = in_pipe = None
    for i in range(desc.bNumEndpoints):
        pi = WINUSB_PIPE_INFORMATION()
        winusb.WinUsb_QueryPipe(iface, 0, i, ctypes.byref(pi))
        if pi.PipeType == 2:              # BULK
            if pi.PipeId & 0x80:
                in_pipe = pi.PipeId
            else:
                out_pipe = pi.PipeId
    if out_pipe is None or in_pipe is None:
        raise RuntimeError("没找到批量端点")

    timeout = w.DWORD(1000)               # 读超时 1s
    for p in (out_pipe, in_pipe):
        winusb.WinUsb_SetPipePolicy(iface, p, PIPE_TRANSFER_TIMEOUT,
                                    ctypes.sizeof(timeout), ctypes.byref(timeout))
    return h, iface, out_pipe, in_pipe, path


def _sync_io(dev, iface, pipe, buf, size, is_read) -> int:
    ov = OVERLAPPED()
    ov.hEvent = k32.CreateEventW(None, 1, 0, None)
    done = w.DWORD()
    fn = winusb.WinUsb_ReadPipe if is_read else winusb.WinUsb_WritePipe
    ok = fn(iface, pipe, buf, size, ctypes.byref(done), ctypes.byref(ov))
    if not ok:
        err = ctypes.get_last_error()
        if err != ERROR_IO_PENDING:
            k32.CloseHandle(ov.hEvent)
            raise RuntimeError(f"{'读' if is_read else '写'}失败 err={err}")
        if not k32.GetOverlappedResult(ctypes.c_void_p(dev), ctypes.byref(ov),
                                       ctypes.byref(done), True):
            err = ctypes.get_last_error()
            k32.CloseHandle(ov.hEvent)
            raise RuntimeError(f"等 IO 完成失败 err={err}")
    k32.CloseHandle(ov.hEvent)
    return done.value


def xfer(dev, iface, out_pipe, in_pipe, payload: bytes, size: int = 64) -> bytes:
    wbuf = (ctypes.c_ubyte * size)()
    wbuf[:len(payload)] = payload
    _sync_io(dev, iface, out_pipe, wbuf, size, False)
    rbuf = (ctypes.c_ubyte * size)()
    n = _sync_io(dev, iface, in_pipe, rbuf, size, True)
    return bytes(rbuf[:n])


def dap_write(dev, iface, op, ip, tbyte: int, value: int) -> bool:
    """DAP_Transfer 写一个 32 位寄存器；tbyte = APnDP|RnW|A[3:2]。"""
    pkt = bytes([0x05, 0x00, 0x01, tbyte,
                 value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF])
    r = xfer(dev, iface, op, ip, pkt)
    return len(r) >= 3 and r[0] == 0x05 and (r[2] & 0x07) == 0x01


def dap_read(dev, iface, op, ip, tbyte: int) -> int | None:
    r = xfer(dev, iface, op, ip, bytes([0x05, 0x00, 0x01, tbyte]))
    if len(r) >= 7 and r[0] == 0x05 and (r[2] & 0x07) == 0x01:
        return int.from_bytes(r[3:7], "little")
    return None


def read_dhcsr(dev, iface, op, ip) -> int | None:
    """读 DHCSR。AP 读有流水线，多读几次取最后一个非零值。"""
    if not dap_write(dev, iface, op, ip, 0x05, 0xE000EDF0):
        return None
    vals = [dap_read(dev, iface, op, ip, 0x0F) for _ in range(4)]
    vals = [v for v in vals if v]
    return vals[-1] if vals else None


def reset_target(dev, iface, op, ip, halt: bool) -> bool:
    """只做两件事：给 DP 上电 + 写 AIRCR 触发复位。

    特意不碰 DHCSR：一旦把内核 halt 住而后续的 run 写入失败，就会永久停住
    （实测 3/3 全挂）。简单序列偶发失败时由上层重试即可。
    """
    dap_write(dev, iface, op, ip, 0x04, 0x50000000)          # CTRL/STAT 上电请求
    dap_write(dev, iface, op, ip, 0x08, 0x00000000)          # SELECT: APSEL=0 bank0
    if not dap_write(dev, iface, op, ip, 0x05, 0xE000ED0C):  # TAR = AIRCR
        return False
    val = 0x05FA0001 if halt else 0x05FA0004                 # VECTRESET / SYSRESETREQ
    return dap_write(dev, iface, op, ip, 0x0D, val)          # DRW = val


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="CMSIS-DAP v2 复位")
    ap.add_argument("--halt", action="store_true", help="用 VECTRESET 后停住")
    ap.add_argument("--info", action="store_true", help="只打印探头信息")
    ap.add_argument("--status", action="store_true",
                    help="读 DHCSR 判断内核在跑还是停住（退出码 0=运行 6=停住）")
    args = ap.parse_args(argv)

    try:
        dev, iface, out_pipe, in_pipe, path = open_dap()
    except RuntimeError as exc:
        print(f"[失败] {exc}")
        return 2
    print(f"[探头] CMSIS-DAP v2  out=0x{out_pipe:02X} in=0x{in_pipe:02X}")

    try:
        r = xfer(dev, iface, out_pipe, in_pipe, bytes([0x00, 0x04]))
        fw = r[2:2 + r[1]].decode("ascii", "replace").rstrip("\x00") if len(r) > 2 and r[1] else "?"
        print(f"[信息] 固件 {fw}")

        r = xfer(dev, iface, out_pipe, in_pipe, bytes([0x02, 0x01]))       # DAP_Connect SWD
        if len(r) < 2 or r[0] != 0x02 or r[1] == 0:
            print(f"[失败] DAP_Connect 返回 {r[:4].hex(' ')}")
            return 3
        xfer(dev, iface, out_pipe, in_pipe, bytes([0x04, 0x00, 0x64, 0x00, 0x00]))

        if args.status:
            d = read_dhcsr(dev, iface, out_pipe, in_pipe)
            if d is None:
                print("[状态] 读 DHCSR 失败")
                return 7
            running = not ((d >> 1) & 1) and not ((d >> 17) & 1)
            print(f"[状态] DHCSR=0x{d:08X}  C_HALT={(d >> 1) & 1}  S_HALT={(d >> 17) & 1}  -> "
                  + ("内核在运行" if running else "内核停住"))
            xfer(dev, iface, out_pipe, in_pipe, bytes([0x03]))
            return 0 if running else 6

        idcode = dap_read(dev, iface, out_pipe, in_pipe, 0x02)             # DP IDCODE
        if idcode is None:
            print("[失败] 读 IDCODE 失败（SWD 没连上目标？）")
            return 3
        print(f"[目标] IDCODE=0x{idcode:08X}")

        ok = reset_target(dev, iface, out_pipe, in_pipe, args.halt)
        if args.info:
            xfer(dev, iface, out_pipe, in_pipe, bytes([0x03]))
            return 0
        print(f"[复位] {'VECTRESET(停住)' if args.halt else 'SYSRESETREQ(复位并运行)'}  "
              f"{'成功' if ok else '失败'}")
        xfer(dev, iface, out_pipe, in_pipe, bytes([0x03]))                 # Disconnect
        return 0 if ok else 4
    except RuntimeError as exc:
        print(f"[失败] {exc}")
        return 5
    finally:
        winusb.WinUsb_Free(iface)
        k32.CloseHandle(ctypes.c_void_p(dev))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
