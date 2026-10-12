"""解析器子包。"""
from .base import BaseParser
from .taoguba import TaogubaParser
from .eastmoney import EastMoneyParser
from .xueqiu import XueqiuParser
from .ths import ThsParser
from .sina import SinaParser

__all__ = ["BaseParser", "TaogubaParser", "EastMoneyParser", "XueqiuParser", "ThsParser", "SinaParser"]