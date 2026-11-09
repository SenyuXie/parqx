"""Application construction stays independent of native data resources."""

from pathlib import Path
from unittest.mock import patch

import pytest

from parqx.config import ParqxConfig
from parqx.tui.app import Parqx


def test_empty_source_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="At least one"):
        Parqx([])


def test_configuration_and_app_construction_do_not_open_data(tmp_path: Path) -> None:
    with (
        patch("parqx.data.duckdb.duckdb.connect", side_effect=AssertionError("open")),
        patch(
            "parqx.data.duckdb.TemporaryDirectory", side_effect=AssertionError("spill")
        ),
    ):
        config = ParqxConfig(threads=1)
        app = Parqx([tmp_path / "missing.parquet"], config=config)
        assert app.config is config
        assert app.catalog.entries[0].state == "loading"
        app.on_unmount()
