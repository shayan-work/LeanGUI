"""Recorded-file IQ input panel, shown when Input Source = Recorded File."""
from PySide6.QtWidgets import QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PySide6.QtCore import Signal


class FileInputPanel(QGroupBox):
    file_selected = Signal(str)

    def __init__(self, default_file="", parent=None):
        super().__init__("Recorded File Input", parent)
        layout = QHBoxLayout()

        layout.addWidget(QLabel("IQ File (.iq / .raw):"))
        self.file_path_input = QLineEdit()
        self.file_path_input.setPlaceholderText("Select a 32-bit float complex IQ file...")
        self.file_path_input.setText(default_file)
        self.file_path_input.editingFinished.connect(
            lambda: self.file_selected.emit(self.file_path_input.text())
        )
        layout.addWidget(self.file_path_input)

        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse)
        layout.addWidget(self.browse_btn)

        self.setLayout(layout)

    def _browse(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open IQ File", "")
        if filepath:
            self.file_path_input.setText(filepath)
            self.file_selected.emit(filepath)

    def file_path(self):
        return self.file_path_input.text()
