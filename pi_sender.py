"""
Pen Plotter — Raspberry Pi 4 G-code Sender
Runs on startup, detects USB flash drive, streams selected .gcode file to Pico 2W.

Requirements:
    pip3 install pyserial

Usage:
    python3 pi_sender.py

The script:
  1. Waits for the Pico to connect on USB serial (/dev/ttyACM0)
  2. Scans for a USB flash drive mounted under /media/
  3. Lists .gcode files found on the drive
  4. User selects a file via terminal input (UI will replace this later)
  5. Streams the file line by line, waiting for 'ok' after each line
  6. Reports progress and any errors
"""

import serial
import serial.tools.list_ports
import os
import sys
import time
import glob

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PICO_BAUD        = 115200
PICO_READY_MSG   = "Plotter ready"   # string Pico prints on boot
SERIAL_TIMEOUT   = 30                # seconds to wait for 'ok' per line
CONNECT_TIMEOUT  = 30                # seconds to wait for Pico to appear
USB_MOUNT_ROOT   = "/media"          # Pi mounts USB drives here
GCODE_EXTENSIONS = ('.gcode', '.gc', '.ngc', '.nc')

# ---------------------------------------------------------------------------
# Serial — find and connect to Pico
# ---------------------------------------------------------------------------

def find_pico_port():
    """
    Scan serial ports for the Pico 2W.
    The Pico shows up as a USB CDC device — vendor ID 0x2E8A (Raspberry Pi).
    Falls back to /dev/ttyACM0 if vendor ID scan fails.
    """
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == 0x2E8A:   # Raspberry Pi / Pico vendor ID
            return port.device
    # Fallback — common default on Pi
    if os.path.exists('/dev/ttyACM0'):
        return '/dev/ttyACM0'
    return None


def connect_to_pico():
    """Wait for Pico to appear on USB, then open serial connection."""
    print("Waiting for Pico to connect...")
    start = time.time()

    while time.time() - start < CONNECT_TIMEOUT:
        port = find_pico_port()
        if port:
            try:
                ser = serial.Serial(port, PICO_BAUD, timeout=SERIAL_TIMEOUT)
                print("Connected to Pico on {}".format(port))
                # Wait for ready message
                wait_start = time.time()
                while time.time() - wait_start < 10:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        print("  Pico: {}".format(line))
                    if PICO_READY_MSG in line:
                        print("Pico is ready.\n")
                        return ser
                # If no ready message, return anyway — Pico may already be running
                return ser
            except serial.SerialException as e:
                print("  Serial error: {} — retrying...".format(e))
        time.sleep(1)

    print("ERROR: Could not connect to Pico after {}s.".format(CONNECT_TIMEOUT))
    sys.exit(1)

# ---------------------------------------------------------------------------
# USB Flash Drive — find and list G-code files
# ---------------------------------------------------------------------------

def find_usb_drives():
    """Return list of mount points under /media/ that look like USB drives."""
    drives = []
    if not os.path.exists(USB_MOUNT_ROOT):
        return drives
    # /media/<username>/<drive_name> on Raspberry Pi OS
    for user_dir in os.listdir(USB_MOUNT_ROOT):
        user_path = os.path.join(USB_MOUNT_ROOT, user_dir)
        if os.path.isdir(user_path):
            for drive in os.listdir(user_path):
                drive_path = os.path.join(user_path, drive)
                if os.path.isdir(drive_path):
                    drives.append(drive_path)
    return drives


def find_gcode_files(drive_path):
    """Recursively find all G-code files on the drive."""
    files = []
    for ext in GCODE_EXTENSIONS:
        pattern = os.path.join(drive_path, '**', '*' + ext)
        files.extend(glob.glob(pattern, recursive=True))
    return sorted(files)


def select_gcode_file():
    """
    Scan for USB drives and let user pick a G-code file.
    Returns the full path to the selected file.
    This will be replaced by the touchscreen UI later.
    """
    print("Scanning for USB flash drive...")

    # Wait up to 10s for drive to mount
    drives = []
    for _ in range(10):
        drives = find_usb_drives()
        if drives:
            break
        time.sleep(1)

    if not drives:
        print("No USB drive found. Place a drive with .gcode files and restart.")
        sys.exit(1)

    # Use first drive found (UI will allow selection later)
    drive = drives[0]
    print("Found drive: {}\n".format(drive))

    files = find_gcode_files(drive)
    if not files:
        print("No .gcode files found on drive.")
        sys.exit(1)

    print("Available G-code files:")
    for i, f in enumerate(files):
        # Show path relative to drive root for readability
        rel = os.path.relpath(f, drive)
        size_kb = os.path.getsize(f) / 1024
        print("  [{}] {} ({:.1f} KB)".format(i + 1, rel, size_kb))

    print()
    while True:
        try:
            choice = input("Select file number (1-{}): ".format(len(files))).strip()
            idx = int(choice) - 1
            if 0 <= idx < len(files):
                return files[idx]
            else:
                print("  Invalid selection — enter a number between 1 and {}".format(len(files)))
        except ValueError:
            print("  Please enter a number.")
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)

# ---------------------------------------------------------------------------
# G-code Streaming
# ---------------------------------------------------------------------------

def count_lines(filepath):
    """Count non-blank, non-comment lines for progress tracking."""
    count = 0
    with open(filepath, 'r', errors='ignore') as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith(';'):
                count += 1
    return count


def send_gcode_file(ser, filepath):
    """
    Stream G-code file to Pico line by line.
    Waits for 'ok' acknowledgment after each line before sending the next.
    """
    total = count_lines(filepath)
    sent = 0
    errors = 0
    start_time = time.time()

    print("\nStarting: {}".format(os.path.basename(filepath)))
    print("Total commands: {}\n".format(total))

    with open(filepath, 'r', errors='ignore') as f:
        for raw_line in f:
            line = raw_line.strip()

            # Skip blank lines and pure comments
            if not line or line.startswith(';'):
                continue

            # Strip inline comments before sending
            if ';' in line:
                line = line[:line.index(';')].strip()
            if not line:
                continue

            # Send line to Pico
            ser.write((line + '\n').encode('utf-8'))
            sent += 1

            # Wait for acknowledgment
            response = wait_for_ack(ser, line)

            if response.startswith('error'):
                errors += 1
                print("  LINE {}: {} → {}".format(sent, line, response))
            
            # Progress update every 10 lines
            if sent % 10 == 0 or sent == total:
                elapsed = time.time() - start_time
                pct = (sent / total * 100) if total > 0 else 0
                rate = sent / elapsed if elapsed > 0 else 0
                eta = (total - sent) / rate if rate > 0 else 0
                print("  Progress: {}/{} ({:.0f}%) | {:.1f} lines/s | ETA {:.0f}s".format(
                    sent, total, pct, rate, eta
                ))

    elapsed = time.time() - start_time
    print("\nDone! {} lines sent in {:.1f}s. {} errors.".format(sent, elapsed, errors))


def wait_for_ack(ser, sent_line):
    """
    Wait for 'ok' or 'error' response from Pico.
    Prints any debug messages (lines starting with '[') from Pico.
    Times out after SERIAL_TIMEOUT seconds.
    """
    start = time.time()
    while time.time() - start < SERIAL_TIMEOUT:
        if ser.in_waiting:
            raw = ser.readline().decode('utf-8', errors='ignore').strip()
            if not raw:
                continue
            # Pass through Pico debug prints to terminal
            if raw.startswith('[') or raw.startswith('  ['):
                print("  Pico: {}".format(raw))
                continue
            # Acknowledgment received
            if raw == 'ok' or raw.startswith('error'):
                return raw
        time.sleep(0.001)

    print("  TIMEOUT waiting for ack after: {}".format(sent_line))
    return 'error: timeout'

# ---------------------------------------------------------------------------
# Emergency Stop — Ctrl+C sends M100 to Pico
# ---------------------------------------------------------------------------

def emergency_stop(ser):
    print("\n\nEMERGENCY STOP — sending M100 to Pico...")
    try:
        ser.write(b'M100\n')
        time.sleep(0.5)
    except Exception:
        pass
    print("Machine halted. Restart Pico to resume.")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 50)
    print("  Pen Plotter — G-code Sender")
    print("=" * 50 + "\n")

    # Step 1: Connect to Pico
    ser = connect_to_pico()

    # Step 2: Select G-code file from USB drive
    filepath = select_gcode_file()
    print("\nSelected: {}".format(filepath))

    # Step 3: Confirm before running
    print()
    confirm = input("Start plotting? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        ser.close()
        sys.exit(0)

    # Step 4: Stream file to Pico
    try:
        send_gcode_file(ser, filepath)
    except KeyboardInterrupt:
        emergency_stop(ser)
    finally:
        ser.close()
        print("Serial connection closed.")

if __name__ == '__main__':
    main()