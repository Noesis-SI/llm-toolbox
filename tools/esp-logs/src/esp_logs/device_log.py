"""Client for the log server of the OtaApp device library (tools/esp-build/device_lib/OtaApp).

On every TCP connection the device sends one JSON status line, then the raw bytes of its retained
log buffer, then closes. `log_end` in the status is the total number of bytes ever logged, which
makes it a cursor: the buffer holds bytes [log_end - len(buffer), log_end).
"""

import json
import socket
from dataclasses import dataclass
from typing import Any

from tb_contract import ToolError

DEFAULT_PORT = 3233


@dataclass(frozen=True)
class Snapshot:
    status: dict[str, Any]
    log: bytes

    @property
    def end(self) -> int:
        return self.status["log_end"]

    @property
    def start(self) -> int:
        return self.end - len(self.log)


def fetch_snapshot(ip: str, port: int, timeout: float) -> Snapshot:
    try:
        with socket.create_connection((ip, port), timeout) as connection:
            connection.settimeout(timeout)
            response = b""
            while chunk := connection.recv(8192):
                response += chunk
    except ConnectionRefusedError:
        raise ToolError(
            "log_server_not_running",
            f"{ip} is up but nothing listens on TCP port {port}.",
            "Logs are only served by firmware built with `tb esp-build` (OtaApp library). If such firmware was "
            "just flashed, the device may still be joining WiFi: retry with `--wait 15`. If it still fails "
            "more than 90 s after a flash, the new firmware could not join WiFi (wrong ESP_WIFI_SSID / "
            "ESP_WIFI_PASSWORD?) and the device rolled back to its previous firmware. Otherwise build and "
            "flash the project with `tb esp-build build` and `tb esp-ota flash` first.",
        ) from None
    except OSError as error:
        raise ToolError(
            "device_unreachable",
            f"Could not read logs from {ip}:{port}: {error}.",
            "The device may be rebooting, off, or on another IP. Retry with `--wait 15`, or run "
            "`tb esp-ota discover` to find its current address.",
        ) from None

    status_line, _, log = response.partition(b"\n")
    try:
        status = json.loads(status_line)
        if not isinstance(status["log_end"], int):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise ToolError(
            "malformed_response",
            f"{ip}:{port} did not answer like an OtaApp log server (got {status_line[:80]!r}).",
            "Check --port (default 3233) and that the device runs firmware built with `tb esp-build`.",
        ) from None
    return Snapshot(status, log)


@dataclass
class LogCollector:
    """Accumulates the complete log lines that appeared after `cursor` across successive snapshots."""

    cursor: int | None
    collected: bytes = b""
    missed_bytes: int = 0
    log_restarted: bool = False

    def add(self, snapshot: Snapshot) -> None:
        if self.cursor is not None and snapshot.end < self.cursor:
            self.log_restarted = True
            self.cursor = None

        starts_mid_line = False
        if self.cursor is None:
            self.cursor = snapshot.start
            starts_mid_line = snapshot.start > 0
        elif snapshot.start > self.cursor:
            self.missed_bytes += snapshot.start - self.cursor
            self.cursor = snapshot.start
            starts_mid_line = True

        fresh = snapshot.log[self.cursor - snapshot.start :]
        if starts_mid_line:
            fresh = fresh.partition(b"\n")[2]
        complete, newline, partial = fresh.rpartition(b"\n")
        if newline:
            self.collected += complete + newline
            self.cursor = snapshot.end - len(partial)

    def lines(self) -> list[str]:
        return self.collected.decode(errors="replace").splitlines()
