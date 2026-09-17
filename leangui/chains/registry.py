"""Lookup table from display name -> DecodeChain subclass.

Chains register themselves with @register_chain at import time (see
chains/__init__.py for where those imports happen).
"""

_REGISTRY = {}


def register_chain(chain_cls):
    _REGISTRY[chain_cls.display_name] = chain_cls
    return chain_cls


def available_chains():
    return list(_REGISTRY.keys())


def create_chain(display_name, **kwargs):
    try:
        chain_cls = _REGISTRY[display_name]
    except KeyError:
        raise ValueError(f"Unknown decode chain: {display_name!r}")
    return chain_cls(**kwargs)
