"""Pluggable decode chains.

Each chain is a DecodeChain subclass (see base.py) registered with the
@register_chain decorator (see registry.py). Importing a chain module here
is what makes it show up in the GUI's chain selector.

To add a new protocol later:
  1. Create leangui/chains/my_protocol_chain.py implementing DecodeChain
     (start/stop/is_running), emitting constellation_points / status_update
     / debug_line / error as it goes.
  2. Decorate the class with @register_chain.
  3. Import that module below.
"""
from . import leandvb_chain  # noqa: F401  (registers LeanDVBChain, LeanDVBSChain)
from . import leandvb_gse_chain  # noqa: F401  (registers LeanDVBGSEChain)
from . import leandvb_mpe_chain  # noqa: F401 (registers LeanDVBMPEChain)
