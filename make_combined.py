#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合并 0 区 Bootloader 与应用镜像，生成一次性烧录文件

产物：
    combined.hex  Intel HEX（绝对地址），可直接给 PowerWriter / J-Flash / CubeProgrammer
    combined.bin  裸镜像，基址 0x08000000，可用 addr_tool.py flash 走串口 ISP 烧
用法：
    python make_combined.py
    python make_combined.py --boot "…\\bootloader\\build\\boot.bin" \
                            --app "…\\Objects\\md32.hex" --out out.hex
"""
from __future__ import annotations

import argparse
from pathlib import Path

BASE = 0x08000000
APP_OFFSET = 0x4000          # 16KB
BOOT_SIZE = 0x4000
TOTAL_SIZE = 0x40000         # 256KB

DEFAULT_BOOT = Path(r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
                    r"\temperature_8_channel - 副本\temperature_8_channel - 副本"
                    r"\bootloader\build\boot.bin")
DEFAULT_APP = Path(r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
                   r"\temperature_8_channel - 副本\temperature_8_channel - 副本"
                   r"\Objects\md32.hex")
DEFAULT_OUT = Path(__file__).resolve().parent / "combined"


def parse_hex(path: Path) -> dict[int, int]:
    data: dict[int, int] = {}
    upper = 0
    for line in path.read_text(encoding="ascii", errors="strict").splitlines():
        line = line.strip()
        if not line.startswith(":") or len(line) < 11:
            continue
        rec = bytes.fromhex(line[1:])
        cnt, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
        payload = rec[4:4 + cnt]
        if typ == 0x00:
            for i, b in enumerate(payload):
                data[upper + addr + i] = b
        elif typ == 0x04 and cnt == 2:
            upper = ((payload[0] << 8) | payload[1]) << 16
        elif typ == 0x01:
            break
    return data


def emit_hex(data: dict[int, int]) -> str:
    lines: list[str] = []
    current_upper = None
    addrs = sorted(data)
    i = 0
    while i < len(addrs):
        start = addrs[i]
        upper = start >> 16
        if upper != current_upper:
            lines.append(_rec(0x04, 0, bytes([(upper >> 8) & 0xFF, upper & 0xFF], )))
            current_upper = upper
        chunk = []
        low = start & 0xFFFF
        j = i
        while j < len(addrs) and len(chunk) < 16 and (addrs[j] >> 16) == upper \
                and addrs[j] == (start + len(chunk)):
            chunk.append(data[addrs[j]])
            j += 1
        if not chunk:
            j = i + 1
        else:
            lines.append(_rec(0x00, low, bytes(chunk)))
        i = j
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def _rec(typ: int, addr: int, payload: bytes) -> str:
    body = bytes([len(payload), (addr >> 8) & 0xFF, addr & 0xFF, typ]) + payload
    chk = (-sum(body)) & 0xFF
    return ":" + (body + bytes([chk])).hex().upper()


def main() -> int:
    ap = argparse.ArgumentParser(description="合并 Boot + App 镜像")
    ap.add_argument("--boot", default=str(DEFAULT_BOOT))
    ap.add_argument("--app", default=str(DEFAULT_APP))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    bootp, appp = Path(args.boot), Path(args.app)
    out = Path(args.out)
    if not bootp.exists() or not appp.exists():
        raise SystemExit(f"[错误] 找不到输入：{bootp} / {appp}")

    boot = bootp.read_bytes()
    if len(boot) > BOOT_SIZE:
        raise SystemExit(f"[错误] Boot {len(boot)} 字节，超过 {BOOT_SIZE}")

    cells = parse_hex(appp) if appp.suffix.lower() in (".hex", ".i32") else {
        BASE + APP_OFFSET + i: b for i, b in enumerate(appp.read_bytes())
    }
    app_min, app_max = min(cells), max(cells)
    if app_min < BASE + APP_OFFSET or app_max >= BASE + APP_OFFSET + 0x1C000:
        raise SystemExit(f"[错误] 应用地址越界：0x{app_min:08X}..0x{app_max:08X}")

    merged = {BASE + i: b for i, b in enumerate(boot)}
    merged.update(cells)

    out.mkdir(parents=True, exist_ok=True)
    (out / "combined.hex").write_text(emit_hex(merged), encoding="ascii")

    image = bytearray(b"\xFF" * TOTAL_SIZE)
    for a, b in merged.items():
        if BASE <= a < BASE + TOTAL_SIZE:
            image[a - BASE] = b
    (out / "combined.bin").write_bytes(bytes(image))

    print(f"[Boot] {bootp.name}  {len(boot)} 字节 @ 0x{BASE:08X}")
    print(f"[App ] {appp.name}  {len(cells)} 字节 @ 0x{app_min:08X}..0x{app_max:08X}")
    print(f"[产物] {out / 'combined.hex'}")
    print(f"[产物] {out / 'combined.bin'}  {len(image)} 字节（基址 0x{BASE:08X}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
