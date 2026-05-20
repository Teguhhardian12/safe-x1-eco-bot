"""Generic helpers: retry decorator, delays, address masking."""
from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, Awaitable, Callable, Tuple, Type

from .printer import Printer


def mask_address(address: str, head: int = 6, tail: int = 4) -> str:
    if not address or len(address) < head + tail + 2:
        return address
    return f"{address[: head + 2]}...{address[-tail:]}"


async def delay(seconds: float, jitter: float = 0.0) -> None:
    if jitter > 0:
        seconds += random.uniform(0, jitter)
    if seconds > 0:
        await asyncio.sleep(seconds)


def retry(
    attempts: int = 3,
    base_delay: float = 5.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    printer: Printer | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for i in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    sleep_for = base_delay * (backoff ** i)
                    if printer:
                        printer.warn(f"{func.__name__} failed (attempt {i + 1}/{attempts}): {exc} — retrying in {sleep_for:.1f}s")
                    await asyncio.sleep(sleep_for)
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


def random_token_name(prefixes: list[str], suffixes: list[str]) -> tuple[str, str]:
    name = f"{random.choice(prefixes)}{random.choice(suffixes)}"
    symbol = "".join(c for c in name if c.isupper())
    if len(symbol) < 3:
        symbol = name[:4].upper()
    return name, symbol


def random_premint(min_amount: int, max_amount: int) -> int:
    return random.randint(min_amount, max_amount)
