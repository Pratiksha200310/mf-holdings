"""Registry of AMC modules, in the order the brief lists them."""
from ..base import AMC
from .quant import Quant

REGISTRY: dict[str, type[AMC]] = {cls.key: cls for cls in [Quant]}
