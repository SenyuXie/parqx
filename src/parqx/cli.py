"""CLI entrypoint for Parqx."""

import logging
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

from parqx.logger import setup_logging
from parqx.tui.app import ParqxApp

logger = logging.getLogger(__name__)

app = typer.Typer(help="Parqx: A TUI Parquet inspector.")


def version_callback(value: bool) -> None:
    """Parqx version callback."""
    if value:
        print(f"parqx {metadata.version('parqx')}")
        raise typer.Exit()


@app.command()
def main(
    paths: Annotated[list[Path], typer.Argument(help="Parquet files to inspect.")],
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="Enable verbose logging (or `-vv` for more verbose output).",
            max=2,
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
    """Parqx: A TUI Parquet inspector."""
    _ = version

    setup_logging(verbose)

    parqx = ParqxApp(paths=paths)
    parqx.run()

    if parqx.load_errors:
        for issue in parqx.load_errors:
            typer.echo(f"parqx: cannot read {issue}", err=True)
        raise typer.Exit(code=1)
