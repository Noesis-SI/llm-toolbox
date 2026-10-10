from pathlib import Path

from esp_build.platformio import parse_diagnostics, parse_memory_usage

PROJECT_DIR = Path("/work/blinker")

BUILD_LOG = """\
Compiling .pio/build/device/src/main.cpp.o
src/main.cpp: In function 'void appSetup()':
src/main.cpp:6:11: error: 'LED' was not declared in this scope
src/main.cpp:6:11: error: 'LED' was not declared in this scope
/work/blinker/src/sensor.cpp:3:10: fatal error: Missing.h: No such file or directory
src/main.cpp:9:7: warning: unused variable 'count' [-Wunused-variable]
/home/user/.platformio/packages/framework/cores/esp32/hal.c:153:9: warning: 'return' with no value
ld: main.cpp:(.text+0x4): undefined reference to `appLoop()'
RAM:   [=         ]  13.1% (used 42868 bytes from 327680 bytes)
Flash: [======    ]  58.7% (used 769654 bytes from 1310720 bytes)
"""


def test_extracts_deduplicated_errors_with_project_relative_paths():
    assert parse_diagnostics(BUILD_LOG, PROJECT_DIR)["errors"] == [
        {"file": "src/main.cpp", "line": 6, "column": 11, "message": "'LED' was not declared in this scope"},
        {"file": "src/sensor.cpp", "line": 3, "column": 10, "message": "Missing.h: No such file or directory"},
        {"message": "undefined reference to `appLoop()'"},
    ]


def test_reports_only_warnings_from_project_files():
    assert parse_diagnostics(BUILD_LOG, PROJECT_DIR)["warnings"] == [
        {"file": "src/main.cpp", "line": 9, "column": 7, "message": "unused variable 'count' [-Wunused-variable]"}
    ]


def test_extracts_memory_usage():
    assert parse_memory_usage(BUILD_LOG) == {
        "ram": {"used_bytes": 42868, "total_bytes": 327680, "percent": 13.1},
        "flash": {"used_bytes": 769654, "total_bytes": 1310720, "percent": 58.7},
    }
