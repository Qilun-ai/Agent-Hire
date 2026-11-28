"""42 Gears careers flow (owned pi module; implementation re-exported from gears).

The canonical implementation lives in ``gears.py`` (registered as ``42gears``
in ``company_flows``). This module exists as the owned path for the
42 Gears review task and re-exports it unchanged so parsing behaviour is
identical whichever import path is used.
"""
from .gears import Gears42

__all__ = ["Gears42"]
