# ESP32 墨水屏副屏：接线与使用指南

做完后，墨水屏会显示电脑的 CPU / 内存 / 磁盘 / 网速，以及 AI token 用量。

**先看你的屏幕：**

- **黑白屏**（4.2 / 2.9 / 7.5 寸）：照本指南第 1 到 7 节做。
- **4.2 寸三色屏**（黑白红或黑白黄）：**不要按本指南烧黑白固件**，改看仓库根目录的 [`README.md`](../README.md)（烧录、运行）和 [`firmware/README.md`](../firmware/README.md) 第 9 节（三色屏参数和刷新规则）。
- 其他尺寸或颜色：本项目没有对应驱动，先不要烧录。

本文命令里的 `%USERPROFILE%\Desktop\Plugin\epaper-sidescreen` 是仓库目录，换成你自己的路径。

## 目录

- [0. 开始之前](#0-开始之前)
- [1. 确认屏幕型号（本设备可跳过）](#1-确认屏幕型号本设备可跳过)
- [2. 路线 A：微雪驱动板（不接线）](#2-路线-a微雪驱动板不接线)
- [3. 路线 B：ESP32-S3 + 转接板（接 8 根线）](#3-路线-besp32-s3--转接板接-8-根线)
- [4. 安装 VS Code 和 PlatformIO](#4-安装-vs-code-和-platformio)
- [5. 编译并烧录固件](#5-编译并烧录固件)
- [6. 运行电脑端程序](#6-运行电脑端程序)
- [7. 屏幕上看什么、怎么刷新](#7-屏幕上看什么怎么刷新)
- [8. 出问题怎么办](#8-出问题怎么办)
- [9. 附录](#9-附录)

---

## 0. 开始之前

### 0.1 工作方式

1. 电脑上的 Python 程序（`host/epaper_host.py`）每 30 秒收集一次数据，组成一行 JSON。
2. 它通过 USB 串口（电脑上叫 COM 口）发给 ESP32，格式是“协议 v1”。
3. ESP32 收到后回复 `OK`，内容有变化就刷新墨水屏。

```
电脑 (epaper_host.py) --USB 串口 115200--> ESP32 --SPI--> 墨水屏
```

墨水屏刷新一次要一两秒，局部刷新有残影，适合“偶尔看一眼”，不适合看视频。

### 0.2 选一条路线

| | 路线 A：微雪驱动板 | 路线 B：ESP32-S3 + 转接板 |
|---|---|---|
| 接线 | 只插屏幕排线 | 8 根杜邦线 |
| 适合 | 第一次做，想少出错（推荐） | 手上有 S3 |
| 固件 env（见 4.4） | `waveshare_esp32` | `esp32s3` |
| 烧录 | 一般自动进入下载模式 | 有时要手动进入 |
| USB 口 | 板载 USB 转串口 | 插标 `USB` 的原生口 |
| 预计时间（估计） | 40～60 分钟 | 60～90 分钟 |

时间主要花在第一次下载工具链（几百 MB）。同一块屏可以在两条路线间换，重新接线并烧对应 env 的固件即可。

### 0.3 要准备的东西

- **硬件**
  - 路线 A：微雪 e-Paper ESP32 驱动板一块（带 24 针 FPC 座，FPC 是屏幕的扁平排线）、能传数据的 USB 线（Rev3 是 Type-C，旧版是 micro-USB）、墨水屏。
  - 路线 B：ESP32-S3 开发板（如 ESP32-S3-DevKitC-1）、屏幕转接板（24 针 FPC 座转 8 个排针）、杜邦线 8 根、墨水屏。万用表强烈建议备一个，镊子、面包板可选。
- **电脑**：Windows 10 / 11，能上网，有空闲 USB 口（第一次尽量直接插电脑，不经过扩展坞）。
- **软件**：VS Code、PlatformIO 扩展、Python 3.10+。要显示 token 用量，还要 tokscale 或 ccusage（见 6.3）。

### 0.4 五条必须遵守的规则

1. 插拔 FPC 排线和杜邦线前，**先拔 USB**。
2. 屏幕的 VCC / PWR **只能接 3.3V**，接 5V 会烧屏。微雪驱动板自带稳压，不要再另外给屏幕接 5V。
3. 一块屏同一时间只接一块板子。
4. **先确认屏幕型号，再烧录**。型号选错会花屏、一直 BUSY（屏幕忙信号一直不结束），看起来像屏坏了。
5. 同一个 COM 口同一时间只能被一个程序打开。烧录或开串口监视器时，先关掉电脑端程序。

---

## 1. 确认屏幕型号（本设备可跳过）

**本项目这台 4.2 寸三色设备是微雪 e-Paper ESP32 Driver Board + 4.2 寸 400×300 三色屏（WS42TRI），不需要做本节核对。** 下面只用于黑白屏或其他来源的屏幕。

### 1.1 去哪里看

按可靠程度从高到低：

1. 屏幕背面的标签（如 `GDEY042T81`、`4.2inch e-Paper (V2)`、`400×300`）。
2. 排线金手指一端的白色补强板，有时印着型号。
3. 包装袋标签。
4. 订单或商品页。

标签只有尺寸时：4.2 寸屏的可视区域对角线约 10.7 cm，2.9 寸约 7.4 cm，7.5 寸约 19.1 cm。

> **别混淆**：`B/W` 是黑白；商品名里的 `(B)` 是黑白红三色，`(C)` 是黑白黄三色。`4.2inch e-Paper (V2)` 是黑白，`4.2inch e-Paper (B)` 是三色。

### 1.2 型号对照

黑白屏，黑白固件支持：

| 屏幕型号 | 尺寸 | 分辨率 | `config.h` 里的宏 | 微雪 1 号开关（见 1.3） |
|---|---|---|---|---|
| `GDEY042T81` / 4.2inch e-Paper (V2) | 4.2 寸 | 400×300 | `PANEL_GDEY042T81`（默认） | 3Ω 档 |
| `GDEY029T94` / 2.9inch e-Paper (V2) | 2.9 寸 | 296×128 | `PANEL_GDEY029T94` | 3Ω 档 |
| `GDEY075T7` / 7.5inch e-Paper (V2) | 7.5 寸 | 800×480 | `PANEL_GDEY075T7` | 0.47Ω 档 |

三色屏：

| 屏幕型号 | 尺寸 | 状态 |
|---|---|---|
| 4.2inch e-Paper (B) / (C) | 4.2 寸 400×300 | 用独立 env `waveshare_esp32_42tri`，见 [`README.md`](../README.md) |
| 2.9 寸 (B)、7.5 寸 (B) 等 | — | **不支持**，不要烧录 |

型号不在表里，先不要烧录：先确认驱动芯片和分辨率。4.2 寸三色按 [`README.md`](../README.md) 继续。

### 1.3 微雪驱动板的两个拨码开关（路线 A）

板子靠近 FPC 座的侧面有两个小拨码开关：

| 开关 | 作用 | 怎么拨 |
|---|---|---|
| **1 号（A/B）** | 切换适配不同屏的电阻档 | 按 1.2 的表：4.2 / 2.9 寸黑白拨 **3Ω 档**，7.5 寸黑白拨 **0.47Ω 档** |
| **2 号（ON/OFF）** | 给板载 USB 转串口芯片供电 | **必须拨到 ON**，OFF 时电脑找不到 COM 口 |

微雪中文文档和英文 wiki 对 A/B 的阻值标注**相反**（中文：A = 0.47Ω，B = 3Ω；英文：A = 3Ω，B = 0.47Ω）。所以**记阻值档，不要只记字母**。

拨好后屏幕不亮、花屏或颜色异常：先拔 USB，把 1 号开关拨到另一档再试。拨错一般不会烧屏，只是显示不对。

> 如果你的板子还有 `BS` 开关（SPI 3 线 / 4 线选择），拨到 **4 线**。本固件不支持 3 线。

### 1.4 在 `firmware/include/config.h` 选屏

打开 `firmware/include/config.h`，找到第 14 行附近的：

```c
#define EPD_PANEL PANEL_GDEY042T81
```

改成你的屏对应的宏（见 1.2 的表），保存。**改完要重新编译并上传**，只编译不上传，板子里还是旧固件。

不想改文件，也可以临时指定（在 `firmware/` 目录下执行，1 = 4.2 寸，2 = 2.9 寸，3 = 7.5 寸）：

```bash
PLATFORMIO_BUILD_FLAGS="-DEPD_PANEL=2" pio run -e esp32s3
```

PowerShell 写法：

```powershell
$env:PLATFORMIO_BUILD_FLAGS='-DEPD_PANEL=2'; pio run -e esp32s3
```

其他两个常用设置也在 `config.h`：

```c
#define EPD_RESET_MS 2        // 屏幕刷不出来、只闪一下：改成 10 再试
#define SERIAL_BAUD 115200    // 必须和电脑端一致，一般不用改
```

### 1.5 检查点

- [ ] 我知道屏幕的型号、尺寸和黑白 / 三色。
- [ ] 如果是三色屏，我已停下，没有烧黑白固件。
- [ ] 我知道 `config.h` 里该用哪个 `PANEL_...` 宏。
- [ ] （路线 A）我知道 1 号开关拨哪一档，2 号开关要拨 ON。

---

## 2. 路线 A：微雪驱动板（不接线）

屏幕排线直接插到驱动板的 24 针 FPC 座，不用杜邦线。

### 2.1 板子上的部件

- **24 针 FPC 座**：插屏幕排线，旁边有黑色小卡扣。
- **BOOT / EN 按钮**：EN 就是复位。烧录失败时用来进入下载模式。
- **USB 口**：Rev3 是 Type-C，2022 年之前的旧版是 micro-USB。
- **1 号 / 2 号开关**：见 1.3。
- **J3 / J4 排针**：用套件里的“转接板 + FFC 延长线”时才用。**新手直接插 24 针 FPC 座**，少一个接触点。

### 2.2 设置开关

1. 不插 USB。
2. 2 号开关拨 **ON**。
3. 1 号开关按 1.2 的表拨。

### 2.3 插 FPC 排线

1. **拔掉 USB。**
2. 用指甲或镊子把 FPC 座的黑色卡扣**轻轻向上抬起**，不要拔下来。
3. 排线**金手指朝向有触点的一面**（朝下对准座子里的金属触点），平直插到底，不能歪、不能只插一半。
4. 把卡扣**压下去**，压到和座子齐平。轻拉排线，应该拉不出来。
5. 上电后完全没反应：断电 → 抬卡扣 → 拔出排线 → 翻面重插 → 压卡扣 → 再上电。排线插反一般不烧，但一定要断电操作。

注意：不要用手摸金手指，不要把排线折死。接触不良是“屏幕不刷新 / 一直 BUSY”最常见的原因。

### 2.4 连电脑，装串口驱动

1. 用**能传数据**的 USB 线连电脑，板子电源灯应该亮。没反应就换线、换口。
2. 打开设备管理器（右键“开始”→ 设备管理器）→ 端口 (COM 和 LPT)：
   - `USB-SERIAL CH343 (COM5)`：Rev3 板，驱动正常；
   - `Silicon Labs CP210x ... (COM5)`：旧版板，驱动正常；
   - `USB-SERIAL CH340 (COM5)`：部分兼容板，驱动正常；
   - 黄色感叹号或“未知设备”：驱动没装。
3. 驱动没装就按芯片装：

   | 芯片 | 用在哪 | 去哪装 |
   |---|---|---|
   | CH343 | 微雪 Rev3（2022 年 7 月后） | WCH 官网搜 `CH343SER` |
   | CP210x | 微雪旧版（2022 年 7 月前） | Silicon Labs 官网搜 `CP210x VCP Driver` |
   | CH340 / CH341 | 部分第三方 ESP32 板、S3 的 UART 口 | WCH 官网搜 `CH341SER` |

4. 装完重新插拔 USB，记下 `COMx` 的编号（如 `COM5`），第 5、6 节要用。

### 2.5 检查点

- [ ] 2 号开关 ON，1 号开关档位正确。
- [ ] FPC 插到底、卡扣压紧。
- [ ] 设备管理器里有 `COMx`，没有黄色感叹号。

---

## 3. 路线 B：ESP32-S3 + 转接板（接 8 根线）

慢一点，接完检查两遍再上电。

### 3.1 认识两块板

**ESP32-S3 开发板**：常有两个 USB 口。

- 标 **`USB`** 的口：芯片原生 USB。**本项目用这个口**，固件开了 `ARDUINO_USB_CDC_ON_BOOT=1`，串口数据走它。
- 标 **`UART`** 或 **`COM`** 的口：板载 USB 转串口芯片，能烧录，但**看不到固件的 `READY` / `OK` / `ERR`**。
- 只有一个口就用那个。一次只插一个口。

**屏幕转接板**：一边是 24 针 FPC 座，一边是 8 个排针。丝印不同厂家略有差异，**以丝印为准，不要按线颜色猜**：

| 转接板丝印 | 别名 | 作用 |
|---|---|---|
| `VCC` / `PWR` | 3V3 | 屏幕电源，**只能 3.3V** |
| `GND` | GND | 地 |
| `DIN` | `SDA` / `MOSI` | 数据（电脑到屏幕） |
| `CLK` | `SCL` / `SCK` | 时钟 |
| `CS` | `CS#` | 片选 |
| `DC` | `D/C` | 数据 / 命令选择 |
| `RST` | `RES` | 复位 |
| `BUSY` | `BUSY` | 屏幕忙信号 |

### 3.2 接线表

| 转接板引脚 | 接到 ESP32-S3 | 建议线色 |
|---|---|---|
| `VCC` / `PWR` | **`3V3`**（绝不能接 5V / VIN） | 红 |
| `GND` | `GND` | 黑 |
| `DIN` | `GPIO 11` | 蓝 |
| `CLK` | `GPIO 12` | 绿 |
| `CS` | `GPIO 10` | 黄 |
| `DC` | `GPIO 9` | 橙 |
| `RST` | `GPIO 8` | 白 |
| `BUSY` | `GPIO 7` | 紫 / 灰 |

这些是 `firmware/include/config.h` 中 `BOARD_ESP32S3` 一段的默认值：

```c
#define EPD_SCK  12  // CLK
#define EPD_MOSI 11  // DIN
#define EPD_CS   10
#define EPD_DC   9
#define EPD_RST  8
#define EPD_BUSY 7
```

接到别的 GPIO，就要同步改这里的数字。

### 3.3 接线步骤

1. **拔掉 S3 的 USB。**
2. 屏幕排线插进转接板：抬卡扣、金手指对准触点、平直插到底、压卡扣。座子方向可能和微雪板不同，插不到底不要硬推。
3. 按 3.2 先接 `GND` 和 `VCC`，**暂时不接信号线**。
4. 插 USB，用万用表量转接板 `VCC` 对 `GND`，应约 **3.3V**。量到 5V 立刻拔电，检查是否接到了 `5V` / `VIN`。
5. 拔 USB，再接 `DIN / CLK / CS / DC / RST / BUSY` 六根信号线。
6. 再检查一遍：没接错、没松动、相邻线没短路。可用万用表通断档逐根确认。
7. USB 线插在 S3 标 **`USB`** 的口，电源灯亮，电脑应识别到串口。

### 3.4 面包板和线的注意事项

- 面包板只用来分线，**不要从面包板的 5V 红轨给屏幕供电**。
- 面包板中间电源轨有的是分段的，用前先用万用表确认导通。
- S3 板比面包板槽宽时不要硬插，直接用杜邦线连排针。
- 杜邦线插到底，SPI 线尽量短（10～15 cm 内），太长容易花屏。
- 不要带电插拔杜邦线。
- 不要把线接到 `5V` / `VIN`，也不要接到 `EN`、`BOOT` 等引脚。
- 微雪驱动板用 USB 5V 供电，是因为板上有稳压。**S3 转接板仍然只能 3.3V。**

### 3.5 检查点

- [ ] 转接板 `VCC` 对 `GND` 量到 3.3V。
- [ ] 8 根线按表接好，逐根确认过，没有短路。
- [ ] FPC 插到底、卡扣压紧。
- [ ] USB 插在标 `USB` 的口，设备管理器里有 `COMx`（缺驱动见 2.4）。

---

## 4. 安装 VS Code 和 PlatformIO

PlatformIO 是编译和烧录 ESP32 固件的工具，装在 VS Code 里。

### 4.1 安装

1. 从 <https://code.visualstudio.com/> 下载并安装 VS Code。
2. 打开 VS Code，按 `Ctrl + Shift + X` 打开扩展，搜索 **PlatformIO IDE**（发布者 PlatformIO），点 Install。装完按提示重新加载。左侧会多出一个 PlatformIO 图标。

扩展商店打不开或下载很慢时，检查网络或代理。

### 4.2 打开 `firmware/` 文件夹

1. VS Code 菜单：File → Open Folder…
2. 选仓库里的 **`firmware`** 文件夹（里面直接有 `platformio.ini`）。**不要打开仓库根目录，也不要打开 `host/`。**
3. 弹出“是否信任此文件夹的作者”时选信任。

### 4.3 等第一次下载完成

第一次打开时，PlatformIO 会自动下载 ESP32 平台包、编译工具和所需库（GxEPD2、Adafruit GFX、Adafruit BusIO、U8g2_for_Adafruit_GFX、ArduinoJson）。要几分钟到十几分钟。**等底部进度条走完、状态栏不再显示 “Installing…” 再继续。**

### 4.4 选对 env

env 是 `platformio.ini` 里的一套构建配置，对应一种板子。在 VS Code 最底部蓝色状态栏点当前 env 名，在列表里选：

| env | 对应 |
|---|---|
| `waveshare_esp32`（默认） | 路线 A：微雪驱动板 + 黑白屏 |
| `esp32s3` | 路线 B：ESP32-S3 |
| `waveshare_esp32_42tri` | 微雪驱动板 + 4.2 寸三色屏，见 [`README.md`](../README.md) |

选错 env，引脚就不对，屏幕不亮或串口没反应。

### 4.5 检查点

- [ ] VS Code 打开的是 `firmware` 文件夹。
- [ ] 第一次下载已完成，没有红色报错。
- [ ] 底部 env 选对了。

---

## 5. 编译并烧录固件

下面的 `pio` 命令都在 `firmware/` 目录下执行。也可以用 VS Code 底部状态栏：✓ 是编译，→ 是上传，插头图标是串口监视器。

### 5.1 烧录前

1. 关掉电脑端程序（`epaper_host.py` 或 `run_host.bat`）和所有串口监视器。
2. 板子连好电脑，设备管理器里有 `COMx`。
3. 路线 A 确认 2 号开关在 ON；路线 B 确认 USB 插在 `USB` 口。
4. 确认 env 选对。

电脑上有多个串口时，先看 `pio device list`，再指定端口：

```bash
pio run -e waveshare_esp32 -t upload --upload-port COM5
```

也可以在 `platformio.ini` 的 env 里加 `upload_port = COM5` 和 `monitor_port = COM5`。

### 5.2 编译

```bash
cd firmware
pio run -e waveshare_esp32     # 路线 B 把 env 换成 esp32s3
```

看到 `[SUCCESS]` 就成功。报错时找**第一个** `error:`，不要只看最后一行。常见原因：库没下载完、`EPD_PANEL` 宏拼错、env 选错。

### 5.3 上传

```bash
pio run -e waveshare_esp32 -t upload     # 路线 B：-e esp32s3
```

看到 `[SUCCESS]` 就成功。上传时不要拔 USB、不要按复位。烧完板子自动复位。

### 5.4 上传失败：手动进入下载模式

如果卡在 `Connecting...`、`Failed to connect to ESP32` 或 `Timed out waiting for packet header`：

1. 按住 **BOOT** 不松。
2. 按一下 **EN**（S3 上叫 **RST**，有的板标 `EN`），松开。
3. 再松开 BOOT。
4. 重新上传。烧完按一下 EN / RST。

还不行就依次试：

- 换能传数据的 USB 线（很多充电线没有数据线芯），换 USB 口，不要经过 Hub。
- 关掉电脑端程序和串口监视器。
- 确认选对了 COM 口，不是蓝牙串口。
- 在 `platformio.ini` 的 env 里加 `upload_speed = 115200` 降速。
- 缺驱动就按 2.4 装。
- 最后手段：擦除 Flash 再烧，`pio run -e waveshare_esp32 -t erase`（路线 B 换 `esp32s3`）。

### 5.5 用串口监视器验证

串口监视器能直接看板子的输出。打开：

```bash
pio device monitor -e waveshare_esp32     # 路线 B：-e esp32s3
```

参数是 115200 8N1，`platformio.ini` 里已配好。

**路线 A**：打开监视器时板子会复位，应看到：

```
READY epaper-sidescreen v1 GDEY042T81 400x300
```

后两项是型号和分辨率。和你的屏不符时，回 1.4 检查 `config.h`。

**路线 B**：原生 USB 打开串口时板子不会复位，可能看不到 `READY`。在监视器里粘贴下面这行并回车，应收到 `OK`：

```json
{"v":1,"type":"ping"}
```

**手动发一条测试数据**（可选）：把下面这行**单行** JSON 粘贴进去回车。先收到 `OK`，几秒后屏幕从 “Waiting for host...” 变成仪表盘。

```json
{"v":1,"type":"stats","time":"10-28 14:05","cpu":23,"mem":61,"disk":72,"net_up":"120K","net_down":"3.4M","gpu":15,"gpu_temp":52,"tok":{"block_tokens":"145.9M","block_cost":40.69,"burn_tpm":"3.3K","remain_min":135,"today_tokens":"210M","today_cost":58.2}}
```

粘贴后没反应时，检查是不是多行、引号是不是英文引号。固件要求一行一个 JSON，最长 1024 字节。

### 5.6 检查点

- [ ] 编译和上传都输出 `SUCCESS`。
- [ ] 看到 `READY ...`，或发 `ping` 收到 `OK`。
- [ ] 屏幕显示 “Waiting for host...”（电脑端程序没跑时这是正常的）。

> 注意区分：**收到 `OK` 只说明串口和固件正常，不说明屏幕已经显示。屏幕是否显示正确，要看实屏。**

---

## 6. 运行电脑端程序

固件烧好后，屏幕显示 “Waiting for host...”。现在让电脑端程序发数据。

**先退出串口监视器**（5.5 打开的那个）：在它的终端里按 `Ctrl + C`，或在 VS Code 里关掉监视器终端。COM 口同一时间只能被一个程序打开，不退出的话，电脑端程序会提示打开串口失败。下面命令都在仓库的 `host` 目录执行。

> 小技巧：在文件资源管理器里打开 `host` 文件夹，地址栏输入 `powershell` 回车，就在该目录打开了 PowerShell。

### 6.1 安装 Python

1. 从 <https://www.python.org/downloads/windows/> 下载 Python 3.10 或更高版本（64 位）。
2. 安装时**勾选 “Add python.exe to PATH”**。
3. 打开**新的** PowerShell，执行 `python --version`，应显示 3.10 或更高。

提示 `Python was not found` 或弹出 Microsoft Store：重装并勾选 Add to PATH，或改用 `py` 命令，或在“设置 → 应用 → 应用执行别名”里关掉 `python.exe` 和 `python3.exe`。

### 6.2 安装依赖

```bat
cd %USERPROFILE%\Desktop\Plugin\epaper-sidescreen\host
python -m pip install -r requirements.txt
python -c "import serial, psutil; print('ok')"
```

最后一条应输出 `ok`。提示 `No module named serial`：装到了别的 Python，用 `python -m pip install ...` 再装。下载慢可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

### 6.3 安装 token 数据工具（可选）

只有想显示 token 用量才需要。没装也能跑，token 字段显示 `--`，其他数据照常。

- **tokscale（默认数据源）**：装了 Token Monitor 桌面程序就不用另装，程序会自动用它自带的 `tokscale.exe`。没装时执行 `npm i -g tokscale`（需要 Node.js 或 Bun，<https://nodejs.org/>）。
- **ccusage（备选，只统计 Claude Code）**：`npm i -g ccusage`（需要 Node.js），并在 `host/config.json` 里设 `"token_source": "ccusage"`。

数据源的详细说明和找不到命令时的处理见 [`host/README.md`](../host/README.md)。

### 6.4 先找到 COM 口

```bat
python epaper_host.py --list-ports
```

正常输出里应有你的板子，如：

```
COM5     VID:PID=1A86:55D3  USB-SERIAL CH343 (COM5)
```

输出 `No serial ports found.` 说明电脑没认到板子，见 8.2。

### 6.5 不接板子先自测

```bat
python epaper_host.py --dry-run --once
```

它打印一行 JSON 然后退出。检查：`cpu` / `mem` / `disk` 是 0 到 100 的数字；`net_up` / `net_down` 是 `120K`、`3.4M` 这样的字符串；有 NVIDIA 显卡才有 `gpu` / `gpu_temp`。token 字段第一次可能要等一会儿，不想等按 `Ctrl + C`。

### 6.6 正式运行

```bat
python epaper_host.py --port COM5
```

不写 `--port` 会自动探测，但电脑上有多个串口时建议写。也可以双击 `host\run_host.bat`，额外参数写在后面，如 `run_host.bat --port COM5`。

常用参数（每条单独执行）：

| 参数 | 作用 |
|---|---|
| `--interval 5` | 每 5 秒发一次（黑白屏固件最快 5 秒刷一次） |
| `--once` | 只发一次就退出 |
| `-v` | 输出调试日志 |
| `--log host.log` | 日志同时写入文件 |

**三色屏**的运行见 [`README.md`](../README.md)。三色屏两次刷新至少隔 180 秒，不会每 30 秒更新。

**看结果**：日志里应出现下面两行，Ctrl + C 停止程序。

```
connected to COM5 @ 115200
board: OK
```

黑白屏几秒内会从 “Waiting for host...” 变成仪表盘，右上角显示 `ONLINE`。这一步只说明串口通、固件回复了；屏幕画面是否正常要用眼睛看。

### 6.7 配置文件（可选）

把 `host\config.example.json` 复制成 `host\config.json` 再改。命令行参数优先于配置文件。常用的键：

| 键 | 默认 | 说明 |
|---|---|---|
| `port` | `null` | 串口，如 `"COM5"`；`null` 为自动探测 |
| `baud` | `115200` | 必须和固件一致 |
| `interval` | `30` | 发送间隔（秒） |
| `token_source` | `"tokscale"` | token 数据源：`"tokscale"` 或 `"ccusage"` |
| `disk` | `null` | 统计哪个盘，如 `"D:\\"`；`null` 为系统盘 |
| `gpu` | `true` | 是否调用 `nvidia-smi` |

完整键表见 [`host/README.md`](../host/README.md) 第 3 节。

### 6.8 开机自启（可选）

**方法 A：启动文件夹（最简单）**

1. 按 `Win + R`，输入 `shell:startup` 回车。
2. 给 `host\run_host.bat` 建快捷方式，放进这个文件夹。
3. 要指定串口：右键快捷方式 → 属性，在“目标”末尾加 ` --port COM5`（前面有空格）。

**方法 B：任务计划程序（不显示黑窗口）**

下面命令在 **cmd** 里执行（PowerShell 的引号规则不同）：

```bat
schtasks /Create /TN "EpaperHost" /SC ONLOGON /RL LIMITED ^
  /TR "\"C:\Users\你的用户名\AppData\Local\Programs\Python\Python312\pythonw.exe\" \"%USERPROFILE%\Desktop\Plugin\epaper-sidescreen\host\epaper_host.py\" --port COM5 --log %USERPROFILE%\Desktop\Plugin\epaper-sidescreen\host\host.log"
```

`pythonw.exe` 和 `epaper_host.py` 的路径换成你自己的（`where.exe pythonw` 可查；cmd 里 `where pythonw` 也行）。`pythonw` 没有窗口，**一定要加 `--log`**，否则出问题看不到原因。删除任务：

```bat
schtasks /Delete /TN "EpaperHost" /F
```

### 6.9 检查点

- [ ] `python -c "import serial, psutil"` 不报错。
- [ ] `--list-ports` 能看到你的板子。
- [ ] `--dry-run --once` 输出一行完整 JSON。
- [ ] 正式运行后日志有 `connected to COM5 @ 115200` 和 `board: OK`。
- [ ] 屏幕右上角是 `ONLINE`，数据在变化（黑白屏）。

---

## 7. 屏幕上看什么、怎么刷新

### 7.1 布局

- **顶部黑条**：左边是电脑时间，右边是 `ONLINE`；120 秒没收到消息变成反色的 `OFFLINE`，最后的数据保留。
- **左块 CLAUDE TOKENS**：当前 5 小时块的费用和 tokens（大号数字）、`Burn /min`（每分钟消耗）、`Block left`（本块剩余时间）、`Today`（今日 tokens）、`Today cost`（今日费用）。
- **右块 SYSTEM**：CPU / MEM / DISK 条形图，`Net up` / `Net down`，`GPU` / `GPU temp`（没有 NVIDIA 显卡或 `nvidia-smi` 不可用时隐藏）。

标签是英文，这是固件设计，不是没刷出中文。

### 7.2 黑白屏的刷新规则

- 内容有变化才刷新，没变化不刷。
- 默认局部刷新（快，有残影）；每 20 次局部刷新或每 30 分钟自动做一次全刷（慢，清残影）。
- 两次刷新至少间隔 5 秒。
- 开机画面是 “Waiting for host...”，收到第一条数据后切换到仪表盘并全刷一次。

三色屏只做全刷，两次刷新至少隔 180 秒（从上次刷新完成起算），规则见 [`firmware/README.md`](../firmware/README.md) 第 9.4 节。

### 7.3 手动全刷

残影明显时，先**关掉电脑端程序**，在 PlatformIO 串口监视器里发送：

```json
{"v":1,"type":"full_refresh"}
```

收到 `OK` 后，下一次刷新时会全刷。完成后关掉监视器，再重新运行电脑端程序。

### 7.4 电脑离线时的天气页（可选）

电脑关机、休眠或程序退出后，板子 120 秒收不到消息就会在仪表盘上标 `OFFLINE`。开启天气页后，离线超过设定的分钟数（默认 10 分钟），板子会自己通过 Wi-Fi 取 Open-Meteo 天气（免费、不用注册），把屏幕换成天气页：当前温度、体感、湿度、风速、未来几小时和几天的预报。每 30 分钟更新一次；电脑一回来，屏幕就切回仪表盘。

要求：板子已经配好 Wi-Fi（见上位机「配网…」），固件是带天气页的版本。

设置方法（上位机窗口左列的「离线天气页」）：
1. 在城市框里输入城市名（中文或英文都行），按 Enter（或点「搜索」）；结果多于一个时用 ↑↓ 选一个、按 Enter 确定，选好后显示「已保存：城市」。
2. 勾选「电脑停止推送 N 分钟后，屏幕自动显示天气」这一句，需要的话改 N（1–240）。
3. 设置保存在 `host/config.json` 的 `v2.weather`，并自动发给板子，板子断电也记得；之后每次连接都会再同步一次。

窗口里会显示板子回报的天气状态（在这一组的最下面一行）。如果固件还是旧版本，这一栏会提示「板子固件还不支持天气页」，不影响仪表盘，升级固件后自动生效。城市搜索需要电脑能上网；板子取天气需要板子能上网。

不用窗口时，在 `config.json` 里手写也行：

```json
"v2": { "weather": { "on": true, "place": "上海", "lat": 31.23, "lon": 121.47, "after_min": 10 } }
```

命令行 `epaper_host.py` 连接时同样会把这份设置发给板子。

### 7.5 手动切换页面、待办页和立即刷新（固件 1.6.0 起）

上位机窗口左列的「页面」可以让屏幕固定显示某一页：自动（默认：电脑在线显示仪表盘，离线一段时间后显示天气）、仪表盘、天气、待办。**点单选框只是预览，不会动板子**：右列会切到「屏幕预览」标签，显示这一页的样子——自动 / 仪表盘是实时的仪表盘，待办是按现在的待办渲染的整页，天气是上位机用「设置」里选的城市自己向 Open-Meteo 取一份预报（10 分钟内用缓存），再交给固件自己的绘图代码画出来（没选城市会写「没有设置城市」，取不到数据会写「拉取天气失败」）。满意了点「推送」：上位机发 `page`，五个数据块也全部重发一遍，屏幕按刷新间隔自己换过去；点「立即推送」则再发一次「立即刷新」，屏幕马上整屏刷新，不用等；选定后板子记住（断电也记得），直到你推送别的页面。推送「待办」时，如果待办改过（或这次运行还没推送过），会先把待办图一起带上，最多刷新一次。推送成功后才会把页面记进 `host/config.json`；立即推送后的刷新期间状态行显示「屏幕刷新中，约 20 秒…」，这两个按钮暂时不可点，刷完显示「屏幕已刷新 时:分:秒」。没连板子时单选框照样能预览，但两个推送按钮灰掉；连上后不会自动补发。天气预览需要先编译预览渲染器（见 `host/README.md`），渲染器比较旧时会提示重新编译。

待办页：在左列的「待办」里添加事项（输入框里按 Enter：选中某行就是保存修改，没选就是添加；双击一行编辑，Delete 删除），可以上下移动，勾「已完成」（灰点阵 + 划线）或「重要」（红色）。改了待办，右列「屏幕预览」会自动切到待办页显示整页效果（不需要连板子）；在「页面」里选「待办」再点「推送」或「立即推送」，把整页作为图片发给板子，板子断电后仍保留最后一次成功的待办页。事项太多放不下时，最后一行显示「+N more」；每条最多两行，超长的以「…」结尾。待办保存在 `host/config.json` 的 `v2.todo`。

「立即推送」让屏幕马上整屏刷新，绕过 180 秒的最短间隔；「推送」不强制刷新，屏幕按刷新间隔自己更新。三色屏刷新太勤会缩短寿命（Waveshare 建议至少间隔 180 秒），多久刷一次由你自己决定；自动更新仍遵守 180 秒。

如果板子固件还是 1.6.0 以前的版本，这几个功能会在界面上提示「板子固件太旧」，不影响其他功能；升级固件后可用。

---

## 8. 出问题怎么办

### 8.1 先做三个判断

1. 电脑端日志有 `board: OK` 吗？
   - 没有：问题在 USB / COM / 驱动 / 接线，看 8.2。
   - 有：串口通了，问题在屏幕侧（型号、排线、开关、供电），看 8.3。
2. 在串口监视器里发 `ping`，有 `OK` 吗？有说明固件在运行。
3. `--list-ports` 能看到 `COMx` 吗？看不到就先解决电脑认不到板子，不要折腾屏幕。

### 8.2 速查表

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 找不到 COM 口 | 线只能充电；微雪 2 号开关在 OFF；驱动没装；S3 插了 `UART` 口；Hub 问题 | 换数据线；2 号开关拨 ON；装驱动（2.4）；S3 换 `USB` 口；直接插电脑 |
| 上传失败（`Connecting...` 等） | 没进下载模式；COM 被占用；选错口 | 见 5.4 |
| 打开串口失败 / `拒绝访问` / `port is busy` | 别的程序占着 COM | 关掉串口监视器、Arduino IDE 等；拔插 USB |
| `pyserial is not installed` | 依赖没装到当前 Python | `python -m pip install -r requirements.txt` |
| 显示 `OFFLINE` | 电脑端没运行或崩了；COM 选错；波特率不对；串口被占用 | 看日志有没有 `board: OK`；确认 `--port`（拔插后 COM 号可能变）；确认 115200；关掉监视器；重启程序。屏幕在 120 秒后才变 OFFLINE，程序每 5 到 10 秒重连一次，等一会儿是正常的 |
| token 显示 `--` | 数据工具没装、不在 PATH、还没跑完 | 见 8.4 |
| 屏幕不刷新 / 一直 BUSY / 全白 / 全黑 | 排线没插好或插反；型号选错；1 号开关档位不对；屏没供电 | 见 8.3 |
| 花屏 / 残影 / 只显示一半 / 颜色不对 | 型号或分辨率选错；SPI 线太长；三色屏用了黑白固件 | 改对 `EPD_PANEL` 重新上传；发 `full_refresh`；缩短杜邦线；三色屏改用三色固件 |

### 8.3 屏幕不刷新 / 一直 BUSY

很多时候不是固件问题，而是**排线没接好**或**屏幕没供电**。按顺序查：

1. 日志有 `board: OK` 吗？没有就先解决串口。
2. 发 `ping` 有 `OK` 吗？有说明固件在运行。
3. 断电重插 FPC：抬卡扣 → 拔出 → 看金手指有无脏污 → 插到底 → 压卡扣；插反就翻面。
4. 确认 1 号开关：4.2 / 2.9 寸黑白用 3Ω 档，7.5 寸用 0.47Ω 档，不确定就换另一档。
5. 确认 `config.h` 的 `EPD_PANEL`，改完要重新上传。
6. 把 `EPD_RESET_MS` 从 2 改成 10，重新上传。
7. 按一下 RST / EN 复位。
8. 路线 B 测转接板 `VCC` 对 `GND` 必须是 3.3V，BUSY 必须接 GPIO 7。路线 A 不要另外给屏接 5V。
9. 有第二块屏或第二块板时交叉对换，能快速判断是屏还是板的问题。

### 8.4 token 显示 `--`

- **全部 token 字段都是 `--`**：数据工具没装、不在 PATH，或调用失败。日志里会有 warning。按下面处理：
  1. 确认 Node.js 和 npm 正常：`node --version`、`npm --version`。
  2. 重新打开一个终端（新装的命令要新窗口才生效）。
  3. 用 `ccusage --version`（或 `tokscale --version`）确认命令能运行，`where.exe ccusage`（PowerShell 里 `where` 是别的命令，要写 `.exe`）查它在哪。只有 `ccusage.cmd` 时，电脑端程序会自动去找 `.cmd`。
  4. 仍找不到，就在 `host\config.json` 里把 `ccusage_cmd`（tokscale 用 `tokscale_cmd`）写成完整路径，改完重启程序：

     ```json
     "ccusage_cmd": "C:/Users/你的用户名/AppData/Roaming/npm/ccusage.cmd"
     ```

     JSON 里反斜杠要写两个 `\\`，或者像上面一样用正斜杠。
  5. 用 ccusage 时，可以先手动试：`ccusage blocks --active --json`。
  数据源的完整说明见 [`host/README.md`](../host/README.md)。
- **只有 `Block left` 是 `--`**：当前没有活跃的 5 小时块，正常。
- **块的 tokens / 费用为 0，或今日为 0**：当前没有活跃块，或今天还没产生 token，正常。

第一次运行数据工具可能要等 1 分钟左右。

### 8.5 求助时提供什么（用文字，不用照片）

1. 屏幕现象：整屏颜色、有没有文字、第三色出现在哪里、是否闪一下后停住。
2. 发生时间、所用 COM、板上固件 env（正式/校准/黑白）。
3. `python epaper_host.py --list-ports` 的输出。
4. `python epaper_host.py --dry-run --once` 的输出（token 可打码）。
5. 电脑端完整日志（`-v` 或 `--log host.log`）。
6. PlatformIO 编译或上传的完整报错（从第一个 `error:` 开始）。
7. 串口监视器里发 `ping` 后看到的内容。

文字描述就够，不需要照片。

---

## 9. 附录

### 9.1 引脚

**路线 A：微雪驱动板**（板上固定，不接线）

| 屏幕信号 | ESP32 GPIO |
|---|---|
| SCK / CLK | 13 |
| DIN / MOSI | 14 |
| CS | 15 |
| DC | 27 |
| RST | 26 |
| BUSY | 25 |

**路线 B：ESP32-S3**：见 3.2。

### 9.2 `config.h` 常用参数

| 宏 | 默认 | 说明 |
|---|---|---|
| `EPD_PANEL` | `PANEL_GDEY042T81` | 屏幕型号，见 1.2 |
| `EPD_RESET_MS` | `2` | 复位脉冲毫秒数；屏幕不亮可试 `10` |
| `SERIAL_BAUD` | `115200` | 串口波特率，必须和电脑端一致 |
| `MAX_LINE_LEN` | `1024` | 单行 JSON 最大字节数 |
| `MIN_REFRESH_MS` | `5000` | 黑白屏两次刷新最小间隔 |
| `FULL_REFRESH_EVERY` | `20` | 每 20 次局部刷新做一次全刷 |
| `FULL_REFRESH_MS` | 30 分钟 | 每 30 分钟做一次全刷 |
| `OFFLINE_TIMEOUT_MS` | `120000` | 120 秒没消息显示 OFFLINE |

### 9.3 串口协议速查

完整说明见 `firmware/README.md` 第 6 节。

- 电脑到 ESP32：每行一个 JSON，以换行结尾，最长 1024 字节。消息类型 `stats`（数据，示例见 5.5）、`ping`、`full_refresh`。
- ESP32 到电脑：`READY ...`（上电）、`OK`（收到合法消息）、`ERR <原因>`（出错）。`OK` 只表示收到，不表示已显示。
- 字段可缺省或为 `null`，屏幕显示 `--`。

### 9.4 已知限制

- 黑白屏只支持 4.2 寸 400×300、2.9 寸 296×128（横屏显示，固件把 128×296 旋转了）、7.5 寸 800×480。4.2 寸三色有独立 env `waveshare_esp32_42tri`，见 [`README.md`](../README.md)；2.9 / 7.5 寸三色不支持。
- 屏幕标签是英文，固件没有中文字库。
- 不是高刷屏：黑白最快 5 秒刷新一次，三色至少隔 180 秒（从上次刷新完成起算）。
- token 依赖 tokscale 或 ccusage；GPU 依赖 `nvidia-smi`。
- 烧录、串口监视器、电脑端程序不能同时打开同一个 COM 口。

---

## 参考资料

- 微雪 e-Paper ESP32 Driver Board 使用说明（中文）：<https://docs.waveshare.net/E-Paper_ESP32_Driver_Board/Instructions-For-Use/>
- 微雪 e-Paper ESP32 Driver Board Wiki（英文）：<https://www.waveshare.com/wiki/E-Paper_ESP32_Driver_Board>
- PlatformIO 文档：<https://docs.platformio.org/>
- GxEPD2 库：<https://github.com/ZinggJM/GxEPD2>
- tokscale：<https://github.com/junhoyeo/tokscale>
- ccusage：<https://github.com/ryoppippi/ccusage>

> 引脚、命令和协议以 `firmware/README.md` 和 `host/README.md` 为准。
