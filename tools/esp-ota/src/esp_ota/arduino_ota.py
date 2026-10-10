"""Client for the ArduinoOTA ("espota") protocol built into the ESP32 and ESP8266 Arduino cores.

The host sends a UDP invitation, optionally answers an authentication challenge, then the
device connects back to the host over TCP and pulls the image.
"""

import hashlib
import os
import secrets
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from tb_contract import ToolError, progress

MDNS_SERVICE_TYPE = "_arduino._tcp.local."
DEFAULT_PORT = 3232
TARGET_COMMANDS = {"firmware": 0, "filesystem": 100}
AUTH_COMMAND = 200

UPLOAD_CHUNK_BYTES = 1024
INVITATION_RESEND_SECONDS = 5
AUTH_REPLY_TIMEOUT_SECONDS = 10
DEVICE_CONNECT_TIMEOUT_SECONDS = 10
CHUNK_ACK_TIMEOUT_SECONDS = 10
PBKDF2_ITERATIONS = 10000
LEGACY_MD5_NONCE_LENGTH = 32
PBKDF2_NONCE_LENGTH = 64


@dataclass(frozen=True)
class Image:
    path: Path
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @cached_property
    def md5(self) -> str:
        return hashlib.md5(self.content).hexdigest()


def _legacy_md5_response(password: str, nonce: str) -> tuple[str, str]:
    client_nonce = secrets.token_hex(16)
    password_hash = hashlib.md5(password.encode()).hexdigest()
    return client_nonce, hashlib.md5(f"{password_hash}:{nonce}:{client_nonce}".encode()).hexdigest()


def _pbkdf2_response(password_hash: str, nonce: str) -> tuple[str, str]:
    client_nonce = secrets.token_hex(32)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256", password_hash.encode(), f"{nonce}:{client_nonce}".encode(), PBKDF2_ITERATIONS
    ).hex()
    return client_nonce, hashlib.sha256(f"{derived_key}:{nonce}:{client_nonce}".encode()).hexdigest()


def _pbkdf2_sha256_password_response(password: str, nonce: str) -> tuple[str, str]:
    return _pbkdf2_response(hashlib.sha256(password.encode()).hexdigest(), nonce)


def _pbkdf2_md5_password_response(password: str, nonce: str) -> tuple[str, str]:
    return _pbkdf2_response(hashlib.md5(password.encode()).hexdigest(), nonce)


AuthScheme = tuple[str, Callable[[str, str], tuple[str, str]]]

# Devices on core >= 3.3.1 may still hold an MD5 password hash set by older sketch code, so both are tried.
AUTH_SCHEMES_BY_NONCE_LENGTH: dict[int, list[AuthScheme]] = {
    LEGACY_MD5_NONCE_LENGTH: [("md5", _legacy_md5_response)],
    PBKDF2_NONCE_LENGTH: [
        ("pbkdf2-sha256", _pbkdf2_sha256_password_response),
        ("pbkdf2-sha256-md5-password", _pbkdf2_md5_password_response),
    ],
}


def _request(
    udp: socket.socket, address: tuple[str, int], message: str, timeout: float, resend_every: float | None
) -> str | None:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        udp.sendto(message.encode(), address)
        udp.settimeout(min(resend_every, remaining) if resend_every else remaining)
        try:
            reply, _ = udp.recvfrom(128)
            return reply.decode(errors="replace").strip()
        except TimeoutError:
            if not resend_every:
                return None
    return None


def _request_challenge(udp: socket.socket, address: tuple[str, int], invitation: str, timeout: float) -> str | None:
    """Returns the device's nonce, or None when it accepts the invitation without authentication."""
    reply = _request(udp, address, invitation, timeout, INVITATION_RESEND_SECONDS)
    if reply is None:
        raise ToolError(
            "no_response",
            f"No reply from {address[0]} on UDP port {address[1]} within {timeout:g}s.",
            "Check that the device is powered, on this network, and that its sketch calls ArduinoOTA.handle() "
            "in loop(). ESP32 listens on port 3232 and ESP8266 on 8266 by default; pass --port if it differs. "
            "Run `tb esp-ota discover` to find the device's current IP and port.",
        )
    if reply == "OK":
        return None
    parts = reply.split()
    if len(parts) == 2 and parts[0] == "AUTH":
        return parts[1]
    raise ToolError(
        "unexpected_reply",
        f"The device answered the invitation with {reply!r}.",
        "The device rejected the update before transfer. For --target filesystem, check that the sketch "
        "supports filesystem updates; otherwise confirm the port really is an ArduinoOTA listener.",
    )


def _negotiate(
    udp: socket.socket, address: tuple[str, int], invitation: str, password: str | None, invite_timeout: float
) -> str:
    nonce = _request_challenge(udp, address, invitation, invite_timeout)
    if nonce is None:
        return "none"
    if not password:
        raise ToolError(
            "auth_required",
            "The device requires an OTA password and none was given.",
            "Pass --password or set the ESP_OTA_PASSWORD environment variable. The password is the one given "
            "to ArduinoOTA.setPassword() in the sketch currently running on the device.",
        )
    schemes = AUTH_SCHEMES_BY_NONCE_LENGTH.get(len(nonce))
    if schemes is None:
        raise ToolError(
            "unsupported_auth",
            f"The device sent a {len(nonce)}-character nonce; only 32 (MD5) and 64 (SHA256) are understood.",
            "The device runs an ArduinoOTA version this tool does not support yet.",
        )

    for attempt, (scheme_name, build_response) in enumerate(schemes):
        if attempt > 0:
            nonce = _request_challenge(udp, address, invitation, invite_timeout)
            if nonce is None:
                return "none"
        client_nonce, response = build_response(password, nonce)
        reply = _request(udp, address, f"{AUTH_COMMAND} {client_nonce} {response}\n", AUTH_REPLY_TIMEOUT_SECONDS, None)
        if reply == "OK":
            return scheme_name

    raise ToolError(
        "auth_failed",
        "The device rejected the OTA password.",
        "Use the password compiled into the sketch currently running on the device, not the one in the "
        "firmware being uploaded.",
    )


def _read_acknowledgement(connection: socket.socket) -> bool:
    """Returns True once the device reports the whole image as written and verified."""
    reply = connection.recv(64)
    if not reply:
        raise ToolError(
            "connection_closed",
            "The device closed the connection before confirming the update.",
            "The update state is unknown. Wait ~15s, run `tb esp-ota discover` to see whether the device is "
            "up, then retry the flash. A device that keeps dropping mid-transfer usually has weak WiFi signal "
            "or too little free flash for the image.",
        )
    text = reply.decode(errors="replace").strip()
    if "OK" in text:
        return True
    if any(character.isalpha() for character in text):
        raise ToolError(
            "device_error",
            f"The device aborted the update: {text!r}.",
            "The device kept its previous firmware. Common causes: image larger than the OTA partition, image "
            "built for a different chip, or a partition scheme without OTA slots.",
        )
    return False


def _upload(connection: socket.socket, image: Image, result_timeout: float) -> None:
    connection.settimeout(CHUNK_ACK_TIMEOUT_SECONDS)
    confirmed = False
    next_reported_percent = 10
    for offset in range(0, image.size, UPLOAD_CHUNK_BYTES):
        connection.sendall(image.content[offset : offset + UPLOAD_CHUNK_BYTES])
        confirmed = _read_acknowledgement(connection)
        percent = min(100, (offset + UPLOAD_CHUNK_BYTES) * 100 // image.size)
        if percent >= next_reported_percent:
            progress(f"uploaded {percent}%")
            next_reported_percent = percent - percent % 10 + 10

    connection.settimeout(result_timeout)
    while not confirmed:
        confirmed = _read_acknowledgement(connection)


def flash_image(
    device_ip: str,
    port: int,
    image: Image,
    target: str,
    password: str | None,
    invite_timeout: float,
    result_timeout: float = 60,
) -> str:
    """Uploads the image and returns the name of the authentication scheme the device used."""
    address = (device_ip, port)
    try:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        ):
            listener.bind(("0.0.0.0", 0))
            listener.listen(1)
            local_port = listener.getsockname()[1]

            invitation = f"{TARGET_COMMANDS[target]} {local_port} {image.size} {image.md5}\n"
            progress(f"inviting {device_ip}:{port}")
            auth_scheme = _negotiate(udp, address, invitation, password, invite_timeout)

            listener.settimeout(DEVICE_CONNECT_TIMEOUT_SECONDS)
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                raise ToolError(
                    "device_did_not_connect",
                    f"The device accepted the invitation but never connected back to this machine on TCP port "
                    f"{local_port}.",
                    "The device kept its firmware. It must be able to open a TCP connection to this machine, "
                    "so a host firewall is the usual cause; changing firewall rules needs the user. Ask them "
                    f"to allow incoming connections for {os.path.realpath(sys.executable)} (on macOS: accept "
                    "the 'accept incoming network connections' dialog, or System Settings > Network > Firewall "
                    "> Options), then retry. Otherwise check that device and host share a subnet without "
                    "client isolation.",
                ) from None

            with connection:
                progress(f"uploading {image.size} bytes")
                _upload(connection, image, result_timeout)
            return auth_scheme
    except OSError as error:
        raise ToolError(
            "network_error",
            f"Network failure while talking to {device_ip}: {error}.",
            "The device may have kept its previous firmware or may be rebooting. Wait ~15s, run "
            "`tb esp-ota discover`, then retry.",
        ) from error
