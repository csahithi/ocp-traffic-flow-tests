import logging
import os
import shlex
import subprocess
import sys
import threading
import typing

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Optional

from logger import logger


_lock = threading.Lock()

_unique_log_id_value = 0


def _unique_log_id() -> int:
    # For each run() call, we log a message when starting the command and when
    # completing it. Add a unique number to those logging statements, so that
    # we can easier find them in a large log.
    with _lock:
        global _unique_log_id_value
        _unique_log_id_value += 1
        return _unique_log_id_value


T = typing.TypeVar("T", bound=str | bytes)


@dataclass(frozen=True)
class BaseResult(ABC, typing.Generic[T]):
    out: T
    err: T
    returncode: int

    @property
    def success(self) -> bool:
        return self.returncode == 0

    def debug_str(self) -> str:
        if self.returncode == 0:
            status = "succcess"
        else:
            status = f"failed ({self.returncode})"

        out = ""
        if self.out:
            out = f"; out={repr(self.out)}"

        err = ""
        if self.err:
            err = f"; err={repr(self.err)}"

        return f"{status}{out}{err}"

    def debug_msg(self) -> str:
        return f"cmd {self.debug_str()}"


@dataclass(frozen=True)
class Result(BaseResult[str]):
    pass


@dataclass(frozen=True)
class BinResult(BaseResult[bytes]):
    def decode(self, errors: str = "strict") -> Result:
        return Result(
            self.out.decode(errors=errors),
            self.err.decode(errors=errors),
            self.returncode,
        )


class Host(ABC):
    @abstractmethod
    def pretty_str(self) -> str:
        pass

    @typing.overload
    def run(
        self,
        cmd: str | Iterable[str],
        *,
        text: typing.Literal[True] = True,
        env: Optional[Mapping[str, Optional[str]]] = None,
        log_prefix: str = "",
        log_level: int = logging.DEBUG,
        log_level_result: Optional[int] = None,
        log_level_fail: Optional[int] = None,
        die_on_error: bool = False,
        decode_errors: Optional[str] = None,
    ) -> Result:
        pass

    @typing.overload
    def run(
        self,
        cmd: str | Iterable[str],
        *,
        text: typing.Literal[False],
        env: Optional[Mapping[str, Optional[str]]] = None,
        log_prefix: str = "",
        log_level: int = logging.DEBUG,
        log_level_result: Optional[int] = None,
        log_level_fail: Optional[int] = None,
        die_on_error: bool = False,
        decode_errors: Optional[str] = None,
    ) -> BinResult:
        pass

    def run(
        self,
        cmd: str | Iterable[str],
        *,
        text: bool = True,
        env: Optional[Mapping[str, Optional[str]]] = None,
        log_prefix: str = "",
        log_level: int = logging.DEBUG,
        log_level_result: Optional[int] = None,
        log_level_fail: Optional[int] = None,
        die_on_error: bool = False,
        decode_errors: Optional[str] = None,
    ) -> Result | BinResult:
        log_id = _unique_log_id()
        if not isinstance(cmd, str):
            cmd = shlex.join(list(cmd))

        if log_level >= 0:
            logger.log(
                log_level,
                f"{log_prefix}cmd[{log_id};{self.pretty_str()}]: call `{cmd}`",
            )

        bin_result = self._run(
            cmd=cmd,
            env=env,
        )

        # The remainder is only concerned with printing a nice logging message and
        # (potentially) decode the binary output.

        str_result: Optional[Result] = None
        unexpected_binary = False
        is_binary = True
        decode_exception: Optional[Exception] = None
        if text:
            # The caller requested string (Result) output. "decode_errors" control what we do.
            #
            # - None (the default). We effectively use "errors='replace'"). On any encoding
            #   error we log an ERROR message.
            # - otherwise, we use "decode_errors" as requested. An encoding error will not
            #   raise the log level, but we will always log the result. We will even log
            #   the result if the decoding results in an exception (see decode_exception).
            try:
                # We first always try to decode strictly to find out whether
                # it's valid utf-8.
                str_result = bin_result.decode(errors="strict")
            except UnicodeError as e:
                if decode_errors == "strict":
                    decode_exception = e
                is_binary = True
            else:
                is_binary = False

            if decode_exception is not None:
                # We had an error. We keep this and re-raise later.
                pass
            elif not is_binary and (
                decode_errors is None
                or decode_errors in ("strict", "ignore", "replace", "surrogateescape")
            ):
                # We are good. The output is not binary, and the caller did not
                # request some unusual decoding. We already did the decoding.
                pass
            elif decode_errors is not None:
                # Decode again, this time with the decoding option requested
                # by the caller.
                try:
                    str_result = bin_result.decode(errors=decode_errors)
                except UnicodeError as e:
                    decode_exception = e
            else:
                # We have a binary and the caller didn't specify a special
                # encoding. We use "replace" fallback, but set a flag that
                # we have unexpected_binary (and lot an ERROR below).
                str_result = bin_result.decode(errors="replace")
                unexpected_binary = True

        status_msg = ""
        if log_level_fail is not None and not bin_result.success:
            result_log_level = log_level_fail
        elif log_level_result is not None:
            result_log_level = log_level_result
        else:
            result_log_level = log_level

        if die_on_error and not bin_result.success:
            if result_log_level < logging.ERROR:
                result_log_level = logging.ERROR
            status_msg += " [FATAL]"

        if text and is_binary:
            status_msg += " [BINARY]"

        if decode_exception:
            # We caught an exception during decoding. We still want to log the result,
            # before re-raising the exception.
            #
            # We don't increase the logging level, because the user requested a special
            # "decode_errors". A decoding error is expected, we just want to log about it
            # (with the level we would have).
            status_msg += " [DECODE_ERROR]"

        if unexpected_binary:
            status_msg += " [UNEXPECTED_BINARY]"
            if result_log_level < logging.ERROR:
                result_log_level = logging.ERROR

        if is_binary:
            # Note that we log the output as binary if either "text=False" or if
            # the output was not valid utf-8. In the latter case, we will still
            # return a string Result (or re-raise decode_exception).
            debug_str = bin_result.debug_str()
        else:
            assert str_result is not None
            debug_str = str_result.debug_str()

        if result_log_level >= 0:
            logger.log(
                result_log_level,
                f"{log_prefix}cmd[{log_id};{self.pretty_str()}]: └──> `{cmd}`:{status_msg} {debug_str}",
            )

        if decode_exception:
            raise decode_exception

        if die_on_error and not bin_result.success:
            sys.exit(-1)

        if str_result is not None:
            return str_result
        return bin_result

    @abstractmethod
    def _run(
        self,
        *,
        cmd: str,
        env: Optional[Mapping[str, Optional[str]]],
    ) -> BinResult:
        pass

    def file_exists(self, path: str | os.PathLike[Any]) -> bool:
        return self.run(["test", "-e", str(path)], log_level=-1, text=False).success


class LocalHost(Host):
    def pretty_str(self) -> str:
        return "localhost"

    def _run(
        self,
        *,
        cmd: str,
        env: Optional[Mapping[str, Optional[str]]],
    ) -> BinResult:
        full_env: Optional[dict[str, str]] = None
        if env is not None:
            full_env = os.environ.copy()
            for k, v in env.items():
                if v is None:
                    full_env.pop(k, None)
                else:
                    full_env[k] = v

        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            env=full_env,
        )

        return BinResult(res.stdout, res.stderr, res.returncode)

    def file_exists(self, path: str | os.PathLike[Any]) -> bool:
        return os.path.exists(path)


local = LocalHost()


def host_or_local(host: Optional[Host]) -> Host:
    if host is None:
        return local
    return host
