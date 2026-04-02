"""SoC temperature (RPi) and optional Hailo thermal if sysfs exists."""

from __future__ import annotations

from pathlib import Path


def read_soc_temp_c() -> float | None:
    p = Path("/sys/class/thermal/thermal_zone0/temp")
    if not p.is_file():
        return None
    try:
        raw = int(p.read_text().strip())
        return raw / 1000.0
    except (OSError, ValueError):
        return None


def read_hailo_temp_c() -> float | None:
    for candidate in (
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
        Path("/sys/class/thermal/thermal_zone1/temp"),
    ):
        if candidate.is_file():
            try:
                raw = int(candidate.read_text().strip())
                return raw / 1000.0 if raw > 200 else float(raw)
            except (OSError, ValueError):
                continue
    return None
