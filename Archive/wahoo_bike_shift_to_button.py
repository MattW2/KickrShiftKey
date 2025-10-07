#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KICKR BIKE SHIFT -> keyboard injection (short-frame only)

- Scans for devices whose name starts with "KICKR BIKE SHIFT"
- Subscribes to Wahoo notify characteristic
- Parses 3-byte short frames "PPQQRR"
  * PPQQ identifies the button "family"
  * RR: bit7 = press(1)/release(0), low7 = rolling sequence
- On press, inject a keyboard tap using pynput (configurable mapping)
install requirments
    pip install bleak pynput
"""
import asyncio
import sys
from typing import Dict, Tuple

from bleak import BleakClient, BleakScanner  # BLE client/scan  (cross-platform)  # [1](https://bleak.readthedocs.io/en/latest/index.html)[2](https://bleak.readthedocs.io/en/latest/api/index.html)
from pynput.keyboard import Controller, Key  # OS-level key injection (app-focused)  # [4](https://pythonhosted.org/pynput/keyboard.html)

# ---------- CONFIG: BLE UUIDs (Wahoo service/characteristic) ----------
WAHOO_SERVICE_UUID = "a026ee0d-0a7d-4ab3-97fa-f1500f9feb8b"
WAHOO_CHAR_UUID    = "a026e03c-0a7d-4ab3-97fa-f1500f9feb8b"

# ---------- CONFIG: Button families (first 2 bytes of 3-byte short frame) ----------
PREFIX_TO_BUTTON: Dict[str, str] = {
    # Right cluster
    "0001": "Right Up",
    "8000": "Right Down",
    "0008": "Right Steer",
    "0004": "Right Shift Up",
    "0002": "Right Shift Down",
    "4000": "Right Brake",
    # Left cluster
    "0200": "Left Up",
    "0400": "Left Down",
    "2000": "Left Steer",
    "1000": "Left Shift Up",
    "0800": "Left Shift Down",
    "0100": "Left Brake",
}

# ---------- CONFIG: Which key to send for each button name ----------
# Use printable characters (e.g., 'k', ' ') or names: 'ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter','Space'
BUTTON_TO_KEY: Dict[str, str] = {
    # Right
    "Right Up": "7",
    "Right Down": "3",
    "Right Steer": "ArrowRight",
    "Right Shift Up": "i",
    "Right Shift Down": "k",
    "Right Brake": "Space",
    # Left
    "Left Up": "3",
    "Left Down": "4",
    "Left Steer": "ArrowLeft",
    "Left Shift Up": "i",
    "Left Shift Down": "k",
    "Left Brake": "Space",
}

# Trigger only on presses (ignore release) — set to False to also fire on release:
TRIGGER_ON_PRESS_ONLY = True

# ---------- Key translation for common names ----------
# pynput uses Key.up/down/left/right and Key.space, etc.
_NAME_TO_SPECIAL = {
    "ArrowUp": Key.up,
    "ArrowDown": Key.down,
    "ArrowLeft": Key.left,
    "ArrowRight": Key.right,
    "Space": Key.space,
    "Enter": Key.enter,
    "Tab": Key.tab,
    "Esc": Key.esc,
    "Escape": Key.esc,
    "Backspace": Key.backspace,
    "Delete": Key.delete,
    "Home": Key.home,
    "End": Key.end,
    "PageUp": Key.page_up,
    "PageDown": Key.page_down,
}

_keyboard = Controller()

def _send_key_tap(key_name: str) -> None:
    """Tap a key using pynput (keydown+keyup)."""
    if not key_name:
        return
    key = _NAME_TO_SPECIAL.get(key_name, key_name)  # fall back to literal (e.g., 'k')
    _keyboard.press(key)
    _keyboard.release(key)

# ---------- Short-frame parsing & de-dup ----------
# Remember last processed sequence for (prefix, type) so we don't double-handle repeats.
_last_seq: Dict[Tuple[str, str], int] = {}

def _handle_short_frame(payload: bytes):
    """
    Short frame is exactly 3 bytes: [P, Q, R]
    - prefix = '%02X%02X' % (P, Q)
    - R: bit7=1=>press, 0=>release; low7=sequence
    """
    if len(payload) != 3:
        return  # ignore long frames
    p, q, r = payload[0], payload[1], payload[2]
    prefix = f"{p:02X}{q:02X}"
    if prefix not in PREFIX_TO_BUTTON:
        return

    pressed = (r & 0x80) != 0
    seq = r & 0x7F
    ev_type = "press" if pressed else "release"

    # de-dup
    key = (prefix, ev_type)
    if _last_seq.get(key) == seq:
        return
    _last_seq[key] = seq

    button_name = PREFIX_TO_BUTTON[prefix]
    should_fire = pressed or (not TRIGGER_ON_PRESS_ONLY)
    print(f"[BLE] {button_name} {ev_type} seq={seq} (prefix={prefix})")

    if should_fire:
        key_name = BUTTON_TO_KEY.get(button_name)
        if key_name:
            _send_key_tap(key_name)

# ---------- BLE scanning, connection, notifications ----------
async def _find_device_by_name_prefix(prefix: str, timeout: float = 10.0):
    print(f"Scanning for devices starting with '{prefix}' …")
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if d.name and d.name.startswith(prefix):
            print(f"Found: {d.name} ({d.address})")
            return d
    return None

def _notification_handler(_char, data: bytearray):
    # data is a bytearray; we only consume 3-byte short frames
    _handle_short_frame(bytes(data))

async def main():
    NAME_PREFIX = "KICKR BIKE SHIFT"  # wildcard: accepts any suffix
    dev = await _find_device_by_name_prefix(NAME_PREFIX, timeout=12.0)
    if not dev:
        print("No matching device found. Ensure the bike is on and advertising.")
        return

    async with BleakClient(dev) as client:
        if not client.is_connected:
            print("Failed to connect.")
            return

        print("Connected. Subscribing to notifications …")
        await client.start_notify(WAHOO_CHAR_UUID, _notification_handler)  # [3](https://github.com/hbldh/bleak/blob/develop/examples/enable_notifications.py)
        print("Listening. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await client.stop_notify(WAHOO_CHAR_UUID)

if __name__ == "__main__":
    # macOS: grant Terminal/Python Accessibility permission to allow key injection. [7](https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac)
    try:
        asyncio.run(main())
    except RuntimeError:
        # For some environments (older Python/Windows event loop policy), fallback:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())