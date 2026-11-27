"""Exercise the CLI's resolved settings through a mounted query workspace."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from parqx.cli import app
from parqx.tui.app import Parqx
from parqx.tui.table.arrow_table import ArrowTable
from tests.helpers import run_query, wait_for


@pytest.mark.parametrize(("flags", "rows"), [([], 2), (["--preview-rows", "3"], 3)])
def test_cli_preview_rows_reach_query_result(
    small_parquet: Path, tmp_path: Path, flags: list[str], rows: int
) -> None:
    config = tmp_path / "team.toml"
    config.write_text("[query]\npreview_rows = 2\n", encoding="utf-8")

    async def inspect(application: Parqx) -> None:
        async with application.run_test() as pilot:
            await wait_for(lambda: bool(application.query(ArrowTable)), pilot)
            table = await run_query(
                application, pilot, "SELECT * FROM smoke ORDER BY id"
            )
            assert table.row_count == rows

    def run_headless(application: Parqx) -> None:
        asyncio.run(inspect(application))

    with patch.object(Parqx, "run", run_headless), patch("parqx.cli.setup_logging"):
        result = CliRunner().invoke(
            app,
            ["--config", str(config), *flags, str(small_parquet)],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
