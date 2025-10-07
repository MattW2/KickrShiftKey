#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KICKR BIKE SHIFT — GUI (short-frames only)
- Connect / Disconnect buttons
- Status indicator
- Debug checkbox (show/hide "other frames")
- Short-frame-only decoding
- Per-button behavior: Tap on press OR Hold until release
- Auto-reconnect if device drops (unless user clicked Disconnect)
- Robust cleanup: stop_notify + disconnect + release held keys

Keyboard injection backend: pydirectinput (replacing pynput)
"""

import asyncio
import threading
import queue
import sys
import atexit
from typing import Dict, Tuple, Optional

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from bleak import BleakClient, BleakScanner, BleakError

# --- Key injection via pydirectinput ---
import pydirectinput

# Make key actions immediate (no built-in delay)
pydirectinput.PAUSE = 0.0
# Optional: disable failsafe if you occasionally move the mouse to 0,0
# pydirectinput.FAILSAFE = False

# ----------------------------
# CONFIGURATION
# ----------------------------

DEVICE_NAME_PREFIX = "KICKR BIKE SHIFT"  # wildcard: matches "KICKR BIKE SHIFT *"

# Wahoo service / characteristic (from your traces)
WAHOO_SERVICE_UUID = "a026ee0d-0a7d-4ab3-97fa-f1500f9feb8b"
WAHOO_CHAR_UUID    = "a026e03c-0a7d-4ab3-97fa-f1500f9feb8b"

# Short-frame families (first 2 bytes). Last byte: MSB=press, low7=sequence.
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

# Which key to send for each button (remove or set None to disable)
# Use printable chars ('k', ' ') or names below (e.g. 'ArrowUp','ArrowDown','Enter','Space').
BUTTON_TO_KEY: Dict[str, Optional[str]] = {
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

# Per-button behavior — "tap" = tap on press; "hold" = hold down until release.
BUTTON_BEHAVIOR: Dict[str, str] = {
    "Right Steer": "hold",
    "Left Steer":  "hold",
    # Uncomment to make brakes hold too:
    # "Right Brake": "hold",
    # "Left Brake":  "hold",
}

# Scan timeout (seconds)
SCAN_TIMEOUT_S = 12.0

# Reconnect wait between attempts (seconds)
RECONNECT_DELAY_S = 1.5

# ----------------------------
# Key name mapping to pydirectinput
# ----------------------------
# pydirectinput expects key strings like "right", "left", "space", "enter", "pageup", "pagedown", etc.
KEY_NAME_MAP = {
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "Space": "space",
    "Enter": "enter",
    "Tab": "tab",
    "Esc": "esc",
    "Escape": "esc",
    "Backspace": "backspace",
    "Delete": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
}

def _to_pydi_key(key_name: str) -> Optional[str]:
    """Convert our logical key name to a pydirectinput key string."""
    if not key_name:
        return None
    # Special names
    mapped = KEY_NAME_MAP.get(key_name)
    if mapped:
        return mapped
    # Printable single characters or strings like 'i', 'k', '3'
    # pydirectinput accepts these as-is (lowercase recommended) 
    # Note: keep case for letters; pydirectinput is case-insensitive for alpha keys.
    return key_name

def send_key_tap(key_name: str):
    """Tap a key (keydown+keyup) via pydirectinput."""
    k = _to_pydi_key(key_name)
    if not k:
        return
    pydirectinput.press(k)

def send_key_down(key_name: str):
    """Press and hold a key (no release) via pydirectinput."""
    k = _to_pydi_key(key_name)
    if not k:
        return
    pydirectinput.keyDown(k)

def send_key_up(key_name: str):
    """Release a previously held key via pydirectinput."""
    k = _to_pydi_key(key_name)
    if not k:
        return
    pydirectinput.keyUp(k)

# Track currently held keys per button to prevent duplicates and to clean up on disconnect.
_HELD_BY_BUTTON: Dict[str, str] = {}  # button_name -> key_name

def hold_if_needed(button_name: str, key_name: str):
    if button_name not in _HELD_BY_BUTTON:
        send_key_down(key_name)
        _HELD_BY_BUTTON[button_name] = key_name

def release_if_held(button_name: str):
    key_name = _HELD_BY_BUTTON.pop(button_name, None)
    if key_name:
        send_key_up(key_name)

def release_all_held_keys():
    for btn, key_name in list(_HELD_BY_BUTTON.items()):
        try:
            send_key_up(key_name)
        except Exception:
            pass
        _HELD_BY_BUTTON.pop(btn, None)

# ----------------------------
# BLE parser (short frames only)
# ----------------------------

# De-dup: remember last sequence handled per (prefix, type)
_last_seq: Dict[Tuple[str, str], int] = {}

def parse_short_frame(payload: bytes):
    """
    Short frame is exactly 3 bytes [P, Q, R]
    - prefix = '%02X%02X' % (P, Q)
    - R: bit7=1 => press, 0 => release; low7 => rolling sequence
    Returns dict or None
    """
    if len(payload) != 3:
        return None
    p, q, r = payload[0], payload[1], payload[2]
    prefix = f"{p:02X}{q:02X}"
    if prefix not in PREFIX_TO_BUTTON:
        return None

    pressed = (r & 0x80) != 0
    seq = r & 0x7F
    ev_type = "press" if pressed else "release"
    return {
        "prefix": prefix,
        "name":   PREFIX_TO_BUTTON[prefix],
        "type":   ev_type,
        "seq":    seq,
        "rrHex":  f"{r:02X}",
    }

def already_handled(prefix: str, ev_type: str, seq: int) -> bool:
    key = (prefix, ev_type)
    if _last_seq.get(key) == seq:
        return True
    _last_seq[key] = seq
    return False

# ----------------------------
# BLE scanning helper
# ----------------------------

async def find_device_by_prefix(prefix: str, timeout: float):
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if d.name and d.name.startswith(prefix):
            return d
    return None

# ----------------------------
# GUI App with auto-reconnect & robust cleanup
# ----------------------------

class BikeKeyGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("KICKR BIKE SHIFT — Button → Key (Short-frames only)")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Comm queue from BLE thread -> GUI
        self.msgq: queue.Queue = queue.Queue()
        self.ble_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # BLE client handle & state
        self.client: Optional[BleakClient] = None
        self._notify_on: bool = False  # track if start_notify succeeded

        self._build_ui()
        self._drain_queue()  # start polling the message queue

        # Ensure best-effort cleanup on interpreter shutdown
        atexit.register(self._atexit_cleanup)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=(6, 4))

        self.btn_connect = ttk.Button(top, text="Connect", command=self.on_connect_clicked)
        self.btn_connect.pack(side="left", padx=(6, 4))

        self.btn_disconnect = ttk.Button(top, text="Disconnect", command=self.on_disconnect_clicked, state="disabled")
        self.btn_disconnect.pack(side="left")

        # Status indicator
        self.dot = tk.Label(top, text="●", font=("Segoe UI", 14), fg="gray")
        self.dot.pack(side="right", padx=(4, 8))
        self.lbl_status = ttk.Label(top, text="Idle")
        self.lbl_status.pack(side="right")

        # Debug checkbox
        mid = ttk.Frame(self.root)
        mid.pack(fill="x", padx=6)
        self.debug_var = tk.BooleanVar(value=False)
        self.chk_debug = ttk.Checkbutton(mid, text="Debug output", variable=self.debug_var)
        self.chk_debug.pack(side="left")

        # Debug log
        self.log = ScrolledText(self.root, height=22, width=100, state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        # Hint
        hint = ttk.Label(
            self.root,
            text="Keys go to the current foreground app. Using pydirectinput for tap/hold."
        )
        hint.pack(padx=6, pady=(0, 8))

    def set_status(self, text: str, color: str = "gray"):
        self.lbl_status.config(text=text)
        self.dot.config(fg=color)

    def append_log(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- Button handlers ----------
    def on_connect_clicked(self):
        if self.ble_thread and self.ble_thread.is_alive():
            return  # already running
        self.stop_event.clear()
        self.set_status("Scanning…", "orange")
        self.append_log("[UI] Scanning for device…")
        self.btn_connect.config(state="disabled")
        self.btn_disconnect.config(state="normal")

        self.ble_thread = threading.Thread(target=self._ble_worker, daemon=True)
        self.ble_thread.start()

    def on_disconnect_clicked(self):
        self.append_log("[UI] Disconnect requested.")
        self.stop_event.set()

    # ---------- Window close & atexit ----------
    def on_close(self):
        self.append_log("[UI] Closing window — requesting cleanup…")
        self._graceful_shutdown()
        self.root.destroy()

    def _atexit_cleanup(self):
        # Best-effort cleanup if process exits without window close callback
        self._graceful_shutdown()

    def _graceful_shutdown(self):
        try:
            self.stop_event.set()
            if self.ble_thread and self.ble_thread.is_alive():
                self.append_log("[UI] Waiting for BLE thread to stop…")
                self.ble_thread.join(timeout=5.0)
        finally:
            # Always release any held keys on shutdown
            release_all_held_keys()

    # ---------- Background worker ----------
    def _ble_worker(self):
        try:
            asyncio.run(self._ble_main())
        except Exception as e:
            self.msgq.put(("log", f"[BLE] Error: {e}"))
            self.msgq.put(("status", ("Error", "red")))
            self.msgq.put(("enable_connect", True))

    async def _ble_main(self):
        """
        Auto-reconnect loop:
        - find device
        - connect, subscribe, listen until disconnected or user pressed Disconnect
        - cleanup
        - if user did not press Disconnect, wait and try again
        """
        dev = None

        while not self.stop_event.is_set():
            # Find or refind device
            if dev is None:
                self.set_status("Scanning…", "orange")
                self.msgq.put(("log", "[BLE] Scanning..."))
                dev = await find_device_by_prefix(DEVICE_NAME_PREFIX, SCAN_TIMEOUT_S)
                if dev is None:
                    self.msgq.put(("log", "[BLE] Device not found. Is the bike on / advertising?"))
                    self.msgq.put(("status", ("Not found", "red")))
                    self.msgq.put(("enable_connect", True))
                    return

            self.msgq.put(("log", f"[BLE] Found {dev.name} ({dev.address}) — connecting…"))

            client: Optional[BleakClient] = None
            self._notify_on = False

            # Local state for this connection try
            disconnected_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            # Disconnect callback (register via constructor)
            def _on_disc(_client):
                loop.call_soon_threadsafe(disconnected_event.set)

            async def notification_handler(_char, data: bytearray):
                payload = bytes(data)
                evt = parse_short_frame(payload)
                if not evt:
                    # Only show "other frames" when Debug output is checked
                    if self.debug_var.get():
                        self.msgq.put(("log", f"[BLE] Other frame: {payload.hex().upper()}"))
                    return
                if already_handled(evt["prefix"], evt["type"], evt["seq"]):
                    return

                name, ev_type, seq, rr = evt["name"], evt["type"], evt["seq"], evt["rrHex"]
                self.msgq.put(("log", f"[BLE] {name} {ev_type} seq={seq} ({evt['prefix']}{rr})"))

                # Resolve behavior and key
                key_name = BUTTON_TO_KEY.get(name)
                behavior = BUTTON_BEHAVIOR.get(name, "tap")

                if ev_type == "press":
                    if not key_name:
                        return
                    if behavior == "tap":
                        send_key_tap(key_name)
                    else:  # "hold"
                        hold_if_needed(name, key_name)
                else:  # release
                    if behavior == "hold":
                        release_if_held(name)
                    # taps ignore release

            try:
                client = BleakClient(dev, disconnected_callback=_on_disc)
                await client.connect()
                self.client = client

                if not client.is_connected:
                    self.msgq.put(("log", "[BLE] Failed to connect."))
                    self.msgq.put(("status", ("Error", "red")))
                    # try to re-scan & reconnect unless user stopped
                    dev = None
                    if self.stop_event.is_set():
                        break
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    continue

                self.msgq.put(("log", "[BLE] Connected. Subscribing to notifications…"))
                await client.start_notify(WAHOO_CHAR_UUID, notification_handler)
                self._notify_on = True

                self.msgq.put(("status", ("Connected", "green")))
                self.msgq.put(("log", "[BLE] Listening (short-frames only)."))
                # Stay here until user clicks Disconnect or device disconnects
                while not self.stop_event.is_set() and not disconnected_event.is_set():
                    await asyncio.sleep(0.1)

            finally:
                # Cleanup this connection attempt
                try:
                    if client:
                        if self._notify_on:
                            try:
                                await client.stop_notify(WAHOO_CHAR_UUID)
                                self.msgq.put(("log", "[BLE] Notifications stopped."))
                            except Exception as e:
                                self.msgq.put(("log", f"[BLE] stop_notify error: {e}"))
                        if client.is_connected:
                            try:
                                await client.disconnect()
                                self.msgq.put(("log", "[BLE] Disconnected from device."))
                            except Exception as e:
                                self.msgq.put(("log", f"[BLE] disconnect error: {e}"))
                finally:
                    self.client = None
                    self._notify_on = False
                    # Always release any held keys when link drops
                    release_all_held_keys()

            # Decide whether to reconnect or exit
            if self.stop_event.is_set():
                self.msgq.put(("status", ("Disconnected", "gray")))
                self.msgq.put(("enable_connect", True))
                break  # user asked to stop

            # Device disconnected unexpectedly -> try to reconnect
            self.msgq.put(("status", ("Reconnecting…", "orange")))
            self.msgq.put(("log", f"[BLE] Will retry in {RECONNECT_DELAY_S:.1f}s…"))
            await asyncio.sleep(RECONNECT_DELAY_S)
            # Keep 'dev' as-is; if that fails next time, loop will re-scan by setting dev=None

    # ---------- UI queue pump ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "status":
                    text, color = payload
                    self.set_status(text, color)
                elif kind == "enable_connect":
                    self.btn_connect.config(state="normal" if payload else "disabled")
                    # Disconnect stays enabled so user can click again; it just sets stop_event
                    self.btn_disconnect.config(state="normal")
        except queue.Empty:
            pass
        # poll again
        self.root.after(80, self._drain_queue)

    # ---------- Main loop ----------
    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = BikeKeyGUI()
    app.run()