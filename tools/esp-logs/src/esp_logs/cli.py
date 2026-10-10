import argparse
import socket
import time
from typing import Any

from tb_contract import ContractArgumentParser, ToolError, progress, run

from esp_logs.device_log import DEFAULT_PORT, LogCollector, fetch_snapshot

POLL_INTERVAL_SECONDS = 1

DESCRIPTION = """\
Read what an ESP32 printed with `Log.println(...)` / `Log.printf(...)`, over WiFi, plus its health.
This replaces the serial monitor for devices that have no USB cable attached.

Only works with firmware built by `tb esp-build` (it bundles the log server). The device keeps the
most recent ~4 KB of log output. That buffer survives crashes, reboots and OTA updates, so the lines
printed just before a crash are still readable afterwards; it is lost only when power is removed.
Each boot adds a line `--- OtaApp boot: reset_reason=... ---`.

Reading is read-only and can be repeated freely. To see only new output, pass the `cursor` from the
previous result as --since.
"""

EXAMPLES = """\
Examples:
  tb esp-logs --host 192.168.1.42
  tb esp-logs --host 192.168.1.42 --since 1873
  tb esp-logs --host 192.168.1.42 --wait 20          # right after flashing: tolerate the reboot, collect 20 s
  tb esp-logs --host blinker.local --max-lines 30

Output (`data` field of the JSON object on stdout):
  {"device": {"hostname", "uptime_seconds", "reset_reason", "safe_mode", "consecutive_crashes",
              "free_heap_bytes"},
   "lines": ["..."], "cursor": 2210, "missed_bytes": 0, "log_restarted": false, "truncated": false}

  device.reset_reason         why the device last booted: power_on, software_restart (also after an OTA
                              update), crash, task_watchdog, interrupt_watchdog, watchdog, brownout,
                              reset_pin, deep_sleep_wake, unknown
  device.safe_mode            true = the application crashed on 3 consecutive boots and is NOT running;
                              flash fixed firmware to leave safe mode
  device.consecutive_crashes  crashes in a row without 30 s of stable uptime in between
  lines                       complete log lines, oldest first
  cursor                      pass as --since next time to get only newer lines
  missed_bytes                log bytes that were overwritten on the device before they could be read
  log_restarted               true = the device lost power since --since was taken; all lines are returned
  truncated                   true = more than --max-lines matched; only the newest are returned

Error codes: host_unresolvable, device_unreachable, log_server_not_running, malformed_response.
"""


def _resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        raise ToolError(
            "host_unresolvable",
            f"Could not resolve `{host}` to an IP address.",
            "Run `tb esp-ota discover` and pass the device's `ip` as --host.",
        ) from None


def _read(arguments: argparse.Namespace) -> dict[str, Any]:
    device_ip = _resolve(arguments.host)
    collector = LogCollector(cursor=arguments.since)
    deadline = time.monotonic() + arguments.wait
    snapshot = None
    last_error = None

    if arguments.wait:
        progress(f"collecting logs for {arguments.wait:g}s")
    while True:
        try:
            snapshot = fetch_snapshot(device_ip, arguments.port, arguments.timeout)
            collector.add(snapshot)
        except ToolError as error:
            last_error = error
        if time.monotonic() + POLL_INTERVAL_SECONDS > deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if snapshot is None:
        raise last_error

    lines = collector.lines()
    status = snapshot.status
    result: dict[str, Any] = {
        "device": {
            "hostname": status.get("hostname"),
            "uptime_seconds": round(status.get("uptime_ms", 0) / 1000, 1),
            "reset_reason": status.get("reset_reason"),
            "safe_mode": status.get("safe_mode"),
            "consecutive_crashes": status.get("consecutive_crashes"),
            "free_heap_bytes": status.get("free_heap_bytes"),
        },
        "lines": lines[-arguments.max_lines :],
        "cursor": collector.cursor,
        "missed_bytes": collector.missed_bytes,
        "log_restarted": collector.log_restarted,
        "truncated": len(lines) > arguments.max_lines,
    }
    if status.get("safe_mode"):
        result["warning"] = (
            "The device is in safe mode: the application crashed on consecutive boots and is not running. "
            "The lines before the last `--- OtaApp boot` markers show what it printed before crashing. "
            "Fix the code, then rebuild and flash; OTA still works."
        )
    return result


def _build_parser() -> ContractArgumentParser:
    parser = ContractArgumentParser(prog="tb esp-logs", description=DESCRIPTION, epilog=EXAMPLES)
    parser.add_argument("--host", required=True, help="Device IP address or hostname (e.g. 192.168.1.42).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Device log port. Default: {DEFAULT_PORT}.")
    parser.add_argument(
        "--since", type=int, help="`cursor` from a previous result; return only lines logged after it."
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0,
        help="Keep collecting for this many seconds before returning, retrying while the device is "
        "unreachable (e.g. rebooting). Default: 0 (read once).",
    )
    parser.add_argument(
        "--max-lines", type=int, default=200, help="Return at most this many lines, keeping the newest. Default: 200."
    )
    parser.add_argument(
        "--timeout", type=float, default=5, help="Seconds to wait for each connection to the device. Default: 5."
    )
    parser.set_defaults(handler=_read)
    return parser


def _execute() -> dict[str, Any]:
    arguments = _build_parser().parse_args()
    return arguments.handler(arguments)


def main() -> None:
    run(_execute)
