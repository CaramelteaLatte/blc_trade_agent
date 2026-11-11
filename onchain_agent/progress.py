"""CLI progress and an idle heartbeat; report stdout stays machine-separable."""

import sys
import json
import os
import re
import threading
import time


def safe_message(message):
    for name in ("OPENAI_API_KEY", "ALCHEMY_RPC_URL"):
        secret = os.environ.get(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
            if name == "ALCHEMY_RPC_URL" and "/v2/" in secret:
                message = message.replace(secret.split("/v2/", 1)[1], "[REDACTED]")
    message = re.sub(r"https://eth-mainnet\.g\.alchemy\.com/v2/[^\s\"<>]+", "[RPC endpoint redacted]", message)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", message)


def record_progress(message):
    """Side channel for the MCP process; its stdout is reserved for JSON-RPC."""
    path = os.environ.get("RESEARCH_PROGRESS_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"message": safe_message(message)}, ensure_ascii=False) + "\n")
        except OSError:
            pass


class ProgressFeed:
    """Tail public model summaries and deterministic collector progress."""
    def __init__(self, path, emit):
        self.path, self.emit = path, emit
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.follow, daemon=True)

    def follow(self):
        pending = ""
        with self.path.open(encoding="utf-8") as stream:
            while True:
                final_pass = self.stop.is_set()
                pending += stream.read()
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and isinstance(item.get("message"), str):
                            self.emit(safe_message(item["message"]))
                    except ValueError:
                        pass
                if final_pass:
                    return
                self.stop.wait(0.2)

    def __enter__(self):
        self.path.touch(mode=0o600)
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.worker.join(timeout=2)


class ConsoleProgress:
    def __init__(self):
        self.started = self.last = time.monotonic()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self.heartbeat, daemon=True)

    def emit(self, message):
        with self.lock:
            self.last = time.monotonic()
            print(f"[{self.last - self.started:.0f}s] {safe_message(message)}", file=sys.stderr, flush=True)

    def heartbeat(self):
        while not self.stop.wait(5):
            if time.monotonic() - self.last >= 15:
                self.emit("仍在运行，等待模型或数据工具返回…")

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.worker.join(timeout=1)
