"""Colored logger wrapper with timestamps."""
from __future__ import annotations

import sys
from datetime import datetime

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)


class Printer:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _log(self, level: str, color: str, msg: str, file=sys.stdout) -> None:
        print(f"{Fore.WHITE}[{self._ts()}]{Style.RESET_ALL} {color}{level:<7}{Style.RESET_ALL} {msg}", file=file, flush=True)

    def info(self, msg: str) -> None:
        self._log("INFO", Fore.CYAN, msg)

    def success(self, msg: str) -> None:
        self._log("OK", Fore.GREEN, msg)

    def warn(self, msg: str) -> None:
        self._log("WARN", Fore.YELLOW, msg)

    def error(self, msg: str) -> None:
        self._log("ERROR", Fore.RED, msg, file=sys.stderr)

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._log("DEBUG", Fore.MAGENTA, msg)
