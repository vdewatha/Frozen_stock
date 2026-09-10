from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class StrategySignalResult:
    action: str
    probability_up: float
    probability_down: float
    confidence: float
    reason: str
    features: dict


class BaseStrategy(ABC):
    name: str
    slug: str

    def __init__(self, parameters: Optional[dict] = None) -> None:
        self.parameters = parameters or {}

    @abstractmethod
    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        raise NotImplementedError


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))
