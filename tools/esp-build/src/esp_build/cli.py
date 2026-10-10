import argparse
import os
import re
from pathlib import Path
from typing import Any

from tb_contract import ContractArgumentParser, ToolError, progress, run

from esp_build.platformio import (
    ENVIRONMENT_NAME,
    firmware_path,
    list_boards,
    parse_diagnostics,
    parse_memory_usage,
    run_platformio,
)
from esp_build.scaffold import DEVICE_CONFIG_RELATIVE_PATH, write_project

WIFI_SSID_ENVIRONMENT_VARIABLE = "ESP_WIFI_SSID"
WIFI_PASSWORD_ENVIRONMENT_VARIABLE = "ESP_WIFI_PASSWORD"
OTA_PASSWORD_ENVIRONMENT_VARIABLE = "ESP_OTA_PASSWORD"
OTA_APP_FIRMWARE_MARKER = b"OtaApp boot"
MAX_REPORTED_ERRORS = 20
MAX_REPORTED_WARNINGS = 10
LOG_TAIL_LINES = 30
VALID_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?")

DESCRIPTION = """\
Write and compile ESP32 firmware (Arduino framework, C++) that can be flashed over WiFi and stays
flashable over WiFi afterwards. Supports every ESP32 family chip (ESP32, S2, S3, C3, C6, ...).

Workflow:
  1. tb esp-ota discover                      find the device; note its ip, port and `board` (chip)
  2. tb esp-build boards <chip>               pick a board id for that chip
  3. tb esp-build init --dir D --board B      create the project (WiFi credentials come from env vars)
  4. edit D/src/main.cpp                      write the application
  5. tb esp-build build --dir D               compile; fix reported errors and repeat
  6. tb esp-ota flash --host IP --firmware <firmware from step 5>
  7. tb esp-logs --host IP                    read what the application printed with Log

How to write the application (D/src/main.cpp):
  - Implement `void appSetup()` (runs once) and `void appLoop()` (runs forever). Do NOT define
    setup() or loop(): the bundled OtaApp library defines them, connects WiFi and serves OTA updates
    from a background task, so OTA keeps working even while appLoop() blocks or delays.
  - WiFi is already connected when appSetup() runs. Use WiFi.h, HTTPClient.h, WebServer.h etc. directly.
  - Print with `Log.println(...)` / `Log.printf(...)` instead of Serial. Log output is readable over
    WiFi with `tb esp-logs` and survives crashes, so it is the way to debug a device with no USB cable.
  - Add more .cpp/.h files under src/ freely. Add third-party libraries under lib_deps in platformio.ini.
  - If the application crashes on 3 consecutive boots, the device stops running it and only serves
    OTA updates ("safe mode") until new firmware is flashed, so a bad build can be replaced remotely.

Credentials are read only from environment variables, never from flags:
  ESP_WIFI_SSID      required  network the device must join (2.4 GHz)
  ESP_WIFI_PASSWORD  required  its password (set to an empty string for an open network)
  ESP_OTA_PASSWORD   optional  password that future OTA uploads to this firmware will require
`init` fails if a required one is unset. They are compiled into the firmware. The user must set them
in the environment; never guess them or write them into files yourself.

If newly flashed firmware cannot join WiFi within 60 s (wrong credentials, network down), the device
rolls back by itself to the firmware it ran before. `tb esp-ota flash` then still reports the device
as back online, so always confirm a flash with `tb esp-logs`: the new firmware logs
"this firmware joined WiFi and is now confirmed"; a rollback logs a line containing "ROLLBACK", or
esp-logs fails with log_server_not_running when the previous firmware was not built by esp-build.
"""

EXAMPLES = """\
Examples:
  tb esp-build boards esp32c3
  tb esp-build init --dir ~/esp/blinker --board esp32-c3-devkitm-1
  tb esp-build build --dir ~/esp/blinker

Output (`data` field of the JSON object on stdout):
  boards  {"boards": [{"board", "name", "chip", "flash_bytes"}], "truncated"}
  init    {"project_dir", "board", "hostname", "created": [...], "kept": [...], "rewritten": [...], "next"}
  build   {"firmware", "size_bytes", "flash": {"used_bytes", "total_bytes", "percent"}, "ram": {...},
           "warnings": [{"file", "line", "column", "message"}], "log", "next"}

Error codes: unknown_board, wifi_credentials_missing, invalid_hostname, not_a_project,
device_config_missing, compile_failed, ota_support_missing, board_listing_failed, timeout.
`compile_failed` carries `details.errors`: [{"file", "line", "column", "message"}], with paths relative
to the project directory.
"""


def _boards(arguments: argparse.Namespace) -> dict[str, Any]:
    boards = list_boards()
    if arguments.query:
        needle = arguments.query.lower().replace("-", "").replace("_", "")
        boards = [
            board
            for board in boards
            if needle in f"{board['board']} {board['name']} {board['chip']}".lower().replace("-", "").replace("_", "")
        ]
    result: dict[str, Any] = {"boards": boards[: arguments.limit], "truncated": len(boards) > arguments.limit}
    if result["truncated"]:
        result["note"] = f"{len(boards)} boards match. Narrow the query or raise --limit."
    if not boards:
        result["note"] = "No boards match. Try a shorter query such as the chip name (esp32, esp32s3, esp32c3)."
    return result


def _default_hostname(project_dir: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", project_dir.name).strip("-")[:32].strip("-") or "esp-device"


def _init(arguments: argparse.Namespace) -> dict[str, Any]:
    project_dir = Path(arguments.dir).expanduser().resolve()

    required_variables = (WIFI_SSID_ENVIRONMENT_VARIABLE, WIFI_PASSWORD_ENVIRONMENT_VARIABLE)
    missing_variables = [name for name in required_variables if name not in os.environ]
    if os.environ.get(WIFI_SSID_ENVIRONMENT_VARIABLE) == "":
        missing_variables.append(WIFI_SSID_ENVIRONMENT_VARIABLE)
    if missing_variables:
        raise ToolError(
            "wifi_credentials_missing",
            f"Required environment variable(s) not set: {', '.join(missing_variables)}.",
            f"Ask the user to export ${WIFI_SSID_ENVIRONMENT_VARIABLE} and ${WIFI_PASSWORD_ENVIRONMENT_VARIABLE} "
            "(the 2.4 GHz network the device is on) in the environment this command runs in, e.g. their shell "
            "profile. Credentials are deliberately not accepted as flags. Never guess them: wrong credentials "
            "take the device offline.",
        )

    hostname = arguments.hostname or _default_hostname(project_dir)
    if not VALID_HOSTNAME.fullmatch(hostname):
        raise ToolError(
            "invalid_hostname",
            f"`{hostname}` is not a valid hostname.",
            "Use 1-32 letters, digits and hyphens, not starting or ending with a hyphen.",
        )

    if not (project_dir / "platformio.ini").exists():
        known_boards = {board["board"] for board in list_boards()}
        if arguments.board not in known_boards:
            raise ToolError(
                "unknown_board",
                f"`{arguments.board}` is not an ESP32-family board id.",
                "Run `tb esp-build boards <chip>` with the `board` value reported by `tb esp-ota discover` "
                "(e.g. esp32c3) and pass one of the returned `board` ids.",
            )

    outcome = write_project(
        project_dir,
        arguments.board,
        wifi_ssid=os.environ[WIFI_SSID_ENVIRONMENT_VARIABLE],
        wifi_password=os.environ[WIFI_PASSWORD_ENVIRONMENT_VARIABLE],
        hostname=hostname,
        ota_password=os.environ.get(OTA_PASSWORD_ENVIRONMENT_VARIABLE, ""),
    )
    result: dict[str, Any] = {
        "project_dir": str(project_dir),
        "board": arguments.board,
        "hostname": hostname,
        **outcome,
        "next": f"Write the application in {project_dir / 'src' / 'main.cpp'} (appSetup/appLoop), then run "
        f"`tb esp-build build --dir {project_dir}`.",
    }
    if "platformio.ini" in outcome["kept"]:
        result["note"] = "platformio.ini already existed and was kept, so --board was not applied."
    return result


def _build(arguments: argparse.Namespace) -> dict[str, Any]:
    project_dir = Path(arguments.dir).expanduser().resolve()
    if not (project_dir / "platformio.ini").is_file():
        raise ToolError(
            "not_a_project",
            f"{project_dir} has no platformio.ini.",
            "Create the project first with `tb esp-build init --dir <dir> --board <board>`.",
        )
    if not (project_dir / DEVICE_CONFIG_RELATIVE_PATH).is_file():
        raise ToolError(
            "device_config_missing",
            f"{project_dir / DEVICE_CONFIG_RELATIVE_PATH} is missing (it holds the WiFi credentials and is not "
            "committed to git).",
            f"Regenerate it with `tb esp-build init --dir {project_dir} --board <board>`. Existing source files "
            "are kept.",
        )

    progress("compiling (the first build downloads the ESP32 toolchain and can take 10+ minutes)")
    build = run_platformio(
        ["run", "--project-dir", str(project_dir), "--environment", ENVIRONMENT_NAME],
        arguments.timeout,
        merge_streams=True,
    )
    log_path = project_dir / ".pio" / "build.log"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text(build.stdout)
    diagnostics = parse_diagnostics(build.stdout, project_dir)

    if build.returncode != 0:
        errors = diagnostics["errors"]
        details: dict[str, Any] = {
            "errors": errors[:MAX_REPORTED_ERRORS],
            "errors_truncated": len(errors) > MAX_REPORTED_ERRORS,
            "log": str(log_path),
        }
        if not errors:
            details["log_tail"] = build.stdout.splitlines()[-LOG_TAIL_LINES:]
        raise ToolError(
            "compile_failed",
            f"The build failed with {len(errors)} error(s)." if errors else "The build failed before compiling.",
            "Fix each entry in `details.errors` and rerun the build. `multiple definition of setup/loop` means "
            "main.cpp defines setup()/loop(): rename them to appSetup()/appLoop(). `undefined reference to "
            "appSetup/appLoop` means they are missing. A missing header usually needs a library added under "
            "lib_deps in platformio.ini."
            if errors
            else "Read `details.log_tail`; this is usually a platformio.ini problem or a failed toolchain download.",
            details,
        )

    firmware = firmware_path(project_dir)
    if OTA_APP_FIRMWARE_MARKER not in firmware.read_bytes():
        raise ToolError(
            "ota_support_missing",
            "The firmware compiled, but the OtaApp library is not linked into it. Flashing it would make the "
            "device unreachable for further OTA updates.",
            "Keep the `symlink://.../OtaApp` line under lib_deps in platformio.ini and `#include <OtaApp.h>` in "
            "src/main.cpp, and implement appSetup()/appLoop() instead of setup()/loop().",
        )

    warnings = diagnostics["warnings"]
    return {
        "firmware": str(firmware),
        "size_bytes": firmware.stat().st_size,
        **parse_memory_usage(build.stdout),
        "warnings": warnings[:MAX_REPORTED_WARNINGS],
        "warnings_truncated": len(warnings) > MAX_REPORTED_WARNINGS,
        "log": str(log_path),
        "next": f"Find the device with `tb esp-ota discover`, then run `tb esp-ota flash --host <ip> --port <port> "
        f"--firmware {firmware}`. If the device currently requires an OTA password, add --password with the "
        "password of the firmware running on it now.",
    }


def _build_parser() -> ContractArgumentParser:
    parser = ContractArgumentParser(prog="tb esp-build", description=DESCRIPTION, epilog=EXAMPLES)
    subcommands = parser.add_subparsers(dest="subcommand", required=True, metavar="{boards,init,build}")

    boards = subcommands.add_parser(
        "boards",
        help="Search ESP32-family board ids by chip or name (read-only).",
        description="Search the board ids accepted by `init --board`. Matches against id, name and chip.",
        epilog="Example:\n  tb esp-build boards esp32c3\n\n"
        "If the exact board is unknown, the chip vendor's devkit id is a safe choice for plain WiFi/GPIO code "
        "(esp32dev, esp32-s3-devkitc-1, esp32-c3-devkitm-1).",
    )
    boards.add_argument("query", nargs="?", help="Text to match, e.g. the `board` value from `tb esp-ota discover`.")
    boards.add_argument("--limit", type=int, default=25, help="Maximum boards to return. Default: 25.")
    boards.set_defaults(handler=_boards)

    init = subcommands.add_parser(
        "init",
        help="Create a firmware project, or refresh the credentials of an existing one.",
        description="Create a firmware project in --dir. Safe to rerun: platformio.ini and src/main.cpp are "
        "never overwritten; only src/device_config.cpp (credentials and hostname) is regenerated.",
        epilog="Example:\n  tb esp-build init --dir ~/esp/blinker --board esp32-c3-devkitm-1\n\n"
        f"Requires ${WIFI_SSID_ENVIRONMENT_VARIABLE} and ${WIFI_PASSWORD_ENVIRONMENT_VARIABLE} in the environment; "
        f"${OTA_PASSWORD_ENVIRONMENT_VARIABLE} is optional. There are no flags for credentials.",
    )
    init.add_argument("--dir", required=True, help="Project directory. Created if missing.")
    init.add_argument("--board", required=True, help="Board id from `tb esp-build boards`.")
    init.add_argument(
        "--hostname",
        help="Network name the device announces (<hostname>.local). Default: derived from the directory name.",
    )
    init.set_defaults(handler=_init)

    build = subcommands.add_parser(
        "build",
        help="Compile the project and report the firmware path or structured compile errors.",
        description="Compile the project. On success returns the path of the .bin to pass to "
        "`tb esp-ota flash`; on failure returns each compiler error with file, line and message.",
        epilog="Example:\n  tb esp-build build --dir ~/esp/blinker",
    )
    build.add_argument("--dir", required=True, help="Project directory created by `init`.")
    build.add_argument(
        "--timeout", type=float, default=1800, help="Seconds before the build is abandoned. Default: 1800."
    )
    build.set_defaults(handler=_build)
    return parser


def _execute() -> dict[str, Any]:
    arguments = _build_parser().parse_args()
    return arguments.handler(arguments)


def main() -> None:
    run(_execute)
