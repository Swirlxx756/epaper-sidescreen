# 三色驱动的厂商来源

`src/epd_tri.cpp`（4.2 寸三色屏驱动）按微雪（Waveshare）公开驱动重新实现了命令时序，没有直接拷贝厂商源文件。本文是查证用的来源表：第 1 节列来源，第 2 节说明每个行为对应哪份厂商代码，第 3、4 节说明与厂商不一致的地方。只想用固件的话，看 [`README.md`](README.md) 第 9 节即可。

名词：**路径**是驱动使用的一套命令（`v2_new`、`v2_old`、`bc_v1`）；**RY 平面**是红/黄色那一层图像数据；**BUSY** 是屏幕忙信号。

## 1. 来源

| 简称 | 来源 | 版本 / 获取方式 | 许可 |
|---|---|---|---|
| **GH-C** | `waveshareteam/e-Paper` 仓库 `RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in2b_V2.c`（文件头 V1.0, 2020-11-25） | GitHub master，提交 `d28b3c4cfc78eef1c6cd6e6e3013654e449d2864`（2026-10-27），<https://github.com/waveshareteam/e-Paper/blob/d28b3c4cfc78eef1c6cd6e6e3013654e449d2864/RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in2b_V2.c> | 文件头为 MIT 式许可（“Permission is hereby granted, free of charge…”） |
| **GH-BC** | 同仓库 `RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in2bc.c` | 同上提交 | 同上 |
| **GH-PY** | 同仓库 `RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd4in2b_V2.py` | 同上提交 | 同上 |
| **GH-ARD** | 同仓库 `Arduino/epd4in2b_V2/epd4in2b_V2.cpp`、`epdif.cpp`（Copyright Waveshare, Nov 25 2020） | 同上提交 | 同上 |
| **ESP32-PKG** | 微雪 E-Paper ESP32 Driver Board 官方示例包 `esp32-waveshare-epd`：`EPD_4in2b_V2.cpp`、`EPD_4in2b_V2_old.cpp`、`EPD_4in2bc.cpp`、`DEV_Config.cpp` | 没有重新读取该包原文；下表中 ESP32-PKG 的行号来自早期的逐行核对。官方下载入口在微雪 Wiki 的 E-Paper ESP32 Driver Board 页面 | 同上（微雪示例） |
| **WIKI** | 微雪 Wiki “4.2inch e-Paper Module (B) Manual”，<https://www.waveshare.com/wiki/4.2inch_e-Paper_Module_(B)_Manual> | 2026-10-28 访问 | — |

## 2. 行为与依据

| 行为 | 实现 | 依据 |
|---|---|---|
| 引脚 SCK13 / DIN14 / CS15 / DC27 / RST26 / BUSY25 | `config.h` `BOARD_WAVESHARE_ESP32` | ESP32-PKG `DEV_Config.h:47-52` |
| 软件 SPI，模式 0，MSB 先，每字节单独拉 CS | `EpdTri::writeByte` | GH-ARD `epdif.cpp` `EPD_SendData()`；ESP32-PKG `DEV_SPI_WriteByte()` |
| 读回：DIN 切成输入，SCK 低电平期间采样，8 位 | `EpdTri::readByte` | GH-ARD `epdif.cpp` `EPD_ReadData()`；ESP32-PKG `DEV_SPI_ReadByte()` |
| 自动选择：复位 → DC=0 发 `0x2F` → 等 50 ms → DC=1 读 1 字节；`0x01` 走新路径，其他走旧路径 | `EpdTri::begin` / `probe` | GH-C `EPD_4IN2B_V2_Init()`；GH-ARD `Epd::Init()`；ESP32-PKG `EPD_4in2b_V2.cpp:185-209` |
| 复位：高 200 ms → 低 N → 高 200 ms；V2 N=2 ms | `EpdTri::reset` | GH-C / GH-PY / GH-ARD `Reset()` |
| B/C V1 复位低电平 200 ms | 同上 | ESP32-PKG `EPD_4in2bc.cpp:91-99`（GH-BC 为 2 ms，可用 `EPD_TRI_RESET_LOW_MS` 覆盖） |
| v2_new 初始化：等 BUSY、`0x12`、等 BUSY、`0x3C 05`、`0x18 80`、`0x11 03`、`0x44 00 31`、`0x45 00 00 2B 01`、`0x4E 00`、`0x4F 00 00`、等 BUSY | `initPanel` | GH-C `EPD_4IN2B_V2_Init_new()`、GH-PY `init()`（flag=1 分支） |
| v2_old 初始化：等 BUSY、`0x04`、等 BUSY、`0x00 0F` | `initPanel` | GH-C `EPD_4IN2B_V2_Init_old()` |
| bc_v1 初始化：`0x06 17 17 17`、`0x04`、等 BUSY、`0x00 0F` | `initPanel` | GH-BC `EPD_4IN2BC_Init()` |
| v2_new BUSY：高电平=忙，每 10 ms 查一次 | `busyNow` / `waitIdle` | GH-C `ReadBusy_new()` |
| v2_old BUSY：每次先发 `0x71`、等 20 ms，低电平=忙；结束后再等 20 ms | 同上 | GH-C `ReadBusy_old()`；ESP32-PKG `EPD_4in2b_V2.cpp:80-89` |
| bc_v1 BUSY：低电平=忙，每 100 ms 查一次 | 同上 | GH-BC `EPD_4IN2BC_ReadBusy()` |
| v2_new 写图：`0x24` 黑、`0x26` 发 `~ry`；刷新 `0x22 F7` + `0x20` | `display` | GH-C `Display_new()`；GH-PY；GH-ARD；ESP32-PKG `:312-329` |
| v2_old 写图：`0x10` 黑、`0x13` 发 `~ry`（默认）；刷新 `0x12` + 100 ms | `display` | GH-C `Display_old()`、GH-PY；ESP32-PKG `EPD_4in2b_V2.cpp:287-303` |
| v2_old 可选：`0x13` 发原始 `ry`（`EPD_TRI_V2_OLD_RY_INVERT=0`） | `display` | ESP32-PKG `EPD_4in2b_V2_old.cpp:295`；GH-ARD `Display_old()` |
| bc_v1 写图：`0x10` 黑、`0x13` 发原始 `ry`；刷新 `0x12` + 100 ms | `display` | GH-BC `EPD_4IN2BC_Display()` |
| 休眠 v2_new：`0x10 03` | `sleepPanel` | GH-C `Sleep_new()`（GH-ARD 为 `0x01`，未采用） |
| 休眠 v2_old：`0x50 F7`、`0x02`、等 BUSY、`0x07 A5` | `sleepPanel` | GH-C `Sleep_old()` |
| 休眠 bc_v1：`0x02`、等 BUSY、`0x07 A5` | `sleepPanel` | GH-BC `EPD_4IN2BC_Sleep()` |
| 平面约定：1=白、0=着色；`ry` 为 0 的像素显示红/黄 | `tri_planes.h` | 厂商 Paint 默认 `0xFF` 白底、Demo `RYImage` 注释“Red or Yellow” |
| 每次刷新：复位 → 初始化 → 写两平面 → 全刷 → 深睡眠 | `display` | WIKI：不刷新时要让屏幕休眠或断电，休眠后需重新初始化 |
| 刷新约 15 s | 刷新超时 45 s；`busy_ms<1000` 时输出警告 | WIKI 规格表 “Refresh time: 15s” |
| 两次刷新间隔 ≥180 s；至少 24 h 刷新一次 | `EPD_TRI_MIN_REFRESH_MS`、`EPD_TRI_MAX_IDLE_MS` | WIKI 注意事项 |

## 3. 两份 V2 旧路径的 RY 极性不一致

- **发 `~ry`，清屏时 `0x13` 写 `0x00`**：ESP32-PKG `EPD_4in2b_V2.cpp`、GH-C、GH-PY。
- **发原始 `ry`，清屏时 `0x13` 写 `0xFF`**：ESP32-PKG `EPD_4in2b_V2_old.cpp`、GH-ARD。

两种写法各自是自洽的：反相时“全白”对应 `0x00`，不反相时对应 `0xFF`。本固件只保留一份平面数据，发送时统一按同一个开关处理，所以不会出现“图像反相、背景不反相”的情况。

`config.h` 的通用默认取 `1`（反相），理由是厂商 ESP32 主线文件 `EPD_4in2b_V2.cpp` 和 GitHub 上的两种参考实现（C、Python）都这样写。走 v2_old 路径时红/黄区域和白底颠倒，就把开关设为 `0`。固件不会根据 `_old` 文件名自动切换，也不会自动尝试另一种极性。

### 3.1 这块屏上的实测

这块屏用旧默认（`auto_v2`，读回 `0xFF` ×3 走 `v2_old` 兜底，`ry_invert=1`）时，显示整屏红色，没有图文。白底时 RY 平面全是 `0xFF`，反相后 `0x13` 几乎全部送出 `0x00`。同样使用 `0x04`/`0x00 0F`/`0x10`/`0x13`/`0x12` 旧命令集的 `EPD_4in2bc` 和 `EPD_4in2b_V2_old` 都以 `0xFF` 作为 `0x13` 的白色清屏值，按它们的约定，这组字节恰好对应整屏着色。

改成固定 `v2_old` + `ry_invert=0` 后，校准图和仪表盘都显示正常。所以正式 env `waveshare_esp32_42tri` 用这一组参数。推测原因是这块屏的旧路径 RY 极性按原始 `ry` 解释，但没有进一步证实。诊断 env 是 `waveshare_esp32_42tri_calib`，见 `README.md` 9.5a。

## 4. 与厂商实现的差异

1. 所有等待（复位延时、BUSY、`0x2F` 后的 50 ms）都改为“每毫秒服务一次串口”，并且有上限；厂商代码是阻塞的 `while`，没有上限。
2. AUTO 模式默认连读 3 次 `0x2F`，3 次结果必须相同（`EPD_TRI_PROBE_READS`，设为 1 就和厂商一样只读一次）。结果不一致时报 `probe_unstable` 故障并停止刷新，不会猜测走哪条路径。
3. 读回只在启动时做一次，之后每次刷新都沿用同一路径。厂商代码在每次 `Init()` 都会读回一次。
4. 没有实现局部刷新：厂商这三套三色例程都只有全屏刷新。
