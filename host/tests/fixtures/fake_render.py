"""假预览渲染器（test_gui 用）：命令行约定和 firmware/test/preview/render.cpp 的 epaper_render 一样——
协议行从 stdin 进，400×300 的 PPM 从 stdout 出，--offline 画离线图标（这里把整张图涂黑来区分）。

  fake_render.py --record <文件> [--mode ok|reject|fail|hang] [--offline]

--record：把收到的参数和 stdin 原样记下来（JSON），测试据此检查 GUI 喂了什么。
--mode：ok 正常；usage 打印 usage、退出码 1（旧版渲染器不认 --weather）；parse 退出码 4（天气数据解析失败）；reject 出图但退出码 3（有行被拒收）；fail 不出图、退出码 2；hang 一直睡（测超时）。
"""
import json
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    record = args[args.index("--record") + 1] if "--record" in args else None
    mode = args[args.index("--mode") + 1] if "--mode" in args else "ok"
    data = sys.stdin.buffer.read()
    if record:
        with open(record, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "stdin": data.decode("utf-8")}) + "\n")
    if mode == "hang":
        time.sleep(60)
    if mode == "usage":
        sys.stderr.write("usage: epaper_render [--offline] [--bw] [--scale N] < lines > screen.ppm\n")
        return 1
    if mode == "parse":
        sys.stderr.write("cannot parse the Open-Meteo response\n")
        return 4
    if mode == "fail":
        sys.stderr.write("cannot write the image\n")
        return 2
    px = b"\x00\x00\x00" if "--offline" in args else b"\xc8\x14\x14"
    sys.stdout.buffer.write(b"P6\n400 300\n255\n" + px * (400 * 300))
    if mode == "reject":
        sys.stderr.write("line 2: ERR bad_field p\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
