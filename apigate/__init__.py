"""apigate - one OpenAI-compatible endpoint over pools of provider API keys."""
from .auth import Principal
from .config import Settings
from .core import Gateway, Result
from .errors import GateError, UpstreamError

__version__ = "0.1.0"
__all__ = ["Gateway", "Settings", "Principal", "Result", "GateError", "UpstreamError", "__version__"]
