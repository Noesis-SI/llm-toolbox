import json
import socket
import threading

import pytest
from tb_contract import ToolError

from esp_logs.device_log import LogCollector, Snapshot, fetch_snapshot

DEVICE_BUFFER_BYTES = 32


def device_snapshot(everything_logged: bytes) -> Snapshot:
    return Snapshot({"log_end": len(everything_logged)}, everything_logged[-DEVICE_BUFFER_BYTES:])


def test_first_read_returns_all_complete_lines_and_holds_back_the_unfinished_one():
    collector = LogCollector(cursor=None)
    collector.add(device_snapshot(b"boot\nready\npart"))
    assert collector.lines() == ["boot", "ready"]
    assert collector.cursor == len(b"boot\nready\n")


def test_since_cursor_returns_only_newer_lines():
    first = LogCollector(cursor=None)
    first.add(device_snapshot(b"boot\nrea"))
    second = LogCollector(cursor=first.cursor)
    second.add(device_snapshot(b"boot\nready\nloop 1\n"))
    assert second.lines() == ["ready", "loop 1"]
    assert second.missed_bytes == 0


def test_polling_accumulates_without_duplicates():
    collector = LogCollector(cursor=None)
    collector.add(device_snapshot(b"a\n"))
    collector.add(device_snapshot(b"a\n"))
    collector.add(device_snapshot(b"a\nb\n"))
    assert collector.lines() == ["a", "b"]


def test_overwritten_output_is_counted_and_the_cut_line_dropped():
    history = b"first line\n" + b"x" * 40 + b"\nlast\n"
    collector = LogCollector(cursor=len(b"first line\n"))
    collector.add(device_snapshot(history))
    assert collector.lines() == ["last"]
    assert collector.missed_bytes == len(history) - DEVICE_BUFFER_BYTES - len(b"first line\n")
    assert collector.cursor == len(history)


def test_wrapped_buffer_on_first_read_drops_the_cut_line():
    collector = LogCollector(cursor=None)
    collector.add(device_snapshot(b"y" * 40 + b"\nkept\n"))
    assert collector.lines() == ["kept"]


def test_power_loss_restarts_the_log():
    collector = LogCollector(cursor=5000)
    collector.add(device_snapshot(b"boot\n"))
    assert collector.log_restarted
    assert collector.lines() == ["boot"]
    assert collector.cursor == len(b"boot\n")


def serve_once(response: bytes) -> int:
    server = socket.create_server(("127.0.0.1", 0))

    def respond() -> None:
        with server, server.accept()[0] as connection:
            connection.sendall(response)

    threading.Thread(target=respond, daemon=True).start()
    return server.getsockname()[1]


def test_fetches_status_and_log_from_device():
    status = {"hostname": "blinker", "uptime_ms": 1200, "log_end": 9}
    port = serve_once(json.dumps(status).encode() + b"\nboot\nrun\n")
    snapshot = fetch_snapshot("127.0.0.1", port, timeout=2)
    assert snapshot.status == status
    assert snapshot.log == b"boot\nrun\n"
    assert snapshot.start == 0


def test_rejects_servers_that_are_not_an_ota_app_log_server():
    port = serve_once(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    with pytest.raises(ToolError) as raised:
        fetch_snapshot("127.0.0.1", port, timeout=2)
    assert raised.value.code == "malformed_response"


def test_reports_missing_log_server():
    with socket.create_server(("127.0.0.1", 0)) as closed_server:
        port = closed_server.getsockname()[1]
    with pytest.raises(ToolError) as raised:
        fetch_snapshot("127.0.0.1", port, timeout=2)
    assert raised.value.code == "log_server_not_running"
