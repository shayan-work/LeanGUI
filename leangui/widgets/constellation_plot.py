"""Constellation scatter plot fed by a decode chain's constellation_points signal."""
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QTimer


class ConstellationPlotWidget(pg.PlotWidget):
    def __init__(self, history_size=1000, redraw_interval_ms=33, parent=None):
        super().__init__(parent)
        self.setBackground('k')
        self.showGrid(x=True, y=True, alpha=0.3)
        self.setLabel('left', 'Quadrature (Q)')
        self.setLabel('bottom', 'In-Phase (I)')
        self.setTitle('Synchronized Symbols')

        # Keep the grid square so circles/constellations aren't skewed
        self.setAspectLocked(True)
        self.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)

        self.i_buffer = deque(maxlen=history_size)
        self.q_buffer = deque(maxlen=history_size)

        # Use ScatterPlotItem for high speed
        self.scatter = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(0, 255, 100, 120))
        self.addItem(self.scatter)

        # Redraw decoupled from point arrival so a fast source doesn't queue
        # up excessive redraws.
        self._redraw_timer = QTimer()
        self._redraw_timer.timeout.connect(self._redraw)
        self._redraw_timer.start(redraw_interval_ms)

    def add_points(self, points):
        for i, q in points:
            self.i_buffer.append(i)
            self.q_buffer.append(q)

    def _redraw(self):
        self.scatter.setData(x=list(self.i_buffer), y=list(self.q_buffer))

    def clear_points(self):
        self.i_buffer.clear()
        self.q_buffer.clear()
        self.scatter.clear()
