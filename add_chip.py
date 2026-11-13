#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""新芯片校准一条龙：分析 CSV → 写固件 → 记录到 xlsx → 编译烧录。

用法：
    python add_chip.py --uid "3C 12 3B CC A4 61 30 58 50 36 31 32" --csv <uid14读取adc.csv>
    python add_chip.py --uid ... --csv ... --dry-run          # 只算不改
    python add_chip.py --uid ... --csv ... --no-flash         # 只写固件/表格
    python add_chip.py --uid ... --csv ... --name uid15       # 指定表格页名

工作原理：
  1. 读 CSV，按通道切出稳定段（去头 2 帧、按 0.1% 去离群），取中位计数
  2. 系数 K = 4930386.347 / 中位计数（1000.000 Ω = 4930386.347 计数）
  3. 在 adc_uid_calibration.h 末尾追加一组 UID + K（GROUP_COUNT 自动 +1）
  4. 在校准 xlsx 里复制最后一页为新页、写入数据与数字格式、填 uid 索引页
  5. 调 flash_md32.py 编译烧录并自动复位

依赖：
  - CSV 分析复用 D:\\code\\set\\seriportTest\\tools\\ 下的 calib_from_csv / pt1000
  - xlsx 读写走 editor_sdk（edsdk.py），要求该文件已在预览里打开；
    没开则本脚本会用 open_file 打开一个后台实例
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

# ---- 可复用的路径（改这几个常量即可换工程） --------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent
SERIAL_TOOLS = Path(r"D:\code\tset\seriportTest\tools")          # CSV 分析工具
EDSDK = (Path(r"C:\Users\77889\.workbuddy\plugins\cache\workbuddy-builtin"
              r"\skill-workbuddy-local-office-edit\5.9.3-wb.42398134.gd4692421.h480e8d1fee0f"
              r"\edsdk.py"))
XLSX = (Path(r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
             r"\temperature_8_channel - 副本\副本cs1237温度校准.xlsx"))
HEADER = (Path(r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
               r"\temperature_8_channel - 副本\temperature_8_channel - 副本"
               r"\Aplication\adc_uid_calibration.h"))
REF = 4930386.347
PY = sys.executable


# ------------------------------------------------------------------ CSV 分析
def analyze_csv(csv_path: Path) -> list[dict]:
    """返回 8 路：[{count, k, ohm, temp, n, ppm, seg}]"""
    sys.path.insert(0, str(SERIAL_TOOLS))
    import calib_from_csv as C   # noqa: E402
    import pt1000                 # noqa: E402
    pt1000.load_table()

    s, meta = C.read_stream(csv_path)
    pk = C.pair_packets(C.split_frames(s), meta)
    out = []
    for d in range(8):
        runs, cur = [], []
        for q in pk:
            if not q["adc"]:
                continue
            v = q["adc"][d]
            if 1e5 < v < 8000000:
                cur.append((q["time"], v))
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        best = None
        for seg in runs:
            seg = seg[2:]                                  # 去掉切换后的头 2 帧
            if len(seg) < 3:
                continue
            med0 = statistics.median([x[1] for x in seg])
            seg = [x for x in seg if abs(x[1] - med0) / med0 < 0.001]
            if len(seg) < 3:
                continue
            vs = [x[1] for x in seg]
            med = statistics.median(vs)
            ppm = (max(vs) - min(vs)) / med * 1e6
            if best is None or len(seg) > best["n"]:
                best = {"count": med, "n": len(seg), "ppm": ppm,
                        "seg": f"{seg[0][0][-12:]}~{seg[-1][0][-12:]} n={len(seg)}"}
        if best is None:
            raise SystemExit(f"dout{d + 1} 没有找到稳定段，请检查 CSV")
        k = REF / best["count"]
        ohm = 1000.0 / k
        out.append({**best, "k": k, "ohm": ohm, "temp": pt1000.ohm_to_temp(ohm)})
    return out


# ------------------------------------------------------------------ 固件写入
def patch_header(uid_bytes: str, rows: list[dict], tag: str) -> int:
    s = HEADER.read_text(encoding="utf-8", errors="surrogateescape")
    m = re.search(r"#define ADC_UID_CALIBRATION_GROUP_COUNT (\d+)U", s)
    idx = int(m.group(1))                       # 新组的下标
    s = s.replace(m.group(0), f"#define ADC_UID_CALIBRATION_GROUP_COUNT {idx + 1}U", 1)

    uid_c = ", ".join(f"0x{int(t, 16):02X}" for t in uid_bytes.split())
    uid_row = (f"\t/* [{idx}] {tag}：UID 尾字节 0x{int(uid_bytes.split()[-1], 16):02X}，"
               f"1000Ω 基准校准 */\n\t{{{uid_c}}}")
    # 在 UID 表的最后一个 "};" 之前插入
    end = s.index("};", s.index("static const uint8_t adc_uid_list"))
    s = s[:end] + ",\n" + uid_row + s[end:]        # 关键：补逗号，否则两组会粘在一起

    ks = ", ".join(f"{r['k']:.9f}f" for r in rows)
    k_row = (f"\t/* [{idx}] {tag}：下标 0~7 = dout1~dout8，增益误差 "
             f"{-max(r['k'] - 1 for r in rows) * 100:.2f}% ~ {-min(r['k'] - 1 for r in rows) * 100:.2f}% */\n"
             f"\t{{\n\t\t{ks}}}")
    end2 = s.index("};", s.index("static const float adc_uid_calibration_table"))
    s = s[:end2] + ",\n" + k_row + s[end2:]

    HEADER.write_text(s, encoding="utf-8", errors="surrogateescape")

    # 写完自检：回读文件，确认新组的 12 字节与输入逐字节一致
    back = HEADER.read_text(encoding="utf-8", errors="surrogateescape")
    if uid_c not in back:
        raise SystemExit(f"[固件] 自检失败：写入的 UID 行不是 {uid_c}")
    return idx


# ------------------------------------------------------------------ xlsx 写入
def edsdk_json(tool: str, payload: dict) -> dict:
    """调用 edsdk 并解析成 dict；失败返回 {}。"""
    raw = edsdk_call(tool, payload)
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def edsdk_call(tool: str, payload: dict) -> str:
    r = subprocess.run([PY, str(EDSDK), "call", tool, "--json",
                        json.dumps(payload, ensure_ascii=False)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or r.stderr).strip()


def next_page_name() -> str:
    """扫描表格里已有的 uidN 页名，返回下一个编号。"""
    info = edsdk_json("sheet_get_sheet_info", {"file_id": str(XLSX)})
    nums = []
    for s in info.get("sheets", []):
        m = re.fullmatch(r"uid(\d+)", s.get("name", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"uid{max(nums) + 1 if nums else 2}"


def patch_xlsx(uid_bytes: str, rows: list[dict], name: str | None, tag: str) -> None:
    if not EDSDK.exists():
        print("[表格] 找不到 edsdk.py，跳过 xlsx 写入")
        return
    uid_str = f"ID(Hex) : [{uid_bytes.upper()}]"

    info = edsdk_json("sheet_get_sheet_info", {"file_id": str(XLSX)})
    sheets = info.get("sheets", [])
    opened_here = False
    if not sheets:
        edsdk_call("open_file", {"file_path": str(XLSX), "wait": True})
        opened_here = True
        info = edsdk_json("sheet_get_sheet_info", {"file_id": str(XLSX)})
        sheets = info.get("sheets", [])
    if not sheets:
        print("[表格] 拿不到工作表列表，跳过 xlsx 写入")
        return
    if not name:                       # 页名必须等 sheets 到手再定，否则会退化成 uid2
        nums = [int(m.group(1)) for s in sheets
                if (m := re.fullmatch(r"uid(\d+)", s.get("name", "")))]
        name = f"uid{max(nums) + 1 if nums else 2}"

    last = sheets[-1]
    copied = edsdk_json("sheet_copy_sheet",
                        {"file_id": str(XLSX), "sheet_id": last["sheet_id"], "new_name": name})
    sid = copied.get("sheet_id", last["sheet_id"])
    if copied.get("name") != name:
        edsdk_call("sheet_rename_sheet", {"file_id": str(XLSX), "sheet_id": sid, "name": name})

    note = (f"换算基准：1000.000 Ω = {REF:.3f} 计数；芯片ADC 取自 CSV 稳定段"
            f"（{tag}，8 路全测，中位）；温度列为 PT1000 真值")
    vals = [
        {"row": 0, "col": 0, "value_type": "STRING", "string_value": name},
        {"row": 0, "col": 1, "value_type": "STRING", "string_value": uid_str},
        {"row": 0, "col": 7, "value_type": "STRING", "string_value": note},
    ]
    for i, r in enumerate(rows):
        ri = 2 + i
        vals += [
            {"row": ri, "col": 0, "value_type": "STRING", "string_value": f"dout{i + 1}"},
            {"row": ri, "col": 1, "value_type": "NUMBER", "number_value": REF},
            {"row": ri, "col": 2, "value_type": "NUMBER", "number_value": r["count"]},
            {"row": ri, "col": 3, "value_type": "NUMBER", "number_value": r["k"]},
            {"row": ri, "col": 4, "value_type": "NUMBER", "number_value": r["ohm"]},
            {"row": ri, "col": 5, "value_type": "NUMBER", "number_value": r["temp"]},
            {"row": ri, "col": 6, "value_type": "STRING", "string_value": r["seg"]},
        ]
    edsdk_call("sheet_set_range_value", {"file_id": str(XLSX), "sheet_id": sid, "values": vals})
    for col, pat in [(1, "0.000"), (2, "0"), (3, "0.000000000"), (4, "0.000"), (5, "0.00")]:
        edsdk_call("sheet_set_cell_style", {"file_id": str(XLSX), "sheet_id": sid,
                                            "start_row": 2, "end_row": 21,
                                            "start_col": col, "end_col": col,
                                            "format": {"number_format_pattern": pat}})
    idx_row = len(sheets)                      # uid 索引页的行号（0-based）
    edsdk_call("sheet_set_range_value",
               {"file_id": str(XLSX), "sheet_id": sheets[0]["sheet_id"],
                "values": [{"row": idx_row, "col": 1, "value_type": "STRING", "string_value": uid_str}]})
    edsdk_call("save_file", {"file_id": str(XLSX), "file_path": str(XLSX)})
    if opened_here:                      # 只关自己开的，别动用户预览
        edsdk_call("close_file", {"file_id": str(XLSX)})
    print(f"[表格] 已写入 {name} 页与 uid 索引页")


# ------------------------------------------------------------------ 主流程
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="新芯片校准一条龙")
    ap.add_argument("--uid", required=True, help='12 字节 UID，空格分隔，如 "3C 12 3B CC ..."')
    ap.add_argument("--csv", required=True, help="采集到的 CSV 文件")
    ap.add_argument("--name", help="表格页名，默认 uid<N+1>")
    ap.add_argument("--dry-run", action="store_true", help="只算不改")
    ap.add_argument("--no-table", action="store_true", help="不写 xlsx")
    ap.add_argument("--no-flash", action="store_true", help="不编译烧录")
    ap.add_argument("--force", action="store_true", help="数据可疑时仍然烧录")
    args = ap.parse_args(argv)

    uid = " ".join(args.uid.split())
    if len(uid.split()) != 12:
        raise SystemExit("--uid 必须是 12 字节")
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV 不存在：{csv_path}")

    rows = analyze_csv(csv_path)
    tag = csv_path.stem

    print(f"\n{'通道':<7}{'中位计数':>12}{'帧数':>6}{'极差ppm':>9}{'系数 K':>16}{'Ω':>10}{'℃':>8}")
    for i, r in enumerate(rows):
        print(f"dout{i + 1:<3}{r['count']:>12.0f}{r['n']:>6}{r['ppm']:>9.0f}"
              f"{r['k']:>16.9f}{r['ohm']:>10.3f}{r['temp']:>8.2f}")

    # 数据体检
    bad = []
    for i, r in enumerate(rows):
        why = []
        if abs(r["ohm"] - 1000.0) > 50:      # 5%：正常增益误差 1~2% 不该报警
            why.append(f"电阻偏 {r['ohm'] - 1000:+.1f}Ω（{r['ohm']:.1f}）")
        if r["ppm"] > 600:
            why.append(f"极差 {r['ppm']:.0f}ppm 偏大")
        if r["n"] < 4:
            why.append(f"仅 {r['n']} 帧")
        if why:
            bad.append((i + 1, why))
    if bad:
        print("\n[警告] 以下通道数据可疑，多半是接触/接线问题：")
        for ch, why in bad:
            print(f"  dout{ch}: " + "；".join(why))

    if args.dry_run:
        print("\n[dry-run] 不做任何改动")
        return 0

    idx = patch_header(uid, rows, tag)
    print(f"[固件] 已追加 [{idx}] 组（GROUP_COUNT → {idx + 1}）")

    if not args.no_table:
        patch_xlsx(uid, rows, args.name, tag)

    if bad and not args.force:
        print("[烧录] 数据可疑且未加 --force，跳过烧录（固件与表格已写入）")
        return 0

    if not args.no_flash:
        print("[烧录] 调用 flash_md32.py …")
        rc = subprocess.run([PY, str(SCRIPTS_DIR / "flash_md32.py")]).returncode
        if rc != 0:
            print(f"[烧录] flash_md32.py 退出码 {rc}（2=编译错 4=烧录失败 5=没跑起来）")
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
