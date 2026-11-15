"""
Lightweight Twitch PubSub WebSocket Client (Pure Python Standard Library).

Connects to wss://pubsub-edge.twitch.tv/v1 and listens to:
  - video-playback-by-id.<channel_id>
  - community-points-user-v1.<user_id>
This maintains active viewer presence on Twitch's Edge WebSocket infrastructure,
exactly replicating what an open Twitch browser tab does in the background.
"""

import base64
import json
import logging
import os
import random
import socket
import ssl
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Set

from .activity_logger import log_event

logger = logging.getLogger("twitch_pubsub")

PUBSUB_HOST = "pubsub-edge.twitch.tv"
PUBSUB_PORT = 443


class TwitchPubSubWatcher:
    """
    Background worker that maintains an authenticated WebSocket connection to Twitch PubSub Edge.
    Listens to video-playback-by-id for the active stream channel and keeps viewer presence alive.
    """

    def __init__(self, user_agent: Optional[str] = None):
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        self._sock: Optional[ssl.SSLSocket] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_channel_id: str = ""
        self._current_channel_login: str = ""
        self._auth_token: str = ""
        self._last_ping_time: float = 0.0
        self._is_connected = False
        self._subscribed_topics: Set[str] = set()

    def is_connected(self) -> bool:
        return self._is_connected and self._running

    def start(self, channel_id: str, channel_login: str = "", auth_token: str = "") -> None:
        """Start or update the PubSub watcher for a given channel."""
        clean_cid = str(channel_id or "").strip()
        clean_login = str(channel_login or "").strip().lower()
        if not clean_cid:
            return

        with self._lock:
            self._current_channel_id = clean_cid
            self._current_channel_login = clean_login
            if auth_token:
                self._auth_token = str(auth_token).strip()

            if self._running and self._thread and self._thread.is_alive():
                # Already running, just send LISTEN for the new topic
                self._send_listen_topic(clean_cid)
                return

            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TwitchPubSub")
            self._thread.start()

    def update_channel(self, channel_id: str, channel_login: str = "", auth_token: str = "") -> None:
        """Switch listening topic to a new channel without reconnecting."""
        self.start(channel_id=channel_id, channel_login=channel_login, auth_token=auth_token)

    def stop(self) -> None:
        """Cleanly close WebSocket connection and stop background thread."""
        with self._lock:
            self._running = False
            self._is_connected = False
            sock = self._sock
            self._sock = None

        if sock:
            try:
                sock.close()
            except Exception:
                pass
        log_event("TWITCH_PUBSUB", "stopped", {"channel": self._current_channel_login})

    def _send_ws_frame(self, sock: ssl.SSLSocket, payload_bytes: bytes, opcode: int = 0x1) -> None:
        length = len(payload_bytes)
        hdr = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            hdr.append(0x80 | length)
        elif length < 65536:
            hdr.append(0x80 | 126)
            hdr.extend(struct.pack("!H", length))
        else:
            hdr.append(0x80 | 127)
            hdr.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        hdr.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload_bytes))
        sock.sendall(bytes(hdr) + masked)

    def _send_listen_topic(self, channel_id: str) -> None:
        topic = f"video-playback-by-id.{channel_id}"
        if topic in self._subscribed_topics:
            return
        sock = self._sock
        if not sock:
            return
        nonce = f"pubsub_{channel_id}_{random.randint(100000, 999999)}"
        listen_payload: Dict[str, Any] = {
            "type": "LISTEN",
            "nonce": nonce,
            "data": {
                "topics": [topic],
            },
        }
        if self._auth_token:
            listen_payload["data"]["auth_token"] = self._auth_token
        try:
            self._send_ws_frame(sock, json.dumps(listen_payload).encode("utf-8"))
            self._subscribed_topics.add(topic)
            log_event(
                "TWITCH_PUBSUB",
                "listen_topic",
                {"topic": topic, "channel": self._current_channel_login, "has_auth": bool(self._auth_token)},
            )
            logger.info("[PUBSUB] Subscribed to %s on pubsub-edge.twitch.tv", topic)
        except Exception as err:
            logger.debug("[PUBSUB] Failed to send LISTEN frame: %s", err)

    def _connect_and_handshake(self) -> Optional[ssl.SSLSocket]:
        try:
            raw_sock = socket.create_connection((PUBSUB_HOST, PUBSUB_PORT), timeout=10)
            ctx = ssl.create_default_context()
            ssock = ctx.wrap_socket(raw_sock, server_hostname=PUBSUB_HOST)
            sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET /v1 HTTP/1.1\r\n"
                f"Host: {PUBSUB_HOST}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {sec_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {self.user_agent}\r\n\r\n"
            ).encode("ascii")
            ssock.sendall(req)

            buf = bytearray()
            ssock.settimeout(10)
            while b"\r\n\r\n" not in buf:
                chunk = ssock.recv(4096)
                if not chunk:
                    raise ConnectionError("PubSub connection closed during handshake")
                buf.extend(chunk)

            hdr = bytes(buf).decode("utf-8", errors="ignore")
            if "101 Switching Protocols" not in hdr:
                raise ConnectionError(f"Unexpected PubSub handshake status: {hdr[:120]!r}")

            ssock.settimeout(5.0)
            log_event("TWITCH_PUBSUB", "connected", {"host": PUBSUB_HOST, "channel": self._current_channel_login})
            logger.info("[PUBSUB] Connected to %s", PUBSUB_HOST)
            return ssock
        except Exception as err:
            log_event("TWITCH_PUBSUB", "connection_error", {"error": str(err)}, level="WARNING")
            return None

    def _run_loop(self) -> None:
        """Maintain persistent connection, send heartbeats, and read frames."""
        while self._running:
            sock = self._connect_and_handshake()
            if not sock:
                # Backoff before reconnect
                for _ in range(10):
                    if not self._running:
                        return
                    time.sleep(1.0)
                continue

            with self._lock:
                self._sock = sock
                self._is_connected = True
                self._subscribed_topics.clear()

            # Subscribe to current channel topic
            cid = self._current_channel_id
            if cid:
                self._send_listen_topic(cid)

            self._last_ping_time = time.time()
            buf = bytearray()

            while self._running:
                now = time.time()
                # Send PubSub PING every 4 minutes (Twitch spec requires PING <= 5m)
                if now - self._last_ping_time > 240:
                    try:
                        self._send_ws_frame(sock, json.dumps({"type": "PING"}).encode("utf-8"))
                        self._last_ping_time = now
                        log_event("TWITCH_PUBSUB", "ping_sent", {"channel": self._current_channel_login})
                    except Exception as err:
                        logger.debug("[PUBSUB] Ping failed: %s", err)
                        break

                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    # Process frame header
                    while len(buf) >= 2:
                        b1, b2 = buf[0], buf[1]
                        opcode = b1 & 0x0F
                        masked = (b2 & 0x80) != 0
                        length = b2 & 0x7F
                        offset = 2
                        if length == 126:
                            if len(buf) < 4:
                                break
                            length = struct.unpack("!H", buf[2:4])[0]
                            offset = 4
                        elif length == 127:
                            if len(buf) < 10:
                                break
                            length = struct.unpack("!Q", buf[2:10])[0]
                            offset = 10

                        if masked:
                            offset += 4
                        if len(buf) < offset + length:
                            break

                        payload = bytes(buf[offset : offset + length])
                        del buf[: offset + length]

                        if opcode == 0x9:  # ping
                            self._send_ws_frame(sock, payload, opcode=0xA)
                        elif opcode == 0x8:  # close
                            break
                        elif opcode == 0x1:  # text
                            try:
                                text_msg = payload.decode("utf-8", errors="ignore")
                                parsed = json.loads(text_msg)
                                msg_type = parsed.get("type")
                                if msg_type == "PONG":
                                    pass
                                elif msg_type == "RECONNECT":
                                    logger.info("[PUBSUB] Received RECONNECT directive from Twitch")
                                    raise ConnectionResetError("Server requested reconnect")
                                elif msg_type == "MESSAGE":
                                    msg_data = parsed.get("data") or {}
                                    topic = msg_data.get("topic", "")
                                    log_event("TWITCH_PUBSUB", "pubsub_message", {"topic": topic})
                            except Exception:
                                pass
                except socket.timeout:
                    continue
                except Exception as err:
                    logger.debug("[PUBSUB] Connection read loop exit: %s", err)
                    break

            with self._lock:
                self._is_connected = False
                if self._sock is sock:
                    self._sock = None
            try:
                sock.close()
            except Exception:
                pass

            if self._running:
                time.sleep(3.0)
