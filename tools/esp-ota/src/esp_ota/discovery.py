import time
from typing import Any

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

from esp_ota.arduino_ota import MDNS_SERVICE_TYPE

SERVICE_INFO_TIMEOUT_MILLISECONDS = 1500


class _ServiceNameCollector(ServiceListener):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def _text_property(properties: dict[bytes, bytes | None], key: str) -> str | None:
    value = properties.get(key.encode())
    return value.decode(errors="replace") if value else None


def discover_arduino_ota_devices(listen_seconds: float) -> list[dict[str, Any]]:
    zeroconf = Zeroconf()
    try:
        collector = _ServiceNameCollector()
        ServiceBrowser(zeroconf, MDNS_SERVICE_TYPE, collector)
        time.sleep(listen_seconds)

        devices = []
        for service_name in sorted(collector.names):
            info = zeroconf.get_service_info(MDNS_SERVICE_TYPE, service_name, SERVICE_INFO_TIMEOUT_MILLISECONDS)
            if info is None:
                continue
            addresses = info.parsed_addresses(IPVersion.V4Only)
            devices.append(
                {
                    "name": service_name.removesuffix("." + MDNS_SERVICE_TYPE),
                    "hostname": (info.server or "").rstrip("."),
                    "ip": addresses[0] if addresses else None,
                    "port": info.port,
                    "board": _text_property(info.properties, "board"),
                    "auth_required": _text_property(info.properties, "auth_upload") == "yes",
                    "protocol": "arduino",
                }
            )
        return devices
    finally:
        zeroconf.close()
