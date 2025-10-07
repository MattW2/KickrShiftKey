#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KICKR BIKE SHIFT — GUI (short-frames only)
- Connect/Disconnect buttons
- Status indicator
- Debug window showing incoming commands
- On press (configurable) -> OS-level key tap via pynput

Requires: bleak, pynput
"""

import asyncio
import threading
import queue
import sys
from typing import Dict, Tuple, Optional

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from bleak import BleakClient, BleakScanner
from pynput.keyboard import Controller, Key

# ----------------------------
# CONFIGURATION
# ----------------------------

# Match any device named "KICKR BIKE SHIFT *"
DEVICE_NAME_PREFIX = "KICKR BIKE SHIFT"

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
    "Right Up": "ArrowUp",
    "Right Down": "ArrowDown",
    "Right Steer": "ArrowRight",
    "Right Shift Up": "k",
    "Right Shift Down": "j",
    "Right Brake": "Space",
    # Left
    "Left Up": "w",
    "Left Down": "s",
    "Left Steer": "ArrowLeft",
    "Left Shift Up": "e",
    "Left Shift Down": "q",
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
    if key_name is None or key_name == "":
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
# BLE worker (async in a thread)
# ----------------------------

async def find_device_by_prefix(prefix: str, timeout: float):
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if d.name and d.name.startswith(prefix):
            return d
    return None

# ----------------------------
# GUI App
# ----------------------------

class BikeKeyGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("KICKR BIKE SHIFT — Button → Key (Short-frames only)")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.msgq: queue.Queue = queue.Queue()
        self.ble_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.client: Optional[BleakClient] = None

        self._build_ui()
        self._drain_queue()  # start polling the message queue

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
        hint = ttk.Label(self.root, text="Note: key taps go to the active foreground app. "
                                         "On macOS, allow Accessibility for Python in System Settings.")
        hint.pack(padx=6, pady=(0, 8))

    def set_status(self, text: str, color: str = "gray"):
        # color one of: gray, orange, green, red
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

    # ---------- Window close ----------
    def on_close(self):
        try:
            self.stop_event.set()
            if self.ble_thread and self.ble_thread.is_alive():
                self.append_log("[UI] Waiting for BLE thread to stop…")
                self.ble_thread.join(timeout=3.0)
        finally:
            self.root.destroy()

    # ---------- Background worker ----------
    def _ble_worker(self):
        try:
            asyncio.run(self._ble_main())
        except Exception as e:
            self.msgq.put(("log", f"[BLE] Error: {e}"))
            self.msgq.put(("status", ("Error", "red")))
            self.msgq.put(("enable_connect", True))

    async def _ble_main(self):
        # Scan
        dev = await find_device_by_prefix(DEVICE_NAME_PREFIX, SCAN_TIMEOUT_S)
        if dev is None:
            self.msgq.put(("log", "[BLE] Device not found. Is the bike on / advertising?"))
            self.msgq.put(("status", ("Not found", "red")))
            self.msgq.put(("enable_connect", True))
            return

        self.msgq.put(("log", f"[BLE] Found {dev.name} ({dev.address}) — connecting…"))

        async def notification_handler(_char, data: bytearray):
            # short-frame only
            payload = bytes(data)
            evt = parse_short_frame(payload)
            if not evt:
                # Log unknown frames for debugging
                # (comment this out if too chatty)
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
                # Perform key injection (OS-level)
                send_key_tap(key_name)

        # Connect and listen
        try:
            async with BleakClient(dev) as client:
                self.client = client
                if not client.is_connected:
                    self.msgq.put(("log", "[BLE] Failed to connect."))
                    self.msgq.put(("status", ("Error", "red")))
                    self.msgq.put(("enable_connect", True))
                    return

                self.msgq.put(("log", "[BLE] Connected. Subscribing to notifications…"))
                await client.start_notify(WAHOO_CHAR_UUID, notification_handler)
                self.msgq.put(("status", ("Connected", "green")))
                self.msgq.put(("log", "[BLE] Listening (short-frames only)."))

                # Loop until user requests disconnect
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.1)

                self.msgq.put(("log", "[BLE] Stopping notifications…"))
                try:
                    await client.stop_notify(WAHOO_CHAR_UUID)
                except Exception:
                    pass
        finally:
            self.client = None
            self.msgq.put(("status", ("Disconnected", "gray")))
            self.msgq.put(("log", "[BLE] Disconnected."))
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
                    # Re-enable connect button when worker completes or errors
                    self.btn_connect.config(state="normal" if payload else "disabled")
                    # Always allow disconnect to be pressed again (it just sets stop_event)
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
