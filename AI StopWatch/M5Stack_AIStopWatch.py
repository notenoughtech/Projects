import time
import math
import socket
import select
import network
import os

import M5
from M5 import *

try:
    import ujson as json
except ImportError:
    import json

try:
    import requests2
except ImportError:
    requests2 = None

try:
    from umqtt import MQTTClient
except ImportError:
    MQTTClient = None

try:
    import machine
except ImportError:
    machine = None


# ---------------------------------------------------------------------------
# StopWatch C152: 466 x 466 round AMOLED, CST820 touch.
# BtnA = yellow/orange key, BtnB = blue key.
# ---------------------------------------------------------------------------

W = 466
H = 466
CX = W // 2
CY = H // 2
SAFE_R = 224
PAGE_COUNT = 4
CFG_PAGE_COUNT = 3

ROT_SAMPLE_MS = 55
ROT_SETTLE_MS = 700
ROT_SWITCH_DEG = 58.0
ROT_SWITCH_MS = 160
ROT_FLAT = 0.35
ROT_CAL_DEG = 42.0

SWIPE_MIN_PX = 60
IDLE_DIM_MS = 120000
CYCLE_IDLE_MS = 15000
CYCLE_EVERY_MS = 10000
DIM_LEVEL = 12
BRIGHT_LEVELS = (30, 80, 150, 255)
BRIGHT_LABELS = ("10%", "30%", "60%", "100%")

# First path that works is used. Writes go to a .tmp file first, so a power cut mid-save
# cannot leave a half-written (unreadable) config behind.
CONFIG_PATHS = ("/flash/stopwatch_ai.json", "/stopwatch_ai.json")
BOOT_WIFI_TIMEOUT_MS = 20000   # per attempt; two attempts at boot
WIFI_RETRY_MS = 20000          # background reconnect interval while on the dashboard
SERVER_RETRY_MS = 30000        # re-fetch MQTT details from Node-RED while it is unreachable
CONFIG_PORT = 1880
CONFIG_ROUTE = "/stopwatch/configuration"

COLOR_BG = 0x070B10
COLOR_PANEL = 0x0F1722
COLOR_PANEL_HI = 0x152233
COLOR_TRACK = 0x1B2A38
COLOR_LINE = 0x233140
COLOR_OUTER = 0x121D27
COLOR_TEXT = 0xF2F7F8
COLOR_MUTED = 0x7C8EA3
COLOR_DIM = 0x46586B
COLOR_PRIMARY = 0x35E0C1
COLOR_PRIMARY_M = 0x1F8F7C
COLOR_PRIMARY_D = 0x116456
COLOR_AMBER = 0xFFB454
COLOR_AMBER_M = 0xA9752C
COLOR_AMBER_D = 0x6D4A16
COLOR_BLUE = 0x5BA8FF
COLOR_RED = 0xFF6B6B
COLOR_BTN = 0x13202C

FONT_PX = (12, 14, 16, 18, 24, 40, 48)
FONTS = {}

cv = None
HAS_CANVAS = False
WLAN = None
HAS_VIBRATION = False

ST = {
    "mode": "dashboard",
    "page": 0,
    "cfg_page": 0,
    "wifi_screen": "list",
    "wifi_aps": [],
    "wifi_page": 0,
    "wifi_scanning": False,
    "wifi_error": "",
    "wifi_target_ssid": "",
    "wifi_target_pass": "",
    "wifi_locked": False,
    "wifi_connect_start": 0,
    "wifi_connect_status": "",
    "wifi_shift": False,
    "wifi_kb_mode": "abc",
    "wifi_auto_attempted": False,
    "wifi_connect_draw": 0,
    "wifi_info_draw": 0,
    "forget_armed_until": 0,
    "rot_on": False,
    "rot_state": "off",
    "rot": 0,
    "rot_ref": None,
    "rot_axis": None,
    "rot_filter": None,
    "rot_error": "",
    "rot_bad": 0,
    "rot_last_sample": 0,
    "rot_settle_start": 0,
    "rot_sums": [0.0, 0.0, 0.0],
    "rot_samples": 0,
    "rot_cal_count": 0,
    "rot_cal_n": None,
    "rot_candidate": 0,
    "rot_candidate_since": 0,
    "dirty": True,
    "cfg": {"ip": "", "mqtt": None, "validated": False, "wifi": None},
    "draft_ip": "",
    "cfg_status": "",
    "cfg_status_col": COLOR_MUTED,
    "testing": False,
    "mqtt": None,
    "poll": None,
    "mqtt_state": "NO CFG",
    "mqtt_err": "",
    "last_msg_ms": None,
    "last_ping_ms": 0,
    "next_connect_ms": 0,
    "connect_fails": 0,
    "raw": None,
    "data": None,
    "wifi": False,
    "touch_down": False,
    "touch_swallow": False,
    "t_x0": 0,
    "t_y0": 0,
    "t_x": 0,
    "t_y": 0,
    "press": "",
    "press_until": 0,
    "toast": "",
    "toast_col": COLOR_TEXT,
    "toast_until": 0,
    "vib_until": 0,
    "pending_reset": False,
    "last_age": -1,
    "last_wifi_check": 0,
    "last_rendered": 0,
    "last_input": 0,
    "last_cycle": 0,
    "dimmed": False,
    "act_config": False,
    "act_next": False,
    "act_reset": False,
    "ignore_click_until": 0,
    "boot_stage": "",
    "boot_start": 0,
    "boot_draw": 0,
    "boot_attempts": 0,
    "boot_msg": "",
    "boot_detail": "",
    "last_wifi_retry": 0,
    "next_fetch_ms": 0,
}


def _ticks():
    return time.ticks_ms()


def _ago_ms(when):
    if when is None:
        return 99999999
    return time.ticks_diff(_ticks(), when)


def _clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _truthy(v):
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def _pick(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _empty_data():
    return {
        "session": {
            "gpu": None, "seconds": None, "wh": None, "cost": None, "tokens": None,
            "power_w": None, "active": None,
        },
        "today": {"seconds": None, "wh": None, "cost": None, "tokens": None},
        "llm": None,
        "hist": {
            "wh": None,
            "seconds": None,
            "cost": None,
            "tokens": None,
            "avg_wh": None,
            "avg_cost": None,
            "daily": [],
        },
        "cloud": [],
        "cloud_tokens": None,
        "currency": "",
    }


def _default_ui():
    return {"bright": 2, "haptic": True, "cycle": False, "dim": True}


FONT_FALLBACK = {
    12: (12, 14, 16, 18, 24, 40, 48),
    14: (14, 12, 16, 18, 24),
    16: (16, 14, 18, 12, 24),
    18: (18, 16, 24, 14, 12),
    24: (24, 18, 40, 16, 14, 12),
    40: (40, 48, 24, 18, 16),
    48: (48, 40, 24, 18, 16),
}


def _font(px):
    order = FONT_FALLBACK.get(px, (px, 48, 40, 24, 18, 16, 14, 12))
    for candidate in order:
        font = FONTS.get(candidate)
        if font is not None:
            return font
    return None


def _line_h(px):
    return {
        12: 14,
        14: 17,
        16: 19,
        18: 22,
        24: 29,
        40: 47,
        48: 56,
    }.get(px, px + 2)


def _est_width(text, px):
    total = 0.0
    narrow = " .,:;'|!iljt()[]/-I"
    wide = "MWmw@%"
    for ch in text:
        if ch in narrow:
            total += 0.36
        elif ch in wide:
            total += 0.94
        elif "0" <= ch <= "9":
            total += 0.64
        else:
            total += 0.72
    return int(total * px) + 2


def _text_w(target, text, px):
    font = _font(px)
    try:
        if font is not None:
            target.setFont(font)
    except Exception:
        pass
    tw = getattr(target, "textWidth", None)
    if tw is not None:
        try:
            width = int(tw(text))
            if width > 0:
                return width
        except Exception:
            pass
    return _est_width(text, px)


def _put(target, text, x, y, px, fg, bg):
    font = _font(px)
    try:
        if font is not None:
            target.setFont(font)
        target.setTextColor(fg, bg)
        draw_string = getattr(target, "drawString", None)
        if draw_string is not None:
            draw_string(text, int(x), int(y))
        else:
            target.setCursor(int(x), int(y))
            target.print(text)
    except Exception:
        pass


def _put_center(target, text, y, px, fg, bg):
    width = _text_w(target, text, px)
    x = (W - width) // 2
    if x < 8:
        x = 8
    _put(target, text, x, y, px, fg, bg)
    return width


def _put_center_x(target, text, x, w, y, px, fg, bg):
    _put(target, text, x + (w - _text_w(target, text, px)) // 2, y, px, fg, bg)


def _put_right(target, text, right, y, px, fg, bg):
    _put(target, text, right - _text_w(target, text, px), y, px, fg, bg)


def _fit_px(target, text, available, candidates):
    for px in candidates:
        if _text_w(target, text, px) <= available:
            return px
    return candidates[len(candidates) - 1]


def _put_center_fit(target, text, y, candidates, fg, bg, available):
    px = _fit_px(target, text, available, candidates)
    _put_center(target, text, y + (_line_h(candidates[0]) - _line_h(px)) // 2, px, fg, bg)
    return px


def _default_client_id():
    suffix = ""
    if machine is not None:
        try:
            uid = machine.unique_id()
            for byte in uid:
                suffix += "%02x" % byte
            suffix = suffix[-6:]
        except Exception:
            suffix = ""
    if suffix:
        return "stopwatch-ai-" + suffix
    return "stopwatch-ai"


def _init_fonts():
    global FONTS
    FONTS = {}
    fonts = getattr(M5.Lcd, "FONTS", None)
    if fonts is None:
        return
    for px in FONT_PX:
        FONTS[px] = getattr(fonts, "Montserrat%d" % px, None)


def _init_canvas():
    global cv, HAS_CANVAS
    try:
        cv = M5.Lcd.newCanvas(W, H, 16, True)
        HAS_CANVAS = True
    except Exception:
        try:
            cv = M5.Lcd.newCanvas(W, H, 16)
            HAS_CANVAS = True
        except Exception:
            cv = M5.Lcd
            HAS_CANVAS = False


def _push():
    if HAS_CANVAS:
        try:
            cv.push(0, 0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_duration(seconds, compact):
    if seconds is None:
        return "--"
    s = int(seconds)
    if s < 0:
        s = 0
    days = s // 86400
    h = (s % 86400) // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if compact:
        if days > 0:
            return "%dd %dh" % (days, h)
        if h > 0:
            return "%dh %02dm" % (h, m)
        if m > 0:
            return "%dm %02ds" % (m, sec)
        return "%ds" % sec
    if days > 0:
        return "%dd %02dh" % (days, h)
    return "%02d:%02d:%02d" % (h, m, sec)


def _fmt_energy(wh):
    if wh is None:
        return "--"
    value = float(wh)
    if value >= 1000.0:
        return "%.2f kWh" % (value / 1000.0) if value < 10000.0 else "%.1f kWh" % (value / 1000.0)
    if value >= 100.0:
        return "%.0f Wh" % value
    if value >= 10.0:
        return "%.1f Wh" % value
    if value >= 1.0:
        return "%.2f Wh" % value
    return "%.3f Wh" % value


def _fmt_money(value, currency):
    if value is None:
        return "--"
    if value < 0.1:
        body = "%.3f" % value
    else:
        body = "%.2f" % value
    if currency:
        return "%s %s" % (currency, body)
    return body


def _fmt_tokens(value):
    if value is None:
        return "--"
    if isinstance(value, str):
        return value
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "--"
    if v >= 1000000000.0:
        return "%.2fB" % (v / 1000000000.0)
    if v >= 1000000.0:
        return "%.2fM" % (v / 1000000.0)
    if v >= 1000.0:
        return "%.1fK" % (v / 1000.0)
    return "%d" % int(v)


def _money_decimals(value):
    if value is None:
        return "--"
    if value < 0.01:
        return "%.4f" % value
    if value < 0.1:
        return "%.3f" % value
    if value < 100.0:
        return "%.2f" % value
    return "%.0f" % value


def _fmt_usd(value):
    if value is None:
        return "--"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "--"
    if v >= 100.0:
        return "$%.0f" % v
    if v >= 1.0:
        return "$%.2f" % v
    if v >= 0.01:
        return "$%.3f" % v
    return "$%.4f" % v


def _short_wh(v):
    if v >= 10000.0:
        return "%.0fk" % (v / 1000.0)
    if v >= 1000.0:
        return "%.1fk" % (v / 1000.0)
    if v >= 100.0:
        return "%.0f" % v
    if v >= 10.0:
        return "%.1f" % v
    return "%.2f" % v


def _short_money(v):
    if v >= 100.0:
        return "%.0f" % v
    if v >= 10.0:
        return "%.1f" % v
    if v >= 0.1:
        return "%.2f" % v
    return "%.3f" % v


def _fit_text(text, px, available):
    if _text_w(cv, text, px) <= available:
        return text
    shortened = text
    while len(shortened) > 1 and _text_w(cv, shortened + ".", px) > available:
        shortened = shortened[:len(shortened) - 1]
    return shortened + "."


def _valid_ip(text):
    if text is None or len(text) < 7 or len(text) > 15:
        return False
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if len(part) < 1 or len(part) > 3:
            return False
        for ch in part:
            if ch < "0" or ch > "9":
                return False
        value = int(part)
        if value < 0 or value > 255:
            return False
    return True


def _derive_reset_topic(topic):
    for suffix in ("/telemetry", "/data", "/live"):
        if topic.endswith(suffix):
            return topic[:-len(suffix)] + "/reset"
    if topic.endswith("/"):
        topic = topic[:-1]
    return topic + "/reset"


def _clean_currency(value):
    if value is None:
        return ""
    text = str(value).upper()
    out = ""
    for ch in text:
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
            out += ch
        if len(out) >= 4:
            break
    return out


def _parse_broker_url(value):
    if not isinstance(value, str) or len(value) < 3:
        return None, None
    text = value
    if "://" in text:
        text = text.split("://", 1)[1]
    if "/" in text:
        text = text.split("/", 1)[0]
    if "@" in text:
        text = text.split("@", 1)[1]
    host = text
    port = None
    if ":" in text:
        parts = text.split(":", 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except (ValueError, TypeError):
            port = None
    if len(host) < 1:
        return None, None
    return host, port


DOW = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


def _weekday_from_date(text):
    # "YYYY-MM-DD..." -> MON/TUE..., Sakamoto's algorithm, no RTC needed.
    try:
        y = int(text[0:4])
        m = int(text[5:7])
        d = int(text[8:10])
        if m < 1 or m > 12 or d < 1 or d > 31:
            return None
    except Exception:
        return None
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if m < 3:
        y -= 1
    return DOW[(y + y // 4 - y // 100 + y // 400 + t[m - 1] + d) % 7]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_config():
    cfg = {
        "ip": "",
        "mqtt": None,
        "validated": False,
        "wifi": None,
        "rotate": {"enabled": False, "ref": None, "axis": None},
        "ui": _default_ui(),
    }
    saved = None
    for base in CONFIG_PATHS:
        for candidate in (base, base + ".tmp"):
            try:
                with open(candidate, "r") as f:
                    data = json.loads(f.read())
                if isinstance(data, dict):
                    saved = data
                    break
            except Exception:
                pass
        if saved is not None:
            break
    try:
        if isinstance(saved, dict):
            ip = saved.get("ip")
            if isinstance(ip, str):
                cfg["ip"] = ip
            mqtt = saved.get("mqtt")
            if isinstance(mqtt, dict) and isinstance(mqtt.get("host"), str):
                cfg["mqtt"] = mqtt
            cfg["validated"] = _truthy(saved.get("validated")) and cfg["mqtt"] is not None
            wifi = saved.get("wifi")
            if isinstance(wifi, dict) and isinstance(wifi.get("ssid"), str):
                password = wifi.get("password")
                cfg["wifi"] = {
                    "ssid": wifi.get("ssid"),
                    "password": password if isinstance(password, str) else "",
                }
            rot = saved.get("rotate")
            if isinstance(rot, dict):
                ref = _vec3(rot.get("ref"))
                axis = _vec3(rot.get("axis"))
                if ref is not None and axis is not None and abs(_dot3(ref, axis)) > 0.2:
                    ref = None
                    axis = None
                cfg["rotate"] = {
                    "enabled": _truthy(rot.get("enabled")),
                    "ref": ref,
                    "axis": axis,
                }
            ui = saved.get("ui")
            if isinstance(ui, dict):
                merged = _default_ui()
                bright = ui.get("bright")
                if isinstance(bright, int) and 0 <= bright < len(BRIGHT_LEVELS):
                    merged["bright"] = bright
                for key in ("haptic", "cycle", "dim"):
                    if key in ui:
                        merged[key] = _truthy(ui.get(key))
                cfg["ui"] = merged
    except Exception:
        pass
    return cfg


def _save_config(cfg):
    try:
        data = json.dumps(cfg)
    except Exception:
        return False
    for path in CONFIG_PATHS:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(data)
        except Exception:
            continue
        try:
            os.rename(tmp, path)            # LittleFS replaces the old file in one step
        except Exception:
            try:
                os.remove(path)             # FAT will not rename over an existing file
            except Exception:
                pass
            try:
                os.rename(tmp, path)
            except Exception:
                continue                    # the .tmp copy is still loadable
        return True
    return False


def _ui():
    ui = ST["cfg"].get("ui")
    if not isinstance(ui, dict):
        ui = _default_ui()
        ST["cfg"]["ui"] = ui
    return ui


# ---------------------------------------------------------------------------
# Display brightness, idle dim, input tracking
# ---------------------------------------------------------------------------


def _set_brightness(value):
    try:
        M5.Display.setBrightness(int(value))
        return
    except Exception:
        pass
    try:
        M5.Lcd.setBrightness(int(value))
    except Exception:
        pass


def _apply_brightness():
    if ST["dimmed"]:
        _set_brightness(DIM_LEVEL)
    else:
        _set_brightness(BRIGHT_LEVELS[_ui()["bright"]])


def _note_input():
    """Register user input. Returns True if the input only woke the screen."""
    ST["last_input"] = _ticks()
    if ST["dimmed"]:
        ST["dimmed"] = False
        _apply_brightness()
        ST["dirty"] = True
        return True
    return False


def _idle_tick():
    if ST["dimmed"]:
        return
    if not _ui()["dim"]:
        return
    if ST["mode"] != "dashboard":
        return
    if _ago_ms(ST["last_input"]) >= IDLE_DIM_MS:
        ST["dimmed"] = True
        _apply_brightness()


def _cycle_tick():
    if not _ui()["cycle"] or ST["mode"] != "dashboard":
        return
    if _ago_ms(ST["last_input"]) < CYCLE_IDLE_MS:
        return
    if _ago_ms(ST["last_cycle"]) < CYCLE_EVERY_MS:
        return
    ST["last_cycle"] = _ticks()
    _change_page(1, True)


# ---------------------------------------------------------------------------
# IMU display orientation
#
# The StopWatch BMI270 axis direction is not documented. Calibration therefore
# stores the gravity vector of the posture the user calls upright and a normal
# derived from one deliberate 90-degree right turn. No axis convention is
# assumed. Rotation is then a signed angle in that calibrated plane.
# ---------------------------------------------------------------------------


def _vec3(value):
    try:
        if value is None or len(value) < 3:
            return None
        x = _num(value[0])
        y = _num(value[1])
        z = _num(value[2])
        if x is None or y is None or z is None:
            return None
        norm = math.sqrt(x * x + y * y + z * z)
        if norm < 0.000001:
            return None
        return [x / norm, y / norm, z / norm]
    except Exception:
        return None


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross3(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _read_accel():
    try:
        acc = Imu.getAccel()
    except Exception:
        return None
    if acc is None:
        return None
    try:
        if len(acc) < 3:
            return None
        x = _num(acc[0])
        y = _num(acc[1])
        z = _num(acc[2])
    except Exception:
        return None
    if x is None or y is None or z is None:
        return None
    if abs(x) > 1000000000.0 or abs(y) > 1000000000.0 or abs(z) > 1000000000.0:
        return None
    return _vec3((x, y, z))


def _rot_status(text, colour):
    if ST["mode"] == "config":
        ST["cfg_status"] = text
        ST["cfg_status_col"] = colour
        ST["dirty"] = True


def _rot_apply(index, force):
    index = index % 4
    if not force and index == ST["rot"]:
        return
    ST["rot"] = index
    try:
        M5.Display.setRotation(index)
    except Exception:
        pass
    ST["dirty"] = True


def _rot_angle(g):
    ref = ST["rot_ref"]
    axis = ST["rot_axis"]
    if ref is None or axis is None:
        return None
    axial = _dot3(g, axis)
    in_plane = math.sqrt(max(0.0, 1.0 - axial * axial))
    if in_plane < ROT_FLAT:
        return None
    cross = _cross3(ref, g)
    return math.degrees(math.atan2(_dot3(cross, axis), _dot3(ref, g)))


def _rot_index(angle):
    quarter = angle / 90.0
    if quarter >= 0:
        value = int(quarter + 0.5)
    else:
        value = -int(-quarter + 0.5)
    return value % 4


def _rot_filter_sample(g):
    filtered = ST["rot_filter"]
    if filtered is None:
        filtered = g
    else:
        filtered = _vec3((
            0.65 * filtered[0] + 0.35 * g[0],
            0.65 * filtered[1] + 0.35 * g[1],
            0.65 * filtered[2] + 0.35 * g[2],
        ))
        if filtered is None:
            filtered = g
    ST["rot_filter"] = filtered
    return filtered


def _rot_update(g, now, immediate):
    angle = _rot_angle(g)
    if angle is None:
        ST["rot_filter"] = None
        ST["rot_candidate"] = ST["rot"]
        ST["rot_candidate_since"] = now
        return
    index = _rot_index(angle)
    if index == ST["rot"]:
        ST["rot_candidate"] = index
        ST["rot_candidate_since"] = now
        return
    delta = angle - ST["rot"] * 90.0
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    if abs(delta) < ROT_SWITCH_DEG:
        ST["rot_candidate"] = ST["rot"]
        ST["rot_candidate_since"] = now
        return
    if ST["rot_candidate"] != index:
        ST["rot_candidate"] = index
        ST["rot_candidate_since"] = now
        if not immediate:
            return
    if immediate or _ago_ms(ST["rot_candidate_since"]) >= ROT_SWITCH_MS:
        _rot_apply(index, False)


def _rot_enable():
    ST["rot_on"] = True
    ST["rot_state"] = "settle"
    ST["rot_settle_start"] = _ticks()
    ST["rot_last_sample"] = 0
    ST["rot_sums"] = [0.0, 0.0, 0.0]
    ST["rot_samples"] = 0
    ST["rot_ref"] = None
    ST["rot_axis"] = None
    ST["rot_filter"] = None
    ST["rot_error"] = ""
    ST["rot_bad"] = 0
    ST["rot_cal_count"] = 0
    ST["rot_cal_n"] = None
    ST["rot_candidate"] = ST["rot"]
    ST["rot_candidate_since"] = _ticks()
    rot = ST["cfg"].get("rotate")
    if not isinstance(rot, dict):
        rot = {}
    rot["enabled"] = True
    rot["ref"] = None
    rot["axis"] = None
    ST["cfg"]["rotate"] = rot
    _save_config(ST["cfg"])
    _rot_apply(0, True)
    _rot_status("HOLD UPRIGHT", COLOR_AMBER)


def _rot_disable():
    ST["rot_on"] = False
    ST["rot_state"] = "off"
    ST["rot_error"] = ""
    ST["rot_filter"] = None
    ST["rot_candidate"] = 0
    rot = ST["cfg"].get("rotate")
    if not isinstance(rot, dict):
        rot = {}
    rot["enabled"] = False
    ST["cfg"]["rotate"] = rot
    _save_config(ST["cfg"])
    _rot_apply(0, True)
    _rot_status("AUTO ROTATE OFF", COLOR_MUTED)


def _rot_toggle():
    if ST["rot_on"]:
        _rot_disable()
    else:
        _rot_enable()
    ST["dirty"] = True
    _buzz(65)


def _rot_restore():
    rot = ST["cfg"].get("rotate")
    if not isinstance(rot, dict):
        ST["rot_on"] = False
        ST["rot_state"] = "off"
        return
    ST["rot_on"] = _truthy(rot.get("enabled"))
    ref = _vec3(rot.get("ref"))
    axis = _vec3(rot.get("axis"))
    if ref is not None and axis is not None and abs(_dot3(ref, axis)) > 0.2:
        ref = None
        axis = None
    ST["rot_ref"] = ref
    ST["rot_axis"] = axis
    ST["rot_error"] = ""
    ST["rot_bad"] = 0
    ST["rot_last_sample"] = 0
    ST["rot_candidate"] = 0
    ST["rot_candidate_since"] = _ticks()
    if not ST["rot_on"]:
        ST["rot_state"] = "off"
        return
    if ref is not None and axis is not None:
        ST["rot_state"] = "active"
        g = _read_accel()
        if g is not None:
            _rot_update(g, _ticks(), True)
    else:
        ST["rot_state"] = "settle"
        ST["rot_settle_start"] = _ticks()
        ST["rot_sums"] = [0.0, 0.0, 0.0]
        ST["rot_samples"] = 0
    ST["dirty"] = True


def _rot_tick():
    if not ST["rot_on"]:
        return
    now = _ticks()
    if _ago_ms(ST["rot_last_sample"]) < ROT_SAMPLE_MS:
        return
    ST["rot_last_sample"] = now
    g = _read_accel()
    if g is None:
        ST["rot_bad"] += 1
        if ST["rot_bad"] >= 3 and ST["rot_error"] != "NO IMU DATA":
            ST["rot_error"] = "NO IMU DATA"
            _rot_status("NO IMU DATA", COLOR_RED)
            ST["dirty"] = True
        return
    ST["rot_bad"] = 0
    if ST["rot_error"]:
        ST["rot_error"] = ""
        if ST["rot_state"] == "active":
            _rot_status("AUTO ROTATE ON", COLOR_PRIMARY)
        ST["dirty"] = True

    state = ST["rot_state"]
    if state == "settle":
        ST["rot_sums"][0] += g[0]
        ST["rot_sums"][1] += g[1]
        ST["rot_sums"][2] += g[2]
        ST["rot_samples"] += 1
        if _ago_ms(ST["rot_settle_start"]) >= ROT_SETTLE_MS and ST["rot_samples"] >= 4:
            ref = _vec3(ST["rot_sums"])
            if ref is None:
                ST["rot_sums"] = [0.0, 0.0, 0.0]
                ST["rot_samples"] = 0
                ST["rot_settle_start"] = now
                return
            ST["rot_ref"] = ref
            ST["rot_state"] = "turn"
            ST["rot_cal_count"] = 0
            ST["rot_cal_n"] = None
            _rot_status("TURN RIGHT 90 DEG", COLOR_AMBER)
            ST["dirty"] = True
        return

    if state == "turn":
        ref = ST["rot_ref"]
        if ref is None:
            ST["rot_state"] = "settle"
            ST["rot_settle_start"] = now
            return
        dot = _clamp(_dot3(ref, g), -1.0, 1.0)
        angle = math.degrees(math.acos(dot))
        if angle < ROT_CAL_DEG:
            ST["rot_cal_count"] = 0
            ST["rot_cal_n"] = None
            return
        n0 = _vec3(_cross3(ref, g))
        if n0 is None:
            return
        previous = ST["rot_cal_n"]
        if previous is not None and _dot3(previous, n0) > 0.92:
            ST["rot_cal_count"] += 1
        else:
            ST["rot_cal_count"] = 1
        ST["rot_cal_n"] = n0
        if ST["rot_cal_count"] < 3:
            return
        axis = _vec3((-n0[0], -n0[1], -n0[2]))
        if axis is None:
            return
        ST["rot_axis"] = axis
        ST["rot_state"] = "active"
        ST["rot_filter"] = g
        ST["rot_candidate"] = ST["rot"]
        ST["rot_candidate_since"] = now
        rot = ST["cfg"].get("rotate")
        if not isinstance(rot, dict):
            rot = {}
        rot["enabled"] = True
        rot["ref"] = ref
        rot["axis"] = axis
        ST["cfg"]["rotate"] = rot
        _save_config(ST["cfg"])
        _rot_update(g, now, True)
        _rot_status("AUTO ROTATE ON", COLOR_PRIMARY)
        _buzz(90)
        ST["dirty"] = True
        return

    if state == "active":
        _rot_update(_rot_filter_sample(g), now, False)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _init_wlan():
    global WLAN
    try:
        WLAN = network.WLAN(network.STA_IF)
        if not WLAN.active():
            WLAN.active(True)
    except Exception:
        WLAN = None


def _wifi_check():
    ok = False
    try:
        if WLAN is not None:
            if not WLAN.active():
                WLAN.active(True)
            ok = bool(WLAN.isconnected())
    except Exception:
        ok = False
    if ok != ST["wifi"]:
        ST["wifi"] = ok
        ST["dirty"] = True


def _decode_text(value):
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode()
        except Exception:
            return ""
    if value is None:
        return ""
    return str(value)


def _ascii_label(text):
    out = ""
    for ch in _decode_text(text):
        if 32 <= ord(ch) < 127:
            out += ch
        else:
            out += "?"
    return out


def _connected_ssid():
    try:
        if WLAN is not None and WLAN.isconnected():
            return _decode_text(WLAN.config("ssid"))
    except Exception:
        pass
    return ""


def _wifi_rssi():
    try:
        if WLAN is not None and WLAN.isconnected():
            return int(WLAN.status("rssi"))
    except Exception:
        pass
    return None


def _wifi_ip():
    try:
        if WLAN is not None and WLAN.isconnected():
            return WLAN.ifconfig()[0]
    except Exception:
        pass
    return ""


def _saved_ssid():
    wifi = ST["cfg"].get("wifi")
    if isinstance(wifi, dict):
        return _decode_text(wifi.get("ssid"))
    return ""


def _wifi_connect_saved():
    """Start a (non-blocking) connection to the saved network. Safe to call repeatedly."""
    wifi = ST["cfg"].get("wifi")
    if not isinstance(wifi, dict) or not wifi.get("ssid") or WLAN is None:
        return False
    ST["last_wifi_retry"] = _ticks()
    try:
        if not WLAN.active():
            WLAN.active(True)
        if WLAN.isconnected():
            return True
        try:
            WLAN.disconnect()               # clears a half-finished attempt (e.g. firmware boot connect)
        except Exception:
            pass
        password = wifi.get("password")
        if isinstance(password, str) and len(password) > 0:
            WLAN.connect(wifi.get("ssid"), password)
        else:
            WLAN.connect(wifi.get("ssid"))
        return True
    except Exception:
        return False


def _wifi_keepalive():
    """Dashboard only: keep trying the saved network after a failed boot or a router reboot."""
    if ST["mode"] != "dashboard" or ST["wifi"] or WLAN is None or not _saved_ssid():
        return
    if _ago_ms(ST["last_wifi_retry"]) < WIFI_RETRY_MS:
        return
    try:
        if WLAN.isconnected():
            return
        if WLAN.status() == 1001:           # STAT_CONNECTING: let the current attempt finish
            ST["last_wifi_retry"] = _ticks()
            return
    except Exception:
        pass
    _wifi_connect_saved()


def _wifi_reconnect():
    if not _saved_ssid():
        ST["cfg_status"] = "NO SAVED NETWORK"
        ST["cfg_status_col"] = COLOR_AMBER
        ST["dirty"] = True
        _buzz(80)
        return
    try:
        if WLAN is not None:
            WLAN.disconnect()
    except Exception:
        pass
    ST["wifi"] = False
    _wifi_connect_saved()
    ST["cfg_status"] = "RECONNECTING..."
    ST["cfg_status_col"] = COLOR_BLUE
    ST["dirty"] = True
    _buzz(60)


def _wifi_forget():
    ST["cfg"]["wifi"] = None
    _save_config(ST["cfg"])
    try:
        if WLAN is not None:
            WLAN.disconnect()
    except Exception:
        pass
    ST["wifi"] = False
    if ST["mqtt"] is not None:
        _mqtt_drop("WIFI FORGOTTEN")
    ST["cfg_status"] = "NETWORK FORGOTTEN"
    ST["cfg_status_col"] = COLOR_AMBER
    ST["dirty"] = True
    _buzz(120)


def _wifi_scan():
    ST["wifi_scanning"] = True
    ST["wifi_error"] = ""
    ST["wifi_page"] = 0
    ST["dirty"] = True
    _render(True)
    if WLAN is None:
        ST["wifi_scanning"] = False
        ST["wifi_error"] = "WI-FI UNAVAILABLE"
        ST["dirty"] = True
        return
    try:
        if not WLAN.active():
            WLAN.active(True)
        raw = WLAN.scan()
        seen = {}
        for item in raw:
            try:
                ssid = _decode_text(item[0])
                if len(ssid) < 1:
                    continue
                rssi = int(item[3])
                security = int(item[4])
            except Exception:
                continue
            old = seen.get(ssid)
            if old is None or rssi > old["rssi"]:
                seen[ssid] = {"ssid": ssid, "rssi": rssi, "security": security}
        aps = []
        for key in seen:
            aps.append(seen[key])
        for i in range(1, len(aps)):
            key = aps[i]
            j = i - 1
            while j >= 0 and aps[j]["rssi"] < key["rssi"]:
                aps[j + 1] = aps[j]
                j -= 1
            aps[j + 1] = key
        ST["wifi_aps"] = aps
        if len(aps) == 0:
            ST["wifi_error"] = "NO NETWORKS FOUND"
    except Exception:
        ST["wifi_error"] = "SCAN FAILED"
    ST["wifi_scanning"] = False
    ST["dirty"] = True


def _wifi_enter():
    ST["mode"] = "wifi"
    ST["wifi_screen"] = "list"
    ST["wifi_error"] = ""
    ST["wifi_page"] = 0
    ST["wifi_shift"] = False
    ST["wifi_kb_mode"] = "abc"
    ST["press"] = ""
    _buzz(60)
    _wifi_scan()


def _wifi_back():
    if ST["mode"] != "wifi":
        return
    if ST["wifi_screen"] == "connecting":
        try:
            if WLAN is not None:
                WLAN.disconnect()
        except Exception:
            pass
        ST["wifi_screen"] = "list"
        ST["wifi_error"] = ""
        ST["wifi_connect_status"] = ""
    elif ST["wifi_screen"] == "password":
        ST["wifi_screen"] = "list"
        ST["wifi_error"] = ""
        ST["wifi_target_pass"] = ""
    else:
        ST["mode"] = "config"
        ST["cfg_page"] = 1
        ST["wifi_screen"] = "list"
        ST["wifi_error"] = ""
        ST["cfg_status"] = ""
        ST["cfg_status_col"] = COLOR_MUTED
    ST["press"] = ""
    ST["dirty"] = True
    _buzz(50)


def _wifi_fail(message):
    try:
        if WLAN is not None:
            WLAN.disconnect()
    except Exception:
        pass
    ST["wifi_screen"] = "password" if ST["wifi_locked"] else "list"
    ST["wifi_error"] = message
    ST["wifi_connect_status"] = ""
    ST["dirty"] = True
    _buzz(120)


def _wifi_success():
    ssid = ST["wifi_target_ssid"]
    password = ST["wifi_target_pass"]
    ST["cfg"]["wifi"] = {"ssid": ssid, "password": password}
    saved = _save_config(ST["cfg"])
    ST["wifi"] = True
    ST["wifi_auto_attempted"] = True
    ST["wifi_error"] = ""
    ST["wifi_connect_status"] = ""
    ST["wifi_screen"] = "list"
    ST["mode"] = "config"
    ST["cfg_page"] = 1
    ST["last_wifi_check"] = _ticks()
    if saved and not _valid_ip(ST["cfg"].get("ip", "")):
        ST["cfg_page"] = 0
        ST["draft_ip"] = ""
        ST["cfg_status"] = "WI-FI OK - ENTER SERVER IP"
        ST["cfg_status_col"] = COLOR_PRIMARY
    elif saved:
        ST["cfg_status"] = "WI-FI CONNECTED"
        ST["cfg_status_col"] = COLOR_PRIMARY
    else:
        ST["cfg_status"] = "WI-FI OK / SAVE FAILED"
        ST["cfg_status_col"] = COLOR_AMBER
    if ST["mqtt"] is not None:
        _mqtt_drop("WIFI CHANGED")
    ST["next_connect_ms"] = 0
    ST["dirty"] = True
    _buzz(140)


def _wifi_start_connect(ssid, password, locked):
    ST["wifi_target_ssid"] = ssid
    ST["wifi_target_pass"] = password
    ST["wifi_locked"] = locked
    ST["wifi_screen"] = "connecting"
    ST["wifi_error"] = ""
    ST["wifi_connect_status"] = "CONNECTING..."
    ST["wifi_connect_start"] = _ticks()
    ST["wifi_connect_draw"] = _ticks()
    ST["dirty"] = True
    _render(True)
    _buzz(70)
    try:
        if WLAN is None:
            _wifi_fail("WI-FI UNAVAILABLE")
            return
        if not WLAN.active():
            WLAN.active(True)
        try:
            WLAN.disconnect()
        except Exception:
            pass
        if len(password) > 0:
            WLAN.connect(ssid, password)
        else:
            WLAN.connect(ssid)
    except Exception:
        _wifi_fail("CONNECT ERROR")


def _wifi_select_ap(index):
    if index < 0 or index >= len(ST["wifi_aps"]):
        return
    ap = ST["wifi_aps"][index]
    ssid = ap["ssid"]
    locked = ap["security"] != 0
    ST["wifi_target_ssid"] = ssid
    ST["wifi_locked"] = locked
    ST["wifi_error"] = ""
    if not locked:
        _wifi_start_connect(ssid, "", False)
        return
    ST["wifi_screen"] = "password"
    ST["wifi_target_pass"] = ""
    wifi = ST["cfg"].get("wifi")
    if isinstance(wifi, dict) and wifi.get("ssid") == ssid:
        saved = wifi.get("password")
        if isinstance(saved, str):
            ST["wifi_target_pass"] = saved
    ST["wifi_shift"] = False
    ST["wifi_kb_mode"] = "abc"
    ST["press"] = ""
    ST["dirty"] = True
    _buzz(55)


def _wifi_tick():
    now = _ticks()
    if ST["mode"] == "config" and ST["cfg_page"] == 1:
        if now - ST["wifi_info_draw"] >= 3000:
            ST["wifi_info_draw"] = now
            ST["dirty"] = True
    if ST["mode"] != "wifi" or ST["wifi_screen"] != "connecting":
        if now - ST["last_wifi_check"] >= 2500:
            ST["last_wifi_check"] = now
            _wifi_check()
        return
    elapsed = time.ticks_diff(now, ST["wifi_connect_start"])
    if WLAN is None:
        _wifi_fail("WI-FI UNAVAILABLE")
        return
    try:
        if WLAN.isconnected():
            _wifi_success()
            return
        status = WLAN.status()
    except Exception:
        _wifi_fail("WI-FI ERROR")
        return
    if status == 201:
        _wifi_fail("NETWORK NOT FOUND")
        return
    if status == 202:
        _wifi_fail("WRONG PASSWORD")
        return
    if status == 203:
        _wifi_fail("CONNECTION FAILED")
        return
    if elapsed >= 20000:
        _wifi_fail("CONNECTION TIMEOUT")
        return
    if elapsed < 1500:
        message = "CONNECTING..."
    elif status == 1001:
        message = "AUTHENTICATING..."
    else:
        message = "OBTAINING IP..."
    if message != ST["wifi_connect_status"]:
        ST["wifi_connect_status"] = message
        ST["dirty"] = True
    if now - ST["wifi_connect_draw"] >= 500:
        ST["wifi_connect_draw"] = now
        ST["dirty"] = True


def _tcp_probe(host, port, timeout):
    sock = None
    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
        sock = socket.socket()
        sock.settimeout(timeout)
        sock.connect(addr)
        return True
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _unwrap_server(data):
    if not isinstance(data, dict):
        return data
    for key in ("configuration", "config", "result", "payload", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            if ("mqtt" in value) or ("broker" in value) or ("host" in value):
                return value
    return data


def _scopes_for(data):
    if not isinstance(data, dict):
        return []
    mqtt = data.get("mqtt")
    if isinstance(mqtt, dict):
        return [mqtt, data]
    broker = data.get("broker")
    if isinstance(broker, dict):
        return [broker, data]
    return [data]


def _parse_mqtt_details(data):
    data = _unwrap_server(data)
    if not isinstance(data, dict):
        return None, "BAD CONFIG"

    host = None
    url_port = None
    mqtt_value = data.get("mqtt")
    if isinstance(mqtt_value, str):
        host = mqtt_value
    scopes = _scopes_for(data)

    if host is None:
        host = _pick(scopes[0], ("host", "server", "broker", "mqtt_host", "broker_host"))
    if host is None and len(scopes) > 1:
        host = _pick(scopes[1], ("mqtt_host", "broker_host", "host"))

    url_value = _pick(scopes[0], ("url", "mqtt_url", "broker_url"))
    if url_value is None and len(scopes) > 1:
        url_value = _pick(scopes[1], ("mqtt_url", "broker_url"))
    if url_value is not None:
        url_host, url_port = _parse_broker_url(url_value)
        if host is None:
            host = url_host

    if not isinstance(host, str) or len(host) < 1:
        return None, "NO MQTT HOST"

    port = _num(_pick(scopes[0], ("port", "mqtt_port", "broker_port")))
    if port is None and len(scopes) > 1:
        port = _num(_pick(scopes[1], ("mqtt_port", "broker_port")))
    if port is None and url_port is not None:
        port = url_port
    if port is None:
        port = 1883
    port = int(port)
    if port < 1 or port > 65535:
        return None, "BAD MQTT PORT"

    topics = _pick(scopes[0], ("topics",), None)
    topic = _pick(scopes[0], ("topic", "telemetry_topic", "data_topic", "subscribe_topic", "mqtt_topic"))
    if topic is None and len(scopes) > 1:
        topic = _pick(scopes[1], ("mqtt_topic", "telemetry_topic"))
    if topic is None and isinstance(topics, dict):
        topic = _pick(topics, ("telemetry", "data", "live", "state"))
    if not isinstance(topic, str) or len(topic) < 1:
        return None, "NO MQTT TOPIC"

    reset_topic = _pick(scopes[0], ("reset_topic", "command_topic", "control_topic", "mqtt_reset_topic"))
    if reset_topic is None and len(scopes) > 1:
        reset_topic = _pick(scopes[1], ("mqtt_reset_topic",))
    if reset_topic is None and isinstance(topics, dict):
        reset_topic = _pick(topics, ("reset", "command", "control"))
    if not isinstance(reset_topic, str) or len(reset_topic) < 1:
        reset_topic = _derive_reset_topic(topic)

    user = _pick(scopes[0], ("user", "username", "mqtt_user", "mqtt_username"))
    if user is None and len(scopes) > 1:
        user = _pick(scopes[1], ("mqtt_user", "mqtt_username"))
    password = _pick(scopes[0], ("password", "pass", "mqtt_password"))
    if password is None and len(scopes) > 1:
        password = _pick(scopes[1], ("mqtt_password",))

    client_id = _pick(scopes[0], ("client_id", "mqtt_client_id"))
    if client_id is None:
        client_id = _default_client_id()

    keepalive = _num(_pick(scopes[0], ("keepalive", "keep_alive")))
    if keepalive is None:
        keepalive = 60
    keepalive = int(keepalive)

    ssl_value = _pick(scopes[0], ("ssl", "tls", "use_tls"), False)
    use_ssl = _truthy(ssl_value) or port == 8883
    ca_file = _pick(scopes[0], ("ca_file", "cafile", "ca_cert"))
    reset_payload = _pick(scopes[0], ("reset_payload", "reset_message"))

    return {
        "host": host,
        "port": port,
        "user": user if isinstance(user, str) and len(user) > 0 else None,
        "password": password if isinstance(password, str) and len(password) > 0 else None,
        "topic": topic,
        "reset_topic": reset_topic,
        "client_id": str(client_id),
        "keepalive": keepalive,
        "ssl": use_ssl,
        "ca_file": ca_file if isinstance(ca_file, str) else None,
        "reset_payload": reset_payload,
    }, None


def _fetch_server_config(ip):
    if requests2 is None:
        return None, "REQUESTS2 MISSING"
    if not ST["wifi"]:
        _wifi_check()
        if not ST["wifi"]:
            return None, "NO WIFI"
    if not _tcp_probe(ip, CONFIG_PORT, 4):
        return None, "SERVER UNREACHABLE"

    url = "http://%s:%d%s" % (ip, CONFIG_PORT, CONFIG_ROUTE)
    response = None
    try:
        headers = {"Accept": "application/json", "Connection": "close"}
        try:
            response = requests2.get(url, headers=headers, timeout=8)
        except TypeError:
            response = requests2.get(url, headers=headers)
        code = int(getattr(response, "status_code", 0))
        body = response.text
        try:
            response.close()
        except Exception:
            pass
        if code < 200 or code >= 300:
            return None, "HTTP %d" % code
        try:
            data = json.loads(body)
        except Exception:
            return None, "BAD JSON"
        return _parse_mqtt_details(data)
    except Exception:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        return None, "REST FAILED"


def _mqtt_callback(data):
    try:
        ST["raw"] = data[1]
    except Exception:
        pass


def _mqtt_drop(reason):
    client = ST["mqtt"]
    if client is not None:
        try:
            client.disconnect()
        except Exception:
            pass
    ST["mqtt"] = None
    ST["poll"] = None
    ST["mqtt_state"] = "OFFLINE"
    ST["mqtt_err"] = reason
    if ST["connect_fails"] < 5:
        ST["connect_fails"] += 1
    delay = 5
    if ST["connect_fails"] >= 2:
        delay = 10 * (ST["connect_fails"] - 1)
    if delay > 30:
        delay = 30
    ST["next_connect_ms"] = _ticks() + delay * 1000
    ST["dirty"] = True


def _mqtt_connect():
    global MQTTClient
    cfg = ST["cfg"]
    details = cfg.get("mqtt")
    if MQTTClient is None:
        return False, "UMQTT MISSING"
    if not cfg.get("validated") or not isinstance(details, dict):
        return False, "NO CONFIG"
    if not ST["wifi"]:
        _wifi_check()
        if not ST["wifi"]:
            return False, "NO WIFI"

    host = details.get("host")
    port = int(details.get("port", 1883))
    if not isinstance(host, str):
        return False, "NO HOST"
    if not _tcp_probe(host, port, 4):
        return False, "NO BROKER"

    client = None
    try:
        ssl_params = {}
        if details.get("ssl"):
            ssl_params["server_hostname"] = host
            if details.get("ca_file"):
                ssl_params["cafile"] = details.get("ca_file")
        client = MQTTClient(
            details.get("client_id", "stopwatch-ai"),
            host,
            port=int(port),
            user=details.get("user"),
            password=details.get("password"),
            keepalive=int(details.get("keepalive", 60)),
            ssl=_truthy(details.get("ssl")),
            ssl_params=ssl_params,
        )
        client.connect(clean_session=True)
        client.subscribe(details.get("topic"), _mqtt_callback, qos=0)

        sock = getattr(client, "sock", None)
        if sock is None:
            sock = getattr(client, "_sock", None)
        poller = None
        if sock is not None:
            try:
                sock.settimeout(0.2)
            except Exception:
                pass
            try:
                poller = select.poll()
                poller.register(sock, select.POLLIN)
            except Exception:
                poller = None

        ST["mqtt"] = client
        ST["poll"] = poller
        ST["mqtt_state"] = "CONNECTED"
        ST["mqtt_err"] = ""
        ST["last_ping_ms"] = _ticks()
        ST["connect_fails"] = 0
        ST["next_connect_ms"] = 0
        if ST["pending_reset"]:
            if _send_reset():
                ST["pending_reset"] = False
                _set_toast("SESSION RESET SENT", COLOR_PRIMARY, 2200)
            elif ST["mqtt"] is None:
                return False, "RESET PUBLISH FAILED"
        ST["dirty"] = True
        return True, ""
    except Exception:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        return False, "BROKER FAIL"


def _send_reset():
    client = ST["mqtt"]
    details = ST["cfg"].get("mqtt")
    if client is None or not isinstance(details, dict):
        return False
    payload = details.get("reset_payload")
    if payload is None:
        payload = '{"action":"reset_session"}'
    elif isinstance(payload, dict):
        try:
            payload = json.dumps(payload)
        except Exception:
            payload = '{"action":"reset_session"}'
    try:
        client.publish(details.get("reset_topic"), payload, qos=0)
        return True
    except Exception:
        _mqtt_drop("PUBLISH FAILED")
        return False


# ---------------------------------------------------------------------------
# Telemetry parsing. Units are fixed: energy is Wh, time is seconds, cost is
# a monetary amount in the configured currency, GPU is percent, cloud prices
# are USD.
# ---------------------------------------------------------------------------


def _energy_wh(d):
    value = _num(_pick(d, ("energy_wh", "wh", "consumed_wh", "session_wh")))
    if value is not None:
        return value
    kwh = _num(_pick(d, ("energy_kwh", "kwh", "consumed_kwh", "session_kwh")))
    if kwh is not None:
        return kwh * 1000.0
    return None


def _seconds(d):
    return _num(_pick(d, (
        "seconds", "duration_s", "duration_seconds", "elapsed_s",
        "elapsed_seconds", "time_used_s", "compute_seconds",
    )))


def _tokens(d):
    value = _pick(d, (
        "tokens", "token_equivalent", "tokens_equivalent",
        "equivalent_tokens", "token_equiv",
    ))
    numeric = _num(value)
    if numeric is not None:
        return numeric
    if isinstance(value, str):
        return value
    return None


def _normalise_cloud(raw):
    if not isinstance(raw, list):
        return None
    providers = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _pick(item, ("name", "provider", "model", "label"))
        cost = _num(_pick(item, ("cost", "estimated_cost", "price")))
        rate = _num(_pick(item, ("usd_per_mtok_blended", "usd_per_mtok", "rate_per_mtok")))
        cost_today = _num(_pick(item, ("cost_today", "today_cost", "today")))
        cost_30d = _num(_pick(item, ("cost_30d", "month_cost", "cost_month", "last_30d")))
        if not isinstance(name, str) or len(name) < 1:
            continue
        if cost is None and rate is None and cost_today is None and cost_30d is None:
            continue
        providers.append({
            "name": name, "cost": cost, "rate": rate,
            "cost_today": cost_today, "cost_30d": cost_30d,
        })
    return providers


def _normalise_session(d):
    result = {
        "gpu": _num(_pick(d, ("gpu_pct", "gpu_percent", "gpu_usage_pct", "gpu_usage", "gpu"))),
        "seconds": _seconds(d),
        "wh": _energy_wh(d),
        "cost": _num(_pick(d, ("cost", "session_cost", "estimated_cost", "spend"))),
        "tokens": _tokens(d),
    }
    if result["gpu"] is not None:
        result["gpu"] = _clamp(result["gpu"], 0.0, 100.0)
    # Only present when the sender includes them; None is meaningful (e.g. Netio stale).
    if "power_w" in d:
        result["power_w"] = _num(d.get("power_w"))
    if "active" in d:
        result["active"] = _truthy(d.get("active"))
    return result


def _llm_str(value, limit):
    if value is None:
        return ""
    return _ascii_label(str(value))[:limit]


def _normalise_llm(d):
    """Model + GPU details from Node-RED (Ollama /api/ps + nvidia-smi via EventGhost)."""
    if not isinstance(d, dict):
        return None
    model = _llm_str(_pick(d, ("model", "name"), ""), 48)
    if model.endswith(":latest"):
        model = model[:-7]
    return {
        "model": model,
        "params": _llm_str(_pick(d, ("params", "parameter_size"), ""), 10),
        "quant": _llm_str(_pick(d, ("quant", "quantization", "quantization_level"), ""), 12),
        "ctx": _num(_pick(d, ("ctx", "context", "context_length"))),
        "offload": _num(_pick(d, ("gpu_offload_pct", "offload_pct"))),
        "vram_used": _num(_pick(d, ("vram_used_gb",))),
        "vram_total": _num(_pick(d, ("vram_total_gb",))),
        "temp": _num(_pick(d, ("temp_c", "gpu_temp_c"))),
        "gpu_w": _num(_pick(d, ("gpu_power_w",))),
    }


def _normalise_today(d):
    return {
        "seconds": _seconds(d),
        "wh": _energy_wh(d),
        "cost": _num(_pick(d, ("cost", "spend", "estimated_cost"))),
        "tokens": _tokens(d),
    }


def _day_label(item):
    label = _pick(item, ("label", "dow", "weekday"))
    if isinstance(label, str) and len(label) > 0:
        return label[:3].upper()
    date = _pick(item, ("date", "day"))
    if isinstance(date, str):
        return _weekday_from_date(date)
    return None


def _normalise_hist(d):
    daily_raw = _pick(d, ("daily", "days", "series", "daily_usage"))
    daily = []
    if isinstance(daily_raw, list):
        start = 0
        if len(daily_raw) > 30:
            start = len(daily_raw) - 30
        for item in daily_raw[start:]:
            if isinstance(item, dict):
                daily.append({
                    "wh": _energy_wh(item),
                    "seconds": _seconds(item),
                    "cost": _num(_pick(item, ("cost", "spend", "estimated_cost"))),
                    "tokens": _tokens(item),
                    "label": _day_label(item),
                })
            else:
                value = _num(item)
                daily.append({"wh": value, "seconds": None, "cost": None, "tokens": None, "label": None})
    else:
        daily = None
    return {
        "wh": _energy_wh(d),
        "seconds": _seconds(d),
        "cost": _num(_pick(d, ("cost", "total_cost", "estimated_cost", "spend"))),
        "tokens": _tokens(d),
        "avg_wh": _num(_pick(d, ("avg_wh", "daily_avg_wh", "average_wh"))),
        "avg_cost": _num(_pick(d, ("avg_cost", "daily_avg_cost", "average_cost"))),
        "daily": daily,
    }


def _normalise_payload(data):
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict) and not ("session" in data or "last_30d" in data or "history" in data):
        data = inner

    out = {}
    session_raw = _pick(data, ("session", "current_session", "current"))
    if isinstance(session_raw, dict):
        out["session"] = _normalise_session(session_raw)
    elif (
        ("gpu_pct" in data) or ("gpu" in data) or
        ("elapsed_s" in data) or ("duration_s" in data) or
        ("seconds" in data) or ("energy_wh" in data) or
        ("cost" in data) or ("tokens" in data) or
        ("cloud" in data) or ("started" in data)
    ):
        out["session"] = _normalise_session(data)
        if "started" in data and (
            ("seconds" in data) or ("energy_wh" in data) or ("cost" in data)
        ):
            out["session_full"] = True

    today_raw = _pick(data, ("today", "today_stats", "current_day"))
    if isinstance(today_raw, dict):
        out["today"] = _normalise_today(today_raw)

    # "llm": null from Node-RED means the PC info went stale -> clear it.
    if "llm" in data:
        out["llm"] = _normalise_llm(data.get("llm"))
    elif isinstance(session_raw, dict) and "llm" in session_raw:
        out["llm"] = _normalise_llm(session_raw.get("llm"))

    cloud_raw = _pick(data, ("cloud", "providers"))
    if cloud_raw is None and isinstance(session_raw, dict):
        cloud_raw = _pick(session_raw, ("cloud", "providers"))
    if isinstance(cloud_raw, list):
        out["cloud"] = _normalise_cloud(cloud_raw)
        basis = None
        if isinstance(session_raw, dict):
            basis = _tokens(session_raw)
        if basis is None:
            basis = _tokens(data)
        out["cloud_tokens"] = basis

    hist_raw = _pick(data, ("last_30d", "history", "stats_30d", "month", "thirty_days", "last30d"))
    if isinstance(hist_raw, dict):
        out["hist"] = _normalise_hist(hist_raw)

    currency = _pick(data, ("currency", "cost_currency"))
    if currency is None and isinstance(session_raw, dict):
        currency = _pick(session_raw, ("currency", "cost_currency"))
    if currency is None and isinstance(hist_raw, dict):
        currency = _pick(hist_raw, ("currency", "cost_currency"))
    if currency is not None:
        out["currency"] = _clean_currency(currency)
    return out


def _merge_payload(payload):
    if "session" in payload:
        merged_session = payload["session"]
        if payload.get("session_full"):
            for key in ("gpu", "seconds", "wh", "cost", "tokens"):
                ST["data"]["session"][key] = merged_session.get(key)
        else:
            for key in ("gpu", "seconds", "wh", "cost", "tokens"):
                if merged_session.get(key) is not None:
                    ST["data"]["session"][key] = merged_session.get(key)
        for key in ("power_w", "active"):
            if key in merged_session:
                ST["data"]["session"][key] = merged_session[key]
    if "llm" in payload:
        ST["data"]["llm"] = payload["llm"]
    if "today" in payload:
        for key in ("seconds", "wh", "cost", "tokens"):
            value = payload["today"].get(key)
            if value is not None:
                ST["data"]["today"][key] = value
    if "hist" in payload:
        merged_hist = payload["hist"]
        for key in ("wh", "seconds", "cost", "tokens", "avg_wh", "avg_cost"):
            if merged_hist.get(key) is not None:
                ST["data"]["hist"][key] = merged_hist.get(key)
        if merged_hist.get("daily") is not None:
            ST["data"]["hist"]["daily"] = merged_hist.get("daily")
    if "cloud" in payload:
        ST["data"]["cloud"] = payload["cloud"] if payload["cloud"] is not None else []
        basis = payload.get("cloud_tokens")
        if basis is None:
            basis = ST["data"]["session"].get("tokens")
        ST["data"]["cloud_tokens"] = basis
    if "currency" in payload and payload["currency"]:
        ST["data"]["currency"] = payload["currency"]


def _process_raw():
    raw = ST["raw"]
    if raw is None:
        return
    ST["raw"] = None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode()
        except Exception:
            return
    try:
        payload = json.loads(raw)
    except Exception:
        ST["mqtt_err"] = "BAD TELEMETRY"
        return
    normalised = _normalise_payload(payload)
    _merge_payload(normalised)
    ST["last_msg_ms"] = _ticks()
    ST["last_age"] = 0
    ST["mqtt_state"] = "LIVE"
    ST["mqtt_err"] = ""
    ST["dirty"] = True


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def _today_value(key):
    # Prefer the explicit "today" block; fall back to the newest daily entry.
    value = ST["data"]["today"].get(key)
    if value is not None:
        return value
    daily = ST["data"]["hist"].get("daily") or []
    if len(daily) > 0:
        return daily[len(daily) - 1].get(key)
    return None


def _daily_avg(key):
    hist = ST["data"]["hist"]
    explicit = hist.get("avg_" + key)
    if explicit is not None:
        return explicit
    daily = hist.get("daily") or []
    total = 0.0
    found = False
    for item in daily:
        value = item.get(key)
        if isinstance(value, (int, float)):
            total += value
            found = True
    if found and len(daily) > 0:
        return total / len(daily)
    month = hist.get(key)
    if isinstance(month, (int, float)):
        return month / 30.0
    return None


def _week(key):
    daily = ST["data"]["hist"].get("daily") or []
    items = daily[-7:]
    values = []
    labels = []
    count = len(items)
    for i in range(count):
        item = items[i]
        value = item.get(key)
        values.append(value if isinstance(value, (int, float)) else None)
        label = item.get("label")
        back = count - 1 - i
        if back == 0:
            labels.append("TODAY" if not label else label)
        elif label:
            labels.append(label)
        else:
            labels.append("-%dD" % back)
    return values, labels


def _week_total(key):
    values, _ = _week(key)
    total = 0.0
    found = False
    for value in values:
        if value is not None:
            total += value
            found = True
    return total if found else None


def _as_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _cloud_rows():
    today_tokens = _as_number(_today_value("tokens"))
    month_tokens = _as_number(ST["data"]["hist"].get("tokens"))
    basis = _as_number(ST["data"].get("cloud_tokens"))
    rows = []
    for item in ST["data"]["cloud"]:
        if not isinstance(item, dict):
            continue
        rate = item.get("rate")
        if rate is None and item.get("cost") is not None and basis:
            rate = item["cost"] / basis * 1000000.0
        today = item.get("cost_today")
        if today is None and rate is not None and today_tokens is not None:
            today = today_tokens * rate / 1000000.0
        month = item.get("cost_30d")
        if month is None and rate is not None and month_tokens is not None:
            month = month_tokens * rate / 1000000.0
        rows.append({"name": item.get("name", ""), "today": today, "month": month, "rate": rate})
    # Sort cheapest first by 30-day forecast, then today, then rate.
    def sort_key(row):
        for key in ("month", "today", "rate"):
            if row[key] is not None:
                return row[key]
        return 1e18
    for i in range(1, len(rows)):
        key = rows[i]
        j = i - 1
        while j >= 0 and sort_key(rows[j]) > sort_key(key):
            rows[j + 1] = rows[j]
            j -= 1
        rows[j + 1] = key
    return rows


# ---------------------------------------------------------------------------
# UI primitives
# ---------------------------------------------------------------------------


def _draw_frame():
    cv.fillRect(0, 0, W, H, COLOR_BG)
    try:
        cv.drawCircle(CX, CY, 224, COLOR_OUTER)
        cv.drawCircle(CX, CY, 221, 0x0C141D)
        for i in range(24):
            angle = i * math.pi / 12.0
            x0 = CX + int(211 * math.cos(angle))
            y0 = CY + int(211 * math.sin(angle))
            x1 = CX + int(219 * math.cos(angle))
            y1 = CY + int(219 * math.sin(angle))
            cv.drawLine(x0, y0, x1, y1, 0x17242F)
    except Exception:
        pass


def _status():
    if ST["toast"]:
        return ST["toast"], ST["toast_col"]
    cfg = ST["cfg"]
    if not cfg.get("ip"):
        return "SET SERVER", COLOR_MUTED
    if not ST["wifi"]:
        return ("WIFI RETRYING" if _saved_ssid() else "NO WIFI"), COLOR_RED
    if not cfg.get("validated") or not isinstance(cfg.get("mqtt"), dict):
        return "SERVER OFFLINE", COLOR_AMBER
    client = ST["mqtt"]
    if client is None:
        if ST["mqtt_state"] == "CONNECTING":
            return "CONNECTING", COLOR_BLUE
        return "OFFLINE", COLOR_RED
    if ST["last_msg_ms"] is None:
        return "WAIT DATA", COLOR_BLUE
    age = _ago_ms(ST["last_msg_ms"]) // 1000
    if age <= 12:
        return "LIVE", COLOR_PRIMARY
    if age > 999:
        age = 999
    return "STALE %ds" % age, COLOR_AMBER


def _draw_status():
    text, colour = _status()
    width = _text_w(cv, text, 12)
    total = 8 + 8 + width
    x = (W - total) // 2
    try:
        cv.fillCircle(x + 4, 31, 4, colour)
    except Exception:
        pass
    _put(cv, text, x + 16, 24, 12, COLOR_TEXT if not ST["toast"] else colour, COLOR_BG)


def _draw_header(title, subtitle):
    _put_center(cv, title, 44, 24, COLOR_TEXT, COLOR_BG)
    if subtitle:
        _put_center(cv, subtitle, 74, 12, COLOR_MUTED, COLOR_BG)


def _draw_arrows(left=True, right=True):
    try:
        if left:
            cv.fillTriangle(30, 233, 42, 224, 42, 242, 0x2A3E52)
        if right:
            cv.fillTriangle(436, 233, 424, 224, 424, 242, 0x2A3E52)
    except Exception:
        pass


def _draw_dots(count, current, y):
    for i in range(count):
        x = CX + int((i - (count - 1) / 2.0) * 18)
        try:
            if i == current:
                cv.fillRoundRect(x - 7, y - 3, 14, 6, 3, COLOR_PRIMARY)
            else:
                cv.fillCircle(x, y, 3, COLOR_DIM)
        except Exception:
            pass


def _draw_footer_hint():
    if ST["page"] == 0:
        text = "ORANGE SETUP   HOLD BLUE RESET"
    else:
        text = "SWIPE OR BLUE FOR NEXT"
    _put_center(cv, text, 400, 12, COLOR_DIM, COLOR_BG)


def _draw_card(x, y, w, h, label, value, accent, sizes=(24, 18, 14)):
    try:
        cv.fillRoundRect(x, y, w, h, 13, COLOR_PANEL)
        cv.fillRoundRect(x + 11, y + 12, 3, h - 24, 2, accent)
    except Exception:
        pass
    _put(cv, label, x + 22, y + 9, 12, COLOR_MUTED, COLOR_PANEL)
    px = _fit_px(cv, value, w - 32, sizes)
    _put(cv, value, x + 22, y + 24 + (_line_h(sizes[0]) - _line_h(px)) // 2, px, COLOR_TEXT, COLOR_PANEL)


# ---------------------------------------------------------------------------
# Dashboard page 0 - live session + today
# ---------------------------------------------------------------------------


def _llm_details(llm):
    """One-line model summary, e.g. '8.2B  Q4_K_M  8K CTX  6.3/8GB'. Returns (text, colour)."""
    parts = []
    if llm.get("params"):
        parts.append(llm["params"].upper())
    if llm.get("quant"):
        parts.append(llm["quant"].upper())
    ctx = llm.get("ctx")
    if ctx:
        parts.append("%dK CTX" % int(ctx / 1024.0 + 0.5) if ctx >= 1024 else "%d CTX" % int(ctx))
    used = llm.get("vram_used")
    total = llm.get("vram_total")
    if used is not None and total:
        parts.append("%.1f/%.0fGB" % (used, total))
    offload = llm.get("offload")
    colour = COLOR_MUTED
    if offload is not None and offload < 99.5:
        # Model did not fit in VRAM: part runs on the CPU, which is far slower.
        parts.append("%d%% GPU" % int(offload + 0.5))
        colour = COLOR_AMBER
    return "  ".join(parts), colour


def _draw_live_header():
    llm = ST["data"].get("llm")
    session = ST["data"]["session"]
    model = llm.get("model") if isinstance(llm, dict) else ""
    if not model:
        _put_center(cv, "LIVE SESSION", 44, 24, COLOR_TEXT, COLOR_BG)
        if isinstance(llm, dict):
            _put_center(cv, "NO MODEL LOADED", 74, 12, COLOR_DIM, COLOR_BG)
        return
    active = session.get("active")
    _put_center_fit(cv, model, 44, (24, 18, 16), COLOR_PRIMARY if active else COLOR_TEXT, COLOR_BG, 300)
    text, colour = _llm_details(llm)
    if active is False:
        text = ("IDLE  " + text) if text else "LOADED  IDLE"
    if text:
        _put_center(cv, _fit_text(text, 12, 310), 74, 12, colour, COLOR_BG)


def _draw_gauge(gpu):
    centre_y = 166
    radius = 64
    segments = 36
    active = 0
    if gpu is not None:
        active = int(round((_clamp(gpu, 0.0, 100.0) / 100.0) * segments))
        active = int(_clamp(active, 0, segments))

    for i in range(segments):
        angle = math.radians(135.0 + (270.0 * i / float(segments - 1)))
        x = CX + int(radius * math.cos(angle))
        y = centre_y + int(radius * math.sin(angle))
        try:
            if i < active:
                cv.fillCircle(x, y, 4, COLOR_PRIMARY)
            else:
                cv.fillCircle(x, y, 3, COLOR_TRACK)
        except Exception:
            pass

    gpu_text = "--" if gpu is None else "%d%%" % int(round(gpu))
    _put_center(cv, gpu_text, centre_y - 26, 40, COLOR_TEXT, COLOR_BG)
    label = "GPU"
    llm = ST["data"].get("llm")
    temp = llm.get("temp") if isinstance(llm, dict) else None
    if temp is not None:
        label = "GPU  %dC" % int(temp + 0.5)
    _put_center(cv, label, centre_y + 26, 12, COLOR_AMBER if temp is not None and temp >= 83 else COLOR_MUTED, COLOR_BG)


def _draw_today_strip(y):
    currency = ST["data"]["currency"]
    _put_center(cv, "TODAY" + ("  /  " + currency if currency else ""), y, 12, COLOR_DIM, COLOR_BG)
    px0 = 84
    pw = 298
    py = y + 16
    ph = 46
    try:
        cv.fillRoundRect(px0, py, pw, ph, 12, COLOR_PANEL)
        cv.drawLine(px0 + pw // 3, py + 9, px0 + pw // 3, py + ph - 9, COLOR_LINE)
        cv.drawLine(px0 + 2 * pw // 3, py + 9, px0 + 2 * pw // 3, py + ph - 9, COLOR_LINE)
    except Exception:
        pass
    cost = _today_value("cost")
    items = (
        ("SPEND", "--" if cost is None else _money_decimals(cost), COLOR_AMBER),
        ("AI TIME", _fmt_duration(_today_value("seconds"), True), COLOR_TEXT),
        ("ENERGY", _fmt_energy(_today_value("wh")), COLOR_PRIMARY),
    )
    col_w = pw // 3
    for i in range(3):
        label, value, colour = items[i]
        cx = px0 + i * col_w
        px = _fit_px(cv, value, col_w - 10, (16, 14, 12))
        _put_center_x(cv, value, cx, col_w, py + 6 + (_line_h(16) - _line_h(px)) // 2, px, colour, COLOR_PANEL)
        _put_center_x(cv, label, cx, col_w, py + 27, 12, COLOR_MUTED, COLOR_PANEL)


def _page_session():
    session = ST["data"]["session"]
    _draw_gauge(session["gpu"])

    if session["seconds"] is None:
        if not ST["cfg"].get("ip"):
            line = "ORANGE TO CONFIGURE"
        else:
            line = "WAITING FOR TELEMETRY"
        _put_center(cv, line, 222, 16, COLOR_MUTED, COLOR_BG)
    else:
        _put_center(cv, "SESSION  " + _fmt_duration(session["seconds"], False), 220, 18, COLOR_TEXT, COLOR_BG)

    currency = ST["data"]["currency"]
    cost = session["cost"]
    title = "COST " + currency if currency else "COST"
    _draw_card(79, 250, 150, 54, title, _money_decimals(cost), COLOR_AMBER)
    power_w = session.get("power_w")
    energy_label = "ENERGY  %dW" % int(power_w + 0.5) if power_w is not None else "ENERGY"
    _draw_card(237, 250, 150, 54, energy_label, _fmt_energy(session["wh"]), COLOR_PRIMARY)

    _draw_today_strip(314)


# ---------------------------------------------------------------------------
# Dashboard pages 1 & 2 - 7 day charts with labels + 30 day figures
# ---------------------------------------------------------------------------


def _draw_week_chart(values, labels, accent, accent_mid, fmt, avg, top, baseline):
    x0 = 86
    width = 294
    slots = 7
    step = width / float(slots)
    bar_w = 24
    max_h = baseline - top - 16

    max_value = None
    for value in values:
        if value is not None and value > 0:
            if max_value is None or value > max_value:
                max_value = value

    try:
        cv.drawLine(x0, baseline, x0 + width, baseline, COLOR_LINE)
    except Exception:
        pass

    if max_value is None:
        _put_center(cv, "NO DAILY DATA", baseline - max_h // 2 - 8, 16, COLOR_DIM, COLOR_BG)
        return

    if avg is not None and avg > max_value:
        max_value = avg

    # Dotted daily-average guide.
    if avg is not None and avg > 0:
        ya = baseline - int((avg / max_value) * max_h)
        try:
            xx = x0
            while xx < x0 + width:
                cv.drawLine(xx, ya, xx + 3, ya, COLOR_DIM)
                xx += 7
        except Exception:
            pass

    count = len(values)
    start_slot = slots - count
    for i in range(count):
        value = values[i]
        slot = start_slot + i
        cx = x0 + int(slot * step + step / 2.0)
        x = cx - bar_w // 2
        latest = i == count - 1
        if value is None or value <= 0:
            try:
                cv.fillRect(x, baseline - 2, bar_w, 2, COLOR_TRACK)
            except Exception:
                pass
            _put_center_x(cv, "0", x - 8, bar_w + 16, baseline - 18, 12, COLOR_DIM, COLOR_BG)
        else:
            height = int((value / max_value) * max_h)
            if height < 3:
                height = 3
            try:
                cv.fillRoundRect(x, baseline - height, bar_w, height, 4, accent if latest else accent_mid)
            except Exception:
                pass
            label = fmt(value)
            _put_center_x(cv, label, x - 10, bar_w + 20, baseline - height - 16, 12,
                          COLOR_TEXT if latest else COLOR_MUTED, COLOR_BG)
        day = labels[i] if i < len(labels) else ""
        if day == "TODAY":
            day = "NOW"
        _put_center_x(cv, day, x - 10, bar_w + 20, baseline + 5, 12,
                      accent if latest else COLOR_DIM, COLOR_BG)


def _page_energy():
    hist = ST["data"]["hist"]
    values, labels = _week("wh")
    avg = _daily_avg("wh")
    _draw_week_chart(values, labels, COLOR_PRIMARY, COLOR_PRIMARY_M, _short_wh, avg, 96, 212)

    _put_center_fit(cv, _fmt_energy(hist["wh"]), 236, (24, 18), COLOR_TEXT, COLOR_BG, 300)
    _put_center(cv, "30 DAY TOTAL", 266, 12, COLOR_MUTED, COLOR_BG)

    _draw_card(79, 286, 150, 54, "DAILY AVG", _fmt_energy(avg), COLOR_PRIMARY, (18, 16, 14))
    _draw_card(237, 286, 150, 54, "AI TIME 30D", _fmt_duration(hist["seconds"], True), COLOR_BLUE, (18, 16, 14))

    extra = []
    week = _week_total("wh")
    if week is not None:
        extra.append("7D " + _fmt_energy(week))
    if hist["wh"] is not None and hist["seconds"]:
        watts = hist["wh"] / (hist["seconds"] / 3600.0)
        extra.append("AVG DRAW %d W" % int(watts + 0.5))
    if extra:
        _put_center(cv, "   ".join(extra), 352, 12, COLOR_MUTED, COLOR_BG)


def _page_cost():
    hist = ST["data"]["hist"]
    currency = ST["data"]["currency"]
    values, labels = _week("cost")
    avg = _daily_avg("cost")
    _draw_week_chart(values, labels, COLOR_AMBER, COLOR_AMBER_M, _short_money, avg, 96, 212)

    _put_center_fit(cv, _fmt_money(hist["cost"], currency), 236, (24, 18), COLOR_AMBER, COLOR_BG, 300)
    _put_center(cv, "30 DAY TOTAL", 266, 12, COLOR_MUTED, COLOR_BG)

    _draw_card(79, 286, 150, 54, "DAILY AVG", _money_decimals(avg), COLOR_AMBER, (18, 16, 14))
    _draw_card(237, 286, 150, 54, "7 DAY TOTAL", _money_decimals(_week_total("cost")), COLOR_AMBER, (18, 16, 14))

    if hist["cost"] is not None and hist["seconds"]:
        per_hour = hist["cost"] / (hist["seconds"] / 3600.0)
        _put_center(cv, "COST PER AI HOUR  " + _fmt_money(per_hour, currency), 352, 12, COLOR_MUTED, COLOR_BG)


# ---------------------------------------------------------------------------
# Dashboard page 3 - tokens + cloud forecast table
# ---------------------------------------------------------------------------


def _provider_parts(name):
    text = str(name).strip()
    words = text.split(" ")
    if len(words) == 0 or len(text) == 0:
        return "?", text
    first = words[0].lower()
    model = " ".join(words[1:])
    prefix = {
        "openai": ("OPENAI", ""),
        "anthropic": ("CLAUDE", "claude "),
        "claude": ("CLAUDE", ""),
        "google": ("GEMINI", "gemini "),
        "gemini": ("GEMINI", ""),
        "deepseek": ("DEEPSEEK", "deepseek "),
        "mistral": ("MISTRAL", ""),
        "xai": ("GROK", "grok "),
        "grok": ("GROK", ""),
    }.get(first)
    if prefix is None:
        provider = words[0].upper()
    else:
        provider = prefix[0]
        strip = prefix[1]
        if strip and model.lower().startswith(strip):
            model = model[len(strip):]
    return provider, model


def _page_tokens():
    session_tokens = ST["data"]["session"].get("tokens")
    if session_tokens is not None:
        _put_center(cv, "SESSION ~" + _fmt_tokens(session_tokens), 74, 12, COLOR_MUTED, COLOR_BG)

    _draw_card(79, 92, 150, 54, "TODAY", _fmt_tokens(_today_value("tokens")), COLOR_BLUE)
    _draw_card(237, 92, 150, 54, "30 DAYS", _fmt_tokens(ST["data"]["hist"].get("tokens")), COLOR_BLUE)

    rows = _cloud_rows()
    left = 76
    col_today = 304
    col_month = 390
    _put(cv, "CLOUD EQUIV. USD", left, 156, 12, COLOR_MUTED, COLOR_BG)
    _put_right(cv, "TODAY", col_today, 156, 12, COLOR_MUTED, COLOR_BG)
    _put_right(cv, "30D", col_month, 156, 12, COLOR_MUTED, COLOR_BG)
    try:
        cv.drawLine(left, 173, col_month, 173, COLOR_LINE)
    except Exception:
        pass

    if not rows:
        _put_center(cv, "NO CLOUD PRICING", 236, 18, COLOR_MUTED, COLOR_BG)
        _put_center(cv, "ADD PROVIDERS IN NODE-RED", 262, 12, COLOR_DIM, COLOR_BG)
    else:
        shown = len(rows)
        if shown > 6:
            shown = 6
        for i in range(shown):
            row = rows[i]
            y = 177 + i * 28
            cheapest = i == 0
            if cheapest:
                try:
                    cv.fillRoundRect(left - 6, y, col_month - left + 12, 26, 8, COLOR_PANEL_HI)
                except Exception:
                    pass
            bg = COLOR_PANEL_HI if cheapest else COLOR_BG
            provider, model = _provider_parts(row["name"])
            pw = _text_w(cv, provider, 12)
            _put(cv, provider, left, y + 6, 12, COLOR_PRIMARY if cheapest else COLOR_MUTED, bg)
            name_room = 228 - (left + pw + 6)
            if model and name_room > 20:
                _put(cv, _fit_text(model, 12, name_room), left + pw + 6, y + 6, 12, COLOR_TEXT, bg)
            _put_right(cv, _fmt_usd(row["today"]), col_today, y + 6, 12, COLOR_TEXT, bg)
            _put_right(cv, _fmt_usd(row["month"]), col_month, y + 6, 12, COLOR_AMBER, bg)
        if len(rows) > shown:
            _put_center(cv, "+%d MORE" % (len(rows) - shown), 177 + shown * 28 + 4, 12, COLOR_DIM, COLOR_BG)

    currency = ST["data"]["currency"]
    local_today = _today_value("cost")
    local_month = ST["data"]["hist"].get("cost")
    if local_today is not None or local_month is not None:
        text = "YOUR PC  %s  /  %s" % (
            _fmt_money(local_today, currency), _fmt_money(local_month, currency))
        _put_center(cv, _fit_text(text, 12, 300), 352, 12, COLOR_PRIMARY, COLOR_BG)


def _draw_dashboard():
    _draw_frame()
    _draw_status()
    page = ST["page"]
    if page == 0:
        _draw_live_header()
        _page_session()
    elif page == 1:
        currency = ""
        _draw_header("ENERGY", "LAST 7 DAYS  /  Wh")
        _page_energy()
    elif page == 2:
        currency = ST["data"]["currency"]
        _draw_header("SPEND", "LAST 7 DAYS" + ("  /  " + currency if currency else ""))
        _page_cost()
    else:
        _draw_header("TOKENS", None)
        _page_tokens()
    _draw_arrows()
    _draw_footer_hint()
    _draw_dots(PAGE_COUNT, ST["page"], 430)


# ---------------------------------------------------------------------------
# Wi-Fi join screens (list / password / connecting)
# ---------------------------------------------------------------------------


def _wifi_pages():
    count = len(ST["wifi_aps"])
    pages = (count + 4) // 5
    if pages < 1:
        pages = 1
    return pages


def _wifi_row_rects():
    rows = []
    for i in range(5):
        rows.append((72, 116 + i * 52, 322, 46))
    return rows


def _draw_signal_bars(x, baseline, rssi):
    if rssi is None:
        level = 0
    elif rssi >= -55:
        level = 4
    elif rssi >= -67:
        level = 3
    elif rssi >= -75:
        level = 2
    elif rssi >= -85:
        level = 1
    else:
        level = 0
    for i in range(4):
        height = 4 + i * 3
        colour = COLOR_PRIMARY if i < level else COLOR_TRACK
        try:
            cv.fillRoundRect(x + i * 5, baseline - height, 3, height, 1, colour)
        except Exception:
            pass


def _draw_wifi_rescan():
    try:
        cv.fillRoundRect(300, 46, 96, 26, 8, COLOR_PANEL)
    except Exception:
        pass
    _put_center_x(cv, "RESCAN", 300, 96, 51, 12, COLOR_TEXT, COLOR_PANEL)


def _wifi_subtitle():
    if ST["wifi_scanning"]:
        return "SCANNING...", COLOR_BLUE
    ssid = _connected_ssid()
    if ssid:
        label = _fit_text("CONNECTED: " + _ascii_label(ssid), 12, 360)
        return label, COLOR_PRIMARY
    return "SELECT A NETWORK", COLOR_MUTED


def _draw_wifi_list():
    _draw_frame()
    _draw_back()
    _draw_wifi_rescan()
    _put_center(cv, "WI-FI", 44, 24, COLOR_TEXT, COLOR_BG)
    subtitle, colour = _wifi_subtitle()
    _put_center(cv, subtitle, 76, 12, colour, COLOR_BG)

    aps = ST["wifi_aps"]
    pages = _wifi_pages()
    page = ST["wifi_page"]
    if page >= pages:
        page = pages - 1
        ST["wifi_page"] = page
    current = _connected_ssid()

    if len(aps) == 0:
        if ST["wifi_scanning"]:
            _put_center(cv, "LOOKING FOR NETWORKS", 238, 16, COLOR_MUTED, COLOR_BG)
        else:
            _put_center(cv, "NO NETWORKS FOUND", 238, 18, COLOR_MUTED, COLOR_BG)
            _put_center(cv, "TAP RESCAN TO TRY AGAIN", 266, 12, COLOR_DIM, COLOR_BG)
    else:
        start = page * 5
        for slot, rect in enumerate(_wifi_row_rects()):
            index = start + slot
            if index >= len(aps):
                break
            ap = aps[index]
            x, y, w, h = rect
            pressed = ST["press"] == "ap:%d" % index
            is_current = ap["ssid"] == current
            fill = COLOR_PRIMARY if pressed else (COLOR_PANEL_HI if is_current else COLOR_PANEL)
            fg = COLOR_BG if pressed else (COLOR_PRIMARY if is_current else COLOR_TEXT)
            try:
                cv.fillRoundRect(x, y, w, h, 11, fill)
            except Exception:
                pass
            ssid = _fit_text(_ascii_label(ap["ssid"]), 18, 218)
            _put(cv, ssid, x + 14, y + 6, 18, fg, fill)
            rssi = ap["rssi"]
            _put_right(cv, "%d" % rssi, x + w - 14, y + 5, 12, COLOR_MUTED, fill)
            security = "OPEN" if ap["security"] == 0 else "LOCK"
            security_colour = COLOR_BLUE if security == "OPEN" else COLOR_MUTED
            _put_right(cv, security, x + w - 14, y + 26, 12, security_colour, fill)
            _draw_signal_bars(x + w - 148, y + 35, rssi)

        if pages > 1:
            _draw_arrows()

    if ST["wifi_error"]:
        _put_center(cv, ST["wifi_error"], 382, 12, COLOR_RED, COLOR_BG)
        if pages > 1:
            _put_center(cv, "PAGE %d/%d" % (page + 1, pages), 400, 12, COLOR_DIM, COLOR_BG)
        else:
            _put_center(cv, "ORANGE = BACK", 400, 12, COLOR_DIM, COLOR_BG)
    elif len(aps) > 0:
        if pages > 1:
            _put_center(cv, "PAGE %d/%d  -  SWIPE" % (page + 1, pages), 382, 12, COLOR_DIM, COLOR_BG)
        else:
            _put_center(cv, "TAP A NETWORK TO JOIN", 382, 12, COLOR_DIM, COLOR_BG)
        _put_center(cv, "ORANGE = BACK", 400, 12, COLOR_DIM, COLOR_BG)
    else:
        _put_center(cv, "ORANGE = BACK", 400, 12, COLOR_DIM, COLOR_BG)


def _wifi_kb_rects():
    rows = []
    y = 158
    mode = ST["wifi_kb_mode"]
    if mode == "abc":
        layouts = [
            ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
            ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
            ["z", "x", "c", "v", "b", "n", "m", ".", "_", "-"],
        ]
    elif mode == "sym2":
        layouts = [
            ["\"", "'", "<", ">", "[", "]", "\\", "{", "|", "}"],
            ["`", "~"],
        ]
    else:
        layouts = [
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
            ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")"],
            ["-", "_", "=", "+", "/", ":", ";", ",", ".", "?"],
        ]
    key_w = 36
    gap = 4
    for layout in layouts:
        count = len(layout)
        total = count * key_w + (count - 1) * gap
        x = (W - total) // 2
        for key in layout:
            label = key
            if mode == "abc" and "a" <= key <= "z" and ST["wifi_shift"]:
                label = key.upper()
            rows.append((label, "char", label, x, y, key_w, 38))
            x += key_w + gap
        y += 42

    if mode == "abc":
        controls = [
            ("SHIFT", "shift", "", 80),
            ("123", "sym", "", 60),
            ("SPACE", "space", "", 124),
            ("DEL", "del", "", 60),
            ("OK", "ok", "", 64),
        ]
    else:
        controls = [
            ("ABC", "sym", "", 66),
            ("SYM1" if mode == "sym2" else "MORE", "more", "", 66),
            ("SPACE", "space", "", 120),
            ("DEL", "del", "", 56),
            ("OK", "ok", "", 76),
        ]
    total = 0
    for label, action, payload, width in controls:
        total += width
    total += (len(controls) - 1) * gap
    x = (W - total) // 2
    for label, action, payload, width in controls:
        rows.append((label, action, payload, x, y, width, 38))
        x += width + gap
    return rows


def _draw_wifi_key(label, x, y, w, h, pressed, active):
    special = label in ("SHIFT", "SPACE", "DEL", "123", "ABC", "MORE", "SYM1")
    is_ok = label == "OK"
    if pressed:
        fill = COLOR_PRIMARY
        fg = COLOR_BG
    elif active or is_ok:
        fill = COLOR_PRIMARY_D
        fg = COLOR_TEXT
    else:
        fill = COLOR_BTN
        fg = COLOR_TEXT
    try:
        cv.fillRoundRect(x, y, w, h, 8, fill)
        if not pressed:
            cv.drawRoundRect(x, y, w, h, 8, COLOR_LINE)
    except Exception:
        pass
    px = 12 if special else 16
    _put_center_x(cv, label, x, w, y + (h - _line_h(px)) // 2 - 1, px, fg, fill)


def _tail_text(text, limit):
    if len(text) <= limit:
        return text
    return "..." + text[-(limit - 3):]


def _draw_wifi_password():
    _draw_frame()
    _draw_back()
    _put_center(cv, "JOIN NETWORK", 42, 24, COLOR_TEXT, COLOR_BG)
    ssid = _fit_text(_ascii_label(ST["wifi_target_ssid"]), 12, 360)
    _put_center(cv, ssid, 74, 12, COLOR_MUTED, COLOR_BG)

    try:
        cv.fillRoundRect(60, 92, 346, 56, 13, COLOR_PANEL)
        cv.drawRoundRect(60, 92, 346, 56, 13, COLOR_PRIMARY_D)
    except Exception:
        pass
    _put(cv, "PASSWORD", 72, 97, 12, COLOR_MUTED, COLOR_PANEL)
    password = ST["wifi_target_pass"]
    if len(password) == 0:
        label = "ENTER PASSWORD"
        colour = COLOR_DIM
    else:
        label = _tail_text(password, 23)
        colour = COLOR_TEXT
    _put(cv, label, 72, 115, 18, colour, COLOR_PANEL)
    if len(password) < 63:
        width = _text_w(cv, label, 18)
        cursor_x = 72 + width + 3
        if cursor_x < 386:
            try:
                cv.fillRect(cursor_x, 116, 2, 20, COLOR_PRIMARY)
            except Exception:
                pass
    _put_right(cv, "%d" % len(password), 394, 97, 12, COLOR_DIM, COLOR_PANEL)

    for label, action, payload, x, y, w, h in _wifi_kb_rects():
        pressed = ST["press"] == "key:" + label
        active = (action == "shift" and ST["wifi_shift"])
        _draw_wifi_key(label, x, y, w, h, pressed, active)

    if ST["wifi_error"]:
        _put_center(cv, ST["wifi_error"], 335, 12, COLOR_RED, COLOR_BG)
    _put_center(cv, "ORANGE = BACK   /   TAP OK TO CONNECT", 354, 12, COLOR_DIM, COLOR_BG)


def _draw_wifi_connecting():
    _draw_frame()
    _draw_back()
    _put_center(cv, "WI-FI", 44, 24, COLOR_TEXT, COLOR_BG)
    _put_center(cv, "CONNECTING", 76, 12, COLOR_BLUE, COLOR_BG)
    ssid = _fit_text(_ascii_label(ST["wifi_target_ssid"]), 24, 340)
    _put_center(cv, ssid, 126, 24, COLOR_TEXT, COLOR_BG)

    elapsed = time.ticks_diff(_ticks(), ST["wifi_connect_start"])
    phase = (elapsed // 250) % 12
    for i in range(12):
        angle = math.radians(i * 30.0)
        x = CX + int(32 * math.cos(angle))
        y = 240 + int(32 * math.sin(angle))
        active = ((i - phase) % 12) < 4
        try:
            cv.fillCircle(x, y, 4 if active else 3, COLOR_PRIMARY if active else COLOR_TRACK)
        except Exception:
            pass
    _put_center(cv, ST["wifi_connect_status"], 300, 16, COLOR_MUTED, COLOR_BG)
    _put_center(cv, "ORANGE TO CANCEL", 390, 12, COLOR_DIM, COLOR_BG)


def _draw_wifi():
    if ST["wifi_screen"] == "password":
        _draw_wifi_password()
    elif ST["wifi_screen"] == "connecting":
        _draw_wifi_connecting()
    else:
        _draw_wifi_list()


# ---------------------------------------------------------------------------
# Settings: 3 swipeable pages - SERVER / WI-FI / DEVICE
# ---------------------------------------------------------------------------


def _key_rects():
    labels = ("1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "DEL")
    rects = []
    widths = 82
    gap = 10
    left = (W - (widths * 3 + gap * 2)) // 2
    for row in range(4):
        for col in range(3):
            index = row * 3 + col
            rects.append((labels[index], left + col * (widths + gap), 170 + row * 56, widths, 48))
    return rects


def _draw_back():
    try:
        cv.fillCircle(112, 66, 14, COLOR_PANEL)
        cv.fillTriangle(107, 66, 116, 59, 116, 73, COLOR_TEXT)
    except Exception:
        pass


def _draw_button(x, y, w, h, label, fill, fg, px=16, border=False):
    try:
        cv.fillRoundRect(x, y, w, h, 12, fill)
        if border:
            cv.drawRoundRect(x, y, w, h, 12, COLOR_LINE)
    except Exception:
        pass
    _put_center_x(cv, label, x, w, y + (h - _line_h(px)) // 2 - 1, px, fg, fill)


def _draw_cfg_status(y, default_text):
    status = ST["cfg_status"] if ST["cfg_status"] else default_text
    status = _fit_text(status, 12, 300)
    _put_center(cv, status, y, 12, ST["cfg_status_col"] if ST["cfg_status"] else COLOR_DIM, COLOR_BG)


def _draw_cfg_server():
    _put_center(cv, "SERVER", 44, 24, COLOR_TEXT, COLOR_BG)
    _draw_cfg_status(76, "NODE-RED ADDRESS")

    try:
        cv.fillRoundRect(68, 94, 330, 66, 14, COLOR_PANEL)
        cv.drawRoundRect(68, 94, 330, 66, 14, COLOR_PRIMARY_D)
    except Exception:
        pass
    _put(cv, "SERVER IP  /  PORT %d" % CONFIG_PORT, 82, 100, 12, COLOR_MUTED, COLOR_PANEL)
    ip_text = ST["draft_ip"]
    if ip_text == "":
        ip_text = "0.0.0.0"
        colour = COLOR_DIM
    else:
        colour = COLOR_TEXT
    _put(cv, ip_text, 82, 118, 24, colour, COLOR_PANEL)
    try:
        cv.fillRoundRect(324, 112, 58, 26, 8, COLOR_BTN)
    except Exception:
        pass
    _put_center_x(cv, "CLEAR", 324, 58, 117, 12, COLOR_MUTED, COLOR_BTN)

    for label, x, y, w, h in _key_rects():
        pressed = ST["press"] == "key:" + label
        fill = COLOR_PRIMARY if pressed else COLOR_BTN
        fg = COLOR_BG if pressed else COLOR_TEXT
        try:
            cv.fillRoundRect(x, y, w, h, 11, fill)
            if not pressed:
                cv.drawRoundRect(x, y, w, h, 11, COLOR_LINE)
        except Exception:
            pass
        px = 18 if label == "DEL" else 24
        _put_center_x(cv, label, x, w, y + (h - _line_h(px)) // 2 - 1, px, fg, fill)

    save_pressed = ST["press"] == "save"
    save_fill = COLOR_PRIMARY if save_pressed else COLOR_PRIMARY_D
    save_fg = COLOR_BG if save_pressed else COLOR_TEXT
    save_text = "TESTING" if ST["testing"] else "SAVE & TEST"
    _draw_button(155, 396, 156, 44, save_text, save_fill, save_fg)


def _draw_info_row(y, label, value, colour):
    _put(cv, label, 90, y + 3, 12, COLOR_MUTED, COLOR_PANEL)
    value = _fit_text(value, 16, 200)
    _put_right(cv, value, 376, y, 16, colour, COLOR_PANEL)


def _draw_cfg_wifi():
    _put_center(cv, "WI-FI", 44, 24, COLOR_TEXT, COLOR_BG)
    connected = ST["wifi"]
    _draw_cfg_status(76, "CONNECTED" if connected else "NOT CONNECTED")

    try:
        cv.fillRoundRect(72, 98, 322, 160, 14, COLOR_PANEL)
    except Exception:
        pass
    ssid = _ascii_label(_connected_ssid())
    rssi = _wifi_rssi()
    _draw_info_row(110, "NETWORK", ssid if ssid else "NONE", COLOR_PRIMARY if ssid else COLOR_DIM)
    if rssi is not None:
        _draw_info_row(148, "SIGNAL", "%d dBm" % rssi, COLOR_TEXT)
        _draw_signal_bars(180, 164, rssi)
    else:
        _draw_info_row(148, "SIGNAL", "--", COLOR_DIM)
    ip = _wifi_ip()
    _draw_info_row(186, "DEVICE IP", ip if ip else "--", COLOR_TEXT if ip else COLOR_DIM)
    saved = _ascii_label(_saved_ssid())
    _draw_info_row(224, "SAVED", saved if saved else "NONE", COLOR_TEXT if saved else COLOR_DIM)

    scan_pressed = ST["press"] == "scan"
    _draw_button(113, 272, 240, 48, "SCAN & JOIN",
                 COLOR_PRIMARY if scan_pressed else COLOR_PRIMARY_D,
                 COLOR_BG if scan_pressed else COLOR_TEXT)
    _draw_button(113, 330, 116, 42, "RECONNECT",
                 COLOR_PANEL_HI if ST["press"] == "reconnect" else COLOR_BTN, COLOR_TEXT, 12, True)
    armed = time.ticks_diff(ST["forget_armed_until"], _ticks()) > 0
    _draw_button(237, 330, 116, 42, "CONFIRM?" if armed else "FORGET",
                 COLOR_RED if armed else COLOR_BTN,
                 COLOR_BG if armed else COLOR_RED, 12, not armed)


def _settings_rows():
    ui = _ui()
    if not ST["rot_on"]:
        rot = ("OFF", COLOR_PANEL_HI, COLOR_MUTED)
    elif ST["rot_error"]:
        rot = ("ERR", COLOR_PANEL_HI, COLOR_RED)
    elif ST["rot_state"] in ("settle", "turn"):
        rot = ("SET", COLOR_AMBER_D, COLOR_AMBER)
    else:
        rot = ("ON", COLOR_PRIMARY_D, COLOR_TEXT)

    def onoff(flag):
        if flag:
            return ("ON", COLOR_PRIMARY_D, COLOR_TEXT)
        return ("OFF", COLOR_PANEL_HI, COLOR_MUTED)

    return [
        ("rot", "AUTO ROTATE", rot),
        ("bright", "BRIGHTNESS", (BRIGHT_LABELS[ui["bright"]], COLOR_PRIMARY_D, COLOR_TEXT)),
        ("haptic", "HAPTICS", onoff(ui["haptic"])),
        ("cycle", "AUTO CYCLE PAGES", onoff(ui["cycle"])),
        ("dim", "DIM WHEN IDLE", onoff(ui["dim"])),
    ]


def _settings_row_rect(i):
    return (78, 96 + i * 48, 310, 42)


def _draw_cfg_device():
    _put_center(cv, "DEVICE", 44, 24, COLOR_TEXT, COLOR_BG)
    _put_center(cv, "TAP TO CHANGE", 76, 12, COLOR_DIM, COLOR_BG)
    rows = _settings_rows()
    for i in range(len(rows)):
        key, label, pill = rows[i]
        x, y, w, h = _settings_row_rect(i)
        pressed = ST["press"] == "set:" + key
        fill = COLOR_PANEL_HI if pressed else COLOR_PANEL
        try:
            cv.fillRoundRect(x, y, w, h, 12, fill)
        except Exception:
            pass
        _put(cv, label, x + 18, y + (h - _line_h(16)) // 2, 16, COLOR_TEXT, fill)
        text, pill_bg, pill_fg = pill
        try:
            cv.fillRoundRect(x + w - 84, y + 9, 68, 24, 8, pill_bg)
        except Exception:
            pass
        _put_center_x(cv, text, x + w - 84, 68, y + 14, 12, pill_fg, pill_bg)

    if ST["rot_on"] and ST["rot_state"] in ("settle", "turn") and not ST["rot_error"]:
        hint = "HOLD UPRIGHT" if ST["rot_state"] == "settle" else "TURN RIGHT 90 DEG"
        _put_center(cv, hint, 344, 12, COLOR_AMBER, COLOR_BG)
    else:
        _draw_cfg_status(344, "")
    _put_center(cv, _default_client_id(), 364, 12, COLOR_DIM, COLOR_BG)


def _draw_config():
    _draw_frame()
    _draw_dots(CFG_PAGE_COUNT, ST["cfg_page"], 26)
    _draw_back()
    page = ST["cfg_page"]
    if page == 0:
        _draw_cfg_server()
    elif page == 1:
        _draw_cfg_wifi()
    else:
        _draw_cfg_device()
    _draw_arrows(page > 0, page < CFG_PAGE_COUNT - 1)


# ---------------------------------------------------------------------------
# Startup: saved Wi-Fi -> Node-RED config (REST) -> MQTT -> dashboard.
# Anything not saved yet opens settings instead.
# ---------------------------------------------------------------------------

BOOT_STEPS = (("wifi", "WI-FI"), ("server", "SERVER"), ("mqtt", "MQTT"))


def _draw_boot():
    _draw_frame()
    _put_center(cv, "AI POWER", 92, 24, COLOR_TEXT, COLOR_BG)
    _put_center(cv, "STOPWATCH DASHBOARD", 124, 12, COLOR_DIM, COLOR_BG)

    elapsed = _ago_ms(ST["boot_start"])
    phase = (elapsed // 250) % 12
    for i in range(12):
        angle = math.radians(i * 30.0)
        x = CX + int(32 * math.cos(angle))
        y = 222 + int(32 * math.sin(angle))
        active = ((i - phase) % 12) < 4
        try:
            cv.fillCircle(x, y, 4 if active else 3, COLOR_PRIMARY if active else COLOR_TRACK)
        except Exception:
            pass

    _put_center(cv, ST["boot_msg"], 282, 16, COLOR_TEXT, COLOR_BG)
    if ST["boot_detail"]:
        _put_center(cv, _fit_text(ST["boot_detail"], 12, 300), 308, 12, COLOR_MUTED, COLOR_BG)

    # WI-FI > SERVER > MQTT progress
    order = [key for key, label in BOOT_STEPS]
    current = order.index(ST["boot_stage"]) if ST["boot_stage"] in order else 0
    gap = "   >   "
    labels = [label for key, label in BOOT_STEPS]
    total = _text_w(cv, gap.join(labels), 12)
    x = (W - total) // 2
    for i in range(len(labels)):
        if i < current:
            colour = COLOR_PRIMARY
        elif i == current:
            colour = COLOR_AMBER
        else:
            colour = COLOR_DIM
        _put(cv, labels[i], x, 346, 12, colour, COLOR_BG)
        x += _text_w(cv, labels[i], 12)
        if i < len(labels) - 1:
            _put(cv, gap, x, 346, 12, COLOR_DIM, COLOR_BG)
            x += _text_w(cv, gap, 12)
    _put_center(cv, "ORANGE = SETTINGS", 396, 12, COLOR_DIM, COLOR_BG)


def _boot_set(stage, message, detail):
    ST["mode"] = "boot"
    ST["boot_stage"] = stage
    ST["boot_msg"] = message
    ST["boot_detail"] = detail
    ST["boot_start"] = _ticks()
    ST["boot_draw"] = _ticks()
    ST["dirty"] = True
    _render(True)            # on screen before the next (possibly blocking) step


def _boot_finish(text, colour):
    ST["boot_stage"] = ""
    ST["mode"] = "dashboard"
    ST["page"] = 0
    ST["last_input"] = _ticks()
    ST["last_cycle"] = _ticks()
    _set_toast(text, colour, 3500)
    ST["dirty"] = True


def _boot_to_settings(page, message, scan=False):
    ST["boot_stage"] = ""
    ST["mode"] = "config"
    ST["cfg_page"] = page
    ST["draft_ip"] = ST["cfg"].get("ip", "")
    ST["cfg_status"] = message
    ST["cfg_status_col"] = COLOR_AMBER
    ST["testing"] = False
    ST["press"] = ""
    ST["last_input"] = _ticks()
    ST["dirty"] = True
    if scan:
        _wifi_enter()        # straight to the network list
    else:
        _buzz(60)


def _boot_start():
    _wifi_check()
    has_wifi = bool(_saved_ssid())
    has_ip = _valid_ip(ST["cfg"].get("ip", ""))
    if not ST["wifi"] and not has_wifi:
        _boot_to_settings(1, "NO SAVED WI-FI", scan=True)
        return
    if not has_ip:
        if not ST["wifi"]:
            _wifi_connect_saved()          # joins in the background while the IP is typed
        _boot_to_settings(0, "ENTER NODE-RED SERVER IP")
        return
    ST["boot_attempts"] = 0
    if ST["wifi"]:
        _boot_set("server", "CONTACTING SERVER", "%s:%d" % (ST["cfg"]["ip"], CONFIG_PORT))
    else:
        _wifi_connect_saved()
        _boot_set("wifi", "CONNECTING WI-FI", _ascii_label(_saved_ssid()))


WIFI_FAIL_TEXT = {
    201: "NETWORK NOT FOUND",
    202: "WRONG PASSWORD",
    203: "CONNECTION FAILED",
    204: "HANDSHAKE TIMEOUT",
}


def _boot_tick():
    if ST["mode"] != "boot":
        return
    stage = ST["boot_stage"]
    now = _ticks()
    cfg = ST["cfg"]

    if stage == "wifi":
        connected = False
        status = None
        try:
            if WLAN is not None:
                connected = WLAN.isconnected()
                status = WLAN.status()
        except Exception:
            pass
        if connected:
            ST["wifi"] = True
            ST["last_wifi_check"] = now
            _boot_set("server", "CONTACTING SERVER", "%s:%d" % (cfg["ip"], CONFIG_PORT))
            return
        elapsed = _ago_ms(ST["boot_start"])
        failure = None
        if elapsed >= 3000 and status in WIFI_FAIL_TEXT:      # ignore stale codes from before connect()
            failure = WIFI_FAIL_TEXT[status]
        elif elapsed >= BOOT_WIFI_TIMEOUT_MS:
            failure = "TIMED OUT"
        if failure is None:
            if _ago_ms(ST["boot_draw"]) >= 250:
                ST["boot_draw"] = now
                ST["dirty"] = True
            return
        if status == 202:
            # Saved details are wrong: retrying will not help.
            _boot_to_settings(1, "SAVED WI-FI: WRONG PASSWORD")
            return
        ST["boot_attempts"] += 1
        if ST["boot_attempts"] < 2:
            _wifi_connect_saved()
            _boot_set("wifi", "RETRYING WI-FI", _ascii_label(_saved_ssid()) + "  -  " + failure)
            return
        # Router may still be booting (power cut): carry on to the dashboard and keep retrying.
        ST["last_wifi_retry"] = now
        _boot_finish("WI-FI: " + failure, COLOR_RED)
        return

    if stage == "server":
        details, error = _fetch_server_config(cfg["ip"])     # blocking; screen already says why
        if details is not None:
            old = cfg.get("mqtt")
            changed = not isinstance(old, dict)
            if not changed:
                for key in details:
                    if old.get(key) != details[key]:
                        changed = True
                        break
            cfg["mqtt"] = details
            if changed or not cfg.get("validated"):
                cfg["validated"] = True
                _save_config(cfg)
        elif not (cfg.get("validated") and isinstance(cfg.get("mqtt"), dict)):
            # Never reached this server: no cached MQTT details to fall back on.
            ST["next_fetch_ms"] = now + SERVER_RETRY_MS
            ST["mqtt_err"] = error
            _boot_finish("SERVER: " + error, COLOR_AMBER)
            return
        # else: server offline but the last good MQTT details are cached -> try them
        _boot_set("mqtt", "CONNECTING MQTT", "%s:%s" % (cfg["mqtt"].get("host"), cfg["mqtt"].get("port", 1883)))
        return

    if stage == "mqtt":
        ST["mqtt_state"] = "CONNECTING"
        ok, error = _mqtt_connect()
        if ok:
            _boot_finish("CONNECTED", COLOR_PRIMARY)
            _buzz(90)
        else:
            ST["mqtt_state"] = "OFFLINE"
            ST["mqtt_err"] = error
            ST["connect_fails"] = 1
            ST["next_connect_ms"] = _ticks() + 5000
            _boot_finish("MQTT: " + error, COLOR_AMBER)
        return


def _render(force):
    now = _ticks()
    if not force:
        if not ST["dirty"]:
            return
        if _ago_ms(ST["last_rendered"]) < 70:
            return
    ST["dirty"] = False
    if ST["mode"] == "config":
        _draw_config()
    elif ST["mode"] == "wifi":
        _draw_wifi()
    elif ST["mode"] == "boot":
        _draw_boot()
    else:
        _draw_dashboard()
    _push()
    ST["last_rendered"] = now


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _set_toast(text, colour, ms):
    ST["toast"] = text
    ST["toast_col"] = colour
    ST["toast_until"] = _ticks() + ms
    ST["dirty"] = True


def _buzz(ms):
    if not HAS_VIBRATION or not _ui()["haptic"]:
        return
    try:
        Power.setVibration(110)
        ST["vib_until"] = _ticks() + ms
    except Exception:
        ST["vib_until"] = 0


def _enter_config():
    ST["mode"] = "config"
    ST["cfg_page"] = 0
    ST["draft_ip"] = ST["cfg"].get("ip", "")
    ST["cfg_status"] = ""
    ST["cfg_status_col"] = COLOR_MUTED
    ST["testing"] = False
    ST["press"] = ""
    ST["dirty"] = True
    _buzz(70)


def _leave_config():
    ST["mode"] = "dashboard"
    ST["testing"] = False
    ST["press"] = ""
    ST["cfg_status"] = ""
    ST["dirty"] = True
    _buzz(50)


def _change_page(delta, quiet=False):
    ST["page"] = (ST["page"] + delta) % PAGE_COUNT
    ST["press"] = ""
    ST["dirty"] = True
    if not quiet:
        _buzz(55)


def _change_cfg_page(delta, wrap=False):
    page = ST["cfg_page"] + delta
    if wrap:
        page = page % CFG_PAGE_COUNT
    elif page < 0 or page >= CFG_PAGE_COUNT:
        return
    ST["cfg_page"] = page
    ST["cfg_status"] = ""
    ST["press"] = ""
    ST["forget_armed_until"] = 0
    ST["dirty"] = True
    _buzz(45)


def _save_and_test():
    ip = ST["draft_ip"]
    if not _valid_ip(ip):
        ST["cfg_status"] = "ENTER A VALID IPv4 ADDRESS"
        ST["cfg_status_col"] = COLOR_RED
        ST["dirty"] = True
        _buzz(90)
        return

    ST["testing"] = True
    ST["cfg"]["ip"] = ip
    ST["cfg"]["validated"] = False
    ST["cfg_status"] = "SAVING ADDRESS"
    ST["cfg_status_col"] = COLOR_BLUE
    if not _save_config(ST["cfg"]):
        ST["testing"] = False
        ST["cfg_status"] = "FLASH SAVE FAILED"
        ST["cfg_status_col"] = COLOR_RED
        ST["dirty"] = True
        _buzz(120)
        return

    ST["cfg_status"] = "TESTING REST + MQTT"
    ST["dirty"] = True
    _render(True)

    details, error = _fetch_server_config(ip)
    if details is None:
        ST["cfg"]["validated"] = False
        _save_config(ST["cfg"])
        ST["testing"] = False
        ST["cfg_status"] = error
        ST["cfg_status_col"] = COLOR_RED
        ST["dirty"] = True
        _buzz(120)
        return

    ST["cfg"]["mqtt"] = details
    ST["cfg"]["validated"] = True
    if not _save_config(ST["cfg"]):
        ST["cfg_status"] = "FLASH SAVE FAILED"
        ST["cfg_status_col"] = COLOR_RED
        ST["testing"] = False
        ST["dirty"] = True
        _buzz(120)
        return

    ST["data"] = _empty_data()
    ST["mqtt_state"] = "CONNECTING"
    ST["dirty"] = True
    _render(True)

    ST["last_msg_ms"] = None
    ST["last_age"] = -1
    ST["raw"] = None
    ok, mqtt_error = _mqtt_connect()
    ST["testing"] = False
    if ok:
        ST["mode"] = "dashboard"
        ST["page"] = 0
        ST["cfg_status"] = ""
        _set_toast("CONNECTED", COLOR_PRIMARY, 2400)
        _buzz(140)
    else:
        ST["mqtt_state"] = "OFFLINE"
        ST["connect_fails"] = 1
        ST["next_connect_ms"] = _ticks() + 5000
        ST["cfg_status"] = "SAVED - " + mqtt_error
        ST["cfg_status_col"] = COLOR_AMBER
        _set_toast("SAVED / BROKER OFFLINE", COLOR_AMBER, 2600)
        _buzz(120)
    ST["dirty"] = True


def _reset_session():
    cfg = ST["cfg"]
    if not cfg.get("validated") or not isinstance(cfg.get("mqtt"), dict):
        _set_toast("NO BROKER CONFIG", COLOR_AMBER, 1800)
        _buzz(70)
        return

    previous = ST["data"]["session"]
    ST["data"]["session"] = {
        "gpu": None, "seconds": 0, "wh": 0.0, "cost": 0.0, "tokens": 0,
        "power_w": previous.get("power_w"), "active": previous.get("active"),
    }
    ST["data"]["cloud_tokens"] = 0
    ST["pending_reset"] = True
    if _send_reset():
        ST["pending_reset"] = False
        _set_toast("SESSION RESET SENT", COLOR_PRIMARY, 2200)
        _buzz(140)
    else:
        _set_toast("RESET QUEUED", COLOR_AMBER, 2400)
        _buzz(100)
    ST["dirty"] = True


def _hit_back(x, y):
    return 92 <= x <= 134 and 44 <= y <= 90


def _press(name):
    ST["press"] = name
    ST["press_until"] = _ticks() + 140
    ST["dirty"] = True


def _cfg_tap_server(x, y):
    if 318 <= x <= 392 and 106 <= y <= 142:
        _press("clear")
        ST["draft_ip"] = ""
        ST["cfg_status"] = "ADDRESS CLEARED"
        ST["cfg_status_col"] = COLOR_MUTED
        _buzz(45)
        return

    if 150 <= x <= 316 and 390 <= y <= 446:
        _press("save")
        _buzz(65)
        _save_and_test()
        return

    for label, bx, by, bw, bh in _key_rects():
        if bx - 4 <= x <= bx + bw + 4 and by - 4 <= y <= by + bh + 4:
            _press("key:" + label)
            if label == "DEL":
                if len(ST["draft_ip"]) > 0:
                    ST["draft_ip"] = ST["draft_ip"][:len(ST["draft_ip"]) - 1]
            else:
                if len(ST["draft_ip"]) < 15:
                    if label == ".":
                        if len(ST["draft_ip"]) > 0 and ST["draft_ip"].count(".") < 3:
                            if ST["draft_ip"][-1] != ".":
                                ST["draft_ip"] += "."
                    else:
                        ST["draft_ip"] += label
            ST["cfg_status"] = ""
            _buzz(40)
            return


def _cfg_tap_wifi(x, y):
    if 113 <= x <= 353 and 266 <= y <= 324:
        _press("scan")
        _wifi_enter()
        return
    if 113 <= x <= 229 and 326 <= y <= 378:
        _press("reconnect")
        _wifi_reconnect()
        return
    if 237 <= x <= 353 and 326 <= y <= 378:
        if time.ticks_diff(ST["forget_armed_until"], _ticks()) > 0:
            ST["forget_armed_until"] = 0
            _wifi_forget()
        else:
            ST["forget_armed_until"] = _ticks() + 3000
            ST["dirty"] = True
            _buzz(60)
        return


def _cfg_tap_device(x, y):
    rows = _settings_rows()
    ui = _ui()
    for i in range(len(rows)):
        key = rows[i][0]
        rx, ry, rw, rh = _settings_row_rect(i)
        if rx <= x <= rx + rw and ry - 3 <= y <= ry + rh + 3:
            _press("set:" + key)
            if key == "rot":
                _rot_toggle()
                return
            if key == "bright":
                ui["bright"] = (ui["bright"] + 1) % len(BRIGHT_LEVELS)
                _apply_brightness()
            else:
                ui[key] = not ui[key]
            _save_config(ST["cfg"])
            ST["cfg_status"] = ""
            _buzz(45)
            return


def _config_tap(x, y):
    if ST["testing"]:
        return
    if _hit_back(x, y):
        _press("back")
        _leave_config()
        return
    page = ST["cfg_page"]
    # Edge chevrons work as page buttons too.
    if 190 <= y <= 280:
        if x < 60 and page > 0:
            _change_cfg_page(-1)
            return
        if x > 406 and page < CFG_PAGE_COUNT - 1:
            _change_cfg_page(1)
            return
    if page == 0:
        _cfg_tap_server(x, y)
    elif page == 1:
        _cfg_tap_wifi(x, y)
    else:
        _cfg_tap_device(x, y)


def _wifi_tap(x, y):
    if ST["mode"] != "wifi":
        return
    screen = ST["wifi_screen"]

    if screen == "connecting":
        if _hit_back(x, y):
            _wifi_back()
        return

    if screen == "password":
        if _hit_back(x, y):
            _wifi_back()
            return
        for label, action, payload, bx, by, bw, bh in _wifi_kb_rects():
            if bx - 2 <= x <= bx + bw + 2 and by - 2 <= y <= by + bh + 2:
                ST["press"] = "key:" + label
                ST["press_until"] = _ticks() + 130
                password = ST["wifi_target_pass"]
                if action == "char":
                    if len(password) < 63:
                        ST["wifi_target_pass"] = password + payload
                    ST["wifi_shift"] = False
                    ST["wifi_error"] = ""
                elif action == "shift":
                    ST["wifi_shift"] = not ST["wifi_shift"]
                elif action == "sym":
                    if ST["wifi_kb_mode"] == "abc":
                        ST["wifi_kb_mode"] = "sym1"
                    else:
                        ST["wifi_kb_mode"] = "abc"
                    ST["wifi_shift"] = False
                    ST["wifi_error"] = ""
                elif action == "more":
                    if ST["wifi_kb_mode"] == "sym2":
                        ST["wifi_kb_mode"] = "sym1"
                    else:
                        ST["wifi_kb_mode"] = "sym2"
                    ST["wifi_shift"] = False
                    ST["wifi_error"] = ""
                elif action == "space":
                    if len(password) < 63:
                        ST["wifi_target_pass"] = password + " "
                    ST["wifi_error"] = ""
                elif action == "del":
                    if len(password) > 0:
                        ST["wifi_target_pass"] = password[:len(password) - 1]
                    ST["wifi_error"] = ""
                elif action == "ok":
                    if ST["wifi_locked"] and len(password) == 0:
                        ST["wifi_error"] = "ENTER PASSWORD"
                        ST["dirty"] = True
                        _buzz(80)
                        return
                    _wifi_start_connect(
                        ST["wifi_target_ssid"], ST["wifi_target_pass"], ST["wifi_locked"]
                    )
                    return
                ST["dirty"] = True
                _buzz(35)
                return
        return

    if _hit_back(x, y):
        _wifi_back()
        return
    if 294 <= x <= 398 and 40 <= y <= 78:
        _wifi_scan()
        return

    pages = _wifi_pages()
    if pages > 1 and 110 <= y <= 380:
        if x < 65:
            _wifi_change_page(-1)
            return
        if x > 401:
            _wifi_change_page(1)
            return

    start = ST["wifi_page"] * 5
    for slot, rect in enumerate(_wifi_row_rects()):
        bx, by, bw, bh = rect
        if bx <= x <= bx + bw and by <= y <= by + bh:
            index = start + slot
            if index < len(ST["wifi_aps"]):
                ST["press"] = "ap:%d" % index
                ST["press_until"] = _ticks() + 130
                _wifi_select_ap(index)
            return


def _wifi_change_page(delta):
    pages = _wifi_pages()
    if pages <= 1:
        return
    ST["wifi_page"] = (ST["wifi_page"] + delta) % pages
    ST["dirty"] = True
    _buzz(45)


def _dashboard_tap(x, y):
    if y >= 414:
        for i in range(PAGE_COUNT):
            dot_x = CX + int((i - (PAGE_COUNT - 1) / 2.0) * 18)
            if dot_x - 8 <= x <= dot_x + 8:
                if i != ST["page"]:
                    ST["page"] = i
                    ST["dirty"] = True
                    _buzz(50)
                return
    if y < 100 or y > 390:
        return
    if x < 70:
        _change_page(-1)
    elif x > 396:
        _change_page(1)


def _handle_swipe(direction):
    # direction: +1 = swipe left (go forward), -1 = swipe right (go back)
    mode = ST["mode"]
    if mode == "dashboard":
        _change_page(direction)
    elif mode == "config":
        if not ST["testing"]:
            _change_cfg_page(direction)
    elif mode == "wifi" and ST["wifi_screen"] == "list":
        _wifi_change_page(direction)


def _handle_touch():
    try:
        count = M5.Touch.getCount()
    except Exception:
        count = 0

    if count > 0:
        try:
            x = M5.Touch.getX()
            y = M5.Touch.getY()
        except Exception:
            return
        if not ST["touch_down"]:
            ST["touch_down"] = True
            ST["touch_swallow"] = _note_input()
            ST["t_x0"] = x
            ST["t_y0"] = y
        else:
            ST["last_input"] = _ticks()
        ST["t_x"] = x
        ST["t_y"] = y
        return

    if not ST["touch_down"]:
        return
    ST["touch_down"] = False
    if ST["touch_swallow"]:
        ST["touch_swallow"] = False
        return

    dx = ST["t_x"] - ST["t_x0"]
    dy = ST["t_y"] - ST["t_y0"]
    if abs(dx) >= SWIPE_MIN_PX and abs(dx) > abs(dy) * 1.3:
        _handle_swipe(1 if dx < 0 else -1)
        _render(False)
        return

    x = ST["t_x0"]
    y = ST["t_y0"]
    if ST["mode"] == "config":
        _config_tap(x, y)
    elif ST["mode"] == "wifi":
        _wifi_tap(x, y)
    elif ST["mode"] == "dashboard":
        _dashboard_tap(x, y)
    _render(False)


def _btn_a_callback(state):
    ST["act_config"] = True


def _btn_b_click_callback(state):
    if time.ticks_diff(_ticks(), ST["ignore_click_until"]) < 0:
        return
    ST["act_next"] = True


def _btn_b_hold_callback(state):
    ST["ignore_click_until"] = _ticks() + 900
    ST["act_reset"] = True


def _init_buttons():
    try:
        BtnA.setCallback(type=BtnA.CB_TYPE.WAS_CLICKED, cb=_btn_a_callback)
        BtnB.setCallback(type=BtnB.CB_TYPE.WAS_CLICKED, cb=_btn_b_click_callback)
        BtnB.setCallback(type=BtnB.CB_TYPE.WAS_HOLD, cb=_btn_b_hold_callback)
    except Exception:
        pass


def _handle_actions():
    if not (ST["act_config"] or ST["act_next"] or ST["act_reset"]):
        return
    if _note_input():
        # Button press only wakes the screen.
        ST["act_config"] = False
        ST["act_next"] = False
        ST["act_reset"] = False
        return
    if ST["act_config"]:
        ST["act_config"] = False
        if ST["mode"] == "dashboard":
            _enter_config()
        elif ST["mode"] == "boot":
            # skip startup; Wi-Fi keeps joining in the background
            _boot_to_settings(0 if ST["wifi"] or ST["boot_stage"] != "wifi" else 1, "")
        elif ST["mode"] == "wifi":
            _wifi_back()
        elif not ST["testing"]:
            _leave_config()
    if ST["act_next"]:
        ST["act_next"] = False
        if ST["mode"] == "dashboard":
            _change_page(1)
        elif ST["mode"] == "config" and not ST["testing"]:
            _change_cfg_page(1, True)
    if ST["act_reset"]:
        ST["act_reset"] = False
        if ST["mode"] == "dashboard":
            _reset_session()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _mqtt_tick():
    now = _ticks()

    # Server IP saved but MQTT details never fetched (server was down when it was saved,
    # or at boot): keep asking Node-RED in the background.
    cfg = ST["cfg"]
    if (ST["mode"] == "dashboard" and ST["mqtt"] is None and ST["wifi"]
            and not cfg.get("validated") and _valid_ip(cfg.get("ip", ""))
            and time.ticks_diff(now, ST["next_fetch_ms"]) >= 0):
        ST["next_fetch_ms"] = now + SERVER_RETRY_MS
        details, error = _fetch_server_config(cfg["ip"])
        if details is not None:
            cfg["mqtt"] = details
            cfg["validated"] = True
            _save_config(cfg)
            ST["next_connect_ms"] = 0
            _set_toast("SERVER FOUND", COLOR_PRIMARY, 2000)
        else:
            ST["mqtt_err"] = error
        ST["dirty"] = True
        now = _ticks()

    if ST["mode"] == "dashboard" and ST["cfg"].get("validated"):
        if ST["mqtt"] is None and ST["wifi"] and now >= ST["next_connect_ms"]:
            ST["mqtt_state"] = "CONNECTING"
            ST["dirty"] = True
            _render(True)
            ok, error = _mqtt_connect()
            if not ok:
                ST["mqtt_state"] = "OFFLINE"
                ST["mqtt_err"] = error
                if ST["connect_fails"] < 5:
                    ST["connect_fails"] += 1
                delay = 5
                if ST["connect_fails"] >= 2:
                    delay = 10 * (ST["connect_fails"] - 1)
                if delay > 30:
                    delay = 30
                ST["next_connect_ms"] = _ticks() + delay * 1000
            ST["dirty"] = True

    client = ST["mqtt"]
    if client is None:
        if ST["raw"] is not None:
            _process_raw()
        return

    poller = ST["poll"]
    try:
        if poller is not None:
            if len(poller.poll(0)) > 0:
                client.check_msg()
        else:
            client.check_msg()
    except Exception:
        _mqtt_drop("LINK LOST")
        client = None

    if ST["raw"] is not None:
        _process_raw()

    if client is None:
        return

    now = _ticks()
    if _ago_ms(ST["last_ping_ms"]) >= 25000:
        try:
            client.ping()
            ST["last_ping_ms"] = now
        except Exception:
            _mqtt_drop("PING FAILED")

    age = -1
    if ST["last_msg_ms"] is not None:
        age = _ago_ms(ST["last_msg_ms"]) // 1000
    if age != ST["last_age"]:
        ST["last_age"] = age
        if age > 12:
            ST["dirty"] = True


def setup():
    global HAS_VIBRATION

    M5.begin()
    try:
        M5.Display.setRotation(0)
    except Exception:
        pass
    try:
        M5.Display.clear(COLOR_BG)
    except Exception:
        pass

    _init_fonts()
    _init_canvas()

    ST["data"] = _empty_data()
    ST["cfg"] = _load_config()
    ST["last_input"] = _ticks()
    ST["last_cycle"] = _ticks()
    _apply_brightness()
    _rot_restore()
    _init_wlan()
    _init_buttons()

    HAS_VIBRATION = hasattr(Power, "setVibration")
    if HAS_VIBRATION:
        try:
            Power.setVibration(0)
        except Exception:
            HAS_VIBRATION = False

    ST["page"] = 0
    _boot_start()            # saved Wi-Fi -> server -> MQTT, or settings if something is missing
    _render(True)


def loop():
    M5.update()

    _handle_actions()
    _handle_touch()
    _boot_tick()
    _wifi_tick()
    _wifi_keepalive()
    _rot_tick()
    _mqtt_tick()
    _idle_tick()
    _cycle_tick()

    now = _ticks()
    if ST["press"] and ST["press_until"] and time.ticks_diff(now, ST["press_until"]) >= 0:
        ST["press"] = ""
        ST["press_until"] = 0
        ST["dirty"] = True

    if ST["forget_armed_until"] and time.ticks_diff(now, ST["forget_armed_until"]) >= 0:
        ST["forget_armed_until"] = 0
        ST["dirty"] = True

    if ST["toast"] and ST["toast_until"] and time.ticks_diff(now, ST["toast_until"]) >= 0:
        ST["toast"] = ""
        ST["toast_until"] = 0
        ST["dirty"] = True

    if ST["vib_until"] and time.ticks_diff(now, ST["vib_until"]) >= 0:
        try:
            Power.setVibration(0)
        except Exception:
            pass
        ST["vib_until"] = 0

    _render(False)
    time.sleep_ms(30)


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            Power.setVibration(0)
        except Exception:
            pass
        try:
            from utility import print_error_msg

            print_error_msg(e)
        except Exception:
            print("stopwatch dashboard error:", e)
