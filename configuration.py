"""
ADS-Pi shared configuration
============================
Reads and writes /etc/ads-pi.conf (INI format).
Used by display, radar, and GDL90 services.
"""

import configparser
import logging
import os

log = logging.getLogger("ads-pi-config")

CONFIG_PATH = "/etc/ads-pi.conf"

_DEFAULTS = {
    "display": {
        "theme": "dark",
    },
    "ownship": {
        "icao": "",
        "callsign": "",
    },
}


class Config:

    def __init__(self, path: str = CONFIG_PATH) -> None:
        self._path = path
        self._cp = configparser.ConfigParser()
        # Populate defaults
        for section, values in _DEFAULTS.items():
            self._cp[section] = values
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                self._cp.read(self._path)
                log.info("Config loaded from %s", self._path)
            except Exception:
                log.exception("Failed to read %s, using defaults", self._path)
        else:
            log.info("Config file %s not found, using defaults", self._path)

    def get(self, section: str, key: str, *, reload: bool = False) -> str:
        if reload:
            self._load()
        return self._cp.get(section, key, fallback=_DEFAULTS.get(section, {}).get(key, ""))

    def set(self, section: str, key: str, value: str) -> None:
        if not self._cp.has_section(section):
            self._cp.add_section(section)
        self._cp.set(section, key, value)

    def save(self) -> None:
        try:
            with open(self._path, "w") as f:
                self._cp.write(f)
            log.info("Config saved to %s", self._path)
        except PermissionError:
            log.warning("Permission denied writing %s — running without save", self._path)
        except Exception:
            log.exception("Failed to save config to %s", self._path)


# ---------------------------------------------------------------------------
# Theme colour palettes  (RGB tuples for PIL)
# ---------------------------------------------------------------------------
PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "dark": {
        "bg":        (0, 0, 0),
        "text":      (255, 255, 255),
        "green":     (0, 200, 0),
        "red":       (255, 60, 60),
        "yellow":    (255, 200, 0),
        "grey":      (120, 120, 120),
        "highlight": (50, 50, 180),
        "surface":   (40, 40, 40),
        "ring":      (50, 50, 50),
        "own":       (0, 180, 255),
    },
    "light": {
        "bg":        (255, 255, 255),
        "text":      (0, 0, 0),
        "green":     (0, 160, 0),
        "red":       (200, 40, 40),
        "yellow":    (210, 120, 0),
        "grey":      (130, 130, 130),
        "highlight": (140, 160, 220),
        "surface":   (215, 215, 215),
        "ring":      (200, 200, 200),
        "own":       (0, 120, 200),
    },
}


def get_palette(theme: str) -> dict[str, tuple[int, int, int]]:
    """Return the colour palette for the given theme name."""
    return PALETTES.get(theme, PALETTES["dark"])
