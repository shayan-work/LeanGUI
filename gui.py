import sys
import os
import json
import re
import select
import time
import subprocess
import pyqtgraph as pg
import numpy as np
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLineEdit, QLabel, 
                             QFileDialog, QDoubleSpinBox, QMessageBox, QComboBox, QGridLayout, QFormLayout, QGroupBox)
from PySide6.QtCore import QTimer, QThread, Signal
from collections import deque

class ConstellationReader(QThread):
    # Signal to send parsed (I, Q) points back to the main GUI thread
    new_points_signal = Signal(list)  # Sends a batch of [i, q] points

    def __init__(self, read_fd):
        super().__init__()
        self.read_fd = read_fd
        self.running = True

    def run(self):
        with os.fdopen(self.read_fd, 'r') as f:
            batch = []
            for line in f:
                if not self.running:
                    break
                line = line.strip()
                if not line.startswith("SYMBOLS "):
                    continue
                try:
                    pairs = json.loads(line[len("SYMBOLS "):])
                    for i, q in pairs:
                        batch.append((i, q))
                        if len(batch) >= 20:
                            self.new_points_signal.emit(batch)
                            batch = []
                except (ValueError, TypeError):
                    continue

            if batch:
                self.new_points_signal.emit(batch)   
    def stop(self):
        self.running = False

class InfoReader(QThread):
    new_line_signal = Signal(str)

    def __init__(self, read_fd):
        super().__init__()
        self.read_fd = read_fd
        self.running = True
        #self.unknown_info_lines = deque(maxlen=6)

    def run(self):
        with os.fdopen(self.read_fd, 'r') as f:
            for line in f:
                if not self.running:
                    break
                line = line.strip()
                if line:
                    self.new_line_signal.emit(line)

    def stop(self):
        self.running = False

class StderrReader(QThread):
    new_line_signal = Signal(str)

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.running = True

    def run(self):
        for line in self.stream:
            if not self.running:
                break
            line = line.decode('utf-8', errors='replace').strip()
            line = re.sub(r'[_.C!]{3,}', '', line).strip()
            if line:
                self.new_line_signal.emit(line)

    def stop(self):
        self.running = False
        
class SymbolRateSearchThread(QThread):
    progress_signal = Signal(str)
    found_signal = Signal(float)
    failed_signal = Signal()

    def __init__(self, iq_path, samp_rate_hz, rs_estimate_msps, trial_timeout_s=2.0):
        super().__init__()
        self.iq_path = iq_path
        self.samp_rate_hz = samp_rate_hz
        self.rs_estimate_msps = rs_estimate_msps
        self.trial_timeout_s = trial_timeout_s
        self.running = True

    def run(self):
        # Relative offsets around the spectral estimate, tightest first
        deltas_pct = [0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0, 5.0, -5.0]
        for i, pct in enumerate(deltas_pct):
            if not self.running:
                return
            candidate = self.rs_estimate_msps * (1.0 + pct / 100.0)
            self.progress_signal.emit(
                f"Trying {candidate:.3f} Msym/s ({i + 1}/{len(deltas_pct)})..."
            )
            if self._try_symbol_rate(candidate):
                self.found_signal.emit(candidate)
                return
        self.failed_signal.emit()

    def _try_symbol_rate(self, rs_msps):
        info_r_fd, info_w_fd = os.pipe()

        cmd = [
            './leandvb', '-v', '-d', '--f32',
            '-f', f"{int(self.samp_rate_hz)}",
            '--sr', f"{int(rs_msps * 1e6)}",
            '--standard', 'DVB-S2',
            '--ldpc-helper', 'ldpc_tool',
            '--fd-info', f"{info_w_fd}",
        ]
        try:
            f = open(self.iq_path, 'rb')
        except OSError:
            os.close(info_r_fd)
            os.close(info_w_fd)
            return False

        proc = subprocess.Popen(
            cmd, stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=[info_w_fd]
        )
        f.close()
        os.close(info_w_fd)

        current_lock = False
        current_vber = None
        consecutive_good = 0
        required_consecutive = 3
        vber_threshold = 0.01
        locked_confirmed = False

        deadline = time.time() + self.trial_timeout_s
        info_stream = os.fdopen(info_r_fd, 'r')
        try:
            while time.time() < deadline and self.running:
                remaining = deadline - time.time()
                ready, _, _ = select.select([info_stream], [], [], max(remaining, 0))
                if not ready:
                    break
                line = info_stream.readline()
                if not line:
                    break
                parts = line.strip().split(maxsplit=1)
                if not parts:
                    continue
                keyword = parts[0]
                value = parts[1].strip() if len(parts) > 1 else ""

                if keyword == "FRAMELOCK":
                    current_lock = (value == "1")
                elif keyword == "VBER":
                    try:
                        current_vber = float(value)
                    except ValueError:
                        current_vber = None

                good_now = current_lock and current_vber is not None and current_vber <= vber_threshold
                consecutive_good = consecutive_good + 1 if good_now else 0
                if consecutive_good >= required_consecutive:
                    locked_confirmed = True
                    break
        finally:
            info_stream.close()
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        return locked_confirmed

class SpectrumAnalyzerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LeanGUI : GUI Wrapper for leandvb")
        self.resize(900, 600)

        # DSP Parameters
        self.fft_size = 4096
        self.window = np.hamming(self.fft_size)
        self.file_handle = None
        self.avg_psd = None
        self.alpha = 0.3
        
        # Buffer Parameters
        self.const_history_size = 1000
        self.i_buffer = deque(maxlen=self.const_history_size)
        self.q_buffer = deque(maxlen=self.const_history_size)
        self.unknown_info_lines = deque(maxlen=6)
        
        # References to manage our background p rocesses
        self.leandvb_process = None
        self.const_reader_thread = None
        self.mpv_process = None
        self.info_reader_thread = None
        self.stderr_reader_thread = None
        self.autotune_thread = None
        
        # Initialize GUI layout
        self.init_ui()
        
        # Setup the QTimer for real-time plotting
        self.timer = QTimer()
        self.timer.timeout.connect(self.process_next_frame)
        
        self.const_timer = QTimer()
        self.const_timer.timeout.connect(self.redraw_constellation)
        self.const_timer.start(33)  # same 30fps cadence as the spectrum plot
        
        self._updating_tuning_ui = False

    def redraw_constellation(self):
        self.const_scatter.setData(x=list(self.i_buffer), y=list(self.q_buffer))

    def init_ui(self):
        # Main Widget and Layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # --- Top Control Bar ---
        control_layout = QHBoxLayout()
        
        # File selector
        control_layout.addWidget(QLabel("IQ File (.iq / .raw):"))
        self.file_path_input = QLineEdit()
        self.file_path_input.setPlaceholderText("Select a 32-bit float complex IQ file...")
        self.file_path_input.setText("/home/eocs/LeanGUI/DVBS2_SPS8_8PSK34_12MSPS_1_5MSymRate_noOffset_RRC")
        control_layout.addWidget(self.file_path_input)
        
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.browse_file)
        control_layout.addWidget(self.browse_btn)

        # Sample Rate Input
        control_layout.addWidget(QLabel("Sample Rate (MHz):"))
        self.samp_rate_spin = QDoubleSpinBox()
        self.samp_rate_spin.setRange(0.001, 1000.0)
        self.samp_rate_spin.setValue(12.000)  # Default 12 MHz
        self.samp_rate_spin.setDecimals(3)
        control_layout.addWidget(self.samp_rate_spin)
        
        # 1. Center/Offset Frequency Input (Text Box)
        control_layout.addWidget(QLabel("Offset (MHz):"))
        self.center_freq_edit = QLineEdit()
        self.center_freq_edit.setText("0.0")  # Set default starting text
        self.center_freq_edit.setFixedWidth(80) # Keep layout clean and compact
        control_layout.addWidget(self.center_freq_edit)
        
        # 2. Symbol Rate Input (Text Box)
        control_layout.addWidget(QLabel("Symbol Rate (MSps):"))
        self.symbol_rate_edit = QLineEdit()
        self.symbol_rate_edit.setText("1.500")  # Set default starting text
        self.symbol_rate_edit.setFixedWidth(80)
        control_layout.addWidget(self.symbol_rate_edit)
        
        # 3. Roll-off Factor Input (Dropdown/Combo Box)
        control_layout.addWidget(QLabel("Roll-off (α):"))
        self.rolloff_combo = QComboBox()
        self.rolloff_combo.addItems(["0.35", "0.25", "0.20"]) 
        self.rolloff_combo.setCurrentText("0.20")  # Default to 0.20
        control_layout.addWidget(self.rolloff_combo)

        # Play/Pause Buttons
        self.start_btn = QPushButton("Start Plotting")
        self.start_btn.setStyleSheet("background-color: green; color: white;")
        self.start_btn.clicked.connect(self.toggle_plotting)
        control_layout.addWidget(self.start_btn)

        # Decode Button
        self.decode_btn = QPushButton("Start Decoding")
        self.decode_btn.setStyleSheet("background-color: blue; color: white;")
        self.decode_btn.clicked.connect(self.toggle_decoding)
        control_layout.addWidget(self.decode_btn)
        
        self.autotune_btn = QPushButton("Auto-Tune Rs")
        self.autotune_btn.clicked.connect(self.start_autotune)
        control_layout.addWidget(self.autotune_btn)

        main_layout.addLayout(control_layout)
        
        self.autotune_status_label = QLabel("")
        main_layout.addWidget(self.autotune_status_label)
        
        # --- Spectrum Plot ---
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('k')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('left', 'Relative Power', units='dB')
        self.plot_widget.setLabel('bottom', 'Frequency', units='Hz')
        self.plot_widget.setTitle('Signal Spectrum')
        self.plot_widget.setYRange(-80, 20)
        self.plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        self.curve = self.plot_widget.plot(pen=pg.mkPen('c', width=1.5))

        # --- Constellation Plot ---
        self.const_widget = pg.PlotWidget()
        self.const_widget.setBackground('k')
        self.const_widget.showGrid(x=True, y=True, alpha=0.3)
        self.const_widget.setLabel('left', 'Quadrature (Q)')
        self.const_widget.setLabel('bottom', 'In-Phase (I)')
        self.const_widget.setTitle('Synchronized Symbols')

        # Keep the grid square so circles/constellations aren't skewed
        self.const_widget.setAspectLocked(True)
        self.const_widget.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)

        # Use ScatterPlotItem for high speed
        self.const_scatter = pg.ScatterPlotItem(
            size=4,
            pen=None,
            brush=pg.mkBrush(0, 255, 100, 120)
        )
        self.const_widget.addItem(self.const_scatter)

         # --- Info panel (bottom-left) ---
        self.info_panel = QGroupBox("Demodulator Status")
        info_layout = QFormLayout()

        self.lock_label = QLabel("—")
        self.ss_label = QLabel("—")
        self.mer_label = QLabel("—")
        self.ber_label = QLabel("—")
        self.offset_label = QLabel("—")
        self.modcod_label = QLabel("—")
        self.error_label = QLabel("—")
        self.error_label.setWordWrap(True)

        info_layout.addRow("Lock Status:", self.lock_label)
        info_layout.addRow("Signal Strength:", self.ss_label)
        info_layout.addRow("MER:", self.mer_label)
        info_layout.addRow("BER:", self.ber_label)
        info_layout.addRow("Freq Offset:", self.offset_label)
        info_layout.addRow("MODCOD:", self.modcod_label)
        info_layout.addRow("Debug Info:", self.error_label)

        self.info_panel.setLayout(info_layout)

        # --- Video output panel (bottom-right, mpv embeds via X11 window id) ---
        self.video_widget = QWidget()
        self.video_widget.setStyleSheet("background-color: black;")
        self.video_widget.setMinimumHeight(280)

        # --- 2x2 grid: spectrum | constellation / info | video ---
        content_grid = QGridLayout()
        content_grid.addWidget(self.plot_widget, 0, 0)
        content_grid.addWidget(self.const_widget, 0, 1)
        content_grid.addWidget(self.info_panel, 1, 0)
        content_grid.addWidget(self.video_widget, 1, 1)
        content_grid.setRowStretch(0, 1)
        content_grid.setRowStretch(1, 1)
        content_grid.setColumnStretch(0, 1)
        content_grid.setColumnStretch(1, 1)
        main_layout.addLayout(content_grid)
        
        
        # Shaded region representing occupied bandwidth (BW = Rs * (1 + alpha))
        self.tuning_region = pg.LinearRegionItem(
            values=[-625000, 625000],  # Default boundaries (Hz)
            orientation='vertical', 
            brush=pg.mkBrush(0, 100, 255, 50), 
            pen=pg.mkPen('r', style=pg.QtCore.Qt.DashLine)
        )
        self.tuning_region.setMovable(False) 
        self.plot_widget.addItem(self.tuning_region)
        self.tuning_region.setMovable(True)

        # Dashed white line indicating exact center frequency (offset)
        self.center_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('w', style=pg.QtCore.Qt.DashLine))
        self.plot_widget.addItem(self.center_line)
        
        # Connect text changes and index changes to trigger redraw
        self.center_freq_edit.textChanged.connect(self.update_tuning_bars)
        self.symbol_rate_edit.textChanged.connect(self.update_tuning_bars)
        self.rolloff_combo.currentIndexChanged.connect(self.update_tuning_bars)

        # Run an initial update calculation to draw default positions on open
        self.update_tuning_bars()
        self._updating_tuning_ui = False
        self.tuning_region.sigRegionChanged.connect(self.on_tuning_region_dragged)
        self.tuning_region.sigRegionChangeFinished.connect(self.estimate_symbol_rate_from_spectrum)

    def toggle_decoding(self):
        if self.leandvb_process:
            self.stop_decoding()
            self.decode_btn.setText("Start Decoding")
            self.decode_btn.setStyleSheet("background-color: blue; color: white;")
        else:
            self.start_decoding()
            self.decode_btn.setText("Stop Decoding")
            self.decode_btn.setStyleSheet("background-color: red; color: white;")

    def start_decoding(self):
        # Prevent double-running
        self.stop_decoding()

        iq_path = self.file_path_input.text()
        if not iq_path or not os.path.exists(iq_path):
            QMessageBox.warning(self, "Error", "Please select a valid IQ file.")
            return

        # Clear old constellation buffers
        self.i_buffer.clear()
        self.q_buffer.clear()
        self.const_scatter.clear()
        self.modcod_label.setText("—")

        # Parse tuning configuration parameters
        try:
            fc_mhz = float(self.center_freq_edit.text().strip())
            rs_msps = float(self.symbol_rate_edit.text().strip())
            # Default sample rate from spin box (scale to Hz)
            samp_rate_hz = self.samp_rate_spin.value() * 1e6 
            beta = float(self.rolloff_combo.currentText())
        except ValueError:
            QMessageBox.warning(self, "Invalid Parameters", "Please check your tuning parameters.")
            return

        # 1. Create a Unix pipe specifically for the constellation data
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()

        # 2. Build the leandvb command line
        cmd = [
            './leandvb',
            '-v',
            '-d',
            '--f32',                    # 32-bit floating point complex input
            '-f', f"{int(samp_rate_hz)}",
            '--sr', f"{int(rs_msps * 1e6)}",
            '--derotate', f"{int(fc_mhz * 1e6)}",
            '--roll-off', f"{beta}",
            '--standard', 'DVB-S2',
            '--ldpc-helper', 'ldpc_tool', # Assumes ldpc_tool is in your system PATH
            '--hq',
            '--fd-const', f"{w_fd}",    # Instruct leandvb to write symbols to our pipe write-end
            '--fd-info', f"{info_w_fd}",
            '--json'                    # Tells leandvb to format outputs as easy-to-parse JSON
        ]
        
        #QTimer.singleShot(2000, lambda: print("leandvb alive:", self.leandvb_process.poll() is None))

        try:
            # 3. Open the file to pipe into stdin
            self.iq_file_stream = open(iq_path, 'rb')

            # 4. Spawn leandvb process
            # pass_fds keeps the specific write-end descriptor open in the child process
            import subprocess
            #self.leandvb_process = subprocess.Popen(cmd, stdin=self.iq_file_stream, pass_fds=[w_fd])
            
            
            self.leandvb_process = subprocess.Popen(
                cmd,
                stdin=self.iq_file_stream,
                stdout=subprocess.PIPE,   # TS stream, now consumed by mpv
                stderr=subprocess.PIPE,
                pass_fds=[w_fd, info_w_fd]
            )
            os.close(w_fd)
            os.close(info_w_fd)
    

            # 4b. Spawn mpv, embedded into video_widget via its X11 window id
            self.mpv_process = subprocess.Popen(
                [
                    'mpv', '-',
                    f'--wid={int(self.video_widget.winId())}',
                    '--no-terminal',
                    '--really-quiet',
                    '--demuxer-lavf-format=mpegts',
                ],
                stdin=self.leandvb_process.stdout,
            )
            # Hand the read end to mpv; without closing our copy, leandvb
            # never gets SIGPIPE/EPIPE when mpv exits or is killed.
            self.leandvb_process.stdout.close()
            


            # 5. Start the background thread reading from the read descriptor
            self.const_reader_thread = ConstellationReader(r_fd)
            self.const_reader_thread.new_points_signal.connect(self.update_constellation_plot)
            self.const_reader_thread.start()
            
            self.info_reader_thread = InfoReader(info_r_fd)
            self.info_reader_thread.new_line_signal.connect(self.update_info_line)
            self.info_reader_thread.start()
            
            self.stderr_reader_thread = StderrReader(self.leandvb_process.stderr)
            self.stderr_reader_thread.new_line_signal.connect(self.update_stderr_line)
            self.stderr_reader_thread.start()

        except Exception as e:
            QMessageBox.critical(self, "Process Error", f"Failed to start leandvb:\n{str(e)}")
            self.stop_decoding()

    def update_info_line(self, line):
        parts = line.split(maxsplit=1)
        if not parts:
            return
        keyword = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""

        if keyword == "FRAMELOCK":
            self.lock_label.setText("LOCKED" if value == "1" else "NOT LOCKED")
        elif keyword == "SS":
            self.ss_label.setText(value)
        elif keyword == "MER":
            self.mer_label.setText(value)
        elif keyword == "VBER":
            self.ber_label.setText(value)
        elif keyword == "FREQ":
            self.offset_label.setText(value)
        elif keyword == "MODCOD":
            self.modcod_label.setText(value)
        else:
            # Unmapped keyword - surface it instead of guessing/discarding
            self.unknown_info_lines.append(line)
            self.error_label.setText("\n".join(self.unknown_info_lines))
    
    def update_constellation_plot(self, points):
        #print(f"got {len(points)} points, e.g. {points[0]}")
        # Unpack the batch and append to our rolling FIFO queues
        for i, q in points:
            self.i_buffer.append(i)
            self.q_buffer.append(q)

        # Re-plot the rolling window
        #self.const_scatter.setData(x=list(self.i_buffer), y=list(self.q_buffer))
        #self.const_widget.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)

        
    def stop_decoding(self):
        # 1. Stop background thread
        if self.const_reader_thread:
            self.const_reader_thread.stop()
            self.const_reader_thread.wait()
            self.const_reader_thread = None

        # 2. Terminate leandvb
        if self.leandvb_process:
            self.leandvb_process.terminate()
            self.leandvb_process.wait()
            self.leandvb_process = None
        
        # 3. Terminate mpv
        if self.mpv_process:
            self.mpv_process.terminate()
            self.mpv_process.wait()
            self.mpv_process = None

        # 4. Close the file stream
        if hasattr(self, 'iq_file_stream') and self.iq_file_stream:
            self.iq_file_stream.close()
            self.iq_file_stream = None
            
        if self.info_reader_thread:
            self.info_reader_thread.stop()
            self.info_reader_thread.wait()
            self.info_reader_thread = None
            
        self.lock_label.setText("—")
        self.ss_label.setText("—")
        self.mer_label.setText("—")
        self.ber_label.setText("—")
        self.offset_label.setText("—")
            
        if self.stderr_reader_thread:
            self.stderr_reader_thread.stop()
            self.stderr_reader_thread.wait()
            self.stderr_reader_thread = None

    def update_tuning_bars(self):
        # Safety guard to prevent crash on startup when widgets trigger signals
        if not hasattr(self, 'tuning_region') or not hasattr(self, 'center_line'):
            return

        # 1. Safely parse Center Frequency
        try:
            text = self.center_freq_edit.text().strip()
            fc_mhz = float(text) if text else 0.0
        except ValueError:
            fc_mhz = 0.0  # Fallback if typing is in progress

        # 2. Safely parse Symbol Rate
        try:
            text = self.symbol_rate_edit.text().strip()
            rs_msps = float(text) if text else 1.0
        except ValueError:
            rs_msps = 1.0  # Fallback if typing is in progress

        # 3. Parse selected Roll-off dropdown value
        try:
            beta = float(self.rolloff_combo.currentText())
        except ValueError:
            beta = 0.25

        # 4. Scale inputs from MHz/MSps to Hz for the plot's X-axis
        fc_hz = fc_mhz * 1e6
        rs_hz = rs_msps * 1e6

        # 5. Occupied Bandwidth Calculation: BW = Rs * (1 + alpha)
        bw_hz = rs_hz * (1 + beta)

        # 6. Set band boundaries
        f_min = fc_hz - (bw_hz / 2.0)
        f_max = fc_hz + (bw_hz / 2.0)

        # 7. Update graph overlays
        self._updating_tuning_ui = True
        try:
            self.tuning_region.setRegion([f_min, f_max])
            self.center_line.setValue(fc_hz)
        finally:
            self._updating_tuning_ui = False
            
    def on_tuning_region_dragged(self):
        if self._updating_tuning_ui:
            return  # this change came from our own code, not a mouse drag

        f_min, f_max = self.tuning_region.getRegion()
        line0, line1 = self.tuning_region.lines
        edge_dragging = getattr(line0, 'moving', False) or getattr(line1, 'moving', False)

        try:
            beta = float(self.rolloff_combo.currentText())
        except ValueError:
            beta = 0.25

        if edge_dragging:
            # Red bar drag -> symbol rate. update_tuning_bars() will
            # re-symmetrize the region around the current center for us.
            rs_hz = (f_max - f_min) / (1 + beta)
            self.symbol_rate_edit.setText(f"{max(rs_hz / 1e6, 0.001):.3f}")
        else:
            # Box body drag -> offset. Width is already preserved natively.
            fc_hz = (f_min + f_max) / 2.0
            self.center_freq_edit.setText(f"{fc_hz / 1e6:.4f}")

    def start_autotune(self):
        self.estimate_symbol_rate_from_spectrum()  # get a Stage-1 starting point first

        iq_path = self.file_path_input.text()
        if not iq_path or not os.path.exists(iq_path):
            QMessageBox.warning(self, "Error", "Please select a valid IQ file.")
            return

        try:
            rs_estimate = float(self.symbol_rate_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Error", "Symbol rate estimate is invalid.")
            return

        samp_rate_hz = self.samp_rate_spin.value() * 1e6

        self.autotune_btn.setEnabled(False)
        self.autotune_status_label.setText("Starting auto-tune...")

        self.autotune_thread = SymbolRateSearchThread(iq_path, samp_rate_hz, rs_estimate)
        self.autotune_thread.progress_signal.connect(self.autotune_status_label.setText)
        self.autotune_thread.found_signal.connect(self.on_autotune_found)
        self.autotune_thread.failed_signal.connect(self.on_autotune_failed)
        self.autotune_thread.start()

    def on_autotune_found(self, rs_msps):
        self.symbol_rate_edit.setText(f"{rs_msps:.3f}")
        self.autotune_status_label.setText(f"Locked at {rs_msps:.3f} Msym/s")
        self.autotune_btn.setEnabled(True)

    def on_autotune_failed(self):
        self.autotune_status_label.setText("Auto-tune failed - no candidate locked.")
        self.autotune_btn.setEnabled(True)
            
    def estimate_symbol_rate_from_spectrum(self):
        if self._updating_tuning_ui:
            return

        iq_path = self.file_path_input.text()
        if not iq_path or not os.path.exists(iq_path):
            QMessageBox.warning(self, "Error", "Please select a valid IQ file first.")
            return

        sample_rate_hz = self.samp_rate_spin.value() * 1e6
        f_min, f_max = self.tuning_region.getRegion()
        fc_hz = (f_min + f_max) / 2.0
        rough_bw_hz = max(f_max - f_min, 500e3)  # only used to isolate from neighbors

        # 1. Read a large raw IQ chunk - needs many symbol periods for a clean line
        N = 1 << 20  # ~1M complex samples (~87ms at 12 MSPS)
        raw = np.fromfile(iq_path, dtype=np.complex64, count=N)
        if len(raw) < 4096:
            QMessageBox.warning(self, "Error", "Not enough samples read from file.")
            return

        # 2. Mix the carrier of interest down to baseband
        n = np.arange(len(raw))
        mixed = raw * np.exp(-1j * 2 * np.pi * fc_hz * n / sample_rate_hz)

        # 3. Rough brick-wall filter to isolate it from other carriers -
        #    generous width, doesn't need a precise drag
        X = np.fft.fft(mixed)
        freqs_full = np.fft.fftfreq(len(mixed), d=1.0 / sample_rate_hz)
        half_bw = max(rough_bw_hz * 1.5, 750e3)
        X[np.abs(freqs_full) > half_bw] = 0
        filtered = np.fft.ifft(X)

        # 4. Instantaneous power strips modulation, exposing the symbol-rate ripple
        power = np.abs(filtered) ** 2
        power = power - power.mean()

        # 5. FFT of the power waveform - the peak IS the symbol rate
        P = np.abs(np.fft.fft(power))
        freqs_p = np.fft.fftfreq(len(power), d=1.0 / sample_rate_hz)

        min_rs, max_rs = 100e3, min(sample_rate_hz / 2, 10e6)
        mask = (freqs_p > min_rs) & (freqs_p < max_rs)
        if not np.any(mask):
            return
        candidate_idx = np.where(mask)[0]
        peak_idx = candidate_idx[np.argmax(P[candidate_idx])]
        rs_hz = freqs_p[peak_idx]

        self._updating_tuning_ui = True
        try:
            self.symbol_rate_edit.setText(f"{max(rs_hz / 1e6, 0.001):.3f}")
        finally:
            self._updating_tuning_ui = False
        
    
    def update_stderr_line(self, line):
        MODCOD_TABLE = {
            1: "QPSK1/4", 2: "QPSK1/3", 3: "QPSK2/5", 4: "QPSK1/2", 5: "QPSK3/5",
            6: "QPSK2/3", 7: "QPSK3/4", 8: "QPSK4/5", 9: "QPSK5/6", 10: "QPSK8/9",
            11: "QPSK9/10", 12: "8PSK3/5", 13: "8PSK2/3", 14: "8PSK3/4", 15: "8PSK5/6",
            16: "8PSK8/9", 17: "8PSK9/10", 18: "16APSK2/3", 19: "16APSK3/4",
            20: "16APSK4/5", 21: "16APSK5/6", 22: "16APSK8/9", 23: "16APSK9/10",
            24: "32APSK3/4", 25: "32APSK4/5", 26: "32APSK5/6", 27: "32APSK8/9",
            28: "32APSK9/10",
        }
        m = re.match(r"Spawning LDPC helper: modcod=(\d+)", line)
        if m:
            modcod_num = int(m.group(1))
            self.modcod_label.setText(MODCOD_TABLE.get(modcod_num, f"Unknown MODCOD {modcod_num}"))
        else:
            self.unknown_info_lines.append(line)
            self.error_label.setText("\n".join(self.unknown_info_lines))
  #          self.error_label.verticalScrollBar().setValue(
   #             self.error_label.verticalScrollBar().maximum()
    #        )
        
    '''    
    def update_stderr_line(self, line):
        if line.startswith("Creating LUT for"):
            self.modcod_label.setText(line[len("Creating LUT for "):])
        else:
            self.unknown_info_lines.append(line)
            self.error_label.setText("\n".join(self.unknown_info_lines))
    '''
    
    def browse_file(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open IQ File", "")
        if filepath:
            self.file_path_input.setText(filepath)

    def toggle_plotting(self):
        if self.timer.isActive():
            # Stop the loop
            self.timer.stop()
            self.start_btn.setText("Start Plotting")
            self.start_btn.setStyleSheet("background-color: green; color: white;")
            if self.file_handle:
                self.file_handle.close()
                self.file_handle = None
        else:
            # Start the loop
            self.avg_psd = None
            filepath = self.file_path_input.text()
            if not filepath or not os.path.exists(filepath):
                QMessageBox.warning(self, "File Error", "Please select a valid IQ file first.")
                return

            try:
                self.file_handle = open(filepath, "rb")
                self.start_btn.setText("Stop Plotting")
                self.start_btn.setStyleSheet("background-color: red; color: white;")
                
                # Start timer (33ms interval ≈ 30 frames per second)
                self.timer.start(33)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open file: {str(e)}")

    def process_next_frame(self):
        if not self.file_handle:
            return

        # IQ format is complex float32: 2 floats per sample (I and Q)
        # We need to read (fft_size * 2) floats from the binary file
        bytes_to_read = self.fft_size * 2 * 4  # 4 bytes per float32
        chunk_bytes = self.file_handle.read(bytes_to_read)

        # Handle looping if we hit the End of File (EOF)
        if len(chunk_bytes) < bytes_to_read:
            self.file_handle.seek(0)
            chunk_bytes = self.file_handle.read(bytes_to_read)
            if not chunk_bytes:
                self.toggle_plotting() # Stop if file is completely empty
                return

        # Convert raw bytes to float32 NumPy array
        raw_floats = np.frombuffer(chunk_bytes, dtype=np.float32)
        
        # Interleave to build complex array
        i_data = raw_floats[0::2]
        q_data = raw_floats[1::2]
        
        # Safety catch if a truncated read occurred at the end of the file
        if len(i_data) < self.fft_size:
            return

        iq_complex = i_data + 1j * q_data

        # 1. Apply Hamming window to reduce spectral leakage
        windowed_iq = iq_complex * self.window

        # 2. Perform FFT
        fft_data = np.fft.fft(windowed_iq)
        
        # 3. Center DC
        fft_shifted = np.fft.fftshift(fft_data)
        
        # 4. Calculate power in dB scale
        magnitude_spectrum = np.abs(fft_shifted)
        psd_db = 20 * np.log10(magnitude_spectrum + 1e-12)
        
        if self.avg_psd is None:
            self.avg_psd = psd_db
        else:
            # First-order IIR / Leaky integrator math:
            self.avg_psd = (self.alpha * psd_db) + ((1.0 - self.alpha) * self.avg_psd)

        # 5. Generate frequency axis based on user input
        sample_rate_hz = self.samp_rate_spin.value() * 1e6
        freqs = np.fft.fftshift(np.fft.fftfreq(self.fft_size, d=1.0/sample_rate_hz))

        # 6. Push data straight to the PyQtGraph curve
        self.curve.setData(freqs, self.avg_psd)

    def closeEvent(self, event):
        self.timer.stop()
        self.stop_decoding() # Cleanup child processes & threads
        if self.file_handle:
            self.file_handle.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SpectrumAnalyzerGUI()
    window.show()
    sys.exit(app.exec())
