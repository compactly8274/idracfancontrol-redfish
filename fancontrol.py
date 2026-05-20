#!/usr/bin/env python3
"""iDRAC 9 Redfish temperature monitor — GPU-aware, pure Redfish."""

import os
import signal
import time
import logging
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import pynvml
    PYNVML_AVAILABLE = True
except ImportError:
    PYNVML_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

IDRAC_HOST = os.environ["IDRAC_HOST"]
IDRAC_USER = os.environ.get("IDRAC_USER", "root")
IDRAC_PASS = os.environ["IDRAC_PASS"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
GPU_MONITORING = os.environ.get("GPU_MONITORING", "true").lower() == "true"
WARN_TEMP = float(os.environ.get("WARN_TEMP", "70"))
CRITICAL_TEMP = float(os.environ.get("CRITICAL_TEMP", "85"))

BASE_URL = f"https://{IDRAC_HOST}/redfish/v1"
THERMAL_URI = f"{BASE_URL}/Chassis/System.Embedded.1/Thermal"

SESSION = requests.Session()
SESSION.verify = False
SESSION.auth = (IDRAC_USER, IDRAC_PASS)

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Shutdown signal received")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def get_idrac_temps():
    try:
        resp = SESSION.get(THERMAL_URI, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [
            (s.get("Name", "Unknown"), s["ReadingCelsius"])
            for s in data.get("Temperatures", [])
            if s.get("ReadingCelsius") is not None
            and s.get("Status", {}).get("State") == "Enabled"
        ]
    except Exception as e:
        log.error(f"Failed to read iDRAC temps: {e}")
        return []


def get_fan_readings():
    try:
        resp = SESSION.get(THERMAL_URI, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [
            (f.get("FanName") or f.get("Name", "Fan"), f["Reading"])
            for f in data.get("Fans", [])
            if f.get("Reading") is not None
            and f.get("Status", {}).get("State") == "Enabled"
        ]
    except Exception:
        return []


def init_gpu():
    if not GPU_MONITORING or not PYNVML_AVAILABLE:
        return
    try:
        pynvml.nvmlInit()
        names = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            names.append(name.decode() if isinstance(name, bytes) else name)
        log.info(f"GPU monitoring active: {names}")
    except Exception as e:
        log.warning(f"GPU init failed: {e}")


def get_gpu_temps():
    if not GPU_MONITORING or not PYNVML_AVAILABLE:
        return []
    try:
        pynvml.nvmlInit()
        temps = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            temps.append((name.decode() if isinstance(name, bytes) else name, temp))
        return temps
    except Exception as e:
        log.warning(f"GPU temp read failed: {e}")
        return []


def log_temp(name, temp):
    if temp >= CRITICAL_TEMP:
        log.error(f"CRITICAL {name}: {temp:.1f}°C (threshold: {CRITICAL_TEMP}°C)")
    elif temp >= WARN_TEMP:
        log.warning(f"HIGH     {name}: {temp:.1f}°C (threshold: {WARN_TEMP}°C)")
    else:
        log.info(f"         {name}: {temp:.1f}°C")


def main():
    init_gpu()
    log.info(f"iDRAC temperature monitor started — host: {IDRAC_HOST}")
    log.info(f"Poll interval: {POLL_INTERVAL}s | Warn: {WARN_TEMP}°C | Critical: {CRITICAL_TEMP}°C")

    while not _shutdown:
        try:
            for name, temp in get_idrac_temps():
                log_temp(name, temp)
            for name, temp in get_gpu_temps():
                log_temp(name, temp)
            for name, rpm in get_fan_readings():
                log.info(f"         {name}: {rpm} RPM")
        except Exception as e:
            log.error(f"Monitor loop error: {e}")

        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
