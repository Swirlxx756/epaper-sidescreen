# 墨水屏副屏 · 电脑端程序

Windows 上运行的 Python 程序。它每 30 秒采集一次系统状态（CPU、内存、磁盘、网速、NVIDIA GPU）和 AI token 用量（默认通过 tokscale 获取，也可以改用 ccusage），然后按协议 v1 通过 USB 串口发送给 ESP32。协议说明见 `firmware/README.md` 第 6 节。固件是 v2 时，程序改发仪表盘 v2 消息，见第 8 节。

## 1. 安装

1. 安装 Python 3.10 或更高版本。安装时勾选 “Add python.exe to PATH”。
2. 安装依赖：

   ```bat
   cd host
   pip install -r requirements.txt
   ```

3. token 数据默认来自 [tokscale](https://github.com/junhoyeo/tokscale)，也就是 Token Monitor 桌面程序内置的统计引擎：
   - 装了 Token Monitor 就不需要再装别的。程序会自动使用它自带的 `tokscale.exe`，位置是 `%LOCALAPPDATA%\Programs\Token Monitor\resources\app.asar.unpacked\node_modules\@tokscale\cli-win32-x64-msvc\bin\`。Token Monitor 不需要处于运行状态，它的设置也不用改。
   - 没装 Token Monitor 时，可以单独安装：`npm i -g tokscale`。程序会优先使用 PATH 里的 `tokscale`。
   - Claude 官方额度（Session/Weekly %）由 `tokscale usage` 读取本机的 Claude Code 登录状态获得，需要联网。
   - 也可以在 config 里设置 `"token_source": "ccusage"`，改用 [ccusage](https://github.com/ryoppippi/ccusage)（`npm i -g ccusage`）。ccusage 只统计 Claude Code，也没有官方额度。
   - 工具没有安装，或者调用失败时，`tok` 里对应的字段为 `null`，屏幕上显示 `--`，程序照常运行。
4. GPU 数据需要能在命令行运行 `nvidia-smi`。没有这个命令时不发送 `gpu` 和 `gpu_temp`，屏幕上的 GPU 两行会隐藏。

## 2. 运行

### Windows 4.2 寸三色屏

在 `host` 目录运行，先列端口，再显式指定本次设备端口（COM5 只是示例）：

```bat
python -m pip install -r requirements.txt
python epaper_host.py --list-ports
run_host_tricolor.bat --port COM5
```

等价命令为 `python epaper_host.py --port COM5`。三色入口沿用原采集与配置，约每 30 秒发送最新 stats。这是发送频率，不是屏幕刷新频率：**屏幕由三色固件控制，从上一次刷新完成起至少 180 秒才刷下一次**。批处理不改屏幕类型、不烧录。`OK` 只表示消息已收到，不代表画面已刷新。三色屏的烧录和刷新规则见 [`README.md`](../README.md) 和 [`firmware/README.md`](../firmware/README.md) 第 9 节。

不采集真实数据、只发固定示例（协议 v1，顶部显示 `DEMO 12:34`）的入口是 `dashboard_demo.py`。它不读 `config.json`，不调用 psutil、nvidia-smi、tokscale、ccusage。两个程序不能同时占用同一串口。

```bat
python -S dashboard_demo.py --dry-run       只打印一条 JSON，不开串口
python dashboard_demo.py --list-ports       只列端口
python dashboard_demo.py --port COM5        持续发送；Ctrl+C 停止
```

发送模式只需要 pyserial，不要加 `-S`。

**正常停止**：常驻模式按 `Ctrl+C`，批处理询问 `Terminate batch job (Y/N)?` 时选 `Y`。`tools/host_runner.py` 用 `--stop-file` 触发同一套正常退出。停止只表示不再发送 stats；屏幕可能还有一次离线刷新，串口关闭不等于刷新完成。

批处理接受全部原参数，例如 `--config my-tricolor.json --port COM5`、`--baud 115200`、`--log host.log`。它不会创建或覆盖 `config.json`；没有配置文件时用内置默认值，有旧配置时仍读取它。需要独立配置时，将 `config.example.json` 复制到一个尚不存在的文件再编辑，用 `--config` 指定。`--port`、`--baud`、`--interval` 命令行值优先。

无需硬件的真实采集检查（不打开任何串口）：

```bat
run_host_tricolor.bat --dry-run --once
python epaper_host.py --dry-run --once
```

JSON v1 单行输出在 stdout，状态/错误在 stderr。`--once` 首次会等待 token 后台采集，默认最多 `3 × token_timeout + 5 = 140` 秒，之后留 1 秒 CPU/网速采样窗口。工具失败时对应 token 为 null；常驻模式先发送缓存值，后台完成后再使用新值。按 **Ctrl+C** 退出，包括首次等待期间。不要同时启动两个实例占用同一端口。

### 原入口与连接状态

```bat
python epaper_host.py --dry-run --once   不开串口，只打印一条 JSON，用来自测
python epaper_host.py --list-ports       列出串口，并显示自动探测会选哪一个
python epaper_host.py                    自动探测串口，一直运行
python epaper_host.py --port COM5        指定串口
python epaper_host.py --interval 5       每 5 秒发送一次
python epaper_host.py --once             发送一条后退出（会打印板子回复的 OK/ERR）
python epaper_host.py -v                 输出调试日志
python epaper_host.py --log host.log     日志同时写入文件
```

也可以直接双击 `run_host.bat`。程序出错退出时，窗口会停住，方便查看错误信息。用批处理时，Ctrl+C 后 cmd 会询问 `Terminate batch job (Y/N)?`（选 `Y`）；`--once` 非零退出也会 `pause` 停在 `Press any key`，需要直接读取 0/2/130 退出码时请改用 `python epaper_host.py ...`。命令行参数可以直接加在后面，例如 `run_host.bat --port COM5`。

- **端口发现**：只枚举系统报告的端口、VID:PID 和描述，不打开或发送探测数据。VID `303A` / `1A86` / `10C4` 或 CH343 等名称仅是 USB 候选提示，不能证明 MCU、板卡或屏幕型号。只有一个候选才自动选择；多个候选时不发送，要求 `--port`（与旧版按 VID 优先级选取的行为不同）。自动模式每次重连重新枚举，可恢复拔插后的 COM 号变化，每次最多打开一个唯一候选。显式 `--port` 或配置 `port` 时只重连指定端口；这种情况下端口号改变需用新端口重启。
- **三层状态**：`serial port opened` 仅表示串口句柄打开；`protocol response received` 表示收到 READY/OK/ERR；`OK` 仅表示消息接收，**不表示实际画面已显示**。ERR 明确记录为协议错误。实际画面必须后续人工联调确认。
- **握手/无响应**：打开时只发一次 JSON v1 ping。常驻模式继续约每 30 秒发 stats；120 秒内未收到 READY/OK/ERR 时给出握手告警，已有响应但静默 180 秒时给出无响应告警（这里的 180 秒是主机静默告警阈值，和固件的屏幕刷新间隔是两回事；刷新间隔见上一节）。检查端口、波特率和固件；长刷新也可能导致延迟。静默仅限频告警，不自动复位或反复开关串口。乱码/未知行与超长行也有诊断，接收缓冲有上限。配置可调长超时。
- **断线恢复**：读/写失败或串口关闭后，关闭旧串口、停止并回收旧 reader 后才重连。重试至少间隔 `reconnect_interval`（默认 10 秒，受采样间隔影响），不轮流试多个设备。若长刷新期间固件不读串口而导致发送缓冲阻塞，2 秒 `write_timeout` 抛出的写超时也会触发上述重连；仅未收到回复但写入正常时仍不重开。DTR/RTS 保持低，不主动触发复位。
- **单次发送**：串口 `--once` 最多等 `handshake_timeout` 得到 ping 的 OK，随后发送一次 stats，再最多等 `response_timeout` 得到后续 OK；超时、ERR 或断线返回退出码 2。串口单次模式退出码 0 仅表示收到 OK，仍不表示刷新。JSON v1 无消息 ID，无法严格逐条关联 OK；常驻模式不要求每条确认。dry-run 完成输出返回 0，配置/依赖错误返回 1。任何 `--once` 模式（含 dry-run）在等待 token、等待 OK 或其他完成前阶段按 Ctrl+C，返回 130，表示单次操作中断；常驻模式 Ctrl+C 正常退出仍返回 0。
- **日志**：输出到 stderr，可用 `--log host.log` 留存。READY/OK/ERR 保持兼容；可选独立 `DISPLAY ...` 行原样记录为“固件报告的显示诊断”，不据此宣称实屏通过，不影响协议应答超时。该行非必需，主机不依赖它。
- **token 数据**：tokscale 或 ccusage 在后台线程里运行，每 60 秒刷新一次（需要联网的额度查询每 180 秒一次）。主循环使用缓存的结果，所以这些工具运行慢或者卡住都不会影响发送。

## 3. 配置文件（可选）

把 `config.example.json` 复制为同目录下的 `config.json`，然后修改。命令行参数的优先级高于配置文件。也可以用 `--config 路径` 指定其他配置文件。

| 键 | 默认值 | 说明 |
|---|---|---|
| `port` | `null` | 串口号，例如 `"COM5"`；走 Wi-Fi 写 `"tcp://192.168.1.50"`（端口默认 7330）。`null` 表示自动探测串口 |
| `transport` | `"serial"` | 只给图形界面用：`"serial"` 或 `"wifi"`，界面里切换时自动保存 |
| `wifi_addr` | `null` | 只给图形界面用：上次连过的板子地址，例如 `"192.168.1.50"`，自动保存 |
| `ota_token` | `null` | 板子的配对码，Wi-Fi 升级固件用。USB 连上时图形界面自动保存，命令行用 `--pair` |
| `baud` | `115200` | 波特率，必须和固件一致 |
| `interval` | `30` | 采样发送间隔（秒），不是屏幕刷新间隔 |
| `handshake_timeout` | `120` | 初始协议响应诊断期限；once 等 ping OK 上限（秒） |
| `response_timeout` | `180` | 响应静默告警间隔；once 等 stats OK 上限（秒） |
| `reconnect_interval` | `10` | I/O 断线重连最小间隔（秒，最低 5） |
| `token_source` | `"tokscale"` | token 数据源：`"tokscale"` 或 `"ccusage"` |
| `token_interval` | `60` | token 数据的刷新间隔（秒） |
| `token_timeout` | `45` | 单次命令行调用的超时时间（秒） |
| `tokscale_cmd` | `null` | tokscale 的路径。`null` 表示先在 PATH 里找，找不到再用 Token Monitor 自带的 exe |
| `tokscale_clients` | `null` | 统计哪些工具。`null` 表示全部（和 Token Monitor 的默认视图一致）；只统计 Claude Code 填 `"claude"`，多个用逗号分隔，例如 `"claude,codex"` |
| `limits_interval` | `180` | `tokscale usage`（官方额度，需要联网）的刷新间隔（秒） |
| `ccusage_cmd` | `null` | ccusage 的路径。`null` 表示在 PATH 里查找 `ccusage.cmd` |
| `disk` | `null` | 统计哪个盘的占用率，例如 `"D:\\"`。`null` 表示系统盘 |
| `gpu` | `true` | 是否调用 `nvidia-smi` |
| `v2` | 见第 8 节 | 仪表盘 v2 的选项：`enabled`、`tm_file`、`mouse`、`primary`、`heat_rows`、`heat_weeks`、`calendar`、`refresh_min`、`weather` |

## 4. 发送的数据

示例（单行，最长 1024 字节）：

```json
{"v":1,"type":"stats","time":"10-28 01:07","cpu":23,"mem":62,"disk":83,"net_up":"38.5K","net_down":"13.0K","gpu":7,"gpu_temp":47,"tok":{"block_tokens":"345.2M","block_cost":89.47,"burn_tpm":"14.5K","remain_min":92,"today_tokens":"152.3M","today_cost":37.09,"session_pct":50,"weekly_pct":17}}
```

| 字段 | 来源 |
|---|---|
| `time` | 本地时间，格式 `MM-DD HH:MM` |
| `cpu` / `mem` / `disk` | psutil 读取的百分比，取整 |
| `net_up` / `net_down` | 两次采样之间的平均网速，单位 B/s，格式如 `120K`、`3.4M` |
| `gpu` / `gpu_temp` | `nvidia-smi` 读取的利用率（%）和温度（°C） |
`tok` 的来源取决于 `token_source`。

**tokscale（默认）**。统计范围由 `tokscale_clients` 决定：

| 字段 | 来源 |
|---|---|
| `tok.today_tokens` / `today_cost` | `tokscale models --today --json`：今日 input+output+cache+reasoning 的合计，以及 `totalCost` |
| `tok.session_pct` / `weekly_pct` | `tokscale usage --json`：Claude 官方额度中 `Session`（5 小时窗口）和 `Weekly` 的 `used_percent` |
| `tok.remain_min` | 距离 Claude Session 窗口重置的分钟数（`resets_at`） |
| `tok.block_tokens` / `block_cost` | `tokscale hourly --json`：当前 Session 窗口内（重置时间往前 5 小时到现在）的 tokens 和费用。按小时汇总，窗口开始的那个小时整段计入 |
| `tok.burn_tpm` | 窗口内平均每分钟的 input+output tokens，不含 cache，口径和 ccusage 的 burn rate 指示一致 |

`session_pct` 和 `weekly_pct` 是协议 v1 之外新增的字段。当前固件会忽略它们，屏幕上显示什么以后再定。

**ccusage**（只统计 Claude Code）：

| 字段 | 来源 |
|---|---|
| `tok.block_tokens` / `block_cost` | `ccusage blocks --active --json`：当前 5 小时块的 `totalTokens` 和 `costUSD` |
| `tok.burn_tpm` | 同上：`burnRate.tokensPerMinuteForIndicator` |
| `tok.remain_min` | 同上：`projection.remainingMinutes` |
| `tok.today_tokens` / `today_cost` | `ccusage daily --json --since <今天>`：今日的 `totalTokens` 和 `totalCost` |

当前没有活跃的块时，`block_tokens`、`block_cost` 和 `burn_tpm` 发送 0，`remain_min` 发送 `null`。

## 5. 打包成 exe（可选）

```bat
pip install pyinstaller
cd host
pyinstaller --onefile --name epaper_host epaper_host.py
```

生成的文件是 `dist\epaper_host.exe`。如果需要配置文件，把 `config.json` 放到 exe 所在的目录。

如果想在后台运行、不显示控制台窗口，打包时加上 `--noconsole`，运行时加 `--log host.log` 把日志写到文件。也可以不打包，直接用 `pythonw epaper_host.py --log host.log`。

## 6. 开机启动（可选）

用图形界面的话，直接勾「显示设置」里的「开机自启」（见上面图形界面一节），下面两种不用做。下面是命令行版 `epaper_host.py` 的做法。

**方法 A：放到启动文件夹（最简单）**

1. 按 `Win + R`，输入 `shell:startup`，回车，会打开启动文件夹。
2. 在这个文件夹里新建 `run_host.bat` 的快捷方式（或者 exe 的快捷方式）。如果需要指定串口，在快捷方式属性的“目标”后面加上 ` --port COM5`。

**方法 B：任务计划程序（不显示窗口，登录后自动启动）**

```bat
schtasks /Create /TN "EpaperHost" /SC ONLOGON /RL LIMITED ^
  /TR "\"C:\path\to\pythonw.exe\" \"C:\path\to\host\epaper_host.py\" --log C:\path\to\host\host.log"
```

删除这个任务：`schtasks /Delete /TN "EpaperHost" /F`。

注意：同一个串口同时只能被一个程序打开。用 PlatformIO 烧录固件或者打开串口监视器之前，要先关掉本程序。

## 7. 软件验证与后续联调

从仓库根目录运行：

```bat
python -m unittest discover -s host/tests -v
```

测试使用模拟串口，覆盖长刷新期间持续应答、无回应、乱码/ERR、读写断连、恢复与资源回收、原入口 dry-run 的真实系统采集格式，以及 `dashboard_demo.py` 的固定示例。测试不打开真实设备、不安装驱动、不烧录。

接真实三色屏时：先 `--list-ports` 选对 COM，再运行主机；烧录或重启后，屏幕要等 180 秒以上才会换成数据画面。`OK`/`refresh done` 只说明串口和驱动正常，画面要看实屏。

## 8. 仪表盘 v2（固件是 v2 时）

协议见 [dashboard-v2-protocol.md](../docs/dashboard-v2-protocol.md)。v2 的数据**不再调用 tokscale / ccusage / psutil**，只读 Token Monitor 写在本机的文件：

```
%APPDATA%\Token Monitor\hub-devices.json
```

Token Monitor 是 Hub，大约每分钟把两台电脑的数据写进这个文件。本程序只读，不改它。文件正在被写、读到半截时，会沿用上一次读好的数据（日志里记一条警告）。文件换了路径，在 config 里写 `v2.tm_file`。

### 怎么运行

正常运行不用改：`python epaper_host.py --port COM5`。连上后程序自己判断固件版本：

- 固件打印 `READY ... v2 ...`，或者对主机发的 v2 `ping` 回 `OK`：走 v2。
- 固件只有 v1 的 `READY`，或者对 v2 消息回 `ERR bad_version`：走原来的 v1 `stats`，行为和以前一样。
- 板子一直不回应：和以前一样发 v1 `stats`，只告警。

不想用 v2 时，在 config 里写 `"v2": {"enabled": false}`。

两个显示选项写在 config 的 `v2` 里，**改完保存，最多 10 秒内生效，不用重启**（读不出来或值不合法时沿用旧值）：

| 键 | 值 | 说明 |
|---|---|---|
| `primary` | `"today"`（默认）/ `"total"` | TOKENS 块展开哪个周期，模型排行也跟着切换。命令行 `--primary today|total` 优先于配置，用了命令行就不再跟配置走 |
| `heat_rows` | `7`（默认，按星期对齐）/ `5` | 热图每列几格 |
| `heat_weeks` | `10`（默认），范围 4–26 | 热图和趋势取最近几个整周（屏上日历 7 行 × 最多 10 列，多于 10 周只显示最后 10 周） |
| `calendar` | `false`（默认）/ `true` | `true`：ACTIVITY 块画成当月日历，标题变成 CALENDAR（`heat` 消息带 `"view":"cal"`）。图形界面的「活动显示为日历」就是它，改了立即重发 |
| `refresh_min` | `3`（默认），可选 3 / 5 / 10 / 15 / 30 / 60 | 屏幕最多几分钟刷新一次（`dash` 消息带 `every` 秒数）。3 = 三色屏最短间隔，也就是原来的行为；画面没变化时不会刷新。开机后第一个数据画面不受它影响。图形界面的「屏幕刷新」就是它，改了立即发给板子 |
| `weather` | 不设 = 不管天气 | 离线天气页：`{"on", "place", "lat", "lon", "after_min"}`（`after_min` 1–240）。图形界面「离线天气页」里搜城市、勾选即写入；每次（重）连接和改动后，GUI 与 `epaper_host.py` 都会发 `wx_cfg` 给板子（`on:false` 或没位置时只发关闭）。旧固件回 `ERR` 时只记一条日志、不再重试。城市搜索用 Open-Meteo 地理编码，逻辑在 `weather.py` |
| `todo` | 不设 = 空列表 | 待办页内容：`{"items":[{"text","done","important"}]}`。图形界面「待办」标签里增删改、排序、勾选完成 / 重要；`page` 记住上次成功推送的页面（`auto`/`dash`/`weather`/`todo`，只用来决定启动时选中哪个单选按钮，不会在连接时重发；点单选框不写它，推送成功后才写）。渲染、PackBits 压缩和分块都在 `todo_page.py`（Pillow，中文用 Windows 自带的微软雅黑 / 黑体，二值化、无抗锯齿） |

**页面、待办、立即刷新（固件 1.6.0 起）**：「页面」里选 自动 / 仪表盘 / 天气 / 待办只切换「屏幕预览」标签，什么也不发；点「推送」发 `page`、「立即推送」发 `page` 和 `refresh_now`，两者都让五个数据块下一轮全部重发（推送待办页且待办改过 / 还没推过时，同一条命令里先带上 `img_begin` / `img_chunk` / `img_end`，最多刷新一次）；`refresh_now` 绕过固件 180 秒的最短间隔（刷新太勤由用户自己决定，Waveshare 建议三色屏至少间隔 180 秒）。推送待办页时把待办整页渲染成 400×300 黑 / 红两个位平面，每个平面做 PackBits，两段首尾相接，base64 分块（每块 600 字节）用 `img_begin` / `img_chunk` / `img_end` 发出，最后发 `refresh_now`；预览不连板子也能看。命令都走工作线程的命令通道（两次推送之间，`HostWorker.request_command` 的 `msg` 可以是一个列表，按顺序发、第一条失败就停）。旧固件回 `ERR unknown_type` 时只在界面上显示「板子固件太旧」一行，不弹窗、不重试。PackBits 测试向量在 `tests/fixtures/packbits_vectors.json`。

`--once` 默认仍然发 v1。要发一轮 v2 就加 `--v2`：`python epaper_host.py --once --v2 --port COM5`。`--v2` 也可以不加 `--once`，表示不判断版本、一直用 v2。

不开串口，只看五条消息长什么样：

```bat
python epaper_host.py --dry-run-v2
```

JSON 一行一条打印在 stdout（顺序 `dash`、`mouse`、`quota`、`models`、`heat`；可加 `--primary today|total`），每条的字节数写在 stderr。这个命令不打开鼠标接收器，所以 `mouse` 一条总是 `"st":"na"`。读不到文件时返回 1。

### 图形界面（gui.py）

不想敲命令行时，用小窗口代替 `epaper_host.py`（tkinter 窗口 + 托盘图标；托盘要 `pystray` 和 `Pillow`、主题要 `sv-ttk`，见 `requirements.txt`，没装时窗口照常用（托盘缺了就是关窗口即退出，`sv-ttk` 缺了就是原生外观）；v2 固件专用）：

```bat
py -3.14 gui.py        带控制台，日志也会打到终端
pyw -3.14 gui.py       无控制台
pyw -3.14 gui.py --tray   不弹窗口，直接在托盘里运行（开机自启用的就是这个）
```

窗口分两列（默认约 1000×680，可以拉大；高分屏按系统缩放自动放大）。装了 `sv-ttk` 时是 Windows 11 风格，并跟随系统的浅色 / 深色模式（没装就用原生主题，功能一样）。

**左列**（内容比窗口高时可以上下滚动，滚轮在左列任意处都能滚；下拉框、数字框不响应滚轮，免得误调）

- **连接**：「方式」选 USB 串口或 Wi-Fi（见第 9 节）。USB 串口方式：下拉框第一项「自动」（让程序自己找板子），后面是电脑上的串口；默认取 config 里的 `port`，没有就是「自动」。找不到任何串口时，下拉框下方提示「没有找到串口：用数据线连接板子，然后点「重新扫描」」。点「连接」后状态灯显示 未连接 / 连接中 / 已连接 v2 / 已连接 v1（不支持）/ 错误，旁边是上次推送时间。连不上、程序在自动重试时，串口和地址输入框保持可改：改好后先点「断开」再点「连接」。已连接但超过 90 秒没有收到板子的回复时，状态改为「无响应」。板子是 v1 固件时只显示状态，不推送。状态下面是板子信息：固件版本、Wi-Fi 状态、屏幕状态（上次刷新完成的时间 / 驱动故障）。「方式」那一行右边是「配网…」（只有 USB 串口连着 v2 板子时可点）和「升级固件…」（只有 Wi-Fi 连着、已配对时可点）。升级期间出现进度条；这时点「断开」或退出程序会先问你确认。配网窗口是模态的，Esc 关闭、Return 等于「写入」。
- **显示设置**：主显示（今日 / 累计）、活动显示为日历，改了立即生效并写回 `config.json` 的 `v2`（其他键原样保留）；「读取鼠标电量」下次连接生效。「屏幕刷新」选每 3–60 分钟最多刷新一次，立即发给板子（三色屏每次整屏刷新至少间隔 3 分钟，画面有变化才刷新，选项再小也不会更快）。「开机自启」勾上后，登录 Windows 时自动在托盘里启动（写当前用户的注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `EpaperSidescreenHost`，不要管理员权限；取消勾选就删掉），记的是当时 `gui.py` 的路径，仓库挪了位置要重新勾一次。
- **页面**：自动 / 仪表盘 / 天气 / 待办单选，只切换右列「屏幕预览」里显示的内容（启动时选中的是上次推送成功的页面）；「推送」（强调色按钮）把选中的页面发给板子、五个数据块下一轮全部重发，屏幕按刷新间隔自己更新；「立即推送」同样发送，再让屏幕马上整屏刷新（不受最短间隔限制）。成功后才把页面写进 config。立即推送成功后，状态行显示「屏幕刷新中，约 20 秒…」，期间这两个按钮暂时不可点；收到板子的刷新完成通知后显示「屏幕已刷新 时:分:秒」，最多等约 45 秒。状态行只在推送 / 刷新 / 出错时出现。没连板子时单选框照样能预览，两个推送按钮灰掉；连上后不会自动补发。
- **待办**：事项列表。输入框里按 Enter：选中了某一行就保存修改，没选就添加；双击一行进入编辑，Delete 删除，Esc 取消编辑；列表有几条就几行高（不单独滚动，左列整体滚），是空的时写「还没有待办」，到 60 条时提示「最多 60 条」。改了待办，右列「屏幕预览」会自动切到待办页（只预览，不推送）；推送用「页面」里选「待办」再点「推送」或「立即推送」。
- **离线天气页**：一句话「☐ 电脑停止推送 N 分钟后，屏幕自动显示天气」（N 为 1–240，填错会提示并恢复原值），下面输入城市名按 Enter 搜索，结果多于一个时用 ↑↓ 选、Enter 确定，选好后显示「已保存：城市」。

**右列**（上面是标签页，下面是日志）

- **屏幕预览**：默认（页面选「自动」/「仪表盘」）把最近推送的五条消息画成墨水屏上的样子（400×300，白 / 黑 / 红），数据一变就重画（攒 0.5 秒）。不用等板子约 23 秒的刷新就能看到效果。没连着板子时画的是板子离线时的样子（带离线图标）。选「待办」显示按现在的待办渲染的整页；选「天气」时上位机在后台线程里用 `v2.weather` 的经纬度向 Open-Meteo 取一份预报（地址、字段和固件 `wxb::requestUrl` 一样，响应缓存约 10 分钟，换城市重新取），喂给 `epaper_render --weather <地名>`，由固件自己的解析和绘图代码画出板子上的天气页（底部那行 OFFLINE · WEATHER 也照画）。没选城市时预览区写「没有设置城市」，取不到数据写「拉取天气失败」。「缩放」可选 1× / 2×，选择会写进 `config.json` 的 `preview_scale`。「立即刷新数据」马上重读 Token Monitor 文件、按当前显示设置重算数据块，只更新屏幕预览和数据明细，不发给板子（鼠标沿用最近一次推送的读数）；没连板子时也能用。高分屏（150% 缩放起）且屏幕放得下时，默认 2×。
- **数据明细**：最近一次推送的文字摘要（Token、鼠标、各家额度、模型排行、活动窗口）。
- **日志**：`epaper_host` / `mouse_rapoo` / `tm_source` 的日志，只保留最近 500 行；右键菜单有「复制」（有选中就复制选中，否则复制全部）和「清空」。
- **保存失败**：配置文件写不进去（文件被占用、JSON 坏了等）时，左列最上面用红字显示原因，改动只在本次运行里有效。
- **托盘**：点窗口的 × 只是收进托盘，连接和推送照常；第一次会弹一个「已收进托盘」的小窗口，勾「不再提示」后以后不再弹（记在 `config.json` 顶层的 `tray_note_off`）。左键单击托盘图标或右键「显示窗口」打开窗口，右键「退出」才真正退出（升级固件期间会先确认）。鼠标停在图标上显示连接状态。同一时间只能开一个：再打开一次会提示已经在运行。

窗口和命令行版都会占用串口（Wi-Fi 时板子也只服务一个连接），**不要同时运行**。托盘「退出」会先停止推送、再关闭鼠标读取和串口。

#### 编译预览渲染器（一次）

预览由 `epaper_render.exe` 画。这个程序用固件自己的绘图代码（`src/dashboard_v2.cpp`、`src/protocol.cpp`，天气页还有 `src/weather_parse.cpp`、`src/weather_page_b.cpp`）在电脑上编译而成，所以预览和真屏不会走样。它和 `test/preview/run.sh` 画出的图逐字节相同。没编译时，预览区会显示下面这条命令。

1. 装 MSYS2 的 g++，位置是 `C:\msys64\ucrt64\bin`。
2. 在 `firmware/` 下跑过一次 `pio run -e waveshare_esp32_42tri`，把库下载到 `.pio/libdeps/`。
3. 在 `firmware/` 目录运行：

```bash
bash test/preview/build.sh
```

- **输出**：`firmware/test/preview/build/epaper_render.exe`，已在 `.gitignore` 里。
- **静态链接**：运行时不需要 MSYS2 在 PATH 上。
- **查找顺序**：GUI 先在 `gui.py`（或打包后的 exe）旁边找 `epaper_render.exe`，再到上面这个位置找。
- **重新编译**：渲染器用到的固件源码（`src/*.h`、`dashboard_v2.cpp`、`protocol.cpp`、`weather_parse.cpp`、`weather_page_b.cpp`）比 exe 新时，预览下方会提示重新编译；旧版渲染器不认 `--weather` 时，天气预览区也会写「渲染器太旧」。改了绘图代码、或合并了新固件后，重跑一次 `build.sh`。

**手动调用**：协议行从 stdin 输入，PPM 从 stdout 输出。`--offline` 画离线图标，`--scale N` 放大。有消息会被固件拒收时，退出码是 3，原因写在 stderr。`--weather "<地名>"` 换成天气页：stdin 是 Open-Meteo 预报的 JSON 原文（`weather.forecast_url(lat, lon)` 给出的地址），解析失败退出码 4；Windows 下地名按 UTF-16 命令行取，中文不会乱码（板子字库里没有的字照样画不出，和真屏一样）。

```bash
epaper_render.exe < lines.txt > screen.ppm
epaper_render.exe --weather "Adelaide" < open-meteo.json > weather.ppm
```

预览只说明绘图代码画出什么，不是实屏照片：墨水屏的对比度、抖动观感、红色深浅仍以实物为准。

### 数据从哪来

| 消息 | 内容 | 来源 |
|---|---|---|
| `dash` | TOKENS 块：`main` 是展开的周期（完整数字带千分位 + 费用），`alt` 是另一个周期（缩写）；`time` 是数据更新时间 | 所有设备 `allTime` / `today` 相加。某台设备的「今天」已过期（没同步、跨天了），不计入今日。`time` 是各设备 `updatedAt` 里最新的，本机时间，不是当前时钟 |
| `quota` | Claude、Codex、Antigrav、DeepSeek 的剩余额度 | 每家取各设备里 `status=ok` 且 `updatedAt` 最新的一条。Claude / Codex 只留 5h 和 Wk 两条（不要 Fable）。Antigravity：Gemini 组的 5 小时和周窗口各一条；Claude/GPT 组写成文字 `C/G 72%/64%`；Argon 写成 `4 Argon 0%`；缺的写 `--`，剩余 ≤10% 的文字段加告警。重置时间已过但还没有新读数，显示 `--`，不会假设变回 100% |
| `models` | 前 5 个模型和占比，`scope` 是 `TODAY` 或 `TOTAL` | `primary=today` 时是各设备 `today.models` 之和（过期设备不计入），占比的分母是今日总量；`total` 时是 `allTime.models` 之和 |
| `heat` | 最近 `heat_weeks` 个整周的热图和趋势：一周从周日开始，`start` 是最早那周的周日，`end` 是今天，长度 = (周数−1)×7 + 今天是本周第几天（周日=1）；`rows` 是每列几格；`calendar` 打开时带 `view:"cal"`；`peak` 和 `active`（有用量的天数）按这个区间算 | 所有设备 `history.daily` 按日期相加。所有设备都开始记录之后没有记录的日子算 0，之前没有记录的日子是未知 |
| `mouse` | 鼠标电量 | `mouse_rapoo.py`。模块不存在或出错时发 `"st":"na"` |

数据新鲜度：设备 `updatedAt` 距今 15 分钟内正常，15 分钟到 2 小时显示 `STALE`，超过 2 小时或读不到显示 `--`。鼠标是 10 分钟 / 1 小时。

### 多久发一次

- 每 30 秒检查 `dash` 和 `mouse`；`quota`、`models`、`heat` 每 60 秒检查一次。
- 内容和上次发出去的一样就不重发。`dash` 里的时间是数据更新时间，Token Monitor 约每分钟更新一次，所以 `dash` 大约每分钟发一次。改了 `primary` / `heat_rows` 后，相关的几条马上重发。
- 超过 30 秒什么都没发，就发一条 v2 `ping` 保活。
- 每条发出后等板子回 `OK`（最多 2 秒）再发下一条；没等到就先停，没发的下一轮补。
- 重连后所有消息重新发一遍。

## 9. Wi-Fi 传输与配网（固件 1.3.0 起）

固件 1.3.0 起，板子可以连 Wi-Fi，电脑通过网络推送，不必一直插着 USB。协议和串口完全一样，约定见 `docs/dashboard-v2-protocol.md` 的「WiFi 传输」。

### 第一次：用 USB 给板子配 Wi-Fi

1. 用 USB 连上板子，打开 `gui.py`，方式选「USB 串口」，点「连接」。
2. 左列「板子」里的「Wi-Fi」一行会显示当前状态。点「配网…」（只有 USB 串口连着 v2 板子时才能点）。
3. 点「扫描」，等几秒，在列表里选你的 Wi-Fi。隐藏网络直接在「名称」框里填。
4. 填密码（可以勾「显示密码」），点「写入」。状态行会依次显示 连接中、已连接 和板子 IP。
5. 连上后，窗口会提示可以切到 Wi-Fi，并把板子 IP 填进地址框。断开 USB 连接，方式改成「Wi-Fi」，点「连接」。
6. 「清除 Wi-Fi」只清掉保存的 Wi-Fi。「恢复出厂」还会清掉保存的最后一屏，会先问你确认。

密码只保存在板子里。电脑上不写进配置，也不写进日志；关闭配网窗口时输入框会清空。

### 之后：用 Wi-Fi 推送

- 图形界面：方式选「Wi-Fi」。点「搜索」会在局域网里找板子，结果出现在地址下拉框里，格式是 `名称 (id) IP`。也可以直接填 IP 或主机名。上次用过的地址会记下来，下次启动自动连。
- 命令行：

```bat
python epaper_host.py --discover                  找板子，打印 名称 id IP:端口 固件版本 busy/free
python epaper_host.py --port tcp://192.168.1.50   用 Wi-Fi 推送（端口默认 7330）
```

  也可以把 `config.json` 里的 `port` 写成 `"tcp://192.168.1.50"`。
- 板子同一时间只服务一个 TCP 客户端，新连接会把旧的踢掉。所以图形界面和命令行不要同时连同一块板子。
- 断线（板子断电、路由器重启）后，程序按 `reconnect_interval` 自动重连，和串口一样。

### 用 Wi-Fi 升级固件（固件 1.3.1 起）

先用 USB 连一次上位机：图形界面连上 v2 板子后会自动向板子要配对码，存进 `config.json` 的 `ota_token`（不进日志）。之后：

- 图形界面：用 Wi-Fi 连上板子，点「升级固件…」，选 `firmware/.pio/build/waveshare_esp32_42tri/firmware.bin`，确认。期间状态行和进度条显示进度，屏幕不刷新，大约 1 分钟；完成后板子自动重启，重连后日志里会显示新的固件版本。没有配对码、或不是 Wi-Fi 连接时按钮是灰的。
- 命令行：

```bat
python epaper_host.py --port COM5 --pair                         用 USB 要配对码，存进配置（只打印板子 id）
python epaper_host.py --ota firmware.bin --port tcp://192.168.1.50   用 Wi-Fi 升级；不写 --port 时用配置里的 tcp:// port 或 wifi_addr
```

  退出码：0 已写入，板子在重启；1 参数 / 配置 / 文件不对（没发任何东西）；2 板子拒绝、传输中断或没有回应（日志里有原因）。
- 新固件起不来时，板子会自动回到旧固件。配对码不对时（例如板子恢复过出厂），用 USB 再连一次就会重新配对。
- 板子同一时间只服务一个客户端：命令行升级会把图形界面的连接踢掉。

### 搜不到板子时

- 搜索靠 UDP 广播。少数路由器会拦广播，或者不让 2.4 GHz 和 5 GHz 之间互通广播。这时手动填板子的 IP（路由器的设备列表里能看到，或者先用 USB 连一次，配网窗口会显示）。
- **板子只支持 2.4 GHz。** 电脑可以在同一路由器的 5 GHz 上，只要两边在同一个局域网。
- 路由器的访客网络、AP 隔离（客户端隔离）会让电脑访问不到板子。把板子和电脑接到同一个普通网络上。
- 防火墙要允许出站 UDP 7331 和 TCP 7330。
