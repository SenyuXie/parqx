"""CLI entrypoint for Parqx."""

import logging
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

from parqx.config_file import load_config
from parqx.logger import setup_logging
from parqx.tui.app import Parqx

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
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Use this TOML file for the session."),
    ] = None,
    preview_rows: Annotated[
        int | None, typer.Option(min=1, help="Maximum rows retained per SQL preview.")
    ] = None,
    preview_bytes: Annotated[
        int | None, typer.Option(min=1, help="Retained byte budget per SQL preview.")
    ] = None,
    memory_limit: Annotated[
        str | None,
        typer.Option(help="DuckDB memory limit per connection (e.g. 256MB)."),
    ] = None,
    threads: Annotated[
        int | None, typer.Option(min=1, help="DuckDB threads per connection.")
    ] = None,
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

    try:
        settings = load_config(
            config,
            preview_rows=preview_rows,
            preview_bytes=preview_bytes,
            memory_limit=memory_limit,
            threads=threads,
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"parqx: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    setup_logging(verbose)
    parqx = Parqx(paths=paths, config=settings)
    parqx.run()

    load_errors = parqx.load_errors
    for issue in load_errors:
        typer.echo(f"parqx: cannot read {issue}", err=True)
    return_code = parqx.return_code or int(bool(load_errors))
    if return_code:
        raise typer.Exit(code=return_code)
