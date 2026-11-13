#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""md32 工具：扫描/读地址/改地址 + 485 OTA 升级（A 方案，不需要 DAP 探头）

依赖极少：
    必需  pyserial           （pip install pyserial）
    界面  tkinter            （Python 自带；Debian/树莓派缺了装 python3-tk）
    不需要 PySide6 / Qt

协议（与 Aplication/main.c、gd32f30x_it.c 对齐）
    读地址（广播 0xff）
        请求  FE FE 03 FF 00 sum                        6 字节，sum=[0..4] 累加
        响应  EF EF 00 10 FF 00 addr uid[12] sum        20 字节，sum=[0..18] 累加
    改地址
        请求  FE FE 10 FF 01 uid[12] new_addr sum       19 字节，sum=[0..17] 累加
        响应  EF EF 00 05 FF 01 result addr sum         9 字节，result=1 成功
    固件逐字节比对 uid，只有与板上芯片一致才改写；新地址 1~254 且不能等于原值。

用法
    图形界面   python addr_tool.py
               python addr_tool.py --port /dev/ttyUSB0
    命令行     python addr_tool.py --port /dev/ttyUSB0 scan
               python addr_tool.py --port /dev/ttyUSB0 write --uid "3D 12 ... 32" --addr 5

RS485 方向脚（DE/RE）
    默认 BCM 12（= wiringPi 26，即 GPIO12），发送前拉高、发完拉低再收。
    自动挑选后端： wiringpi -> RPi.GPIO -> lgpio ；都没有则按无方向脚运行。
    参数： --de-pin 12（BCM） / --wpi-pin 26 / --de-hold-ms 2 / --no-de

树莓派
    sudo apt install python3-tk          # 只在缺 tkinter 时
    pip install pyserial
    sudo usermod -aG dialout $USER       # 重新登录后串口免 sudo
    串口名一般是 /dev/ttyUSB0 或 /dev/ttyACM0
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None

BAUD = 115200
PAD_BYTE_DEFAULT = False     # 垫字节实测无效，默认关；真因是 flush() 挡住回包
BCAST = 0xFF
CMD_READ = 0x00
CMD_WRITE = 0x01


# ---------------- 组帧 / 解帧 ----------------

def frame_read() -> bytes:
    body = bytes([0xFE, 0xFE, 0x03, BCAST, CMD_READ])
    return body + bytes([sum(body) & 0xFF])


def frame_write(uid: bytes, new_addr: int) -> bytes:
    body = bytes([0xFE, 0xFE, 0x10, BCAST, CMD_WRITE]) + uid + bytes([new_addr])
    return body + bytes([sum(body) & 0xFF])


def split_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    for i in range(len(buf) - 1):
        if buf[i:i + 2] != b"\xEF\xEF":
            continue
        if len(buf) - i < 4:
            return [], buf[i:]
        total = 4 + buf[i + 3]
        if not (5 <= total <= 64):
            continue
        if len(buf) - i < total:
            return [], buf[i:]
        frame = bytes(buf[i:i + total])
        if (sum(frame[:-1]) & 0xFF) == frame[-1]:
            rest, tail = split_frames(buf[i + total:])
            return [frame] + rest, tail
    return [], b""


def parse_read_resp(f: bytes):
    if len(f) == 20 and f[3] == 0x10 and f[5] == CMD_READ:
        return f[6], bytes(f[7:19])
    return None


def parse_write_resp(f: bytes):
    if len(f) == 9 and f[3] == 0x05 and f[5] == CMD_WRITE:
        return f[6], f[7]
    return None


def uid_hex(uid: bytes) -> str:
    return " ".join(f"{b:02X}" for b in uid)


def parse_uid(text: str) -> bytes:
    toks = text.replace(",", " ").split()
    if len(toks) != 12:
        raise ValueError("UID 需要 12 个字节，例如 3D 12 3B CC A4 61 30 58 50 36 31 32")
    return bytes(int(t, 16) for t in toks)


# ---------------- RS485 方向脚（DE/RE） ----------------

class DePin:
    """485 收发方向控制：high=发送，low=接收。

    树莓派上按可用库自动选择后端：
        wiringpi  -> digitalWrite(wpi_pin, 1/0)   与固件端 C++ 代码一致（wPi 26 = BCM 12）
        RPi.GPIO  -> 按 BCM 编号
        lgpio     -> 按 BCM 编号
    其它平台（Windows）自动变成空操作，方便同一份代码两边跑。
    """

    # 40pin 头上常用的 BCM -> wiringPi 编号换算
    WPI_OF_BCM = {
        17: 0, 18: 1, 27: 2, 22: 3, 23: 4, 24: 5, 25: 6, 4: 7,
        14: 8, 15: 9, 5: 21, 6: 22, 13: 23, 19: 24, 26: 25, 12: 26,
        16: 27, 20: 28, 21: 29,
    }

    def __init__(self, bcm: int | None = 12, wpi: int | None = None, enabled: bool = True) -> None:
        self.backend = "none"
        self.bcm = bcm
        self.wpi = wpi if wpi is not None else self.WPI_OF_BCM.get(bcm or -1)
        self._drv = None
        self._initialized = False
        if not enabled or sys.platform.startswith("win"):
            return
        self._init_driver()

    def _init_driver(self) -> None:
        # 1) wiringpi（与 C++ 代码同库，编号直接用 wPi）
        try:
            import wiringpi  # type: ignore
            wiringpi.wiringPiSetup()
            self._drv = wiringpi
            self.backend = f"wiringpi(wPi {self.wpi})"
            self.low()
            self._initialized = True
            return
        except Exception:  # noqa: BLE001
            pass
        # 2) RPi.GPIO / lgpio（BCM 编号）
        for mod_name in ("RPi.GPIO", "lgpio"):
            try:
                mod = __import__(mod_name, fromlist=["x"])
            except Exception:  # noqa: BLE001
                continue
            try:
                if mod_name == "RPi.GPIO":
                    mod.setwarnings(False)          # 重复 setup 时不再刷警告
                    try:
                        mod.setmode(mod.BCM)
                    except Exception:               # 已设置过 BCM
                        pass
                    try:
                        mod.setup(self.bcm, mod.OUT, initial=mod.LOW)
                    except Exception:               # 已被占用就沿用现有配置
                        pass
                else:
                    self._chip = mod.gpiochip_open(0)
                    mod.gpio_claim_output(self._chip, self.bcm, 0)
                self._drv = mod
                self.backend = f"{mod_name}(BCM {self.bcm})"
                self._initialized = True
                return
            except Exception:  # noqa: BLE001
                continue
        self.backend = "none"

    def high(self) -> None:
        self._write(1)

    def low(self) -> None:
        self._write(0)

    def _write(self, level: int) -> None:
        if not self._initialized:
            return
        try:
            if self.backend.startswith("wiringpi"):
                self._drv.digitalWrite(self.wpi, 1 if level else 0)
            elif self.backend.startswith("RPi.GPIO"):
                self._drv.output(self.bcm, self._drv.HIGH if level else self._drv.LOW)
            else:
                self._drv.gpio_write(self._chip, self.bcm, level)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if not self._initialized:
            return
        try:
            self.low()
        finally:
            self._initialized = False


# ---------------- 串口 ----------------

class Link:
    def __init__(self, port: str, baud: int = BAUD, de: "DePin | None" = None,
                 de_hold_ms: float = 0.5, de_settle_ms: float = 2.0) -> None:
        self.port = port
        self.baud = baud
        self.de = de
        self.de_hold_ms = de_hold_ms
        self.de_settle_ms = de_settle_ms
        self.pad_byte = PAD_BYTE_DEFAULT
        self.ser = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None

    def open(self) -> None:
        self.close()
        self.ser = serial.serial_for_url(self.port, self.baud, timeout=0.05, write_timeout=1)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    def _pump(self, deadline: float, out: list[bytes]) -> None:
        # 带超时的 read() 累积，不依赖 in_waiting（虚拟端口上它不可靠）
        buf = b""
        while time.monotonic() < deadline:
            chunk = self.ser.read(64)
            if chunk:
                buf += chunk
                frames, buf = split_frames(buf)
                out.extend(frames)

    def _send(self, frame: bytes) -> None:
        """发一帧；有方向脚时先抬 DE，发完等最后一个字节移出再拉低。"""
        if self.ser is None:
            return
        if self.de:
            self.de.low()                       # 先确保处于接收态再清缓冲
        self.ser.reset_input_buffer()
        if self.de:
            self.de.high()
            time.sleep(self.de_settle_ms / 1000.0)   # 抬高后先稳定，再发
        data = (b"\x00" if self.pad_byte else b"") + frame
        self.ser.write(data)
        if self.de:
            # 关键：绝对不要调用 self.ser.flush()！
            # pyserial 的 flush() = tcdrain()，在 Pi 的 PL011 上固定耗时约 11ms，
            # 与帧长无关。短帧时这 11ms 纯属浪费，DE 会一直保持高电平，
            # 把设备 2ms 后就开始发的应答整个挡掉（表现为"一个字节都收不到"）。
            # 改为按实际字节数精确等待最后一位移出，再加一点硬件余量。
            tx_s = len(data) * 10.0 / self.baud
            time.sleep(tx_s + self.de_hold_ms / 1000.0)
            self.de.low()

    def read_addr(self, timeout: float = 0.5):
        self._send(frame_read())
        frames: list[bytes] = []
        self._pump(time.monotonic() + timeout, frames)
        for f in frames:
            r = parse_read_resp(f)
            if r:
                return r
        return None

    def scan(self, seconds: float = 3.0) -> dict[str, int]:
        """广播读地址，收集总线上所有应答的设备 {uid_hex: addr}。

        多片同时在线会撞帧，所以按短间隔反复发，靠多轮重试凑齐。
        """
        found: dict[str, int] = {}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._send(frame_read())
            frames: list[bytes] = []
            self._pump(min(deadline, time.monotonic() + 0.12), frames)
            for f in frames:
                r = parse_read_resp(f)
                if r:
                    found[uid_hex(r[1])] = r[0]
            time.sleep(0.02)
        return found

    def write_addr(self, uid: bytes, new_addr: int, timeout: float = 0.6):
        self._send(frame_write(uid, new_addr))
        frames: list[bytes] = []
        self._pump(time.monotonic() + timeout, frames)
        for f in frames:
            r = parse_write_resp(f)
            if r:
                return r
        return None


def list_ports() -> list[str]:
    if serial is None:
        return []
    return [p.device for p in serial.tools.list_ports.comports()]



# ---------------- 串口 ISP 烧录（GD32F303 片内 Bootloader） ----------------

DEFAULT_FROMELF = Path(r"D://keil//Keil_v5//ARM//ARMCC//bin//fromelf.exe")
ISP_ACK = 0x79
ISP_NACK = 0x1F
ISP_GET = 0x00
ISP_GET_ID = 0x02
ISP_READ = 0x11
ISP_GO = 0x21
ISP_WRITE = 0x31
ISP_ERASE = 0x43
FLASH_BASE = 0x08000000


def make_bin(axf: Path, fromelf: Path = DEFAULT_FROMELF) -> Path:
    """用 Keil 的 fromelf 把 axf 转成 bin。"""
    if not axf.exists():
        raise SystemExit(f"[错误] 找不到 axf：{axf}")
    if not fromelf.exists():
        raise SystemExit(f"[错误] 找不到 fromelf：{fromelf}")
    out = axf.with_suffix(".bin")
    r = subprocess.run([str(fromelf), "--bin", "-o", str(out), str(axf)],
                       capture_output=True, text=True, errors="replace")
    if not out.exists():
        raise SystemExit("[错误] 生成 bin 失败：\n" + (r.stdout or "") + (r.stderr or ""))
    return out


class Isp:
    """ST/GD 兼容 ISP 客户端；只负责协议，不关心界面。"""

    def __init__(self, port: str, baud: int = BAUD, de=None,
                 de_settle_ms: float = 2.0, de_hold_ms: float = 0.5) -> None:
        self.ser = serial.serial_for_url(port, baud, timeout=0.05, write_timeout=2)
        self.de = de
        self.de_settle_ms = de_settle_ms
        self.de_hold_ms = de_hold_ms
        self.log = print
        self.debug = False

    def close(self) -> None:
        try:
            self.ser.close()
        finally:
            if self.de:
                self.de.close()

    # --- 底层 ---
    def _write(self, data: bytes) -> None:
        if self.de:
            self.de.high()
            time.sleep(self.de_settle_ms / 1000.0)
        self.ser.write(data)
        if self.de:
            # 同 Link._send：不用 flush()，按帧长精确等待，避免挡住设备应答
            time.sleep(len(data) * 10.0 / self.baud + self.de_hold_ms / 1000.0)
            self.de.low()

    def _read(self, n: int, timeout: float = 1.0) -> bytes:
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < n and time.monotonic() < deadline:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return buf

    def _ack(self, what: str, timeout: float = 1.0) -> bool:
        b = self._read(1, timeout)
        if b and b[0] == ISP_ACK:
            return True
        got = f"0x{b[0]:02X}" if b else "无应答"
        self.log(f"  [ISP_NACK] {what}：{got}")
        return False

    # --- 命令 ---
    def sync(self, tries: int = 10) -> bool:
        for _ in range(tries):
            self.ser.reset_input_buffer()
            self._write(b"\x7F")
            if self._ack("同步", 0.4):
                return True
        return False

    def get_id(self) -> int | None:
        self._write(bytes([ISP_GET_ID, (~ISP_GET_ID) & 0xFF]))
        if not self._ack("GET_ID"):
            return None
        n = self._read(1, 1.0)
        if not n:
            return None
        pid = self._read(n[0] + 1, 1.0)
        if not self._ack("GET_ID 尾"):
            return None
        return (pid[0] << 8) | pid[1] if len(pid) >= 2 else None

    def erase_all(self, timeout: float = 30.0) -> bool:
        self._write(bytes([ISP_ERASE, (~ISP_ERASE) & 0xFF]))
        if not self._ack("ERASE"):
            return False
        self._write(b"\xFF\x00")
        return self._ack("ERASE 确认", timeout)

    def write_block(self, addr: int, data: bytes) -> bool:
        if not (0 < len(data) <= 256):
            return False
        a = addr.to_bytes(4, "big")
        chk = a[0] ^ a[1] ^ a[2] ^ a[3] ^ (len(data) - 1)
        self._write(bytes([ISP_WRITE, (~ISP_WRITE) & 0xFF]))
        if not self._ack("WRITE", 2.0):
            return False
        self._write(a + bytes([chk]))
        if not self._ack("WRITE 地址", 2.0):
            return False
        c = len(data) - 1
        for b in data:
            c ^= b
        self._write(bytes([len(data) - 1]) + data + bytes([c & 0xFF]))
        return self._ack("WRITE 数据", 5.0)

    def read_block(self, addr: int, n: int) -> bytes | None:
        a = addr.to_bytes(4, "big")
        chk = a[0] ^ a[1] ^ a[2] ^ a[3]
        self._write(bytes([ISP_READ, (~ISP_READ) & 0xFF]))
        if not self._ack("READ"):
            return None
        self._write(a + bytes([chk]))
        if not self._ack("READ 地址", 2.0):
            return None
        m = self._read(1, 2.0)
        if not m:
            return None
        data = self._read(m[0] + 1, 3.0)
        self._ack("READ 数据", 2.0)
        return data

    def go(self, addr: int = FLASH_BASE) -> bool:
        a = addr.to_bytes(4, "big")
        chk = a[0] ^ a[1] ^ a[2] ^ a[3]
        self._write(bytes([ISP_GO, (~ISP_GO) & 0xFF]))
        if not self._ack("GO", 2.0):
            return False
        self._write(a + bytes([chk]))
        return self._ack("GO 确认", 2.0)


def read_uid_app(port: str, de=None, baud: int = BAUD,
                 de_hold_ms: float = 0.5, de_settle_ms: float = 2.0,
                 timeout: float = 0.8):
    """在应用态用 EF 协议读一次 (地址, UID)。返回 (addr, uid_bytes) 或 None。"""
    link = Link(port, baud, de, de_hold_ms, de_settle_ms)
    link.open()
    try:
        return link.read_addr(timeout)
    finally:
        link.close()


def flash_firmware(port: str, payload: bytes, base: int, *, baud: int = BAUD,
                   de=None, chunk: int = 256, verify: bool = True,
                   do_go: bool = True, erase: bool = True,
                   log=print, progress=None) -> int:
    """烧录主流程。返回 0 成功，非 0 为失败阶段（3 同步/4 擦除/5 写/6 校验/7 跳转）。"""
    isp = Isp(port, baud, de)
    isp.log = log
    try:
        log("[同步] 握手…")
        if not isp.sync():
            log("[同步] 失败：确认 BOOT0 已拉高、串口是 USART0、波特率正确")
            return 3
        pid = isp.get_id()
        if pid is None:
            log("[错误] 读不到芯片 ID")
            return 3
        log(f"[芯片] PID = 0x{pid:04X}")
        if erase:
            log("[擦除] 全片擦除…（会清掉参数区：地址 / xishu / chanshu）")
            if not isp.erase_all():
                return 4
        else:
            log("[擦除] 已跳过（不擦除）——目标区已有数据时写入会失败或校验不一致")
        total = len(payload)
        chunk = max(1, min(256, chunk))
        t0 = time.monotonic()
        for off in range(0, total, chunk):
            block = payload[off:off + chunk]
            if not isp.write_block(base + off, block):
                log(f"[失败] 写 0x{base + off:08X} 被拒")
                return 5
            if progress:
                progress((off + len(block)) * 100 // total)
        log(f"[写入] 完成 {total} 字节，用时 {time.monotonic() - t0:.1f}s")
        if verify:
            log("[校验] 回读比对…")
            bad = 0
            for off in range(0, total, chunk):
                block = payload[off:off + chunk]
                if isp.read_block(base + off, len(block)) != block:
                    bad += 1
            if bad:
                log(f"[校验] {bad} 块不一致")
                return 6
            log("[校验] 全部一致")
        if do_go:
            if not isp.go(base):
                log("[启动] 跳转失败，按复位键手动启动")
                return 7
            log("[完成] 已跳转到应用；记得把 BOOT0 恢复拉低")
        return 0
    finally:
        isp.close()


# ---------------- 485 IAP 升级 ----------------

APP_BASE = 0x08000000
UPDATER_ADDR = 0x0803D800
# 旧方案 IAP 的命令字：必须加前缀！否则会覆盖上面地址协议的 CMD_WRITE(0x01)，
# 导致「改地址」发出 cmd=0x03，设备不认，表现为"写地址无应答"。
IAP_CMD_PING, IAP_CMD_ERASE, IAP_CMD_WRITE, IAP_CMD_FINISH = 0x01, 0x02, 0x03, 0x04
CMD_ENTER_UPDATE = 0x05

STATUS_TEXT = {0: "OK", 1: "参数错", 3: "擦/写失败", 4: "CRC 不一致", 9: "校验和错"}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build(addr: int, cmd: int, payload: bytes = b"") -> bytes:
    # len 字段包含 addr + cmd + payload + 校验和，所以是 3 + payload
    ln = 3 + len(payload)
    if ln > 255:
        raise ValueError(f"单帧过长：payload {len(payload)} 字节，上限 252（数据块最多 248）")
    body = bytes([0xFE, 0xFE, ln, addr & 0xFF, cmd]) + payload
    return body + bytes([sum(body) & 0xFF])


def parse(buf: bytes):
    """从缓冲里切出应答帧，返回 [(cmd, status, payload), ...] 和剩余缓冲。"""
    out = []
    i = 0
    while True:
        j = buf.find(b"\xFE\xFE", i)
        if j < 0 or len(buf) - j < 3:
            return out, buf[i:] if i else buf
        total = buf[j + 2] + 3
        if len(buf) - j < total:
            return out, buf[j:]
        f = bytes(buf[j:j + total])
        if sum(f[:-1]) & 0xFF != f[-1]:
            i = j + 2
            continue
        out.append(((f[4] & 0x7F), f[5] if total > 6 else 0, f[6:-1]))
        i = j + total


def _retry(fn, tries: int, log=None, what: str = ""):
    """反复调用 fn() 直到返回非 None / 真值，最多 tries 次。"""
    for k in range(tries):
        r = fn()
        if r:
            return r
        if log and (k + 1) % 3 == 0:
            log(f"    [重试] {what} 第 {k + 1}/{tries} 次仍失败…")
    return None


class Iap:
    def __init__(self, port: str, de=None, baud: int = 115200,
                 de_hold_ms: float = 0.5, de_settle_ms: float = 2.0) -> None:
        self.link = Link(port, baud, de, de_hold_ms, de_settle_ms)
        self.link.open()
        self.log = print
        self.debug = False
        self._last_raw = b""

    def close(self) -> None:
        self.link.close()

    # --- 底层 ---
    def _send(self, frame: bytes) -> None:
        self.link._send(frame)

    def _recv(self, timeout: float) -> list:
        buf = b""
        raw = b""
        deadline = time.monotonic() + timeout
        out = []
        while time.monotonic() < deadline:
            chunk = self.link.ser.read(256)
            if chunk:
                raw += chunk
                buf += chunk
                got, buf = parse(buf)
                out.extend(got)
                if out:
                    break
        self._last_raw = raw
        return out

    def cmd(self, addr: int, cmd: int, payload: bytes = b"", timeout: float = 2.0,
            retries: int = 6):
        for k in range(retries):
            frame = build(addr, cmd, payload)
            if self.debug:
                head = " ".join(f"{b:02X}" for b in frame[:12])
                self.log(f"    TX({len(frame)}B) {head}{' …' if len(frame) > 12 else ''}")
            self._send(frame)
            got = self._recv(timeout)
            if self.debug:
                raw = getattr(self, "_last_raw", b"")
                self.log("    RX " + (" ".join(f"{b:02X}" for b in raw[:20]) if raw else "无"))
            for c, st, extra in got:
                if c == cmd:
                    return st, extra
            self.log(f"  [重试] cmd 0x{cmd:02X} 第 {k + 1} 次无应答")
            time.sleep(0.05)
        return None, b""

    # --- 应用态 ---
    def read_addr(self, timeout: float = 0.8):
        return self.link.read_addr(timeout)

    def enter_update(self, addr: int, retries: int = 3) -> bool:
        """给应用发 0x05，等它的 EF 应答（EF EF 00 05 addr 05 status sum）。

        设备每秒会主动发一帧温度，若正好撞上，命令会丢——所以重试几次，
        并把收到的原始字节打出来，便于判断是“没回”还是“回的格式不对”。
        """
        for k in range(retries):
            time.sleep(0.15)                 # 避开正在发送的温度帧
            f = bytes([0xFE, 0xFE, 0x03, addr, CMD_ENTER_UPDATE])
            self._send(f + bytes([sum(f) & 0xFF]))
            buf = b""
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                chunk = self.link.ser.read(64)
                if chunk:
                    buf += chunk
                    j = buf.find(b"\xEF\xEF")
                    if j >= 0 and len(buf) - j >= 8:
                        frame = buf[j:j + 8]     # EF EF 00 05 addr 05 status sum
                        return frame[6] == 0x01
            got = " ".join(f"{b:02X}" for b in buf[:24]) if buf else "无"
            self.log(f"  [进入升级] 第 {k + 1} 次无有效应答，收到：{got}")
        return False

    # --- 升级器态 ---
    def ping(self, addr: int = 0xFF, timeout: float = 1.0, tries: int = 10):
        for _ in range(tries):
            st, extra = self.cmd(addr, IAP_CMD_PING, b"", timeout, retries=1)
            if st == 0:
                ver = extra[0] if extra else 0
                self.log(f"  [PING] 升级器在线，版本 {ver >> 4}.{ver & 0xF}")
                return True
            time.sleep(0.2)
        return False


# 单帧上限：len 只有 1 字节 → len = 3 + payload ≤ 255 → payload ≤ 252
# 而 0x03/0x07 的 payload 里有 4 字节地址，所以数据块最多 248 字节
MAX_CHUNK = 248


def iap_upload(port: str, payload: bytes, *, addr: int | None = None, de=None,
               chunk: int = 240, already_in_boot: bool = False,
               log=print, progress=None, baud: int = 115200,
               de_hold_ms: float = 0.5, de_settle_ms: float = 2.0) -> int:
    """返回 0 成功；1 找不到应用/地址；2 进不了升级器；3 擦除失败；4 写入失败；5 校验失败"""
    if Link is None:
        log("缺少依赖：addr_tool.py 不在同一目录")
        return 1

    chunk = max(16, min(int(chunk), MAX_CHUNK))
    iap = Iap(port, de, baud, de_hold_ms, de_settle_ms)
    iap.log = log
    try:
        if not already_in_boot:
            log("[准备] 应用态读取地址…")
            r = iap.read_addr(1.5)
            if r is None:
                log("[准备] 读不到应用应答。若板子已在升级器里，加 --already-in-boot")
                return 1
            cur, uid = r
            log(f"[准备] 设备地址 {cur}，UID {' '.join(f'{b:02X}' for b in uid)}")
            use_addr = cur if addr is None else addr
            log(f"[准备] 通知设备进入升级（cmd 0x05）…")
            if not iap.enter_update(use_addr):
                # 可能 ACK 丢了但设备已经跳过去，先探一下升级器再决定
                log("[准备] 没收到确认，先探测升级器（可能 ACK 丢了但已经跳过去）…")
                if not iap.ping(0xFF, timeout=0.8, tries=4):
                    log("[准备] 升级器也没有响应：确认固件含 0x05 分支、且已烧过 updater.bin")
                    return 2
                log("[准备] 升级器在线，继续")
            time.sleep(0.35)                 # 等它跳过去（若已在线也无害）
        else:
            use_addr = 0xFF if addr is None else addr

        log("[升级] 等待升级器响应…")
        if not iap.ping(0xFF):
            log("[升级] 升级器无响应：确认已烧过 updater.bin（0x0803D800）")
            return 2

        total = len(payload)
        page = 0x800
        erase_len = (total + page - 1) // page * page
        log(f"[擦除] 0x{APP_BASE:08X} .. 0x{APP_BASE + erase_len - 1:08X}（{erase_len} 字节）")
        st, _ = iap.cmd(0xFF, IAP_CMD_ERASE,
                        APP_BASE.to_bytes(4, "big") + erase_len.to_bytes(4, "big"),
                        timeout=20.0, retries=2)
        if st != 0:
            log(f"[擦除] 失败：{STATUS_TEXT.get(st, st)}")
            return 3

        t0 = time.monotonic()
        for off in range(0, total, chunk):
            block = payload[off:off + chunk]
            st, _ = iap.cmd(0xFF, IAP_CMD_WRITE,
                            (APP_BASE + off).to_bytes(4, "big") + block,
                            timeout=3.0, retries=3)
            if st != 0:
                log(f"\n[写入] 0x{APP_BASE + off:08X} 失败：{STATUS_TEXT.get(st, st)}")
                return 4
            if progress:
                progress((off + len(block)) * 100 // total)
        log(f"[写入] {total} 字节完成，用时 {time.monotonic() - t0:.1f}s")

        crc = crc16_modbus(payload)
        log(f"[校验] 整片 CRC16 = 0x{crc:04X}，交给升级器复核…")
        st, _ = iap.cmd(0xFF, IAP_CMD_FINISH,
                        total.to_bytes(4, "big") + crc.to_bytes(2, "big"),
                        timeout=5.0, retries=2)
        if st != 0:
            log(f"[校验] 未通过：{STATUS_TEXT.get(st, st)}")
            return 5
        log("[完成] 校验通过，设备已跳回应用")
        return 0
    finally:
        iap.close()



def install_updater(port: str, payload: bytes, *, de=None, chunk: int = 240,
                    log=print, progress=None, de_hold_ms: float = 0.5,
                    de_settle_ms: float = 2.0, verify: bool = True,
                    debug: bool = False) -> int:
    """把升级器写进 0x0803D800（应用态，走 0x06 擦 / 0x07 写）。

    返回 0 成功；1 读不到应用；2 镜像超 8KB；3 擦除失败；4 写入失败；5 装完 PING 不通
    """
    if len(payload) > 0x2000:
        log(f"[错误] 镜像 {len(payload)} 字节，超过升级器区 8KB")
        return 2
    iap = Iap(port, de, BAUD, de_hold_ms, de_settle_ms)
    iap.log = log
    iap.debug = debug
    try:
        r = _retry(lambda: iap.read_addr(1.2), 8, log, "读地址")
        if r is None:
            log("[准备] 应用无应答（链路丢包严重或板子未运行）")
            return 1
        addr, uid = r
        log(f"[准备] 设备地址 {addr}，UID {' '.join(f'{b:02X}' for b in uid)}")

        log(f"[擦除] 0x{UPDATER_ADDR:08X} .. 0x{UPDATER_ADDR + 0x2000 - 1:08X}（8KB）")

        def do_erase():
            st, _ = iap.cmd(addr, 0x06,
                            UPDATER_ADDR.to_bytes(4, "big") + (0x2000).to_bytes(4, "big"),
                            timeout=2.0, retries=1)
            return st == 0
        if not _retry(do_erase, 12, log, "擦除"):
            log("[擦除] 多次重试仍失败（链路丢包严重）")
            return 3

        # 应用侧 ISR 限单帧 64 字节；0x07 帧总长 = 10 + 数据，所以每包最多 54 字节。
        # 若首包失败，自动降到 32 / 16 / 8 再试（有些老固件只认更短的帧）。
        total = len(payload)
        sizes = [max(8, min(int(chunk), 54))] + [32, 16, 8]
        ok_chunk = None
        for size in sizes:
            if size > total:
                continue
            first = payload[:size]

            def do_first(_b=first):
                st, _ = iap.cmd(addr, 0x07, UPDATER_ADDR.to_bytes(4, "big") + _b,
                                timeout=0.8, retries=1)
                return st == 0
            if _retry(do_first, 10, log, "首包"):
                ok_chunk = size
                log(f"[写入] 每包 {size} 字节")
                written = size
                if progress:
                    progress(written * 100 // total)
                break
            log(f"[写入] 每包 {size} 字节多次重试仍失败，换更小的块…")
        if ok_chunk is None:
            log("[写入] 所有块大小都失败，0x07 命令没有应答")
            return 4

        for off in range(written, total, ok_chunk):
            block = payload[off:off + ok_chunk]
            def do_block(_o=off, _b=block):
                st, _ = iap.cmd(addr, 0x07,
                                (UPDATER_ADDR + _o).to_bytes(4, "big") + _b,
                                timeout=0.6, retries=1)
                return st == 0
            if not _retry(do_block, 15, log, f"0x{UPDATER_ADDR + off:08X}"):
                log(f"\n[写入] 0x{UPDATER_ADDR + off:08X} 多次重试仍失败")
                return 4
            if progress:
                progress((off + len(block)) * 100 // total)
        log(f"[写入] 升级器 {total} 字节完成")

        if verify:
            log("[验证] 让设备跳进升级器并握手…")
            if not iap.enter_update(addr):
                log("[验证] 应用没有确认进入升级（可能 ACK 丢了），直接探升级器")
            time.sleep(0.35)
            if not iap.ping(0xFF, timeout=1.0, tries=6):
                log("[验证] 升级器没响应，安装可能失败")
                return 5
            log("[验证] 升级器在线，安装成功")
        return 0
    finally:
        iap.close()



# ---------------- A 方案：0 区 Bootloader OTA 协议 ----------------
OTA_BLOCK = 240            # 与 boot.c 的 seq = off/240 约定一致

def ota_upload(port: str, payload: bytes, *, de=None, version: int = 1,
               log=print, progress=None, already_in_boot: bool = False,
               resume: bool = False, target_uid: str | None = None,
               de_hold_ms: float = 0.5, de_settle_ms: float = 2.0,
               max_retry: int = 12) -> int:
    """A 方案 OTA：新固件写 BACKUP，Boot 校验后复制到 APP。

    返回 0 成功；1 进不了 Boot；2 BEGIN 失败；3 数据块失败；4 END 校验失败；
          5 = UID 过滤未命中（未发送任何升级命令）。
    """
    total = len(payload)
    crc = crc16_modbus(payload)
    iap = Iap(port, de, BAUD, de_hold_ms, de_settle_ms)
    iap.log = log
    try:
        # ① 让应用写「请求升级」标志并复位，Boot 上电后留在升级模式
        if not already_in_boot:
            addr = None
            if target_uid:
                # ID 过滤：先扫描总线，找到目标 UID 对应的设备地址。
                # 广播读地址在多片时会撞帧，所以多轮扫描凑齐；找不到就中止，
                # 一个升级命令都不发，避免误升级别的板子。
                try:
                    want = uid_hex(parse_uid(target_uid))
                except ValueError as e:
                    log(f"[过滤] UID 格式错：{e}")
                    return 5
                found: dict[str, int] = {}
                for _ in range(3):
                    found.update(iap.link.scan(1.2))
                    if want in found:
                        break
                if want not in found:
                    others = ", ".join(found) if found else "（总线上无设备应答）"
                    log(f"[过滤] 未找到 UID {want}，已中止，未发送任何升级命令")
                    log(f"[过滤] 总线上实际是：{others}")
                    return 5
                addr = found[want]
                log(f"[过滤] 命中 {want} → 设备地址 {addr}")
            else:
                r = _retry(lambda: iap.read_addr(1.0), 6, log, "读应用地址")
                if r:
                    addr = r[0]
            if addr is not None:
                f = bytes([0xFE, 0xFE, 0x03, addr, 0x05])
                log(f"[准备] 只通知地址 {addr} 进入 Boot…")
                iap._send(f + bytes([sum(f) & 0xFF]))
            else:
                log("[准备] 应用没应答，尝试直接探 Boot（可能已在升级模式）")
            time.sleep(1.2)
        elif target_uid:
            log("[过滤] 设备已在 Boot，Boot 不返回 UID，本次无法按 UID 过滤")

        # ② PING Boot，取回状态与断点
        info = None
        for _ in range(8):
            st, extra = iap.cmd(0xFF, 0x01, b"", timeout=0.8, retries=1)
            if st == 0 and len(extra) >= 10:
                info = extra
                break
            time.sleep(0.3)
        if info is None:
            log("[握手] Boot 无响应：确认 boot.bin 已烧到 0x08000000")
            return 1
        state = int.from_bytes(info[2:6], "big")
        received = int.from_bytes(info[6:10], "big")
        log(f"[握手] Boot 版本 {info[0]}.{info[1]}  状态={state} 已收={received}")

        # ③ BEGIN；若上次升级中断且用户要续传，则跳过 BEGIN 直接从断点继续
        start = 0
        can_resume = (resume and state == 1 and 0 < received <= total
                      and received % OTA_BLOCK == 0)
        if can_resume:
            start = received
            log(f"[续传] 从 {start} 字节继续（跳过 BEGIN，保留 BACKUP 已有数据）")
        else:
            body = total.to_bytes(4, "big") + crc.to_bytes(2, "big") + version.to_bytes(4, "big")
            def do_begin():
                st, _ = iap.cmd(0xFF, 0x11, body, timeout=6.0, retries=1)
                return st == 0
            if not _retry(do_begin, max_retry, log, "BEGIN"):
                log("[开始] BEGIN 失败")
                return 2
            log(f"[开始] 擦 BACKUP 完成，镜像 {total} 字节 CRC16=0x{crc:04X}")

        # ④ DATA：固定 240 字节/包，带序号和每包 CRC16
        for off in range(start, total, OTA_BLOCK):
            block = payload[off:off + OTA_BLOCK]
            seq = off // OTA_BLOCK
            data = (off.to_bytes(4, "big") + seq.to_bytes(2, "big") + block
                    + crc16_modbus(block).to_bytes(2, "big"))
            def do_data(_d=data):
                st, _ = iap.cmd(0xFF, 0x12, _d, timeout=1.2, retries=1)
                return st == 0
            if not _retry(do_data, max_retry, log, f"块 {seq}"):
                log(f"\n[数据] 块 {seq} @0x{off:06X} 失败")
                return 3
            if progress:
                progress((off + len(block)) * 100 // total)
        log(f"[数据] {total} 字节发送完成")

        # ⑤ END：Boot 校验 BACKUP 后置 COPY_PENDING 并复位
        st, _ = iap.cmd(0xFF, 0x13, b"", timeout=8.0, retries=max_retry)
        if st != 0:
            log(f"[结束] 校验/切换失败：{STATUS_TEXT.get(st, st)}")
            return 4
        log("[结束] Boot 校验通过，正在复位并复制 BACKUP→APP（约数秒）")
        return 0
    finally:
        iap.close()


# ---------------- CLI ----------------

def ota_cli(args) -> int:
    if args.bin:
        path = Path(args.bin)
    elif args.axf:
        path = make_bin(Path(args.axf), Path(args.fromelf))
    else:
        print("需要 --bin 或 --axf")
        return 2
    if not path.exists():
        print(f"[错误] 找不到 {path}")
        return 2
    payload = path.read_bytes()
    print(f"[镜像] {path.name}  {len(payload)} 字节")
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    if de.backend != "none":
        print(f"[485] 方向脚：{de.backend}")
    rc = ota_upload(args.port, payload, de=de,
                    already_in_boot=args.already_in_boot,
                    resume=getattr(args, "resume", False),
                    target_uid=args.uid,
                    de_hold_ms=args.de_hold_ms, de_settle_ms=args.de_settle_ms)
    print({0: "成功", 1: "进不了 Boot", 2: "BEGIN 失败", 3: "数据块失败",
           4: "END 校验失败", 5: "UID 过滤未命中"}.get(rc, rc))
    return rc


def raw_cli(args) -> int:
    """发自定义帧（自动补校验和），打印原始回包——排查用。"""
    if not args.hex:
        print('用法：raw --hex "FE FE 03 02 05"（不用写校验和，会自动补）')
        return 2
    body = bytes.fromhex(args.hex.replace(" ", ""))
    frame = body + bytes([sum(body) & 0xFF])
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    link = Link(args.port, args.baud, de, args.de_hold_ms, args.de_settle_ms)
    link.open()
    try:
        print("TX:", " ".join(f"{b:02X}" for b in frame))
        link._send(frame)
        time.sleep(args.wait)
        buf = b""
        deadline = time.monotonic() + args.wait + 0.2
        while time.monotonic() < deadline:
            chunk = link.ser.read(256)
            if chunk:
                buf += chunk
        print("RX:", " ".join(f"{b:02X}" for b in buf) if buf else "（无）")
        return 0
    finally:
        link.close()


def install_updater_cli(args) -> int:
    if args.bin:
        path = Path(args.bin)
    elif args.axf:
        path = make_bin(Path(args.axf), Path(args.fromelf))
    else:
        print("需要 --bin（升级器 bin，通常是 bootloader/build/updater.bin）")
        return 2
    if not path.exists():
        print(f"[错误] 找不到 {path}")
        return 2
    payload = path.read_bytes()
    print(f"[镜像] {path.name}  {len(payload)} 字节 → 0x{UPDATER_ADDR:08X}")
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    if de.backend != "none":
        print(f"[485] 方向脚：{de.backend}")
    rc = install_updater(args.port, payload, de=de, chunk=args.chunk,
                         de_hold_ms=args.de_hold_ms, de_settle_ms=args.de_settle_ms,
                         debug=getattr(args, "debug", False))
    print({0: "成功", 1: "读不到应用", 2: "镜像超 8KB", 3: "擦除失败",
           4: "写入失败", 5: "装完 PING 不通"}.get(rc, rc))
    return rc


def iap_cli(args) -> int:
    if args.bin:
        path = Path(args.bin)
        if not path.exists():
            print(f"[错误] 找不到 {path}")
            return 2
    elif args.axf:
        path = make_bin(Path(args.axf), Path(args.fromelf))
    else:
        print("需要 --bin 或 --axf")
        return 2
    payload = path.read_bytes()
    print(f"[镜像] {path.name}  {len(payload)} 字节")
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    if de.backend != "none":
        print(f"[485] 方向脚：{de.backend}")
    print("[提示] iap 是旧方案协议，已自动改用 A 方案 ota（0 区 Boot）")
    rc = ota_upload(args.port, payload, de=de,
                    already_in_boot=args.already_in_boot,
                    de_hold_ms=args.de_hold_ms, de_settle_ms=args.de_settle_ms)
    print({0: "成功", 1: "进不了 Boot", 2: "BEGIN 失败", 3: "数据块失败",
           4: "END 校验失败", 5: "UID 过滤未命中"}.get(rc, rc))
    return rc


def flash_cli(args) -> int:
    if serial is None:
        print("未安装 pyserial：pip install pyserial")
        return 2
    if not args.port:
        print("可用串口：", ", ".join(list_ports()) or "（无）")
        print("用 --port 指定")
        return 2
    if args.bin:
        path = Path(args.bin)
        if not path.exists():
            print(f"[错误] 找不到 {path}")
            return 2
    elif args.axf:
        path = make_bin(Path(args.axf), Path(args.fromelf))
    else:
        print("需要 --axf 或 --bin")
        return 2
    payload = path.read_bytes()
    base = int(args.base, 0)
    print(f"[镜像] {path.name}  {len(payload)} 字节 → 0x{base:08X}")
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    if de.backend != "none":
        print(f"[485] 方向脚：{de.backend}")

    # 只烧指定 UID：先在应用态核对身份，再让用户切到 Bootloader
    if args.uid:
        want = parse_uid(args.uid)
        print("[核对] 应用态读取芯片 UID…")
        got = read_uid_app(args.port, de, args.baud, args.de_hold_ms, args.de_settle_ms)
        if got is None:
            print("[核对] 读不到应答：板子可能已在 Bootloader 模式，或程序没在跑")
            print("       先让它按正常方式启动（BOOT0 低）再试，或去掉 --uid")
            return 3
        addr, uid = got
        print(f"[核对] 板上 UID = {uid_hex(uid)}  当前地址 {addr}")
        if uid != want:
            print(f"[核对] 与指定 UID 不符（{args.uid.strip()}），已中止，未做任何擦写")
            return 8
        print("[核对] UID 一致")
        if sys.stdin.isatty():
            input(">>> 现在把 BOOT0 拉高并复位，进入 Bootloader，然后按回车继续…")
        else:
            print("[核对] 非交互模式，跳过等待：请确认已进入 Bootloader")
    if args.probe:
        isp = Isp(args.port, args.baud, de, args.de_settle_ms, args.de_hold_ms)
        try:
            if not isp.sync():
                print("[探测] 同步失败：确认 BOOT0 已拉高")
                return 3
            pid = isp.get_id()
            print(f"[探测] PID = 0x{pid:04X}" if pid else "[探测] 读不到 ID")
            return 0 if pid else 3
        finally:
            isp.close()
    return flash_firmware(args.port, payload, base, baud=args.baud, de=de,
                          chunk=args.chunk, verify=not args.no_verify_bin,
                          do_go=not args.no_go, erase=(args.erase == "mass"))


def cli(args) -> int:
    if serial is None:
        print("未安装 pyserial：pip install pyserial")
        return 2
    if not args.port:
        print("可用串口：", ", ".join(list_ports()) or "（无）")
        print("用 --port 指定，例如 --port /dev/ttyUSB0")
        return 2
    de = DePin(args.de_pin, args.wpi_pin, enabled=not args.no_de)
    if de.backend != "none":
        print(f"485 方向脚：{de.backend}")
    elif not args.no_de and sys.platform.startswith("linux"):
        print("485 方向脚：未找到 GPIO 库（wiringpi / RPi.GPIO / lgpio），按无方向脚处理")
    try:
        return _cli_with(args, de)
    finally:
        de.close()


def _cli_with(args, de) -> int:
    with Link(args.port, args.baud, de=de, de_hold_ms=args.de_hold_ms,
              de_settle_ms=args.de_settle_ms) as link:
        if args.cmd == "scan":
            found = link.scan(args.seconds)
            if not found:
                print("没扫到设备（检查接线、是否只接一片、波特率）")
                return 1
            print(f"扫到 {len(found)} 片：")
            for i, (u, a) in enumerate(sorted(found.items(), key=lambda kv: kv[1]), 1):
                print(f"  {i}. [{u}]  原地址 {a}")
            return 0
        if args.cmd == "read":
            r = link.read_addr(1.0)
            if not r:
                print("无应答")
                return 1
            print(f"[{uid_hex(r[1])}]  原地址 {r[0]}")
            return 0
        if args.cmd == "write":
            if not args.uid:
                print("需要 --uid")
                return 2
            if not (1 <= args.addr <= 254):
                print("--addr 需在 1~254")
                return 2
            r = link.write_addr(parse_uid(args.uid), args.addr)
            if not r:
                print("无应答")
                return 1
            result, addr = r
            print(("成功，当前地址 %d" % addr) if result == 1
                  else ("被拒绝（UID 不匹配或地址非法），当前仍是 %d" % addr))
            return 0 if result == 1 else 1
    return 0


# ---------------- GUI（tkinter，无第三方依赖） ----------------

def gui(port_hint: str | None, de_bcm: int = 12, de_hold_ms: float = 0.5,
        no_de: bool = False, de_settle_ms: float = 2.0) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("md32 地址工具")
    root.geometry("940x720")
    root.minsize(820, 480)

    de = DePin(de_bcm, None, enabled=not no_de)
    state = {"link": None, "devices": [], "scanning": False}

    def say(msg: str) -> None:
        log.configure(state="normal")
        log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        log.see("end")
        log.configure(state="disabled")

    # --- 串口行 ---
    f1 = ttk.Frame(root, padding=(10, 8, 10, 0))
    f1.pack(fill="x")
    ttk.Label(f1, text="端口").pack(side="left")
    cb_port = ttk.Combobox(f1, width=26, state="readonly")
    cb_port.pack(side="left", padx=(6, 6))

    def refresh_ports() -> None:
        ports = list_ports()
        cb_port["values"] = ports or ["（未发现串口）"]
        if ports:
            cb_port.current(0)
            if port_hint in ports:
                cb_port.current(ports.index(port_hint))
    ttk.Button(f1, text="刷新", width=6, command=refresh_ports).pack(side="left")

    btn_open = ttk.Button(f1, text="打开")
    btn_open.pack(side="left", padx=(6, 10))
    lb_state = ttk.Label(f1, text="未连接")
    lb_state.pack(side="left")

    def open_port() -> bool:
        """按当前 cb_port 打开持久连接；成功返回 True。"""
        port = cb_port.get()
        if not port or "未发现" in port:
            return False
        try:
            link = Link(port, de=de, de_hold_ms=de_hold_ms, de_settle_ms=de_settle_ms)
            link.open()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))
            return False
        state["link"] = link
        btn_open.configure(text="关闭")
        lb_state.configure(text=f"已连接 {port} @ {BAUD}")
        say(f"打开 {port} @ {BAUD}")
        return True

    def toggle_port() -> None:
        if state["link"]:
            state["link"].close()
            state["link"] = None
            btn_open.configure(text="打开")
            lb_state.configure(text="未连接")
            say("已关闭串口")
            return
        open_port()

    btn_open.configure(command=toggle_port)

    # --- 设备行 ---
    f2 = ttk.LabelFrame(root, text="设备（广播读地址扫描）", padding=(10, 8))
    f2.pack(fill="x", padx=10, pady=(10, 0))
    row2 = ttk.Frame(f2)
    row2.pack(fill="x")
    btn_scan = ttk.Button(row2, text="扫描总线", width=10)
    btn_scan.pack(side="left")
    cb_dev = ttk.Combobox(row2, state="readonly")
    cb_dev.pack(side="left", fill="x", expand=True, padx=(8, 0))

    lb_sel = ttk.Label(f2, text="未选择设备")
    lb_sel.pack(fill="x", pady=(6, 0))

    row3 = ttk.Frame(f2)
    row3.pack(fill="x", pady=(6, 0))
    ttk.Label(row3, text="新地址").pack(side="left")
    sp_addr = tk.Spinbox(row3, from_=1, to=254, width=6)
    sp_addr.pack(side="left", padx=(6, 10))
    btn_write = ttk.Button(row3, text="写入选中设备", width=14)
    btn_write.pack(side="left")
    btn_read = ttk.Button(row3, text="重读地址", width=10)
    btn_read.pack(side="left", padx=(8, 0))
    ttk.Label(row3, text="（读地址是广播命令，总线上一次只接一片）", foreground="#666")\
        .pack(side="left", padx=(12, 0))

    # --- 固件烧录 ---
    box4 = ttk.LabelFrame(root, text="485 固件升级（A 方案 · 0 区 Boot）", padding=(10, 8))
    box4.pack(fill="x", padx=10, pady=(10, 0))
    r4 = ttk.Frame(box4)
    r4.pack(fill="x")
    fw = {"path": None}
    btn_fw = ttk.Button(r4, text="选择固件…", width=11)
    btn_fw.pack(side="left")
    lb_fw = ttk.Label(r4, text="（未选择 .axf / .bin）")
    lb_fw.pack(side="left", padx=(8, 8))
    btn_iap = ttk.Button(r4, text="485 OTA", width=9)
    btn_iap.pack(side="left", padx=(6, 0))
    pb = ttk.Progressbar(r4, maximum=100)
    pb.pack(side="left", fill="x", expand=True, padx=(8, 0))

    r_uid = ttk.Frame(box4)
    r_uid.pack(fill="x", pady=(6, 0))
    only_uid = tk.BooleanVar(value=False)
    in_boot = tk.BooleanVar(value=False)
    cb_only_uid = ttk.Checkbutton(r_uid, text="只升级该 UID", variable=only_uid)
    cb_only_uid.pack(side="left")
    ed_uid = ttk.Entry(r_uid, width=42, state="disabled")
    ed_uid.pack(side="left", padx=(6, 8))
    ttk.Label(r_uid, foreground="#666",
              text="勾选后自动填入扫描下拉选中的设备；不勾则升级总线上任意一片").pack(side="left")

    r_boot = ttk.Frame(box4)
    r_boot.pack(fill="x", pady=(4, 0))
    ttk.Checkbutton(r_boot, text="设备已在 Boot（救砖模式）", variable=in_boot).pack(side="left")
    btn_probe = ttk.Button(r_boot, text="判断状态", width=9)
    btn_probe.pack(side="left", padx=(8, 0))
    lb_probe = ttk.Label(r_boot, foreground="#006", text="设备状态：未探测")
    lb_probe.pack(side="left", padx=(8, 0))
    ttk.Label(box4, foreground="#C00", wraplength=820, justify="left",
              text="⚠ 救砖模式跳过 UID 过滤、广播 PING Boot：请确保总线上只有一片卡在 Boot 的板子，否则会误刷多片").pack(
                  fill="x", pady=(2, 0))

    def on_boot_toggle(*_):
        # 救砖模式下 UID 过滤无效，强制关掉「只升级该 UID」并禁用
        if in_boot.get():
            only_uid.set(False)
            cb_only_uid.configure(state="disabled")
            ed_uid.configure(state="disabled")
        else:
            cb_only_uid.configure(state="normal")
    in_boot.trace_add("write", on_boot_toggle)

    def sync_uid_from_sel() -> None:
        """扫描下拉选中变化时，把该片 UID 同步到过滤输入框。"""
        if not only_uid.get():
            return
        i = cb_dev.current()
        if 0 <= i < len(state["devices"]):
            uid = state["devices"][i][0]
            ed_uid.configure(state="normal")
            ed_uid.delete(0, "end")
            ed_uid.insert(0, uid)

    def on_uid_toggle(*_):
        ed_uid.configure(state="normal" if (only_uid.get() and not in_boot.get()) else "disabled")
        if only_uid.get() and not in_boot.get():
            sync_uid_from_sel()

    only_uid.trace_add("write", on_uid_toggle)

    ttk.Label(box4, foreground="#666", wraplength=700, justify="left",
              text="升级流程：应用收到请求 → 复位进 0 区 Boot → 新固件写 BACKUP → 整片 CRC 校验"
                   " → Boot 复制到 APP → 跳转。Boot 区与参数区（地址 / xishu / chanshu）不受影响。\n"
                   "若板子卡在 Boot（扫描看不到、read 无应答），勾选「设备已在 Boot」走救砖流程"
                   "（广播 PING Boot，不过滤 UID，请确保总线上只有一片）。").pack(
                       fill="x", pady=(6, 0))

    def pick_fw() -> None:
        f = filedialog.askopenfilename(
            title="选择固件",
            filetypes=[("Keil 产物", "*.axf *.bin"), ("所有文件", "*.*")])
        if f:
            fw["path"] = Path(f)
            lb_fw.configure(text=Path(f).name)

    def burn_iap() -> None:
        """485 OTA：应用收 0x05 请求复位，0 区 Boot 接管并把固件写进 BACKUP→APP。"""
        if not fw["path"]:
            messagebox.showinfo("提示", "先选择 .axf 或 .bin")
            return
        port = cb_port.get()
        if not port or "未发现" in port:
            messagebox.showinfo("提示", "先选一个串口")
            return
        target_uid = ed_uid.get().strip() if only_uid.get() else None
        if only_uid.get() and not target_uid:
            messagebox.showinfo("提示", "先扫描总线并在设备下拉里选中目标，或取消「只升级该 UID」")
            return
        if "updater" in fw["path"].name.lower():
            messagebox.showerror(
                "选错文件",
                "这是升级器镜像（updater.bin），属于旧方案，不能当应用固件烧。\n\n"
                "A 方案请选应用的 md32.bin / md32.axf。")
            return
        mode_tip = ("（救砖模式：设备已在 Boot，将广播 PING 直接升级，不过滤 UID）\n"
                    if in_boot.get() else "")
        if not messagebox.askyesno(
                "确认 485 OTA",
                mode_tip +
                "新固件先写 BACKUP 区，校验通过后由 0 区 Boot 复制到 APP。\n"
                "参数区（地址 / xishu / chanshu）不受影响。\n\n"
                "升级期间会占用 485 总线，别断电。继续？"):
            return
        was_open = state["link"] is not None
        if state["link"]:
            state["link"].close()
            state["link"] = None
            btn_open.configure(text="打开")
            lb_state.configure(text="未连接")
        btn_iap.configure(state="disabled")
        pb.configure(value=0)

        def work() -> None:
            try:
                src = fw["path"]
                img = make_bin(src) if src.suffix.lower() == ".axf" else src
                payload = img.read_bytes()
                root.after(0, lambda: say(f"485 OTA {img.name}  {len(payload)} 字节"))
                de2 = DePin(de_bcm, None, enabled=not no_de)
                # A 方案：交给 0 区 Boot（BEGIN/DATA/END → BACKUP → 复制到 APP）。
                # 不能用旧的 iap_upload：那是「升级器在 Flash 末尾」的旧协议，
                # 新 Boot 不认识 cmd 0x02，会回 status=1「参数错」。
                rc = ota_upload(
                    port, payload, de=de2, target_uid=target_uid,
                    already_in_boot=in_boot.get(),
                    log=lambda m: root.after(0, lambda: say(m)),
                    progress=lambda pct: root.after(0, lambda: pb.configure(value=pct)),
                    de_hold_ms=de_hold_ms, de_settle_ms=de_settle_ms)
                root.after(0, lambda: done_iap(rc))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                root.after(0, lambda: done_iap(-1, msg))

    def done_iap(rc: int, err: str | None = None) -> None:
        btn_iap.configure(state="normal")
        if was_open:
            open_port()   # 升级前若已打开，结束后自动恢复，方便直接扫总线验证
        if rc == 0:
            pb.configure(value=100)
            say("485 OTA 完成")
            messagebox.showinfo("完成", "升级完成，设备已跳回应用")
        else:
            say("485 OTA 失败（退出码 %s）%s" % (rc, ("：" + err) if err else ""))
            messagebox.showerror("失败", "OTA 未完成，退出码 %s" % rc)

        threading.Thread(target=work, daemon=True).start()

    def probe_state() -> None:
        """探测总线上目标设备状态：应用态（能回 read_addr/UID）还是 Boot 态（只回 Boot PING）。
        据此自动勾选/取消「设备已在 Boot」，避免人工选错模式（应用态却用救砖导致 PING 无应答）。"""
        port = cb_port.get()
        if not port or "未发现" in port:
            messagebox.showinfo("提示", "先选一个串口")
            return
        # 探测要独占串口，先关掉 GUI 维护的常连接
        was_open = state["link"] is not None
        if state["link"]:
            state["link"].close()
            state["link"] = None
            btn_open.configure(text="打开")
            lb_state.configure(text="未连接")
        btn_probe.configure(state="disabled")
        lb_probe.configure(text="设备状态：探测中…")

        def work() -> None:
            try:
                de2 = DePin(de_bcm, None, enabled=not no_de)
                iap = Iap(port, de2, de_hold_ms=de_hold_ms, de_settle_ms=de_settle_ms)
                try:
                    # ① 先试应用态：广播读地址，能回 UID 即应用态
                    r = iap.read_addr(1.5)
                    if r:
                        root.after(0, lambda: in_boot.set(False))
                        root.after(0, lambda: lb_probe.configure(
                            text="设备状态：应用态（App）"))
                        info = "应用态（App）\nUID %s  地址 %d\n可直接点「485 OTA」，无需勾救砖" % (
                            " ".join("%02X" % b for b in r[1]), r[0])
                        root.after(0, lambda m=info: say("[状态] " + m.replace("\n", "，")))
                        root.after(0, lambda m=info: messagebox.showinfo("设备状态", m))
                        return
                    # ② 再试 Boot：广播 PING，能回即已在升级模式
                    st, _ = iap.cmd(0xFF, IAP_CMD_PING, b"", timeout=1.0, retries=3)
                    if st == 0:
                        root.after(0, lambda: in_boot.set(True))
                        root.after(0, lambda: lb_probe.configure(
                            text="设备状态：Boot 态（救砖）"))
                        root.after(0, lambda: say(
                            "[状态] Boot 态（救砖）：设备已在升级模式，请保持「设备已在 Boot」再升级"))
                        root.after(0, lambda: messagebox.showinfo(
                            "设备状态",
                            "Boot 态（救砖模式）\n设备已在升级模式、不回应用帧。\n"
                            "请保持勾选「设备已在 Boot」再点「485 OTA」"))
                        return
                    root.after(0, lambda: lb_probe.configure(text="设备状态：未发现设备"))
                    root.after(0, lambda: say(
                        "[状态] 未发现设备：应用态 read 与 Boot PING 都无应答"))
                    root.after(0, lambda: messagebox.showwarning(
                        "设备状态",
                        "未发现设备：应用态 read 与 Boot PING 都无应答。\n检查接线/方向脚/是否上电。"))
                finally:
                    iap.close()
            except Exception as e:  # noqa: BLE001
                root.after(0, lambda: lb_probe.configure(text="设备状态：探测异常"))
                root.after(0, lambda m=str(e): say("[状态] 探测异常：" + m))
            finally:
                if was_open:
                    root.after(0, open_port)   # 探测前若已打开，结束后自动恢复
                root.after(0, lambda: btn_probe.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    btn_fw.configure(command=pick_fw)
    btn_iap.configure(command=burn_iap)
    btn_probe.configure(command=probe_state)

    # --- 日志 ---
    f3 = ttk.LabelFrame(root, text="日志", padding=(10, 8))
    f3.pack(fill="both", expand=True, padx=10, pady=(10, 10))
    log = tk.Text(f3, height=10, state="disabled", wrap="none")
    log.pack(fill="both", expand=True)

    # --- 交互 ---
    def on_pick(_e=None) -> None:
        i = cb_dev.current()
        if i < 0 or i >= len(state["devices"]):
            lb_sel.configure(text="未选择设备")
            return
        uid, addr = state["devices"][i]
        lb_sel.configure(text=f"当前选中：{uid}    原地址 {addr}")
        sp_addr.delete(0, "end")
        sp_addr.insert(0, str(min(254, addr + 1)))
        sync_uid_from_sel()

    cb_dev.bind("<<ComboboxSelected>>", on_pick)

    def scan() -> None:
        if not state["link"]:
            messagebox.showinfo("提示", "先打开串口")
            return
        if state["scanning"]:
            return
        state["scanning"] = True
        btn_scan.configure(state="disabled")
        lb_sel.configure(text="扫描中…（约 3 秒）")
        root.update_idletasks()

        link = state["link"]

        def work() -> None:
            try:
                found = link.scan(3.0)
            except Exception as e:  # noqa: BLE001
                found = {}
                say(f"扫描出错：{e}")
            root.after(0, lambda: done(found))

        def done(found: dict) -> None:
            state["scanning"] = False
            btn_scan.configure(state="normal")
            state["devices"] = sorted(found.items(), key=lambda kv: kv[1])
            if not state["devices"]:
                cb_dev["values"] = []
                cb_dev.set("")
                lb_sel.configure(text="没扫到设备")
                say("扫描无应答（检查接线/波特率/是否只接一片）")
                return
            cb_dev["values"] = [f"[{u}]  (原地址 {a})" for u, a in state["devices"]]
            cb_dev.current(0)
            on_pick()
            lb_sel.configure(text=f"扫到 {len(state['devices'])} 片")
            say(f"扫描完成：{len(state['devices'])} 片")

        threading.Thread(target=work, daemon=True).start()

    btn_scan.configure(command=scan)

    def read_one() -> None:
        if not state["link"]:
            return
        i = cb_dev.current()
        if i < 0:
            return
        uid, _ = state["devices"][i]
        r = state["link"].read_addr(1.0)
        if not r:
            say("重读：无应答")
            return
        addr, got = r
        if uid_hex(got) == uid:
            state["devices"][i] = (uid, addr)
            cb_dev["values"] = [f"[{u}]  (原地址 {a})" for u, a in state["devices"]]
            cb_dev.current(i)
            on_pick()
            say(f"重读：地址 {addr}（UID 一致）")
        else:
            say(f"重读：应答的是 {uid_hex(got)}（地址 {addr}），与选中项不同")

    btn_read.configure(command=read_one)

    def write() -> None:
        if not state["link"]:
            messagebox.showinfo("提示", "先打开串口")
            return
        i = cb_dev.current()
        if i < 0:
            messagebox.showinfo("提示", "先扫描并选中设备")
            return
        uid, old = state["devices"][i]
        try:
            new_addr = int(sp_addr.get())
        except ValueError:
            messagebox.showerror("错误", "新地址要是 1~254 的整数")
            return
        if not (1 <= new_addr <= 254):
            messagebox.showerror("错误", "新地址要在 1~254")
            return
        if new_addr == old:
            messagebox.showinfo("提示", "新地址与原地址相同，固件会拒绝")
            return
        if not messagebox.askyesno("确认", f"把 [{uid}] 的地址从 {old} 改为 {new_addr}？"):
            return
        r = state["link"].write_addr(parse_uid(uid), new_addr)
        if not r:
            say("写入：无应答")
            messagebox.showwarning("无应答", "没收到应答帧")
            return
        result, addr = r
        if result == 1:
            state["devices"][i] = (uid, addr)
            cb_dev["values"] = [f"[{u}]  (原地址 {a})" for u, a in state["devices"]]
            cb_dev.current(i)
            on_pick()
            say(f"[{uid}] 改地址成功 → {addr}")
            messagebox.showinfo("成功", f"地址已改为 {addr}")
        else:
            say(f"[{uid}] 被拒绝（UID 不匹配或地址非法），当前仍是 {addr}")
            messagebox.showwarning("被拒绝", f"固件拒绝了本次修改，当前地址仍是 {addr}。")

    btn_write.configure(command=write)

    def on_close() -> None:
        if state["link"]:
            state["link"].close()
        de.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    refresh_ports()
    if de.backend != "none":
        say(f"485 方向脚：{de.backend}")
    elif not sys.platform.startswith("win"):
        say("485 方向脚：未找到 GPIO 库，按无方向脚处理（若收不到应答，先装 wiringpi/RPi.GPIO）")
    say("提示：读地址为广播命令，扫描时总线上只接一片")
    root.mainloop()
    return 0


# ---------------- 入口 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="md32 地址工具（扫描/读/改）")
    ap.add_argument("cmd", nargs="?", default="gui",
                    choices=["gui", "scan", "read", "write", "flash", "iap",
                             "install-updater", "raw", "ota"],
                    help="默认 gui；flash 仅用于救砖/烧 combined，日常升级用 ota")
    ap.add_argument("--port", help="串口，如 COM10 或 /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--uid", help="12 字节 UID：write 指定目标，ota 用作 ID 过滤（只升级该片）")
    ap.add_argument("--addr", type=int, help="新地址 1~254")
    ap.add_argument("--seconds", type=float, default=3.0, help="扫描时长")
    ap.add_argument("--de-pin", type=int, default=12,
                    help="485 方向脚 BCM 编号，默认 12（= wiringPi 26，即 GPIO12）")
    ap.add_argument("--wpi-pin", type=int, default=None,
                    help="wiringPi 编号；不给则按 BCM 自动换算（BCM12 → wPi26）")
    ap.add_argument("--de-hold-ms", type=float, default=0.5,
                    help="发完最后一个字节后保持 DE 高电平的时间（毫秒），默认 2")
    ap.add_argument("--de-settle-ms", type=float, default=2.0,
                    help="抬 DE 后先等多久再发（毫秒），默认 2，与 C++ 实现一致")
    ap.add_argument("--no-de", action="store_true", help="不控制方向脚（模块自带自动收发时用）")
    ap.add_argument("--no-pad", action="store_true",
                    help="发送前不垫哑字节（默认垫，用来吸收半双工切换丢掉的首字节）")
    # 烧录相关
    ap.add_argument("--axf", help="烧录：Keil 产物 .axf（自动转 bin）")
    ap.add_argument("--bin", help="烧录：直接给 .bin")
    ap.add_argument("--fromelf", default=str(DEFAULT_FROMELF))
    ap.add_argument("--base", default="0x08000000", help="烧录起始地址")
    ap.add_argument("--chunk", type=int, default=240,
                    help="每块字节数：ISP 烧录 ≤256，485 升级/装升级器 ≤248")
    ap.add_argument("--probe", action="store_true", help="烧录前只握手+读 ID，不擦不写")
    ap.add_argument("--erase", choices=["mass", "none"], default="mass",
                    help="flash(救砖)专用：mass=全片擦除，none=不擦除")
    ap.add_argument("--no-go", action="store_true", help="烧完不跳转")
    ap.add_argument("--no-verify-bin", dest="no_verify_bin", action="store_true",
                    help="烧完不回读校验")
    ap.add_argument("--debug", action="store_true", help="打印每个命令的收发字节")
    ap.add_argument("--resume", action="store_true",
                    help="ota：上次升级中断时从断点续传（默认重新 BEGIN 全量发送）")
    ap.add_argument("--hex", help="raw：要发送的帧（不含校验和），如 \"FE FE 03 02 05\"")
    ap.add_argument("--wait", type=float, default=0.5, help="raw：发完等多久再收")
    ap.add_argument("--already-in-boot", action="store_true",
                    help="IAP：板子已在升级器里，跳过“通知应用进升级”")
    args = ap.parse_args()
    global PAD_BYTE_DEFAULT
    if getattr(args, "no_pad", False):
        PAD_BYTE_DEFAULT = False

    if args.cmd == "flash":
        return flash_cli(args)
    if args.cmd == "iap":
        return iap_cli(args)
    if args.cmd == "install-updater":
        return install_updater_cli(args)
    if args.cmd == "raw":
        return raw_cli(args)
    if args.cmd == "ota":
        return ota_cli(args)
    if args.cmd == "gui":
        try:
            return gui(args.port, args.de_pin, args.de_hold_ms, args.no_de,
                       args.de_settle_ms)
        except Exception as e:  # noqa: BLE001
            if "tkinter" in str(e) or "No module named 'tkinter'" in str(e):
                print("这台机器没有 tkinter，用命令行模式：")
                print("  python addr_tool.py --port /dev/ttyUSB0 scan")
                print("  或安装： sudo apt install python3-tk")
                return 2
            raise
    return cli(args)


if __name__ == "__main__":
    sys.exit(main())
