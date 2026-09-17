"""GSE (Generic Stream Encapsulation, ETSI TS 102 606-1) reassembly for
leandvb's raw --fd-gse dump, plus IP-flow classification/content-sniffing.

Ported from the standalone gse_decap_headerless.py / gse_receive.py scripts
at /home/eocs/SDR/mutahar_work/, adapted for streaming (a live pipe from a
running leandvb, not a finished-and-closed file) so the GUI can show flows
and their content as they arrive instead of only after the capture ends.
"""
