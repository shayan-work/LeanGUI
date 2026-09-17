"""SDR (software-defined radio) integration.

Today: a single live source, BladeRF via bladeRF-cli (see bladerf_source.py).
A source is independent of any decode chain - it just produces raw IQ
samples (for the spectrum display) and writes the same samples into a FIFO
that a DecodeChain can be pointed at via chains.base.IQSource(kind="fifo").

To add another SDR later: implement a class with the same shape as
BladeRFSource (start(config)/stop()/is_running(), plus spectrum_chunk /
status_changed / error signals and a lean_fifo_path + sample_format), and
give it its own config panel in widgets/.
"""
