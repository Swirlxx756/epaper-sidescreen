# 仪表盘 v2 协议（主机 → 固件）

适用屏幕：4.2 寸三色 400×300。布局：左栏 TOKENS / QUOTA（总量 / 额度），右栏 ACTIVITY 日历热图 + TREND 趋势 + MODELS 模型排行，右上角今天日期（MM-DD），左下角数据时间、鼠标电量和在线图标。

## 原则

- **沿用 v1 的帧**：一行一个 JSON 对象，`\n` 结尾，单行 ≤ 1024 字节（含 `\n`），115200 8N1，每条合法消息回 `OK`。v1 的 `stats` / `ping` / `full_refresh` 保留不变。
- **与传输无关**：同样的行以后可以走 WiFi（TCP）或 BLE，固件的解析不依赖 `Serial`。
- **主机算好、固件只画**：所有数字由主机格式化成字符串；新鲜度、告警由主机判断后用状态字段告诉固件。固件没有时钟，不自己算时间。
- **v2 消息带 `"v":2`**。固件收到任一 v2 消息后切到 v2 界面；之后再收到 v1 `stats` 就切回 v1 界面（便于回退）。
- **分块、各自完整**：每种消息是该区块的完整快照，后到的覆盖先到的；某块从没收到过就显示占位（`--` / `NO DATA`）。不做多包组帧。
- 主机每 30 秒发一轮 `dash` + `mouse`；`quota`、`models` 每 60 秒或内容变化时发；`heat` 每 10 分钟或内容变化时发。固件按「显示内容是否变化」决定刷新，三色屏仍是上次刷新结束后至少 180 秒、24 小时保底。
- 缺失值一律 `null` 或省略，固件显示 `--`；`0` 是真实的 0。

## 状态字段 `st`

所有区块共用：`"ok"` 正常 / `"stale"` 数据过期但还显示旧值（固件在区块标题右侧画反色小标签 `STALE`）/ `"na"` 取不到（固件显示 `--`，不画旧值）。省略 = `"ok"`。

主机判断规则：Token Monitor 数据的观测时间（设备 `updatedAt`）距今 ≤ 15 分钟为 ok，15 分钟–2 小时为 stale，超过 2 小时或读不到为 na。鼠标：最近一条报告 ≤ 10 分钟为 ok，10–60 分钟 stale，否则 na。

## 消息

### `dash`：TOKENS 块 + 左下角时间

```json
{"v":2,"type":"dash","st":"ok","time":"10-28 20:20",
 "main":{"l":"TODAY","tok":"1,430,736,150","cost":"$363.69"},
 "alt":{"l":"TOTAL","tok":"7.43B"}}
```

- `main`：展开显示的周期（主机配置 `primary`：`today` / `total`），`tok` 是**完整数字带千分位**，固件按宽度自动选字号；`cost` 只显示这一个。
- `alt`：另一个周期，缩写成小字（`TOTAL 7.43B` 或 `TODAY 1.43B`）。
- 今日 = 两台电脑 today 之和；累计 = allTime 之和。
- `time`：数据更新时间（Token Monitor 设备最新的 `updatedAt`，主机本地时间 `MM-DD HH:MM`）：`HH:MM` 画在屏幕左下角，`MM-DD` 画在右栏标题行右端（热图和日历样式都一样）。
- `every`（可选，秒，0–3600）：屏幕刷新间隔。板子显示过第一个数据画面之后，两次刷新至少隔这么久（比三色屏的 180 秒短就按 180 秒）；`full_refresh` 不受它限制。主机配置 `refresh_min` 大于 3 时才带。`st:"na"` 时也认；它不算画面内容，单独改它不会触发刷新。

### `quota`：额度（最多 4 家）

```json
{"v":2,"type":"quota","st":"ok","p":[
  {"n":"Claude","plan":"Max 5x","i":[{"l":"5h","left":83,"rst":"4h29m"},{"l":"Wk","left":60,"rst":"5d7h"}]},
  {"n":"Codex","plan":"Plus","i":[{"l":"5h","left":53,"rst":"1h55m"},{"l":"Wk","left":93,"rst":"6d20h"}]},
  {"n":"Antigrav","plan":"Pro","i":[{"l":"5h","left":100,"rst":"4h59m"},{"l":"Wk","left":97,"rst":"1d20h"}],
   "txt":[{"s":"C/G 72%/64%"},{"s":"4 Argon 0%","w":1}]},
  {"n":"DeepSeek","plan":"PAYG","bal":"¥38.66"}]}
```

- `i`：条形图项，每家最多 2 项（5h、Wk）。`l` ≤ 4 个字符；`left` 0–100（剩余百分比），`null` = 未知；`rst` 重置倒计时，可省略。
- `txt`：不画条、只写一行字的项，各段之间留空；`w:1` 表示该段告警（剩余 ≤ 10），固件把这一段画成反色。Antigravity 的 Claude/GPT 组写 `C/G 5h%/周%`，Argon 写 `4 Argon 周%`；Claude 带标签的周窗口写 `Fable 94%`；余额类（DeepSeek）写今日花费 `Today ¥7.42`（与 `bal` 同时出现）。
- `bal`：余额类，写在该家标题行右侧。
- 告警：条形项 `left` ≤ 10 且该家 `st` 为 ok 时，额度条外框和剩余部分用强调色。

### `models`：模型排行（最多 5 项）

```json
{"v":2,"type":"models","st":"ok","scope":"TODAY","m":[["claude-opus-5-5","750M",52],["claude-sonnet-5-5","403M",28]]}
```

每项 `[名称, token 字符串, 占比整数%]`，按 token 降序，占比的分母是该范围的总量。`scope` 跟着 `primary` 走：`TODAY` = 两台电脑今天的模型之和，`TOTAL` = allTime；固件标题只写 `MODELS`，不标范围。名称太长由固件截断加 `~`。

### `heat`：活动热图 + 趋势（同源同区间）

```json
{"v":2,"type":"heat","st":"ok","start":"2026-07-20","end":"2026-10-28","rows":7,
 "d":"-------…0012345…","t":"------…AZz09…","peak":"1.41B","active":46}
```

- 区间按**整周**取：最近 `heat_weeks` 周（主机配置，默认 10），一周从**周日**开始，`start` 是最早那周的**周日**，`end` 是今天；长度 = (heat_weeks−1)×7 + 今天是本周第几天（周日=1…周六=7）。`d` / `t` 的第 i 个字符是 `start` 之后第 i 天。固件按字符串长度画（不写死）。
- 热图排成**日历**：7 行（周日…周六，从上到下，不标星期），每周一列，最旧一周在最左边，今天在最后一列；固件只显示最后 10 周 = 7 行 × 10 列。`rows` 字段保留但固件不再使用。
- 每格 18×18 方格，**无描边、无间隔**。
- `d`：每天的密度档 `'0'`–`'9'`、`'A'`–`'C'`（10–12 档），或 `'-'` = 未知（Token Monitor 还没开始记录的日子）。档位按固定刻度：r = 该日 token ÷ 1B（1,000,000,000，主机常量 `HEAT_FULL`）；`0` = 0 token，`k`（1–12）= (k−1)/12<r≤k/12，即 ceil(r×12)，1B 及以上都是 12。每档 1/12 ≈ 83.3M，和区间里的峰值无关（`peak` 只给 TREND 用）。
- `t`：每天的趋势点，Base64 字母表（`A–Z a–z 0–9 + /`）里的一个字符，表示 0–63 的高度（相对 `peak`），`'-'` = 未知（断线）。
- `peak`：区间内已知日最大 token（已格式化）；`active`：区间里 token>0 的天数（固件不再显示）。
- `view`（可选）：`"cal"` = 日历样式：标题 `CALENDAR`，当月整月挂历（周日开头、周日标红，4–6 行随月份），格子底色用同一套热图色阶，上个月的格子只有底色不写字；没有这个字段或其他值 = 热图。`st:"na"` 时也认（标题跟着变）。
- 固件画法：
  - 热图：**4×4 规则点阵**，按屏幕坐标对齐（相邻同档格连成一片）。档 0 空白；档 1–8 白底加红点，每档多 2/16，档 8 纯红；档 9–12 红底加黑点，每档多 2/16，档 12 红黑各半。未知日（还没开始记录）和档 0 一样空白；今天在日历右侧、对着它那一行画朝左的小三角。
  - 趋势：与热图同样的天，分段时间轴（最近 15 天占右半，更早的占左半），用圆滑曲线连起每天的真实值（不做平均，峰值那天碰到顶），**只画曲线、不填充、不画坐标轴**；未知日按 0。保留标题右侧 `peak` 和下方 3 个日期。
  - 文字、描边永远实心黑；黑白屏上红退化为黑灰度。

### `mouse`：鼠标电量

```json
{"v":2,"type":"mouse","st":"ok","pct":68,"chg":false,"at":"19:39"}
```

- `pct` 0–100 或 `null`；`chg` 充电中（不确定就省略）；`at` 最近一次读到的时间。
- `st`：`ok` / `stale` / `na`，另外 `"off"` = 接收器在但鼠标没上报（休眠 / 关机）。
- 告警：`pct` ≤ 15 且 `st` 为 ok 时，电池填充画成强调色。

## 固件回复

v2 消息与 v1 一样回 `OK` / `ERR <原因>`。`READY` 行改为 `READY epaper-sidescreen v2 <panel> <宽>x<高>`，主机据此知道固件支持 v2；收到 `v1` 的 READY 或握手 ping 后发现不支持（对 v2 消息回 `ERR bad_version`）时，主机退回 v1 `stats`。

## WiFi 传输（固件 1.3.0 起）

消息内容和回复规则与串口完全相同，只是多一种传输方式。USB 串口和 WiFi 可以同时在线，回复发回给发这一行的那条连接。

### 连接

- 板子连上 WiFi 后，在 **TCP 7330** 端口等上位机连接。同一时间只服务一个 TCP 客户端；新客户端连上时，旧的被断开。例外（固件 1.7.0 起）：旧客户端正在升级固件（`ota_begin` 之后）或传待办图片（`img_begin` 之后、`img_end` 之前）时，新连接被直接关闭，旧连接不受影响；传输结束（或断线、超时）后新连接才能接入。
- 固件 1.7.0 起每一行回复和 DIAG 都用一次 `write()` 发出（不会被拆成两个 TCP 段），连接上开了 TCP keepalive（空闲 30 秒后每 10 秒探测，3 次无应答即视为断线）；写失败时板子关闭这个客户端。DIAG 行是可选的：socket 发送缓冲快满时（上位机不读数据）直接丢弃，不阻塞主循环。
- 客户端连上后，板子先发一行 `READY epaper-sidescreen v2 <panel> <宽>x<高>`，之后一行 JSON 进、一行 `OK` / `ERR <原因>` 出，和串口一样。
- 上位机的端口写法：`tcp://<IP 或主机名>[:端口]`（端口默认 7330）。串口仍写 `COM3`。
- 任何传输上收到的有效消息都算"在线"（`OFFLINE_TIMEOUT_MS` 不变）。
- 固件 1.3.1 起，`DIAG ...` 诊断行除了 USB 串口，也发给当前 TCP 客户端（刷新完成 `DIAG epd refresh done ...`、驱动故障 `DIAG epd fault ...` / `DIAG display stopped ...` 等）。屏幕已故障时，新连上的客户端在 `READY` 之后马上收到这两行。故障后板子照常回 `OK`，只是不再刷新。固件 1.7.0 起，故障时先尽力让屏幕进入省电状态（不等 BUSY 直接发断电 / 深度睡眠命令，`DIAG epd fault sleep sent ...`），然后约 3 分钟后自动重启（`DIAG recovery: auto reboot in 180 s (fault reboot N of 3)`，重启前 `DIAG recovery: rebooting now`）；连续 3 次故障重启后不再自动重启（`DIAG recovery: off, ...`，需要断电重插），重启次数记在 RTC 内存里，一次刷新成功就清零，断电也清零。重启后的那次开机会打印 `DIAG recovery: boot after fault reboot N of 3`，新连上的 TCP 客户端也会收到这一行。升级或传图期间不会自动重启。

### 发现（UDP 7331）

- 上位机向 `255.255.255.255:7331`（以及各网卡的子网广播地址）发一个 UDP 包，内容是 `{"v":2,"type":"discover"}`。
- 板子单播回给发送方一行 JSON：

```json
{"v":2,"type":"here","name":"epaper-sidescreen","id":"a1b2c3","ip":"192.168.1.50","port":7330,"fw":"1.3.0","busy":false}
```

- `id`：MAC 后 6 位十六进制，用来区分多块屏；`busy`：当前是否已有 TCP 客户端。
- 广播被路由器拦住时（少数 Mesh 路由器会拦 2.4G 和 5G 之间的广播），上位机改用手动填的 IP 或上次连过的地址。

### 配网命令（只接受 USB 串口）

从 TCP 发来这些命令时回 `ERR usb_only`。

| 命令 | 作用 | 回复 |
|---|---|---|
| `{"v":2,"type":"wifi_scan"}` | 扫描附近 WiFi（约 2–4 秒） | 先一行 `WIFI {"type":"scan","nets":[{"ssid":"Home-2.4G","rssi":-52,"sec":true}, ...]}`（按信号强弱排序，最多 20 个，同名去重），再 `OK` |
| `{"v":2,"type":"wifi_set","ssid":"...","pass":"..."}` | 保存到 NVS 并马上去连 | `OK`（只表示已保存；连接结果看后面的 `WIFI status` 行） |
| `{"v":2,"type":"wifi_status"}` | 查询 | 先一行 `WIFI status`，再 `OK` |
| `{"v":2,"type":"wifi_clear"}` | 清掉保存的 WiFi 并断开 | `OK` |
| `{"v":2,"type":"reset_settings"}` | 清掉 WiFi、保存的最后一屏、配对码和天气设置（恢复出厂） | `OK` |

- `ssid` 1–32 字节，`pass` 0–63 字节（0 = 开放网络）；超出回 `ERR bad_field ssid` / `ERR bad_field pass`。
- 板子的 WiFi 状态变化时（连上、断开、失败），会主动在 **USB 串口**上发一行：

```
WIFI {"type":"status","st":"connected","ssid":"Home-2.4G","ip":"192.168.1.50","rssi":-52,"port":7330,"id":"a1b2c3"}
```

  `st`：`off`（没配过）/ `connecting` / `connected` / `failed`（密码错或找不到网络，之后每 30 秒重试）。开机在 `READY` 之后也发一次。
- 密码不会出现在任何回复和日志里。
- 恢复出厂也可以在板子上做：开机后长按 BOOT 键（GPIO0）5 秒，效果同 `reset_settings`，串口打印 `DIAG settings cleared`。

### 断电保存（NVS）

- 命名空间 `wifi`：`ssid`、`pass`。
- 命名空间 `wx`：离线天气设置（`on`、`place`、`lat`、`lon`、`after`）。
- 命名空间 `page`：手动切页设置（`p`，一个字节）。待办图片不在 NVS，见「待办页图片」。
- 命名空间 `snap`：最近一次收到的 5 条 v2 消息原文（`dash` / `quota` / `models` / `heat` / `mouse` 各一条）。
  - 内容有变化、且距上次写入至少 10 分钟时才写，减少闪存磨损；断电最多丢最近 10 分钟的变化。
  - 开机时如果有保存的消息，先把它们重放进界面，开机画面直接画这份仪表盘（右上角显示离线），不再先画 waiting for host。之后按正常的 180 秒规则刷新。

## 离线天气（固件 1.5.0 起）

上位机离线一段时间后，板子自己联网取 Open-Meteo 天气，把屏幕换成天气页；上位机一发来有效消息就换回仪表盘。需要 WiFi 版固件（`waveshare_esp32_42tri`）。

### 配置命令（USB 串口和 TCP 都接受，需要 `"v":2`）

| 命令 | 作用 | 回复 |
|---|---|---|
| `{"v":2,"type":"wx_cfg","on":true,"place":"上海","lat":31.23,"lon":121.47,"after_min":10}` | 开启并保存地点 | `OK` |
| `{"v":2,"type":"wx_cfg","on":false}` | 关闭（保留已存的地点） | `OK` |
| `{"v":2,"type":"wx_status"}` | 查询 | 先一行 `WX {...}`，再 `OK` |

- `on`：必填，布尔值。`false` 时其余字段被忽略。
- `place` / `lat` / `lon`：要给就三个一起给（缺哪个回 `ERR missing_field <名>`）。`place` 1–23 字节合法 UTF-8（不能有控制字符、`"`、`\`；1.7.0 起非法 UTF-8 序列回 `ERR bad_field place`），`lat` -90..90，`lon` -180..180；不合格回 `ERR bad_field place|lat|lon`。经纬度按 0.0001 度取整保存。都不给 = 沿用板子上已存的地点（还没有地点时，开启后天气页显示 `NO CITY SET`）。
- `after_min`：上位机离线多少分钟后显示天气，1..240，不给 = 沿用已存的值（默认 10）。实际等待时间不会短于 `OFFLINE_TIMEOUT_MS`（120 秒）。回 `ERR bad_field after_min`。
- 保存到 NVS 命名空间 `wx`；内容没变就不写。上位机每次（重新）连上都可以重发同一条，板子不会因此重取或重画。`reset_settings` / 长按 BOOT 恢复出厂会一并清掉。写 NVS 失败回 `ERR nvs_write`。这两条命令不算"上位机在线"，不影响离线计时。
- `wx_status` 的 `WX` 行：

```
WX {"type":"status","on":true,"place":"上海","lat":31.23,"lon":121.47,"after_min":10,"mode":"dash","last":"11-02 01:15","err":null}
```

  `mode`：`dash` 或 `weather`（屏幕上现在是哪个）；`last`：最近一次取天气成功时的当地时间 `MM-DD HH:MM`，没取过为 `null`；`err`：最近一次失败的原因（成功后清空为 `null`）：`no_wifi` / `sntp`（对不上时间）/ `low_mem` / `tls`（连不上或证书不对）/ `http_<状态码>` / `too_big` / `empty` / `parse`；没有地点时 `lat`、`lon` 为 `null`，`place` 为 `""`。
- 板子每次取完天气会在 USB 串口和 TCP 客户端上发一行 `DIAG wx fetch ok <MM-DD HH:MM> stack_free=<字节>` 或 `DIAG wx fetch fail <原因> stack_free=<字节>`（`stack_free` 是取数据任务栈的历史最小剩余，1.7.0 起；老固件没有这个后缀）。取数据任务超过 90 秒还没结束，板子放弃它：`DIAG wx fetch watchdog: ...`，按失败处理（原因 `timeout`，5 分钟后重试）；旧任务真正退出之前不会开新的取数据。升级固件或传待办图片期间不开始取数据。

### 什么时候显示天气

- 条件（全部满足）：天气已开启；上位机已有 `after_min` 分钟（且不少于 120 秒）没发有效消息（开机后从未收到时从开机算起）；WiFi 已连上；有地点。进入天气页还要手里有数据可画（第一次取到之前继续显示仪表盘 + OFFLINE）。
- 没有地点：开启并过了离线时间后直接显示 `NO CITY SET` 页（不需要 WiFi）。天气关闭：行为和以前一样（仪表盘 + OFFLINE）。
- 进入后每 30 分钟重取一次；失败时保留上一份好数据，页面标 `STALE`（WiFi 断了标 `NO WIFI`），5 分钟后重试。
- 收到任何有效的 v1 / v2 数据或心跳消息就换回仪表盘。
- 天气页只在内容变化（取到新数据、状态变化）时重画，走和仪表盘同一个刷新调度：两次刷新至少隔 180 秒。

### 取数据

- `GET https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&timezone=auto&forecast_days=4&forecast_hours=12&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day&hourly=temperature_2m,precipitation_probability&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset`，在单独的任务里做，不阻塞串口 / TCP / 刷屏。无需密钥。
- 页面上的时间全部来自返回的当地时间（`current.time`），板子不需要知道时区；SNTP（`pool.ntp.org` 等）只用来让 TLS 证书校验有时间可用。
- TLS 校验服务器证书链，信任根只有 ISRG Root X1（Let's Encrypt，2035-06-04 到期）。Open-Meteo 以后换了签发机构，取数据会报 `tls`，要换 `src/weather_net.cpp` 里的根证书。
- 提醒文字（英文，一行内，红色）由板子按今天的数据推出，优先级从高到低：`THUNDERSTORM`（天气码 95–99）、`SNOW`（71–77、85、86）、`HEAVY RAIN <降水概率>%`（概率 >= 80）或 `HEAVY RAIN`（天气码 65、67、82）、`HIGH TEMP <最高>°C`（>= 35）、`LOW TEMP <最低>°C`（<= -5）；都不满足为空。

## 手动切页、待办页、立即刷新（固件 1.6.0 起）

这一节的命令都需要 `"v":2`，USB 串口和 TCP 都接受，不算"上位机在线"（不影响离线计时，也不会让板子离开天气页）。旧固件对它们回 `ERR unknown_type`。

### `page`：手动切页

```json
{"v":2,"type":"page","p":"auto"}
```

- `p`：`auto` / `dash` / `weather` / `todo`。缺 → `ERR missing_field p`；其他值（含大小写不同）→ `ERR bad_field p`。成功回 `OK`。
- `auto` = 原来的行为：上位机在线显示仪表盘，离线超过 `after_min` 且天气已配置则显示天气页。
- `dash` / `weather` / `todo` = 不管上位机在不在线，一直显示该页，直到再发 `auto`。强制页下收到的 `dash` / `quota` / … 照常更新板子里的数据，只是不显示。强制 `dash` / `todo` 时离线天气规则不起作用，也不取天气。
- 强制 `weather`：板子没有天气数据或数据超过 30 分钟时马上取（需要 WiFi 和地点，即使 `wx_cfg` 的 `on` 是 `false`；上次取失败则等 5 分钟重试间隔）。取不到时画天气页已有的 `STALE` / `NO WIFI` / `NO CITY SET`。第一次切到天气页且还没有数据、又有取数据任务在跑时，板子最多等 30 秒（取完为止）再画，避免先画一屏空占位。等满 30 秒还没取到时先画占位页；1.7.0 起，之后这次取数据成功时板子会像 `refresh_now` 那样绕过一次 180 秒间隔，马上把真实数据画出来（只有"占位页刚画出、数据随后到达"这一种情况，后续的 30 分钟刷新不绕过）。
- 强制 `todo`：没有收到过待办图片时画空状态页：`TODO` 标题 + `NO TODOS` / `Add them in the PC app`。
- 保存在 NVS 命名空间 `page`（键 `p`，一个字节），重启后保持；值没变不重复写。`reset_settings` / 长按 BOOT 恢复出厂会清回 `auto`（同时清掉已存的待办图片）。写 NVS 失败回 `ERR nvs_write`。
- 切页本身只改板子内部状态；屏幕什么时候重画由刷新调度决定（见 `refresh_now`）。主机的手动操作应在 `page` 之后紧接着发 `refresh_now`；板子不会因为 `page` 自动绕过 180 秒。

### `page_status`：查询

```json
{"v":2,"type":"page_status"}
```

回一行 `PAGE {...}`，再回 `OK`：

```
PAGE {"type":"status","p":"auto","shown":"dash","todo_crc":null}
```

- `p`：当前设置（`auto` / `dash` / `weather` / `todo`）。
- `shown`：板子内部**将要**显示的页（`dash` / `weather` / `todo`）：`auto` 下就是离线规则的结果。它是内部状态，不是屏幕上已经画出来的页：刷新还没轮到（180 秒规则）时屏幕可能还是上一页。
- `todo_crc`：板子里存着的待办图片的压缩数据 crc32（无符号整数，和 `img_begin` 的 `crc` 同一个值）；没有图片为 `null`。主机可以用它判断要不要重推。
- `wx_status` 不变（它的 `mode` 仍只有 `dash` / `weather`，反映离线天气规则；强制页以 `page_status` 为准）。

### `refresh_now`：立即刷新

```json
{"v":2,"type":"refresh_now"}
```

回 `OK`。下一次整屏刷新尽快做：**只这一次**绕过 `EPD_TRI_MIN_REFRESH_MS`（180 秒）的最短间隔；内容没变也会重画。正在刷新时，等这次刷完立刻再来一次（板子在刷屏的 23 秒左右仍然响应串口 / TCP）。驱动已故障时不刷新，请求保留。自动更新仍然遵守 180 秒规则。

三色屏厂商建议两次刷新至少隔 180 秒；频繁手动强刷是用户自己的选择（增加残影和寿命损耗的风险），固件不做额外限制。

### 待办页图片（`img_begin` / `img_chunk` / `img_end`）

板子的中文字库只有大约 574 个字，待办文字由上位机画成 400×300 的两个 1 位平面发给板子，板子只负责解压和显示，不需要新字库。

**平面**：黑平面 + 红平面，各 15000 字节（400/8 × 300），行优先，每行 50 字节，每字节最高位是最左的像素，`1` = 有墨。两个平面都置位的像素显示红色（红覆盖黑）。

**压缩（PackBits RLE，每个平面单独压）**：

- 压缩流里的控制字节 `c`：
  - `0..127`：后面 `c+1` 个字节原样复制（字面量，1..128 字节）；
  - `129..255`：把后面 **1** 个字节重复 `257-c` 次（2..128 次）；
  - `128`：非法，固件拒绝（`ERR bad_rle`）。
- 载荷 = `RLE(黑平面)` 紧接着 `RLE(红平面)`，没有头、没有分隔。固件按顺序解码：先解出 15000 字节作黑平面，再从接着的位置解出 15000 字节作红平面；一个 run 或字面量不能越过平面末尾（越界 = 非法）；两个平面解完后载荷必须正好用完，多一个字节也非法。
- **规范编码器**（主机应这样编码，使输出字节固定，便于和 `docs/todo-image-fixtures.json` 逐字节比对；固件的解码器接受任何合法流）：
  从 `i = 0` 开始，每步：令 `r` = 从 `i` 起连续相同字节的个数，最多取 128。
  - `r >= 3`：写 `257-r` 和该字节，`i += r`；
  - 否则写字面量：从 `j = i` 起向后扩展，直到到达 128 字节、数据结束，或者 `j > i` 且从 `j` 起有 **3 个以上**连续相同字节（同样不限长度地数）为止；写 `j-i-1` 和这些字节，`i = j`。
  长度恰为 2 的相同字节只算字面量（不单独成 run）；超过 128 的 run 先写 128 再对剩余部分重新判断。
- 压缩后载荷最大 `12288` 字节（`IMG_MAX_COMPRESSED`）。文字页通常几 KB；噪点类图像压不下来（最坏每 128 字节多 1 个字节），超限的在 `img_begin` 回 `ERR too_big`。

**传输**（每行 ≤ 1024 字节，每条回 `OK` 或 `ERR <原因>`；三条都可以走 USB 或 TCP）：

```json
{"v":2,"type":"img_begin","id":"todo","size":2079,"crc":2132524130}
{"v":2,"type":"img_chunk","id":"todo","off":0,"d":"<base64>"}
{"v":2,"type":"img_end","id":"todo"}
```

- `id` 目前只有 `"todo"`（缺 → `ERR missing_field id`，其他 → `ERR bad_field id`）。
- `size`：压缩载荷总字节数，1..12288 的整数（0 / 非整数 → `ERR bad_field size`，超过 → `ERR too_big`）。`crc`：整个压缩载荷的 crc32（zlib / PNG 的那一种：多项式 `0xEDB88320`，初值和结尾异或 `0xFFFFFFFF`；测试向量 `"123456789"` → `0xCBF43926` = 3421780262），0..4294967295 的整数。
- `img_begin` 开始一次传输，**同时丢弃**还没完成的上一次传输；板子为它分配 `size` 字节暂存（堆不够回 `ERR low_mem`）。已存的好图片此时不动。
- `img_chunk`：`off` = 这一块在载荷里的字节偏移，`d` = 该块字节的标准 base64（RFC 4648，字母表 `A-Za-z0-9+/`，**必须带 `=` 补齐**，不能有空白、换行）。解码后 1..720 字节；一整行不能超过 1024 字节，所以主机每块取 **≤ 600 字节**（编码后 ≤ 800 字符）。回复：
  - 块必须按顺序（`off` 等于已收到的字节数）→ `OK`；
  - 已收到范围内的重发（`off + 长度 ≤ 已收到`，比如上次的 `OK` 丢了）→ `OK`，不改动已收数据；
  - 有空洞或和已收部分部分重叠 → `ERR bad_offset`；超出 `size`、或长度为 0 → `ERR bad_range`；没有进行中的传输 → `ERR no_transfer`；`d` 不是合法 base64 / 空 / 太长 → `ERR bad_field d`。
  - 出错后传输仍然保留，主机可以重发正确的块，也可以重新 `img_begin`。
- `img_end`：板子检查是否收满（`ERR incomplete`）、crc（`ERR bad_crc`）、整个载荷能否按上面的规则解码（`ERR bad_rle`）；都过了才写入保存，回 `OK`。无论成败，这次传输到此结束。没有进行中的传输 → `ERR no_transfer`。保存失败 → `ERR flash_write`。
- **失败或中断的传输绝不替换上一份好图片**：任何 `ERR`、超时、断线、重新 `img_begin` 之后，已保存的图片和屏幕都不变。例外：保存这一步本身（`flash_write`）失败时旧图片已被擦掉，板子之后按"没有图片"处理。
- **超时**：一次未完成的传输，20 秒没有收到新的（被接受的）`img_chunk` 就被丢弃，板子发一行 `DIAG img timeout`（不发 `ERR`，免得和命令回复错位）；之后再来的块回 `ERR no_transfer`。
- 推送成功后，主机一般接着发 `page`（`todo`）和 `refresh_now`。板子不会因为 `img_end` 自动绕过 180 秒；待办页显示着时，新图片（crc 不同）让页面内容变化，和其他变化一样走 180 秒规则。`img_end` 收到的图片 crc 和已存的一样时，页面内容没变，不会自己重画（要重画请 `refresh_now`）。1.7.0 起，大小和 crc 都和已存图片相同的 `img_end` 直接回 `OK`，不再擦写闪存（上位机重连后重新推同一张图是常态）。

**保存**：压缩载荷连同 16 字节头（魔数、长度、crc32）存在默认分区表里本来没人用的 `spiffs` 数据分区（0x290000，1.4 MB，直接用 `esp_partition` 读写，不挂文件系统），先擦 16 KB 再写数据最后写头，所以写到一半断电只会变成"没有图片"。NVS 分区只有 20 KB，已经放了仪表盘快照，放不下 12 KB 的块。开机读头并校验 crc，通过才算有图片。显示时板子把压缩数据读进临时堆缓冲（`size` 字节）解码到屏幕缓冲，再释放；没有额外的 30 KB 图像缓冲。没有 `spiffs` 分区（自定义分区表去掉了它）时无处保存，`img_end` 回 `ERR flash_write`。

**共享测试数据**：[`docs/todo-image-fixtures.json`](todo-image-fixtures.json)，主机编码器和固件解码器测试都读它：`crc32_vectors`（crc32 向量）、`rle_cases`（原始字节 ↔ 规范编码，两个方向都要对）、`rle_decode_only`（合法但非规范的流，只测解码）、`rle_invalid`（必须拒绝）、`frames`（整页：`black` / `red` 用 `"十六进制字节*次数"` 的序列描述 15000 字节平面，附 `payload_hex`、`payload_size`、`payload_crc32` 和按 `chunk_size` 切好的完整传输行 `lines`）。主机编码器要对 `rle_cases` 和每个 `frames` 项产生**逐字节相同**的载荷；固件测试（`firmware/test/native/test_img.cpp`）解码并重组这些行。

## WiFi 升级固件（固件 1.3.1 起）

只在 TCP 连接上做；USB 串口照旧用 esptool 刷。

### 配对码

- `{"v":2,"type":"pair"}`（**只接受 USB 串口**，TCP 上回 `ERR usb_only`）：回一行 `WIFI {"type":"pair","token":"<16 位十六进制>","id":"a1b2c3"}`，再回 `OK`。
- 配对码第一次被要的时候由板子随机生成，存在 NVS 命名空间 `ota`（键 `token`），之后不变；`reset_settings` / 长按 BOOT 恢复出厂会清掉它，下次再要时换一个新的。
- 上位机把它存在 `host/config.json` 顶层的 `ota_token`。配对码不进日志。

### 升级流程

1. 上位机发 `{"v":2,"type":"ota_begin","size":<字节数>,"md5":"<32 位小写十六进制>","token":"<配对码>"}`。
   - 解析错误：`ERR missing_field size|md5|token`、`ERR bad_field size|md5|token`（`size` 是 1–16777215 的整数，`md5` 32 位十六进制，`token` 1–32 字节）。
   - 从 USB 串口发来：`ERR tcp_only`。
   - 配对码不对或板子还没生成过：`ERR ota_token`；比 OTA 分区大：`ERR ota_too_big`；闪存准备失败：`ERR ota_begin`。
2. 板子回 `OK` 之后，这条 TCP 连接上紧接着的 `size` 个字节都是固件原文（`.pio/build/<env>/firmware.bin`），不再按行解析。上位机在这期间不能发别的消息。
3. 收满 `size` 字节后，板子校验 MD5 并写好启动分区，回一行：
   - `OK`：已写入；板子接着发 `DIAG ota done, rebooting`，约 0.5 秒后断开并重启。
   - `ERR ota_md5`（校验不对）/ `ERR ota_write`（写闪存失败）/ `ERR ota_end`（其他）：板子不切换固件，回到按行收消息。
4. 中途断开、新客户端连上、或者 10 秒收不到数据：放弃本次升级（超时时先回 `ERR ota_timeout`），旧固件不受影响。
5. 升级期间不刷新屏幕；数据照常在升级完成后的下一轮推送。

### 自动回滚

- 通过 WiFi 刷入的新固件第一次启动后处于"待确认"状态。有 TCP 客户端连上，或者收到第一条有效消息，它才被确认。
- 确认之前如果板子重启（新固件起不来、死机重启、按复位、断电），引导程序自动回到上一个固件。
- 1.7.0 起，确认调用本身失败（读状态或写入出错）时不再当作已确认，下一条有效消息或下一个 TCP 客户端会再试一次（`DIAG ota confirm failed, will retry` 只打一次）。
- 用 USB 刷的固件不需要确认。

### 版本

- 每个新 TCP 客户端在 `READY` 之后都会收到 `DIAG fw version=<版本> test_pattern=<0|1>`，上位机可以据此确认升级后的版本。
