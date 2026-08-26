"""Y.A.C.I.N.E — Yet Another Cognitive Intelligent Neural Engineer.

Local, privacy-first autonomous software-engineering agent.
"""
__version__ = "0.1.0"

from .core.config import get_settings
from .core.engine import AutonomousEngine

__all__ = ["get_settings", "AutonomousEngine", "__version__"]
