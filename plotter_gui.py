"""
Pen Plotter — Touchscreen GUI
Designed for 800x480 display on Raspberry Pi 4.
Integrates with pi_sender.py logic for Pico communication.

Requirements:
    sudo apt install python3-serial python3-tk
    
Run:
    python3 plotter_gui.py
"""

import tkinter as tk
from tkinter import font as tkfont
import serial
import serial.tools.list_ports
import threading
import os
import glob
import time
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCREEN_W         = 800
SCREEN_H         = 480
PICO_BAUD        = 115200
PICO_READY_MSG   = "Plotter ready"
SERIAL_TIMEOUT   = 30
CONNECT_TIMEOUT  = 15
USB_MOUNT_ROOT   = "/media"
GCODE_EXTENSIONS = ('.gcode', '.gc', '.ngc', '.nc')

# ---------------------------------------------------------------------------
# Color Palette — Industrial dark theme
# ---------------------------------------------------------------------------

BG          = "#0d0d0d"    # near black background
PANEL       = "#151515"    # slightly lighter panels
BORDER      = "#2a2a2a"    # subtle borders
ACCENT      = "#e8c547"    # amber/yellow — machine warning aesthetic
ACCENT2     = "#4fc3f7"    # cyan — status/info
GREEN       = "#69f0ae"    # success / running
RED         = "#ff5252"    # error / stop
TEXT        = "#f0f0f0"    # primary text
TEXT_DIM    = "#666666"    # secondary text
TEXT_MID    = "#aaaaaa"    # mid text

# ---------------------------------------------------------------------------
# Serial State (shared between GUI and worker threads)
# ---------------------------------------------------------------------------

ser           = None
is_connected  = False
is_running    = False
stop_flag     = False

# ---------------------------------------------------------------------------
# Helper — find USB drives and G-code files
# ---------------------------------------------------------------------------

def find_usb_drives():
    drives = []
    if not os.path.exists(USB_MOUNT_ROOT):
        return drives
    for user_dir in os.listdir(USB_MOUNT_ROOT):
        user_path = os.path.join(USB_MOUNT_ROOT, user_dir)
        if os.path.isdir(user_path):
            for drive in os.listdir(user_path):
                drive_path = os.path.join(user_path, drive)
                if os.path.isdir(drive_path):
                    drives.append(drive_path)
    return drives


def find_gcode_files(drive_path):
    files = []
    for ext in GCODE_EXTENSIONS:
        files.extend(glob.glob(os.path.join(drive_path, '**', '*' + ext), recursive=True))
    return sorted(files)


def find_pico_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == 0x2E8A:
            return port.device
    if os.path.exists('/dev/ttyACM0'):
        return '/dev/ttyACM0'
    return None


def count_lines(filepath):
    count = 0
    with open(filepath, 'r', errors='ignore') as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith(';'):
                count += 1
    return count

# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class PlotterApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Pen Plotter")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        # Fullscreen on startup
        self.root.attributes('-fullscreen', True)

        # Update geometry vars to match actual screen size after fullscreen
        self.root.update_idletasks()
        SCREEN_W = self.root.winfo_screenwidth()
        SCREEN_H = self.root.winfo_screenheight()

        # Press Escape to exit fullscreen (useful for development)
        self.root.bind('<Escape>', lambda e: self.root.attributes('-fullscreen', False))

        # Hide cursor for touchscreen kiosk mode
        # self.root.config(cursor="none")  # uncomment for kiosk

        # State
        self.selected_file  = None
        self.gcode_files    = []
        self.total_lines    = 0
        self.sent_lines     = 0
        self.file_list_offset = 0   # scroll offset for file list

        # Fonts
        self.font_title  = tkfont.Font(family="Courier", size=13, weight="bold")
        self.font_label  = tkfont.Font(family="Courier", size=10)
        self.font_small  = tkfont.Font(family="Courier", size=8)
        self.font_btn    = tkfont.Font(family="Courier", size=11, weight="bold")
        self.font_status = tkfont.Font(family="Courier", size=9)
        self.font_file   = tkfont.Font(family="Courier", size=10)
        self.font_big    = tkfont.Font(family="Courier", size=18, weight="bold")

        self._build_ui()
        self._connect_pico_async()
        self._scan_usb()

    # -----------------------------------------------------------------------
    # UI Construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        # ── Top header bar ──────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=PANEL, height=52)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        tk.Label(header, text="◈  PEN PLOTTER CONTROL",
                 font=self.font_title, bg=PANEL, fg=ACCENT
                 ).place(x=16, y=14)

        self.conn_dot = tk.Label(header, text="●", font=self.font_title,
                                  bg=PANEL, fg=RED)
        self.conn_dot.place(x=680, y=14)

        self.conn_label = tk.Label(header, text="DISCONNECTED",
                                    font=self.font_small, bg=PANEL, fg=RED)
        self.conn_label.place(x=700, y=17)

        # Divider line
        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill=tk.X)

        # ── Main body ────────────────────────────────────────────────────────
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Left panel — file browser (480px wide)
        left = tk.Frame(body, bg=PANEL, width=480)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        self._build_file_panel(left)

        # Divider
        tk.Frame(body, bg=BORDER, width=2).pack(side=tk.LEFT, fill=tk.Y)

        # Right panel — controls & status (316px wide)
        right = tk.Frame(body, bg=BG, width=316)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right.pack_propagate(False)
        self._build_control_panel(right)

        # ── Bottom status bar ────────────────────────────────────────────────
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X)
        statusbar = tk.Frame(self.root, bg=PANEL, height=28)
        statusbar.pack(fill=tk.X, side=tk.BOTTOM)
        statusbar.pack_propagate(False)

        self.status_var = tk.StringVar(value="Ready. Insert USB drive with G-code files.")
        tk.Label(statusbar, textvariable=self.status_var,
                 font=self.font_small, bg=PANEL, fg=TEXT_DIM,
                 anchor='w').pack(side=tk.LEFT, padx=10, pady=6)

    def _build_file_panel(self, parent):
        # Section header
        hdr = tk.Frame(parent, bg=PANEL, height=36)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="USB DRIVE  /  G-CODE FILES",
                 font=self.font_small, bg=PANEL, fg=TEXT_DIM,
                 anchor='w').pack(side=tk.LEFT, padx=12, pady=10)

        # Refresh button
        self.refresh_btn = self._make_button(
            hdr, "↻", self._scan_usb, width=3,
            bg=PANEL, fg=ACCENT, active_bg="#1e1e1e"
        )
        self.refresh_btn.pack(side=tk.RIGHT, padx=8, pady=4)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

        # Drive label
        self.drive_var = tk.StringVar(value="No drive detected")
        tk.Label(parent, textvariable=self.drive_var,
                 font=self.font_small, bg=PANEL, fg=TEXT_DIM,
                 anchor='w').pack(fill=tk.X, padx=12, pady=(6, 2))

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

        # File list container
        self.file_frame = tk.Frame(parent, bg=PANEL)
        self.file_frame.pack(fill=tk.BOTH, expand=True)

        # Scroll buttons
        scroll_bar = tk.Frame(parent, bg=PANEL, height=36)
        scroll_bar.pack(fill=tk.X)
        scroll_bar.pack_propagate(False)

        self._make_button(scroll_bar, "▲ SCROLL UP",
                          self._scroll_up, width=20,
                          bg=BORDER, fg=TEXT_MID).pack(side=tk.LEFT, padx=6, pady=4)
        self._make_button(scroll_bar, "▼ SCROLL DOWN",
                          self._scroll_down, width=20,
                          bg=BORDER, fg=TEXT_MID).pack(side=tk.RIGHT, padx=6, pady=4)

    def _build_control_panel(self, parent):
        # Selected file display
        sel_frame = tk.Frame(parent, bg=BG, height=72)
        sel_frame.pack(fill=tk.X, padx=14, pady=(12, 0))
        sel_frame.pack_propagate(False)

        tk.Label(sel_frame, text="SELECTED FILE",
                 font=self.font_small, bg=BG, fg=TEXT_DIM,
                 anchor='w').pack(anchor='w')

        self.selected_var = tk.StringVar(value="—")
        tk.Label(sel_frame, textvariable=self.selected_var,
                 font=self.font_label, bg=BG, fg=ACCENT,
                 anchor='w', wraplength=280, justify='left'
                 ).pack(anchor='w', pady=(2, 0))

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X, padx=14, pady=8)

        # Progress section
        prog_frame = tk.Frame(parent, bg=BG)
        prog_frame.pack(fill=tk.X, padx=14)

        tk.Label(prog_frame, text="PROGRESS",
                 font=self.font_small, bg=BG, fg=TEXT_DIM,
                 anchor='w').pack(anchor='w')

        self.progress_var = tk.StringVar(value="0 / 0 lines")
        tk.Label(prog_frame, textvariable=self.progress_var,
                 font=self.font_big, bg=BG, fg=TEXT,
                 anchor='w').pack(anchor='w')

        # Progress bar track
        bar_track = tk.Frame(prog_frame, bg=BORDER, height=8)
        bar_track.pack(fill=tk.X, pady=(4, 0))

        self.prog_bar = tk.Frame(bar_track, bg=ACCENT, height=8, width=0)
        self.prog_bar.place(x=0, y=0)
        self._prog_bar_width = 0

        self.pct_var = tk.StringVar(value="0%")
        tk.Label(prog_frame, textvariable=self.pct_var,
                 font=self.font_small, bg=BG, fg=TEXT_DIM,
                 anchor='w').pack(anchor='w', pady=(2, 0))

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X, padx=14, pady=10)

        # Machine status
        stat_frame = tk.Frame(parent, bg=BG)
        stat_frame.pack(fill=tk.X, padx=14)

        tk.Label(stat_frame, text="MACHINE STATUS",
                 font=self.font_small, bg=BG, fg=TEXT_DIM,
                 anchor='w').pack(anchor='w')

        self.machine_status_var = tk.StringVar(value="IDLE")
        self.machine_status_lbl = tk.Label(
            stat_frame, textvariable=self.machine_status_var,
            font=self.font_btn, bg=BG, fg=TEXT_DIM, anchor='w'
        )
        self.machine_status_lbl.pack(anchor='w')

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X, padx=14, pady=10)

        # Control buttons
        btn_frame = tk.Frame(parent, bg=BG)
        btn_frame.pack(fill=tk.X, padx=14)

        # RUN button
        self.run_btn = self._make_button(
            btn_frame, "▶  RUN", self._run_file,
            width=26, height=2,
            bg=GREEN, fg=BG, active_bg="#4caf7d"
        )
        self.run_btn.pack(fill=tk.X, pady=(0, 6))

        # STOP button
        self.stop_btn = self._make_button(
            btn_frame, "■  STOP", self._stop,
            width=26, height=2,
            bg=RED, fg=BG, active_bg="#cc3333"
        )
        self.stop_btn.pack(fill=tk.X, pady=(0, 6))
        self.stop_btn.config(state=tk.DISABLED)

        # HOME button
        self.home_btn = self._make_button(
            btn_frame, "⌂  HOME AXES", self._home,
            width=26,
            bg=BORDER, fg=ACCENT2, active_bg="#2a2a2a"
        )
        self.home_btn.pack(fill=tk.X)

    # -----------------------------------------------------------------------
    # Button Factory
    # -----------------------------------------------------------------------

    def _make_button(self, parent, text, command, width=10, height=1,
                     bg=BORDER, fg=TEXT, active_bg=None):
        if active_bg is None:
            active_bg = bg
        btn = tk.Button(
            parent, text=text, command=command,
            font=self.font_btn, width=width, height=height,
            bg=bg, fg=fg, activebackground=active_bg, activeforeground=fg,
            relief=tk.FLAT, bd=0, cursor="hand2",
            highlightthickness=1, highlightbackground=BORDER
        )
        return btn

    # -----------------------------------------------------------------------
    # File Browser
    # -----------------------------------------------------------------------

    def _scan_usb(self):
        self.gcode_files = []
        self.file_list_offset = 0
        drives = find_usb_drives()

        if not drives:
            self.drive_var.set("No drive detected")
            self._set_status("No USB drive found. Insert drive and press ↻")
            self._render_file_list()
            return

        drive = drives[0]
        self.drive_var.set("Drive: " + os.path.basename(drive))
        self.gcode_files = find_gcode_files(drive)

        if not self.gcode_files:
            self._set_status("No .gcode files found on drive.")
        else:
            self._set_status("Found {} file(s). Tap to select.".format(len(self.gcode_files)))

        self._render_file_list()

    def _render_file_list(self):
        # Clear existing
        for widget in self.file_frame.winfo_children():
            widget.destroy()

        if not self.gcode_files:
            tk.Label(self.file_frame,
                     text="No files found.\nInsert USB drive with .gcode files.",
                     font=self.font_label, bg=PANEL, fg=TEXT_DIM,
                     justify='center'
                     ).pack(expand=True)
            return

        # Show up to 6 files at a time
        visible = self.gcode_files[self.file_list_offset:self.file_list_offset + 6]

        for i, filepath in enumerate(visible):
            is_selected = (filepath == self.selected_file)
            fname = os.path.basename(filepath)
            size_kb = os.path.getsize(filepath) / 1024

            row_bg = ACCENT if is_selected else PANEL
            row_fg = BG if is_selected else TEXT

            row = tk.Frame(self.file_frame,
                           bg=row_bg,
                           height=52,
                           cursor="hand2")
            row.pack(fill=tk.X)
            row.pack_propagate(False)

            # File icon + name
            tk.Label(row, text="▸ " + fname,
                     font=self.font_file,
                     bg=row_bg, fg=row_fg,
                     anchor='w'
                     ).place(x=12, y=8)

            # File size
            tk.Label(row, text="{:.1f} KB".format(size_kb),
                     font=self.font_small,
                     bg=row_bg, fg=BG if is_selected else TEXT_DIM,
                     anchor='w'
                     ).place(x=12, y=30)

            # Index indicator
            idx = self.file_list_offset + i + 1
            tk.Label(row, text=str(idx),
                     font=self.font_small,
                     bg=row_bg, fg=BG if is_selected else TEXT_DIM,
                     anchor='e'
                     ).place(x=440, y=18)

            # Bind click
            fp = filepath
            row.bind("<Button-1>", lambda e, f=fp: self._select_file(f))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda e, f=fp: self._select_file(f))

            # Separator
            if i < len(visible) - 1:
                tk.Frame(self.file_frame, bg=BORDER, height=1).pack(fill=tk.X)

    def _select_file(self, filepath):
        self.selected_file = filepath
        name = os.path.basename(filepath)
        self.selected_var.set(name)
        self.total_lines = count_lines(filepath)
        self.progress_var.set("0 / {}".format(self.total_lines))
        self.pct_var.set("0%")
        self._update_progress_bar(0)
        self._set_status("Selected: {}  ({} commands)".format(name, self.total_lines))
        self._render_file_list()

    def _scroll_up(self):
        if self.file_list_offset > 0:
            self.file_list_offset -= 1
            self._render_file_list()

    def _scroll_down(self):
        if self.file_list_offset + 6 < len(self.gcode_files):
            self.file_list_offset += 1
            self._render_file_list()

    # -----------------------------------------------------------------------
    # Progress Bar
    # -----------------------------------------------------------------------

    def _update_progress_bar(self, fraction):
        # Progress bar width scales to ~270px (right panel minus padding)
        max_w = 270
        w = int(fraction * max_w)
        self.prog_bar.config(width=max(w, 0))

    # -----------------------------------------------------------------------
    # Pico Connection
    # -----------------------------------------------------------------------

    def _connect_pico_async(self):
        self._set_conn_status(False)
        t = threading.Thread(target=self._connect_pico_worker, daemon=True)
        t.start()

    def _connect_pico_worker(self):
        global ser, is_connected
        start = time.time()
        while time.time() - start < CONNECT_TIMEOUT:
            port = find_pico_port()
            if port:
                try:
                    ser = serial.Serial(port, PICO_BAUD, timeout=SERIAL_TIMEOUT)
                    is_connected = True
                    self.root.after(0, lambda: self._set_conn_status(True))
                    self.root.after(0, lambda: self._set_status(
                        "Pico connected on {}".format(port)))
                    return
                except Exception as e:
                    pass
            time.sleep(1)
        self.root.after(0, lambda: self._set_status(
            "Pico not found. Check USB cable and reboot Pico."))

    def _set_conn_status(self, connected):
        if connected:
            self.conn_dot.config(fg=GREEN)
            self.conn_label.config(text="CONNECTED", fg=GREEN)
        else:
            self.conn_dot.config(fg=RED)
            self.conn_label.config(text="DISCONNECTED", fg=RED)

    # -----------------------------------------------------------------------
    # Machine Controls
    # -----------------------------------------------------------------------

    def _run_file(self):
        global is_running, stop_flag
        if not is_connected:
            self._set_status("ERROR: Pico not connected.")
            return
        if not self.selected_file:
            self._set_status("ERROR: No file selected.")
            return
        if is_running:
            return

        stop_flag = False
        is_running = True
        self.run_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.home_btn.config(state=tk.DISABLED)
        self._set_machine_status("RUNNING", GREEN)

        t = threading.Thread(target=self._stream_worker, daemon=True)
        t.start()

    def _stream_worker(self):
        global is_running, stop_flag
        sent = 0

        try:
            with open(self.selected_file, 'r', errors='ignore') as f:
                for raw_line in f:
                    if stop_flag:
                        break

                    line = raw_line.strip()
                    if not line or line.startswith(';'):
                        continue
                    if ';' in line:
                        line = line[:line.index(';')].strip()
                    if not line:
                        continue

                    ser.write((line + '\n').encode('utf-8'))
                    sent += 1

                    # Wait for ack
                    ack = self._wait_for_ack(line)

                    # Update UI from main thread
                    s = sent
                    t = self.total_lines
                    pct = (s / t) if t > 0 else 0
                    self.root.after(0, lambda s=s, t=t, p=pct: self._update_ui_progress(s, t, p))

            if stop_flag:
                self.root.after(0, lambda: self._set_machine_status("STOPPED", RED))
                self.root.after(0, lambda: self._set_status("Stopped by user."))
            else:
                self.root.after(0, lambda: self._set_machine_status("COMPLETE", ACCENT2))
                self.root.after(0, lambda: self._set_status("Plot complete!"))

        except Exception as e:
            self.root.after(0, lambda: self._set_status("ERROR: {}".format(str(e))))
            self.root.after(0, lambda: self._set_machine_status("ERROR", RED))

        finally:
            is_running = False
            self.root.after(0, self._reset_buttons)

    def _wait_for_ack(self, sent_line):
        start = time.time()
        while time.time() - start < SERIAL_TIMEOUT:
            if ser.in_waiting:
                raw = ser.readline().decode('utf-8', errors='ignore').strip()
                if raw in ('ok',) or raw.startswith('error'):
                    return raw
            time.sleep(0.001)
        return 'error: timeout'

    def _update_ui_progress(self, sent, total, fraction):
        self.progress_var.set("{} / {}".format(sent, total))
        self.pct_var.set("{:.0f}%".format(fraction * 100))
        self._update_progress_bar(fraction)

    def _stop(self):
        global stop_flag
        stop_flag = True
        if ser and is_connected:
            try:
                ser.write(b'M100\n')
            except Exception:
                pass
        self._set_status("Stop signal sent.")

    def _home(self):
        if not is_connected:
            self._set_status("ERROR: Pico not connected.")
            return
        if is_running:
            self._set_status("Cannot home while running.")
            return
        try:
            ser.write(b'G28\n')
            self._set_machine_status("HOMING", ACCENT)
            self._set_status("Homing axes...")
        except Exception as e:
            self._set_status("ERROR: {}".format(str(e)))

    def _reset_buttons(self):
        self.run_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.home_btn.config(state=tk.NORMAL)

    # -----------------------------------------------------------------------
    # Status Helpers
    # -----------------------------------------------------------------------

    def _set_status(self, msg):
        self.status_var.set(msg)

    def _set_machine_status(self, text, color):
        self.machine_status_var.set(text)
        self.machine_status_lbl.config(fg=color)

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    root = tk.Tk()
    app = PlotterApp(root)
    root.mainloop()