"""BladeRF configuration panel, shown when Input Source = Real-Time Signal (SDR)."""
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                                QHBoxLayout, QLabel, QLineEdit, QWidget)

from ..sdr.bladerf_source import BladeRFConfig


class SdrConfigPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Real-Time Signal Input (BladeRF)", parent)
        layout = QFormLayout()

        # RF center frequency (actual hardware tuning frequency)
        self.freq_edit = QLineEdit()
        layout.addRow("RF Center Frequency (MHz):", self.freq_edit)

        # RX bandwidth
        self.bandwidth_edit = QLineEdit()
        layout.addRow("RX Bandwidth (MHz):", self.bandwidth_edit)

        # Gain mode
        gain_row = QHBoxLayout()
        self.gain_mode_combo = QComboBox()
        self.gain_mode_combo.addItem("AGC (Automatic)", "agc")
        self.gain_mode_combo.addItem("Manual", "manual")
        self.gain_mode_combo.currentIndexChanged.connect(self._on_gain_mode_changed)
        gain_row.addWidget(self.gain_mode_combo)

        self.manual_gain_spin = QDoubleSpinBox()
        self.manual_gain_spin.setRange(0, 70)
        self.manual_gain_spin.setSuffix(" dB")
        # No true "empty" state on a spin box - show blank at the minimum
        # instead of a real value like 40, forcing the user to type one in.
        self.manual_gain_spin.setSpecialValueText(" ")
        self.manual_gain_spin.setValue(0)
        self.manual_gain_spin.setEnabled(False)
        gain_row.addWidget(self.manual_gain_spin)

        gain_row_widget = QWidget()
        gain_row_widget.setLayout(gain_row)
        layout.addRow("RX Gain:", gain_row_widget)

        self.status_label = QLabel("Idle.")
        self.status_label.setStyleSheet("color: orange;")
        layout.addRow("Status:", self.status_label)

        self.setLayout(layout)

    def _on_gain_mode_changed(self, _index):
        mode = self.gain_mode_combo.currentData()
        self.manual_gain_spin.setEnabled(mode == "manual")

    def set_status(self, text, color):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def build_config(self, sample_rate_hz) -> BladeRFConfig:
        """Raises ValueError if a numeric field can't be parsed."""
        return BladeRFConfig(
            rf_freq_hz=float(self.freq_edit.text().strip()) * 1e6,
            sample_rate_hz=sample_rate_hz,
            bandwidth_hz=float(self.bandwidth_edit.text().strip()) * 1e6,
            gain_mode=self.gain_mode_combo.currentData(),
            manual_gain_db=int(self.manual_gain_spin.value()),
        )
