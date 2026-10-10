import argparse
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from tb_contract import ContractArgumentParser, ToolError, progress, run

from esp_ota.arduino_ota import DEFAULT_PORT, TARGET_COMMANDS, Image, flash_image
from esp_ota.discovery import discover_arduino_ota_devices

PASSWORD_ENVIRONMENT_VARIABLE = "ESP_OTA_PASSWORD"
ESP_APP_IMAGE_MAGIC = b"\xe9"
GZIP_MAGIC = b"\x1f\x8b"
REBOOT_GRACE_SECONDS = 3

DESCRIPTION = """\
Flash ESP32 / ESP8266 devices over WiFi, without a USB cable.

Works with any device whose running sketch includes the ArduinoOTA library (Arduino IDE,
arduino-cli and PlatformIO `framework = arduino` builds). The device must already be running
OTA-capable firmware; the very first flash of a blank chip has to go over USB.

Typical flow: `discover` to find the device, then `flash` with its ip and port.
"""

EXAMPLES = """\
Examples:
  tb esp-ota discover
  tb esp-ota flash --host 192.168.1.42 --firmware .pio/build/esp32dev/firmware.bin
  tb esp-ota flash --host livingroom.local --port 8266 --firmware build/sketch.ino.bin --password hunter2
  tb esp-ota flash --host 192.168.1.42 --firmware firmware.bin --dry-run

Output (`data` field of the JSON object on stdout):
  discover  {"devices": [{"name", "hostname", "ip", "port", "board", "auth_required", "protocol"}]}
  flash     {"ip", "port", "target", "image": {"path", "size_bytes", "md5"}, "auth",
             "duration_seconds", "back_online"}
            A successful flash means the device received the image, verified its MD5 and is rebooting
            into it. `back_online` is true if the device answered a ping after rebooting, false if it
            did not within --online-timeout, null if not checked.

Error codes from flash: image_not_found, invalid_image, host_unresolvable, no_response, auth_required,
auth_failed, unsupported_auth, unexpected_reply, device_did_not_connect, device_error, connection_closed,
network_error. Every error carries a `hint` describing the fix.
"""


def _load_image(path_argument: str, target: str) -> Image:
    path = Path(path_argument).expanduser().resolve()
    if not path.is_file():
        raise ToolError(
            "image_not_found",
            f"No file at {path}.",
            "Pass the path to a compiled .bin. PlatformIO writes it to .pio/build/<env>/firmware.bin; "
            "arduino-cli writes <sketch>.ino.bin into its --output-dir.",
        )
    content = path.read_bytes()
    if target == "firmware" and not content.startswith((ESP_APP_IMAGE_MAGIC, GZIP_MAGIC)):
        raise ToolError(
            "invalid_image",
            f"{path} is not an ESP application image (it does not start with byte 0xE9).",
            "Pass the application .bin (firmware.bin / <sketch>.ino.bin), not an .elf, bootloader, partition "
            "table or merged factory image. For a LittleFS/SPIFFS image use --target filesystem.",
        )
    return Image(path, content)


def _resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        raise ToolError(
            "host_unresolvable",
            f"Could not resolve `{host}` to an IP address.",
            "Run `tb esp-ota discover` and pass the device's `ip` as --host.",
        ) from None


def _answers_ping_within(ip: str, timeout: float) -> bool | None:
    time.sleep(REBOOT_GRACE_SECONDS)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ping = subprocess.run(
                ["ping", "-c", "1", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2
            )
            if ping.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            return None
        time.sleep(1)
    return False


def _discover(arguments: argparse.Namespace) -> dict[str, Any]:
    progress(f"listening for mDNS announcements for {arguments.timeout:g}s")
    devices = discover_arduino_ota_devices(arguments.timeout)
    result: dict[str, Any] = {"devices": devices}
    if not devices:
        result["note"] = (
            "No devices announced themselves. Discovery relies on mDNS, which some networks block; a device "
            "that is not listed can still be flashed if you know its IP. Otherwise retry with a longer "
            "--timeout and check that the device is powered and joined to this network."
        )
    return result


def _flash(arguments: argparse.Namespace) -> dict[str, Any]:
    image = _load_image(arguments.firmware, arguments.target)
    device_ip = _resolve(arguments.host)
    result: dict[str, Any] = {
        "ip": device_ip,
        "port": arguments.port,
        "target": arguments.target,
        "image": {"path": str(image.path), "size_bytes": image.size, "md5": image.md5},
    }
    if arguments.dry_run:
        return {**result, "dry_run": True}

    password = arguments.password or os.environ.get(PASSWORD_ENVIRONMENT_VARIABLE)
    started = time.monotonic()
    result["auth"] = flash_image(
        device_ip, arguments.port, image, arguments.target, password, arguments.invite_timeout
    )
    result["duration_seconds"] = round(time.monotonic() - started, 1)

    result["back_online"] = None
    if not arguments.no_wait:
        progress("image accepted; waiting for the device to reboot")
        result["back_online"] = _answers_ping_within(device_ip, arguments.online_timeout)
        if result["back_online"] is False:
            result["warning"] = (
                f"The device accepted the image but did not answer a ping within {arguments.online_timeout:g}s "
                "of rebooting. The new firmware may be crashing, failing to join WiFi, or using a different IP. "
                "Run `tb esp-ota discover` to look for it."
            )
    return result


def _build_parser() -> ContractArgumentParser:
    parser = ContractArgumentParser(prog="tb esp-ota", description=DESCRIPTION, epilog=EXAMPLES)
    subcommands = parser.add_subparsers(dest="subcommand", required=True, metavar="{discover,flash}")

    discover = subcommands.add_parser(
        "discover",
        help="Find OTA-capable devices on the local network (read-only).",
        description="Listen for mDNS announcements from OTA-capable ESP devices. Read-only.",
        epilog="Example:\n  tb esp-ota discover --timeout 5",
    )
    discover.add_argument("--timeout", type=float, default=3, help="Seconds to listen. Default: 3.")
    discover.set_defaults(handler=_discover)

    flash = subcommands.add_parser(
        "flash",
        help="Upload a compiled .bin to one device and reboot it into the new image.",
        description="Upload a compiled image to one device over WiFi. The device reboots into it on success "
        "and keeps its previous firmware if the transfer fails.",
        epilog="Example:\n  tb esp-ota flash --host 192.168.1.42 --firmware .pio/build/esp32dev/firmware.bin",
    )
    flash.add_argument("--host", required=True, help="Device IP address or hostname (e.g. 192.168.1.42).")
    flash.add_argument("--firmware", required=True, help="Path to the compiled .bin image to upload.")
    flash.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Device OTA port. Default: {DEFAULT_PORT} (ESP32). ESP8266 uses 8266. `discover` reports it.",
    )
    flash.add_argument(
        "--target",
        choices=sorted(TARGET_COMMANDS),
        default="firmware",
        help="What the image is: the application (`firmware`, default) or a LittleFS/SPIFFS image (`filesystem`).",
    )
    flash.add_argument(
        "--password",
        help=f"OTA password, if the device requires one. Falls back to ${PASSWORD_ENVIRONMENT_VARIABLE}.",
    )
    flash.add_argument(
        "--protocol",
        choices=["arduino"],
        default="arduino",
        help="OTA protocol the device speaks. Only `arduino` (ArduinoOTA / espota) is supported so far.",
    )
    flash.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the image and resolve the host, but send nothing to the device.",
    )
    flash.add_argument(
        "--no-wait", action="store_true", help="Return as soon as the image is accepted; skip the reboot check."
    )
    flash.add_argument(
        "--invite-timeout",
        type=float,
        default=10,
        help="Seconds to wait for the device to answer the initial invitation. Default: 10.",
    )
    flash.add_argument(
        "--online-timeout",
        type=float,
        default=30,
        help="Seconds to wait for the device to answer a ping after rebooting. Default: 30.",
    )
    flash.set_defaults(handler=_flash)
    return parser


def _execute() -> dict[str, Any]:
    arguments = _build_parser().parse_args()
    return arguments.handler(arguments)


def main() -> None:
    run(_execute)
