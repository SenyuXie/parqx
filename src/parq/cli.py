"""The Parq CLI entrypoint."""

from importlib import metadata
from pathlib import Path
from typing import Annotated

import pyarrow.parquet as pq
import typer

from parq.tui.app import ParqApp

app = typer.Typer(help="Parq: A Parquet TUI inspector.")


def version_callback(value: bool) -> None:
    """Parq version callback."""
    if value:
        typer.echo(f"parq {metadata.version('parq')}")
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
    table = pq.read_table(path)  # type: ignore
    ParqApp(table).run()
