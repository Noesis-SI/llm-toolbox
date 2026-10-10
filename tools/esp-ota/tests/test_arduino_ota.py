import hashlib
import secrets
import socket
import threading
from pathlib import Path

import pytest
from tb_contract import ToolError

from esp_ota.arduino_ota import AUTH_COMMAND, PBKDF2_ITERATIONS, Image, flash_image

IMAGE = Image(Path("firmware.bin"), b"\xe9" + secrets.token_bytes(5000))


class FakeDevice:
    """Speaks the device side of the ArduinoOTA protocol on localhost."""

    def __init__(self, auth: str | None = None, stored_password_hash: str | None = None, final_reply: bytes = b"OK"):
        self.auth = auth
        self.stored_password_hash = stored_password_hash
        self.final_reply = final_reply
        self.received = b""
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(5)
        self.port = self.udp.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _expected_response(self, nonce: str, client_nonce: str) -> str:
        if self.auth == "md5":
            return hashlib.md5(f"{self.stored_password_hash}:{nonce}:{client_nonce}".encode()).hexdigest()
        derived_key = hashlib.pbkdf2_hmac(
            "sha256", self.stored_password_hash.encode(), f"{nonce}:{client_nonce}".encode(), PBKDF2_ITERATIONS
        ).hex()
        return hashlib.sha256(f"{derived_key}:{nonce}:{client_nonce}".encode()).hexdigest()

    def _serve(self) -> None:
        try:
            while True:
                invitation, host = self.udp.recvfrom(256)
                _, host_port, size, md5 = invitation.decode().split()
                if self.auth:
                    nonce = secrets.token_hex(16 if self.auth == "md5" else 32)
                    self.udp.sendto(f"AUTH {nonce}".encode(), host)
                    command, client_nonce, response = self.udp.recvfrom(256)[0].decode().split()
                    assert int(command) == AUTH_COMMAND
                    if response != self._expected_response(nonce, client_nonce):
                        self.udp.sendto(b"Authentication Failed", host)
                        continue
                self.udp.sendto(b"OK", host)
                break

            with socket.create_connection((host[0], int(host_port)), timeout=5) as tcp:
                while len(self.received) < int(size):
                    chunk = tcp.recv(1460)
                    self.received += chunk
                    tcp.sendall(str(len(chunk)).encode())
                assert hashlib.md5(self.received).hexdigest() == md5
                tcp.sendall(self.final_reply)
        except TimeoutError:
            pass
        finally:
            self.udp.close()


def flash(device: FakeDevice, password: str | None = None) -> str:
    scheme = flash_image("127.0.0.1", device.port, IMAGE, "firmware", password, invite_timeout=3, result_timeout=3)
    device.thread.join(timeout=5)
    return scheme


def test_flashes_device_without_password():
    device = FakeDevice()
    assert flash(device) == "none"
    assert device.received == IMAGE.content


def test_authenticates_with_pbkdf2_sha256():
    device = FakeDevice(auth="pbkdf2", stored_password_hash=hashlib.sha256(b"hunter2").hexdigest())
    assert flash(device, "hunter2") == "pbkdf2-sha256"
    assert device.received == IMAGE.content


def test_falls_back_to_md5_password_hash_on_pbkdf2_devices():
    device = FakeDevice(auth="pbkdf2", stored_password_hash=hashlib.md5(b"hunter2").hexdigest())
    assert flash(device, "hunter2") == "pbkdf2-sha256-md5-password"
    assert device.received == IMAGE.content


def test_authenticates_with_legacy_md5():
    device = FakeDevice(auth="md5", stored_password_hash=hashlib.md5(b"hunter2").hexdigest())
    assert flash(device, "hunter2") == "md5"
    assert device.received == IMAGE.content


@pytest.mark.parametrize(
    ("device_options", "password", "expected_code"),
    [
        ({"auth": "pbkdf2", "stored_password_hash": hashlib.sha256(b"hunter2").hexdigest()}, "wrong", "auth_failed"),
        ({"auth": "pbkdf2", "stored_password_hash": hashlib.sha256(b"hunter2").hexdigest()}, None, "auth_required"),
        ({"final_reply": b"Update error: MD5 Check Failed"}, None, "device_error"),
    ],
)
def test_reports_failures_with_specific_codes(device_options, password, expected_code):
    device = FakeDevice(**device_options)
    with pytest.raises(ToolError) as raised:
        flash(device, password)
    assert raised.value.code == expected_code
    assert raised.value.hint


def test_reports_no_response_when_nothing_listens():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as unused:
        unused.bind(("127.0.0.1", 0))
        silent_port = unused.getsockname()[1]
        with pytest.raises(ToolError) as raised:
            flash_image("127.0.0.1", silent_port, IMAGE, "firmware", None, invite_timeout=1)
    assert raised.value.code == "no_response"
