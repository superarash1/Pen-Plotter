"""
Pen Plotter G-code Interpreter — Raspberry Pi Pico 2W
Receives G-code lines over USB serial from Pi 4, executes motion.

Pin mapping:
  Y-axis (Motor 1):  DIR1=GP18, STEP1=GP19, EN1=GP20
  X-axis (Motor 2):  DIR2=GP16, STEP2=GP17, EN2=GP21
  Limit switch Y:    GP15
  Limit switch X:    GP14
  Pen servo:         GP10 PWM

DIR=1 → negative direction
DIR=0 → positive direction
"""

import sys
import select
import time
from machine import Pin, PWM

# ---------------------------------------------------------------------------
# Pin Definitions
# ---------------------------------------------------------------------------

# Y-axis — Motor 1 (your existing pins)
DIR_Y  = Pin(18, Pin.OUT)
STEP_Y = Pin(19, Pin.OUT)
EN_Y   = Pin(20, Pin.OUT)

# X-axis — Motor 2 (your existing pins)
DIR_X  = Pin(16, Pin.OUT)
STEP_X = Pin(17, Pin.OUT)
EN_X   = Pin(21, Pin.OUT)

# Limit switches (normally HIGH via PULL_UP, LOW when triggered)
limit_switch_y = Pin(14, Pin.IN, Pin.PULL_UP)
limit_switch_x = Pin(15, Pin.IN, Pin.PULL_UP)

# Pen lift servo on GP10 via PWM
# SG90: 1ms pulse = up, 2ms pulse = down, 50Hz period
servo_pwm = PWM(Pin(10))
servo_pwm.freq(50)

# ---------------------------------------------------------------------------
# Machine Configuration — tune these for your machine
# ---------------------------------------------------------------------------

STEPS_PER_MM_X = 50      # calibrated: commanded 50mm, actual 96.82mm → 100 * (50/96.82)
STEPS_PER_MM_Y = 50      # calibrated: commanded 50mm, actual 94.78mm → 100 * (50/94.78)

MAX_FEEDRATE     = 5000.0    # mm/min hard cap
DEFAULT_FEEDRATE = 3000.0    # mm/min used if F not specified in G1
STEP_DELAY_US    = 500      # decrease to go faster (match your existing value)
HOME_STEP_DELAY  = 1200      # slower pulse delay during homing

# Servo duty cycles (65535 = 100% at 50Hz, period = 20ms)
# 1ms / 20ms = 5% duty → pen up
# 2ms / 20ms = 10% duty → pen down
PEN_DOWN_DUTY   = int(65535 * 0.05)
PEN_UP_DUTY = int(65535 * 0.10)
PEN_SETTLE_MS = 150           # ms to wait after pen moves

# ---------------------------------------------------------------------------
# Machine State
# ---------------------------------------------------------------------------

pos_x = 0.0
pos_y = 0.0
feedrate = DEFAULT_FEEDRATE
absolute_mode = True    # G90=True, G91=False
pen_is_down = False
motors_enabled = False

# ---------------------------------------------------------------------------
# Motor Enable / Disable
# ---------------------------------------------------------------------------

def enable_motors():
    global motors_enabled
    EN_Y.value(0)    # TMC2209 EN is active LOW
    EN_X.value(0)
    motors_enabled = True

def disable_motors():
    global motors_enabled
    EN_Y.value(1)
    EN_X.value(1)
    motors_enabled = False

# ---------------------------------------------------------------------------
# Pen Control
# ---------------------------------------------------------------------------

def pen_up():
    global pen_is_down
    servo_pwm.duty_u16(PEN_UP_DUTY)
    pen_is_down = False
    time.sleep_ms(PEN_SETTLE_MS)

def pen_down():
    global pen_is_down
    servo_pwm.duty_u16(PEN_DOWN_DUTY)
    pen_is_down = True
    time.sleep_ms(PEN_SETTLE_MS)

# ---------------------------------------------------------------------------
# Single-axis step helpers
# ---------------------------------------------------------------------------

def step_y(steps, direction):
    """Step Y axis motor. direction: 1=negative, 0=positive."""
    DIR_Y.value(direction)
    time.sleep_ms(2)
    for _ in range(steps):
        STEP_Y.value(1)
        time.sleep_us(STEP_DELAY_US)
        STEP_Y.value(0)
        time.sleep_us(STEP_DELAY_US)

def step_x(steps, direction):
    """Step X axis motor. direction: 1=negative, 0=positive."""
    DIR_X.value(direction)
    time.sleep_ms(2)
    for _ in range(steps):
        STEP_X.value(1)
        time.sleep_us(STEP_DELAY_US)
        STEP_X.value(0)
        time.sleep_us(STEP_DELAY_US)

# ---------------------------------------------------------------------------
# Linear Move — Bresenham line algorithm for smooth diagonals
# ---------------------------------------------------------------------------

def linear_move(target_x, target_y):
    """
    Move from current position to (target_x, target_y) in mm.
    Uses Bresenham's algorithm to synchronize X and Y steps so
    diagonal moves are smooth lines, not staircase artifacts.
    """
    global pos_x, pos_y

    delta_x = target_x - pos_x
    delta_y = target_y - pos_y

    steps_x = int(abs(delta_x) * STEPS_PER_MM_X)
    steps_y = int(abs(delta_y) * STEPS_PER_MM_Y)

    # DIR=1 is negative, DIR=0 is positive
    dir_x = 1 if delta_x < 0 else 0
    dir_y = 1 if delta_y < 0 else 0

    DIR_X.value(dir_x)
    DIR_Y.value(dir_y)
    time.sleep_ms(2)    # let DIR pin settle before stepping

    if max(steps_x, steps_y) == 0:
        return

    ex = 0
    ey = 0

    if steps_x >= steps_y:
        # X is the dominant axis — step X every tick, Y when error accumulates
        for _ in range(steps_x):
            STEP_X.value(1)
            ey += steps_y
            if ey >= steps_x:
                STEP_Y.value(1)
                ey -= steps_x
            time.sleep_us(STEP_DELAY_US)
            STEP_X.value(0)
            STEP_Y.value(0)
            time.sleep_us(STEP_DELAY_US)
    else:
        # Y is the dominant axis — step Y every tick, X when error accumulates
        for _ in range(steps_y):
            STEP_Y.value(1)
            ex += steps_x
            if ex >= steps_y:
                STEP_X.value(1)
                ex -= steps_y
            time.sleep_us(STEP_DELAY_US)
            STEP_Y.value(0)
            STEP_X.value(0)
            time.sleep_us(STEP_DELAY_US)

    pos_x = target_x
    pos_y = target_y

# ---------------------------------------------------------------------------
# Homing
# ---------------------------------------------------------------------------

# Maximum steps allowed during homing before giving up (safety timeout)
# 300mm * 100 steps/mm = 30000 steps — increase if your travel is larger
HOME_MAX_STEPS = 30000

def home_axis_y():
    """Drive Y negative until limit switch triggers, then back off 2mm."""
    global pos_y

    # Debug: print switch state before moving so you can verify wiring
    print("  [home_y] switch state before move: {}".format(limit_switch_y.value()))
    print("  [home_y] expected: 1=not triggered, 0=triggered")

    # Safety check — if switch is already triggered, back off first
    if limit_switch_y.value() == 0:
        print("  [home_y] switch already triggered — backing off first")
        step_y(int(5 * STEPS_PER_MM_Y), 0)   # back off 5mm positive

    DIR_Y.value(1)    # negative direction toward switch
    time.sleep_ms(2)

    steps_taken = 0
    while limit_switch_y.value() == 1:    # 1 = not triggered, keep moving
        STEP_Y.value(1)
        time.sleep_us(HOME_STEP_DELAY)
        STEP_Y.value(0)
        time.sleep_us(HOME_STEP_DELAY)
        steps_taken += 1
        if steps_taken >= HOME_MAX_STEPS:
            print("  [home_y] ERROR: limit switch never triggered — check wiring!")
            print("  [home_y] Switch reads: {}".format(limit_switch_y.value()))
            return   # abort homing, do not update pos

    print("  [home_y] switch triggered after {} steps".format(steps_taken))
    pos_y = 0.0
    step_y(int(2 * STEPS_PER_MM_Y), 0)   # back off 2mm away from switch
    print("  [home_y] done")

def home_axis_x():
    """Drive X negative until limit switch triggers, then back off 2mm."""
    global pos_x

    print("  [home_x] switch state before move: {}".format(limit_switch_x.value()))
    print("  [home_x] expected: 1=not triggered, 0=triggered")

    if limit_switch_x.value() == 0:
        print("  [home_x] switch already triggered — backing off first")
        step_x(int(5 * STEPS_PER_MM_X), 0)

    DIR_X.value(1)    # negative direction toward switch
    time.sleep_ms(2)

    steps_taken = 0
    while limit_switch_x.value() == 1:
        STEP_X.value(1)
        time.sleep_us(HOME_STEP_DELAY)
        STEP_X.value(0)
        time.sleep_us(HOME_STEP_DELAY)
        steps_taken += 1
        if steps_taken >= HOME_MAX_STEPS:
            print("  [home_x] ERROR: limit switch never triggered — check wiring!")
            print("  [home_x] Switch reads: {}".format(limit_switch_x.value()))
            return

    print("  [home_x] switch triggered after {} steps".format(steps_taken))
    pos_x = 0.0
    step_x(int(2 * STEPS_PER_MM_X), 0)   # back off 2mm away from switch
    print("  [home_x] done")

def home_all():
    pen_up()
    print("[G28] Homing Y axis...")
    home_axis_y()
    print("[G28] Homing X axis...")
    home_axis_x()
    print("[G28] Homing complete. X=0 Y=0")

# ---------------------------------------------------------------------------
# G-code Parser
# ---------------------------------------------------------------------------

def parse_gcode_line(line):
    """
    Parse one G-code line into a parameter dict.
    'G1 X50.0 Y30.5 F2000' → {'cmd': 'G1', 'X': 50.0, 'Y': 30.5, 'F': 2000.0}
    Returns None for blank lines or pure comments.
    """
    if ';' in line:
        line = line[:line.index(';')]
    line = line.strip().upper()
    if not line:
        return None

    tokens = line.split()
    result = {}

    for token in tokens:
        if token[0].isalpha():
            key = token[0]
            try:
                result[key] = float(token[1:])
            except ValueError:
                pass

    if 'G' in result:
        result['cmd'] = 'G' + str(int(result['G']))
    elif 'M' in result:
        result['cmd'] = 'M' + str(int(result['M']))
    else:
        result['cmd'] = None

    return result

# ---------------------------------------------------------------------------
# Arc Interpolation — G2 (CW) and G3 (CCW)
# ---------------------------------------------------------------------------

import math

def arc_move(target_x, target_y, offset_i, offset_j, clockwise):
    """
    Arc from current position to (target_x, target_y).
    Center is at (pos_x + I, pos_y + J).
    Breaks arc into small linear segments fed through linear_move.
    clockwise=True → G2, clockwise=False → G3
    """
    global pos_x, pos_y

    # Arc center in absolute coordinates
    cx = pos_x + offset_i
    cy = pos_y + offset_j

    # Radius from center to start point
    radius = math.sqrt((pos_x - cx) ** 2 + (pos_y - cy) ** 2)

    # Start and end angles
    start_angle = math.atan2(pos_y - cy, pos_x - cx)
    end_angle   = math.atan2(target_y - cy, target_x - cx)

    # Sweep angle — ensure correct direction
    if clockwise:
        if end_angle >= start_angle:
            end_angle -= 2 * math.pi
    else:
        if end_angle <= start_angle:
            end_angle += 2 * math.pi

    sweep = end_angle - start_angle

    # Number of segments — 1 segment per degree gives smooth arcs
    num_segments = max(1, int(abs(sweep) * radius))  # ~1 segment per mm of arc
    num_segments = max(num_segments, 20)              # minimum 20 segments always

    for i in range(1, num_segments + 1):
        angle = start_angle + sweep * (i / num_segments)
        sx = cx + radius * math.cos(angle)
        sy = cy + radius * math.sin(angle)
        linear_move(sx, sy)

# ---------------------------------------------------------------------------
# G-code Executor
# ---------------------------------------------------------------------------

def execute(params):
    global pos_x, pos_y, feedrate, absolute_mode

    cmd = params.get('cmd')
    if cmd is None:
        return

    # G0 — Rapid move (no feedrate, full speed)
    if cmd == 'G0':
        if absolute_mode:
            tx = params.get('X', pos_x)
            ty = params.get('Y', pos_y)
        else:
            tx = pos_x + params.get('X', 0.0)
            ty = pos_y + params.get('Y', 0.0)
        linear_move(tx, ty)

    # G1 — Linear move at feedrate
    elif cmd == 'G1':
        if 'F' in params:
            feedrate = min(params['F'], MAX_FEEDRATE)
        if absolute_mode:
            tx = params.get('X', pos_x)
            ty = params.get('Y', pos_y)
        else:
            tx = pos_x + params.get('X', 0.0)
            ty = pos_y + params.get('Y', 0.0)
        linear_move(tx, ty)

    # G2 — Clockwise arc
    elif cmd == 'G2':
        if 'F' in params:
            feedrate = min(params['F'], MAX_FEEDRATE)
        if absolute_mode:
            tx = params.get('X', pos_x)
            ty = params.get('Y', pos_y)
        else:
            tx = pos_x + params.get('X', 0.0)
            ty = pos_y + params.get('Y', 0.0)
        i = params.get('I', 0.0)
        j = params.get('J', 0.0)
        arc_move(tx, ty, i, j, clockwise=True)

    # G3 — Counter-clockwise arc
    elif cmd == 'G3':
        if 'F' in params:
            feedrate = min(params['F'], MAX_FEEDRATE)
        if absolute_mode:
            tx = params.get('X', pos_x)
            ty = params.get('Y', pos_y)
        else:
            tx = pos_x + params.get('X', 0.0)
            ty = pos_y + params.get('Y', 0.0)
        i = params.get('I', 0.0)
        j = params.get('J', 0.0)
        arc_move(tx, ty, i, j, clockwise=False)

    # G4 — Dwell (P = milliseconds)
    elif cmd == 'G4':
        time.sleep_ms(int(params.get('P', 0)))

    # G28 — Home all axes
    elif cmd == 'G28':
        home_all()

    # G90 — Absolute positioning mode
    elif cmd == 'G90':
        absolute_mode = True

    # G91 — Relative positioning mode
    elif cmd == 'G91':
        absolute_mode = False

    # G92 — Set current position as origin
    elif cmd == 'G92':
        pos_x = params.get('X', pos_x)
        pos_y = params.get('Y', pos_y)

    # M3 — Pen down
    elif cmd == 'M3':
        pen_down()

    # M5 — Pen up
    elif cmd == 'M5':
        pen_up()

    # M17 — Enable motors
    elif cmd == 'M17':
        enable_motors()

    # M18 — Disable motors
    elif cmd == 'M18':
        disable_motors()

    # M100 — Emergency stop (requires hard reset to recover)
    elif cmd == 'M100':
        pen_up()
        disable_motors()
        while True:
            pass

    # M2 — End of program
    elif cmd == 'M2':
        pen_up()

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# DEBUG_MODE = True  → type G-code directly in Thonny's shell
# DEBUG_MODE = False → production mode, receive from Pi 4 over USB serial
DEBUG_MODE = False

def main():
    enable_motors()
    pen_up()

    if DEBUG_MODE:
        print("=== DEBUG MODE — Type G-code commands below ===")
        print("Examples:  G1 X50 Y0    G28    M3    M5    M18")
        print("Type 'exit' to quit\n")
        debug_shell()
    else:
        print("Plotter ready")
        serial_loop()

# ---------------------------------------------------------------------------
# Debug Shell — interactive Thonny testing
# ---------------------------------------------------------------------------

def debug_shell():
    while True:
        try:
            line = input("gcode> ").strip()
        except EOFError:
            break

        if not line:
            continue

        if line.lower() == 'exit':
            pen_up()
            disable_motors()
            print("Motors disabled. Goodbye.")
            break

        if line.lower() == 'pos':
            print("  X={:.3f}mm  Y={:.3f}mm  pen={}  mode={}".format(
                pos_x, pos_y,
                "DOWN" if pen_is_down else "UP",
                "ABS" if absolute_mode else "REL"
            ))
            continue

        if line.lower() == 'help':
            print("  G-code: G0, G1, G4, G28, G90, G91, G92")
            print("  Pen:    M3 (down)  M5 (up)")
            print("  Motors: M17 (enable)  M18 (disable)  M100 (e-stop)")
            print("  Debug:  pos, help, exit")
            continue

        try:
            params = parse_gcode_line(line)
            if params and params.get('cmd'):
                execute(params)
                print("  ok | X={:.3f} Y={:.3f} pen={}".format(
                    pos_x, pos_y,
                    "DOWN" if pen_is_down else "UP"
                ))
            else:
                print("  (blank or comment — skipped)")
        except Exception as e:
            print("  error: {}".format(str(e)))

# ---------------------------------------------------------------------------
# Serial Loop — production mode, Pi 4 sends G-code over USB
# ---------------------------------------------------------------------------

def serial_loop():
    while True:
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline().strip()
            if not line:
                sys.stdout.write("ok\n")
                continue
            try:
                params = parse_gcode_line(line)
                if params:
                    execute(params)
                sys.stdout.write("ok\n")
            except Exception as e:
                sys.stdout.write("error: {}\n".format(str(e)))

main()