"""在局域网里找墨水屏副屏（固件 1.3.0 起的 WiFi 传输）。

向 UDP 7331 广播 {"v":2,"type":"discover"}，板子单播回一行 here JSON。
协议见 docs/dashboard-v2-protocol.md「WiFi 传输」。只用标准库；装了 psutil 就顺便向每块网卡
的子网广播地址也发一份（Windows 上 255.255.255.255 只会从其中一块网卡出去）。

  py -3.14 host/epaper_host.py --discover
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time

log = logging.getLogger("epaper_host.discovery")

DISCOVERY_PORT = 7331
DEFAULT_TCP_PORT = 7330
PROBE = b'{"v":2,"type":"discover"}'
MAX_BOARDS = 32
MAX_REPLY = 2048


def broadcast_targets(port: int = DISCOVERY_PORT) -> list[tuple[str, int]]:
    """255.255.255.255 + 各 IPv4 网卡的定向广播地址（跳过回环和 /32）。psutil 不可用就只有前者。"""
    targets = [("255.255.255.255", port)]
    try:
        import psutil
        ifaces = psutil.net_if_addrs()
    except Exception as e:  # noqa: BLE001 - 可选依赖，枚举失败不影响搜索
        log.debug("psutil unavailable, broadcast only to 255.255.255.255: %s", e)
        return targets
    for addrs in ifaces.values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.address or not a.netmask:
                continue
            try:
                ip = ipaddress.IPv4Address(a.address)
                if ip.is_loopback or ip.is_unspecified:
                    continue
                net = ipaddress.IPv4Network((a.address, a.netmask), strict=False)
            except ValueError:
                continue
            if net.prefixlen >= 31:
                continue
            target = (str(net.broadcast_address), port)
            if target not in targets:
                targets.append(target)
    return targets


def parse_reply(data: bytes, source_ip: str) -> dict | None:
    """一条 here 回复 -> 规范化的 dict；不是 here / 坏 JSON / 字段不对 -> None。"""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "here":
        return None
    ip = msg.get("ip")
    if not isinstance(ip, str):
        ip = source_ip
    else:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            ip = source_ip
    port = msg.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        port = DEFAULT_TCP_PORT
    ident = msg.get("id")
    name = msg.get("name")
    fw = msg.get("fw")
    return {
        "name": name if isinstance(name, str) and name else "epaper-sidescreen",
        "id": ident if isinstance(ident, str) else "",
        "ip": ip,
        "port": port,
        "fw": fw if isinstance(fw, str) else "",
        "busy": msg.get("busy") is True,
    }


def _no_connreset(sock: socket.socket) -> None:
    """Windows：对没人听的地址 sendto 后，recvfrom 会抛 WSAECONNRESET；关掉这个行为。"""
    ioctl = getattr(sock, "ioctl", None)
    if ioctl is None:
        return
    try:
        ioctl(getattr(socket, "SIO_UDP_CONNRESET"), False)
    except (OSError, AttributeError, ValueError):
        pass


def discover(timeout: float = 1.5, port: int = DISCOVERY_PORT,
             targets: list[tuple[str, int]] | None = None) -> list[dict]:
    """广播并收集 here 回复，最多等 `timeout` 秒。按 id（没有就按 ip）去重，按名字 / id 排序。
    `targets` 给出时只发给这些 (地址, 端口)，测试用。"""
    if targets is None:
        targets = broadcast_targets(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        _no_connreset(sock)
        sock.bind(("", 0))
        start = time.monotonic()
        deadline = start + timeout
        resend_at = start + timeout * 0.4     # UDP 广播会丢包，中途再发一轮；重复回复由去重处理
        sent_twice = False

        def send_all() -> None:
            for t in targets:
                try:
                    sock.sendto(PROBE, t)
                except OSError as e:
                    log.debug("discover send to %s failed: %s", t, e)

        send_all()
        while len(found) < MAX_BOARDS:
            now = time.monotonic()
            if now >= deadline:
                break
            if not sent_twice and now >= resend_at:
                sent_twice = True
                send_all()
            wait = deadline - now
            if not sent_twice:
                wait = min(wait, max(0.01, resend_at - now))
            sock.settimeout(wait)
            try:
                data, addr = sock.recvfrom(MAX_REPLY)
            except socket.timeout:
                continue
            except ConnectionResetError:    # 上一次 sendto 撞上了 ICMP 不可达，忽略
                continue
            except OSError as e:
                log.debug("discover receive failed: %s", e)
                break
            board = parse_reply(data, addr[0])
            if board is None:
                continue
            found.setdefault(board["id"] or board["ip"], board)
    finally:
        sock.close()
    return sorted(found.values(), key=lambda b: (b["name"], b["id"], b["ip"]))
