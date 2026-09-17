import os

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QMainWindow, QMessageBox, QVBoxLayout, QWidget

from .chains.base import IQSource, TuningParams
from .chains.leandvb_gse_chain import LeanDVBGSEChain
from .chains.registry import available_chains, create_chain
from .dsp.spectrum import (
    EmptyFileError,
    InsufficientSamplesError,
    compute_power_spectrum,
    estimate_snr_db,
    estimate_symbol_rate_hz,
    freq_axis,
    power_to_db,
    read_iq_chunk,
)
from .sdr.bladerf_source import BladeRFSource
from .widgets.constellation_plot import ConstellationPlotWidget
from .widgets.control_bar import ControlBar
from .widgets.file_input_panel import FileInputPanel
from .widgets.gse_text_panel import GseTextPanel
from .widgets.info_panel import DemodStatusPanel
from .widgets.sdr_panel import SdrConfigPanel
from .widgets.source_selector import SourceSelector
from .widgets.spectrum_plot import SpectrumPlotWidget
from .widgets.video_panel import VideoPanelWidget

DEFAULT_IQ_FILE = ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LeanGUI : GUI Wrapper for leandvb")
        self.resize(900, 600)

        # DSP parameters for the spectrum preview (shared by both sources)
        self.fft_size = 4096
        self.window = np.hamming(self.fft_size)
        self.file_handle = None
        self.avg_power = None  # running average in linear power, not dB
        self.avg_psd = None  # dB version of avg_power, for display/SNR
        self.alpha = 0.3

        # "file" = recorded-file source, "realtime" = live SDR source
        self.current_source = "file"

        # Active decode chain / SDR source (None when stopped)
        self.active_chain = None
        self.sdr_source = None
        self._updating_tuning_ui = False
        # Latest chunk handed off by the SDR fan-out thread, redrawn at a
        # fixed cadence by spectrum_timer instead of on every arrival - see
        # _on_realtime_chunk.
        self._latest_realtime_chunk = None

        self._init_ui()

        self.spectrum_timer = QTimer()
        self.spectrum_timer.timeout.connect(self._process_next_frame)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.source_selector = SourceSelector()
        main_layout.addWidget(self.source_selector)

        self.file_panel = FileInputPanel(default_file=DEFAULT_IQ_FILE)
        main_layout.addWidget(self.file_panel)

        self.sdr_panel = SdrConfigPanel()
        self.sdr_panel.setVisible(False)
        main_layout.addWidget(self.sdr_panel)

        self.control_bar = ControlBar(available_chains())
        main_layout.addWidget(self.control_bar)

        self.spectrum_plot = SpectrumPlotWidget()
        self.constellation_plot = ConstellationPlotWidget()
        self.info_panel = DemodStatusPanel()
        self.video_panel = VideoPanelWidget()

        # Text content recovered from a GSE/IP flow (leandvb (DVB-S2, GSE/IP
        # data) chain only) goes here, to the left of the video panel; stays
        # hidden until a text flow actually shows up.
        self.gse_text_panel = GseTextPanel()
        self.gse_text_panel.setVisible(False)
        video_row = QWidget()
        video_row_layout = QHBoxLayout(video_row)
        video_row_layout.setContentsMargins(0, 0, 0, 0)
        video_row_layout.addWidget(self.gse_text_panel, 1)
        video_row_layout.addWidget(self.video_panel, 2)

        # 2x2 grid: spectrum | constellation / info | (gse text + video)
        content_grid = QGridLayout()
        content_grid.addWidget(self.spectrum_plot, 0, 0)
        content_grid.addWidget(self.constellation_plot, 0, 1)
        content_grid.addWidget(self.info_panel, 1, 0)
        content_grid.addWidget(video_row, 1, 1)
        content_grid.setRowStretch(0, 1)
        content_grid.setRowStretch(1, 1)
        content_grid.setColumnStretch(0, 1)
        content_grid.setColumnStretch(1, 1)
        main_layout.addLayout(content_grid)

        self.source_selector.source_changed.connect(self.on_source_changed)
        self.file_panel.file_selected.connect(self.start_plotting)
        self.control_bar.decode_toggle_requested.connect(self.toggle_decoding)
        self.control_bar.tuning_changed.connect(self.update_tuning_bars)

        self.spectrum_plot.region_changed.connect(self.on_tuning_region_dragged)
        self.spectrum_plot.region_change_finished.connect(self.estimate_symbol_rate_from_spectrum)

        # Run an initial update calculation to draw default positions on open
        self.update_tuning_bars()

    # --- input source ---------------------------------------------------
    def on_source_changed(self, source):
        # Stop anything currently running whenever the source changes.
        self.stop_plotting()
        self.stop_decoding()

        # Clear the spectrum plot / rolling average so switching sources
        # never leaves a stale trace on screen.
        self.avg_power = None
        self.avg_psd = None
        self.spectrum_plot.update_curve([], [])
        self.spectrum_plot.set_snr_text("SNR: -- dB")

        self.current_source = source
        self.file_panel.setVisible(source == "file")
        self.sdr_panel.setVisible(source == "realtime")
        if source == "realtime":
            self.sdr_panel.set_status("Idle.", "orange")

    # --- decode chain lifecycle ---------------------------------------
    def toggle_decoding(self):
        if self.active_chain is not None:
            self.stop_decoding()
        else:
            self.start_decoding()

    def start_decoding(self):
        if self.current_source == "file":
            self._start_decoding_file()
        elif self.current_source == "realtime":
            self._start_decoding_realtime()

    def _build_tuning(self):
        """Raises ValueError if a field can't be parsed."""
        return TuningParams(
            sample_rate_hz=self.control_bar.sample_rate_hz(),
            offset_hz=float(self.control_bar.offset_text()) * 1e6,
            symbol_rate_hz=float(self.control_bar.symbol_rate_text()) * 1e6,
            rolloff=self.control_bar.rolloff(),
        )

    def _launch_chain(self, source: IQSource, tuning: TuningParams):
        chain_name = self.control_bar.selected_chain()
        if chain_name is None:
            QMessageBox.warning(self, "No Chain Selected", "Please select a decode chain.")
            return None
        chain = create_chain(chain_name, video_widget=self.video_panel)
        chain.constellation_points.connect(self.constellation_plot.add_points)
        chain.status_update.connect(self.info_panel.apply_status)
        chain.debug_line.connect(self.info_panel.append_debug)
        chain.error.connect(self._on_chain_error)
        chain.encryption_detected.connect(self.video_panel.set_encrypted)

        self.gse_text_panel.clear()
        self.gse_text_panel.setVisible(False)
        # Duck-typed on the signal's presence rather than isinstance(chain,
        # LeanDVBGSEChain): LeanDVBMPEChain also exposes gse_text_chunk for
        # its own extracted IP text, without inheriting from the GSE chain.
        if hasattr(chain, "gse_text_chunk"):
            chain.gse_text_chunk.connect(self._on_gse_text_chunk)
        if isinstance(chain, LeanDVBGSEChain):
            chain.gse_flow_update.connect(self.gse_text_panel.update_flow_summary)

        chain.start(source, tuning)
        if chain.is_running():
            self.active_chain = chain
            self.control_bar.set_decoding_active(True)
        return chain

    def _on_gse_text_chunk(self, flow_key, text):
        # Only shown once a GSE flow actually turns out to be text - most
        # GSE traffic is video/binary, which stays in the video panel / is
        # left out of the UI entirely.
        self.gse_text_panel.setVisible(True)
        self.gse_text_panel.append_text(flow_key, text)

    def _start_decoding_file(self):
        self.stop_decoding()  # prevent double-running

        iq_path = self.file_panel.file_path()
        if not iq_path or not os.path.exists(iq_path):
            QMessageBox.warning(self, "Error", "Please select a valid IQ file.")
            return

        self.constellation_plot.clear_points()
        self.info_panel.clear_modcod()
        self.video_panel.set_encrypted(False)

        try:
            tuning = self._build_tuning()
        except ValueError:
            QMessageBox.warning(self, "Invalid Parameters", "Please check your tuning parameters.")
            return

        self._launch_chain(IQSource(kind="file", path=iq_path, sample_format="f32"), tuning)

    def _start_decoding_realtime(self):
        self.stop_decoding()  # prevent double-running

        self.constellation_plot.clear_points()
        self.info_panel.clear_modcod()
        self.video_panel.set_encrypted(False)

        try:
            tuning = self._build_tuning()
            sdr_config = self.sdr_panel.build_config(tuning.sample_rate_hz)
        except ValueError:
            QMessageBox.warning(self, "Invalid Parameters", "Please check your tuning / BladeRF parameters.")
            return

        self.avg_power = None
        self.avg_psd = None
        self._latest_realtime_chunk = None

        sdr_source = BladeRFSource(self.fft_size)
        sdr_source.spectrum_chunk.connect(self._on_realtime_chunk)
        sdr_source.status_changed.connect(self.sdr_panel.set_status)
        sdr_source.error.connect(self._on_chain_error)
        sdr_source.start(sdr_config)

        if not sdr_source.is_running():
            return
        self.sdr_source = sdr_source
        self.spectrum_timer.start(33)  # ~30 frames per second, see _on_realtime_chunk

        source = IQSource(kind="fifo", path=sdr_source.lean_fifo_path, sample_format=sdr_source.sample_format)
        chain = self._launch_chain(source, tuning)
        if chain is None or not chain.is_running():
            self.sdr_source.stop()
            self.sdr_source = None
            self.spectrum_timer.stop()

    def _on_chain_error(self, message):
        QMessageBox.critical(self, "Process Error", message)

    def stop_decoding(self):
        if self.active_chain:
            self.active_chain.stop()
            self.active_chain = None
        if self.sdr_source:
            self.sdr_source.stop()
            self.sdr_source = None
        if self.current_source == "realtime":
            self.spectrum_timer.stop()
            self._latest_realtime_chunk = None
        self.info_panel.reset()
        self.control_bar.set_decoding_active(False)

    # --- spectrum preview (recorded file + real-time SDR) ---------------
    def start_plotting(self, filepath):
        # This starts spectrum_timer for the recorded-file source; the
        # real-time source instead starts it from _start_decoding_realtime
        # once the SDR is streaming (see _on_realtime_chunk).
        if self.current_source != "file":
            return

        if not filepath or not os.path.exists(filepath):
            QMessageBox.warning(self, "File Error", "Please select a valid IQ file first.")
            return

        self.stop_plotting()

        self.avg_power = None
        self.avg_psd = None
        try:
            self.file_handle = open(filepath, "rb")
            self.spectrum_timer.start(33)  # ~30 frames per second
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open file: {str(e)}")

    def stop_plotting(self):
        self.spectrum_timer.stop()
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None

    def _process_next_frame(self):
        if self.current_source == "file":
            if not self.file_handle:
                return

            try:
                iq_complex = read_iq_chunk(self.file_handle, self.fft_size)
            except EmptyFileError:
                self.stop_plotting()
                return
            if iq_complex is None:
                return  # truncated read at EOF boundary; try again next tick

            self._update_spectrum_from_iq(iq_complex)

        elif self.current_source == "realtime":
            iq_complex = self._latest_realtime_chunk
            if iq_complex is None:
                return  # fan-out thread hasn't handed off a chunk since our last tick
            self._latest_realtime_chunk = None
            self._update_spectrum_from_iq(iq_complex)

    def _on_realtime_chunk(self, iq_complex):
        # The fan-out thread (see BladeRFSource) reads samples as fast as
        # bladeRF-cli produces them - far faster than the GUI could ever
        # usefully redraw. Just stash the latest chunk here; spectrum_timer
        # (33ms, ~30fps) is what actually drives _update_spectrum_from_iq,
        # the same decoupling ConstellationPlotWidget already does for
        # points vs. redraws. Redrawing directly from every emission here
        # instead used to flood the GUI thread and crash the app.
        if self.current_source != "realtime":
            return  # stale signal from a source switch or shutdown
        self._latest_realtime_chunk = iq_complex

    def _update_spectrum_from_iq(self, iq_complex):
        if len(iq_complex) < self.fft_size:
            return
        if self.control_bar.sample_rate_hz() <= 0:
            return  # sample rate field is blank/0 until the user fills it in - freq_axis() divides by it

        power_spectrum = compute_power_spectrum(iq_complex[:self.fft_size], self.window)
        if self.avg_power is None:
            self.avg_power = power_spectrum
        else:
            # First-order IIR / leaky integrator, averaged in linear power.
            # Averaging dB values directly would be biased low (log is
            # concave), understating both peaks and the noise floor.
            self.avg_power = (self.alpha * power_spectrum) + ((1.0 - self.alpha) * self.avg_power)
        self.avg_psd = power_to_db(self.avg_power)

        sample_rate_hz = self.control_bar.sample_rate_hz()
        freqs = freq_axis(self.fft_size, sample_rate_hz)
        self.spectrum_plot.update_curve(freqs, self.avg_psd)
        self._update_snr_display()

    def _update_snr_display(self):
        if self.avg_psd is None:
            return

        sample_rate_hz = self.control_bar.sample_rate_hz()
        if sample_rate_hz <= 0:
            return
        freqs = freq_axis(self.fft_size, sample_rate_hz)
        f_min, f_max = self.spectrum_plot.get_region()
        snr_db = estimate_snr_db(self.avg_psd, freqs, f_min, f_max)
        if snr_db is None:
            self.spectrum_plot.set_snr_text("SNR: -- dB")
            return

        self.spectrum_plot.set_snr_text(f"SNR: {snr_db:.1f} dB")
        self.spectrum_plot.position_snr_text()

    # --- tuning overlay ---------------------------------------------
    def update_tuning_bars(self):
        if not hasattr(self, 'spectrum_plot'):
            return  # widgets triggered signals before init finished

        try:
            text = self.control_bar.offset_text()
            fc_mhz = float(text) if text else 0.0
        except ValueError:
            fc_mhz = 0.0  # Fallback if typing is in progress

        try:
            text = self.control_bar.symbol_rate_text()
            rs_msps = float(text) if text else 1.0
        except ValueError:
            rs_msps = 1.0  # Fallback if typing is in progress

        try:
            beta = self.control_bar.rolloff()
        except ValueError:
            beta = 0.25

        fc_hz = fc_mhz * 1e6
        rs_hz = rs_msps * 1e6

        # Occupied Bandwidth: BW = Rs * (1 + alpha)
        bw_hz = rs_hz * (1 + beta)
        f_min = fc_hz - (bw_hz / 2.0)
        f_max = fc_hz + (bw_hz / 2.0)

        self._updating_tuning_ui = True
        try:
            self.spectrum_plot.set_region(f_min, f_max)
            self.spectrum_plot.set_center(fc_hz)
            # The overall view stays put (always the full captured band,
            # centered at 0) - only the region-of-interest box/line above
            # slides to fc_hz. Re-applied here only so it stays correct
            # if the sample rate changes.
            self.spectrum_plot.center_view(0, self.control_bar.sample_rate_hz())
        finally:
            self._updating_tuning_ui = False

    def on_tuning_region_dragged(self):
        if self._updating_tuning_ui:
            return  # this change came from our own code, not a mouse drag

        f_min, f_max = self.spectrum_plot.get_region()
        try:
            beta = self.control_bar.rolloff()
        except ValueError:
            beta = 0.25

        if self.spectrum_plot.edge_dragging():
            # Red bar drag -> symbol rate. update_tuning_bars() will
            # re-symmetrize the region around the current center for us.
            rs_hz = (f_max - f_min) / (1 + beta)
            self.control_bar.set_symbol_rate_text(f"{max(rs_hz / 1e6, 0.001):.3f}")
        else:
            # Box body drag -> offset. Width is already preserved natively.
            fc_hz = (f_min + f_max) / 2.0
            self.control_bar.set_offset_text(f"{fc_hz / 1e6:.4f}")

    def estimate_symbol_rate_from_spectrum(self):
        if self._updating_tuning_ui:
            return
        if self.current_source != "file":
            return  # only a recorded file can be re-read from disk to analyze
        iq_path = self.file_panel.file_path()
        if not iq_path or not os.path.exists(iq_path):
            QMessageBox.warning(self, "Error", "Please select a valid IQ file first.")
            return

        sample_rate_hz = self.control_bar.sample_rate_hz()
        if sample_rate_hz <= 0:
            return  # sample rate field is blank/0 until the user fills it in
        f_min, f_max = self.spectrum_plot.get_region()
        fc_hz = (f_min + f_max) / 2.0
        rough_bw_hz = max(f_max - f_min, 500e3)  # only used to isolate from neighbors

        try:
            rs_hz = estimate_symbol_rate_hz(iq_path, sample_rate_hz, fc_hz, rough_bw_hz)
        except InsufficientSamplesError:
            QMessageBox.warning(self, "Error", "Not enough samples read from file.")
            return
        if rs_hz is None:
            return

        self._updating_tuning_ui = True
        try:
            self.control_bar.set_symbol_rate_text(f"{max(rs_hz / 1e6, 0.001):.3f}")
        finally:
            self._updating_tuning_ui = False

    def closeEvent(self, event):
        self.stop_plotting()
        self.stop_decoding()  # Cleanup child processes & threads
        event.accept()
