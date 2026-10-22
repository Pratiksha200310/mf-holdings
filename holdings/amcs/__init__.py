"""Registry of AMC modules, in the order the brief lists them."""
from ..base import AMC
from .quant import Quant
from .sundaram import Sundaram
from .hdfc import HDFC
from .mahindra import MahindraManulife
from .jioblackrock import JioBlackRock
from .bandhan import Bandhan

REGISTRY: dict[str, type[AMC]] = {cls.key: cls for cls in [Quant, Sundaram, HDFC, MahindraManulife, JioBlackRock, Bandhan]}
