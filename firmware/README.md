# ESP32 墨水屏副屏固件

ESP32 墨水屏副屏的固件，用 PlatformIO（Arduino 框架）构建。ESP32 通过 USB 串口一行一行接收 JSON（协议 v1 / v2），在墨水屏上显示 token 用量和系统监控（v1），或新版仪表盘（v2：总量、额度、模型、热图、趋势、鼠标电量）。

第一次使用请先看 [`docs/guide.md`](../docs/guide.md)（从接线到运行的完整步骤）。本文是固件的参考手册：改屏幕型号、引脚、协议、三色屏参数时查它。

```
firmware/
├── platformio.ini      四个 env：waveshare_esp32 / waveshare_esp32_42tri / waveshare_esp32_42tri_calib（诊断用）/ esp32s3
├── include/config.h    选屏幕型号、引脚、刷新参数   ← 一般只改这里
├── VENDOR_SOURCES.md   三色驱动的厂商来源和逐项依据
├── src/
│   ├── main.cpp        串口 / TCP 读行 + 刷新调度 + 配网命令（回 OK/ERR）
│   ├── protocol.*      一行 JSON → 更新显示数据 / 识别配网命令（与 Serial 无关，串口和 WiFi 共用）
│   ├── net.*           WiFi 连接、TCP 7330、UDP 发现 7331（只在 ENABLE_WIFI=1 时编译）
│   ├── settings.*      NVS：WiFi 密码、最后一屏（snap_flush_policy.h 是写入节奏，纯逻辑）
│   ├── refresh_policy.h  何时刷新、全刷/局刷（纯逻辑，可在电脑上测试）
│   ├── ui.cpp / ui.h   屏幕驱动初始化 + v1 布局绘制
│   ├── dashboard_v2.*  v2 仪表盘（400×300）的绘制，只依赖 Adafruit_GFX + U8g2
│   ├── weather_*.{h,cpp} 离线天气：weather_model.h（数据）、weather_parse（Open-Meteo JSON → 数据 + 提醒，纯逻辑）、weather_policy.h（何时显示 / 何时取，纯逻辑）、weather_net（HTTPS 取数据，独立任务）、weather_page_b（画页面）
│   ├── dash_model.h    v2 显示数据（紧凑、定长，约 1 KB）+ 内容签名
│   ├── dither.h        4×4 Bayer 抖动（纯逻辑）
│   ├── epd_tri.*       4.2 寸三色屏驱动（厂商时序，无 Arduino 依赖）
│   ├── tri_planes.h    三色双平面帧缓冲
│   └── model.h         显示数据结构（v1 的 Stats + v2 的 Dash）
└── test/
    ├── native/         电脑端测试：三色驱动/刷新策略（test_tri）、v2 协议/抖动/签名（test_dash）、离线天气（test_weather）
    ├── host_shim/      电脑上代替 Arduino.h 的最小替身（只给测试和预览用）
    └── preview/        离线预览：把 v2 界面画成 PPM 图片，见 9.7
```

## 1. 准备环境

1. 在 VS Code 里安装 **PlatformIO IDE** 扩展。
2. 用 VS Code 打开 `firmware/` 文件夹（不是仓库根目录）。第一次打开会自动下载工具链和库，要等几分钟。
3. 也可以直接用命令行的 `pio`（PlatformIO Core）。下面的命令都在 `firmware/` 目录下执行。

## 2. 选择板子（env）

env 是 `platformio.ini` 里的一套构建配置，每种板子一个。

| env | 板子 | 屏幕连接方式 |
|---|---|---|
| `waveshare_esp32`（默认） | 微雪 e-Paper ESP32 Driver Board | 屏幕排线插到板载 FPC 座，不需要接线 |
| `waveshare_esp32_42tri` | 同上，接 **4.2 寸 400×300 三色屏**（黑/白 + 红或黄）。**正式三色版**，固件 1.7.0，参数按实屏校准固定，带 WiFi（第 10 节）。详见第 9 节 | 同上，详见第 9 节 |
| `waveshare_esp32_42tri_calib` | 同上，**只用于诊断**：开机显示三色校准图 | 同上，见 9.5a |
| `esp32s3` | 通用 ESP32-S3 DevKitC | 用杜邦线接屏幕转接板，见下表 |

## 3. 切换屏幕型号

打开 `include/config.h`，把 `EPD_PANEL` 改成你的屏：

```c
#define EPD_PANEL PANEL_GDEY042T81   // 默认
```

| 宏 | 屏幕 | 分辨率 | 对应微雪型号 |
|---|---|---|---|
| `PANEL_GDEY042T81` | 4.2 寸黑白（默认） | 400x300 | 4.2inch e-Paper (V2) |
| `PANEL_GDEY029T94` | 2.9 寸黑白 | 296x128（横屏） | 2.9inch e-Paper (V2) |
| `PANEL_GDEY075T7`  | 7.5 寸黑白 | 800x480 | 7.5inch e-Paper (V2) |
| `PANEL_WS42_TRI`（4） | 4.2 寸三色 | 400x300 | 4.2inch e-Paper (B)/(C)，V2 或 V1；用 `waveshare_esp32_42tri`，见第 9 节 |

型号看屏幕背面或排线上的丝印。布局会按分辨率自动选字号和间距。

不改文件也可以临时指定型号：`PLATFORMIO_BUILD_FLAGS="-DEPD_PANEL=2" pio run -e esp32s3`（1 = 4.2 寸，2 = 2.9 寸，3 = 7.5 寸）。PowerShell 写法：`$env:PLATFORMIO_BUILD_FLAGS='-DEPD_PANEL=2'; pio run -e esp32s3`。

> 屏幕刷不出来或只闪一下：把 `config.h` 里的 `EPD_RESET_MS` 从 2 改成 10 再试。微雪驱动板和 HAT 用 2，裸屏有时要 10。

## 4. 引脚

### 微雪 ESP32 驱动板（`waveshare_esp32`）

引脚是驱动板上固定的，不用接线，也不要改：

| 屏幕信号 | GPIO |
|---|---|
| SCK / CLK | 13 |
| DIN / MOSI | 14 |
| CS | 15 |
| DC | 27 |
| RST | 26 |
| BUSY | 25 |

### ESP32-S3（`esp32s3`）

用杜邦线连屏幕转接板。默认接法如下；要改就改 `include/config.h` 里 `BOARD_ESP32S3` 那一段：

| 转接板引脚 | ESP32-S3 |
|---|---|
| VCC | 3V3（不要接 5V） |
| GND | GND |
| DIN | GPIO 11 |
| CLK | GPIO 12 |
| CS | GPIO 10 |
| DC | GPIO 9 |
| RST | GPIO 8 |
| BUSY | GPIO 7 |

转接板上有 PWR 引脚时，也接 3V3。S3 固件用芯片原生 USB（已开 `ARDUINO_USB_CDC_ON_BOOT=1`），USB 线要插板上标 **USB** 的口，不要插标 **UART/COM** 的口。

## 5. 编译与烧录

```bash
# 编译
pio run -e waveshare_esp32
pio run -e waveshare_esp32_42tri   # 4.2 寸三色
pio run -e esp32s3

# 烧录（自动找串口；指定串口加 --upload-port COM5）
pio run -e waveshare_esp32 -t upload
pio run -e esp32s3 -t upload

# 打开串口监视器（115200）
pio device monitor -e waveshare_esp32
```

VS Code 里：先在底部状态栏选 env，再点 ✓ 编译、→ 烧录、🔌 串口监视器。

烧录失败时手动进入下载模式：按住 **BOOT**，按一下 **RST**（微雪板叫 **EN**），松开 RST，再松开 BOOT，然后重新烧录。烧完按一次 RST / EN。更多排错见 [`docs/guide.md`](../docs/guide.md) 5.4。

## 6. 串口协议（v1 + v2）

- 串口参数为 115200 8N1，UTF-8 编码。每条消息是一行 JSON，以 `\n` 结尾，单行最多 1024 字节，超长的行会被丢弃并回复 `ERR too_long`。
- 上电后打印 `READY epaper-sidescreen v2 <panel> <宽>x<高>`，例如 `READY epaper-sidescreen v2 GDEY042T81 400x300`。`v2` 表示固件认识 v2 消息（旧固件打印 `v1`）。
  - 注意：S3 走原生 USB，电脑打开串口时板子不会复位，所以可能收不到 READY。电脑端应该发一条 `ping`，以收到 `OK` 作为连接成功的判断。
- 每条合法消息回复 `OK`。出错时回复 `ERR <原因>`，原因有：`bad_json <ArduinoJson 错误码>`、`not_object`、`bad_version`、`missing_type`、`unknown_type`、`too_long`，v2 另有 `missing_field <名>`、`bad_field <名>`。出错的消息不会改动任何显示数据。电脑端应忽略其他非 OK/ERR 的行。
- 消息类型：
  - `{"v":1,"type":"stats", ...}`：仪表盘数据，字段见下面的例子。每条 stats 都是一份完整快照，缺失或为 null 的字段显示 `--`；`gpu` 和 `gpu_temp` 都没有时，GPU 两行会隐藏。
  - `{"v":1,"type":"ping"}`：心跳，用于保持在线状态。
  - `{"v":1,"type":"full_refresh"}`：下一次刷新强制全刷。

```json
{"v":1,"type":"stats","time":"10-28 14:05","cpu":23,"mem":61,"disk":72,
 "net_up":"120K","net_down":"3.4M","gpu":15,"gpu_temp":52,
 "tok":{"block_tokens":"145.9M","block_cost":40.69,"burn_tpm":"3.3K","remain_min":135,
        "today_tokens":"210M","today_cost":58.2}}
```

固件对字段格式比较宽容：
- 文本字段可以传字符串，也可以传数字。数字会自动缩写，例如 145900000 显示为 `145.9M`。
- 费用传数字时显示成 `$40.69`。
- `remain_min` 传数字时显示成 `2h15m`。
- `gpu_temp` 传数字时显示成 `52°C`。
- 百分比可以是整数、小数或数字字符串，会取整并限制在 0–100。

`v` 只能是 1 或 2（省略按 1 处理），其他值回复 `ERR bad_version`；`stats` 只接受 v1，`dash/quota/models/heat/mouse` 只接受 v2，不匹配也回 `bad_version`。

### v2 消息（完整契约见 [`docs/dashboard-v2-protocol.md`](../docs/dashboard-v2-protocol.md)）

- 一行一个区块的完整快照，后到的覆盖先到的；单行同样最多 1024 字节。所有数字由电脑端格式化成字符串，固件只画，不算时间。
- `{"v":2,"type":"dash",...}` TOKENS 块 + 左下角时间：`main`（`l` 周期名 TODAY/TOTAL、`tok` 完整带千分位的数字、`cost`）和 `alt`（`l`、`tok` 缩写）；`time`（`MM-DD HH:MM`）的日期部分放在右栏标题行右端，时间部分放左下角；可选的 `every`（秒，最多 3600）是屏幕刷新间隔：显示过第一个数据画面后，两次刷新至少隔这么久（短于面板最短间隔时按最短间隔），`full_refresh` 不受限制，不算画面内容。`quota` 额度（最多 4 家；每家 `i` 条形项最多 2 项，多发到 3 项也能画；`txt` 是条下面的一行字，每段 `s`，`w:1` 的段反色；`bal` 余额放在该家标题行右侧）；`models` 模型排行（最多 5 项，标题只写 `MODELS`，`scope` 不显示）；`heat` 热图 + 趋势（`d` 为 `0`–`9`、`A`–`C`/`-`，`t` 为 Base64 字母/`-`；主机按**整周**发最近 10 周（64–70 天，一周从周日开始，第一天是周日），固件按字符串长度画：日历显示最后 10 周，更长的只取最后 112 天（旧的 182 天也不会出错）；`rows`（旧字段）被忽略；`active` 是有用量的天数（旧名 `active30` 也认，不再显示）；`view:"cal"` = 日历样式（没有或其他值 = 热图）；`d`、`t` 都有时长度必须相同，字符不合法或为空回 `bad_field ...`）；`mouse` 鼠标电量。另有 v2 的 `ping`、`full_refresh`。旧格式的 `dash`（没有 `main` / `alt`）不会出错，对应位置显示 `--`。
- 每个区块有 `st`：`ok`（省略也是 ok）/ `stale`（旧值照画，标题右侧加反色 `STALE` 标签）/ `na`（固件清空该区块并显示 `--`，不画旧值）；`mouse` 另有 `off`。从没收到过的区块显示 `NO DATA`（布局不变）。
- 额度告警：该家 `st` 与整块 `st` 都是 ok 时，`left` ≤ 10 的额度条剩余部分用强调色；鼠标 `pct` ≤ 15 且 `st` 为 ok 时电池填充用强调色。
- 字符串超长会被截断（在 UTF-8 字符边界上），模型名超过 18 个字符变成前 17 个字符加 `~`。
- 切换：收到任一 v2 数据消息（`dash/quota/models/heat/mouse`）就显示 v2 界面；之后再收到 v1 `stats` 就切回 v1 界面（已收到的 v2 区块保留）。v2 的 `ping`、`full_refresh` 不切界面。v2 界面只在 400×300 的屏上画（4.2 寸黑白、三色）；别的屏收到 v2 数据仍显示 v1 界面。
- 解析函数是 `handleLine(view, line, len)`（`src/protocol.cpp`），只更新数据、返回 `OK`/`ERR` 的结果，不碰 `Serial`；`main.cpp` 的串口代码只负责切行再调用它。以后从 WiFi / BLE 收到同样的行，调用同一个函数即可。

不接电脑端程序也能手动测试：在串口监视器里粘贴上面那行 JSON（必须是单行），回车后应看到 `OK`，几秒后屏幕刷新。

## 7. 显示与刷新策略

- **开机画面**：显示 "Waiting for host..."、串口参数、屏幕型号和分辨率，收到第一条 stats 后切换到仪表盘。
- **仪表盘（v1，收到 `stats` 时）**：
  - 顶部黑条：左边是时间（电脑端发来的 `time`），右边是 `ONLINE`，离线时变成反色的 `OFFLINE`。
  - 左块 **CLAUDE TOKENS**：5h block 的费用、tokens（大号数字）、Burn /min、Block left、Today、Today cost。
  - 右块 **SYSTEM**：CPU、MEM、DISK 条形图，Net up 和 Net down，GPU 条形图和 GPU temp（没有 GPU 数据时隐藏）。
- **仪表盘（v2，收到 v2 数据消息时，仅 400×300）**：整屏 400×300，**没有顶栏**，左右两栏，中间一条 50% 抖动竖线，区块之间用 50% 抖动横线。不再显示 CPU / 内存 / 磁盘 / 网速 / GPU。
  - 左栏 **TOKENS**：第 1 行左边 `TOKENS`、右边费用 `main.cost`；第 2 行是完整数字 `main.tok`，在 logisoso 28 → 24 → 20 → helvB14 里选第一个能放进 188 px 的字号，靠左；第 3 行左边是反色徽章 `main.l`（TODAY / TOTAL），右边小字 `alt.l alt.tok`（如 `TOTAL 7.43B`）。
  - 左栏 **QUOTA**：每家一行标题：名称 + 套餐，余额靠右放在同一行（如 `DeepSeek  PAYG  ¥38.66`）；下面每个条形项一行：标签、条形图、剩余 %、重置倒计时；`txt` 一行字，任何一家都可以有（包括只有余额的）：两段时第一段靠左、最后一段靠右对齐到栏右边，只有一段时靠左，`w:1` 的段反色（黑底白字）。行距固定，装不下的整行丢掉、不压缩。条形图：整条有 1 px 黑色外框，剩余部分实心填充，已用部分是纯白（不抖动）。告警（`left` ≤ 10 且该家 ok）：整条（外框 + 剩余部分）和百分比文字都变红；`txt` 里 `w:1` 的段整段用红色文字（不再是黑底反白）；红色文字是实心的。黑白屏上红退化为黑：条和百分比照常画黑色，`w:1` 的段仍用反白显示。
  - 左下角（上方一条 50 % 点线隔开）：左边 `dash.time` 的时间部分（`HH:MM`），helvB12 大字；右端是鼠标电量（小电池图标 + 百分比）和连接图标。在线 = 实心圆点加圆环；离线 = 强调色（黑白屏为黑色）的 ×。鼠标 `off` / `na` 显示空心电池加 `--`；`stale` 时百分比放在反色框里；充电时百分比右边画一个小闪电（其余部分左移让出位置）；`pct` ≤ 15 且 ok 时电池画成强调色。
  - 右栏第一行：左边 `ACTIVITY`（日历样式时是 `CALENDAR`），数据偏旧时后面跟 `STALE`；右端是 `dash.time` 的日期部分（如 `10-28`，日历样式也一样），字号和标题一样（helvB08）。
  - 右栏 **热图（日历）**：7 行 = 周日到周六（从上到下，不写星期），每周一列，最旧的一周在最左边，今天在最后一列；第一天按它的星期几放到对应行（主机默认从周日开始）；只显示最后 10 周。格子 18×18，**无描边、无间隔**，整块 180×126。档位由主机按固定刻度算（一天 1B token 及以上 = 档 12，每档 ≈ 83.3M）。填色用 4×4 规则点阵、按屏幕坐标对齐（`heatInk`，在 `src/dash_model.h`）：档 0 空白；档 1–8 白底加红点，每档多 2/16，档 8 纯红；档 9–12 红底加黑点，每档多 2/16，档 12 红黑各半。未知日（还没开始记录）和档 0 一样空白；今天在日历右侧、对着它那一行画朝左的小三角。黑白屏上红画成黑。
  - 右栏 **日历样式**（`heat.view` 为 `cal`）：当月整月挂历，上面一行 `S M T W T F S`（周日红色），一周一行，行数随月份 4–6 行（行高最多 23 px，6 行时 19 px）；格子 25×(行高−1) px，间隔 1 px 白，底色和热图同一套色阶（`heatInk`）；每格写日期数字（白边，周日红字），上个月的格子只有底色不写字，今天之后的日子白底；今天不另做标记。
  - 右栏 **TREND**：用日历上显示的那些天，**分段时间轴**：最近 15 天占右半（中点是 15 天前那天，每天一样宽，进入右半的那一步也一样宽），更早的天挤在左半（线性）；不到 17 天就整宽线性。未知日按 0；不做平均，用 Catmull-Rom 样条把每天的真实值连起来（每天一个节点，某天的斜率取前后两天连线的斜率，曲线流过每个点、不会在每个点上停平），超出 0 到峰值的一点点过冲裁掉；峰值那天碰到顶，今天就是今天的量；曲线密集取样后用圆形笔刷描，陡的地方和平的地方一样粗；**只画 1 px 红色曲线**（`TREND_STROKE`，可编成 2 或 3）（黑白屏为黑），不填充，没有坐标轴。标题右侧 `peak ...`；下方三个日期：第一天、中点（15 天前那天）、最后一天。
  - 误差扩散在 `src/diffuse.h`（纯逻辑，可在电脑上测试）：把每个目标像素映射成「亮度 + 红度」两个数，在这个二维空间里累计误差，取最近的调色板点（白 / 红 / 黑），误差按 7/16、3/16、5/16、1/16 扩散，蛇形扫描；只用两行误差缓冲（约 1.6 KB，放在栈上），不分配整屏缓冲。
  - 右栏 **MODELS**：放在右栏最底部，前 5 名，名称、token、占比都用 helvetica 8（行距 11 px），省下的高度全部给 TREND；标题不标 TODAY / TOTAL。
  - 缺失显示：区块从没收到显示 `NO DATA`（热图 / 趋势画外框，中间写字），`na` 显示 `--`，`stale` 在标题右侧加 `STALE`。文字永远是实心黑，不压在抖动上；强调色只用于连接断开、额度 ≤ 10、电量 ≤ 15，黑白屏退化为黑 / 白。
  - 刷新依据是显示内容的签名（64 位哈希，覆盖所有显示内容和 ONLINE/OFFLINE）：内容没变就不刷新。
  - 离线预览：`bash test/preview/run.sh` 把这个界面画成图片，见 9.7。
- **刷新**：
  - 只有显示内容变化时才刷新。默认用局部刷新，每 20 次局部刷新或每 30 分钟做一次全刷，以清除残影。
  - 两次刷新至少间隔 5 秒。
  - 收到 `full_refresh` 或从开机画面切到仪表盘时，做一次全刷。
- **离线**：120 秒内没有收到任何合法消息（包括 ping）时显示 `OFFLINE`，其余数据保留最后一次的值。
- **串口不阻塞**：
  - 串口接收缓冲区为 4 KB，墨水屏刷新时收到的数据先存在缓冲区里。
  - 等待屏幕 BUSY 的过程中，固件会通过 GxEPD2 的 busy 回调继续读取串口并回复 OK。
  - 刷新期间收到的新数据，在下一轮刷新时只显示最新的一条。

上面这些参数都可以在 `config.h` 的「Behaviour」一节修改。三色屏的刷新规则不同，见第 9 节。

## 8. 字体

标签都是英文，只用 U8g2 自带的 Helvetica（标签和数值）和 Logisoso（大号数字），没有中文字库。原因：显示内容只有数字和简短英文；`wqy` 中文字库每个字号占 100–200 KB Flash，英文字体每个字号只有几 KB。

以后要中文，可在 `ui.cpp` 的 Theme 里把 `label` 换成 `u8g2_font_wqy12_t_gb2312` 等字体（先确认 Flash 够用）。

## 9. 4.2 寸三色屏（`waveshare_esp32_42tri`）

适用：微雪 e-Paper ESP32 Driver Board + 4.2 寸 400×300 三色屏（黑/白 + 红或黄）。驱动是 `src/epd_tri.cpp`，按微雪官方驱动 `EPD_4in2b_V2` / `EPD_4in2bc` 的命令时序重新实现，逐项依据见 [`VENDOR_SOURCES.md`](VENDOR_SOURCES.md)。串口协议、JSON v1 字段、`OK`/`ERR` 和布局都和黑白版一样。

> **状态**：正式 env 的参数已在这块屏上实屏校准：固定 `v2_old`、`EPD_TRI_V2_OLD_RY_INVERT=0`、`EPD_TRI_TEST_PATTERN=0`。在这块屏上，旧默认 `auto_v2` + `ry_invert=1` 显示整屏红色，改成 `v2_old` + `ry_invert=0` 后显示正常。推测原因是旧路径的 RY 极性不对，但没有进一步证实。
>
> `OK` 和 `DIAG ... refresh done` 只说明串口收到、驱动报告刷新完成，不说明画面正确。画面要看实屏。

### 9.0 正式 env 的参数和优先级

| 宏 | `config.h` 通用默认 | 正式 env `waveshare_esp32_42tri` | 诊断 env `..._calib` |
|---|---|---|---|
| `EPD_TRI_PROFILE` | `0`（`auto_v2`） | `2`（`v2_old`，不做 0x2F 读回） | `2` |
| `EPD_TRI_V2_OLD_RY_INVERT` | `1` | `0`（`0x13` 发原始 ry） | `0` |
| `EPD_TRI_TEST_PATTERN` | `0` | `0`（正常开机画面） | `1`（校准图） |
| 其他（180 s 间隔、超时、24 h 保底等） | 见 9.3 | 用通用默认，未改 | 同左 |

- **为什么这样设**：这块屏上实测，`v2_old` + 原始 ry 显示正确；旧默认 `auto_v2` + invert 1 显示整屏红色。正式 env 直接用实测正确的这组参数。固定 `v2_old` 也去掉了“读回 `0xFF` 只是兜底”的不确定性。
- **为什么只改 env，不改 `config.h` 默认**：这只是单块屏的结果。别的三色屏（例如真正的 `v2_new` 屏）可能需要 `auto_v2`，所以通用默认保持不变。
- **优先级**：`platformio.ini` 里 env 的 `-D` 参数 > `config.h` 里的 `#ifndef` 默认值。要改正式 env 的某个值，直接改 env 里那一行，不要再追加同名 `-D`（重复定义只会出编译警告，取值不可靠）。`test/native` 会检查正式 env 每个宏只定义一次。
- **其他 env 不受影响**：`waveshare_esp32`、`esp32s3` 没有任何 `EPD_TRI_*` 参数，仍用 4.2 寸黑白默认屏；诊断 env `waveshare_esp32_42tri_calib` 保持原样。

### 9.1 构建与烧录

```bash
pio run -e waveshare_esp32_42tri               # 编译
pio run -e waveshare_esp32_42tri -t upload     # 烧录
pio device monitor -e waveshare_esp32_42tri    # 串口 115200
```

烧录时用 `--upload-port COMx` 指定唯一的目标端口；端口号每次以 `python host/epaper_host.py --list-ports` 当场列出的为准，不要沿用旧记录里的 COM 号。

上电后先打印 `DIAG epd config ...` 和 `DIAG fw version=1.3.1 test_pattern=0`，然后是 `READY epaper-sidescreen v2 WS42TRI 400x300`（协议字段 `v1` 改为 `v2`，其余格式没变）。`test_pattern=0` 表示正式构建，`1` 表示诊断构建。驱动板上的 1 号开关按板卡手册拨到 4.2 (B)/(C) 那一档（手册第 7 页列在 B 档）。注意中英文文档对 A/B 的标注相反，见 `docs/guide.md` 1.3；型号不明时不要盲目切换。

### 9.2 驱动路径（`EPD_TRI_PROFILE`）

| 值 | 名称 | 行为 | 厂商来源 |
|---|---|---|---|
| `0`（`config.h` 默认；正式 env 不用） | `auto_v2` | 复位后发 `0x2F`，把 DIN（GPIO14）切成输入读回 1 字节；`0x01` 走 `v2_new`，其他值走 `v2_old` | `EPD_4in2b_V2` 的 `Init()` |
| `1` | `v2_new` | `0x24`/`0x26` 写两平面，`0x22 F7`+`0x20` 刷新，BUSY 高电平=忙，休眠 `0x10 03` | `*_new` 函数 |
| `2`（正式 env、诊断 env） | `v2_old` | `0x04` 上电，`0x10`/`0x13` 写两平面，`0x12` 刷新，查 BUSY 前先发 `0x71`，低电平=忙，休眠 `0x50 F7`→`0x02`→`0x07 A5` | `*_old` 函数 |
| `3` | `bc_v1` | 4.2 寸 B/C 第一代：`0x06` 升压、`0x04`、`0x10`/`0x13`、`0x12`，复位低电平 200 ms，休眠 `0x02`→`0x07 A5` | `EPD_4in2bc` |

- 自动模式只做厂商自己的“新/旧”二选一，**不能**识别商品型号，**不能**区分红色和黄色，也**不能**识别 `bc_v1`（第一代屏要手动设成 `3`）。
- 固件不会自动换另一套波形重试，也不会根据 `_old` 文件名选择旧路径。
- 自动模式默认连读 3 次（`EPD_TRI_PROBE_READS`），3 次结果必须相同。结果不一致时报故障并停止刷新，这时请手动指定路径。

### 9.3 其他配置（`include/config.h` 第 4 节，或在 env 的 `build_flags` 里加 `-D...`）

| 宏 | 默认 | 说明 |
|---|---|---|
| `EPD_TRI_V2_OLD_RY_INVERT` | `1`（正式 env 为 `0`） | 只影响 `v2_old`。两份厂商 V2 代码的旧路径极性相反（见 VENDOR_SOURCES §3）；红/黄和白底颠倒时设为 `0` |
| `EPD_TRI_RESET_LOW_MS` | `0` | 复位低电平时长；`0` 表示用厂商值（V2 2 ms，B/C V1 200 ms） |
| `EPD_TRI_PROBE_READS` | `3` | 自动模式要求结果一致的读回次数（厂商只读 1 次） |
| `EPD_TRI_BUSY_TIMEOUT_MS` | `5000` | 复位、上电、断电时等待 BUSY 的上限 |
| `EPD_TRI_REFRESH_TIMEOUT_MS` | `45000` | 一次刷新等待 BUSY 的上限（厂商规格约 15 s，取 3 倍） |
| `EPD_TRI_MIN_REFRESH_MS` | `180000` | 上一次刷新**结束**到下一次开始的最小间隔。厂商建议至少 180 s；编译时检查不能小于 60 s |
| `EPD_TRI_MAX_IDLE_MS` | 24 h | 内容一直不变时，也至少隔这么久刷新一次（厂商建议三色屏至少每 24 h 刷新一次） |
| `EPD_TRI_ALERT_PCT` | `90` | CPU/MEM/DISK/GPU 条达到这个百分比时用红/黄色填充 |

正式 env 现在的写法如下。要改某个值（例如换一块需要 `v2_new` 的屏），就地改对应那一行，不要追加第二个同名 `-D`；改了就不再是实测过的配置，需要重新实测：

```ini
[env:waveshare_esp32_42tri]
build_flags =
    ${env.build_flags}
    -DBOARD_WAVESHARE_ESP32
    -DEPD_PANEL=4
    -DEPD_TRI_PROFILE=2
    -DEPD_TRI_V2_OLD_RY_INVERT=0
    -DEPD_TRI_TEST_PATTERN=0
```

### 9.4 刷新、复位、BUSY 和休眠

- **只做全屏刷新**，不使用黑白屏的局部刷新。每次刷新的步骤：硬件复位 → 按路径初始化 → 写黑白平面和红/黄平面（各 15000 字节）→ 全刷 → 深度休眠。下一次刷新再从复位开始。
- **节流**：只有内容变化、收到 `full_refresh`、或者超过 24 h 没刷新时才刷新。即便如此，也必须等上一次刷新**结束**满 `EPD_TRI_MIN_REFRESH_MS`（默认 180 s）。`full_refresh` 同样要遵守这个间隔，所以开机画面之后，第一次显示仪表盘最多要等 3 分钟左右。
- **只显示最新数据**：等待期间收到的 stats 会直接覆盖旧数据，下一次刷新只显示最新的一份。
- **串口不中断**：复位延时、写平面（每 50 字节一次）和等待 BUSY 时都会继续读串口并回复 `OK`。`OK` 只表示消息已收到，不表示已经显示到屏幕上。
- **离线判断**和刷新无关：120 秒没收到合法消息就算离线，到下一次刷新时显示红/黄色的 `OFFLINE` 标记。
- **BUSY 和初始化都有超时**。超时或自动读回无法判定时，固件输出故障诊断行，此后不再刷新屏幕，但串口协议照常工作。要重试，请检查接线或修改配置后**重新上电/复位**；1.7.0 起固件自己会在故障约 3 分钟后重启一次再试，**最多连续 3 次**（`fault_recovery.h`，重启次数存在 RTC 内存，一次刷新成功就清零，断电也清零），3 次都没救回来就停下，`DIAG recovery: off ...`。BUSY 超时的那一刻固件还会不等 BUSY 直接发断电 / 深度睡眠命令（`DIAG epd fault sleep sent ...`），避免屏幕带着高压停在刷新中。

### 9.5 串口诊断行

诊断行都以 `DIAG ` 开头，和 `OK`/`ERR`/`READY` 互不干扰（电脑端会忽略不认识的行），已有消息的字段和含义都没有改。

```
DIAG fw version=1.3.1 test_pattern=0
DIAG epd probe cmd=0x2F reads=0x01,0x01,0x01 rule=0x01->v2_new,else->v2_old basis=read_0x01
DIAG epd probe cmd=0x2F reads=0xFF,0xFF,0xFF rule=0x01->v2_new,else->v2_old basis=else_fallback note=0xFF_same_as_undriven_DIN
DIAG epd config profile=auto_v2 path=v2_new ry_invert=1 reset_low_ms=2 busy_timeout_ms=5000 refresh_timeout_ms=45000
DIAG epd planes path=v2_new black_cmd=0x24 black_ink_px=21530 ry_cmd=0x26 ry_invert=1 ry_color_px=812 ry_sent_zero_bits=119188 ry_sent_one_bits=812
DIAG epd refresh done path=v2_new busy_ms=15210 total_ms=16480
DIAG epd warn refresh_fast busy_ms=3 (expected ~15000)
DIAG epd fault busy_timeout stage=refresh waited_ms=45000 path=v2_new; refresh stopped until reboot
DIAG epd fault probe_unstable reads=0x01,0xFF,0x01 path=none; refresh stopped until reboot
DIAG display stopped (driver fault, see DIAG epd fault); reboot to retry
```

- `probe` 的 `basis`：`read_0x01` 表示读到 `0x01`，按厂商规则走新路径；`else_fallback` 表示没读到 `0x01`，旧路径只是厂商规则的 else 兜底，**不是型号识别**。`0xFF` 和 DIN 没有被驱动时读到的值相同，所以额外标注 `note=`。
- `planes`：每次刷新前实际送出的位数。`black_ink_px` 是黑平面的 0 位数，`ry_color_px` 是画面要求的红/黄像素数，`ry_sent_zero_bits`/`ry_sent_one_bits` 是红/黄平面实际发出的 0/1 位数（反相后）。它只说明发出了什么，不说明屏幕显示了什么。
- `refresh_fast`：BUSY 很快就释放了，通常说明 BUSY 引脚没有跟着屏幕变化，或者选错了路径。这只是警告，不算故障。
- `busy_timeout` 的 `stage` 可能是 `init`、`swreset`、`power_on`、`refresh`、`power_off`。
- 开机时的 `probe`/`config`/`fw` 行会出现在 `READY` 之前。正式 env 固定路径，所以没有 `probe` 行，`config` 行是 `profile=v2_old path=v2_old ry_invert=0`。
- `fw`：固件版本和是否为诊断构建（`test_pattern=1`）。只有三色构建打印这一行，黑白构建的输出不变。

### 9.5a 诊断构建 `waveshare_esp32_42tri_calib`

这是为“整屏红色、没有图文”准备的诊断目标，**不是默认配置，不要日常使用**。日常请烧正式 env `waveshare_esp32_42tri`，驱动参数相同，只是不显示校准图。它固定走 `v2_old`，`0x13` 发原始 `ry`（`EPD_TRI_V2_OLD_RY_INVERT=0`），开机画面换成三条竖色块（黑 | 红/黄 | 白）和黑字标签（`EPD_TRI_TEST_PATTERN=1`，见 `src/tri_calib.h`），仪表盘不变。

判读：三条色块和标签都正常，说明路径和极性对；整屏红色或黄色、没有黑色图文，多半是 RY 极性与面板不符，可改 `EPD_TRI_V2_OLD_RY_INVERT` 再试；屏幕完全没有变化，先查 BUSY 日志和接线。

### 9.6 颜色

- 黑白平面和红/黄平面同时绘制，每个像素只能是白、黑、红/黄三者之一，所以两个平面的背景始终一致。
- 红/黄色用在：离线时的 `OFFLINE` 标记（红/黄底白字）、各区标题下的分隔线、达到 `EPD_TRI_ALERT_PCT` 的条形图、开机画面的标题。其余部分和黑白版一样。
- 没有 GPU 数据时 GPU 行会隐藏；没有 token 数据时显示 `--`，和黑白版相同。
- v2 界面（见第 7 节）的强调色只用于告警：离线图标、剩余 ≤ 10% 的额度条（含外框）、电量 ≤ 15% 的电池块。热图、趋势、分隔线、标题都不用强调色（强调色不能和抖动混用）。

### 9.7 测试

```bash
# 在 firmware/ 下执行，需要电脑上的 g++（例如 MSYS2）
g++ -std=c++17 -Wall -Wextra -Isrc test/native/test_tri.cpp src/epd_tri.cpp -o test_tri && ./test_tri
```

v2 的测试和预览（电脑端，需要先在 `firmware/` 下跑过一次 `pio run -e waveshare_esp32_42tri` 把库拉到 `.pio/libdeps/`）：

```bash
# v2 协议 / 抖动 / 解码 / 签名测试（一条命令，需 MSYS2 g++；Windows 下 g++ 在 C:/msys64/ucrt64/bin）
g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_dash.cpp src/protocol.cpp -o test_dash && ./test_dash

# 离线预览：编译真正的 src/dashboard_v2.cpp + Adafruit GFX + U8g2，输出 test/preview/out/*.ppm
bash test/preview/run.sh        # 可加参数放大，如 bash test/preview/run.sh 3；再加一个或多个文件名（每行一条协议消息，例如主机 --dry-run-v2 的输出），每个文件另外画成 out/<文件名>.ppm

# 只编译不画图：同时编出上位机 GUI 屏幕预览用的 test/preview/build/epaper_render.exe（见 host/README.md「编译预览渲染器」）
bash test/preview/build.sh
```

- `test_dash` 覆盖：抖动各档的黑点比例、档位嵌套、按绝对坐标对齐（相邻区块不出接缝）；字符串截断（UTF-8 不切半个字）、模型名 `~`、热图 / 趋势字符解码、按长度布局（1 / 4 / 29–36 / 101 / 112 天，旧的 182 天只取最后 112 天）、日历按周排（每周 7 天、按星期几对齐、只显示最后 10 周，窗口长度来自数据长度）、热图各档红 / 黑点数和按坐标对齐、日历放得下右栏、虚线描边四角为黑、趋势取值（未知按 0、不做平均、与日历同样的天）和分段时间轴（近 15 天占右半、右半每步等宽、单调递增、天数少时整宽线性）、误差扩散（纯白 / 纯红 / 纯黑、只用白 + 红时不出黑、50% 灰的黑白比例、白红 / 红黑混合比例、亮度随渐变单调、黑白屏不出红、不画出矩形、跳过的像素不受影响、宽矩形分段）、日期和星期推算；`handleLine` 对 v1 / v2 各消息的解析、`bad_version` / `too_long` / `missing_field` / `bad_field` 等错误码、出错不改数据、`na` 清空区块、界面切换；内容签名（每个显示项变化都会改变，重复消息不变）。
- 预览用示例消息生成：`normal`（正常）、`normal_bw`（黑白屏）、`alert`（额度告警 + 低电量 + OFFLINE）、`stale_off`（数据过期 + 鼠标 off）、`stale_mouse`、`charging`、`na_all`、`partial`（只收到 dash）、`missing`（全缺失）、`full_5w`（35 天全有数据）、`short_window`（12 天）、`long_window`（101 天，只显示最后 5 周）、`bignum_short` / `bignum_long`（大数字字号回退）、`old_dash`（旧格式 dash）、`stress`（4 家 × 3 项额度 + 文字行）。红色是强调色。预览每次运行还会自检告警画法（无告警时额度块没有红色；条形图已用部分是纯白；告警时整条条和百分比文字、`w:1` 段文字是红色实心，不含黑点）。预览只说明绘图代码画出什么，不是实屏照片：墨水屏的对比度、抖动观感、红 / 黄色都要烧录后看。
- 注意：`ARDUINOJSON_ENABLE_*` 几个 `-D` 是因为电脑上用的是 `test/host_shim/Arduino.h` 这个最小替身，不是真正的 Arduino 核心。

```bash
# 离线天气测试（Open-Meteo JSON 解析、提醒、wx_cfg 校验、wx_status、显示 / 取数据规则；用真实返回样本 test/native/wx_open_meteo.inc）
g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_weather.cpp src/weather_parse.cpp src/protocol.cpp -o test_weather && ./test_weather

# 天气页预览（6 个场景，输出 test/preview/out/b_wx_*.ppm）
bash test/preview/build_weather_b.sh
```

下面是三色驱动测试（test_tri）。它用一块模拟屏幕：解码软件 SPI、回应 `0x2F`、驱动 BUSY。这只是软件模拟，不代替实屏。覆盖内容：

- **驱动**：三条路径各自的命令序列、平面寄存器和红/黄极性；指定路径时不读回；读回结果不一致时报故障且不发送数据；BUSY 卡住时在超时内报故障，之后不再尝试；读回 `0xFF` 时日志标为兜底而不是识别。
- **串口不中断**：整个刷新过程中串口服务间隔不超过 1 ms，两次服务之间最多发 51 个 SPI 字节。
- **刷新策略**：三色永远全刷；间隔从刷新结束算起；24 h 保底；故障后停止；黑白版行为不变；模拟 1 小时的数据合并和节流。
- **诊断**：`planes` 行的计数与线上解码出的位数一致；校准图在两种极性下送出的 `0x10`/`0x13` 与手写期望逐字节相符。在“`0x13` 中 0 = 着色”的假设模型下，`ry_invert=1` 会把白底黑字变成整屏着色（只是模型）。
- **开机画面到首个数据画面**：用与 `main.cpp` 相同的 `RefreshScheduler`，开机画面刷新期间收到的多条 stats 只保留最后一条；主机随后断开时，开机画面刷新完成满 180 s 就全刷显示这条 stats 并标记 OFFLINE，不会拖到 24 h 保底刷新。
- **正式 env**：
  - `test_env_flags` 读 `platformio.ini`：正式 env 是 `PROFILE=2`、`RY_INVERT=0`、`TEST_PATTERN=0`，每个只定义一次，没有改 180 s 间隔；诊断 env 不变；两个黑白 env 没有任何三色参数。所以测试必须在 `firmware/` 下运行。
  - `test_production_dashboard`：用正式 env 的驱动配置连发两帧仪表盘样图（在线、带 OFFLINE 标记）。不发 `0x2F`；`0x10`/`0x13` 与绘制的平面逐字节相同（`0x13` 不取反）；每帧都从复位、上电开始，以休眠结束。在 H1 模型下画面与绘制一致、以白底为主（只是模型）。
  - `test_offline_recovery`：主机停发后，超时满 60 s 后的第一次允许刷新显示 OFFLINE 和停发前最后的值；主机不在期间不再刷新；主机恢复后第一次刷新显示 ONLINE 和最新值；所有间隔从刷新完成算都不少于 180 s。

### 9.8 限制

- 正式 env 的参数只在这一块屏上实测过。别的屏需要重新实测：先烧诊断 env 看校准图，再烧正式 env 看仪表盘、离线和恢复。
- 软件 SPI 每次刷新要传约 30 KB，传输时间预计不到 1 秒（未实测），远小于约 15 秒的刷新时间。
- 帧缓冲是两块 15000 字节的静态内存。
- 只实现了 ESP32 驱动板的 env。S3 理论上可以通过 `-DEPD_PANEL=4` 使用，但没有提供 env，也没有测试过。

## 10. WiFi（固件 1.3.0 起，只有 `waveshare_esp32_42tri` 带）

完整契约见 [`docs/dashboard-v2-protocol.md`](../docs/dashboard-v2-protocol.md) 的「WiFi 传输」。这里只写固件怎么做。

- 开关：`config.h` 的 `ENABLE_WIFI`，默认 0；只有 `waveshare_esp32_42tri` 在 `build_flags` 里写了 `-DENABLE_WIFI=1`。别的 env 不编译任何 WiFi / NVS 代码，收到配网命令回 `ERR unsupported`。
- 端口：TCP `7330`（同时只服务一个客户端，新连接顶掉旧的，连上先发 `READY`；1.7.0 起，旧客户端正在升级固件或传待办图片时新连接被直接关闭；每行回复一次 `write()` 发出，开了 TCP keepalive，写失败就关掉这个客户端）；UDP `7331`（收到 `{"v":2,"type":"discover"}` 就把 `here` 单播回去）。USB 和 WiFi 可以同时用，回复发回给发来那一行的连接。
- 配网：用 USB 串口发 `wifi_scan`、`wifi_set`、`wifi_status`、`wifi_clear`、`reset_settings`。从 TCP 发这些命令回 `ERR usb_only`。SSID 和密码存在 NVS 的 `wifi` 命名空间，下次开机自动连。密码不会打印。
- 状态：WiFi 状态变化时，固件在 USB 串口主动发一行 `WIFI {"type":"status",...}`；开机在 `READY` 之后也发一次。连不上（密码错、找不到）20 秒后记为 `failed`，之后每 30 秒重试；已连上的掉线会自动重连。
- 断电保存：最近一次收到的 `dash` / `quota` / `models` / `heat` / `mouse` 原文存在 NVS 的 `snap` 命名空间。内容有变化且距上次写入满 10 分钟才写（保护闪存）；板子从没存过时第一次不等。开机先把存的几行重放一遍，开机画面直接是上次的仪表盘（显示离线）。
- 恢复出厂：发 `reset_settings`，或开机后按住 BOOT 键（GPIO0）5 秒，串口打印 `DIAG settings cleared`。同时清掉 WiFi、保存的最后一屏和 WiFi 升级的配对码。
- WiFi 升级固件（1.3.1 起，`ota.cpp`）：USB 发 `pair` 拿配对码（第一次要时随机生成，存在 NVS 的 `ota` 命名空间）；TCP 上 `ota_begin` 带对配对码后，接下来的字节直接写进另一个 OTA 分区，收满校验 MD5 再切启动分区、重启。升级期间不刷新屏幕；断开、换客户端或 10 秒没数据就放弃，旧固件不动。新固件第一次启动处于"待确认"，有 TCP 客户端连上或收到有效消息才确认（`verifyRollbackLater()` 返回 true，确认时调 `esp_ota_mark_app_valid_cancel_rollback()`）；确认前重启会自动回到旧固件。每个新 TCP 客户端在 `READY` 后收到 `DIAG fw version=...`。
- 刷新墨水屏的 23 秒里，串口、TCP、UDP 照常应答（忙等回调里会调 `pollAll()`）。唯一会卡住的是 `wifi_scan`（2–4 秒）。周期性的快照写 NVS（`settings::service`）只在主循环里做；但在回调里收到的 `wx_cfg` / `page` / `wifi_set` / `img_end` / `reset_settings` 照常写 NVS 或闪存（几十毫秒，位移 SPI 没有时序要求，不影响刷屏）。1.7.0 之前这里写的"NVS 不在回调里写"是不对的。
- 测试：`test_dash` 里有命令解析（含 `pair` / `ota_begin`）、`kind`、写入节奏的检查；WiFi 和升级本身（`net.cpp`、`ota.cpp`）只能上板验证，电脑上没有测试。

## 11. 离线天气（固件 1.5.0 起，同样只有 `waveshare_esp32_42tri`）

契约见 [`docs/dashboard-v2-protocol.md`](../docs/dashboard-v2-protocol.md) 的「离线天气」。

- 上位机离线满 `after_min` 分钟（默认 10，且不短于 `OFFLINE_TIMEOUT_MS` = 120 秒）、WiFi 已连上、天气已开启并有地点时，`main.cpp` 的 `wxService()` 启动取数据任务，取到后把 `ViewState` 切到天气页（`view.weather`）。任何有效的主机消息立刻切回仪表盘。判断规则都在 `weather_policy.h`（纯逻辑，`test_weather` 覆盖）。
- 取数据在单独的 FreeRTOS 任务里（`weather_net.cpp`，栈 16 KB，每次取完自己退出），主循环、串口、TCP、刷屏回调不会被 DNS / TLS / 下载卡住。每 30 分钟一次，失败 5 分钟后重试，失败时保留上一份数据并标 `STALE` / `NO WIFI`。堆空闲小于 70 KB 时不取（错误 `low_mem`）。
- TLS：`WiFiClientSecure::setCACert()` 校验服务器证书链，只信任 ISRG Root X1（`weather_net.cpp` 里的 PEM，到期 2035-06-04）。证书校验需要时间，所以任务先 SNTP 对时（最多等 15 秒，失败报 `sntp`）。页面上的时间用返回的当地时间，不依赖板子时区。
- 解析：`weather_parse.cpp` 用 ArduinoJson 过滤文档只保留用到的字段，输出 `wxb::WeatherB`；提醒文字（雷暴 / 雪 / 暴雨 / 高温 / 低温）也在这里推出。
- 页面只在内容签名变化时重画（`ViewState::signature()` 在天气模式下用 `wxb::signature(wx)`），走原来的 `RefreshScheduler`，所以两次刷新仍然至少隔 180 秒。
- NVS 命名空间 `wx` 存一个 46 字节的整块 `cfg`（`weather_policy.h` 的 `encodeCfg` / `decodeCfg`：版本、`on`、有无地点、`after`、`place`、`lat`、`lon`），一次写入、掉电不会写成一半；1.5.0 / 1.6.0 留下的分开的键（`on` / `place` / `lat` / `lon` / `after`）在没有整块时读一次，下次保存时迁移并删掉。同样的 `wx_cfg` 重复发不会写 NVS；地点换了会同时清掉上次失败的原因。
- 只能上板验证的部分：WiFi + TLS + SNTP 的实际握手、证书链（电脑上用 `openssl s_client -CAfile` 验过链能到 ISRG Root X1，但没有在 mbedTLS 上跑过）、任务栈够不够、与刷屏同时进行时的表现。

## 12. 手动切页、待办页、立即刷新（固件 1.6.0 起）

契约见 [`docs/dashboard-v2-protocol.md`](../docs/dashboard-v2-protocol.md) 的「手动切页、待办页、立即刷新」。

- **切页**：`page` 把 `auto|dash|weather|todo` 存到 NVS 命名空间 `page`（`settings::pageSave`），`main.cpp` 的 `wxService()` 据此设置 `view.weather` / `view.todo`。强制 `weather` 时按 `page_policy.h` 的 `forcedFetchDue` 取数据（没有数据或超过 30 分钟，不要求 `wx_cfg` 开启），第一次取数据期间 `refreshIfNeeded()` 最多挂起 30 秒（`holdForFetch`）；等满 30 秒仍画了占位页时，数据随后取到会触发一次 `refreshNow`（`page_policy.h` 的 `bypassAfterFetch`，1.7.0 起），真实数据马上显示，不用再等 180 秒。`page_status` 回 `PAGE {...}`。
- **待办图片**：`img_begin` / `img_chunk` / `img_end` 由 `img_rx.h` 的 `imgrx::Rx` 组装（暂存区是 `malloc(size)` 的堆缓冲，只在传输期间存在，20 秒没有新块就丢弃并发 `DIAG img timeout`），校验 crc32 和 RLE 之后才写入 `todo_store.cpp`：默认分区表里没用的 `spiffs` 数据分区（0x290000），直接 `esp_partition_*` 读写，16 字节头最后写。显示时 `ui.cpp` 的 `drawTodo()` 把压缩数据读进临时缓冲，直接解码进屏幕的两个平面再转成面板极性（`imgrx::hostToPanel`），不需要新字库、也没有额外的 30 KB 缓冲。没有图片画 `NO TODOS` 空状态页。
- **立即刷新**：`refresh_now` 设 `RefreshScheduler::refreshNow`，`decideRefresh()` 看到它就跳过 `EPD_TRI_MIN_REFRESH_MS` 并且内容没变也重画，一次有效；刷新进行中收到的留到这次刷完立刻执行。自动更新仍然是 180 秒。厂商建议三色屏两次刷新至少隔 180 秒，频繁强刷由用户自己决定。
- 这些命令都不算"上位机在线"。`reset_settings` / 长按 BOOT 会把页面清回 `auto` 并擦掉待办图片。
- 测试（`firmware/` 下，需要先跑过一次 `pio run -e waveshare_esp32_42tri` 拉库；读 `../docs/todo-image-fixtures.json`，和上位机编码器共用）：

```bash
g++ -std=c++17 -Wall -Wextra -DARDUINO=100 -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 -DARDUINOJSON_ENABLE_PROGMEM=0 -Itest/host_shim -Isrc -I.pio/libdeps/waveshare_esp32_42tri/ArduinoJson/src test/native/test_img.cpp src/protocol.cpp -o test_img && ./test_img
```

  `test_img` 覆盖：crc32 / base64、RLE 编码（规范）与解码（含非法流）、共享整页样例、分块重组（乱序、重复、越界、`bad_crc`、`incomplete`、`bad_rle`、超时、重新开始）、`page` / `page_status` / `refresh_now` / `img_*` 的解析和错误码、强制页规则、`refresh_now` 只绕过一次间隔。
- 只能上板验证的部分：`esp_partition` 读写 `spiffs` 分区、屏幕上的实际画面、与刷屏同时收图片时的表现、`low_mem` 阈值。
- 固件体积：1.5.0 → 1.6.0 增加约 5 KB flash、0.9 KB RAM；OTA 应用分区 1.31 MB，1.6.0 用 85.1%。

## 13. 1.7.0 可靠性改动

只改稳定性，协议和画面不变。逐项对应的代码和测试：

| 改动 | 代码 | 测试 |
|---|---|---|
| BUSY 超时 / 任何驱动故障：不等 BUSY 直接发断电 + 深度睡眠（`0x02` `0x07 0xA5`，v2_new 是 `0x10 0x03`） | `epd_tri.cpp` `emergencySleep()` | `test_tri`：三条路径 + 初始化超时 + 探测不稳定时不发 |
| 故障 3 分钟后自动重启，RTC 计数最多连续 3 次，一次刷新成功清零，DIAG 报告 | `fault_recovery.h`、`main.cpp` `faultRecovery()` | `test_tri` `test_fault_recovery` |
| `img_end` 和已存图片大小 + crc 相同：回 `OK` 不擦写 | `img_rx.h` `sameAsStored()`、`main.cpp` `runImg` | `test_img` |
| OTA 确认失败不再当作已确认（`CONFIRM_OK` / `NOT_PENDING` / `FAILED`），下次再试 | `ota.cpp`、`main.cpp` `confirmFirmware()` | 只能上板 |
| 星期推算：日期字段必须是数字，取模不出负数 | `weather_parse.cpp` | `test_weather` |
| 天气 HTTP 正文流式读取，超过 `BODY_MAX`（12 KB）立即放弃，另有无数据 10 秒 / 总 30 秒超时 | `weather_net.cpp` `readBody()` | 只能上板 |
| 回复 / DIAG 每行一次 `write()`；TCP keepalive；DIAG 在发送缓冲将满时丢弃；写失败关闭客户端 | `net.cpp` `TcpOut` | 只能上板 |
| 升级 / 传图期间新 TCP 连接被关闭，不顶掉旧的 | `net.cpp` `pollTcp()`、`Handlers::keepClient` | 只能上板 |
| 天气任务 90 秒看门狗（原因 `timeout`），任务真正退出前不开新取数据；每次取数据报栈余量；升级 / 传图时不取数据 | `weather_net.cpp`、`main.cpp` | `test_weather`（`fetchOverdue`）；任务本身只能上板 |
| 天气地点必须是合法 UTF-8（和 Wi-Fi 扫描共用 `utf8.h`） | `weather_policy.h`、`utf8.h` | `test_weather` |
| 强制天气页画了占位后数据到达：绕过一次 180 秒（选了"绕过"，没有拉长 30 秒等待） | `page_policy.h`、`main.cpp` | `test_img` |
| 天气配置存成一个 NVS 整块，旧键读一次后迁移 | `settings.cpp`、`weather_policy.h` | `test_weather`（编码 / 解码 / 损坏） |
| `signature()` 不再每轮循环重算：先看间隔是否允许刷新；有改动用 `markDirty()` 通知，最长 1 秒复查一次；刷新时间点不变 | `refresh_policy.h`、`main.cpp` | `test_img`（逐点对比刷新时刻、计数） |
| 删掉证书 PEM 后多余的 `# ISRG Root X2` 文本；`toPct` 拒绝 nan / inf；换地点清掉上次失败原因；`test_tri` 的初始化器警告 | 各自文件 | `test_dash`、`test_tri`（用 `-Wall -Wextra` 编译无警告） |

没做的：
- **F18（三色版编译掉 v1 路径省 RAM）**：三色版仍接受 v1 消息（协议文档和测试都覆盖），能省的只有 `Stats` 结构里十几个 `String`，约几百字节，不值得加编译开关。
- **F19（硬件 SPI 驱动屏幕）**：现在的位移 SPI 是照厂商时序写的，并且有模拟屏幕的测试，换成硬件 SPI 要重新在实屏上验证，不是小改动。

测试命令和之前一样（`test_img` 必须在 `firmware/` 下跑）。固件体积：1.6.0 → 1.7.0，`waveshare_esp32_42tri` flash +2.3 KB、RAM +0.7 KB；`waveshare_esp32` flash +2.0 KB、RAM +0.1 KB。
