"""Input-source picker: Recorded File vs Real-Time SDR."""
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QLabel
from PySide6.QtCore import Signal


class SourceSelector(QGroupBox):
    source_changed = Signal(str)  # "file" or "realtime"

    def __init__(self, parent=None):
        super().__init__("Input Source", parent)
        layout = QHBoxLayout()
        layout.addWidget(QLabel("Source:"))

        self.combo = QComboBox()
        self.combo.addItem("Recorded File", "file")
        self.combo.addItem("Real-Time Signal (SDR)", "realtime")
        self.combo.currentIndexChanged.connect(
            lambda _index: self.source_changed.emit(self.combo.currentData())
        )
        layout.addWidget(self.combo)
        layout.addStretch()
        self.setLayout(layout)

    def current_source(self):
        return self.combo.currentData()
