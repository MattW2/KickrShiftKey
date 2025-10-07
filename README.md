# Wahoo Bike Shift → Windows Keyboard Bridge

Small helper that connects to a Wahoo "KICKR BIKE SHIFT" BLE peripheral and converts its button events into synthetic keyboard events on Windows.

This repository contains a single-script demo `wahoo_bike_shift_BLE_keyboard.py` (plus some archived GUI variants in `Archive/`). The script uses BLE notifications to detect button presses/releases from the bike shifters and injects corresponding key presses on the PC (via pyautogui). A tiny Tkinter GUI is included for connect/disconnect and runtime logging.

## Features
- Connect/disconnect GUI with status indicator and log
- Auto-reconnect loop (unless the user explicitly disconnects)
- Map BLE button frames to logical buttons and then to keyboard keys
- Support for two behaviors per button:
  - tap: press a key briefly on button press
  - hold: press (key down) on button press and release on button release
- Optional repeating for held keys (typematic behavior)

## Quick start

1. Install Python 3.8+ (3.10/3.11 recommended) on Windows.
2. Install required packages in your active environment:

```powershell
python -m pip install bleak pyautogui pillow
# On Windows you may also need pywin32 which pyautogui can depend on:
python -m pip install pywin32
```

3. Run the script from a terminal:

```powershell
python wahoo_bike_shift_BLE_keyboard.py
```

4. In the GUI click Connect. When the KICKR BIKE SHIFT device is discovered the script will subscribe to notifications and begin logging button frames.

Notes:
- Bluetooth hardware and Windows BLE support are required. The script uses the default bleak backend for Windows.
- If you have multiple Bluetooth adapters or a paired device that blocks advertising, you may need to unpair or disable other adapters.

## Configuration points (inside the script)

Open `wahoo_bike_shift_BLE_keyboard.py` and look for the top-level constants to tune behavior:

- DEVICE_NAME_PREFIX — device name prefix used when scanning (default "KICKR BIKE SHIFT").
- WAHOO_SERVICE_UUID / WAHOO_CHAR_UUID — service and characteristic UUID used for notifications.
- PREFIX_TO_BUTTON — maps BLE frame prefixes to logical button names.
- BUTTON_TO_KEY — maps logical button names to keyboard keys (pyautogui key names or single characters).
- BUTTON_BEHAVIOR — sets whether a button is a "tap" or "hold".
- REPEAT_HZ — if a button is a hold, the rate to re-send keyDown calls for apps that require repeats.
- SCAN_TIMEOUT_S / RECONNECT_DELAY_S — scanning and reconnect retry timing.

You can safely change key mapping and behaviors without touching the BLE parsing logic.

## How it works (brief)

- The BLE peripheral sends short frames (3 bytes) for button events.
- The script parses the first two bytes as a prefix to identify the button, and the third byte contains press/release information plus a 7-bit rolling sequence number.
- Duplicate frames are filtered using the rolling sequence value.
- For `tap` behavior the script sends a single pyautogui.press on press.
- For `hold` behavior it calls pyautogui.keyDown when press is seen and pyautogui.keyUp on release; it may also periodically re-issue keyDown to emulate typematic repeat.

## Safety and notes

- pyautogui will generate real keyboard events. Be careful while testing; the system will receive those keys.
- If the script crashes or you kill it while a key is held, the script tries to release any held keys at exit (best-effort). If a key remains logically held, manually press/release the key or reboot.
- pyautogui has a fail-safe: move the mouse to the top-left to abort; you can disable this in code but it's not recommended for testing.

## Troubleshooting

- No device discovered: make sure the bike/shifters are powered on and advertising, and that Windows Bluetooth is enabled. Some devices stop advertising if already paired — try unpairing from Windows Bluetooth settings.
- Bleak errors on Windows: ensure you are using a supported Python version and the MSFT backend is available on your Windows version.
- Keyboard events not recognized by an application: try enabling the repeating typematic (increase `REPEAT_HZ`) or experiment with different pyautogui key names for special keys (e.g. `left`, `right`, `up`, `down`).

## Files of interest
- `wahoo_bike_shift_BLE_keyboard.py` — main script with GUI, BLE client, and keyboard mapping logic.
- `Archive/` — older or experimental GUI/script variants.