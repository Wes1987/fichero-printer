"""
ESPHome Bluetooth proxy transport for the Fichero printer.

Replaces the bleak.BleakClient used in printer.py with a remote
connection routed through an ESP32 running the bluetooth_proxy component.

pip install aioesphomeapi
"""

from __future__ import annotations
import asyncio
import logging
from typing import Callable

from aioesphomeapi import APIClient, BluetoothProxyFeature
from aioesphomeapi.model import BluetoothGATTCharacteristic

_LOGGER = logging.getLogger(__name__)

# AiYin / LuckPrinter BLE service & characteristics (D11s)
SERVICE_UUID  = "0000ae30-0000-1000-8000-00805f9b34fb"
WRITE_UUID    = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID   = "0000ae02-0000-1000-8000-00805f9b34fb"


def _mac_to_int(mac: str) -> int:
    """Convert 'AA:BB:CC:DD:EE:FF' to integer address used by aioesphomeapi."""
    return int(mac.replace(":", ""), 16)


class ProxyClient:
    """
    Thin adapter that mimics the bleak.BleakClient interface used in printer.py,
    but routes all BLE traffic through an ESPHome bluetooth_proxy.

    Usage:
        async with ProxyClient(proxy_host, printer_mac) as pc:
            await pc.start_notify(NOTIFY_UUID, handler)
            await pc.write_gatt_char(WRITE_UUID, data)
    """

    def __init__(
        self,
        proxy_host: str,
        printer_mac: str,
        *,
        proxy_port: int = 6053,
        proxy_key: str | None = None,  # base64 noise key from api.encryption
        noise_psk: str | None = None,  # alias; same thing
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._noise_psk = noise_psk or proxy_key
        self._printer_mac = printer_mac
        self._printer_addr = _mac_to_int(printer_mac)

        self._api: APIClient | None = None
        self._handles: dict[str, int] = {}  # uuid -> GATT handle

    # ------------------------------------------------------------------ #
    # Context manager                                                       #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "ProxyClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                  #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        self._api = APIClient(
            self._proxy_host,
            self._proxy_port,
            password=None,
            noise_psk=self._noise_psk,
        )
        await self._api.connect(login=True)
        _LOGGER.debug("Connected to ESPHome proxy at %s", self._proxy_host)

        # Verify the proxy supports active connections
        device_info = await self._api.device_info()
        if not (device_info.bluetooth_proxy_feature_flags
                & BluetoothProxyFeature.ACTIVE_CONNECTIONS):
            raise RuntimeError(
                "ESPHome proxy does not support active BLE connections. "
                "Add 'bluetooth_proxy: active: true' to your proxy YAML."
            )

        # Connect to the printer via the proxy
        await self._api.bluetooth_device_connect(
            self._printer_addr,
            has_cache=False,
            address_type=0,          # 0 = public address
            wait_for_timeout=10.0,
        )
        _LOGGER.debug("BLE connected to printer %s", self._printer_mac)

        # Discover services and build uuid→handle map
        await self._resolve_handles()

    async def disconnect(self) -> None:
        if self._api is None:
            return
        try:
            await self._api.bluetooth_device_disconnect(self._printer_addr)
        except Exception:
            pass
        try:
            await self._api.disconnect()
        except Exception:
            pass
        self._api = None
        self._handles.clear()

    # ------------------------------------------------------------------ #
    # GATT operations (same signature as bleak.BleakClient)               #
    # ------------------------------------------------------------------ #

    async def write_gatt_char(
        self,
        char_specifier: str,
        data: bytes | bytearray,
        response: bool = False,
    ) -> None:
        handle = self._handle(char_specifier)
        await self._api.bluetooth_gatt_write(
            self._printer_addr,
            handle,
            bytes(data),
            response=response,
        )

    async def start_notify(
        self,
        char_specifier: str,
        callback: Callable[[int, bytearray], None],
    ) -> None:
        handle = self._handle(char_specifier)

        def _on_notify(address: int, h: int, data: bytes) -> None:
            if address == self._printer_addr and h == handle:
                callback(h, bytearray(data))

        await self._api.bluetooth_gatt_start_notify(
            self._printer_addr,
            handle,
            _on_notify,
        )

    async def stop_notify(self, char_specifier: str) -> None:
        handle = self._handle(char_specifier)
        await self._api.bluetooth_gatt_stop_notify(self._printer_addr, handle)

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        handle = self._handle(char_specifier)
        data = await self._api.bluetooth_gatt_read(self._printer_addr, handle)
        return bytearray(data)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _resolve_handles(self) -> None:
        """Walk the GATT service tree and build a uuid → handle lookup."""
        services = await self._api.bluetooth_gatt_get_services(self._printer_addr)
        for service in services.services:
            for char in service.characteristics:
                self._handles[char.uuid.lower()] = char.handle
        _LOGGER.debug("Resolved %d GATT handles", len(self._handles))

    def _handle(self, uuid: str) -> int:
        key = uuid.lower()
        if key not in self._handles:
            raise KeyError(
                f"Characteristic {uuid} not found on printer. "
                f"Known UUIDs: {list(self._handles)}"
            )
        return self._handles[key]
