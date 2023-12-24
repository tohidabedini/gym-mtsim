from typing import List, Tuple, Dict, Any, Optional, Union, Callable

import copy
from datetime import datetime
from pathos.multiprocessing import ProcessingPool as Pool

import numpy as np
from scipy.special import expit
import pandas as pd

import matplotlib.pyplot as plt
import matplotlib.cm as plt_cm
import matplotlib.colors as plt_colors
import plotly.graph_objects as go

import gymnasium as gym
from gymnasium import spaces
from gymnasium.utils import seeding

from ..simulator import MtSimulator, OrderType


class MtEnv(gym.Env):

    metadata = {'render_modes': ['human', 'simple_figure', 'advanced_figure']}

    def __init__(
            self, original_simulator: MtSimulator, trading_symbols: List[str],
            window_size: int, time_points: Optional[List[datetime]]=None,
            hold_threshold: float=0.5, close_threshold: float=0.5,
            fee: Union[float, Callable[[str], float]]=0.0005,
            fee_type:str = "fixed",
            sl_tp_type:str = None,
            sl: float=None,
            tp:float=None,
            symbol_max_orders: int=1, multiprocessing_processes: Optional[int]=None,
            render_mode:str =None,
            observation_mode: int=0,
            normalize_observation: bool=True,
            orders_observation_detail_count: int=2,
            action_dtype=np.float64,
            observation_dtype=np.float32,
            action_mode: int=0,
            discrete_actions_count: int=3, # should be odd number: 1, 3, 5, ...
            balance_or_free_margin_for_volume_computation:bool=True,
            ohlc_count_in_symbols_data: int=4,
            sl_tp_log:bool=False,
            trailing_distance:int=None,

    ) -> None:


        # validations
        assert len(original_simulator.symbols_data) > 0, "no data available"
        assert len(original_simulator.symbols_info) > 0, "no data available"
        assert len(trading_symbols) > 0, "no trading symbols provided"
        assert 0. <= hold_threshold <= 1., "'hold_threshold' must be in range [0., 1.]"

        if not original_simulator.hedge:
            symbol_max_orders = 1

        for symbol in trading_symbols:
            assert symbol in original_simulator.symbols_info, f"symbol '{symbol}' not found"
            currency_profit = original_simulator.symbols_info[symbol].currency_profit
            assert original_simulator._get_unit_symbol_info(currency_profit) is not None, \
                   f"unit symbol for '{currency_profit}' not found"

        if time_points is None:
            time_points = original_simulator.symbols_data[trading_symbols[0]].index.to_pydatetime().tolist()
        assert len(time_points) > window_size, "not enough time points provided"

        # attributes
        # self.seed()
        assert render_mode is None or render_mode in self.metadata["render_modes"]
        self.trailing_distance = trailing_distance
        self.ohlc_count_in_symbols_data = ohlc_count_in_symbols_data
        self.sl_tp_log = sl_tp_log
        self.action_dtype = action_dtype
        self.observation_dtype = observation_dtype
        self.render_mode = render_mode
        self.observation_mode = observation_mode
        self.action_mode = action_mode
        self.discrete_actions_count = discrete_actions_count
        self.actions_fuzzy_term = self.fuzzy_terms_generator(discrete_actions_count)
        self.balance_or_free_margin_for_volume_computation = balance_or_free_margin_for_volume_computation


        self.original_simulator = original_simulator
        self.trading_symbols = trading_symbols
        self.window_size = window_size
        self.time_points = time_points
        self.hold_threshold = hold_threshold
        self.close_threshold = close_threshold
        self.fee = fee
        self.fee_type = fee_type
        self.sl_tp_type = sl_tp_type
        self.sl = sl
        self.tp = tp
        self.normalize_observation = normalize_observation
        self.symbol_max_orders = symbol_max_orders
        self.multiprocessing_pool = Pool(multiprocessing_processes) if multiprocessing_processes else None

        self.prices = self._get_prices()
        self.signal_features = self._process_data()
        self.features_shape = (self.window_size, self.signal_features.shape[1])
        self.orders_observation_detail_count=orders_observation_detail_count
        self.orders_shape = (len(self.trading_symbols), self.symbol_max_orders, self.orders_observation_detail_count)
        self.flattened_balance_equity_margin_orders_shape = (self.window_size, np.prod(self.orders_shape) + 4)

        # episode
        self._start_tick = self.window_size - 1
        self._end_tick = len(self.time_points) - 1
        self._done: bool = NotImplemented
        self._current_tick: int = NotImplemented
        self.simulator: MtSimulator = NotImplemented
        self.history: List[Dict[str, Any]] = NotImplemented

        # spaces
        self.action_space = self._get_action_space()
        self.observation_space = self._get_observation_space()
        self.orders_balance_equity_margin_array = np.zeros(self.flattened_balance_equity_margin_orders_shape)

    # def seed(self, seed: Optional[int]=None) -> List[int]:
    #     self.np_random, seed = seeding.np_random(seed)
    #     return [seed]

    @staticmethod
    def fuzzy_terms_generator(x):
        if x % 2 == 0:
            raise ValueError("Input must be an odd integer.")

        if x == 1:
            return [0]

        step = 2 / (x - 1)
        return [i * step - 1 for i in range(x)]

    def _update_price_with_fee(self, net_price, symbol):
        fee = self.fee if type(self.fee) is float else self.fee(symbol)
        if self.fee_type=="fixed":
            net_price += fee
        elif self.fee_type=="floating":
            net_price *= (1 + fee)
        return net_price

    def _get_volume_for_discrete_action(self, symbol, action_fuzzy_value):
        entry_time = self.simulator.current_time
        entry_price = self.simulator.price_at(symbol, entry_time)['Close']
        free_margin = self.simulator.free_margin
        balance = self.simulator.balance
        if self.balance_or_free_margin_for_volume_computation:
            which_to_choose = balance
        else:
            which_to_choose = free_margin

        entry_price_with_fee = self._update_price_with_fee(entry_price, symbol)
        volume = (action_fuzzy_value * which_to_choose) / entry_price_with_fee
        # print(f"free_margin: {self.simulator.free_margin}, margin: {self.simulator.margin}, equity: {self.simulator.equity}, balance: {self.simulator.balance}, price: {entry_price}, entry_price_with_fee: {entry_price_with_fee}")
        return volume

    def set_thresholds(self, close_threshold=None, hold_threshold=None):
        if close_threshold is not None:
            self.close_threshold = close_threshold
        if hold_threshold is not None:
            self.hold_threshold = hold_threshold

    def set_sl_tp_and_types(self, sl=None, tp=None, sl_tp_type=None, sl_tp_log=False, trailing_distance=None):
        self.sl = sl
        self.tp = tp
        self.sl_tp_type = sl_tp_type
        self.sl_tp_log = sl_tp_log
        self.trailing_distance = trailing_distance


    def _init_orders_balance_equity_margin_array(self):
        for i in range(self.window_size):
            self.update_orders_balance_equity_margin_array()

    def _get_action_space(self):

        INF = 1e10
        if self.action_mode == 0:
            action_space = spaces.Box(
                low=-1e2, high=1e2, dtype=self.action_dtype,
                shape=(len(self.trading_symbols) * (self.symbol_max_orders + 2),)
            )  # symbol -> [close_order_i(logit), hold(logit), volume]

        elif self.action_mode == 1:
            # discrete mode only available for one trading_symbols and hedge=False now!
            action_space = spaces.Discrete(len(self.trading_symbols) * self.discrete_actions_count)
        elif self.action_mode == 2:
            single_symbol_action_space = spaces.Tuple((
                spaces.MultiBinary(self.symbol_max_orders),
                # spaces.Discrete(2),
                spaces.Discrete(2),
                spaces.Box(low=-1e2, high=1e2, shape=(1,), dtype=self.action_dtype,)
            ))
            action_space = spaces.Tuple(tuple(single_symbol_action_space for _ in range(len(self.trading_symbols))))

            # action_space = spaces.Tuple((
            #     spaces.Discrete(2),
            #     spaces.Discrete(2),
            #     spaces.Box(
            #             low=-1e2, high=1e2, dtype=self.action_dtype,
            #             shape=(len(self.trading_symbols) * (self.symbol_max_orders),)
            #             ),
            #     )
            # )

        return action_space

    def _get_observation_space(self):
        INF = 1e10
        if self.observation_mode == 0:
            observation_space = spaces.Dict({
                'balance': spaces.Box(low=-INF, high=INF, shape=(1,), dtype=self.observation_dtype),
                'equity': spaces.Box(low=-INF, high=INF, shape=(1,), dtype=self.observation_dtype),
                'margin': spaces.Box(low=-INF, high=INF, shape=(1,), dtype=self.observation_dtype),
                'free_margin': spaces.Box(low=-INF, high=INF, shape=(1,), dtype=self.observation_dtype),
                'features': spaces.Box(low=-INF, high=INF, shape=self.features_shape, dtype=self.observation_dtype),
                'orders': spaces.Box(low=-INF, high=INF, dtype=self.observation_dtype, shape=self.orders_shape)
                # symbol, order_i -> [entry_price, volume, profit] or [volume, profit] based on orders_observation_detail_count
            })
        elif self.observation_mode == 1:
            observation_shape = (self.window_size, self.signal_features.shape[1] + self.flattened_balance_equity_margin_orders_shape[1])
            observation_space = spaces.Box(low=-INF, high=INF, shape=observation_shape, dtype=self.observation_dtype)

        return observation_space


    def _get_info(self):
        orders = np.zeros(self.orders_shape)
        for i, symbol in enumerate(self.trading_symbols):
            symbol_orders = self.simulator.symbol_orders(symbol)
            for j, order in enumerate(symbol_orders):
                orders[i, j] = [order.entry_price, order.volume, order.profit]

        balance, equity, margin, free_margin = self._get_balance_equity_margin()

        return dict(
            balance = np.array([balance]),
            equity = np.array([equity]),
            margin = np.array([margin]),
            free_margin = np.array([free_margin]),
            orders = orders,
        )


    def reset(self, seed=None, **kwargs) -> Dict[str, np.ndarray]:
        super().reset(seed=seed)
        self._done = False
        self._current_tick = self._start_tick
        self.simulator = copy.deepcopy(self.original_simulator)
        self.simulator.current_time = self.time_points[self._current_tick]
        self.history = [self._create_info()]

        self._init_orders_balance_equity_margin_array()

        observation = self._get_observation()
        info = self._get_info()

        if self.render_mode == "human":
            self.render()

        return observation, info


    def step(self, action: np.ndarray) -> Tuple[Dict[str, np.ndarray], float, bool, Dict[str, Any]]:
        orders_info, closed_orders_info = self._apply_action(action)

        self.orders_sl_update()

        self._current_tick += 1
        if self._current_tick == self._end_tick:
            self._done = True

        dt = self.time_points[self._current_tick] - self.time_points[self._current_tick - 1]
        self.simulator.tick(dt)

        step_reward = self._calculate_reward()

        info = self._create_info(
            orders=orders_info, closed_orders=closed_orders_info, step_reward=step_reward
        )
        observation = self._get_observation()
        self.history.append(info)

        if self.render_mode == "human":
            self.render()

        if self.observation_mode==1:
            self.update_orders_balance_equity_margin_array()

        # print(f"step_reward: {step_reward}")

        return observation, step_reward, self._done, False, info


    def order_trailing_sl_updater(self, order):
        current_close = self.simulator.price_at(order.symbol, self.simulator.current_time)["Close"]
        previous_sl = order.sl

        if order.type == OrderType.Buy:
            if order.sl_tp_type == "pip":
                order.sl = min(order.sl, order.initial_sl + order.entry_price - current_close)
            elif order.sl_tp_type == "percent":
                order.sl = min(order.sl, 1 - ((current_close * (1 - order.sl))/order.entry_price))

        elif order.type == OrderType.Sell:
            if order.sl_tp_type == "pip":
                order.sl = min(order.sl, order.initial_sl - order.entry_price + current_close)
            elif order.sl_tp_type == "percent":
                order.sl = min(order.sl, ((current_close * (1 + order.sl))/order.entry_price) - 1 )
        if self.sl_tp_log:
            print(f"Previous SL: {previous_sl}, New SL: {order.sl}, Current Close: {current_close}")


    def orders_sl_update(self):
        if self.trailing_distance is not None:
            for i, symbol in enumerate(self.trading_symbols):
                orders = self.simulator.symbol_orders(symbol)
                for order in orders:
                    self.order_trailing_sl_updater(order)


    def _apply_action(self, action: np.ndarray) -> Tuple[Dict, Dict]:
        orders_info = {}
        closed_orders_info = {symbol: [] for symbol in self.trading_symbols}

        k = self.symbol_max_orders + 2

        for i, symbol in enumerate(self.trading_symbols):
            if self.action_mode == 0:
                symbol_action = action[k*i:k*(i+1)]
                close_orders_logit = symbol_action[:-2]
                hold_logit = symbol_action[-2]
                volume = symbol_action[-1]

                close_orders_probability = expit(close_orders_logit)
                hold_probability = expit(hold_logit)
                hold = bool(hold_probability > self.hold_threshold)
                # print(hold_logit, hold_probability, hold)
                # print(self.hold_threshold, self.close_threshold, hold, hold_probability, close_orders_probability)

            elif self.action_mode == 1:
                # this is just for keeping code able to handle multiple assets in the future
                action = [action]
                symbol_action = action[i]
                action_fuzzy_value = self.actions_fuzzy_term[symbol_action]
                hold = True if action_fuzzy_value == 0 else False
                hold_probability = 100.0 if hold else 0.0
                close_orders_probability = np.array([])
                volume = self._get_volume_for_discrete_action(symbol, action_fuzzy_value)
                # print(action, action_fuzzy_value, volume)

            elif self.action_mode == 2:
                symbol_action = action[i]
                close_orders_probability = symbol_action[0]
                hold = bool(symbol_action[1])
                volume = symbol_action[2][0]
                hold_probability = 100.0 if hold else 0.0
                # print(close_orders_probability, hold, volume)

            modified_volume = self._get_modified_volume(symbol, volume)
            symbol_orders = self.simulator.symbol_orders(symbol)
            orders_to_close_index = np.where(
                close_orders_probability[:len(symbol_orders)] > self.close_threshold
            )[0]
            orders_to_close = np.array(symbol_orders)[orders_to_close_index]

            # print(f"HOLD: {hold}")

            # print(orders_to_close)
            # print(f"hold_logit:{hold_logit}, hold_probability:{hold_probability}, close_orders_logit:{close_orders_logit}, close_orders_probability:{close_orders_probability}, orders_to_close:{orders_to_close}")
            # print(f"close_orders_probability:{close_orders_probability}, orders_to_close:{orders_to_close}, volume:{volume}, modified_volume:{modified_volume}")



            for j, order in enumerate(orders_to_close):
                self.simulator.close_order(order)
                closed_orders_info[symbol].append(dict(
                    order_id=order.id, symbol=order.symbol, order_type=order.type,
                    volume=order.volume, fee=order.fee,
                    margin=order.margin, profit=order.profit,
                    close_probability=close_orders_probability[orders_to_close_index][j],
                    fee_type=order.fee_type, sl=order.sl, tp=order.tp, sl_tp_type=order.sl_tp_type, trailing_distance=order.trailing_distance,
                ))
                # print(f"equity: {self.simulator.equity}, margin:{self.simulator.margin}, balance:{self.simulator.balance}")

            orders = self.simulator.symbol_orders(symbol)
            for order in orders:
                if self.check_is_not_none(order.sl_tp_type):
                    if self.check_sl_tp_condition(order, log=self.sl_tp_log):
                        closed_orders_info[symbol].append(dict(
                            order_id=order.id, symbol=order.symbol, order_type=order.type,
                            volume=order.volume, fee=order.fee,
                            margin=order.margin, profit=order.profit,
                            fee_type=order.fee_type, sl=order.sl, tp=order.tp, sl_tp_type=order.sl_tp_type, trailing_distance=order.trailing_distance,
                        ))

            orders_capacity = self.symbol_max_orders - (len(self.simulator.symbol_orders(symbol)))
            # print(f"orders_capacity: {orders_capacity}")

            orders_info[symbol] = dict(
                order_id=None, symbol=symbol, hold_probability=hold_probability,
                hold=hold, volume=volume, capacity=orders_capacity, order_type=None,
                modified_volume=modified_volume, fee=float('nan'), margin=float('nan'),
                fee_type=self.fee_type, sl=self.sl, tp=self.tp, sl_tp_type=self.sl_tp_type, trailing_distance=self.trailing_distance,
                error='',
            )

            if self.simulator.hedge and orders_capacity == 0:
                orders_info[symbol].update(dict(
                    error="cannot add more orders"
                ))
            elif not hold:
                order_type = OrderType.Buy if volume > 0. else OrderType.Sell
                fee = self.fee if type(self.fee) is float else self.fee(symbol)
                # print("IN HOLD")
                # print(f"equity: {self.simulator.equity}, margin:{self.simulator.margin}, balance:{self.simulator.balance}")

                try:
                    order = self.simulator.create_order(order_type, symbol, modified_volume, fee, self.fee_type, sl=self.sl, tp=self.tp, sl_tp_type=self.sl_tp_type, trailing_distance=self.trailing_distance)
                    new_info = dict(
                        order_id=order.id, order_type=order_type,
                        fee=fee, margin=order.margin,
                    )
                except ValueError as e:
                    new_info = dict(error=str(e))

                # print(f"equity: {self.simulator.equity}, margin:{self.simulator.margin}, balance:{self.simulator.balance}")
                # print("HOLD DONE")

                orders_info[symbol].update(new_info)

                # print(f"symbol_orders:{self.simulator.symbol_orders(symbol)} ,hold:{hold}, new_info:{new_info}")
            # print("---------------------------------------")
                # print()
        return orders_info, closed_orders_info


    def order_sl_or_tp_creator(self, order, low_or_high):
        if order.type == OrderType.Buy:
            if low_or_high=="Low":
                sl_or_tp = order.sl
            elif low_or_high=="High":
                sl_or_tp = order.tp
        elif order.type == OrderType.Sell:
            if low_or_high=="Low":
                sl_or_tp = order.tp
            elif low_or_high=="High":
                sl_or_tp = order.sl

        return sl_or_tp


    def sl_tp_conditions_creator(self, order, low_or_high):
        sl_or_tp = self.order_sl_or_tp_creator(order, low_or_high)

        if order.sl_tp_type == "pip":
            if low_or_high=="Low":
                return order.entry_price - sl_or_tp, sl_or_tp
            elif low_or_high=="High":
                return order.entry_price + sl_or_tp, sl_or_tp
        elif order.sl_tp_type == "percent":
            if low_or_high=="Low":
                return order.entry_price * (1 - sl_or_tp), sl_or_tp
            elif low_or_high=="High":
                return order.entry_price * (1 + sl_or_tp), sl_or_tp

    @staticmethod
    def check_is_not_none(condition):
        if condition is not None:
            return True
        else:
            return False


    def check_sl_tp_condition(self, order, log=False):
        current_ohlc = self.simulator.price_at(order.symbol, self.simulator.current_time)
        close_order = False
        sl_or_tp_low  = self.order_sl_or_tp_creator(order, low_or_high="Low")
        sl_or_tp_high = self.order_sl_or_tp_creator(order, low_or_high="High")


        if order.type == OrderType.Buy:
            if self.check_is_not_none(sl_or_tp_low):
                thresh, sl = self.sl_tp_conditions_creator(order, "Low")
                if current_ohlc["Low"] <= thresh:
                    if log:
                        print(f"Buy SL Hit, SL: {sl}, Threshold: {thresh} ,Entry: {order.entry_price}, Close: {current_ohlc['Close']}, Low: {current_ohlc['Low']}")
                    close_order = True
                    close_price = thresh

            if self.check_is_not_none(sl_or_tp_high):
                thresh, tp = self.sl_tp_conditions_creator(order, "High")
                if current_ohlc["High"] >= thresh:
                    if log:
                        print(f"Buy TP Hit, TP: {tp}, Threshold: {thresh} ,Entry: {order.entry_price}, Close: {current_ohlc['Close']}, High: {current_ohlc['High']}")
                    close_order = True
                    close_price = thresh

        elif order.type == OrderType.Sell:
            if self.check_is_not_none(sl_or_tp_high):
                thresh, sl = self.sl_tp_conditions_creator(order, "High")
                if current_ohlc["High"] >= thresh:
                    if log:
                        print(f"Sell SL Hit, SL: {sl}, Threshold: {thresh} ,Entry: {order.entry_price}, Close: {current_ohlc['Close']}, High: {current_ohlc['High']}")
                    close_order = True
                    close_price = thresh

            if self.check_is_not_none(sl_or_tp_low):
                thresh, tp = self.sl_tp_conditions_creator(order, "Low")
                if current_ohlc["Low"] <= thresh:
                    if log:
                        print(f"Sell TP Hit, TP: {tp}, Threshold: {thresh} ,Entry: {order.entry_price}, Close: {current_ohlc['Close']}, Low: {current_ohlc['Low']}")
                    close_order = True
                    close_price = thresh

        if close_order:
            self.simulator.close_order(order, close_price=close_price)
            return True
        else:
            return False



    def _get_prices(self, keys: List[str]=['Close', 'Open']) -> Dict[str, np.ndarray]:
        prices = {}

        for symbol in self.trading_symbols:
            get_price_at = lambda time: \
                self.original_simulator.price_at(symbol, time)[keys]

            if self.multiprocessing_pool is None:
                p = list(map(get_price_at, self.time_points))
            else:
                p = self.multiprocessing_pool.map(get_price_at, self.time_points)

            prices[symbol] = np.array(p)

        return prices


    def _process_data(self) -> np.ndarray:
        # data = self.prices

        data = {}
        for symbol in self.trading_symbols:
            data[symbol] = np.array(self.original_simulator.symbols_data[symbol].iloc[:, self.ohlc_count_in_symbols_data:])

        signal_features = np.column_stack(list(data.values()))
        return signal_features.astype(np.float32)

    def _get_order_detail_list(self, order):
        entry_price = order.entry_price
        volume = order.volume
        profit = order.profit

        if self.normalize_observation:
            if order.entry_price != 0 and order.volume != 0:
                current_close = self.simulator.price_at(order.symbol, self.simulator.current_time)["Close"]

                profit = profit / (entry_price * volume)
                if order.type == OrderType.Buy:
                    entry_price = (current_close / entry_price) - 1
                elif order.type == OrderType.Sell:
                    entry_price = (entry_price / current_close) - 1

        if self.orders_observation_detail_count == 3:
            return [entry_price, volume, profit]
        elif self.orders_observation_detail_count == 2:
            return [volume, profit]

    def _get_orders(self):
        orders = np.zeros(self.orders_shape)
        for i, symbol in enumerate(self.trading_symbols):
            symbol_orders = self.simulator.symbol_orders(symbol)
            for j, order in enumerate(symbol_orders):
                orders[i, j] = self._get_order_detail_list(order)
        return orders

    def _get_orders_balance_equity_margin_one_step_flattened(self):
        orders_flattened = list(self._get_orders().flatten())
        balance, equity, margin, free_margin = self._get_balance_equity_margin()
        balance_equity_margin = [balance, equity, margin, free_margin]
        orders_balance_equity_margin_one_step_flattened = np.array(orders_flattened + balance_equity_margin)
        return orders_balance_equity_margin_one_step_flattened

    def add_row_shift_down(self, new_row, arr=None):
        if arr is None:
            arr = self.orders_balance_equity_margin_array

        if len(new_row) != arr.shape[1]:
            raise ValueError("The dimensions of the new row do not match the array's columns.")
        arr = np.vstack((new_row, arr[:-1]))
        return arr

    def update_orders_balance_equity_margin_array(self):
        new_row = self._get_orders_balance_equity_margin_one_step_flattened()
        self.orders_balance_equity_margin_array = self.add_row_shift_down(new_row)


    def _get_balance_equity_margin(self):
        balance = self.simulator.balance
        equity = self.simulator.equity
        margin = self.simulator.margin
        free_margin = self.simulator.free_margin
        if self.normalize_observation:
            balance /= self.simulator.initial_balance
            equity /= self.simulator.initial_balance
            margin /= self.simulator.initial_balance
            free_margin /= self.simulator.initial_balance

        return balance, equity, margin, free_margin

    def _get_observation(self):
        features = self.signal_features[(self._current_tick-self.window_size+1):(self._current_tick+1)]

        if self.observation_mode==0:
            balance, equity, margin, free_margin = self._get_balance_equity_margin()
            orders = self._get_orders()

            observation = {
                'balance': np.array([balance]),
                'equity': np.array([equity]),
                'margin': np.array([margin]),
                'free_margin': np.array([free_margin]),
                'features': features,
                'orders': orders,
            }

        elif self.observation_mode==1:
            # print(self.orders_balance_equity_margin_array.shape, features.shape)
            observation = np.concatenate((features, self.orders_balance_equity_margin_array), axis=1)

        return observation


    def _calculate_reward(self) -> float:
        prev_equity = self.history[-1]['equity']
        current_equity = self.simulator.equity
        step_reward = current_equity - prev_equity
        return step_reward


    def _create_info(self, **kwargs: Any) -> Dict[str, Any]:
        info = {k: v for k, v in kwargs.items()}
        info['balance'] = self.simulator.balance
        info['equity'] = self.simulator.equity
        info['margin'] = self.simulator.margin
        info['free_margin'] = self.simulator.free_margin
        info['margin_level'] = self.simulator.margin_level
        return info


    def _get_modified_volume(self, symbol: str, volume: float) -> float:
        si = self.simulator.symbols_info[symbol]
        v = abs(volume)
        v = np.clip(v, si.volume_min, si.volume_max)
        v = round(v / si.volume_step) * si.volume_step
        return v


    def render(self, mode: str='human', **kwargs: Any) -> Any:
        if mode == 'simple_figure':
            return self._render_simple_figure(**kwargs)
        if mode == 'advanced_figure':
            return self._render_advanced_figure(**kwargs)
        return self.simulator.get_state(**kwargs)


    def _render_simple_figure(
        self, figsize: Tuple[float, float]=(14, 6), return_figure: bool=False
    ) -> Any:
        fig, ax = plt.subplots(figsize=figsize, facecolor='white')

        cmap_colors = np.array(plt_cm.tab10.colors)[[0, 1, 4, 5, 6, 8]]
        cmap = plt_colors.LinearSegmentedColormap.from_list('mtsim', cmap_colors)
        symbol_colors = cmap(np.linspace(0, 1, len(self.trading_symbols)))

        for j, symbol in enumerate(self.trading_symbols):
            close_price = self.prices[symbol][:, 0]
            symbol_color = symbol_colors[j]

            ax.plot(self.time_points, close_price, c=symbol_color, marker='.', label=symbol)

            buy_ticks = []
            buy_error_ticks = []
            sell_ticks = []
            sell_error_ticks = []
            close_ticks = []

            for i in range(1, len(self.history)):
                tick = self._start_tick + i - 1

                order = self.history[i]['orders'].get(symbol, {})
                if order and not order['hold']:
                    if order['order_type'] == OrderType.Buy:
                        if order['error']:
                            buy_error_ticks.append(tick)
                        else:
                            buy_ticks.append(tick)
                    else:
                        if order['error']:
                            sell_error_ticks.append(tick)
                        else:
                            sell_ticks.append(tick)

                closed_orders = self.history[i]['closed_orders'].get(symbol, [])
                if len(closed_orders) > 0:
                    close_ticks.append(tick)

            tp = np.array(self.time_points)
            ax.plot(tp[buy_ticks], close_price[buy_ticks], '^', color='green')
            ax.plot(tp[buy_error_ticks], close_price[buy_error_ticks], '^', color='gray')
            ax.plot(tp[sell_ticks], close_price[sell_ticks], 'v', color='red')
            ax.plot(tp[sell_error_ticks], close_price[sell_error_ticks], 'v', color='gray')
            ax.plot(tp[close_ticks], close_price[close_ticks], '|', color='black')

            ax.tick_params(axis='y', labelcolor=symbol_color)
            ax.yaxis.tick_left()
            if j < len(self.trading_symbols) - 1:
                ax = ax.twinx()

        fig.suptitle(
            f"Balance: {self.simulator.balance:.6f} {self.simulator.unit} ~ "
            f"Equity: {self.simulator.equity:.6f} ~ "
            f"Margin: {self.simulator.margin:.6f} ~ "
            f"Free Margin: {self.simulator.free_margin:.6f} ~ "
            f"Margin Level: {self.simulator.margin_level:.6f}"
        )
        fig.legend(loc='right')

        if return_figure:
            return fig

        plt.show()


    def _render_advanced_figure(
            self, figsize: Tuple[float, float]=(1400, 600), time_format: str="%Y-%m-%d %H:%m",
            return_figure: bool=False
        ) -> Any:

        fig = go.Figure()

        cmap_colors = np.array(plt_cm.tab10.colors)[[0, 1, 4, 5, 6, 8]]
        cmap = plt_colors.LinearSegmentedColormap.from_list('mtsim', cmap_colors)
        symbol_colors = cmap(np.linspace(0, 1, len(self.trading_symbols)))
        get_color_string = lambda color: "rgba(%s, %s, %s, %s)" % tuple(color)

        extra_info = [
            f"balance: {h['balance']:.6f} {self.simulator.unit}<br>"
            f"equity: {h['equity']:.6f}<br>"
            f"margin: {h['margin']:.6f}<br>"
            f"free margin: {h['free_margin']:.6f}<br>"
            f"margin level: {h['margin_level']:.6f}"
            for h in self.history
        ]
        extra_info = [extra_info[0]] * (self.window_size - 1) + extra_info

        for j, symbol in enumerate(self.trading_symbols):
            close_price = self.prices[symbol][:, 0]
            symbol_color = symbol_colors[j]

            fig.add_trace(
                go.Scatter(
                    x=self.time_points,
                    y=close_price,
                    mode='lines+markers',
                    line_color=get_color_string(symbol_color),
                    opacity=1.0,
                    hovertext=extra_info,
                    name=symbol,
                    yaxis=f'y{j+1}',
                    legendgroup=f'g{j+1}',
                ),
            )

            fig.update_layout(**{
                f'yaxis{j+1}': dict(
                    tickfont=dict(color=get_color_string(symbol_color * [1, 1, 1, 0.8])),
                    overlaying='y' if j > 0 else None,
                    # position=0.035*j
                ),
            })

            trade_ticks = []
            trade_markers = []
            trade_colors = []
            trade_sizes = []
            trade_extra_info = []
            trade_max_volume = max([
                h.get('orders', {}).get(symbol, {}).get('modified_volume') or 0
                for h in self.history
            ])
            close_ticks = []
            close_extra_info = []

            for i in range(1, len(self.history)):
                tick = self._start_tick + i - 1

                order = self.history[i]['orders'].get(symbol)
                if order and not order['hold']:
                    marker = None
                    color = None
                    size = 8 + 22 * (order['modified_volume'] / trade_max_volume)
                    info = (
                        f"order id: {order['order_id'] or ''}<br>"
                        f"hold probability: {order['hold_probability']:.4f}<br>"
                        f"hold: {order['hold']}<br>"
                        f"volume: {order['volume']:.6f}<br>"
                        f"modified volume: {order['modified_volume']:.4f}<br>"
                        f"fee: {order['fee']:.6f}<br>"
                        f"margin: {order['margin']:.6f}<br>"
                        f"error: {order['error']}"
                    )

                    if order['order_type'] == OrderType.Buy:
                        marker = 'triangle-up'
                        color = 'gray' if order['error'] else 'green'
                    else:
                        marker = 'triangle-down'
                        color = 'gray' if order['error'] else 'red'

                    trade_ticks.append(tick)
                    trade_markers.append(marker)
                    trade_colors.append(color)
                    trade_sizes.append(size)
                    trade_extra_info.append(info)

                closed_orders = self.history[i]['closed_orders'].get(symbol, [])
                if len(closed_orders) > 0:
                    info = []
                    for order in closed_orders:
                        info_i = (
                            f"order id: {order['order_id']}<br>"
                            f"order type: {order['order_type'].name}<br>"
                            f"close probability: {order['close_probability']:.4f}<br>"
                            f"margin: {order['margin']:.6f}<br>"
                            f"profit: {order['profit']:.6f}"
                        )
                        info.append(info_i)
                    info = '<br>---------------------------------<br>'.join(info)

                    close_ticks.append(tick)
                    close_extra_info.append(info)

            fig.add_trace(
                go.Scatter(
                    x=np.array(self.time_points)[trade_ticks],
                    y=close_price[trade_ticks],
                    mode='markers',
                    hovertext=trade_extra_info,
                    marker_symbol=trade_markers,
                    marker_color=trade_colors,
                    marker_size=trade_sizes,
                    name=symbol,
                    yaxis=f'y{j+1}',
                    showlegend=False,
                    legendgroup=f'g{j+1}',
                ),
            )

            fig.add_trace(
                go.Scatter(
                    x=np.array(self.time_points)[close_ticks],
                    y=close_price[close_ticks],
                    mode='markers',
                    hovertext=close_extra_info,
                    marker_symbol='line-ns',
                    marker_color='black',
                    marker_size=7,
                    marker_line_width=1.5,
                    name=symbol,
                    yaxis=f'y{j+1}',
                    showlegend=False,
                    legendgroup=f'g{j+1}',
                ),
            )

        title = (
            f"Balance: {self.simulator.balance:.6f} {self.simulator.unit} ~ "
            f"Equity: {self.simulator.equity:.6f} ~ "
            f"Margin: {self.simulator.margin:.6f} ~ "
            f"Free Margin: {self.simulator.free_margin:.6f} ~ "
            f"Margin Level: {self.simulator.margin_level:.6f}"
        )
        fig.update_layout(
            title=title,
            xaxis_tickformat=time_format,
            width=figsize[0],
            height=figsize[1],
        )

        if return_figure:
            return fig

        fig.show()


    def close(self) -> None:
        plt.close()


    def orders_extractor_from_history(self, change_index=False, sort=True):
        state = self.render()
        orders = state['orders'].copy().iloc[::-1]

        # orders['Profit_'] = ((orders['Entry Price'] - orders['Exit Price']) -orders['Fee']) * orders['Volume']
        orders['Entry Value'] = ((orders['Entry Price'] * orders['Volume']))
        orders['Return'] = orders['Profit'] / ((orders['Entry Price'] * orders['Volume']))
        orders['Duration'] = orders['Exit Time'] - orders['Entry Time']
        orders["Paid Fee"]  = orders["Gross Profit"] - orders["Profit"]

        if change_index:
            column = 'Entry Time' if entry else 'Exit Time'
            orders['Date'] = orders[column]
            orders.set_index('Date', inplace=True)

        if sort:
            orders = orders.sort_index()

        return orders

    def returns_equity_extractor_from_history(self):
        state = self.render()

        equity = pd.Series([h['equity'] for h in self.history], index=self.time_points[self.window_size - 1:])
        returns = equity.pct_change().iloc[1:]

        return returns, equity

    def returns_equity_close_prices_orders_extractor_from_history(self, symbol, change_index=False, sort=True):
        returns, equity = self.returns_equity_extractor_from_history()
        close_prices_list = self.prices[symbol][:, 0]
        orders = self.orders_extractor_from_history(change_index=change_index, sort=sort)
        return returns, equity, close_prices_list, orders
