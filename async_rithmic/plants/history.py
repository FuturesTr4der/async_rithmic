from datetime import datetime
import asyncio
from collections import defaultdict

from .base import BasePlant
from ..enums import SysInfraType, TimeBarType
from .. import protocol_buffers as pb

class HistoryPlant(BasePlant):
    infra_type = SysInfraType.HISTORY_PLANT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.historical_tick_data = defaultdict(list)
        self.historical_time_bar_data = defaultdict(list)

        self.historical_tick_events = {}  # key -> asyncio.Event()
        self.historical_time_bar_events = {}  # key -> asyncio.Event()

        self.client.on_historical_tick += self._on_historical_tick
        self.client.on_historical_time_bar += self._on_historical_time_bar

    async def _login(self):
        await super()._login()

        for symbol, exchange, bar_type, bar_type_periods in self._subscriptions["time_bar"]:
            await self.subscribe_to_time_bar_data(symbol, exchange, bar_type, bar_type_periods)

    def _datetime_to_index(self, dt: datetime):
        dt = self._datetime_to_utc(dt)
        return int(dt.timestamp())

    async def _on_historical_time_bar(self, data):
        # FIX: Include period in key to avoid collision when fetching multiple timeframes concurrently
        key = f"{data['symbol']}_{data['type']}_{data.get('period', '1')}"
        self.historical_time_bar_data[key].append(data)

    async def _on_historical_tick(self, data):
        key = f"{data['symbol']}"
        self.historical_tick_data[key].append(data)

    async def get_historical_tick_data(
        self,
        symbol: str,
        exchange: str,
        start_time: datetime,
        end_time: datetime,
        wait: bool = True
    ):
        """
        Creates and sends request for download of tick data for security/exchange over time period

        :param request_id: (str) generated request id used for processing as messages come in
        :param symbol: (str) valid security code (e.g. ES)
        :param exchange: (str) valid exchange code (e.g. CME)
        :param start_time: (dt) start time as datetime in utc
        :param end_time: (dt) end time as datetime in utc
        """
        # FIX: Calculate key upfront for per-request event
        key = f"{symbol}"

        if wait:
            # FIX: Create per-request event instead of shared event
            self.historical_tick_events[key] = asyncio.Event()

        await self._send_and_recv_immediate(
            template_id=206,
            user_msg=symbol,
            symbol=symbol,
            exchange=exchange,
            bar_type=pb.request_tick_bar_replay_pb2.RequestTickBarReplay.BarType.TICK_BAR,
            bar_type_specifier="1",
            bar_sub_type=pb.request_tick_bar_replay_pb2.RequestTickBarReplay.BarSubType.REGULAR,
            time_order=pb.request_tick_bar_replay_pb2.RequestTickBarReplay.TimeOrder.FORWARDS,
            start_index=self._datetime_to_index(start_time),
            finish_index=self._datetime_to_index(end_time),
        )

        # Wait until all the historical data has been fetched before returning it
        if wait:
            try:
                await asyncio.wait_for(self.historical_tick_events[key].wait(), 5.0)
            except asyncio.TimeoutError:
                if len(self.historical_tick_data[key]) == 0:
                    # No data returned by Rithmic for the request
                    self.historical_tick_events.pop(key, None)  # Cleanup
                    return []

            await self.historical_tick_events[key].wait()
            self.historical_tick_events.pop(key, None)  # Cleanup after use

            data = self.historical_tick_data.pop(key)
            return data

    async def get_historical_time_bars(
        self,
        symbol: str,
        exchange: str,
        start_time: datetime,
        end_time: datetime,
        bar_type: TimeBarType,
        bar_type_periods: int,
        wait: bool = True
    ):
        # FIX: Calculate key upfront for per-request event
        period_seconds = bar_type_periods * 60 if bar_type == TimeBarType.MINUTE_BAR else bar_type_periods
        key = f"{symbol}_{bar_type}_{period_seconds}"

        if wait:
            # FIX: Create per-request event instead of shared event
            self.historical_time_bar_events[key] = asyncio.Event()

        await self._send_and_recv_immediate(
            template_id=202,
            symbol=symbol,
            exchange=exchange,
            bar_type=bar_type,
            bar_type_period=bar_type_periods,
            time_order=pb.request_time_bar_replay_pb2.RequestTimeBarReplay.TimeOrder.FORWARDS,
            start_index=self._datetime_to_index(start_time),
            finish_index=self._datetime_to_index(end_time),
        )

        # Wait until all the historical data has been fetched before returning it
        if wait:
            try:
                await asyncio.wait_for(self.historical_time_bar_events[key].wait(), 5.0)
            except asyncio.TimeoutError:
                if len(self.historical_time_bar_data[key]) == 0:
                    # No data returned by Rithmic for the request
                    self.historical_time_bar_events.pop(key, None)  # Cleanup
                    return []

            await self.historical_time_bar_events[key].wait()
            self.historical_time_bar_events.pop(key, None)  # Cleanup after use

            data = self.historical_time_bar_data.pop(key)
            return data

    async def subscribe_to_time_bar_data(
        self,
        symbol: str,
        exchange: str,
        bar_type: TimeBarType,
        bar_type_periods: int
    ):
        """
        Subscribes to time bars
        """

        sub = (symbol, exchange, bar_type, bar_type_periods)
        self._subscriptions["time_bar"].add(sub)

        return await self._send_and_recv_immediate(
            template_id=200,
            symbol=symbol,
            exchange=exchange,
            request=pb.request_time_bar_update_pb2.RequestTimeBarUpdate.Request.SUBSCRIBE,
            bar_type=bar_type,
            bar_type_period=bar_type_periods,
        )

    async def unsubscribe_from_time_bar_data(
        self,
        symbol: str,
        exchange: str,
        bar_type: TimeBarType,
        bar_type_periods: int
    ):
        sub = (symbol, exchange, bar_type, bar_type_periods)
        self._subscriptions["time_bar"].discard(sub)

        return await self._send_and_recv_immediate(
            template_id=200,
            symbol=symbol,
            exchange=exchange,
            request=pb.request_time_bar_update_pb2.RequestTimeBarUpdate.Request.UNSUBSCRIBE,
            bar_type=bar_type,
            bar_type_period=bar_type_periods,
        )

    async def _process_response(self, response):
        if await super()._process_response(response):
            return True

        if response.template_id == 203:
            # Historical time bar
            is_last_bar = response.rp_code == ['0'] or response.rq_handler_rp_code == []
            if is_last_bar:
                # FIX: Set event for the specific request that completed
                # Extract key from response to identify which request finished
                data = self._response_to_dict(response)
                key = f"{data.get('symbol', '')}_{data.get('type', '')}_{data.get('period', '1')}"
                if key in self.historical_time_bar_events:
                    self.historical_time_bar_events[key].set()
                return

            data = self._response_to_dict(response)
            data["bar_end_datetime"] = datetime.fromtimestamp(data['marker'])

            await self.client.on_historical_time_bar.call_async(data)

        elif response.template_id == 207:
            # Historical tick bar
            is_last_bar = response.rp_code == ['0'] or response.rq_handler_rp_code == []
            if is_last_bar:
                # FIX: Set event for the specific request (tick data uses symbol as key)
                data = self._response_to_dict(response)
                key = f"{data.get('symbol', '')}"
                if key in self.historical_tick_events:
                    self.historical_tick_events[key].set()
                return

            data = self._response_to_dict(response)
            data["datetime"] = self._ssboe_usecs_to_datetime(response.data_bar_ssboe[0], response.data_bar_usecs[0])

            await self.client.on_historical_tick.call_async(data)

        elif response.template_id == 250:
            # Time Bar
            data = self._response_to_dict(response)
            data["bar_end_datetime"] = datetime.fromtimestamp(data['marker'])

            await self.client.on_time_bar.call_async(data)

        else:
            self.logger.warning(f"Unhandled inbound message with template_id={response.template_id}")
