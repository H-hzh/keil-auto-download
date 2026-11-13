#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""md32 命令行构建 / 烧录 / 复位 / 校验 一条龙。

脚本位置：D:\\code\\keil\\脚本\\md32\\flash_md32.py
默认工程：见下方 DEFAULT_PROJECT；不传工程参数就用它。

用法：
    python flash_md32.py [工程目录或 .uvprojx] [选项]

示例：
    python flash_md32.py                                  # 默认工程：编译+烧录+复位+校验
    python flash_md32.py --chip GD32F303CC --serial COM10
    python flash_md32.py --no-build                       # 只烧录
    python flash_md32.py --build-only                     # 只编译
    python flash_md32.py --wait 60                        # 等手动复位最多 60s
    python flash_md32.py "D:\\other\\proj\\foo.uvprojx"    # 换工程

烧录模式（默认全烧，--app 退回旧应用模式）：
    默认（不传 --app）：编译 md32 → 编译 0 区 Boot → 合并成 Objects/combined.hex/.bin
        → 生成临时 merged UV4 工程，经同一根 PWLINK2/CMSIS-DAP 下载器全片擦除+烧录。
        （下载器就是板子上的 SWD 探头，不是串口 ISP；全片擦除会清掉参数区，烧完要重设地址/校准）
    --app：只编译并用 UV4(-f) 烧录应用区（DAP 探头，BOOT0 低），即旧行为。

为什么要"复位"这一步：
    uVision 的 `-f`（Flash Download）默认下载完把内核留在停止态，
    除非工程里勾了 Options for Target -> Debug -> Settings -> Flash Download
    -> "Reset and Run"。一次性勾上之后本脚本的复位步骤会自动跳过（检测到
    串口有帧即认为已在运行）。没勾的话脚本会尝试 DTR/RTS 脉冲，
    再失败就提示你按板子复位键，并等待 --wait 秒。

退出码：
    0  全部成功（编译/烧录/运行校验）
    2  编译错误
    3  编译只有警告但用户要求 --strict
    4  烧录失败或日志里没有 Verify OK
    5  烧录成功但目标没有运行起来（复位未完成）
    6  工程/工具路径等前置条件不满足
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_UV4 = Path(r"D:\keil\Keil_v5\UV4\UV4.exe")
DEFAULT_PROJECT = Path(r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
                       r"\temperature_8_channel - 副本\temperature_8_channel - 副本\md32.uvprojx")
# 记住上一次用的工程：不传工程参数时优先用它，没有才回落到 DEFAULT_PROJECT
STATE_FILE = Path(__file__).resolve().parent / "last_project.txt"
# 同目录下的辅助脚本：build_boot / make_combined / addr_tool
SCRIPTS_DIR = Path(__file__).resolve().parent
HDR_F1 = b"\xff\xff\xff\xf1"
HDR_A1 = b"\xaa\xaa\xaa\xa1"


# ---------------------------------------------------------------- 上次工程
def load_last() -> Path | None:
    try:
        txt = STATE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not txt:
        return None
    p = Path(txt)
    return p if p.exists() else None


def save_last(prj: Path) -> None:
    try:
        STATE_FILE.write_text(str(prj.resolve()), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 工程/芯片
def resolve_project(p: str) -> Path:
    path = Path(p)
    if path.is_dir():
        cands = sorted(path.glob("*.uvprojx"))
        if not cands:
            cands = sorted(path.glob("*.uvproj"))
        if not cands:
            raise SystemExit(f"[前置条件] {path} 下找不到 .uvprojx")
        if len(cands) > 1:
            raise SystemExit("[前置条件] 目录下有多个工程，请直接指定：\n  "
                             + "\n  ".join(str(c) for c in cands))
        return cands[0]
    if not path.exists():
        raise SystemExit(f"[前置条件] 工程不存在：{path}")
    return path


def read_device(prj: Path) -> str:
    m = re.search(r"<Device>([^<]+)</Device>", prj.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else ""


def set_device(prj: Path, chip: str) -> None:
    """改 <Device>。只改型号字段，Flash 算法不动，会提示人工确认。"""
    raw = prj.read_text(encoding="utf-8", errors="surrogateescape")
    if not re.search(r"<Device>[^<]+</Device>", raw):
        raise SystemExit("[前置条件] 工程里找不到 <Device> 字段，无法设定型号")
    bak = prj.with_suffix(prj.suffix + ".bak_chip")
    if not bak.exists():
        bak.write_text(raw, encoding="utf-8", errors="surrogateescape")
    new = re.sub(r"<Device>[^<]+</Device>", f"<Device>{chip}</Device>", raw, count=1)
    prj.write_text(new, encoding="utf-8", errors="surrogateescape")
    print(f"[型号] 已把 <Device> 改为 {chip}（原文件备份 {bak.name}）")
    print("[型号] 注意：Flash 算法 / 启动文件 / 宏定义 不会自动跟着改，"
          "换系列时请在 Keil 里核对一遍")


# ---------------------------------------------------------------- UV4 调用
def run_uv4(uv4: Path, cmd: str, prj: Path, log: Path) -> int:
    args = [str(uv4), cmd, str(prj), "-j0", "-o", str(log)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[UV4] {cmd} 超时（600s）")
    except OSError as exc:
        raise SystemExit(f"[UV4] 启动失败：{exc}")
    return proc.returncode


def tail_log(log: Path, n: int = 6) -> str:
    if not log.exists():
        return "(无日志)"
    lines = [ln.rstrip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())[-2000:]


def parse_build_result(log: Path) -> tuple[int, int]:
    txt = log.read_text(encoding="utf-8", errors="replace")
    m = re.findall(r"(\d+)\s+Error\(s\),\s*(\d+)\s+Warning\(s\)", txt)
    if not m:
        return (-1, -1)
    err, warn = m[-1]
    return int(err), int(warn)


# ---------------------------------------------------------------- 串口
def open_port(name: str, baud: int):
    try:
        import serial
    except ImportError:
        return None, "pyserial 未安装"
    try:
        return serial.Serial(name, baud, timeout=0.2), ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def count_frames(raw: bytes) -> int:
    """数 A1（温度）帧：AA AA AA A1 + 8*i16 + AA AA AA AA。"""
    n = 0
    pos = 0
    while True:
        i = raw.find(HDR_A1, pos)
        if i < 0 or i + 24 > len(raw):
            break
        if raw[i + 20:i + 24] == b"\xaa\xaa\xaa\xaa":
            n += 1
        pos = i + 1
    return n


def read_temps(ser, seconds: float) -> tuple[int, list[list[float]]]:
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < seconds:
        buf += ser.read(512)
    raw = bytes(buf)
    frames = []
    pos = 0
    while True:
        i = raw.find(HDR_A1, pos)
        if i < 0 or i + 24 > len(raw):
            break
        if raw[i + 20:i + 24] == b"\xaa\xaa\xaa\xaa":
            vals = [int.from_bytes(raw[i + 4 + k * 2:i + 6 + k * 2], "big", signed=True) / 100
                    for k in range(8)]
            frames.append(vals)
        pos = i + 1
    return len(frames), frames


def pulse(ser, line: str) -> None:
    try:
        setattr(ser, line, False)
        time.sleep(0.12)
        setattr(ser, line, True)
        time.sleep(0.25)
    except Exception:  # noqa: BLE001
        pass


def warn_a1_branch(prj: Path) -> None:
    """编译前检查 PTC.C 里 A1 温度帧的发送块有没有被注释掉。

    该块被 /* */ 包住时固件只发开机包、不发温度帧，现象很像"复位失效"。
    启用态写作 "///*"：用行注释吃掉 /*，块就不生效。
    """
    src = prj.parent / "Aplication" / "PTC.C"
    if not src.exists():
        return
    try:
        lines = src.read_bytes().decode("gbk", "replace").splitlines()
    except Exception:  # noqa: BLE001
        return
    for i, line in enumerate(lines):
        low = line.replace(" ", "").lower()
        if "tx_buffer[3]=0xa1" not in low:      # 定位 A1 帧的 tag 赋值
            continue
        for j in range(i - 1, max(i - 6, -1), -1):   # 往回找最近的注释开关
            s = lines[j].strip()
            if s in ("/*", "///*"):
                if s == "/*":
                    print("[警告] PTC.C 里 A1 温度帧发送块被 /* */ 注释掉了！")
                    print("[警告] 该行要写成 ///* 才生效，否则板子只发开机包、收不到温度帧")
                return
        return


def dap_reset(timeout: float = 20.0) -> bool:
    """通过 CMSIS-DAP v2 给目标发 SYSRESETREQ。

    uVision 的 -f 在批处理模式下不会执行 Reset and Run，只能自己复位。
    """
    tool = Path(__file__).resolve().parent / "dap_reset.py"
    if not tool.exists():
        print("[复位] 找不到 dap_reset.py，跳过 DAP 复位")
        return False
    print("[复位] uVision 下载后不复位，改用 CMSIS-DAP 发 SYSRESETREQ…")
    try:
        p = subprocess.run([sys.executable, str(tool)], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"[复位] 调用 dap_reset 失败：{exc}")
        return False
    for line in (p.stdout or "").strip().splitlines():
        print("[复位] " + line)
    if p.returncode != 0 and p.stderr:
        print("[复位] " + p.stderr.strip().splitlines()[-1])
    return p.returncode == 0


# ---------------------------------------------------------------- HTTP 回退
# 串口常被 seriportTest 的服务（127.0.0.1:8766）占着，此时改用它的 HTTP 接口取帧。
API_URL = "http://127.0.0.1:8766/call"


API_HEALTH = "http://127.0.0.1:8766/health"


def api_alive() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(API_HEALTH, timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def api_ensure_port(port: str, baud: int) -> bool:
    """确认串口服务真的连上了目标；没连就替它开一次。

    服务活着但 serial_open=false 时，frames.capture 永远返回空，
    容易被误判成"板子没运行"。
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(API_HEALTH, timeout=3) as resp:
            info = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return False
    if info.get("serial_open"):
        return True
    print(f"[校验] 串口服务未连接 {port}，尝试打开…")
    try:
        r = api_post("port.open", {"port": port, "baud": baud})
        if r.get("ok"):
            print(f"[校验] 已连接 {port}")
            return True
        print(f"[校验] 打开 {port} 失败：{r.get('error', '未知')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[校验] 打开 {port} 异常：{exc}")
    return False


def api_post(command: str, args: dict, timeout: float = 30.0):
    import json
    import urllib.request
    body = json.dumps({"command": command, "args": args}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def api_capture_temps(ms: int) -> tuple[int, list[list[float]]]:
    """通过 HTTP 抓一段，返回 (温度帧数, 每帧 8 路温度)。"""
    try:
        data = api_post("frames.capture", {"ms": ms})
    except Exception:  # noqa: BLE001
        return 0, []
    frames = data.get("result") or []
    out = []
    for fr in frames:
        fields = fr.get("fields") or {}
        if isinstance(fields, str):
            continue
        vals = fields.get("values")
        if isinstance(vals, list) and fields.get("kind") == "ptc16":
            try:
                out.append([float(v.get("value")) for v in vals])
            except Exception:  # noqa: BLE001
                pass
            continue
        # 兜底：自己从 hex 里解 A1 帧
        hexs = fr.get("hex") or ""
        try:
            raw = bytes(int(t, 16) for t in hexs.split())
        except Exception:  # noqa: BLE001
            continue
        i = raw.find(HDR_A1)
        if i >= 0 and i + 24 <= len(raw) and raw[i + 20:i + 24] == b"\xaa\xaa\xaa\xaa":
            out.append([int.from_bytes(raw[i + 4 + k * 2:i + 6 + k * 2], "big", signed=True) / 100
                        for k in range(8)])
    return len(out), out


# ---------------------------------------------------------------- 合并 / 全烧
def run_tool(script: Path, *cli, timeout: int = 600) -> int:
    """用当前 Python 解释器跑同目录（或给定）的辅助脚本，转发其输出。"""
    args = [sys.executable, str(script), *cli]
    print(f"[运行] {script.name} {' '.join(cli)}")
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[运行] {script.name} 超时（{timeout}s）")
        return 1
    out = (proc.stdout or "").strip()
    if out:
        for line in out.splitlines():
            print("  " + line)
    err = (proc.stderr or "").strip()
    if err:
        for line in err.splitlines()[-8:]:
            print("  [err] " + line)
    return proc.returncode


# combined_flash.s：把 Objects/combined.bin 整块当只读数据拼到 0x08000000
COMBINED_S = r"""    AREA COMBINED, DATA, READONLY, ALIGN 4
    EXPORT combined_image
combined_image
    INCBIN "combined_flash.bin"
    END
"""

# combined_flash.sct：单加载区覆盖整片 256KB，使 UV4 走下载器全片擦除+烧录
COMBINED_SCT = """LR_COMB 0x08000000 {
  ER_COMB 0x08000000 0x00040000 {
    combined_flash.o (COMBINED, +FIRST)
  }
}
"""


def gen_combined_project(prj: Path) -> Path:
    """基于 md32.uvprojx 生成一个 merged 工程：只保留一个 INCBIN 汇编文件，
    改 IROM/链接基址为全片，改用 scatter，去掉 RTE（避免 DFP 外设源被编进来）。
    产物供 UV4 经同一根 PWLINK2/CMSIS-DAP 下载器全片擦除+烧录。"""
    root = prj.parent
    src = prj.read_text(encoding="utf-8", errors="replace")

    # 输出名、IROM、OCR_RVCT4、链接基址都改成全片 0x08000000/0x40000
    src = src.replace("<OutputName>md32</OutputName>", "<OutputName>combined</OutputName>")
    src = src.replace(
        "<IROM>\n                <Type>1</Type>\n                <StartAddress>0x8004000</StartAddress>\n                <Size>0x1C000</Size>\n              </IROM>",
        "<IROM>\n                <Type>1</Type>\n                <StartAddress>0x8000000</StartAddress>\n                <Size>0x40000</Size>\n              </IROM>")
    src = src.replace(
        "<OCR_RVCT4>\n                <Type>1</Type>\n                <StartAddress>0x8004000</StartAddress>\n                <Size>0x1C000</Size>\n              </OCR_RVCT4>",
        "<OCR_RVCT4>\n                <Type>1</Type>\n                <StartAddress>0x8000000</StartAddress>\n                <Size>0x40000</Size>\n              </OCR_RVCT4>")
    src = src.replace("<TextAddressRange>0x08004000</TextAddressRange>",
                      "<TextAddressRange>0x08000000</TextAddressRange>")
    src = src.replace("<useFile>0</useFile>", "<useFile>1</useFile>")
    src = src.replace("<ScatterFile></ScatterFile>", "<ScatterFile>combined_flash.sct</ScatterFile>")
    # 链接入口（DATA 镜像无所谓，给个符号避免 armlink 告警）
    src = src.replace("<Misc></Misc>", "<Misc>--entry combined_image</Misc>")
    # 去掉 RTE：否则 DFP 外设 .c 会被一起编进镜像，和 combined.bin 冲突
    src = re.sub(r"<RTE>.*?</RTE>", "", src, flags=re.S)
    # Groups 只留一个汇编文件 combined_flash.s
    groups = ('  <Groups>\n'
              '    <Group>\n'
              '      <GroupName>combined</GroupName>\n'
              '      <Files>\n'
              '        <File>\n'
              '          <FileName>combined_flash.s</FileName>\n'
              '          <FileType>2</FileType>\n'
              '          <FilePath>./combined_flash.s</FilePath>\n'
              '        </File>\n'
              '      </Files>\n'
              '    </Group>\n'
              '  </Groups>')
    src = re.sub(r"<Groups>.*?</Groups>", groups, src, flags=re.S)

    out_uv = root / "combined_flash.uvprojx"
    out_uv.write_text(src, encoding="utf-8")
    (root / "combined_flash.s").write_text(COMBINED_S, encoding="utf-8")
    (root / "combined_flash.sct").write_text(COMBINED_SCT, encoding="utf-8")
    # 复制 combined.bin 到工程根给 INCBIN 用（避免中文/空格路径问题）
    cb = root / "Objects" / "combined.bin"
    if cb.exists():
        (root / "combined_flash.bin").write_bytes(cb.read_bytes())
    return out_uv


def build_combined(prj: Path) -> int:
    """编译 0 区 Boot（若工程里有 build_boot.py）并合并成 Objects/combined.{hex,bin}。"""
    root = prj.parent
    objects = root / "Objects"
    boot_py = root / "bootloader" / "build_boot.py"
    boot_bin = root / "bootloader" / "build" / "boot.bin"
    app_hex = objects / "md32.hex"
    make_py = SCRIPTS_DIR / "make_combined.py"

    if boot_py.exists():
        print("[合并] 重新编译 0 区 Boot…")
        if run_tool(boot_py) != 0:
            print("[合并] boot 编译失败")
            return 2
    elif not boot_bin.exists():
        print(f"[合并] 找不到 {boot_py}，也找不到现成的 {boot_bin}，无法生成 combined")
        return 2

    if not app_hex.exists():
        print(f"[合并] 找不到 {app_hex}，请先确保 md32 已编译（--no-build 时需已存在）")
        return 2

    print("[合并] 生成 combined 镜像到 Objects/")
    if run_tool(make_py, "--boot", str(boot_bin), "--app", str(app_hex),
                "--out", str(objects)) != 0:
        print("[合并] make_combined 失败")
        return 2
    return 0


def flash_combined(prj: Path, args) -> int:
    """全烧模式：生成 merged 工程 → UV4 -b 出 combined.axf → UV4 -f 经下载器全片烧。"""
    uv4 = Path(args.uv4)
    if not uv4.exists():
        print(f"[前置条件] 找不到 UV4.exe：{uv4}")
        return 6
    if not (prj.parent / "Objects" / "combined.bin").exists():
        print("[全烧] 找不到 Objects/combined.bin，请先确保合并步骤成功")
        return 4

    print("[全烧] 生成 merged UV4 工程（INCBIN Objects/combined.bin）…")
    combined_uv = gen_combined_project(prj)

    # 编译出 combined.axf
    blog = prj.parent / "_uv4_combined_build.log"
    rc = run_uv4(uv4, "-b", combined_uv, blog)
    err, warn = parse_build_result(blog)
    print(f"[全烧] 编译 {err} error / {warn} warning")
    if err > 0 or rc >= 2:
        print(tail_log(blog))
        return 2
    if warn:
        print(tail_log(blog, 8))

    # 烧录（走同一根 PWLINK2/CMSIS-DAP，全片擦除+写）
    print("[全烧] UV4 -f 经下载器全片擦除并烧录 combined（Boot 0区 + App）…")
    flog = prj.parent / "_uv4_combined_flash.log"
    rc = run_uv4(uv4, "-f", combined_uv, flog)
    txt = flog.read_text(encoding="utf-8", errors="replace") if flog.exists() else ""
    ok = ("Verify OK" in txt) and ("Programming Done" in txt) and rc == 0
    for line in tail_log(flog).splitlines():
        print("[全烧] " + line)
    if not ok:
        print("[全烧] 失败：日志里没有 Programming Done + Verify OK")
        print("[全烧] 常见原因：1) PowerWriter/其他软件占着 PWLINK2；"
              "2) uvoptx 里 -U 序列号与当前探头不一致（No Debug Unit is selected）")
        return 4

    print("[完成] combined 已全片烧录（Boot 0区 + App），参数区已清空")
    print("[完成] ⚠ 多片板子地址会变回 1，且校准被清，烧完请用上位机重新设地址/校准")
    return 0


def reset_and_verify(args) -> int:
    """App 模式烧完后的复位 + 运行校验（与原逻辑一致）。"""
    # 默认：烧完只复位，不再读串口判断运行（要跑通校验请加 --verify）
    if not (args.verify or args.verify_only):
        if dap_reset():
            print("[复位] SYSRESETREQ 已发送，流程结束")
        else:
            print("[复位] DAP 复位失败：探头被占用或目标未连接")
        return 0

    # ---- 复位 + 运行校验（串口被占用时走 seriportTest 的 HTTP 接口）
    ser, why = open_port(args.serial, args.baud)
    use_api = ser is None
    if use_api:
        if not api_alive():
            print(f"[校验] 打不开 {args.serial}（{why}），串口服务也不在，跳过运行校验")
            return 0
        print(f"[校验] {args.serial} 被占用（{why}），改用 HTTP 127.0.0.1:8766 读帧")
        if not api_ensure_port(args.serial, args.baud):
            print("[校验] 串口服务没连上目标，无法判断目标是否运行；跳过运行校验")
            print("[校验] （这种情况以前会被误报成『目标没有运行』）")
            return 0

    def grab(sec: float):
        return api_capture_temps(int(sec * 1000)) if use_api else read_temps(ser, sec)

    try:
        n, _ = grab(1.2)
        for _attempt in (1, 2):          # DAP 复位偶尔会与 halt 状态打架，最多试两次
            if n:
                break
            if dap_reset():
                n, _ = grab(3.5)   # 复位后固件启动有延迟，别急着判失败
        if n == 0 and not use_api:
            print("[复位] DAP 复位无效，尝试 DTR 脉冲…")
            pulse(ser, "dtr")
            n, _ = grab(1.5)
        if n == 0 and not use_api:
            print("[复位] DTR 无效，尝试 RTS 脉冲…")
            pulse(ser, "rts")
            n, _ = grab(1.5)
        if n == 0:
            print("")
            print("  ┌────────────────────────────────────────────────────────────┐")
            print("  │ 复位指令已成功送达内核（SYSRESETREQ 有 ACK），但串口无数据。│")
            print("  │ 这不一定是复位失败，按下面顺序看：                         │")
            print("  │   1) 按一下板子复位键：按下后仍无数据 → 查固件是否发送；    │")
            print("  │      按下后有数据 → 是复位时序问题，需加大等待窗口。        │")
            print("  │   2) 确认固件里 A1 温度帧的发送分支没被注释掉。             │")
            print("  │   3) 确认串口接线与波特率（本工程 115200）。               │")
            print(f"  │ 仍会继续等待 {args.wait:.0f}s，期间一旦有帧立刻继续。               │")
            print("  └────────────────────────────────────────────────────────────┘")
            t0 = time.time()
            while time.time() - t0 < args.wait:
                n, _ = grab(1.0)
                if n:
                    break
            if n == 0:
                print("[校验] 等待超时：复位已发出，但串口始终没有数据")
                print("[校验] 结论：不是复位问题，请查固件发送分支/接线/波特率")
                return 5
            print(f"[复位] 目标已启动（等待 {time.time() - t0:.1f}s）")

        n, frames = grab(3.0)
        if not frames:
            print("[校验] 3s 内没有完整温度帧")
            return 5
        last = frames[-1]
        print(f"[校验] 3s 内 {n} 帧；通道 a~h 温度："
              + " ".join("%.2f" % v for v in last))
        unconnected = sum(1 for v in last if abs(v + 100) < 0.01)
        if unconnected:
            print(f"[校验] 其中 {unconnected} 路为 -100（未接传感器）")
        return 0
    finally:
        if ser is not None:
            ser.close()


# ---------------------------------------------------------------- 主流程
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="md32 构建 / 烧录 / 复位 / 校验")
    ap.add_argument("project", nargs="?", default=None,
                    help="工程目录或 .uvprojx 路径（省略则用上次的工程，再没有才用内置默认）")
    ap.add_argument("--forget", action="store_true", help="清除“上次工程”记录，回到内置默认")
    ap.add_argument("--chip", help="设定芯片型号，如 GD32F303CC（默认沿用工程里的）")
    ap.add_argument("--uv4", default=str(DEFAULT_UV4), help="UV4.exe 路径")
    ap.add_argument("--serial", default="COM10", help="运行校验用的串口，default=COM10")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-build", action="store_true", help="跳过编译，直接烧录现有 axf")
    ap.add_argument("--build-only", action="store_true", help="只编译不烧录")
    ap.add_argument("--no-flash", action="store_true", help="同 --build-only")
    ap.add_argument("--app", dest="app_mode", action="store_true",
                    help="App 模式：只编译并用 UV4(-f) 烧录应用区（经 DAP 探头，BOOT0 低）。"
                         "默认是全烧：编译 md32 → 编译 0区 Boot → 合并成 Objects/combined "
                         "→ 生成临时 merged 工程经 PWLINK2/CMSIS-DAP 下载器全片擦除+烧录")
    ap.add_argument("--verify", action="store_true", help="烧完做串口运行校验（默认只复位，不校验）")
    ap.add_argument("--no-verify", action="store_true", help="兼容旧用法：不校验（现已是默认）")
    ap.add_argument("--verify-only", action="store_true", help="只做复位+运行校验，不编译不烧录")
    ap.add_argument("--wait", type=float, default=30.0, help="等目标运行（手动复位）最长秒数")
    ap.add_argument("--strict", action="store_true", help="编译有警告也算失败")
    args = ap.parse_args(argv)

    if args.forget:
        try:
            STATE_FILE.unlink()
            print("[工程] 已清除上次记录")
        except OSError:
            pass

    if args.project:
        prj = resolve_project(args.project)
        save_last(prj)
        src = "命令行指定，已记为上次"
    else:
        last = load_last()
        if last is not None:
            prj, src = last, "上次使用"
        else:
            prj, src = resolve_project(str(DEFAULT_PROJECT)), "内置默认"

    uv4 = Path(args.uv4)
    if not uv4.exists():
        print(f"[前置条件] 找不到 UV4.exe：{uv4}")
        return 6
    print(f"[工程] {prj}（{src}）")

    if args.chip:
        cur = read_device(prj)
        if cur and cur.lower() != args.chip.lower():
            set_device(prj, args.chip)
        else:
            print(f"[型号] 已是 {cur or args.chip}，无需改动")
    else:
        print(f"[型号] 沿用工程设置：{read_device(prj)}")

    warn_a1_branch(prj)

    logs = prj.parent

    # ---- 编译
    if args.verify_only:
        print("[跳过] --verify-only：不编译、不烧录")
    elif not args.no_build:
        blog = logs / "_uv4_build.log"
        rc = run_uv4(uv4, "-b", prj, blog)
        err, warn = parse_build_result(blog)
        if err < 0:
            print("[编译] 日志里没有编译统计，输出如下：")
            print(tail_log(blog))
            return 2
        print(f"[编译] {err} error / {warn} warning")
        if err > 0 or rc >= 2:
            print(tail_log(blog))
            return 2
        if args.strict and warn > 0:
            print("[编译] --strict 模式下警告视为失败")
            return 3
        axf = next(iter(sorted(prj.parent.glob("**/Objects/*.axf"))), None)
        if axf:
            print(f"[产物] {axf}  {axf.stat().st_size} 字节")

    # ---- 仅校验：不编译不烧录，直接复位+读帧
    if args.verify_only:
        return reset_and_verify(args)

    # ---- 只编译：app 模式到此结束；全烧模式顺手把 boot+app 合并出来
    if args.build_only or args.no_flash:
        if not args.app_mode:
            if build_combined(prj) != 0:
                return 2
            print("[完成] 仅编译：combined 镜像已生成在 Objects/（未烧录）")
        return 0

    # ---- 选择烧录方式
    if args.app_mode:
        # 应用模式：UV4(-f) 只烧应用区（需 DAP 探头，BOOT0 低）
        flog = logs / "_uv4_flash.log"
        rc = run_uv4(uv4, "-f", prj, flog)
        txt = flog.read_text(encoding="utf-8", errors="replace") if flog.exists() else ""
        ok = ("Verify OK" in txt) and ("Programming Done" in txt) and rc == 0
        for line in tail_log(flog).splitlines():
            print("[烧录] " + line)
        if not ok:
            print("[烧录] 失败：日志里没有 Programming Done + Verify OK")
            print("[烧录] 常见原因：1) PowerWriter 占着探头，先断开/关闭；"
                  "2) uvoptx 里的 -U 序列号与当前探头不一致（会报 No Debug Unit is selected）")
            return 4
        return reset_and_verify(args)

    # ---- 全烧模式（默认）：编译 0区 Boot → 合并 → UV4 经下载器全片烧 combined
    if build_combined(prj) != 0:
        return 2
    return flash_combined(prj, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
