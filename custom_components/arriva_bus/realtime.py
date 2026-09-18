"""Realtime Arriva KV6 subscriber."""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial

import zmq
import zmq.asyncio

from .const import (
    DATA_OWNER,
    KV6_ENDPOINT,
    KV6_ENVELOPE,
    KV6_FRAME_TIMEOUT_SECONDS,
)
from .models import Kv6Event, ParseError, parse_kv6_xml

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[Kv6Event], Awaitable[None]]


class Kv6Subscriber:
    """Receive and decode the public NDOV Arriva KV6 stream."""

    def __init__(self, line_planning_number: str) -> None:
        self._line_planning_number = line_planning_number
        self._connected = False
        self._last_frame_monotonic: float | None = None
        self._last_frame_received_at: datetime | None = None
        self._stop = asyncio.Event()
        self._socket: zmq.asyncio.Socket | None = None

    @property
    def connected(self) -> bool:
        """Return whether the stream has delivered data recently."""
        return (
            self._connected
            and self._last_frame_monotonic is not None
            and time.monotonic() - self._last_frame_monotonic <= KV6_FRAME_TIMEOUT_SECONDS
        )

    @property
    def last_frame_received_at(self) -> datetime | None:
        """Return when the latest valid Arriva frame arrived locally."""
        return self._last_frame_received_at

    async def async_run(self, callback: EventCallback) -> None:
        """Run until stopped, reconnecting after transport errors."""
        retry_seconds = 1

        while not self._stop.is_set():
            socket = zmq.asyncio.Context.instance().socket(zmq.SUB)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.SUBSCRIBE, KV6_ENVELOPE.encode())
            socket.connect(KV6_ENDPOINT)
            self._socket = socket

            try:
                while not self._stop.is_set():
                    frames = await asyncio.wait_for(
                        socket.recv_multipart(),
                        timeout=KV6_FRAME_TIMEOUT_SECONDS,
                    )
                    if len(frames) < 2 or frames[0] != KV6_ENVELOPE.encode():
                        continue

                    received_at = datetime.now(UTC)
                    self._connected = True
                    self._last_frame_monotonic = time.monotonic()
                    self._last_frame_received_at = received_at
                    retry_seconds = 1

                    try:
                        payload = gzip.decompress(b"".join(frames[1:]))
                        events = await asyncio.to_thread(
                            partial(
                                parse_kv6_xml,
                                payload,
                                data_owner=DATA_OWNER,
                                line_planning_number=self._line_planning_number,
                                received_at=received_at,
                            )
                        )
                    except (OSError, ParseError) as err:
                        _LOGGER.warning("Invalid Arriva KV6 message: %s", err)
                        continue

                    for event in events:
                        await callback(event)

            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if not self._stop.is_set():
                    _LOGGER.warning(
                        "No Arriva KV6 frame received for %s seconds; reconnecting",
                        KV6_FRAME_TIMEOUT_SECONDS,
                    )
            except zmq.ZMQError as err:
                if not self._stop.is_set():
                    _LOGGER.warning("Arriva KV6 connection failed: %s", err)
            finally:
                self._connected = False
                if self._socket is socket:
                    self._socket = None
                socket.close(linger=0)

            if self._stop.is_set():
                return

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=retry_seconds)
            except TimeoutError:
                retry_seconds = min(retry_seconds * 2, 60)

    async def async_stop(self) -> None:
        """Stop the subscriber and unblock a pending receive."""
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.close(linger=0)
