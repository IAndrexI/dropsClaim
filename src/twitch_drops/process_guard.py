"""
Process Guard and Singleton Execution Enforcer for Twitch Drops Miner.
Ensures that only ONE instance of the Twitch Drops Miner runs at any time,
preventing duplicate stream viewing and conflicts.

ZERO EMOJIS compliant. Python standard library only.
"""

import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
PID_FILE = os.path.join(DATA_DIR, "miner.pid")


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is alive."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process != 0:
                kernel32.CloseHandle(process)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def acquire_pid_lock() -> bool:
    """
    Ensure only 1 instance of the Twitch Drops Miner can run at a time.
    Writes current PID to data/miner.pid.
    If another miner is running, returns False.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    current_pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid != current_pid and is_pid_alive(old_pid):
                        return False
        except Exception:
            pass

    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(current_pid))
        return True
    except Exception:
        return True


def release_pid_lock():
    """Remove PID lockfile upon shutdown."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.isdigit() and int(content) == os.getpid():
                    os.remove(PID_FILE)
    except Exception:
        pass


def get_running_bot_processes() -> List[Dict[str, str]]:
    """Scan operating system process list for active Twitch miner processes."""
    bots = []
    current_pid = os.getpid()

    # Linux / Alpine (/proc scan)
    if os.path.exists("/proc"):
        try:
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                pid = int(pid_dir)
                if pid == current_pid:
                    continue
                cmdline_path = f"/proc/{pid}/cmdline"
                try:
                    with open(cmdline_path, "rb") as f:
                        cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
                    if any(k in cmdline.lower() for k in ["twitch_drops", "miner.py", "twitchdropsminer"]) and "check_bots" not in cmdline:
                        bots.append({
                            "pid": str(pid),
                            "cmdline": cmdline,
                            "source": "/proc",
                        })
                except (IOError, PermissionError):
                    continue
        except Exception:
            pass

    # Windows fallback
    if not bots and os.name == "nt":
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", 
                 "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*twitch_drops*' -or $_.CommandLine -like '*miner.py*' } | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
                text=True,
                errors="ignore"
            ).strip()
            if out:
                import json
                try:
                    data = json.loads(out)
                    if isinstance(data, dict):
                        data = [data]
                    for item in data:
                        pid = str(item.get("ProcessId"))
                        cmd = str(item.get("CommandLine", ""))
                        if int(pid) != current_pid and "check_bots" not in cmd and "Get-CimInstance" not in cmd:
                            bots.append({"pid": pid, "cmdline": cmd, "source": "win32"})
                except Exception:
                    pass
        except Exception:
            pass

    return bots


def kill_pid(pid: int) -> bool:
    """Terminate process by PID."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False
