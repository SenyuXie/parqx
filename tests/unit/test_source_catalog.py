from pathlib import Path

import pytest

from parqx.data.catalog import SourceCatalog, quote_identifier


def test_input_order_and_duplicate_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.parquet"
    first.touch()
    second = tmp_path / "second.parquet"
    second.touch()
    monkeypatch.chdir(tmp_path)

    catalog = SourceCatalog([Path("first.parquet"), second, first])

    assert [entry.spec.source_id for entry in catalog.entries] == [
        "source-1",
        "source-2",
    ]
    assert [entry.spec.path for entry in catalog.entries] == [first, second]
    assert [entry.spec.table_name for entry in catalog.entries] == ["first", "second"]
    assert all(entry.state == "loading" for entry in catalog.entries)
    assert catalog.snapshot() == ()


def test_symlink_uses_first_input_name(tmp_path: Path) -> None:
    target = tmp_path / "original.parquet"
    target.touch()
    alias = tmp_path / "alias.parquet"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")

    catalog = SourceCatalog([alias, target])

    assert len(catalog.entries) == 1
    source = catalog.entries[0].spec
    assert source.path == target
    assert source.display_name == "alias.parquet"
    assert source.table_name == "alias"


def test_case_alias_of_existing_file_is_deduplicated(tmp_path: Path) -> None:
    original = tmp_path / "example.parquet"
    original.touch()
    alias = tmp_path / "EXAMPLE.parquet"
    if not alias.exists() or not original.samefile(alias):
        pytest.skip("Filesystem is case sensitive")

    catalog = SourceCatalog([alias, original])

    assert len(catalog.entries) == 1
    assert catalog.entries[0].spec.display_name == "EXAMPLE.parquet"
    assert catalog.entries[0].spec.table_name == "EXAMPLE"


def test_hardlink_uses_first_input_name(tmp_path: Path) -> None:
    original = tmp_path / "original.parquet"
    original.touch()
    alias = tmp_path / "alias.parquet"
    try:
        alias.hardlink_to(original)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Hardlinks unavailable: {exc}")

    catalog = SourceCatalog([alias, original])

    assert len(catalog.entries) == 1
    source = catalog.entries[0].spec
    assert source.path == alias
    assert source.display_name == "alias.parquet"
    assert source.table_name == "alias"


def test_names_handle_ascii_case_and_occupied_suffixes(tmp_path: Path) -> None:
    names = ["users", "users_2", "Users", "users", "users_3", "Å", "å"]
    catalog = SourceCatalog(
        [tmp_path / str(index) / f"{name}.parquet" for index, name in enumerate(names)]
    )

    assert [entry.spec.table_name for entry in catalog.entries] == [
        "users",
        "users_2",
        "Users_3",
        "users_4",
        "users_3_2",
        "Å",
        "å",
    ]


@pytest.mark.parametrize(
    ("filename", "name", "quoted"),
    [
        ("order-items.parquet", "order-items", '"order-items"'),
        ("two words.parquet", "two words", '"two words"'),
        ("销售记录.parquet", "销售记录", '"销售记录"'),
        ("many.dots.parquet", "many.dots", '"many.dots"'),
        ('a"b.parquet', 'a"b', '"a""b"'),
        ("select.parquet", "select", '"select"'),
        ("data.parquet", "data", '"data"'),
    ],
)
def test_names_preserve_file_stem(
    tmp_path: Path, filename: str, name: str, quoted: str
) -> None:
    source = SourceCatalog([tmp_path / filename]).entries[0].spec
    assert source.display_name == filename
    assert source.table_name == name
    assert source.quoted_name == quoted
    assert quote_identifier(name) == quoted


def test_ready_failed_and_closed_sources_keep_stable_identity(tmp_path: Path) -> None:
    catalog = SourceCatalog(
        [tmp_path / "a" / "same.parquet", tmp_path / "b" / "same.parquet"]
    )
    initial = catalog.entries
    first, second = (entry.spec for entry in initial)
    catalog.mark_ready(second.source_id)
    snapshot = catalog.snapshot()
    assert snapshot == (second,)

    catalog.mark_failed(first.source_id, "Not a Parquet file")
    failed = catalog.get(first.source_id)
    assert failed.state == "failed"
    assert failed.issue is not None
    assert failed.issue.source is first
    assert str(failed.issue) == f'"same" ({first.path}): Not a Parquet file'
    catalog.mark_closed(second.source_id)
    assert not catalog.get(second.source_id).is_open
    assert catalog.snapshot() == (second,)

    catalog.mark_ready(first.source_id)
    assert catalog.get(first.source_id).issue is None
    assert catalog.snapshot() == (first, second)
    assert snapshot == (second,)
    assert [entry.state for entry in initial] == ["loading", "loading"]
    assert [entry.spec.table_name for entry in catalog.entries] == ["same", "same_2"]


def test_loading_source_can_finish_after_its_view_closes(tmp_path: Path) -> None:
    catalog = SourceCatalog([tmp_path / "pending.parquet"])
    catalog.mark_closed("source-1")
    catalog.mark_ready("source-1")
    assert not catalog.get("source-1").is_open
    assert catalog.snapshot() == (catalog.get("source-1").spec,)


@pytest.mark.parametrize(
    "failure", [OSError("Permission denied"), RuntimeError("Loop")]
)
def test_resolution_failure_does_not_abort_other_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    bad = tmp_path / "bad.parquet"
    good = tmp_path / "good.parquet"
    original = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == bad:
            raise failure
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    catalog = SourceCatalog([bad, good, bad])

    assert len(catalog.entries) == 2
    failed, pending = catalog.entries
    assert failed.state == "failed"
    assert failed.spec.path == bad
    assert failed.issue is not None
    assert failed.issue.message == str(failure)
    assert pending.state == "loading"
    catalog.mark_ready(pending.spec.source_id)
    assert catalog.snapshot() == (pending.spec,)


def test_unknown_source_id_is_rejected() -> None:
    catalog = SourceCatalog([])
    with pytest.raises(KeyError, match="unknown"):
        catalog.get("unknown")
