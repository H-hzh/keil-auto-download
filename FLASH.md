# md32 命令行：编译 → 烧录（默认全烧 Boot+App）→ 复位（默认不读串口）

一条命令跑完整个下载流程，不用打开 Keil 界面。
**默认行为 = 全烧**：把 0 区 Boot 和 App 一起合并后，经 PWLINK2/CMSIS-DAP 下载器全片擦除+烧录。
加 `--app`（Git Bash 用 `app`）退回旧的"只烧应用区"模式。

| 文件 | 说明 |
|---|---|
| `flash_md32.py` | 主程序：解析工程 → 设定型号 → 编译 → 烧录 → 复位（`--verify` 才做串口校验）|
| `flash_cli.bat` | cmd / 双击用启动器 |
| `flash_cli.sh` | Git Bash / MSYS2 用启动器（把字面量 `app` 自动翻成 `--app`）|
| `dap_reset.py` | 通过 CMSIS-DAP v2 复位目标（绕过 uVision -f 不复位）；被主脚本自动调用 |
| `make_combined.py` | 合并 0 区 Boot + App 成 `Objects/combined.{hex,bin}`；被主脚本在全烧模式下自动调用 |
| `patch_app_layout.py` | 改应用的 IROM/VTOR/0x05 请求升级开关（`--apply` 应用 / `--restore` 回滚，带 `.bak_bootA` 备份）|
| `add_chip.py` | 新芯片标定一条龙：CSV → 算系数 → 写 `adc_uid_calibration.h` → 加 xlsx 页 → 烧录（`--uid/--csv`，`--dry-run` 只算不改，`--no-flash` 跳烧）|
| `addr_tool.py` / `addr_tool.bat` / `addr_tool.sh` | 485 改地址 / 读写参数区 / OTA 升级（`ota` / `install-updater` 子命令）|
| `last_project.txt` | 自动生成，记录上次用的工程（可删） |

## 前置条件（一次配好，长期有效）

| 项 | 要求 |
|---|---|
| Keil | `D:\keil\Keil_v5\UV4\UV4.exe`（AC5 V5.06） |
| Python | `D:\ux-venv\Scripts\python.exe`（需 pyserial） |
| 探头 | PWLINK2，设备名 `#PW_WINUSB_CMSIS-DAP` |
| 工程调试器 | Options for Target → Debug → **CMSIS-DAP Debugger**（本工程已配好） |
| 探头序列号 | `md32.uvoptx` 里 `CMSIS_AGDI` 那条的 `-U<32位序列号>` 必须与当前探头一致。<br>不一致会报 *No Debug Unit is selected*，改法见下文 |
| PowerWriter | **必须断开或关闭**，探头是独占的 |
| 复位 | 由 `dap_reset.py` 走 CMSIS-DAP 自动完成，不需要勾 Reset and Run（见下） |

## 用法

脚本放在 `D:\code\keil\脚本\md32\`，**会自动记住上次用过的工程**，所以日常不用带任何参数：

```bat
:: cmd / 双击
flash_cli.bat                       :: 编译+烧录+复位（上次的工程，没有则内置默认）
flash_cli.bat --build-only          :: 只编译
flash_cli.bat --chip GD32F303CC     :: 显式指定型号
flash_cli.bat --wait 60             :: 手动复位时等 60s
flash_cli.bat "D:\别的工程\foo.uvprojx"   :: 换工程，并记为下次默认
flash_cli.bat --forget              :: 忘掉上次记录，回到内置默认
```

```bash
# Git Bash / MSYS2：不能直接敲 flash_cli.bat，用 .sh
cd /d/code/keil/脚本/md32
./flash_cli.sh --build-only
./flash_cli.sh "/d/code/keil/xxx/foo.uvprojx"      # 换工程后自动记住
```

> 在 Git Bash 里直接敲 `flash_cli.bat` 会报 `command not found`——bash 不在当前目录找命令。
> 加 `./` 就行：`./flash_cli.bat`（Git 会转交 cmd 执行），或用 `./flash_cli.sh`。

把 `D:\code\keil\脚本\md32` 加进 PATH 后，cmd 里直接敲 `flash_cli` 即可。
`.bat` / `.sh` 都只是 `flash_md32.py` 的启动器，参数原样透传。

> **烧录范围**：默认是**全烧**（Boot+App 一起经下载器全片擦除+烧录）；
> 只想更新应用、保留参数区，用 `./flash_cli.sh app`（Git Bash）或 `flash_md32.py --app`（cmd / 直接调 .py）。

### 记住上次工程

- 只要**显式传了工程**（目录或 `.uvprojx`），就写进脚本目录的 `last_project.txt`；
- 下次不带参数运行时优先用它，文件不存在或路径失效才回落到 `DEFAULT_PROJECT`；
- 输出里会标明来源：`（上次使用）` / `（内置默认）` / `（命令行指定，已记为上次）`；
- `--forget` 删除记录。整个状态就是一个纯文本文件，删掉即可重置。

### 参数

| 参数 | 说明 |
|---|---|
| `project` | 可选。工程目录或 `.uvprojx` 路径；省略则用上次的工程，再没有才用内置默认 |
| `--chip` | 设定 `<Device>`，默认沿用工程里的（当前 `GD32F303CC`）<br>只改型号字段，**Flash 算法/启动文件/宏要自己核对** |
| `--serial` / `--baud` | 运行校验用串口，默认 `COM10` / `115200` |
| `--no-build` | 跳过编译直接烧 |
| `--app` | 退回旧"应用模式"：只编译并用 UV4(-f) 烧录应用区（经 DAP 探头，BOOT0 低）。<br>**默认是全烧**：编译 md32 → 编译 0 区 Boot → 合并成 `Objects/combined` → 生成 merged 工程经 PWLINK2/CMSIS-DAP 下载器全片擦除+烧录 |
| `--build-only` / `--no-flash` | 只编译 |
| `--verify-only` | 不编译不烧录，只做复位+串口校验 |
| `--verify` | 烧完额外做串口运行校验（默认不做，只复位） |
| `--no-verify` | 兼容旧用法，等同默认行为 |
| `--wait` | 等目标启动的最长秒数，默认 30 |
| `--strict` | 编译有警告也算失败 |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 编译错误 |
| 3 | `--strict` 下出现警告 |
| 4 | 烧录失败 / 日志没有 `Verify OK` |
| 5 | 烧录成功但目标没跑起来（复位未完成） |
| 6 | 工程、UV4 路径等前置条件不满足 |

## 内部做了什么

### 默认（全烧 Boot+App）

1. `UV4 -b <prj>` 编译应用（md32），解析 `N Error(s), M Warning(s)`
2. 若工程里有 `bootloader/build_boot.py`，先用 armcc(AC5) 重新编译 0 区 Boot → `bootloader/build/boot.bin`
3. `make_combined.py --boot boot.bin --app Objects/md32.hex --out Objects` → 合并出 `Objects/combined.{hex,bin}`
4. 基于 uvprojx 生成临时 **merged 工程**（`combined_flash.uvprojx` + `combined_flash.s` + `combined_flash.sct`），
   用 INCBIN 把 `combined.bin` 拼到 `0x08000000` 覆盖整片 256KB（同时去掉 RTE，避免 DFP 外设源被编进镜像）
5. `UV4 -b combined` 出 `combined.axf` → `UV4 -f combined` 经 PWLINK2/CMSIS-DAP **全片擦除+烧录**，
   日志必须同时出现 `Programming Done` 和 `Verify OK`
6. `dap_reset.py` 发 SYSRESETREQ 复位（默认不读串口；要校验加 `--verify`）

> ⚠ 全片擦除会清掉 **FLAG 和 PARAM 区**：多片板子地址会变回 1，校准也被清。
> 烧完请用上位机（`addr_tool.py`）重新设地址、必要时重烧标定。详见下方"全片擦除副作用"。

### `--app`（旧应用模式，只烧应用区）

1. `UV4 -b <prj>` 编译应用
2. `UV4 -f <prj>` 只把应用烧进 `0x08004000`（需 DAP 探头、BOOT0 低），日志需 `Programming Done` + `Verify OK`
3. `dap_reset.py` 复位；若带 `--verify`，烧完读 3 秒温度帧确认 a~h 八路有数据
   （串口被 seriportTest 的服务占着时，自动走 `http://127.0.0.1:8766` 的 HTTP 接口读帧，不用抢端口）

## 全片擦除副作用（全烧模式）

全烧走的是 `UV4 -f` 对整片 256KB 的擦除+编程，**FLAG 和 PARAM 区一并被清掉**：

- **地址**：多片板子的从机地址会变回 `1`（PARAM 区首字节）。
- **校准**：参数区里的标定值同样被清，烧完需重新标定 / 写入。

脚本烧完会打印这条警告，但仍会继续完成复位。这是全片烧录的物理副作用，无法避免——
想保留参数区就改用 `--app`（只擦应用区），或接受烧完用 `addr_tool.py` 重设一次。

## 关于"复位"（已自动化）

**结论：uVision 的 `UV4 -f` 在批处理模式下不会执行 Reset and Run。**
你在 Keil 界面里勾选、甚至按 F8 能自动运行，但命令行 `-f` 走完 `Verify OK` 就退出，
内核停在停止态——这不是配置问题，是 uVision 命令行自身的限制（`-f` 的日志里也不会出现
`Application running ...`）。

所以脚本改为**自己复位**：烧录成功后调用同目录的 `dap_reset.py`，通过 PWLINK2 的
CMSIS-DAP v2 接口（WinUSB）直接给内核写 `AIRCR = 0x05FA0004`（SYSRESETREQ）。
实测烧完 1 秒内串口就出帧，全程不需要人工干预。

```
[烧录] Erase Done.Programming Done.Verify OK.
[复位] uVision 下载后不复位，改用 CMSIS-DAP 发 SYSRESETREQ…
[复位] [目标] IDCODE=0x2BA01477
[复位] [复位] SYSRESETREQ(复位并运行)  成功
[校验] 3s 内 3 帧；通道 a~h 温度：...
```

`dap_reset.py` 的实现要点（踩过的坑）：

| 坑 | 说明 |
|---|---|
| 接口必须 `FILE_FLAG_OVERLAPPED` 打开 | 否则 `WinUsb_Initialize` 返回 `err=6`（ERROR_INVALID_HANDLE） |
| 接口 GUID | `{CDB3B5AD-293B-4663-AA36-1AAE46463776}`（CMSIS-DAP v2，从注册表 `DeviceInterfaceGUIDs` 查到） |
| **AIRCR 不是 DP 寄存器** | `0xE000ED0C` 属内核系统寄存器，必须走 MEM-AP：先写 `TAR`（AP 0x04），再写 `DRW`（AP 0x0C）。直接写 DP 的 0x0C 是 RDBUFF，**等于什么都没做** |
| 上电请求 | 写 `CTRL/STAT = 0x50000000`（CSYSPWRUPREQ\|CDBGPWRUPREQ）再操作 |

单独用：

```bash
python dap_reset.py          # 复位并运行
python dap_reset.py --halt   # VECTRESET 后停住
python dap_reset.py --info   # 只看探头/目标信息
```

**回退路径**：若 `dap_reset.py` 不存在或失败，脚本仍按老逻辑尝试 DTR/RTS 脉冲
（这块板没接 NRST，无效），最后提示手动按复位键并等 `--wait` 秒。

## 常见报错对照

| 现象 | 原因 / 处理 |
|---|---|
| `Flash Download failed - Target DLL has been cancelled` | 探头被占用：关掉 PowerWriter，或它还在连着目标 |
| `No Debug Unit is selected` | `uvoptx` 的 `-U` 序列号与当前探头不符。查实际序列号：<br>`Get-PnpDevice -PresentOnly \| ? InstanceId -like '*VID_0D28*'`<br>取 `USB\VID_0D28&PID_0204\<序列号>` 里的 32 位串，替换 `-U` 后的值 |
| 编译 0 error 但烧进去没反应 | 检查是否只擦除没编程：日志应同时有 `Programming Done` 和 `Verify OK`；<br>只有 `Full Chip Erase Done` 说明 `-FO` 字段被改坏了，恢复 `-FO7` |
| 烧完串口没数据 | 内核停在复位态，见上文"关于复位" |

## 校准相关提醒

`Aplication/adc_uid_calibration.h` 里每组芯片的 8 个系数是**乘在原始 ADC 上**的。
如果这一轮是要**采集原始数据重算系数**，必须先把该组系数改回 `1.0f`（直通），
否则采到的是补偿后的值，再算会重复补偿。采完填入新 K 后再烧。
