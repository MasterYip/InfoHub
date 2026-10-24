"""Pluggable info sources (Layer 1).

Each source is a module (or package) in this folder that defines:

    NAME: str                      # config key, e.g. "lark"
    DEFAULT_CONFIG: dict           # merged into cfg["sources"][NAME]
    POLICY_DEFAULTS: list          # optional, merged into cfg["policy_defaults"][NAME]
    def sync(cfg, store, overrides, record) -> None
        # fetch new messages into the store. Call record(step, stats, ids=(), started=None)
        # once per logical sub-step (e.g. one org, or calendar). `ids` are new message ids.
    def refresh_channels(cfg, store, record) -> None     # optional: re-read mute/metadata
    def doctor(cfg) -> dict                               # optional: health info
    def init(cfg, **kw) -> dict                           # optional: one-time setup (e.g. source-init)

The core never imports a specific source by name. Deleting a source's file/folder (and its
config block) removes it cleanly. Layers 2 and 3 are source-agnostic and need no changes.
"""
from __future__ import annotations

import importlib
import pkgutil


def discover() -> dict:
    """Return {NAME: module} for every importable source that defines NAME and sync()."""
    out = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:                      # a broken/removed source never breaks the rest
            continue
        if getattr(mod, "NAME", None) and hasattr(mod, "sync"):
            out[mod.NAME] = mod
    return out
