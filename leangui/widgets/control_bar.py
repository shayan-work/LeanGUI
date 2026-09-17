"""Signal-parameters control bar: tuning fields, chain selector, decode button.

File/SDR source selection lives in their own panels (see source_selector.py,
file_input_panel.py, sdr_panel.py) since those differ by input source; this
bar is shared by every source.
"""
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QWidget, QLineEdit
from PySide6.QtCore import Signal


class ControlBar(QWidget):
    decode_toggle_requested = Signal()
    tuning_changed = Signal()

    def __init__(self, chain_names, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Sample Rate (MHz):"))
        self.samp_rate_spin = QDoubleSpinBox()
        self.samp_rate_spin.setRange(0.0, 1000.0)
        self.samp_rate_spin.setDecimals(3)
        # QDoubleSpinBox always shows a number - there's no true "empty" - so
        # show blank at the minimum instead of a real value like 12.000,
        # forcing the user to actually type one in.
        self.samp_rate_spin.setSpecialValueText(" ")
        self.samp_rate_spin.setValue(0.0)
        layout.addWidget(self.samp_rate_spin)

        layout.addWidget(QLabel("Offset (MHz):"))
        self.center_freq_edit = QLineEdit()
        self.center_freq_edit.setFixedWidth(80)
        layout.addWidget(self.center_freq_edit)

        layout.addWidget(QLabel("Symbol Rate (MSps):"))
        self.symbol_rate_edit = QLineEdit()
        self.symbol_rate_edit.setFixedWidth(80)
        layout.addWidget(self.symbol_rate_edit)

        layout.addWidget(QLabel("Roll-off (α):"))
        self.rolloff_combo = QComboBox()
        self.rolloff_combo.addItems(["", "0.35", "0.25", "0.20"])
        self.rolloff_combo.setCurrentIndex(0)
        layout.addWidget(self.rolloff_combo)

        layout.addWidget(QLabel("Chain:"))
        self.chain_combo = QComboBox()
        self.chain_combo.addItem("None", None)
        for name in chain_names:
            self.chain_combo.addItem(name, name)
        layout.addWidget(self.chain_combo)

        self.decode_btn = QPushButton("Start Decoding")
        self.decode_btn.setStyleSheet("background-color: blue; color: white;")
        self.decode_btn.clicked.connect(self.decode_toggle_requested)
        layout.addWidget(self.decode_btn)

        self.samp_rate_spin.valueChanged.connect(self.tuning_changed)
        self.center_freq_edit.textChanged.connect(self.tuning_changed)
        self.symbol_rate_edit.textChanged.connect(self.tuning_changed)
        self.rolloff_combo.currentIndexChanged.connect(self.tuning_changed)

    # --- accessors ---------------------------------------------------
    def sample_rate_hz(self):
        return self.samp_rate_spin.value() * 1e6

    def offset_text(self):
        return self.center_freq_edit.text().strip()

    def symbol_rate_text(self):
        return self.symbol_rate_edit.text().strip()

    def rolloff(self):
        return float(self.rolloff_combo.currentText())

    def selected_chain(self):
        """Returns the chain's display name, or None if "None" is selected."""
        return self.chain_combo.currentData()

    def set_decoding_active(self, active):
        self.decode_btn.setText("Stop Decoding" if active else "Start Decoding")
        color = "red" if active else "blue"
        self.decode_btn.setStyleSheet(f"background-color: {color}; color: white;")

    def set_symbol_rate_text(self, msps_text):
        self.symbol_rate_edit.setText(msps_text)

    def set_offset_text(self, mhz_text):
        self.center_freq_edit.setText(mhz_text)
