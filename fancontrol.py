#!/usr/bin/env python3
"""iDRAC 9 Redfish fan controller — GPU-aware, pure Redfish, no IPMI."""

import os
import signal
import time
import logging
import yaml
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
FAN_CURVE_FILE = os.environ.get("FAN_CURVE_FILE", "/config/fan_curve.yaml")
GPU_MONITORING = os.environ.get("GPU_MONITORING", "true").lower() == "true"

BASE_URL = f"https://{IDRAC_HOST}/redfish/v1"
THERMAL_URI = f"{BASE_URL}/Chassis/System.Embedded.1/Thermal"
ATTRS_URI = f"{BASE_URL}/Managers/iDRAC.Embedded.1/Attributes"

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


def load_fan_curve(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    curve = [(float(p["temp"]), int(p["fan_pct"])) for p in data["fan_curve"]]
    curve.sort(key=lambda x: x[0])
    return curve


def interpolate(curve, temp):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, f0 = curve[i]
        t1, f1 = curve[i + 1]
        if t0 <= temp <= t1:
            return int(f0 + (f1 - f0) * (temp - t0) / (t1 - t0))
    return curve[-1][1]


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


def patch_attrs(attrs: dict) -> bool:
    try:
        resp = SESSION.patch(ATTRS_URI, json={"Attributes": attrs}, timeout=10)
        if resp.status_code in (200, 202, 204):
            return True
        log.warning(f"Attribute PATCH {list(attrs)} returned HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        log.warning(f"Attribute PATCH failed: {e}")
        return False


def suppress_pcie_fan_pin():
    # Try known attribute names — varies by firmware revision
    candidates = [
        {"ThermalSettings.1.PCISlotLFMEnable": "Disabled"},
        {"ThermalSettings.1.ThirdPartyPCIFanResponse": "Default"},
    ]
    for attrs in candidates:
        if patch_attrs(attrs):
            log.info(f"PCIe fan suppression applied via {list(attrs)}")
            return
    log.warning(
        "PCIe fan suppression via Redfish not supported on this firmware.\n"
        "If fans are pinned at 100%, set 'Third Party PCIe Card Default Cooling Response'\n"
        "to Default in the iDRAC web UI: Configuration > System Settings > Hardware Settings > Fan Settings"
    )


def enable_custom_thermal():
    """Switch iDRAC to Custom thermal profile so minimum fan speed is honoured."""
    return patch_attrs({"ThermalSettings.1.ThermalProfile": "Custom"})


def set_fan_speed(fan_pct: int) -> bool:
    """Set minimum fan speed percentage via iDRAC Attributes."""
    return patch_attrs({"ThermalSettings.1.MinimumFanSpeed": fan_pct})


def restore_auto_control():
    log.info("Restoring iDRAC automatic fan control (PerformancePerWatt DAPC)")
    patch_attrs({"ThermalSettings.1.ThermalProfile": "PerformancePerWatt(DAPC)"})


def main():
    curve = load_fan_curve(FAN_CURVE_FILE)
    init_gpu()

    log.info(f"iDRAC fan controller started — host: {IDRAC_HOST}")
    log.info(f"Poll interval: {POLL_INTERVAL}s | GPU monitoring: {GPU_MONITORING and PYNVML_AVAILABLE}")
    log.info(f"Fan curve: {curve}")

    suppress_pcie_fan_pin()

    if not enable_custom_thermal():
        log.warning(
            "Could not switch to Custom thermal profile — fan speed override may not take effect.\n"
            "Continuing anyway; check your iDRAC firmware version."
        )

    last_pct = None

    while not _shutdown:
        try:
            idrac_temps = get_idrac_temps()
            gpu_temps = get_gpu_temps()

            idrac_max = max((t for _, t in idrac_temps), default=0.0)
            gpu_max = max((t for _, t in gpu_temps), default=0.0)
            system_max = max(idrac_max, gpu_max)
            driver = "GPU" if gpu_max > idrac_max else "iDRAC"

            log.info(f"  iDRAC max: {idrac_max:.1f}°C")
            if gpu_temps:
                log.info(f"  GPU max: {gpu_max:.1f}°C")

            for name, rpm in get_fan_readings()[:3]:
                log.info(f"  {name}: {rpm} RPM")

            target_pct = interpolate(curve, system_max)
            if target_pct != last_pct:
                log.info(f"Setting fan speed: {target_pct}% (temp={system_max:.1f}°C, driver={driver})")
                if set_fan_speed(target_pct):
                    last_pct = target_pct
                else:
                    log.warning(
                        "Fan speed PATCH failed — fans remain under iDRAC control.\n"
                        "Verify your iDRAC firmware supports ThermalSettings.1.MinimumFanSpeed."
                    )

        except Exception as e:
            log.error(f"Control loop error: {e}")

        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    restore_auto_control()


if __name__ == "__main__":
    main()
