# ============================================================
#  ESP32 MicroPython BLE RaceBox Client
#  Target board : M5StickC Plus2  (ESP32-PICO-V3-02)
#  Display      : ST7789V2  135 × 240  (1.14" TFT, landscape → 240 × 135)
#
#  Converted from Arduino/NimBLE C++ code by Vincent Kratzer (GPL-3.0)
#
#  Required drivers (copy to device flash before running):
#    • st7789.py  – devbis/st7789_mpy  or  russhughes/st7789py_mpy
#    • vga1_8x16.py (or any font file from russhughes' collection)
#      Font download: https://github.com/russhughes/st7789py_mpy/tree/main/fonts
#
#  M5StickC Plus2 hardware notes
#  ─────────────────────────────
#  Display SPI  : SPI bus 1
#    SCK  → GPIO 13
#    MOSI → GPIO 15
#    CS   → GPIO  5
#    DC   → GPIO 14   ← Plus2 specific (Plus used GPIO 23)
#    RST  → GPIO 12   ← Plus2 specific (Plus used GPIO 18)
#  Backlight    : GPIO 27  PWM (HIGH = on, no PMIC on Plus2)
#  Buttons      : A = GPIO 37, B = GPIO 39, C = GPIO 35  (power btn)
#  I2C (IMU/RTC): SDA = GPIO 21, SCL = GPIO 22
#  HOLD pin     : GPIO 4  ← must be kept HIGH to stay powered on battery
#
#  Power-on  : hold Button C (GPIO 35) > 2 s
#  Power-off : hold Button C > 6 s  OR set GPIO 4 LOW in code
# ============================================================

import bluetooth
import struct
import time
import sys
from micropython import const
from machine import Pin, SPI, PWM

# =================== USER SETTINGS ===================
# Uncomment and fill in to lock to one specific RaceBox:
# TARGET_DEVICE_ADDRESS = "aa:bb:cc:dd:ee:ff"
TARGET_DEVICE_ADDRESS = None   # None → connect to any "RaceBox" device

# Display rotation: 0=portrait(135×240), 1=landscape(240×135) ← recommended
DISPLAY_ROTATION = 1
# ======================================================

# ── CRITICAL: keep HOLD pin HIGH so device stays on when on battery ──
_hold = Pin(4, Pin.OUT)
_hold.value(1)

# ═══════════════════════════════════════════════════════
#  M5StickC Plus2 pin definitions
# ═══════════════════════════════════════════════════════
TFT_SCK  = 13
TFT_MOSI = 15
TFT_CS   = 5
TFT_DC   = 14
TFT_RST  = 12
TFT_BL   = 27   # backlight – PWM on Plus2 (no PMIC)

BTN_A = 37
BTN_B = 39

SCREEN_W = 240   # logical width  in landscape
SCREEN_H = 135   # logical height in landscape

# ─── RGB565 colour constants ───
BLACK   = const(0x0000)
BLUE    = const(0x001F)
RED     = const(0xF800)
GREEN   = const(0x07E0)
CYAN    = const(0x07FF)
MAGENTA = const(0xF81F)
YELLOW  = const(0xFFE0)
WHITE   = const(0xFFFF)
DKGRAY  = const(0x4208)

# ═══════════════════════════════════════════════════════
#  Display initialisation  (st7789py driver)
# ═══════════════════════════════════════════════════════
try:
    import st7789py as st7789
    _USE_C_DRIVER = False
except ImportError:
    try:
        import st7789
        _USE_C_DRIVER = True
    except ImportError:
        st7789 = None
        print("WARNING: No ST7789 driver found. Display will be disabled.")
        print("         Copy st7789py.py (russhughes) or st7789.mpy (devbis) to the device.")

try:
    import vga1_8x16 as font
except ImportError:
    font = None
    print("WARNING: No font file found. Text rendering will be disabled.")
    print("         Copy a font file from russhughes/st7789py_mpy/fonts/ to the device.")

def _init_display():
    """Initialise the ST7789V2 on the M5StickC Plus2 and turn backlight on."""
    # Backlight – drive GPIO 27 via PWM (duty 0-1023 on MicroPython)
    bl = PWM(Pin(TFT_BL), freq=1000, duty=800)   # ~78 % brightness

    if st7789 is None:
        return None, bl

    spi = SPI(1, baudrate=20_000_000, polarity=1, phase=0,
              sck=Pin(TFT_SCK), mosi=Pin(TFT_MOSI))

    if _USE_C_DRIVER:
        # devbis/st7789_mpy C driver
        tft = st7789.ST7789(
            spi, 135, 240,
            reset=Pin(TFT_RST, Pin.OUT),
            dc=Pin(TFT_DC, Pin.OUT),
            cs=Pin(TFT_CS, Pin.OUT),
            rotation=DISPLAY_ROTATION,
        )
    else:
        # russhughes/st7789py_mpy pure-Python driver
        tft = st7789.ST7789(
            spi, 135, 240,
            reset=Pin(TFT_RST, Pin.OUT),
            dc=Pin(TFT_DC, Pin.OUT),
            cs=Pin(TFT_CS, Pin.OUT),
            rotation=DISPLAY_ROTATION,
        )

    tft.init()
    tft.fill(BLACK)
    return tft, bl

tft, _bl = _init_display()


# ═══════════════════════════════════════════════════════
#  Minimal display helper (works with either driver)
# ═══════════════════════════════════════════════════════
_LINE_H = 17    # pixel height per text row (8×16 font + 1 px gap)

def tft_text(x, y, text, color=WHITE, bg=BLACK):
    """Draw a string at pixel (x, y) using the 8×16 font."""
    if tft is None or font is None:
        return
    tft.text(font, text, x, y, color, bg)

def tft_fill(color=BLACK):
    if tft:
        tft.fill(color)

def tft_hline(x, y, w, color=WHITE):
    if tft:
        tft.hline(x, y, w, color)

def tft_clear_row(y, bg=BLACK):
    """Blank a single text row."""
    if tft:
        tft.fill_rect(0, y, SCREEN_W, _LINE_H, bg)


# ═══════════════════════════════════════════════════════
#  BLE UUIDs
# ═══════════════════════════════════════════════════════
UART_SERVICE_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
TX_CHAR_UUID      = bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

# ═══════════════════════════════════════════════════════
#  Rate-limiting
# ═══════════════════════════════════════════════════════
OUTPUT_FREQ_HZ    = 8
OUTPUT_INTERVAL   = 1000 // OUTPUT_FREQ_HZ   # ms

# ═══════════════════════════════════════════════════════
#  Global state
# ═══════════════════════════════════════════════════════
connected    = False
updated_data = False
device_type  = -1        # 0 = Mini/Mini S, 1 = Micro, -1 = unknown
last_output_ms = 0

# ── Parsed RaceBox data fields ──
iTOW = year = month = day = hour = minute = second = 0
validity_flags = time_accuracy = nanoseconds = 0
fix_status = fix_status_flags = datetime_flags = num_svs = 0
longitude = latitude = wgs_altitude = msl_altitude = 0
horizontal_accuracy = vertical_accuracy = 0
speed_val = heading = speed_accuracy = heading_accuracy = 0
pdop = lat_lon_flags = battery_status = 0
g_force_x = g_force_y = g_force_z = 0
rot_rate_x = rot_rate_y = rot_rate_z = 0
heading_degrees  = 0.0
compass_direction = ""


# ═══════════════════════════════════════════════════════
#  Utility helpers
# ═══════════════════════════════════════════════════════

def get_compass(deg):
    if deg >= 337.5 or deg < 22.5:  return "N"
    if deg < 67.5:                   return "NE"
    if deg < 112.5:                  return "E"
    if deg < 157.5:                  return "SE"
    if deg < 202.5:                  return "S"
    if deg < 247.5:                  return "SW"
    if deg < 292.5:                  return "W"
    return "NW"


def decode_battery(batt):
    if device_type == 0:
        charging = bool(batt & 0x80)
        level    = batt & 0x7F
        print(f"Battery (Mini/Mini S): {'Charging' if charging else 'Not charging'}  {level}%")
    elif device_type == 1:
        print(f"Battery (Micro): input {batt / 10.0:.1f} V")
    else:
        print(f"Battery: raw=0x{batt:02X}  (device type unknown)")


def checksum(data, pkt_len):
    """Fletcher-8 checksum (u-blox / RaceBox protocol)."""
    a = b = 0
    for i in range(2, pkt_len - 2):
        a = (a + data[i]) & 0xFF
        b = (b + a)       & 0xFF
    return a, b


# ═══════════════════════════════════════════════════════
#  Payload parsing
# ═══════════════════════════════════════════════════════

def parse_payload(data: bytes):
    if len(data) < 8 or data[0] != 0xB5 or data[1] != 0x62:
        print("Bad frame header – not a RaceBox packet.")
        return

    msg_class      = data[2]
    msg_id         = data[3]
    payload_length = struct.unpack_from('<H', data, 4)[0]
    pkt_len        = 6 + payload_length + 2

    if pkt_len > 512 or len(data) < pkt_len:
        print(f"Packet length error ({pkt_len} vs {len(data)} bytes).")
        return

    ck_a, ck_b = checksum(data, pkt_len)
    if data[pkt_len - 2] != ck_a or data[pkt_len - 1] != ck_b:
        print("Checksum failed – dropping packet.")
        return

    if   msg_class == 0xFF and msg_id == 0x01:
        parse_racebox_data_message(data)
    elif msg_class == 0xFF and msg_id == 0x21:
        print("History Data Message (not implemented).")
    elif msg_class == 0xFF and msg_id == 0x22:
        print("Standalone Recording Status (not implemented).")
    elif msg_class == 0xFF and msg_id == 0x23:
        print("Recorded Data Download (not implemented).")
    elif msg_class == 0xFF and msg_id == 0x26:
        print("Recording State Change (not implemented).")
    else:
        print(f"Unknown msg class=0x{msg_class:02X} id=0x{msg_id:02X}.")


def parse_racebox_data_message(data: bytes):
    global iTOW, year, month, day, hour, minute, second
    global validity_flags, time_accuracy, nanoseconds
    global fix_status, fix_status_flags, datetime_flags, num_svs
    global longitude, latitude, wgs_altitude, msl_altitude
    global horizontal_accuracy, vertical_accuracy
    global speed_val, heading, speed_accuracy, heading_accuracy
    global pdop, lat_lon_flags, battery_status
    global g_force_x, g_force_y, g_force_z
    global rot_rate_x, rot_rate_y, rot_rate_z
    global heading_degrees, compass_direction, updated_data

    u  = struct.unpack_from   # shorthand
    iTOW              = u('<I', data,  6)[0]
    year              = u('<H', data, 10)[0]
    month             = data[12]
    day               = data[13]
    hour              = data[14]
    minute            = data[15]
    second            = data[16]
    validity_flags    = data[17]
    time_accuracy     = u('<I', data, 18)[0]
    nanoseconds       = u('<I', data, 22)[0]
    fix_status        = data[26]
    fix_status_flags  = data[27]
    datetime_flags    = data[28]
    num_svs           = data[29]
    longitude         = u('<i', data, 30)[0]   # signed
    latitude          = u('<i', data, 34)[0]
    wgs_altitude      = u('<i', data, 38)[0]
    msl_altitude      = u('<i', data, 42)[0]
    horizontal_accuracy = u('<I', data, 46)[0]
    vertical_accuracy   = u('<I', data, 50)[0]
    speed_val         = u('<I', data, 54)[0]
    heading           = u('<I', data, 58)[0]
    speed_accuracy    = u('<I', data, 62)[0]
    heading_accuracy  = u('<I', data, 66)[0]
    pdop              = u('<H', data, 70)[0]
    lat_lon_flags     = data[72]
    battery_status    = data[73]
    g_force_x         = u('<h', data, 74)[0]   # signed int16
    g_force_y         = u('<h', data, 76)[0]
    g_force_z         = u('<h', data, 78)[0]
    rot_rate_x = rot_rate_y = rot_rate_z = 0   # not in 80-byte payload

    heading_degrees   = heading / 100_000.0
    compass_direction = get_compass(heading_degrees)
    updated_data      = True


# ═══════════════════════════════════════════════════════
#  Serial output  (rate-limited)
# ═══════════════════════════════════════════════════════

def print_to_serial():
    global last_output_ms
    now = time.ticks_ms()
    if time.ticks_diff(now, last_output_ms) < OUTPUT_INTERVAL:
        return
    last_output_ms = now

    fix_txt = {0: "No Fix", 2: "2D Fix", 3: "3D Fix"}.get(fix_status, "Unknown")
    hdg_ok  = bool(fix_status_flags & 0x20)

    print()
    print("─" * 60)
    print(f"  RaceBox Data  {year:04d}-{month:02d}-{day:02d}  {hour:02d}:{minute:02d}:{second:02d} UTC")
    print("─" * 60)
    print(f"  Fix       : {fix_txt}   SVs: {num_svs}")
    print(f"  Lat       : {latitude  / 1e7:.7f}°")
    print(f"  Lon       : {longitude / 1e7:.7f}°")
    print(f"  WGS Alt   : {wgs_altitude  / 1000.0:.2f} m")
    print(f"  MSL Alt   : {msl_altitude  / 1000.0:.2f} m")
    print(f"  H-Acc     : {horizontal_accuracy / 1000.0:.2f} m")
    print(f"  V-Acc     : {vertical_accuracy   / 1000.0:.2f} m")
    print(f"  Speed     : {speed_val / 1000.0:.2f} m/s  "
          f"({speed_val * 3.6 / 1000.0:.1f} km/h)")
    print(f"  Heading   : {heading_degrees:.1f}°  {compass_direction}"
          f"  [{'valid' if hdg_ok else 'needs movement'}]")
    print(f"  PDOP      : {pdop / 100.0:.2f}")
    print(f"  GForce    : X={g_force_x/1000.0:.3f}  "
          f"Y={g_force_y/1000.0:.3f}  "
          f"Z={g_force_z/1000.0:.3f}  G")
    decode_battery(battery_status)
    print("─" * 60)


# ═══════════════════════════════════════════════════════
#  Display output  (135 × 240 landscape → 240 w × 135 h)
# ═══════════════════════════════════════════════════════
#
#  Layout (each row = 17 px, font 8×16):
#   y=  0  ── header bar ──────────────────────────────────
#   y= 17  GPS: 3D Fix  SVs: 11
#   y= 34  Lat:  48.1234567°
#   y= 51  Lon:   11.5678901°
#   y= 68  Spd: 123.4 km/h
#   y= 85  Hdg: 270.0° NW  [valid]
#   y=102  Gx:-0.01 Gy: 0.11 Gz: 0.97
#   y=119  Batt / time
#

def draw_header(status_color=GREEN, status_text="connected"):
    tft_fill(BLACK)
    tft_hline(0, 0, SCREEN_W, WHITE)
    tft_text(2,  2, "RaceBox", RED)
    tft_text(70, 2, status_text, status_color)
    tft_hline(0, _LINE_H, SCREEN_W, WHITE)


def print_to_display():
    if tft is None:
        return

    fix_txt   = {0: "No Fix ", 2: "2D Fix ", 3: "3D Fix "}.get(fix_status, "???Fix ")
    fix_color = {0: RED,       2: CYAN,       3: GREEN   }.get(fix_status, MAGENTA)
    hdg_ok    = bool(fix_status_flags & 0x20)

    # Redraw entire screen each update (135px height is small – no flicker trick needed)
    tft_fill(BLACK)

    # ── Header ──
    tft_hline(0, 0, SCREEN_W, WHITE)
    tft_text(2,  2, "RaceBox", RED)
    tft_text(70, 2, "connected", GREEN)
    tft_hline(0, _LINE_H - 1, SCREEN_W, WHITE)

    y = _LINE_H + 1

    # ── Fix + SVs ──
    tft_text(0, y, "GPS:", WHITE)
    tft_text(36, y, fix_txt, fix_color)
    tft_text(100, y, f"SVs:{num_svs:2d}", YELLOW)
    y += _LINE_H

    # ── Lat / Lon ──
    tft_text(0, y, f"Lat:{latitude  / 1e7:10.5f}", YELLOW)
    y += _LINE_H
    tft_text(0, y, f"Lon:{longitude / 1e7:10.5f}", GREEN)
    y += _LINE_H

    # ── Speed ──
    kmh = speed_val * 3.6 / 1000.0
    tft_text(0, y, f"Spd:{kmh:6.1f} km/h", WHITE)
    y += _LINE_H

    # ── Heading ──
    if hdg_ok:
        tft_text(0, y,
                 f"Hdg:{heading_degrees:5.1f} {compass_direction:<2}",
                 CYAN)
    else:
        tft_text(0, y, "Hdg: -- (no fix)  ", RED)
    y += _LINE_H

    # ── G-Forces ──
    tft_text(0, y,
             f"G {g_force_x/1000.0:+.2f} {g_force_y/1000.0:+.2f} {g_force_z/1000.0:+.2f}",
             BLUE)
    y += _LINE_H

    # ── Battery / time ──
    if device_type == 0:
        batt_str = f"Batt:{battery_status & 0x7F:3d}%"
    elif device_type == 1:
        batt_str = f"Vin:{battery_status / 10.0:.1f}V"
    else:
        batt_str = f"Batt:0x{battery_status:02X}"

    tft_text(0, y, f"{batt_str}  {hour:02d}:{minute:02d}:{second:02d}", DKGRAY)


def show_scanning():
    """Show scanning splash screen."""
    if tft is None:
        return
    tft_fill(BLACK)
    tft_hline(0, 0, SCREEN_W, WHITE)
    tft_text(2,  2, "RaceBox", RED)
    tft_text(70, 2, "BLE CLIENT", CYAN)
    tft_hline(0, _LINE_H - 1, SCREEN_W, WHITE)
    tft_text(0, _LINE_H + 2, "Scanning for RaceBox...", WHITE)
    if TARGET_DEVICE_ADDRESS:
        tft_text(0, _LINE_H * 2 + 4, TARGET_DEVICE_ADDRESS[:20], MAGENTA)


def show_disconnected():
    if tft is None:
        return
    tft_fill(BLACK)
    tft_hline(0, 0, SCREEN_W, WHITE)
    tft_text(2,  2, "RaceBox", RED)
    tft_text(70, 2, "DISCONNECTED", RED)
    tft_hline(0, _LINE_H - 1, SCREEN_W, WHITE)
    tft_text(0, _LINE_H + 2, "Reconnecting...", YELLOW)


# ═══════════════════════════════════════════════════════
#  BLE Central implementation
# ═══════════════════════════════════════════════════════

class BLERaceBoxClient:
    _IRQ_SCAN_RESULT               = const(5)
    _IRQ_SCAN_DONE                 = const(6)
    _IRQ_PERIPHERAL_CONNECT        = const(7)
    _IRQ_PERIPHERAL_DISCONNECT     = const(8)
    _IRQ_GATTC_SERVICE_RESULT      = const(9)
    _IRQ_GATTC_SERVICE_DONE        = const(10)
    _IRQ_GATTC_CHARACTERISTIC_RESULT = const(11)
    _IRQ_GATTC_CHARACTERISTIC_DONE   = const(12)
    _IRQ_GATTC_NOTIFY              = const(18)

    _NOTIFY_ENABLE = b'\x01\x00'

    def __init__(self):
        self._ble           = bluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)
        self._conn_handle   = None
        self._tx_val_handle = None
        self._svc_start     = None
        self._svc_end       = None
        self._scanning      = False

    # ── IRQ handler ─────────────────────────────────────────
    def _irq(self, event, data):
        global connected, device_type

        if event == self._IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv_data = data
            name = self._decode_name(adv_data)
            if name:
                print(f"Found: '{name}'  {bytes(addr).hex(':')}")

            if name and name.startswith("RaceBox"):
                addr_str = bytes(addr).hex(':')

                # Device type detection
                if   name.startswith("RaceBox Micro"): device_type = 1
                elif name.startswith("RaceBox Mini"):  device_type = 0
                else:                                   device_type = -1

                # Optional address filter
                if TARGET_DEVICE_ADDRESS and addr_str.lower() != TARGET_DEVICE_ADDRESS.lower():
                    print(f"  Skipping – address doesn't match TARGET_DEVICE_ADDRESS")
                    return

                print(f"RaceBox '{name}' found at {addr_str}. Connecting...")
                self._ble.gap_scan(None)
                self._scanning = False
                self._ble.gap_connect(addr_type, addr)

        elif event == self._IRQ_SCAN_DONE:
            if self._scanning:
                print("Scan done, no RaceBox found. Retrying...")
                self.start_scan()

        elif event == self._IRQ_PERIPHERAL_CONNECT:
            conn_handle, addr_type, addr = data
            self._conn_handle = conn_handle
            connected = True
            print(f"Connected! handle={conn_handle}")
            self._ble.gattc_discover_services(conn_handle)

        elif event == self._IRQ_PERIPHERAL_DISCONNECT:
            self._conn_handle   = None
            self._tx_val_handle = None
            connected = False
            print("Disconnected. Restarting scan...")
            show_disconnected()
            self.start_scan()

        elif event == self._IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start_h, end_h, uuid = data
            if bluetooth.UUID(bytes(uuid)) == UART_SERVICE_UUID:
                print("UART service found.")
                self._svc_start = start_h
                self._svc_end   = end_h

        elif event == self._IRQ_GATTC_SERVICE_DONE:
            conn_handle, status = data
            if self._svc_start is not None:
                self._ble.gattc_discover_characteristics(
                    conn_handle, self._svc_start, self._svc_end)
            else:
                print("UART service not found – disconnecting.")
                self._ble.gap_disconnect(conn_handle)

        elif event == self._IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, def_h, val_h, props, uuid = data
            if bluetooth.UUID(bytes(uuid)) == TX_CHAR_UUID:
                print(f"TX characteristic found  val_handle={val_h}")
                self._tx_val_handle = val_h

        elif event == self._IRQ_GATTC_CHARACTERISTIC_DONE:
            conn_handle, status = data
            if self._tx_val_handle is not None:
                cccd = self._tx_val_handle + 1
                self._ble.gattc_write(conn_handle, cccd, self._NOTIFY_ENABLE, 1)
                print("Subscribed to TX notifications.")
            else:
                print("TX characteristic not found.")

        elif event == self._IRQ_GATTC_NOTIFY:
            conn_handle, val_handle, notify_data = data
            if val_handle == self._tx_val_handle:
                payload = bytes(notify_data)
                if len(payload) < 80:
                    print(f"Short packet ({len(payload)} B), expected 80 for live data.")
                parse_payload(payload)

    # ── Helpers ─────────────────────────────────────────────
    @staticmethod
    def _decode_name(adv_data: bytes) -> str:
        i = 0
        while i < len(adv_data):
            length = adv_data[i]
            if length == 0:
                break
            ad_type = adv_data[i + 1]
            if ad_type in (0x08, 0x09):
                try:
                    return adv_data[i + 2: i + 1 + length].decode('utf-8')
                except Exception:
                    pass
            i += 1 + length
        return ""

    def start_scan(self):
        print("BLE scan started...")
        self._scanning = True
        # duration=0 → indefinite; interval=45 ms, window=15 ms, active=True
        self._ble.gap_scan(0, 45_000, 15_000, True)


# ═══════════════════════════════════════════════════════
#  Button handling  (Button A = GPIO37, Button B = GPIO39)
# ═══════════════════════════════════════════════════════
btn_a = Pin(BTN_A, Pin.IN)
btn_b = Pin(BTN_B, Pin.IN)

def handle_buttons():
    """Poll buttons and run stub functions (active LOW)."""
    if btn_a.value() == 0:
        time.sleep_ms(50)           # debounce
        if btn_a.value() == 0:
            function1()
    if btn_b.value() == 0:
        time.sleep_ms(50)
        if btn_b.value() == 0:
            function2()


# ═══════════════════════════════════════════════════════
#  Stub functions  (called by buttons or serial input)
# ═══════════════════════════════════════════════════════

def function1():
    """Stub – e.g. start standalone recording."""
    print("### function1 (Button A / '1') – not yet implemented ###")
    time.sleep_ms(500)

def function2():
    """Stub – e.g. stop standalone recording."""
    print("### function2 (Button B / '2') – not yet implemented ###")
    time.sleep_ms(500)

def function3():
    """Stub – e.g. request recorded data."""
    print("### function3 ('3') – not yet implemented ###")
    time.sleep_ms(500)


# ═══════════════════════════════════════════════════════
#  Serial console input  (non-blocking, REPL friendly)
# ═══════════════════════════════════════════════════════

def interpret_serial_input():
    try:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return
        ch = sys.stdin.read(1)
    except Exception:
        return

    if   ch == '1': function1()
    elif ch == '2': function2()
    elif ch == '3': function3()
    else: print(f"Unknown input '{ch}'. Use 1, 2, or 3.")


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main():
    global updated_data

    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║  M5StickC Plus2  –  RaceBox BLE Client      ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    if TARGET_DEVICE_ADDRESS:
        print(f"  Connecting only to: {TARGET_DEVICE_ADDRESS}")
    else:
        print("  Connecting to any RaceBox (TARGET_DEVICE_ADDRESS not set).")
    print()
    print("  Buttons:  A (GPIO37) = function1,  B (GPIO39) = function2")
    print("  Serial:   type 1 / 2 / 3 + Enter in the REPL")
    print()

    show_scanning()

    client = BLERaceBoxClient()
    client.start_scan()

    while True:
        interpret_serial_input()
        handle_buttons()

        if updated_data:
            print_to_serial()
            print_to_display()
            updated_data = False

        time.sleep_ms(20)


if __name__ == "__main__":
    main()
