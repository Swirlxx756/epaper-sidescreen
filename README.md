# epaper-sidescreen

ESP32 墨水屏副屏。电脑上的 Python 程序读 Token Monitor 的本机文件和 Rapoo 鼠标电量，通过 USB 串口一行一行发 JSON 给 ESP32，ESP32 把它画到 4.2 寸三色墨水屏上。

```
电脑 (host/) --USB 串口 115200，JSON 行--> ESP32 (firmware/) --SPI--> 墨水屏
```

## 目录

```
firmware/   ESP32 固件（PlatformIO + GxEPD2），含电脑端测试和离线预览
host/       电脑端程序：epaper_host.py（命令行）、gui.py（小窗口）、测试
tools/      串口抓取等辅助脚本
docs/       使用指南和串口协议
```

## 当前状态

- 屏幕：微雪 e-Paper ESP32 Driver Board + 4.2 寸 400×300 三色屏（黑 / 白 / 红）。
- 固件 1.2.0，串口协议 v2，env 是 `waveshare_esp32_42tri`。
- 仪表盘 v2 布局：
  - 左栏：TOKENS（总量）、QUOTA（各家额度）。
  - 右栏：ACTIVITY 日历热图、TREND 趋势、MODELS 模型排行。
  - 右上角：今天日期（MM-DD）。左下角：数据时间、鼠标电量和在线图标。
- 还保留旧的黑白屏 env（`waveshare_esp32`、`esp32s3`），用法见 `docs/guide.md`。

## 刷新规则

三色屏只做全屏刷新，一次约 15～25 秒。

两次刷新之间，必须从上一次刷新结束起至少隔 180 秒。所以固件开机后先显示开机画面，第一个数据画面要等约 3 分钟才出现。

电脑端每 30 秒发一次数据，这只是发送频率，不是屏幕刷新频率。屏幕在画面有变化时刷新，最快每 3 分钟一次；想刷得慢一点，在上位机「设置」标签的「显示设置」→「屏幕刷新」里选 5–60 分钟（配置 `v2.refresh_min`）。

## 离线天气页

电脑关机或程序退出后，板子在设定的分钟数（默认 10）后自己通过 Wi-Fi 取 Open-Meteo 天气（免费、无需 key），屏幕换成天气页；电脑回来就切回仪表盘。在上位机「设置」标签的「离线天气页」里勾选「电脑停止推送 N 分钟后，屏幕自动显示天气」、输入城市名按 Enter 搜索并选一个城市即可，设置存在 `host/config.json` 的 `v2.weather`，每次连接都会同步给板子。需要板子已配好 Wi-Fi，并烧录带天气页的固件（旧固件会提示「不支持」，不影响仪表盘）。详见 `docs/guide.md` 第 7.4 节。

## 手动切换页面、待办页、立即刷新

上位机左列「页面」可固定显示 仪表盘 / 天气 / 待办，或切回自动；「推送到屏幕」把选中的页面发给板子并马上刷新，「立即刷新屏幕」不换页、只让屏幕马上整屏刷新，都绕过最短间隔（频率由你自己决定），期间显示「屏幕刷新中，约 20 秒…」，完成后显示「屏幕已刷新 时:分:秒」。左列「待办」里编辑事项（完成 / 重要 / 排序，改了右列「屏幕预览」自动切到待办页），上位机用 Pillow 把整页画成 400×300 的黑红位图，PackBits 压缩后分块发给板子，板子不需要中文字库。需要固件 1.6.0 以上，旧固件会提示「板子固件太旧」。详见 `docs/guide.md` 第 7.5 节。

## 编译和烧录

先关掉电脑端程序和串口监视器，再在 `firmware/` 目录执行：

```bash
pio run -e waveshare_esp32_42tri -t upload --upload-port COM3
```

COM 号以 `py -3.14 host\epaper_host.py --list-ports` 当场列出的为准。

PlatformIO 如果不在 PATH 里，用 `~/.platformio/penv/Scripts/pio.exe`。

这个 env 固定用 `v2_old` 驱动路径和 `EPD_TRI_V2_OLD_RY_INVERT=0`。这块屏上实测这组参数显示正确，旧默认值会整屏变红。细节见 `firmware/README.md` 第 9 节。

## 运行电脑端程序

小窗口（推荐，打开后自动连接；关窗口收进托盘，右键托盘图标「退出」才退出；「设置」标签里可勾开机自启）：

```bat
pyw -3.14 host\gui.py
```

命令行：

```bat
py -3.14 host\epaper_host.py --port COM3
```

一个串口同一时间只能被一个程序打开。烧录前先关掉它们。

## 配置

把 `host/config.example.json` 复制成 `host/config.json` 再改。`config.json` 不进 git。

常用键有 `port`、`interval`，以及 `v2` 下的 `primary`（今日 / 累计）、`heat_weeks` 和 `calendar`（活动块显示为日历）。完整说明见 `host/README.md`。

## 测试

电脑端程序（在仓库根目录）：

```bat
py -3.14 -m unittest discover -s host/tests
```

固件电脑端测试（`test_tri`、`test_dash`）的命令见 `firmware/README.md` 第 9.7 节。

离线预览屏幕（在 `firmware/` 目录，数字是放大倍数）：

```bash
bash test/preview/run.sh 3
```

预览图输出在 `firmware/test/preview/out/`。它只反映绘图代码，墨水屏上的对比度和红色要烧录后看实屏。

## 文档

- `docs/guide.md`：接线、装环境、烧录、运行、排错的完整指南（含黑白屏）。
- `docs/dashboard-v2-protocol.md`：v2 串口协议。
- `firmware/README.md`：固件参考手册（env、配置、协议、三色屏参数、测试）。
- `firmware/VENDOR_SOURCES.md`：三色驱动对应的微雪厂商代码来源。
- `host/README.md`：电脑端程序参考（数据来源、配置、v2、GUI、开机自启）。
