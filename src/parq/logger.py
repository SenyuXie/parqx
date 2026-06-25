"""The Parq logging configuration."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_dir
from textual.logging import TextualHandler

_LOGGER_NAME = "parq"
_LOG_FILE_NAME = "parq.log"
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_LOG_FILE_BACKUP_COUNT = 3
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _verbosity_to_level(verbose: int) -> int:
    """Map the `--verbose` count to a `logging` level."""
    if verbose <= 0:
        return logging.WARNING
    if verbose == 1:
        return logging.INFO
    return logging.DEBUG


def _log_file_path() -> Path:
    """Return the rotating log file path under the platform user log directory."""
    log_dir = Path(user_log_dir("parq", appauthor=False, ensure_exists=True))
    return log_dir / _LOG_FILE_NAME


def setup_logging(verbose: int = 0) -> None:
    r"""Configure logging for the Parq application.

    A TUI takes over the terminal, so log records must never reach `stdout` or
    `stderr` while the app is running. This function installs two handlers on
    the `parq` logger:

    - A `RotatingFileHandler` writing detailed records to a log file under
    the platform user log directory (e.g. `%LOCALAPPDATA%\\parq\\Logs\\parq.log`
    on Windows, `~/.local/state/parq/log/parq.log` on Linux).
    - A `TextualHandler` so `logger.*` calls are also visible in `textual console`
    during development.

    Propagation to the root logger is disabled so records can't accidentally
    leak to `stderr` and corrupt the TUI. The function is idempotent - calling
    it again replaces the previously installed handlers.

    Args:
        verbose: Verbosity count from the CLI (`-v` once, `-vv` twice, ...).
            0 maps to `WARNING`, 1 to `INFO`, 2+ to `DEBUG`.
    """
    level = _verbosity_to_level(verbose)
    log_file = _log_file_path()

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for existing in logger.handlers:
        logger.removeHandler(existing)
        existing.close()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=_LOG_FILE_MAX_BYTES,
        backupCount=_LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(file_handler)

    logger.addHandler(TextualHandler())
