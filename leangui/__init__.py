# Import PySide6 before pyqtgraph anywhere in this package. The system also
# has PyQt6 installed; pyqtgraph auto-detects whichever Qt binding it finds
# first, and if that's PyQt6 it loads a different libQt6Core into the
# process than the one PySide6's wheel bundles, which crashes on import
# with an ABI mismatch. Importing PySide6.QtCore first pins the binding
# pyqtgraph will reuse, regardless of which submodule happens to import it.
import PySide6.QtCore  # noqa: F401
