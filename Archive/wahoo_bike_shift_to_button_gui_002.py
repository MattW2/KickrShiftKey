#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KICKR BIKE SHIFT — GUI (short-frames only) with guaranteed cleanup on exit:
- Connect / Disconnect buttons
- Status indicator
- Debug window showing incoming commands
- Short-frame-only decoding; on press => OS-level key tap via pynput
- Robust cleanup: stop_notify + disconnect on Disconnect, window close, or program exit
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

from bleak import BleakClient, BleakScanner
from pynput.keyboard import Controller, Key

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
# Use printable chars ('k', ' ') or names: 'ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter','Space', etc.
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

# Fire only on press? (True -> ignore release)
TRIGGER_ON_PRESS_ONLY = True

# Scan timeout (seconds)
SCAN_TIMEOUT_S = 12.0

# ----------------------------
# Key injection helpers
# ----------------------------

keyboard = Controller()

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

def send_key_tap(key_name: str):
    """Tap a key using pynput (OS-level; goes to the foreground app)."""
    if not key_name:
        return
    key_obj = _NAME_TO_SPECIAL.get(key_name, key_name)  # use literal if not special
    keyboard.press(key_obj)
    keyboard.release(key_obj)

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
        "name": PREFIX_TO_BUTTON[prefix],
        "type": ev_type,
        "seq": seq,
        "rrHex": f"{r:02X}",
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
# GUI App with robust cleanup
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

        # Debug log
        self.log = ScrolledText(self.root, height=22, width=100, state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        # Hint
        hint = ttk.Label(
            self.root,
            text="Note: key taps go to the active foreground app. On macOS, allow Accessibility for Python."
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
        except Exception:
            pass

    # ---------- Background worker ----------
    def _ble_worker(self):
        try:
            asyncio.run(self._ble_main())
        except Exception as e:
            self.msgq.put(("log", f"[BLE] Error: {e}"))
            self.msgq.put(("status", ("Error", "red")))
            self.msgq.put(("enable_connect", True))

    async def _ble_main(self):
        dev = await find_device_by_prefix(DEVICE_NAME_PREFIX, SCAN_TIMEOUT_S)
        if dev is None:
            self.msgq.put(("log", "[BLE] Device not found. Is the bike on / advertising?"))
            self.msgq.put(("status", ("Not found", "red")))
            self.msgq.put(("enable_connect", True))
            return

        self.msgq.put(("log", f"[BLE] Found {dev.name} ({dev.address}) — connecting…"))
        client: Optional[BleakClient] = None
        self._notify_on = False

        async def notification_handler(_char, data: bytearray):
            payload = bytes(data)
            evt = parse_short_frame(payload)
            if not evt:
                # Log unknown frames for debugging; comment out if too chatty
                self.msgq.put(("log", f"[BLE] Other frame: {payload.hex().upper()}"))
                return
            if already_handled(evt["prefix"], evt["type"], evt["seq"]):
                return

            name, ev_type, seq, rr = evt["name"], evt["type"], evt["seq"], evt["rrHex"]
            self.msgq.put(("log", f"[BLE] {name} {ev_type} seq={seq} ({evt['prefix']}{rr})"))

            if TRIGGER_ON_PRESS_ONLY and ev_type != "press":
                return
            key_name = BUTTON_TO_KEY.get(name)
            if key_name:
                send_key_tap(key_name)

        try:
            client = BleakClient(dev)
            await client.connect()
            self.client = client

            if not client.is_connected:
                self.msgq.put(("log", "[BLE] Failed to connect."))
                self.msgq.put(("status", ("Error", "red")))
                self.msgq.put(("enable_connect", True))
                return

            self.msgq.put(("log", "[BLE] Connected. Subscribing to notifications…"))
            await client.start_notify(WAHOO_CHAR_UUID, notification_handler)
            self._notify_on = True

            self.msgq.put(("status", ("Connected", "green")))
            self.msgq.put(("log", "[BLE] Listening (short-frames only)."))

            # Run until requested to stop
            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)

        finally:
            # Robust cleanup — ALWAYS attempt to stop notifications and disconnect
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
                self.msgq.put(("status", ("Disconnected", "gray")))
                self.msgq.put(("enable_connect", True))

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