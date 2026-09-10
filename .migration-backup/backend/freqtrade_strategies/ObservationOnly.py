"""Integration probe only: intentionally generates no entries or exits."""
from freqtrade.strategy import IStrategy


class ObservationOnly(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 10.0}
    stoploss = -0.02
    startup_candle_count = 1

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        return dataframe
