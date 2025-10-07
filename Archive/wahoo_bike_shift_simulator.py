#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KICKR BIKE SHIFT — BLE Simulator (GATT server with notifications)
- Advertises the Wahoo service & characteristic UUIDs your client uses
- GUI: 12 buttons + per-button Hold (tap vs. hold)
- Keyboard driving: map keys to buttons (tap or hold semantics)
- Sends short-frame notifications: PP QQ RR (MSB of RR=press; lower 7 bits=sequence)
"""

import asyncio
import threading
from collections import defaultdict
from typing import Dict, Optional

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from pynput import keyboard as pynput_keyboard

# Bless: cross-platform BLE GATT server with notify/indicate support
# pip install bless
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

# ----------------------------
# Wahoo UUIDs (match your client)
# ----------------------------
WAHOO_SERVICE_UUID = "a026ee0d-0a7d-4ab3-97fa-f1500f9feb8b"
WAHOO_CHAR_UUID    = "a026e03c-0a7d-4ab3-97fa-f1500f9feb8b"

DEVICE_NAME = "KICKR BIKE SHIFT SIM"  # matches client's namePrefix ("KICKR BIKE SHIFT")

# ----------------------------
# Button families (prefix -> name)
# Each short-frame notification is: [P, Q, R] where P Q form the 4-hex prefix.
# ----------------------------
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

BUTTONS = list(PREFIX_TO_BUTTON.values())
BUTTON_TO_PREFIX = {v: k for (k, v) in PREFIX_TO_BUTTON.items()}

# ----------------------------
# Per-button behavior (default)
# "tap": press + release immediately; "hold": press on down, release on up
# You can change this in the GUI per button (Hold checkbox).
# ----------------------------
DEFAULT_BEHAVIOR = {
    "Right Steer": "hold",
    "Left Steer":  "hold",
}

# ----------------------------
# Keyboard mappings for simulator control
# keysym -> button name
# ----------------------------
KEY_TO_BUTTON = {
    # Right side
    "Up":    "Right Up",
    "Down":  "Right Down",
    "Right": "Right Steer",
    "I":     "Right Shift Up",     # i
    "K":     "Right Shift Down",   # k
    "space": "Right Brake",

    # Left side
    "W":     "Left Up",
    "S":     "Left Down",
    "Left":  "Left Steer",
    "E":     "Left Shift Up",
    "Q":     "Left Shift Down",
    "Return":"Left Brake",         # Enter
}
# To make matching easy across platforms and Tk keysyms:
NORM_KEYS = {k.lower(): v for k, v in KEY_TO_BUTTON.items()}

# ----------------------------
# BLE simulator (server) in an asyncio loop running in a background thread
# ----------------------------
class WahooSimBLE:
    def __init__(self, log_fn):
        self.log = log_fn
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server: Optional[BlessServer] = None
        self.char: Optional[BlessGATTCharacteristic] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        # per-prefix rolling sequence (7-bit), increment on press only
        self.seq_by_prefix = defaultdict(int)

    # ----------- BLE notifier helpers -----------
    async def _start_server(self):
        self.server = BlessServer(name=DEVICE_NAME)
        # optional read/write callbacks (we only need notify)
        def _read_request(characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
            # allow clients to read the last value (not required by your client)
            return characteristic.value or bytearray()

        self.server.read_request_func = _read_request

        # add service
        await self.server.add_new_service(WAHOO_SERVICE_UUID)

        # add characteristic with notify+indicate (+read for diagnostics)
        char_flags = (
            GATTCharacteristicProperties.notify
            | GATTCharacteristicProperties.indicate
            | GATTCharacteristicProperties.read
        )
        permissions = GATTAttributePermissions.readable

        await self.server.add_new_characteristic(
            WAHOO_SERVICE_UUID, WAHOO_CHAR_UUID, char_flags, None, permissions
        )

        # keep a handle on the characteristic
        self.char = self.server.get_characteristic(WAHOO_CHAR_UUID)

        # start advertising
        await self.server.start()
        self.log("[BLE] Advertising as '{}' (service {})".format(DEVICE_NAME, WAHOO_SERVICE_UUID))

    async def _stop_server(self):
        try:
            if self.server:
                await self.server.stop()
                self.log("[BLE] Advertising stopped.")
        finally:
            self.server = None
            self.char = None

    async def _notify_payload(self, payload: bytes):
        """Set the characteristic value then trigger a notify/indicate to subscribers."""
        if not self.server or not self.char:
            return
        self.char.value = bytearray(payload)
        # Bless updates and sends notification/indication to subscribers
        self.server.update_value(WAHOO_SERVICE_UUID, WAHOO_CHAR_UUID)

    # ----------- lifecycle (thread + loop) -----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._stop_server(), self.loop).result()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start_server())
            # keep loop alive until stop requested
            while not self._stop_evt.is_set():
                self.loop.run_until_complete(asyncio.sleep(0.1))
        except Exception as e:
            self.log(f"[BLE] ERROR: {e}")
        finally:
            try:
                self.loop.run_until_complete(self._stop_server())
            except Exception:
                pass
            self.loop.close()

    # ----------- public API for the GUI -----------
    def send_press(self, button_name: str):
        prefix = BUTTON_TO_PREFIX.get(button_name)
        if not prefix:
            return
        # increment sequence on press (wrap to 0..127)
        seq = (self.seq_by_prefix[prefix] + 1) & 0x7F
        self.seq_by_prefix[prefix] = seq
        rr = 0x80 | seq  # MSB=1 => press
        payload = bytes.fromhex(prefix) + bytes([rr])
        self.log(f"[SIM] {button_name} press seq={seq} ({prefix}{rr:02X})")
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._notify_payload(payload), self.loop)

    def send_release(self, button_name: str):
        prefix = BUTTON_TO_PREFIX.get(button_name)
        if not prefix:
            return
        # reuse same sequence for release (MSB=0)
        seq = self.seq_by_prefix[prefix] & 0x7F
        rr = seq  # MSB=0 => release
        payload = bytes.fromhex(prefix) + bytes([rr])
        self.log(f"[SIM] {button_name} release seq={seq} ({prefix}{rr:02X})")
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._notify_payload(payload), self.loop)

# ----------------------------
# GUI app
# ----------------------------
class WahooSimGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("KICKR BIKE SHIFT — BLE Simulator")

        # UI
        self.log = ScrolledText(self.root, height=18, width=100, state="disabled")
        self.status = ttk.Label(self.root, text="Idle")
        self.dot = tk.Label(self.root, text="●", font=("Segoe UI", 14), fg="gray")

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=6, pady=(6, 0))
        btn_start = ttk.Button(top, text="Start Advertising", command=self.on_start)
        btn_stop  = ttk.Button(top, text="Stop Advertising",  command=self.on_stop)
        btn_start.pack(side="left", padx=(0, 6))
        btn_stop.pack(side="left")
        self.status.pack(side="right", padx=(6, 4), in_=top)
        self.dot.pack(side="right", in_=top)

        # Button grid with Hold checkboxes
        grid = ttk.Frame(self.root)
        grid.pack(fill="x", padx=6, pady=6)

        self.behavior_by_btn: Dict[str, tk.BooleanVar] = {}
        self._build_button_row(grid, "Right Up",      "Right Down",    "Right Steer")
        self._build_button_row(grid, "Right Shift Up","Right Shift Down","Right Brake")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=6, pady=6)
        self._build_button_row(grid, "Left Up",       "Left Down",     "Left Steer")
        self._build_button_row(grid, "Left Shift Up", "Left Shift Down","Left Brake")

        # Debug output
        mid = ttk.Frame(self.root)
        mid.pack(fill="x", padx=6)
        self.debug_var = tk.BooleanVar(value=True)
        dbg = ttk.Checkbutton(mid, text="Debug output", variable=self.debug_var)
        dbg.pack(side="left")

        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        # BLE
        self.ble = WahooSimBLE(self.append_log)

        # Keyboard listener (global) — supports hold/tap
        self._held_keys = set()
        self._kb_listener = pynput_keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        self._kb_listener.start()

        # Window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Helpers
        self._pending_taps = {}  # button_name -> after-id for scheduled release

        self._set_status("Idle", "gray")

    def _build_button_row(self, parent, a, b, c):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        for name in (a, b, c):
            frm = ttk.LabelFrame(row, text=name)
            frm.pack(side="left", padx=6, pady=3)

            btn_press = ttk.Button(frm, text="Press", command=lambda n=name: self._button_press(n))
            btn_rel   = ttk.Button(frm, text="Release", command=lambda n=name: self._button_release(n))
            btn_press.grid(row=0, column=0, padx=4, pady=4)
            btn_rel.grid(row=0, column=1, padx=4, pady=4)

            hold_var = tk.BooleanVar(value=(DEFAULT_BEHAVIOR.get(name, "tap") == "hold"))
            chk_hold = ttk.Checkbutton(frm, text="Hold", variable=hold_var)
            chk_hold.grid(row=1, column=0, columnspan=2, padx=4, pady=2)
            self.behavior_by_btn[name] = hold_var

            lab = ttk.Label(frm, text=f"Key: {self._key_for_button(name)}")
            lab.grid(row=2, column=0, columnspan=2, padx=4, pady=2)

    def _key_for_button(self, name):
        for k, v in NORM_KEYS.items():
            if v == name:
                return k
        return "-"

    # ---------- logging & status ----------
    def append_log(self, line: str):
        if not self.debug_var.get() and line.startswith("[SIM] Other"):
            return
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str, color: str):
        self.status.config(text=text)
        self.dot.config(fg=color)

    # ---------- BLE control ----------
    def on_start(self):
        self.ble.start()
        self._set_status("Advertising", "green")
        self.append_log("[UI] Simulator advertising started.")

    def on_stop(self):
        self.ble.stop()
        self._set_status("Stopped", "gray")
        self.append_log("[UI] Simulator advertising stopped.")

    # ---------- Button actions ----------
    def _button_press(self, name: str):
        if self.behavior_by_btn[name].get():  # hold
            self.ble.send_press(name)
        else:  # tap
            self.ble.send_press(name)
            # schedule release after a short delay (50 ms)
            aid = self.root.after(50, lambda n=name: self._button_release(n))
            self._pending_taps[name] = aid

    def _button_release(self, name: str):
        # cancel any pending tap release (if user pressed Release manually)
        aid = self._pending_taps.pop(name, None)
        if aid:
            self.root.after_cancel(aid)
        self.ble.send_release(name)

    # ---------- Keyboard driving ----------
    def _on_key_press(self, key):
        try:
            # normalize keysym
            if hasattr(key, 'char') and key.char:
                ksym = key.char.lower()
            else:
                # special keys (e.g., Key.space, Key.right)
                ksym = str(key).split('.')[-1].lower()

            if ksym in self._held_keys:
                return  # ignore auto-repeat
            self._held_keys.add(ksym)

            btn = NORM_KEYS.get(ksym)
            if not btn:
                return

            if self.behavior_by_btn[btn].get():  # hold
                self.ble.send_press(btn)
            else:  # tap
                self.ble.send_press(btn)
                # schedule auto-release ~50 ms later
                aid = self.root.after(50, lambda n=btn: self._button_release(n))
                self._pending_taps[btn] = aid
        except Exception as e:
            self.append_log(f"[SIM] KeyPress error: {e}")

    def _on_key_release(self, key):
        try:
            if hasattr(key, 'char') and key.char:
                ksym = key.char.lower()
            else:
                ksym = str(key).split('.')[-1].lower()

            self._held_keys.discard(ksym)

            btn = NORM_KEYS.get(ksym)
            if not btn:
                return

            # only send release for hold-mode buttons
            if self.behavior_by_btn[btn].get():  # hold
                self.ble.send_release(btn)
        except Exception as e:
            self.append_log(f"[SIM] KeyRelease error: {e}")

    def on_close(self):
        try:
            self.on_stop()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    WahooSimGUI().run()