"""Small JSON-RPC client for the local Codex app-server (stdio)."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time


class CodexClient:
    def __init__(self, command="codex"):
        self.process = subprocess.Popen(
            [command, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.inbox = queue.Queue()
        self.events = []
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "infohub", "title": "Info Hub", "version": "1.0"}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.inbox.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout=60):
        try:
            message = self.inbox.get(timeout=max(.01, timeout))
        except queue.Empty:
            raise TimeoutError("Codex response timed out") from None
        if message is None:
            raise RuntimeError("Codex app-server exited")
        return message

    def call(self, method, params, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(deadline - time.monotonic())
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message", "Codex request failed")))
                return message.get("result", {})
            self.events.append(message)
        raise TimeoutError(f"Codex {method} timed out")

    def next_event(self, timeout=60):
        return self.events.pop(0) if self.events else self.receive(timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
