"""Demodulator status panel, fed by a decode chain's status_update/debug_line signals."""
from collections import deque

from PySide6.QtWidgets import QGroupBox, QFormLayout, QLabel


class DemodStatusPanel(QGroupBox):
    def __init__(self, debug_lines=6, parent=None):
        super().__init__("Demodulator Status", parent)
        layout = QFormLayout()

        self.lock_label = QLabel("—")
        self.ss_label = QLabel("—")
        self.mer_label = QLabel("—")
        self.ber_label = QLabel("—")
        self.offset_label = QLabel("—")
        self.modcod_label = QLabel("—")
        self.debug_label = QLabel("—")
        self.debug_label.setWordWrap(True)

        layout.addRow("Lock Status:", self.lock_label)
        layout.addRow("Signal Strength:", self.ss_label)
        layout.addRow("MER:", self.mer_label)
        layout.addRow("BER:", self.ber_label)
        layout.addRow("Freq Offset:", self.offset_label)
        layout.addRow("MODCOD:", self.modcod_label)
        layout.addRow("Debug Info:", self.debug_label)
        self.setLayout(layout)

        self._debug_lines = deque(maxlen=debug_lines)

    def apply_status(self, status: dict):
        if "lock" in status:
            self.lock_label.setText("LOCKED" if status["lock"] else "NOT LOCKED")
        if "SS" in status:
            self.ss_label.setText(status["SS"])
        if "MER" in status:
            self.mer_label.setText(status["MER"])
        if "BER" in status:
            self.ber_label.setText(status["BER"])
        if "offset" in status:
            self.offset_label.setText(status["offset"])
        if "modcod" in status:
            self.modcod_label.setText(status["modcod"])

    def append_debug(self, line: str):
        self._debug_lines.append(line)
        self.debug_label.setText("\n".join(self._debug_lines))

    def clear_modcod(self):
        self.modcod_label.setText("—")

    def reset(self):
        for lbl in (self.lock_label, self.ss_label, self.mer_label, self.ber_label, self.offset_label):
            lbl.setText("—")
