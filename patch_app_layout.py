#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把应用工程切换到「A 方案」分区（可回滚）

改动：
    1. md32.uvprojx  IROM1: 0x08000000/0x40000 → 0x08004000/0x1C000
    2. Aplication/main.c
       - 开头设 SCB->VTOR = APP_ADDR（中断向量表偏移，必须早于 systick_config）
       - 启动自检成功时写「确认」标志（配合 Boot 的试运行机制）
       - 0x05 命令改为「写升级请求标志 + 复位」，不再直接跳 Flash 末尾
    3. 自动备份 md32.uvprojx / md32.uvoptx / main.c 为 *.bak_bootA

用法：
    python patch_app_layout.py --status
    python patch_app_layout.py --apply
    python patch_app_layout.py --restore      # 一键回到改造前
    python patch_app_layout.py --project "…\\md32.uvprojx"

注意：--apply 之后必须 ./flash_cli.sh --build-only 验证编译；
      上线时还要把 bootloader/build/boot.bin 烧到 0x08000000（见 README_A.md）。
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

DEFAULT_PROJECT = Path(
    r"D:\xwechat_files\wxid_oooc559wabci22_f1da\msg\file\2026-10"
    r"\temperature_8_channel - 副本\temperature_8_channel - 副本\md32.uvprojx"
)
SUFFIX = ".bak_bootA"
NEW_IROM_START = "0x8004000"
NEW_IROM_SIZE = "0x1C000"

HELPERS = '''
/* ---------------- A 方案：Bootloader 分区 + 升级请求（patch_app_layout.py 生成） -------- */
#include "../bootloader/boot_flag.h"

static void boot_flag_write(const boot_flag_t *f)
{
	unsigned char i;
	fmc_unlock();
	fmc_flag_clear(FMC_FLAG_BANK0_END | FMC_FLAG_BANK0_WPERR | FMC_FLAG_BANK0_PGERR);
	fmc_page_erase(FLAG_ADDR);
	fmc_flag_clear(FMC_FLAG_BANK0_END | FMC_FLAG_BANK0_WPERR | FMC_FLAG_BANK0_PGERR);
	for (i = 0; i < sizeof(boot_flag_t); i += 4)
	{
		fmc_word_program(FLAG_ADDR + i, *(uint32_t *)((unsigned char *)f + i));
		fmc_flag_clear(FMC_FLAG_BANK0_END | FMC_FLAG_BANK0_WPERR | FMC_FLAG_BANK0_PGERR);
	}
	fmc_lock();
}

/* 应用自检通过：把 CONFIRM_PENDING 清成 IDLE，告诉 Boot 新固件可用 */
static void boot_confirm_app(void)
{
	boot_flag_t f = *(boot_flag_t *)FLAG_ADDR;
	if ((f.magic == FLAG_MAGIC) && (f.state == ST_CONFIRM_PEND))
	{
		f.state = ST_IDLE;
		f.attempts = 0;
		f.xor_check = boot_flag_xor(&f);
		boot_flag_write(&f);
	}
}

/* 收到 0x05：写「请求升级」标志并复位，Boot 上电后留在升级模式 */
static void boot_request_upgrade(void)
{
	boot_flag_t f;
	f.magic = FLAG_MAGIC;
	f.state = ST_RECEIVING;
	f.fw_len = 0;
	f.fw_crc = 0;
	f.fw_version = 0;
	f.received = 0;
	f.attempts = 0;
	f.xor_check = boot_flag_xor(&f);
	boot_flag_write(&f);
	NVIC_SystemReset();
}
/* -------------------------------------------------------------------------------------- */

'''


def read_gbk(p: Path) -> str:
    return p.read_bytes().decode("gbk", "surrogateescape")


def write_gbk(p: Path, s: str) -> None:
    p.write_bytes(s.encode("gbk", "surrogateescape"))


def backup(paths: list[Path]) -> None:
    for p in paths:
        b = p.with_suffix(p.suffix + SUFFIX)
        if not b.exists():
            shutil.copy2(p, b)
            print(f"[备份] {b.name}")


def status(proj: Path) -> int:
    s = proj.read_text(encoding="utf-8", errors="surrogateescape")
    m = re.search(r"<OCR_RVCT4>\s*<Type>1</Type>\s*<StartAddress>([^<]+)</StartAddress>\s*<Size>([^<]+)</Size>", s)
    print(f"工程: {proj}")
    if m:
        print(f"IROM1: start={m.group(1)} size={m.group(2)}")
        print("状态:", "A 方案（应用 0x08004000）" if m.group(1).lower() == NEW_IROM_START.lower()
              else "原分区（应用 0x08000000）")
    mc = read_gbk(proj.parent / "Aplication" / "main.c")
    print("main.c 含 VTOR 偏移:", "SCB->VTOR = APP_ADDR" in mc)
    print("备份存在:", all((p.with_suffix(p.suffix + SUFFIX)).exists() for p in
                       [proj, proj.parent / "Aplication" / "main.c"]))
    return 0


def apply(proj: Path) -> int:
    root = proj.parent
    mainc = root / "Aplication" / "main.c"
    uvopt = proj.with_suffix(".uvoptx")
    paths = [proj, uvopt, mainc]
    backup(paths)

    s = proj.read_text(encoding="utf-8", errors="surrogateescape")
    # uVision 的 --ro-base 取 <IROM>，<OCR_RVCT4> 也要同步，两处都改
    pats = [
        re.compile(r"(<IROM>\s*<Type>1</Type>\s*<StartAddress>)[^<]+(</StartAddress>\s*<Size>)[^<]+(</Size>)"),
        re.compile(r"(<OCR_RVCT4>\s*<Type>1</Type>\s*<StartAddress>)[^<]+(</StartAddress>\s*<Size>)[^<]+(</Size>)"),
    ]
    changed = 0
    for pat in pats:
        s, n = pat.subn(rf"\g<1>{NEW_IROM_START}\g<2>{NEW_IROM_SIZE}\g<3>", s, count=1)
        changed += n
    # LDads/TextAddressRange 才是 --ro-base 的直接来源
    s, n = re.subn(r"(<TextAddressRange>)[^<]+(</TextAddressRange>)",
                   rf"\g<1>0x08004000\g<2>", s, count=1)
    changed += n
    if changed == 0:
        raise SystemExit("[错误] 没找到 IROM / OCR_RVCT4 内存配置")
    proj.write_text(s, encoding="utf-8", errors="surrogateescape")
    print(f"[改动] IROM 两处 → {NEW_IROM_START} / {NEW_IROM_SIZE}")

    mc = read_gbk(mainc).replace("\r\n", "\n")
    if "SCB->VTOR = APP_ADDR" in mc:
        print("[跳过] main.c 已打过补丁")
        return 0

    anchor = "int main(void)"
    assert anchor in mc
    mc = mc.replace(anchor, HELPERS + anchor, 1)

    mc = mc.replace('''int main(void)
{
	/* initialize the LEDs */''', '''int main(void)
{
	/* A 方案：应用链接到 0x08004000，向量表必须重定位；
	 * 且要早于 systick_config()（它会开 SysTick 中断）。 */
	SCB->VTOR = APP_ADDR;
	boot_confirm_app();            /* 新固件自检通过，清除待确认标志 */

	/* initialize the LEDs */''', 1)

    mc = mc.replace("\t\t\t\tiap_jump_to(IAP_UPDATER_ADDR);", "\t\t\t\tboot_request_upgrade();", 1)
    mc = mc.replace("/* 进入串口升级：回 ACK 后跳到升级器 */",
                    "/* 进入升级：回 ACK 后写标志 + 复位，由 0 区 Boot 接管 */", 1)

    write_gbk(mainc, mc.replace("\n", "\r\n"))
    print("[改动] main.c：VTOR / boot_confirm_app / 0x05 改走 Boot 标志")
    return 0


def restore(proj: Path) -> int:
    for rel in [proj.name, proj.with_suffix(".uvoptx").name, "Aplication/main.c"]:
        p = proj.parent / rel
        b = p.with_suffix(p.suffix + SUFFIX)
        if b.exists():
            shutil.copy2(b, p)
            print(f"[还原] {p.name}")
        else:
            print(f"[跳过] 没有备份 {b.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="应用工程切到 A 方案分区（可回滚）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", action="store_true")
    ap.add_argument("--project", default=str(DEFAULT_PROJECT))
    args = ap.parse_args()

    proj = Path(args.project)
    if not proj.exists():
        raise SystemExit(f"[错误] 找不到工程 {proj}")
    if args.status:
        return status(proj)
    if args.apply:
        return apply(proj)
    return restore(proj)


if __name__ == "__main__":
    raise SystemExit(main())
