import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from tb_contract import ToolError

ENVIRONMENT_NAME = "device"
PLATFORM = "espressif32"
BOARD_LISTING_TIMEOUT_SECONDS = 120

_COMPILER_DIAGNOSTIC = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<column>\d+): (?P<severity>fatal error|error|warning): (?P<message>.*)$"
)
_LINKER_ERROR = re.compile(r"(?P<message>(undefined reference to|multiple definition of) .*)$")
_MEMORY_USAGE = re.compile(
    r"^(?P<region>RAM|Flash):\s+\[.*?\]\s+(?P<percent>[\d.]+)% \(used (?P<used>\d+) bytes from (?P<total>\d+) bytes\)"
)


def run_platformio(arguments: list[str], timeout: float, merge_streams: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "-m", "platformio", "--no-ansi", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_streams else subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            "timeout",
            f"PlatformIO did not finish within {timeout:g}s.",
            "The first build downloads the ESP32 toolchain (several hundred MB) and can take 10+ minutes. "
            "Rerun with a larger --timeout; downloads already completed are kept.",
        ) from None


def list_boards() -> list[dict[str, Any]]:
    listing = run_platformio(["boards", PLATFORM, "--json-output"], BOARD_LISTING_TIMEOUT_SECONDS)
    try:
        boards = json.loads(listing.stdout)
    except ValueError:
        raise ToolError(
            "board_listing_failed",
            f"PlatformIO could not list boards: {listing.stderr.strip()[-500:]}",
            "PlatformIO needs internet access the first time it lists boards. Check connectivity and retry.",
        ) from None
    return [
        {"board": board["id"], "name": board["name"], "chip": board["mcu"].lower(), "flash_bytes": board["rom"]}
        for board in boards
        if board["platform"] == PLATFORM and "arduino" in board["frameworks"]
    ]


def firmware_path(project_dir: Path) -> Path:
    return project_dir / ".pio" / "build" / ENVIRONMENT_NAME / "firmware.bin"


def _relative_to_project(file: str, project_dir: Path) -> str:
    path = Path(file)
    if path.is_absolute() and path.is_relative_to(project_dir):
        return str(path.relative_to(project_dir))
    return file


def parse_diagnostics(build_log: str, project_dir: Path) -> dict[str, list[dict[str, Any]]]:
    diagnostics: dict[str, list[dict[str, Any]]] = {"errors": [], "warnings": []}
    for line in build_log.splitlines():
        if match := _COMPILER_DIAGNOSTIC.match(line):
            diagnostic = {
                "file": _relative_to_project(match["file"], project_dir),
                "line": int(match["line"]),
                "column": int(match["column"]),
                "message": match["message"],
            }
            is_warning = match["severity"] == "warning"
            if is_warning and Path(diagnostic["file"]).is_absolute():
                continue
            bucket = diagnostics["warnings" if is_warning else "errors"]
        elif match := _LINKER_ERROR.search(line):
            diagnostic = {"message": match["message"]}
            bucket = diagnostics["errors"]
        else:
            continue
        if diagnostic not in bucket:
            bucket.append(diagnostic)
    return diagnostics


def parse_memory_usage(build_log: str) -> dict[str, dict[str, Any]]:
    usage = {}
    for line in build_log.splitlines():
        if match := _MEMORY_USAGE.match(line):
            usage[match["region"].lower()] = {
                "used_bytes": int(match["used"]),
                "total_bytes": int(match["total"]),
                "percent": float(match["percent"]),
            }
    return usage
