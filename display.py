#!/usr/bin/env python3
"""
ADS-B Station Display Driver
==============================
Drives a Waveshare 1.44inch LCD HAT (ST7735S, 128x128, SPI) on a Raspberry Pi.

Screens:
  - Status  : WiFi, ADS-B / GPS / GDL90 data-flow, CPU temp, uptime
  - Config  : Power >, WiFi >, Theme toggle
  - Power   : Shutdown / Reboot with 3-second countdown
  - Network : WiFi network selection

Inputs:
  KEY1 (GPIO21) = Back
  KEY2 (GPIO20) = Lock / Unlock
  KEY3 (GPIO16) = Config Menu
  Joystick Up/Down (GPIO 6/19) = navigate menus
  Joystick Left/Right (GPIO 5/26) = theme toggle / back
  Joystick Press (GPIO13) = confirm selection

Requires: spidev, RPi.GPIO, Pillow  (apt: python3-pil, pip: spidev)
"""

import json
import logging
import math
import os
import signal
import sqlite3
import subprocess
import threading
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

from configuration import Config

log = logging.getLogger("adsb-display")

# ── Try importing Pi-specific libs; allow running on dev machine for linting ──
GPIO: Any = None
spidev: Any = None
_HAS_HW: bool = False

try:
    import spidev  # type: ignore[no-redef]
    import RPi.GPIO as GPIO  # type: ignore[no-redef,import-untyped]
    _HAS_HW = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Pin definitions  (BCM numbering)
# ---------------------------------------------------------------------------
PIN_KEY1       = 21   # Back
PIN_KEY2       = 20   # Lock / Unlock
PIN_KEY3       = 16   # Config menu
PIN_JOY_UP     = 6
PIN_JOY_DOWN   = 19
PIN_JOY_LEFT   = 5
PIN_JOY_RIGHT  = 26
PIN_JOY_PRESS  = 13

PIN_DC         = 25
PIN_RST        = 27
PIN_BL         = 24
PIN_CS         = 8    # CE0

ALL_INPUT_PINS = [
    PIN_KEY1, PIN_KEY2, PIN_KEY3,
    PIN_JOY_UP, PIN_JOY_DOWN, PIN_JOY_LEFT, PIN_JOY_RIGHT, PIN_JOY_PRESS,
]

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
LCD_WIDTH  = 128
LCD_HEIGHT = 128
LCD_X_OFFSET = 2   # ST7735S 132-wide RAM → 128-wide panel starts at col 2
LCD_Y_OFFSET = 1

REFRESH_INTERVAL = 3.0   # seconds between idle redraws
DEBOUNCE_MS      = 500   # milliseconds for buttons
DEBOUNCE_JOY_MS  = 400   # milliseconds for joystick (longer to avoid double-step)
COUNTDOWN_SECS   = 3

# ---------------------------------------------------------------------------
# Colours (RGB tuples for PIL)
# ---------------------------------------------------------------------------
_THEMES = {
    "dark":  {"bg": (0, 0, 0),       "text": (255, 255, 255)},
    "light": {"bg": (255, 255, 255), "text": (0, 0, 0)},
}

COL_BG         = (0, 0, 0)
COL_TEXT       = (255, 255, 255)
COL_GREEN      = (0, 200, 0)
COL_RED        = (255, 60, 60)
COL_YELLOW     = (255, 200, 0)
COL_GREY       = (120, 120, 120)
COL_HIGHLIGHT  = (50, 50, 180)
COL_SECTION    = (80, 80, 80)
COL_SOFTKEY_BG = (40, 40, 40)
COL_RADAR_RING = (50, 50, 50)
COL_RADAR_OWN  = (0, 180, 255)
COL_BAND_RED   = (255, 60, 60)
COL_BAND_YELLOW = (255, 200, 0)

# ---------------------------------------------------------------------------
# ST7735S commands
# ---------------------------------------------------------------------------
_SWRESET = 0x01
_SLPIN   = 0x10
_SLPOUT  = 0x11
_FRMCTR1 = 0xB1
_FRMCTR2 = 0xB2
_FRMCTR3 = 0xB3
_INVCTR  = 0xB4
_PWCTR1  = 0xC0
_PWCTR2  = 0xC1
_PWCTR3  = 0xC2
_PWCTR4  = 0xC3
_PWCTR5  = 0xC4
_VMCTR1  = 0xC5
_INVOFF  = 0x20
_MADCTL  = 0x36
_COLMOD  = 0x3A
_CASET   = 0x2A
_RASET   = 0x2B
_RAMWR   = 0x2C
_GMCTRP1 = 0xE0
_GMCTRN1 = 0xE1
_NORON   = 0x13
_DISPOFF = 0x28
_DISPON  = 0x29

# ---------------------------------------------------------------------------
# Embedded ST7735S SPI driver
# ---------------------------------------------------------------------------

class ST7735S:
    """Minimal driver for ST7735S 128x128 LCD via SPI."""

    def __init__(self) -> None:
        self._spi: Any = None

    # ── low-level ──────────────────────────────────────────────────────────

    def _command(self, cmd: int) -> None:
        GPIO.output(PIN_DC, GPIO.LOW)
        self._spi.writebytes([cmd])

    def _data(self, data) -> None:
        GPIO.output(PIN_DC, GPIO.HIGH)
        if isinstance(data, int):
            self._spi.writebytes([data])
        else:
            # spidev has a 4096-byte transfer limit
            buf = list(data) if not isinstance(data, list) else data
            for i in range(0, len(buf), 4096):
                self._spi.writebytes(buf[i:i+4096])

    def _write_cmd(self, cmd: int, *args: int) -> None:
        self._command(cmd)
        if args:
            self._data(list(args))

    # ── public API ─────────────────────────────────────────────────────────

    def init(self) -> None:
        """Initialise GPIO, SPI and the ST7735S controller."""
        log.info("Initialising GPIO (BCM mode)")
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(PIN_DC,  GPIO.OUT)
        GPIO.setup(PIN_RST, GPIO.OUT)
        GPIO.setup(PIN_BL,  GPIO.OUT)
        # PIN_CS (CE0) is managed by the SPI driver — do not setup manually

        # Input pins with pull-up (active LOW)
        for pin in ALL_INPUT_PINS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        log.info("GPIO pins configured: DC=%d RST=%d BL=%d CS=%d",
                 PIN_DC, PIN_RST, PIN_BL, PIN_CS)

        # Backlight ON
        GPIO.output(PIN_BL, GPIO.HIGH)
        log.info("Backlight ON")

        # Hardware reset — needs long pulses for reliable startup
        log.info("Hardware reset sequence start")
        GPIO.output(PIN_RST, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(PIN_RST, GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(PIN_RST, GPIO.HIGH)
        time.sleep(0.1)
        log.info("Hardware reset complete")

        # SPI
        try:
            self._spi = spidev.SpiDev(0, 0)
            self._spi.max_speed_hz = 9_000_000
            self._spi.mode = 0
            log.info("SPI opened: bus=0 dev=0 speed=%d Hz mode=%d",
                     self._spi.max_speed_hz, self._spi.mode)
        except Exception:
            log.exception("Failed to open SPI device")
            raise

        # ST7735S init sequence
        self._write_cmd(_SWRESET)
        time.sleep(0.15)
        self._write_cmd(_SLPOUT)
        time.sleep(0.5)

        self._write_cmd(_FRMCTR1, 0x01, 0x2C, 0x2D)
        self._write_cmd(_FRMCTR2, 0x01, 0x2C, 0x2D)
        self._write_cmd(_FRMCTR3, 0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D)
        self._write_cmd(_INVCTR, 0x07)
        self._write_cmd(_PWCTR1, 0xA2, 0x02, 0x84)
        self._write_cmd(_PWCTR2, 0xC5)
        self._write_cmd(_PWCTR3, 0x0A, 0x00)
        self._write_cmd(_PWCTR4, 0x8A, 0x2A)
        self._write_cmd(_PWCTR5, 0x8A, 0xEE)
        self._write_cmd(_VMCTR1, 0x0E)
        self._write_cmd(_INVOFF)
        # MADCTL: Waveshare 1.44" HAT orientation
        self._write_cmd(_MADCTL, 0x78)
        self._write_cmd(_COLMOD, 0x05)  # 16-bit RGB565

        # Gamma
        self._write_cmd(_GMCTRP1,
            0x0F, 0x1A, 0x0F, 0x18, 0x2F, 0x28, 0x20, 0x22,
            0x1F, 0x1B, 0x23, 0x37, 0x00, 0x07, 0x02, 0x10)
        self._write_cmd(_GMCTRN1,
            0x0F, 0x1B, 0x0F, 0x17, 0x33, 0x2C, 0x29, 0x2E,
            0x30, 0x30, 0x39, 0x3F, 0x00, 0x07, 0x03, 0x10)

        self._write_cmd(_NORON)
        time.sleep(0.01)
        self._write_cmd(_DISPON)
        time.sleep(0.1)
        log.info("ST7735S init sequence complete — display ON")

        # ---- Pin diagnostics ----
        bl_state = GPIO.input(PIN_BL)
        rst_state = GPIO.input(PIN_RST)
        log.info("Pin readback: BL(GPIO%d)=%d  RST(GPIO%d)=%d",
                 PIN_BL, bl_state, PIN_RST, rst_state)
        if bl_state != 1:
            log.warning("Backlight pin reads LOW — display will be dark")
        if rst_state != 1:
            log.warning("RST pin reads LOW — display held in reset!")

    def test_pattern(self) -> None:
        """Flash R/G/B test screens to verify SPI + pin connections."""
        for name, colour in [("RED", (255, 0, 0)),
                             ("GREEN", (0, 255, 0)),
                             ("BLUE", (0, 0, 255))]:
            log.info("Test pattern: %s", name)
            self.clear(colour)
            time.sleep(0.5)
        log.info("Test pattern done — if you saw R/G/B the connection is OK")

    def show_image(self, image: Image.Image) -> None:
        """Send a 128x128 RGB PIL Image to the display."""
        x0 = LCD_X_OFFSET
        x1 = LCD_X_OFFSET + LCD_WIDTH - 1
        y0 = LCD_Y_OFFSET
        y1 = LCD_Y_OFFSET + LCD_HEIGHT - 1

        self._write_cmd(_CASET, x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._write_cmd(_RASET, y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._command(_RAMWR)

        # Convert PIL RGB → RGB565 bytes
        px = image.convert("RGB").tobytes()
        buf = bytearray(LCD_WIDTH * LCD_HEIGHT * 2)
        idx = 0
        for i in range(0, len(px), 3):
            r, g, b = px[i], px[i+1], px[i+2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = (rgb565 >> 8) & 0xFF
            buf[idx + 1] = rgb565 & 0xFF
            idx += 2

        self._data(buf)

    def clear(self, colour: tuple = (0, 0, 0)) -> None:
        img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), colour)
        self.show_image(img)

    def cleanup(self) -> None:
        """Turn off display, enter sleep mode, backlight OFF, clean up GPIO."""
        log.info("Cleaning up: display off, sleep, backlight OFF, closing SPI")
        if self._spi:
            try:
                self._write_cmd(_DISPOFF)
                time.sleep(0.05)
                self._write_cmd(_SLPIN)
                time.sleep(0.12)
            except Exception:
                log.exception("Failed to send sleep commands")
            self._spi.close()
        GPIO.output(PIN_BL, GPIO.LOW)
        GPIO.cleanup()


# ---------------------------------------------------------------------------
# System info gathering
# ---------------------------------------------------------------------------


def _get_known_networks() -> list[str]:
    """Return list of saved WiFi connection names via nmcli."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            timeout=5, stderr=subprocess.DEVNULL,
        ).decode().strip()
        names = []
        for line in out.splitlines():
            parts = line.split(":", 1)
            if len(parts) == 2 and "wireless" in parts[1]:
                name = parts[0].strip()
                if name:
                    names.append(name)
        return names
    except Exception:
        return []


def _get_visible_ssids() -> set[str]:
    """Return set of SSIDs currently visible via nmcli wifi scan."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
            timeout=10, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return {s.strip() for s in out.splitlines() if s.strip()}
    except Exception:
        return set()


def _connect_network(name: str) -> bool:
    """Attempt to connect to a saved network. Returns True on success."""
    try:
        subprocess.check_call(
            ["sudo", "nmcli", "connection", "up", name],
            timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _get_wifi_info() -> tuple[str, str]:
    """Return (ssid, ip_address). Empty strings if not connected."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"],
            timeout=3, stderr=subprocess.DEVNULL,
        ).decode().strip()
        ssid = ""
        for line in out.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                ssid = line.split(":", 1)[1].strip()
                if ssid == "--":
                    ssid = ""
                break
    except Exception:
        ssid = ""

    try:
        out = subprocess.check_output(
            ["hostname", "-I"], timeout=3, stderr=subprocess.DEVNULL,
        ).decode().strip()
        ip = out.split()[0] if out else ""
    except Exception:
        ip = ""

    return ssid, ip


def _get_cpu_temp() -> str:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read().strip()) / 1000:.0f}\u00b0C"
    except Exception:
        return "?"


def _get_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        h, remainder = divmod(secs, 3600)
        m = remainder // 60
        if h > 0:
            return f"{h}h{m:02d}m"
        return f"{m}m"
    except Exception:
        return "?"


def _get_cpu_percent(prev: list) -> tuple[str, list]:
    """Return CPU usage % since last call. prev = [idle, total] from prior read."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        if prev[1] == 0:
            pct = 0.0
        else:
            d_total = total - prev[1]
            d_idle = idle - prev[0]
            pct = ((d_total - d_idle) / d_total * 100) if d_total else 0.0
        return f"{pct:.0f}%", [idle, total]
    except Exception:
        return "?", prev


def _get_ram_percent() -> str:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if parts[0] in ("MemTotal:", "MemAvailable:"):
                info[parts[0]] = int(parts[1])
        total = info["MemTotal:"]
        avail = info["MemAvailable:"]
        pct = (total - avail) / total * 100
        return f"{pct:.0f}%"
    except Exception:
        return "?"


def _get_throttle_status() -> str:
    """Return throttle warning string, or empty if OK."""
    try:
        out = subprocess.check_output(
            ["vcgencmd", "get_throttled"], timeout=2, stderr=subprocess.DEVNULL,
        ).decode().strip()
        # Format: throttled=0x0
        val = int(out.split("=")[1], 16)
        flags = []
        if val & 0x1:
            flags.append("UV")      # under-voltage now
        if val & 0x2:
            flags.append("FREQ")    # frequency capped now
        if val & 0x4:
            flags.append("THROT")   # throttled now
        if val & 0x8:
            flags.append("TLIM")    # soft temp limit
        return "/".join(flags)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Port flow monitors — lightweight non-blocking sniffers
# ---------------------------------------------------------------------------

class _FlowMonitor:
    """Track whether data is flowing by reading a heartbeat file from the bridge."""

    def __init__(self, heartbeat_file: str) -> None:
        self.heartbeat_file = heartbeat_file
        self._last_data_ts = 0.0
        self._running = False
        self._thread = None

    @property
    def active(self) -> bool:
        """True if data was seen in the last 5 seconds."""
        return (time.time() - self._last_data_ts) < 5.0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            try:
                with open(self.heartbeat_file, "r") as f:
                    ts = float(f.read().strip())
                if (time.time() - ts) < 5.0:
                    self._last_data_ts = time.time()
            except Exception:
                pass
            time.sleep(3)


# ---------------------------------------------------------------------------
# Display application
# ---------------------------------------------------------------------------

# Screen states
SCREEN_STATUS  = 0
SCREEN_POWER   = 1
SCREEN_NETWORK = 2
SCREEN_CONFIG  = 3
SCREEN_RADAR   = 4
SCREEN_OWNSHIP = 5

RADAR_JSON_PATH = "/tmp/radar.json"
READSB_JSON_PATH = "/run/readsb/aircraft.json"
OWNSHIP_JSON_PATH = "/tmp/ownship.json"
AIRCRAFT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aircraft.db")

POWER_SHUTDOWN = 0
POWER_REBOOT   = 1

# Config menu item indices
CFG_THEME    = 0
CFG_OWNSHIP  = 1
CFG_WIFI     = 2
CFG_POWER    = 3
_CFG_COUNT   = 4
_THEME_OPTIONS = ["dark", "light"]


class DisplayApp:

    def __init__(self) -> None:
        self.lcd = ST7735S()
        self.cfg = Config()

        # State
        self.screen = SCREEN_STATUS
        self.locked = False
        self.power_selection = POWER_SHUTDOWN
        self._countdown_active = False

        # Config menu state
        self._cfg_selection = 0

        # Network menu state
        self._net_list: list[tuple[str, bool]] = []  # (name, available)
        self._net_selection = 0
        self._net_scroll = 0  # top visible index
        self._net_connecting = False

        # Ownship aircraft list state
        self._own_list: list[dict] = []  # [{icao, callsign, dist_nm}]
        self._own_selection = 0
        self._own_scroll = 0
        self._own_has_lock = False
        self._own_lock_label = ""

        # Debounce timestamps per pin
        self._last_press: dict[int, float] = {p: 0.0 for p in ALL_INPUT_PINS}

        # Font — try DejaVu first, fall back to default
        self._font = None
        self._font_sm = None
        self._font_xs = None
        self._load_fonts()

        # Apply theme from config
        self._apply_theme()

        # Flow monitors
        self.mon_adsb  = _FlowMonitor("/tmp/adsb_heartbeat")
        self.mon_gps   = _FlowMonitor("/tmp/gps_heartbeat")
        self.mon_gdl90 = _FlowMonitor("/tmp/gdl90_heartbeat")

        # CPU usage tracker (previous [idle, total] for delta calculation)
        self._cpu_prev: list = [0, 0]

        # Redraw event — set when input received for instant redraw
        self._redraw_event = threading.Event()

        # Last rendered frame for screenshots
        self._last_frame: Image.Image | None = None

        # Running flag
        self._running = True

    def _apply_theme(self) -> None:
        global COL_BG, COL_TEXT
        theme = self.cfg.get("display", "theme")
        palette = _THEMES.get(theme, _THEMES["dark"])
        COL_BG = palette["bg"]
        COL_TEXT = palette["text"]

    def _load_fonts(self) -> None:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                self._font = ImageFont.truetype(fp, 12)
                self._font_sm = ImageFont.truetype(fp, 10)
                self._font_xs = ImageFont.truetype(fp, 9)
                break
        else:
            self._font = ImageFont.load_default()
            self._font_sm = self._font
            self._font_xs = self._font

        # Icon font (Symbola) for Unicode symbols
        icon_font_paths = [
            "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
        ]
        self._icon_font = None
        self._icon_font_sm = None
        for fp in icon_font_paths:
            if os.path.exists(fp):
                self._icon_font = ImageFont.truetype(fp, 12)
                self._icon_font_sm = ImageFont.truetype(fp, 10)
                break
        if self._icon_font is None:
            self._icon_font = self._font
            self._icon_font_sm = self._font_sm

    # ── input handling ─────────────────────────────────────────────────────

    def _debounced(self, pin: int) -> bool:
        """Return True if pin is pressed and debounce window has passed."""
        if GPIO.input(pin) != 0:          # active LOW
            return False
        now = time.time()
        joy_pins = (PIN_JOY_UP, PIN_JOY_DOWN, PIN_JOY_LEFT, PIN_JOY_RIGHT, PIN_JOY_PRESS)
        ms = DEBOUNCE_JOY_MS if pin in joy_pins else DEBOUNCE_MS
        if (now - self._last_press[pin]) < (ms / 1000.0):
            return False
        self._last_press[pin] = now
        return True

    def _poll_input(self) -> None:
        """Poll all inputs once. Must be called in a tight loop."""
        # KEY2 = Lock/Unlock always works
        if self._debounced(PIN_KEY2):
            self.locked = not self.locked
            self._redraw_event.set()
            return

        # Everything else blocked when locked
        if self.locked:
            return

        if self.screen == SCREEN_STATUS:
            if self._debounced(PIN_KEY3):
                self.screen = SCREEN_CONFIG
                self._cfg_selection = 0
                self._redraw_event.set()
            elif self._debounced(PIN_JOY_UP) or self._debounced(PIN_JOY_DOWN):
                self.screen = SCREEN_RADAR
                self._redraw_event.set()

        elif self.screen == SCREEN_RADAR:
            if self._debounced(PIN_JOY_UP) or self._debounced(PIN_JOY_DOWN):
                self.screen = SCREEN_STATUS
                self._redraw_event.set()

        elif self.screen == SCREEN_CONFIG:
            if self._debounced(PIN_KEY1):
                self.screen = SCREEN_STATUS
                self._redraw_event.set()

            elif self._debounced(PIN_JOY_LEFT):
                if self._cfg_selection == CFG_THEME:
                    self._cycle_theme(-1)
                else:
                    self.screen = SCREEN_STATUS
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_UP):
                if self._cfg_selection > 0:
                    self._cfg_selection -= 1
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_DOWN):
                if self._cfg_selection < _CFG_COUNT - 1:
                    self._cfg_selection += 1
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_RIGHT):
                if self._cfg_selection == CFG_THEME:
                    self._cycle_theme(1)
                elif self._cfg_selection == CFG_POWER:
                    self.screen = SCREEN_POWER
                    self.power_selection = POWER_SHUTDOWN
                    self._redraw_event.set()
                elif self._cfg_selection == CFG_WIFI:
                    self._open_network_menu()
                elif self._cfg_selection == CFG_OWNSHIP:
                    self._open_ownship_menu()

            elif self._debounced(PIN_JOY_PRESS):
                if self._cfg_selection == CFG_POWER:
                    self.screen = SCREEN_POWER
                    self.power_selection = POWER_SHUTDOWN
                    self._redraw_event.set()
                elif self._cfg_selection == CFG_WIFI:
                    self._open_network_menu()
                elif self._cfg_selection == CFG_OWNSHIP:
                    self._open_ownship_menu()

        elif self.screen == SCREEN_POWER:
            if self._debounced(PIN_KEY1) or self._debounced(PIN_JOY_LEFT):
                # Back → return to config
                self.screen = SCREEN_CONFIG
                self._redraw_event.set()

            elif self._debounced(PIN_JOY_UP) or self._debounced(PIN_JOY_DOWN):
                # Toggle between shutdown / reboot
                self.power_selection = (
                    POWER_REBOOT if self.power_selection == POWER_SHUTDOWN
                    else POWER_SHUTDOWN
                )
                self._redraw_event.set()

            elif self._debounced(PIN_JOY_PRESS):
                self._execute_power_action()

        elif self.screen == SCREEN_NETWORK:
            if self._debounced(PIN_KEY1) or self._debounced(PIN_JOY_LEFT):
                # Back → return to config
                self.screen = SCREEN_CONFIG
                self._redraw_event.set()

            elif self._debounced(PIN_JOY_UP):
                if self._net_selection > 0:
                    self._net_selection -= 1
                    if self._net_selection < self._net_scroll:
                        self._net_scroll = self._net_selection
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_DOWN):
                if self._net_selection < len(self._net_list) - 1:
                    self._net_selection += 1
                    # 5 visible rows max
                    if self._net_selection >= self._net_scroll + 5:
                        self._net_scroll = self._net_selection - 4
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_PRESS):
                if self._net_list:
                    self._connect_to_selected_network()

        elif self.screen == SCREEN_OWNSHIP:
            if self._debounced(PIN_KEY1) or self._debounced(PIN_JOY_LEFT):
                self.screen = SCREEN_CONFIG
                self._redraw_event.set()

            elif self._debounced(PIN_JOY_UP):
                if self._own_selection > 0:
                    self._own_selection -= 1
                    if self._own_selection < self._own_scroll:
                        self._own_scroll = self._own_selection
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_DOWN):
                total = len(self._own_list) + (1 if self._own_has_lock else 0)
                if self._own_selection < total - 1:
                    self._own_selection += 1
                    if self._own_selection >= self._own_scroll + 5:
                        self._own_scroll = self._own_selection - 4
                    self._redraw_event.set()

            elif self._debounced(PIN_JOY_PRESS):
                self._select_ownship_aircraft()

    def _any_key_pressed(self) -> bool:
        """Return True if any button or joystick direction is pressed."""
        for pin in ALL_INPUT_PINS:
            if GPIO.input(pin) == 0:
                return True
        return False

    def _cycle_theme(self, direction: int) -> None:
        """Cycle theme left (-1) or right (+1), save, and apply."""
        current = self.cfg.get("display", "theme")
        idx = _THEME_OPTIONS.index(current) if current in _THEME_OPTIONS else 0
        idx = (idx + direction) % len(_THEME_OPTIONS)
        self.cfg.set("display", "theme", _THEME_OPTIONS[idx])
        self.cfg.save()
        self._apply_theme()
        self._redraw_event.set()

    def _open_network_menu(self) -> None:
        """Scan networks and open the network selection screen."""
        known = set(_get_known_networks())
        visible = _get_visible_ssids()
        ssid, _ = _get_wifi_info()
        # Show only networks that are both known and visible
        self._net_list = sorted(
            [(n, n == ssid) for n in visible & known],
            key=lambda item: (not item[1], item[0].lower()),
        )
        self._net_selection = 0
        self._net_scroll = 0
        self._net_connecting = False
        self.screen = SCREEN_NETWORK
        self._redraw_event.set()

    def _connect_to_selected_network(self) -> None:
        """Connect to the selected network, showing feedback on screen."""
        name, _connected = self._net_list[self._net_selection]

        self._net_connecting = True
        self._redraw_event.set()
        # Give render loop a moment to show "Connecting..." before blocking
        time.sleep(0.15)

        ok = _connect_network(name)

        self._net_connecting = False
        if ok:
            # Show brief success then return to status
            img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
            draw = ImageDraw.Draw(img)
            draw.text((10, 50), "Connected!", fill=COL_GREEN, font=self._font)
            draw.text((10, 68), name[:16], fill=COL_TEXT, font=self._font_sm)
            self.lcd.show_image(img)
            time.sleep(1.0)
            self.screen = SCREEN_STATUS
        else:
            img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
            draw = ImageDraw.Draw(img)
            draw.text((10, 50), "Failed!", fill=COL_RED, font=self._font)
            draw.text((10, 68), name[:16], fill=COL_TEXT, font=self._font_sm)
            self.lcd.show_image(img)
            time.sleep(1.5)
        self._redraw_event.set()

    def _open_ownship_menu(self) -> None:
        """Build aircraft list sorted by distance and open the ownship screen."""
        # Read ownship position for distance calculation
        own_lat, own_lon = None, None
        try:
            with open(OWNSHIP_JSON_PATH, "r") as f:
                own = json.load(f)
            if own.get("has_fix"):
                own_lat = own["lat"]
                own_lon = own["lon"]
        except Exception:
            pass

        # Read aircraft from readsb
        aircraft = []
        try:
            with open(READSB_JSON_PATH, "r") as f:
                data = json.load(f)
            for ac in data.get("aircraft", []):
                icao = ac.get("hex", "").strip().upper()
                if not icao:
                    continue
                lat = ac.get("lat")
                lon = ac.get("lon")
                if lat is None or lon is None:
                    continue
                # Prefer registration from DB, then flight callsign, then ICAO
                reg = self._lookup_registration(icao)
                callsign = (reg or ac.get("flight") or icao).strip()
                dist = None
                if own_lat is not None and own_lon is not None:
                    dist = self._haversine_nm(own_lat, own_lon, lat, lon)
                aircraft.append({
                    "icao": icao,
                    "callsign": callsign,
                    "dist_nm": dist,
                })
        except Exception:
            pass

        # Sort by distance (None = unknown → end of list)
        aircraft.sort(key=lambda a: a["dist_nm"] if a["dist_nm"] is not None else 9999)

        # If ownship override is active, prepend a "Reset" entry
        current_icao = self.cfg.get("ownship", "icao").strip()
        current_cs = self.cfg.get("ownship", "callsign").strip()
        self._own_has_lock = bool(current_icao)
        self._own_lock_label = current_cs or current_icao

        self._own_list = aircraft
        self._own_selection = 0
        self._own_scroll = 0
        self.screen = SCREEN_OWNSHIP
        self._redraw_event.set()

    def _select_ownship_aircraft(self) -> None:
        """Handle press on the ownship aircraft list."""
        if self._own_has_lock and self._own_selection == 0:
            # Reset — clear the lock
            self.cfg.set("ownship", "icao", "")
            self.cfg.set("ownship", "callsign", "")
            self.cfg.save()
            self.screen = SCREEN_CONFIG
            self._redraw_event.set()
            return

        # Offset: if lock is active, first row is Reset
        idx = self._own_selection - (1 if self._own_has_lock else 0)
        if 0 <= idx < len(self._own_list):
            ac = self._own_list[idx]
            self.cfg.set("ownship", "icao", ac["icao"])
            self.cfg.set("ownship", "callsign", ac["callsign"])
            self.cfg.save()

            # Brief confirmation
            img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
            draw = ImageDraw.Draw(img)
            draw.text((10, 45), "Ownship set:", fill=COL_GREEN, font=self._font)
            draw.text((10, 63), ac["callsign"][:12], fill=COL_TEXT, font=self._font_sm)
            self.lcd.show_image(img)
            time.sleep(1.0)

            self.screen = SCREEN_STATUS
            self._redraw_event.set()

    @staticmethod
    def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in nautical miles."""
        lat1, lon1, lat2, lon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * math.asin(math.sqrt(a)) * 3440.065

    @staticmethod
    def _lookup_registration(icao: str) -> str:
        """Look up aircraft registration by ICAO hex from the SQLite DB."""
        if not os.path.exists(AIRCRAFT_DB_PATH):
            return ""
        try:
            db = sqlite3.connect(AIRCRAFT_DB_PATH)
            db.execute("PRAGMA query_only = ON")
            row = db.execute(
                "SELECT reg FROM aircraft WHERE icao = ?", (icao.upper(),)
            ).fetchone()
            db.close()
            return row[0] if row and row[0] else ""
        except Exception:
            return ""

    def _execute_power_action(self) -> None:
        """Show countdown then shutdown or reboot."""
        action = "Shutting down" if self.power_selection == POWER_SHUTDOWN else "Rebooting"
        cmd = "poweroff" if self.power_selection == POWER_SHUTDOWN else "reboot"

        # Block render loop from overwriting countdown frames
        self._countdown_active = True

        # Wait for joystick release before starting countdown
        while GPIO.input(PIN_JOY_PRESS) == 0:
            time.sleep(0.05)
        time.sleep(0.3)

        for remaining in range(COUNTDOWN_SECS, 0, -1):
            img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
            draw = ImageDraw.Draw(img)
            draw.text((10, 40), f"{action}",
                      fill=COL_YELLOW, font=self._font)
            draw.text((10, 57), f"in {remaining}...",
                      fill=COL_TEXT, font=self._font)
            draw.text((5, 100), "Press any key to cancel",
                      fill=COL_GREY, font=self._font_xs)
            self.lcd.show_image(img)

            # Allow cancel during countdown — any key aborts
            t0 = time.time()
            while time.time() - t0 < 1.0:
                if self._any_key_pressed():
                    # Wait for all keys to release so the input loop
                    # doesn't process the same press as a new action.
                    while self._any_key_pressed():
                        time.sleep(0.05)
                    self._countdown_active = False
                    self.screen = SCREEN_STATUS
                    self._redraw_event.set()
                    return
                time.sleep(0.05)

        # Show final message
        img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
        draw = ImageDraw.Draw(img)
        msg = "Shutting down..." if cmd == "poweroff" else "Rebooting..."
        draw.text((10, 55), msg, fill=COL_RED, font=self._font)
        self.lcd.show_image(img)
        time.sleep(0.5)

        os.system(f"sudo {cmd}")

    # ── screen rendering ───────────────────────────────────────────────────

    # Soft key strip width on the right edge
    _SK_W = 18
    _CONTENT_W = LCD_WIDTH - _SK_W  # usable width for content

    # ── icon drawing helpers ───────────────────────────────────────────────

    @staticmethod
    def _draw_wifi_icon(draw: ImageDraw.ImageDraw, x: int, y: int,
                        connected: bool, font: Any) -> None:
        """Draw WiFi status using Unicode glyph."""
        col = COL_GREEN if connected else COL_RED
        draw.text((x, y), "\u25C9" if connected else "\u25CE", fill=col, font=font)

    @staticmethod
    def _draw_status_dot(draw: ImageDraw.ImageDraw, x: int, y: int,
                         active: bool, font: Any) -> None:
        """Draw status indicator using Unicode glyph."""
        if active:
            draw.text((x, y), "\u2714", fill=COL_GREEN, font=font)
        else:
            draw.text((x, y), "\u2718", fill=COL_RED, font=font)

    @staticmethod
    def _draw_temp_icon(draw: ImageDraw.ImageDraw, x: int, y: int,
                        font: Any) -> None:
        """Draw temperature icon using Unicode glyph."""
        draw.text((x, y), "\U0001F321", fill=COL_RED, font=font)

    @staticmethod
    def _draw_clock_icon(draw: ImageDraw.ImageDraw, x: int, y: int,
                         font: Any) -> None:
        """Draw uptime icon using Unicode glyph."""
        draw.text((x, y), "\u23F1", fill=COL_TEXT, font=font)

    # ── soft key strip (right edge, vertical) ──────────────────────────────

    def _draw_softkeys(self, draw: ImageDraw.ImageDraw,
                       k1: str, k2: str, k3: str) -> None:
        """Draw the soft key strip on the right edge."""
        x0 = self._CONTENT_W
        strip_h = LCD_HEIGHT // 3

        # Background strip
        draw.rectangle([(x0, 0), (LCD_WIDTH - 1, LCD_HEIGHT - 1)],
                       fill=COL_SOFTKEY_BG)
        # Dividers
        draw.line([(x0, strip_h), (LCD_WIDTH, strip_h)], fill=COL_GREY)
        draw.line([(x0, strip_h * 2), (LCD_WIDTH, strip_h * 2)], fill=COL_GREY)

        for i, key_fn in enumerate([k1, k2, k3]):
            if not key_fn:
                continue
            cx = x0 + self._SK_W // 2
            cy = i * strip_h + strip_h // 2
            if key_fn == "back":
                draw.text((cx - 5, cy - 6), "\u2190", fill=(255, 255, 255),
                          font=self._icon_font)
            elif key_fn == "lock":
                col = COL_RED if self.locked else COL_GREEN
                draw.text((cx - 5, cy - 6),
                          "\U0001F512" if self.locked else "\U0001F513",
                          fill=col, font=self._icon_font)
            elif key_fn == "power":
                draw.text((cx - 5, cy - 6), "\u23FB", fill=(255, 255, 255),
                          font=self._icon_font)
            elif key_fn == "wifi":
                draw.text((cx - 5, cy - 6), "\U0001F310", fill=(255, 255, 255),
                          font=self._icon_font)
            elif key_fn == "config":
                draw.text((cx - 5, cy - 6), "\u2699", fill=(255, 255, 255),
                          font=self._icon_font)

    # ── section header ─────────────────────────────────────────────────────

    @staticmethod
    def _draw_section(draw: ImageDraw.ImageDraw, y: int, label: str,
                      font: Any, max_w: int) -> int:
        """Draw a centered section header with side lines. Returns new y."""
        tw = font.getlength(label) if hasattr(font, 'getlength') else len(label) * 6
        tx = int((max_w - tw) / 2)
        line_y = y + 6
        draw.line([(2, line_y), (tx - 4, line_y)], fill=COL_SECTION)
        draw.text((tx, y), label, fill=COL_SECTION, font=font)
        draw.line([(tx + int(tw) + 4, line_y), (max_w - 2, line_y)],
                  fill=COL_SECTION)
        return y + 14

    # ── status screen ──────────────────────────────────────────────────────

    def _draw_status(self, draw: ImageDraw.ImageDraw) -> None:
        cw = self._CONTENT_W
        y = 1

        # ── NETWORK ──
        y = self._draw_section(draw, y, "NETWORK", self._font_sm, cw)

        ssid, ip = _get_wifi_info()
        self._draw_wifi_icon(draw, 3, y, bool(ssid), self._icon_font_sm)
        text_x = 15  # glyph width + gap
        if ssid:
            # Truncate only if SSID doesn't fit
            avail_w = cw - text_x - 2
            display_ssid = ssid
            while len(display_ssid) > 1:
                tw = self._font_sm.getlength(display_ssid) if hasattr(self._font_sm, 'getlength') else len(display_ssid) * 6 # type: ignore
                if tw <= avail_w:
                    break
                display_ssid = display_ssid[:-1]
            draw.text((text_x, y + 1), display_ssid, fill=COL_GREEN, font=self._font_sm)
        else:
            draw.text((text_x, y + 1), "No WiFi", fill=COL_RED, font=self._font_sm)
        y += 14

        if ip:
            draw.text((3, y), f"IP:{ip}", fill=COL_TEXT, font=self._font_xs)
        else:
            draw.text((3, y), "IP: --", fill=COL_GREY, font=self._font_xs)
        y += 14

        # ── RX / TX ──
        y = self._draw_section(draw, y, "RX / TX", self._font_sm, cw)

        # Row 1: ADS-B (left) + GPS (right)
        self._draw_status_dot(draw, 3, y, self.mon_adsb.active, self._icon_font_sm)
        draw.text((14, y), "ADS-B", fill=COL_TEXT, font=self._font_sm)

        # GPS: show pause icon if ownship override is active
        own_icao = self.cfg.get("ownship", "icao").strip()
        if own_icao:
            draw.text((56, y), "\u23F8", fill=COL_YELLOW, font=self._icon_font_sm)
            draw.text((67, y), "GPS", fill=COL_TEXT, font=self._font_sm)
        else:
            self._draw_status_dot(draw, 56, y, self.mon_gps.active, self._icon_font_sm)
            draw.text((67, y), "GPS", fill=COL_TEXT, font=self._font_sm)
        y += 14

        # Row 2: GDL90 (left) + Ownship aircraft (right, only when override active)
        self._draw_status_dot(draw, 3, y, self.mon_gdl90.active, self._icon_font_sm)
        draw.text((14, y), "GDL90", fill=COL_TEXT, font=self._font_sm)
        if own_icao:
            own_cs = self.cfg.get("ownship", "callsign").strip() or own_icao
            draw.text((56, y), "\u2708", fill=COL_GREEN, font=self._icon_font_sm)
            draw.text((67, y), own_cs[:6], fill=COL_TEXT, font=self._font_sm)
        y += 14

        # ── SYSTEM ──
        y = self._draw_section(draw, y, "SYSTEM", self._font_sm, cw)

        temp = _get_cpu_temp()
        cpu_str, self._cpu_prev = _get_cpu_percent(self._cpu_prev)
        # Row 1: temp + CPU%
        self._draw_temp_icon(draw, 3, y, self._icon_font_sm)
        draw.text((14, y + 1), temp, fill=COL_TEXT, font=self._font_sm)
        draw.text((52, y), "CPU", fill=COL_GREY, font=self._font_sm)
        draw.text((74, y), cpu_str, fill=COL_TEXT, font=self._font_sm)
        y += 14

        up = _get_uptime()
        ram_str = _get_ram_percent()
        # Row 2: uptime + RAM%
        self._draw_clock_icon(draw, 3, y, self._icon_font_sm)
        draw.text((14, y + 1), up, fill=COL_TEXT, font=self._font_sm)
        draw.text((52, y), "RAM", fill=COL_GREY, font=self._font_sm)
        draw.text((74, y), ram_str, fill=COL_TEXT, font=self._font_sm)

        # Throttle warning (conditional, only when active)
        throttle = _get_throttle_status()
        if throttle:
            y += 14
            draw.text((3, y), f"\u26a0 {throttle}", fill=COL_YELLOW, font=self._font_sm)

        # Soft keys: (none) | lock | config
        self._draw_softkeys(draw, "", "lock", "config")

    # ── config menu ────────────────────────────────────────────────────────

    def _draw_config_menu(self, draw: ImageDraw.ImageDraw) -> None:
        cw = self._CONTENT_W

        # Title
        draw.text((15, 20), "Config", fill=COL_YELLOW, font=self._font)
        draw.line([(5, 36), (cw - 5, 36)], fill=COL_GREY)

        # Theme inline toggle — sun for light, moon for dark
        theme_val = self.cfg.get("display", "theme")
        theme_icon = "\u263E" if theme_val == "dark" else "\u2600"

        items = [
            ("Theme", f"< {theme_icon} >"),
            ("Ownship", ">"),
            ("WiFi", ">"),
            ("Power", ">"),
        ]

        for i, (label, suffix) in enumerate(items):
            y = 42 + i * 20
            selected = i == self._cfg_selection
            if selected:
                draw.rectangle([(4, y - 2), (cw - 4, y + 16)],
                               fill=COL_HIGHLIGHT)
                draw.text((10, y), f"> {label}", fill=COL_TEXT, font=self._font)
            else:
                draw.text((10, y), f"  {label}", fill=COL_GREY, font=self._font)
            # Right-aligned suffix (use icon font for theme row)
            sfont = self._icon_font_sm if i == CFG_THEME else self._font_sm
            sw = sfont.getlength(suffix) if hasattr(sfont, 'getlength') else len(suffix) * 6 # type: ignore
            draw.text((int(cw - sw - 6), y + 2), suffix,
                      fill=COL_TEXT if selected else COL_GREY,
                      font=sfont)

        # Soft keys: back | lock | (none)
        self._draw_softkeys(draw, "back", "lock", "")

    # ── power menu ─────────────────────────────────────────────────────────

    def _draw_power_menu(self, draw: ImageDraw.ImageDraw) -> None:
        cw = self._CONTENT_W

        # Title
        draw.text((15, 20), "Power", fill=COL_YELLOW, font=self._font)
        draw.line([(5, 36), (cw - 5, 36)], fill=COL_GREY)

        opts = ["Shutdown", "Reboot"]
        for i, label in enumerate(opts):
            y = 48 + i * 25
            if i == self.power_selection:
                draw.rectangle([(4, y - 2), (cw - 4, y + 16)],
                               fill=COL_HIGHLIGHT)
                draw.text((10, y), f"> {label}", fill=COL_TEXT, font=self._font)
            else:
                draw.text((10, y), f"  {label}", fill=COL_GREY, font=self._font)

        # Hint
        draw.text((4, 108), "Press to confirm", fill=COL_GREY, font=self._font_sm)

        # Soft keys: back | lock | (none)
        self._draw_softkeys(draw, "back", "lock", "")

    # ── network menu ───────────────────────────────────────────────────────

    def _draw_network_menu(self, draw: ImageDraw.ImageDraw) -> None:
        cw = self._CONTENT_W

        # Title
        draw.text((10, 2), "WiFi Networks", fill=COL_YELLOW, font=self._font)
        draw.line([(5, 17), (cw - 5, 17)], fill=COL_GREY)

        if self._net_connecting:
            name = self._net_list[self._net_selection][0] if self._net_list else ""
            draw.text((10, 50), "Connecting...", fill=COL_YELLOW, font=self._font)
            draw.text((10, 68), name[:16], fill=COL_TEXT, font=self._font_sm)
            self._draw_softkeys(draw, "back", "lock", "")
            return

        if not self._net_list:
            draw.text((10, 50), "No networks", fill=COL_GREY, font=self._font)
            draw.text((10, 66), "found", fill=COL_GREY, font=self._font)
            self._draw_softkeys(draw, "back", "lock", "")
            return

        # Scrollable list — 5 visible rows
        max_visible = 5
        row_h = 18
        y_start = 21
        end = min(self._net_scroll + max_visible, len(self._net_list))

        # Scroll indicator top
        if self._net_scroll > 0:
            draw.text((cw - 15, y_start - 2), "\u25B2", fill=COL_GREY,
                      font=self._font_xs)

        for idx in range(self._net_scroll, end):
            name, connected = self._net_list[idx]
            y = y_start + (idx - self._net_scroll) * row_h
            selected = idx == self._net_selection

            # Truncate name to fit
            display_name = name
            max_w = cw - 22
            while len(display_name) > 1:
                tw = self._font_sm.getlength(display_name) if hasattr( # type: ignore
                    self._font_sm, 'getlength') else len(display_name) * 6
                if tw <= max_w:
                    break
                display_name = display_name[:-1]

            if selected:
                draw.rectangle([(2, y - 1), (cw - 2, y + row_h - 3)],
                               fill=COL_HIGHLIGHT)
                prefix = ">"
            else:
                prefix = " "

            col = COL_GREEN if connected else COL_TEXT
            draw.text((4, y), prefix, fill=COL_TEXT, font=self._font_sm)
            draw.text((12, y), display_name, fill=col, font=self._font_sm)

        # Scroll indicator bottom
        if end < len(self._net_list):
            draw.text((cw - 15, y_start + max_visible * row_h - 4),
                      "\u25BC", fill=COL_GREY, font=self._font_xs)

        # Hint
        draw.text((4, 114), "Green = connected",
                  fill=COL_GREY, font=self._font_xs)

        # Soft keys: back | lock | (none)
        self._draw_softkeys(draw, "back", "lock", "")

    # ── ownship aircraft screen ──────────────────────────────────────────

    def _draw_ownship_menu(self, draw: ImageDraw.ImageDraw) -> None:
        cw = self._CONTENT_W

        # Title
        draw.text((10, 2), "Ownship", fill=COL_YELLOW, font=self._font)
        draw.line([(5, 17), (cw - 5, 17)], fill=COL_GREY)

        # Build display list: optionally "Reset" row + aircraft
        rows: list[tuple[str, str, tuple]] = []  # (left_text, right_text, colour)
        if self._own_has_lock:
            rows.append(
                (f"\u2718 {self._own_lock_label[:10]}", "Reset", COL_RED)
            )

        for ac in self._own_list:
            cs = ac["callsign"][:8]
            if ac["dist_nm"] is not None:
                dist_str = f"{ac['dist_nm']:.1f}nm"
            else:
                dist_str = ""
            rows.append((cs, dist_str, COL_TEXT))

        if not rows:
            draw.text((10, 50), "No aircraft", fill=COL_GREY, font=self._font)
            self._draw_softkeys(draw, "back", "lock", "")
            return

        max_visible = 5
        row_h = 18
        y_start = 21
        end = min(self._own_scroll + max_visible, len(rows))

        if self._own_scroll > 0:
            draw.text((cw - 15, y_start - 2), "\u25B2", fill=COL_GREY,
                      font=self._font_xs)

        for idx in range(self._own_scroll, end):
            left, right, col = rows[idx]
            y = y_start + (idx - self._own_scroll) * row_h
            selected = idx == self._own_selection

            if selected:
                draw.rectangle([(2, y - 1), (cw - 2, y + row_h - 3)],
                               fill=COL_HIGHLIGHT)

            prefix = ">" if selected else " "
            draw.text((4, y), prefix, fill=COL_TEXT, font=self._font_sm)
            draw.text((12, y), left, fill=col, font=self._font_sm)
            # Right-align distance
            if right:
                rw = self._font_xs.getlength(right) if hasattr(self._font_xs, 'getlength') else len(right) * 6 # type: ignore
                draw.text((int(cw - rw - 4), y + 1), right,
                          fill=COL_GREY if col != COL_RED else COL_RED,
                          font=self._font_xs)

        if end < len(rows):
            draw.text((cw - 15, y_start + max_visible * row_h - 4),
                      "\u25BC", fill=COL_GREY, font=self._font_xs)

        draw.text((4, 114), "Press to select",
                  fill=COL_GREY, font=self._font_xs)

        self._draw_softkeys(draw, "back", "lock", "")

    # ── radar screen ─────────────────────────────────────────────────────────────────

    _DIAMOND_SIZE = 3                    # half-size of traffic diamond

    _BAND_COLOURS: dict[str, tuple] = {
        "red": COL_BAND_RED,
        "yellow": COL_BAND_YELLOW,
        "white": COL_TEXT,
    }

    def _read_radar_json(self) -> dict | None:
        try:
            with open(RADAR_JSON_PATH, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _draw_radar(self, draw: ImageDraw.ImageDraw) -> None:
        cx = LCD_WIDTH // 2
        cy = LCD_HEIGHT // 2
        r = 60                               # usable radius in pixels
        ring_r = r // 2                      # 5 nm ring at half-radius
        ds = self._DIAMOND_SIZE

        # Range ring at 5 nm (half of 10 nm full radius)
        draw.ellipse(
            [(cx - ring_r, cy - ring_r), (cx + ring_r, cy + ring_r)],
            outline=COL_RADAR_RING,
        )
        # Outer boundary (10 nm)
        draw.ellipse(
            [(cx - r, cy - r), (cx + r, cy + r)],
            outline=COL_RADAR_RING,
        )

        # Cross-hair lines
        draw.line([(cx, cy - r), (cx, cy + r)], fill=COL_RADAR_RING)
        draw.line([(cx - r, cy), (cx + r, cy)], fill=COL_RADAR_RING)

        # Range labels
        draw.text((cx + ring_r + 1, cy - 4), "5", fill=COL_GREY, font=self._font_xs)
        draw.text((cx + r + 1, cy - 4), "10", fill=COL_GREY, font=self._font_xs)

        # North indicator
        draw.text((cx - 3, 1), "N", fill=COL_GREY, font=self._font_xs)

        radar = self._read_radar_json()
        if radar is None or not radar.get("has_fix"):
            draw.text((cx - 25, cy - 5), "NO GPS", fill=COL_RED, font=self._font)
            return

        own_track = radar.get("ownship_track") or 0.0

        # Ownship marker (small filled circle)
        draw.ellipse(
            [(cx - 2, cy - 2), (cx + 2, cy + 2)],
            fill=COL_RADAR_OWN,
        )

        # Traffic diamonds
        for t in radar.get("traffic", []):
            bearing = t.get("bearing", 0.0)
            dist_nm = t.get("dist_nm", 0.0)
            band = t.get("band", "white")

            # Track-up rotation: subtract ownship track
            rel_bearing = math.radians(bearing - own_track)

            # Project to pixel coordinates (north = up = -y)
            px_dist = (dist_nm / 10.0) * r
            tx = cx + px_dist * math.sin(rel_bearing)
            ty = cy - px_dist * math.cos(rel_bearing)

            # Clamp to display bounds
            tx = max(ds, min(LCD_WIDTH - 1 - ds, tx))
            ty = max(ds, min(LCD_HEIGHT - 1 - ds, ty))

            col = self._BAND_COLOURS.get(band, COL_TEXT)
            ix, iy = int(tx), int(ty)

            # Diamond shape (4 points)
            diamond = [(ix, iy - ds), (ix + ds, iy), (ix, iy + ds), (ix - ds, iy)]
            draw.polygon(diamond, fill=col)

            # Altitude diff label: e.g. "+2,1" or "-1,8"
            alt_diff = t.get("alt_diff_ft", 0)
            alt_k = abs(alt_diff) / 1000.0
            sign = "+" if alt_diff >= 0 else "-"
            # European format: comma as decimal separator, 1 decimal
            alt_label = f"{sign}{alt_k:.1f}".replace(".", ",")
            draw.text((ix + ds + 2, iy - 5), alt_label,
                      fill=col, font=self._font_xs)

        # Lock indicator — small icon at bottom-right corner
        if self.locked:
            draw.text((LCD_WIDTH - 12, LCD_HEIGHT - 12), "\U0001F512",
                      fill=COL_RED, font=self._icon_font_sm)

    def _render(self) -> None:
        if self._countdown_active:
            return

        img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), COL_BG)
        draw = ImageDraw.Draw(img)

        if self.screen == SCREEN_STATUS:
            self._draw_status(draw)
        elif self.screen == SCREEN_CONFIG:
            self._draw_config_menu(draw)
        elif self.screen == SCREEN_POWER:
            self._draw_power_menu(draw)
        elif self.screen == SCREEN_NETWORK:
            self._draw_network_menu(draw)
        elif self.screen == SCREEN_OWNSHIP:
            self._draw_ownship_menu(draw)
        elif self.screen == SCREEN_RADAR:
            self._draw_radar(draw)

        self._last_frame = img
        self.lcd.show_image(img)

    def save_screenshot(self) -> None:
        """Save the last rendered frame to /tmp/adsb-display-screenshot.png."""
        if self._last_frame is not None:
            path = "/tmp/adsb-display-screenshot.png"
            self._last_frame.save(path)
            log.info("Screenshot saved to %s", path)

    # ── main loops ─────────────────────────────────────────────────────────

    def _input_loop(self) -> None:
        """Runs in a thread — polls GPIO at ~50 Hz."""
        while self._running:
            self._poll_input()
            time.sleep(0.02)

    def run(self) -> None:
        """Entry point — init hardware, start threads, render loop."""
        if not _HAS_HW:
            print("ERROR: RPi.GPIO / spidev not available. Run on a Raspberry Pi.")
            return

        log.info("Starting ADS-B display driver")
        self.lcd.init()
        self.lcd.test_pattern()
        self.lcd.clear()
        log.info("Display cleared — entering main loop")

        # Start flow monitors
        self.mon_adsb.start()
        self.mon_gps.start()
        self.mon_gdl90.start()

        # Start input thread
        input_thread = threading.Thread(target=self._input_loop, daemon=True)
        input_thread.start()

        try:
            while self._running:
                self._render()
                # Wait up to REFRESH_INTERVAL, but wake early on input
                self._redraw_event.wait(timeout=REFRESH_INTERVAL)
                self._redraw_event.clear()
        except KeyboardInterrupt:
            pass
        finally:
            self.mon_adsb.stop()
            self.mon_gps.stop()
            self.mon_gdl90.stop()
            self.lcd.clear()
            self.lcd.cleanup()


# ---------------------------------------------------------------------------
# Graceful signal handling for systemd
# ---------------------------------------------------------------------------

_app: DisplayApp | None = None


def _signal_handler(sig, frame):
    if _app:
        _app._running = False


def _screenshot_handler(sig, frame):
    if _app:
        _app.save_screenshot()


def main() -> None:
    global _app
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGUSR1, _screenshot_handler) # type: ignore
    _app = DisplayApp()
    _app.run()


if __name__ == "__main__":
    main()
