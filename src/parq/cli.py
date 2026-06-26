"""The Parq CLI entrypoint."""

import logging
from importlib import metadata
from pathlib import Path
from typing import Annotated

import pyarrow.parquet as pq
import typer

from parq.logger import setup_logging
from parq.tui.app import ParqApp

logger = logging.getLogger(__name__)

app = typer.Typer(help="Parq: A Parquet TUI inspector.")


def version_callback(value: bool) -> None:
    """Parq version callback."""
    if value:
        print(f"parq {metadata.version('parq')}")
        raise typer.Exit()


@app.command()
def main(
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Parquet file to inspect.",
        ),
    ],
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="Enable verbose logging (or `-vv` for more verbose output).",
        ),
    ] = 0,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = None,
) -> None:
    """Parq: A Parquet TUI inspector."""
    _ = version

    setup_logging(verbose)

    table = pq.read_table(path)  # type: ignore
    ParqApp(table).run()
