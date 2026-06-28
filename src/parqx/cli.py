"""The Parqx CLI entrypoint."""

import logging
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

from parqx.logger import setup_logging
from parqx.tui.app import ParqxApp

logger = logging.getLogger(__name__)

app = typer.Typer(help="Parqx: A Parquet TUI inspector.")


def version_callback(value: bool) -> None:
    """Parqx version callback."""
    if value:
        print(f"parqx {metadata.version('parqx')}")
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
    """Parqx: A Parquet TUI inspector."""
    _ = version

    setup_logging(verbose)

    parqx = ParqxApp(path=path)
    parqx.run()

    if parqx.load_error is not None:
        typer.echo(f"parqx: cannot read {path}: {parqx.load_error}", err=True)
        raise typer.Exit(code=1)
