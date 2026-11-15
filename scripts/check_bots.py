"""
Diagnostic and process management tool for Twitch Drops Miner.
Checks if any bots or miners are running on the system, verifies PID locks,
and ensures only 1 viewing instance of Twitch is running at any time.

ZERO EMOJIS compliant. Python standard library only.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.twitch_drops.process_guard import (
    PID_FILE,
    get_running_bot_processes,
    is_pid_alive,
    kill_pid,
)

DATA_DIR = os.path.join(BASE_DIR, "data")
STATUS_FILE = os.path.join(DATA_DIR, "miner_status.json")


def get_pid_file_info() -> Dict[str, Optional[int]]:

    """Inspect data/miner.pid file status."""
    if not os.path.exists(PID_FILE):
        return {"exists": False, "pid": None, "alive": False}
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            pid = int(content)
            alive = is_pid_alive(pid)
            return {"exists": True, "pid": pid, "alive": alive}
    except Exception:
        return {"exists": True, "pid": None, "alive": False}


def main():
    parser = argparse.ArgumentParser(description="Check and manage running Twitch bot processes")
    parser.add_argument("--kill-duplicates", action="store_true", help="Kill duplicate miner processes keeping at most 1 alive")
    parser.add_argument("--kill-all", action="store_true", help="Kill all running miner processes")
    args = parser.parse_args()

    print("==========================================================")
    print("           TWITCH BOT RUNTIME AUDIT & DIAGNOSTIC          ")
    print("==========================================================")

    pid_info = get_pid_file_info()
    if pid_info["exists"]:
        status_text = "ALIVE" if pid_info["alive"] else "DEAD (stale lockfile)"
        print(f"PID File: {PID_FILE} -> PID: {pid_info['pid']} [{status_text}]")
    else:
        print(f"PID File: None active")

    running_bots = get_running_bot_processes()
    print(f"\nActive Twitch Bot Processes Detected: {len(running_bots)}")

    if not running_bots:
        print(" [OK] No conflicting background Twitch bots detected on this system.")
    else:
        for idx, bot in enumerate(running_bots, 1):
            print(f" [{idx}] PID: {bot['pid']}")
            print(f"     Command: {bot['cmdline']}")

    if args.kill_all and running_bots:
        print("\n[ACTION] Terminating all running Twitch miner processes...")
        for bot in running_bots:
            p = int(bot["pid"])
            if kill_pid(p):
                print(f"  Terminated PID {p}")
            else:
                print(f"  Failed to terminate PID {p}")
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
                print("  Removed stale PID lockfile.")
            except Exception:
                pass
        return

    if args.kill_duplicates:
        if len(running_bots) > 1:
            print(f"\n[ACTION] Found {len(running_bots)} running bots. Enforcing single viewing by terminating duplicates...")
            # Keep the first or primary one, terminate the rest
            for bot in running_bots[1:]:
                p = int(bot["pid"])
                if kill_pid(p):
                    print(f"  Terminated duplicate bot PID {p}")
                else:
                    print(f"  Failed to terminate PID {p}")
        else:
            print("\n[OK] No duplicates to kill.")

    if len(running_bots) > 1:
        print("\n[WARNING] MULTIPLE BOT PROCESSES DETECTED!")
        print("Twitch drop policy: Only 1 channel can earn drops at a time.")
        print("Multiple bots watching streams simultaneously causes Twitch to pause all progress.")
        print("Run with --kill-duplicates or restart the service to enforce single viewing.")

    print("==========================================================")


if __name__ == "__main__":
    main()
