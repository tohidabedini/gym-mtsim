from enum import IntEnum
from datetime import datetime


class OrderType(IntEnum):
    Sell = 0
    Buy = 1

    @property
    def sign(self) -> float:
        return 1. if self == OrderType.Buy else -1.

    @property
    def opposite(self) -> 'OrderType':
        if self == OrderType.Sell:
            return OrderType.Buy
        return OrderType.Sell


class Order:

    def __init__(
        self,
        id: int, type: OrderType, symbol: str, volume: float, fee: float,
        entry_time: datetime, entry_price: float,
        exit_time: datetime, exit_price: float,
        fee_type: str="fixed", sl: float=None, tp:float=None, sl_tp_type: str=None,
        trailing_distance: str=None,
    ) -> None:
        # sl_tp_type: "percent", "pip"

        self.id = id
        self.type = type
        self.symbol = symbol
        self.volume = volume
        self.fee = fee
        self.fee_type = fee_type
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.profit = 0.
        self.gross_profit= 0.
        self.sl = sl
        self.tp = tp
        self.sl_tp_type = sl_tp_type
        self.margin = 0.
        self.closed = False
        self.initial_sl = sl
        self.trailing_distance = trailing_distance
