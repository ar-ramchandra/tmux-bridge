#!/usr/bin/env python3
"""
Terminal server — runs on each machine with tmux sessions.
Connects outward to gateway via WebSocket (reverse connection model).
Single multiplexed WebSocket with channel-based terminal I/O.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pty
import struct
import fcntl
import termios
import subprocess
import signal
import ssl
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosedError

from protocol import (
    MSG_CONNECT, MSG_DISCONNECT, MSG_RESIZE, MSG_RPC_REQUEST,
    MSG_PING, MSG_PONG, MSG_REGISTER, MSG_SESSION_UPDATE, MSG_CHANNEL_CLOSED,
    pack_data, unpack_data, make_register, make_session_update,
    make_rpc_response, make_pong,
)
from auth import create_auth_proof

# ── Config ──

CONFIG_DIR = Path(os.environ.get("TMUX_BRIDGE_AGENT_DIR", Path.home() / ".tmux-bridge")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE = CONFIG_DIR / "server.key"
CERT_FILE = CONFIG_DIR / "server.crt"
CA_FILE = CONFIG_DIR / "ca.crt"

RECONNECT_DELAY = 5  # seconds between reconnect attempts
RECONNECT_MAX = 60
SESSION_POLL_INTERVAL = 10  # seconds between session list updates


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def websocket_tls_options(config):
    tls = config.get('tls')
    if tls is None:
        return {}
    if not config['gateway'].startswith(('https://', 'wss://')):
        raise ValueError('Configured mTLS requires an HTTPS/WSS gateway; no plaintext fallback')
    def path(key):
        value = Path(tls[key]).expanduser()
        return str(value if value.is_absolute() else CONFIG_DIR/value)
    context = ssl.create_default_context(cafile=path('ca'))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(path('certificate'), path('private_key'))
    return {'ssl': context, 'server_hostname': tls['server_hostname']}


# ── Metrics collection (10 min rolling window) ──

METRICS_INTERVAL = 5
METRICS_MAXLEN = 120

_metrics = {
    "cpu": deque(maxlen=METRICS_MAXLEN),
    "memory": deque(maxlen=METRICS_MAXLEN),
    "net_rx": deque(maxlen=METRICS_MAXLEN),
    "net_tx": deque(maxlen=METRICS_MAXLEN),
    "disk_read": deque(maxlen=METRICS_MAXLEN),
    "disk_write": deque(maxlen=METRICS_MAXLEN),
    "gpu_util": deque(maxlen=METRICS_MAXLEN),
    "gpu_mem": deque(maxlen=METRICS_MAXLEN),
    "gpu_temp": deque(maxlen=METRICS_MAXLEN),
}
_has_gpu = False
_session_metrics = {}


def _sample_metrics(previous=None):
    """Collect a complete snapshot in one worker, without touching live buffers."""
    import psutil
    from sysinfo import HAS_NVIDIA

    now = time.monotonic()
    net = psutil.net_io_counters()
    disk = psutil.disk_io_counters()
    state = {"time": now, "net": net, "disk": disk, "processes": {}}
    samples = {"cpu": psutil.cpu_percent(), "memory": psutil.virtual_memory().percent}
    if previous is None:
        return state, None, {}, HAS_NVIDIA
    dt = max(now - previous["time"], 0.001)
    for key, current, old, field in (
        ("net_rx", net, previous["net"], "bytes_recv"),
        ("net_tx", net, previous["net"], "bytes_sent"),
        ("disk_read", disk, previous["disk"], "read_bytes"),
        ("disk_write", disk, previous["disk"], "write_bytes"),
    ):
        samples[key] = max(0, (getattr(current, field, 0) - getattr(old, field, 0)) / dt)
    if HAS_NVIDIA:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                text=True, stderr=subprocess.DEVNULL, timeout=2,
            ).strip().split(", ")
            samples.update(zip(("gpu_util", "gpu_mem", "gpu_temp"), map(float, out)))
        except Exception:
            pass

    # One tmux call and one process-table scan for all sessions. Calling
    # Process.children(recursive=True) separately for every pane scans the
    # entire host repeatedly and stalls busy hosts with many sessions.
    roots = {}
    try:
        panes = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        for line in panes.splitlines():
            name, pid = line.rsplit("\t", 1)
            roots.setdefault(name, set()).add(int(pid))
    except Exception:
        # Keep the last per-session samples if tmux is temporarily unavailable.
        return state, samples, None, HAS_NVIDIA

    children, processes = {}, {}
    for process in psutil.process_iter(["pid", "ppid", "create_time", "cpu_times", "memory_info"]):
        try:
            info = process.info
            pid = info["pid"]
            children.setdefault(info["ppid"], []).append(pid)
            cpu = info["cpu_times"]
            memory = info["memory_info"]
            identity = (pid, info["create_time"])
            cpu_time = cpu.user + cpu.system if cpu else 0.0
            state["processes"][identity] = cpu_time
            old_cpu = previous["processes"].get(identity, cpu_time)
            processes[pid] = (max(0.0, cpu_time - old_cpu) * 100.0 / dt,
                              memory.rss if memory else 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    sessions = {}
    for name, pane_pids in roots.items():
        pending, visited = list(pane_pids), set()
        cpu_sum, mem_sum = 0.0, 0
        while pending:
            pid = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            pending.extend(children.get(pid, ()))
            cpu, mem = processes.get(pid, (0.0, 0))
            cpu_sum += cpu
            mem_sum += mem
        sessions[name] = (round(cpu_sum, 1), round(mem_sum / (1024**2), 1))
    return state, samples, sessions, HAS_NVIDIA


async def _collect_metrics():
    global _has_gpu
    loop = asyncio.get_running_loop()
    # A dedicated worker keeps slow GPU/process probes away from both the
    # event loop and the thread pool used by terminal PTY reads.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-metrics") as worker:
        previous = None
        while True:
            try:
                previous, samples, sessions, _has_gpu = await loop.run_in_executor(
                    worker, _sample_metrics, previous,
                )
                # Publish only on the event loop so RPC readers never race
                # a worker mutating dictionaries or rolling sample buffers.
                if samples is not None:
                    for key, value in samples.items():
                        _metrics[key].append(value)
                if sessions is not None:
                    for name, (cpu, mem) in sessions.items():
                        history = _session_metrics.setdefault(name, {
                            "cpu": deque(maxlen=METRICS_MAXLEN),
                            "mem": deque(maxlen=METRICS_MAXLEN),
                        })
                        history["cpu"].append(cpu)
                        history["mem"].append(mem)
                    for gone in set(_session_metrics) - set(sessions):
                        del _session_metrics[gone]
            except Exception as exc:
                print(f"[SERVER] Metrics sample failed: {exc}", flush=True)
            await asyncio.sleep(METRICS_INTERVAL)


# ── TmuxSession — manages PTY attachment to one tmux session ──

class TmuxSession:
    def __init__(self, name: str, channel: int, send_callback, cols: int = 200, rows: int = 50, exit_callback=None):
        self.name = name
        self.channel = channel
        self.send_callback = send_callback  # async fn(bytes) to send packed data
        self.exit_callback = exit_callback
        self.cols = cols
        self.rows = rows
        self.proc = None
        self.master_fd = None
        self.own_tty: str | None = None
        self.buffer = deque(maxlen=100000)
        self.running = False
        self._read_task = None
        self._input_buffer = bytearray()
        self._input_writer_fd = None

    async def start(self):
        if self.running:
            return
        subprocess.run(["tmux", "set-option", "-t", self.name, "status", "off"], capture_output=True)
        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        self.own_tty = os.ttyname(slave_fd)
        self.proc = subprocess.Popen(
            ["tmux", "attach-session", "-t", self.name],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            preexec_fn=os.setsid,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.running = True
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        loop = asyncio.get_event_loop()
        while self.running and self.proc and self.proc.poll() is None:
            try:
                data = await loop.run_in_executor(None, self._blocking_read)
                if data:
                    self.buffer.extend(data)
                    # Send as binary frame with channel ID
                    frame = pack_data(self.channel, data)
                    await self.send_callback(frame)
            except Exception:
                await asyncio.sleep(0.05)
        unexpected = self.running
        self.running = False
        if unexpected and self.exit_callback:
            await self.exit_callback(self.channel)

    def _blocking_read(self) -> bytes:
        import select
        r, _, _ = select.select([self.master_fd], [], [], 0.1)
        if r:
            try:
                return os.read(self.master_fd, 65536)
            except (OSError, BlockingIOError):
                return b""
        return b""

    def resize(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGWINCH)
            except OSError:
                pass

    def write_input(self, data: bytes) -> bool:
        if self.master_fd is None or len(self._input_buffer) + len(data) > 2**22:
            return False
        self._input_buffer.extend(data)
        return self._flush_input()

    def _clear_input_writer(self):
        if self._input_writer_fd is not None:
            asyncio.get_running_loop().remove_writer(self._input_writer_fd)
            self._input_writer_fd = None

    def _flush_input(self):
        if self.master_fd is None:
            return False
        try:
            if self._input_buffer:
                written = os.write(self.master_fd, self._input_buffer)
                del self._input_buffer[:written]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self._clear_input_writer()
            self._input_buffer.clear()
            if self.exit_callback and self.running:
                self.running = False
                asyncio.create_task(self.exit_callback(self.channel))
            return False
        if self._input_buffer:
            if self._input_writer_fd is None:
                asyncio.get_running_loop().add_writer(self.master_fd, self._flush_input)
                self._input_writer_fd = self.master_fd
        else:
            self._clear_input_writer()
        return True

    def get_replay(self) -> bytes:
        return bytes(self.buffer)

    def get_tmux_clients(self) -> list[dict]:
        try:
            r = subprocess.run(
                ["tmux", "list-clients", "-t", self.name, "-F",
                 "#{client_name}\t#{client_width}x#{client_height}"],
                capture_output=True, text=True, timeout=2,
            )
            clients = []
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                tty = parts[0] if parts else ""
                size = parts[1] if len(parts) > 1 else ""
                # Skip our own PTY attachments
                if self.own_tty and tty == self.own_tty:
                    continue
                clients.append({
                    "tty": tty, "size": size,
                    "label": f"terminal ({tty.split('/')[-1]})",
                })
            return clients
        except Exception:
            return []

    async def stop(self):
        self.running = False
        self._clear_input_writer()
        self._input_buffer.clear()
        subprocess.run(["tmux", "set-option", "-t", self.name, "status", "on"], capture_output=True)
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait()
            except Exception:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.proc = None

    @property
    def alive(self) -> bool:
        return self.running and self.proc is not None and self.proc.poll() is None


# ── RPC handlers (reused from old FastAPI endpoints) ──

def _rpc_list_sessions(manager) -> dict:
    return {"status": 200, "body": manager.list_sessions()}


def _rpc_create_session(name: str, body: dict) -> dict:
    r = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
    if r.returncode == 0:
        return {"status": 200, "body": {"ok": True, "created": False}}
    cmd = body.get("command")
    if cmd:
        r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50", cmd], capture_output=True)
    else:
        r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50"], capture_output=True)
    if r.returncode == 0:
        return {"status": 200, "body": {"ok": True, "created": True}}
    return {"status": 500, "body": {"ok": False, "error": r.stderr.decode().strip()}}


def _rpc_kill_session(name: str, manager) -> dict:
    if name in manager.channels:
        sess = manager.channels[name]
        asyncio.create_task(sess.stop())
        del manager.channels[name]
    r = subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    if r.returncode == 0:
        return {"status": 200, "body": {"ok": True}}
    return {"status": 404, "body": {"ok": False, "error": "session not found"}}


def _rpc_specs() -> dict:
    from sysinfo import get_specs
    return {"status": 200, "body": {**get_specs(), "bridge_version": "2026-09-23-usability"}}


def _rpc_scroll(body: dict) -> dict:
    """Scroll the touched pane, choosing application mouse input or tmux history."""
    name = body.get("session")
    try:
        lines = max(-24, min(24, int(body.get("lines", 0))))
        col, row = max(1, int(body.get("col", 1))), max(1, int(body.get("row", 1)))
    except (ValueError, TypeError, OverflowError):
        return {"status": 400, "body": {"ok": False, "error": "invalid scroll"}}
    if not isinstance(name, str) or not name or not lines:
        return {"status": 400, "body": {"ok": False, "error": "session and scroll amount required"}}
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + name + ":", "-F",
             "#{pane_id}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t"
             "#{pane_mode}\t#{mouse_any_flag}\t#{mouse_sgr_flag}\t#{mouse_utf8_flag}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            return {"status": 404, "body": {"ok": False, "error": "session not found"}}
        pane = None
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 9:
                continue
            left, top, width, height = map(int, fields[1:5])
            if left < col <= left + width and top < row <= top + height:
                pane = fields
                col, row = col - left, row - top
                break
        if pane is None:
            return {"status": 400, "body": {"ok": False, "error": "no pane at touch position"}}
        pane_id, mode, mouse, sgr, utf8 = pane[0], pane[5], pane[6], pane[7], pane[8]
        if mode and mode != "copy-mode":
            return {"status": 409, "body": {"ok": False, "error": "close the pane menu before scrolling"}}
        if mouse == "1" and not mode:
            button = 64 if lines < 0 else 65
            if sgr == "1":
                report = ("\x1b[<%d;%d;%dM" % (button, col, row)).encode()
            elif utf8 == "1":
                report = ("\x1b[M" + chr(button + 32) + chr(min(col, 2015) + 32) + chr(min(row, 2015) + 32)).encode()
            else:
                report = b"\x1b[M" + bytes((button + 32, min(col, 223) + 32, min(row, 223) + 32))
            # -H sends bytes to this pane; no browser WheelEvent or mouse binding required.
            command = ["tmux", "send-keys", "-t", pane_id, "-H"] + ["%02x" % byte for byte in report * abs(lines)]
            copy_mode = False
        else:
            if not mode:
                if lines > 0:
                    return {"status": 200, "body": {"ok": True, "copy_mode": False, "pane": pane_id}}
                enter = subprocess.run(["tmux", "copy-mode", "-e", "-t", pane_id], capture_output=True, timeout=5)
                if enter.returncode:
                    return {"status": 500, "body": {"ok": False, "error": "could not open history"}}
            command = ["tmux", "send-keys", "-t", pane_id, "-X", "-N", str(abs(lines)),
                       "scroll-up" if lines < 0 else "scroll-down"]
            copy_mode = True
        sent = subprocess.run(command, capture_output=True, timeout=5)
        if sent.returncode:
            return {"status": 500, "body": {"ok": False, "error": "scroll failed"}}
        return {"status": 200, "body": {"ok": True, "copy_mode": copy_mode, "pane": pane_id}}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"status": 503, "body": {"ok": False, "error": "scroll unavailable"}}


def _rpc_history(body: dict, return_live: bool = False) -> dict:
    """Read pane history without taking input control or changing copy mode."""
    name = body.get("session", "")
    if not isinstance(name, str) or not name:
        return {"status": 400, "body": {"ok": False, "error": "session required"}}
    try:
        # Resolve an exact session first, then use the pane ID for all commands.
        pane = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + name + ":",
             "-F", "#{pane_active}\t#{pane_id}\t#{pane_mode}"], capture_output=True, text=True, timeout=5,
        )
        if pane.returncode:
            return {"status": 404, "body": {"ok": False, "error": "session not found"}}
        requested_pane = body.get("pane") if return_live else None
        active = next((line for line in pane.stdout.splitlines()
                       if (line.split("\t")[1] == requested_pane if requested_pane else line.startswith("1\t"))), "")
        if not active:
            return {"status": 404, "body": {"ok": False, "error": "active pane not found"}}
        pane_id, _, mode = active[2:].partition("\t")
        if return_live:
            if mode == "copy-mode":
                result = subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "-X", "cancel"],
                    capture_output=True, timeout=5,
                )
                if result.returncode:
                    return {"status": 500, "body": {"ok": False, "error": "could not leave history"}}
            return {"status": 200, "body": {"ok": True}}
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", "-5000", "-t", pane_id],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            return {"status": 500, "body": {"ok": False, "error": "could not read history"}}
        return {"status": 200, "body": {"ok": True, "text": result.stdout, "pane": pane_id}}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": 503, "body": {"ok": False, "error": "history unavailable"}}


def _rpc_files(path: str) -> dict:
    target = os.path.expanduser(path)
    if not os.path.isdir(target):
        return {"status": 400, "body": {"ok": False, "error": "not a directory"}}
    entries = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith('.'):
                continue
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else 0,
            })
    except PermissionError:
        return {"status": 403, "body": {"ok": False, "error": "permission denied"}}
    return {"status": 200, "body": {"ok": True, "path": target, "entries": entries}}


def _rpc_upload(body: dict) -> dict:
    filename = body.get("filename", "image.png")
    data_b64 = body.get("data", "")
    if not data_b64:
        return {"status": 400, "body": {"ok": False, "error": "no data"}}
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
    if not safe_name:
        safe_name = "upload.png"
    ts = int(time.time())
    safe_name = f"{ts}-{safe_name}"
    upload_dir = "/tmp/claude-uploads"
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, safe_name)
    with open(filepath, "wb") as f:
        f.write(base64.b64decode(data_b64))
    return {"status": 200, "body": {"ok": True, "path": filepath, "filename": safe_name}}


def _rpc_metrics() -> dict:
    def fmt_bytes(b):
        if b < 1024: return f"{b:.0f} B/s"
        if b < 1024**2: return f"{b/1024:.1f} KB/s"
        return f"{b/1024**2:.1f} MB/s"
    result = {
        "cpu": list(_metrics["cpu"]),
        "memory": list(_metrics["memory"]),
        "net_rx": list(_metrics["net_rx"]),
        "net_tx": list(_metrics["net_tx"]),
        "disk_read": list(_metrics["disk_read"]),
        "disk_write": list(_metrics["disk_write"]),
        "interval": METRICS_INTERVAL,
        "has_gpu": _has_gpu,
        "current": {
            "cpu": list(_metrics["cpu"])[-1] if _metrics["cpu"] else 0,
            "memory": list(_metrics["memory"])[-1] if _metrics["memory"] else 0,
            "net_rx": fmt_bytes(list(_metrics["net_rx"])[-1]) if _metrics["net_rx"] else "0 B/s",
            "net_tx": fmt_bytes(list(_metrics["net_tx"])[-1]) if _metrics["net_tx"] else "0 B/s",
            "disk_read": fmt_bytes(list(_metrics["disk_read"])[-1]) if _metrics["disk_read"] else "0 B/s",
            "disk_write": fmt_bytes(list(_metrics["disk_write"])[-1]) if _metrics["disk_write"] else "0 B/s",
        },
    }
    if _has_gpu:
        result["gpu_util"] = list(_metrics["gpu_util"])
        result["gpu_mem"] = list(_metrics["gpu_mem"])
        result["gpu_temp"] = list(_metrics["gpu_temp"])
        result["current"]["gpu_util"] = list(_metrics["gpu_util"])[-1] if _metrics["gpu_util"] else 0
        result["current"]["gpu_mem"] = list(_metrics["gpu_mem"])[-1] if _metrics["gpu_mem"] else 0
        result["current"]["gpu_temp"] = list(_metrics["gpu_temp"])[-1] if _metrics["gpu_temp"] else 0
    result["sessions"] = {}
    for sname, sd in _session_metrics.items():
        result["sessions"][sname] = {
            "cpu": list(sd["cpu"]),
            "mem": list(sd["mem"]),
            "current_cpu": round(list(sd["cpu"])[-1], 1) if sd["cpu"] else 0,
            "current_mem": round(list(sd["mem"])[-1], 1) if sd["mem"] else 0,
        }
    return {"status": 200, "body": result}


# ── SessionManager — tracks active channel sessions ──

class SessionManager:
    def __init__(self, on_channel_exit=None):
        self.channels: dict[int, TmuxSession] = {}  # channel_id -> TmuxSession
        self._channel_by_name: dict[str, int] = {}   # session_name -> channel_id
        self._reaper_task = None
        self.on_channel_exit = on_channel_exit

    async def start(self):
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def open_channel(self, channel: int, session_name: str, send_callback,
                           cols: int = 120, rows: int = 40) -> TmuxSession | None:
        """Open a terminal channel for a tmux session."""
        r = subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True)
        if r.returncode != 0:
            return None

        # Reuse existing session if channel already open for this name
        if session_name in self._channel_by_name:
            old_ch = self._channel_by_name[session_name]
            if old_ch in self.channels and self.channels[old_ch].alive:
                old_sess = self.channels.pop(old_ch)
                old_sess.channel = channel
                old_sess.send_callback = send_callback
                self.channels[channel] = old_sess
                self._channel_by_name[session_name] = channel
                return old_sess
            await self.close_channel(old_ch)

        sess = TmuxSession(session_name, channel, send_callback, cols, rows, self.channel_exited)
        await sess.start()
        self.channels[channel] = sess
        self._channel_by_name[session_name] = channel
        return sess

    async def close_channel(self, channel: int):
        sess = self.channels.pop(channel, None)
        if sess:
            if self._channel_by_name.get(sess.name) == channel:
                self._channel_by_name.pop(sess.name, None)
            await sess.stop()

    async def channel_exited(self, channel: int):
        if channel not in self.channels:
            return
        await self.close_channel(channel)
        if self.on_channel_exit:
            await self.on_channel_exit(channel)

    def list_sessions(self) -> list[dict]:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}\t#{session_created}\t#{session_windows}\t#{session_attached}"],
            capture_output=True, text=True,
        )
        sessions = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            name = parts[0]
            ch = self._channel_by_name.get(name)
            connected = ch is not None and ch in self.channels and self.channels[ch].alive
            sess = self.channels.get(ch) if ch else None
            tmux_clients = sess.get_tmux_clients() if sess and connected else []
            sessions.append({
                "name": name,
                "created": int(parts[1]) if len(parts) > 1 else 0,
                "windows": int(parts[2]) if len(parts) > 2 else 1,
                "attached": int(parts[3]) if len(parts) > 3 else 0,
                "bridged": connected,
                "tmux_clients": tmux_clients,
            })
        return sessions

    async def stop_all(self):
        for ch, sess in list(self.channels.items()):
            await sess.stop()
        self.channels.clear()
        self._channel_by_name.clear()

    async def _reaper_loop(self):
        while True:
            await asyncio.sleep(30)
            for ch, sess in list(self.channels.items()):
                if not sess.alive:
                    await self.channel_exited(ch)


# ── Main connection loop ──

class ServerAgent:
    """Manages the outbound WebSocket connection to the gateway."""

    def __init__(self, config: dict):
        self.config = config
        self.gateway_url = config["gateway"]
        self.name = config["name"]
        self.manager = SessionManager(self._channel_exited)
        self.ws = None
        self._running = True

    async def run(self):
        """Main entry point — starts metrics, session manager, and connects."""
        asyncio.create_task(_collect_metrics())
        await self.manager.start()

        delay = RECONNECT_DELAY
        while self._running:
            self._registered = False
            try:
                await self._connect()
            except ConnectionClosedError as e:
                if e.code == 1012:
                    # Replaced by another connection from the same server —
                    # likely a duplicate process. Exit instead of fighting.
                    print(f"[SERVER] Replaced by another connection (code 1012). Exiting.", flush=True)
                    self._running = False
                    return
                print(f"[SERVER] Connection closed: {e}", flush=True)
            except Exception as e:
                print(f"[SERVER] Connection error: {e}", flush=True)
            if self._registered:
                # A working connection usually ends by raising, not returning.
                # Do not carry old failure backoff into the next interruption.
                delay = RECONNECT_DELAY
            if self._running:
                print(f"[SERVER] Reconnecting in {delay}s...", flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)

    async def _connect(self):
        """Connect to gateway, authenticate, and enter message loop."""
        ws_base = self.gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_base}/server-ws"

        print(f"[SERVER] Connecting to {url}...", flush=True)
        async with websockets.connect(url, max_size=2**22, ping_interval=30, ping_timeout=60,
                                      **websocket_tls_options(self.config)) as ws:
            self.ws = ws

            # Send auth proof
            key_pem = KEY_FILE.read_bytes()
            cert_pem = CERT_FILE.read_bytes()
            auth_msg = create_auth_proof(key_pem, cert_pem)
            await ws.send(json.dumps(auth_msg))

            # Wait for auth response
            resp = json.loads(await ws.recv())
            if resp.get("type") == "error":
                print(f"[SERVER] Auth rejected: {resp.get('msg')}", flush=True)
                return
            if resp.get("type") != "auth_ok":
                print(f"[SERVER] Unexpected auth response: {resp}", flush=True)
                return

            print(f"[SERVER] Authenticated as '{self.name}'", flush=True)

            # Register with current session list
            sessions = self.manager.list_sessions()
            await ws.send(make_register(self.name, sessions))
            self._registered = True

            # Start session update poller
            update_task = asyncio.create_task(self._session_update_loop(ws))

            try:
                await self._message_loop(ws)
            finally:
                update_task.cancel()
                await self.manager.stop_all()
                self.ws = None

    async def _message_loop(self, ws):
        """Process incoming messages from gateway."""
        async for message in ws:
            if isinstance(message, bytes):
                # Binary frame: terminal input from gateway
                channel_id, data = unpack_data(message)
                sess = self.manager.channels.get(channel_id)
                if sess:
                    if not sess.write_input(data):
                        await self.manager.channel_exited(channel_id)
            else:
                # JSON control message
                msg = json.loads(message)
                msg_type = msg.get("type")

                if msg_type == MSG_CONNECT:
                    await self._handle_connect(ws, msg)
                elif msg_type == MSG_DISCONNECT:
                    await self._handle_disconnect(msg)
                elif msg_type == MSG_RESIZE:
                    self._handle_resize(msg)
                elif msg_type == MSG_RPC_REQUEST:
                    await self._handle_rpc(ws, msg)
                elif msg_type == MSG_PING:
                    await ws.send(make_pong())

    async def _handle_connect(self, ws, msg: dict):
        channel = msg["channel"]
        session = msg["session"]
        cols = msg.get("cols", 120)
        rows = msg.get("rows", 40)

        async def send_cb(data: bytes):
            try:
                await ws.send(data)
            except Exception:
                pass

        sess = await self.manager.open_channel(channel, session, send_cb, cols, rows)
        if sess:
            # Send replay buffer
            replay = sess.get_replay()
            if replay:
                await ws.send(pack_data(channel, replay))
            print(f"[SERVER] Channel {channel} opened for session '{session}'", flush=True)
        else:
            # Session doesn't exist
            await ws.send(json.dumps({"type": MSG_CHANNEL_CLOSED, "channel": channel,
                                      "reason": "Session unavailable. Waiting for tmux recovery."}))

    async def _channel_exited(self, channel):
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": MSG_CHANNEL_CLOSED, "channel": channel,
                                              "reason": "Terminal attachment ended. Reconnecting…"}))
            except Exception:
                pass

    async def _handle_disconnect(self, msg: dict):
        channel = msg["channel"]
        await self.manager.close_channel(channel)
        print(f"[SERVER] Channel {channel} closed", flush=True)

    def _handle_resize(self, msg: dict):
        channel = msg["channel"]
        sess = self.manager.channels.get(channel)
        if sess:
            sess.resize(msg["cols"], msg["rows"])

    async def _handle_rpc(self, ws, msg: dict):
        """Handle RPC request from gateway — reuses existing handler logic."""
        req_id = msg["id"]
        method = msg.get("method", "GET").upper()
        path = msg.get("path", "")
        body = msg.get("body", {})

        # Route to handler
        if path == "/api/sessions" and method == "GET":
            result = _rpc_list_sessions(self.manager)
        elif path == "/api/history" and method == "GET":
            result = await asyncio.get_running_loop().run_in_executor(None, _rpc_history, body or {})
        elif path == "/api/scroll" and method == "POST":
            result = await asyncio.get_running_loop().run_in_executor(None, _rpc_scroll, body or {})
        elif path == "/api/history/live" and method == "POST":
            result = await asyncio.get_running_loop().run_in_executor(None, _rpc_history, body or {}, True)
        elif path.startswith("/api/sessions/") and method == "POST":
            name = path.split("/")[-1]
            result = _rpc_create_session(name, body)
        elif path.startswith("/api/sessions/") and method == "DELETE":
            name = path.split("/")[-1]
            result = _rpc_kill_session(name, self.manager)
        elif path == "/api/specs" and method == "GET":
            result = _rpc_specs()
        elif path == "/api/files" and method == "GET":
            file_path = body.get("path", "~") if body else "~"
            result = _rpc_files(file_path)
        elif path == "/api/upload" and method == "POST":
            result = _rpc_upload(body)
        elif path == "/api/metrics" and method == "GET":
            result = _rpc_metrics()
        else:
            result = {"status": 404, "body": {"error": f"Unknown RPC: {method} {path}"}}

        await ws.send(make_rpc_response(req_id, result["status"], result["body"]))

    async def _session_update_loop(self, ws):
        """Periodically send session list updates to gateway."""
        prev_sessions = None
        while True:
            await asyncio.sleep(SESSION_POLL_INTERVAL)
            sessions = self.manager.list_sessions()
            # Only send if changed
            if sessions != prev_sessions:
                try:
                    await ws.send(make_session_update(sessions))
                except Exception:
                    break
                prev_sessions = sessions

    def stop(self):
        self._running = False
        if self.ws is not None:
            asyncio.create_task(self.ws.close())


async def main():
    if not CONFIG_FILE.exists():
        print("Error: No config found. Enroll this server first.", flush=True)
        print("  Run the enrollment command from the gateway.", flush=True)
        return

    config = load_config()
    agent = ServerAgent(config)

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, agent.stop)

    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
