# fichero/scanner_proxy.py
import asyncio
from aioesphomeapi import APIClient


async def find_printer_via_proxy(
    proxy_host: str,
    proxy_key: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """
    Subscribe to BLE advertisements forwarded by the ESPHome proxy and
    return the MAC address of the first Fichero / D11s printer found.
    """
    PRINTER_NAMES = {"FICHERO", "D11S", "AIYIN"}
    found: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    api = APIClient(proxy_host, 6053, password=None, noise_psk=proxy_key)
    await api.connect(login=True)

    def _on_adv(adv):
        name = (adv.name or "").upper()
        if any(n in name for n in PRINTER_NAMES) and not found.done():
            found.set_result(adv.address)

    unsub = await api.subscribe_bluetooth_le_advertisements(_on_adv)

    try:
        return await asyncio.wait_for(found, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        unsub()
        await api.disconnect()
