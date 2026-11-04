"""Breeze TTS 2 协议自检桩（不是语音合成服务）。

用途：在没有可用的 Linux + CUDA 机器时，验证本应用的 Breeze 适配器是否
严格按官方 `breeze_infer.api` 的 HTTP 契约收发数据。

它只做三件事：
1. 暴露 `GET /v1/models`，供适配器做存活探测；
2. 校验 `POST /v1/audio/speech` 的 multipart 字段（text / ref_audio / ref_text / instruction / cfg_scale / seed）；
3. 返回一段 24 kHz 单声道 s16le 的测试音（时长按文本长度估算），并把收到的字段写入 JSONL 日志。

输出为测试音而非人声，不要把它当成 Breeze 的实际合成效果。

    python tools/breeze_stub.py --port 7860 --log tmp/breeze_stub.jsonl
"""

from __future__ import annotations

import argparse
import cgi
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAMPLE_RATE = 24000
LOG_PATH: Path | None = None


def _tone_pcm(seconds: float, freq: float = 220.0) -> bytes:
    total = int(SAMPLE_RATE * seconds)
    frames = bytearray()
    for i in range(total):
        t = i / SAMPLE_RATE
        # 轻微颤音 + 起落包络，避免听起来是纯直流蜂鸣
        env = min(1.0, t / 0.05, max(0.0, (seconds - t) / 0.08))
        value = 0.28 * env * math.sin(2 * math.pi * (freq + 6 * math.sin(2 * math.pi * 3 * t)) * t)
        frames += int(value * 32767).to_bytes(2, "little", signed=True)
    return bytes(frames)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[stub] {fmt % args}", flush=True)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models"):
            self._json(200, {"data": [{"id": "breeze-tts-2", "object": "model"}]})
        elif self.path.startswith("/health"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/v1/audio/speech"):
            self._json(404, {"detail": "not found"})
            return

        ctype = self.headers.get("content-type", "")
        if "multipart/form-data" not in ctype:
            self._json(415, {"detail": "需要 multipart/form-data"})
            return

        length = int(self.headers.get("content-length") or 0)
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": ctype,
            "CONTENT_LENGTH": str(length),
        }
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)

        text = (form.getvalue("text") or "").strip()
        if not text:
            self._json(400, {"detail": "text 不能为空"})
            return
        if "ref_audio" not in form:
            self._json(400, {"detail": "缺少 ref_audio"})
            return
        item = form["ref_audio"]
        ref_bytes = item.file.read() if getattr(item, "file", None) else b""
        if not ref_bytes:
            self._json(400, {"detail": "ref_audio 为空"})
            return

        record = {
            "ts": time.time(),
            "text": text,
            "ref_text": form.getvalue("ref_text") or "",
            "instruction": form.getvalue("instruction") or "",
            "cfg_scale": form.getvalue("cfg_scale") or "",
            "seed": form.getvalue("seed") or "",
            "ref_audio_filename": getattr(item, "filename", ""),
            "ref_audio_bytes": len(ref_bytes),
        }
        if LOG_PATH:
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        seconds = max(0.8, min(8.0, len(text) * 0.16))
        pcm = _tone_pcm(seconds)
        self.send_response(200)
        self.send_header("content-type", "application/octet-stream")
        self.send_header("x-sample-rate", str(SAMPLE_RATE))
        self.send_header("content-length", str(len(pcm)))
        self.end_headers()
        self.wfile.write(pcm)


def main() -> None:
    global LOG_PATH
    parser = argparse.ArgumentParser(description="Breeze TTS 2 协议自检桩")
    # 默认 7860：与应用侧 breeze-tts-2 的 default_endpoint 对齐，
    # 这样不带参数启动即可让界面从「未连接」变为「已连接」。
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default="tmp/breeze_stub.jsonl")
    args = parser.parse_args()

    LOG_PATH = Path(args.log)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[stub] listening on http://{args.host}:{args.port}  log={LOG_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
