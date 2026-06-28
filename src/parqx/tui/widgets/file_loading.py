"""The FileLoading widget."""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import LoadingIndicator, Static


def _format_size(path: Path) -> str:
    """Return a human-readable size for the file at `path`."""
    try:
        size = float(path.stat().st_size)
    except OSError:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


class FileLoading(Container):
    """Full-screen placeholder shown while a file is being read."""

    DEFAULT_CSS = """
    FileLoading {
        align: center middle;
        width: 100%;
        height: 100%;

        & > .fileloading--label {
            width: 100%;
            height: 1;
            content-align: center middle;
            color: $text-muted;
            margin-bottom: 1;
        }

        & > LoadingIndicator {
            width: 100%;
            height: 1;
        }
    }
    """

    def __init__(self, path: Path) -> None:
        """Initialize the indicator for the given file path.

        Args:
            path: File being loaded; used to derive the displayed label.
        """
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        """Yield a centered "Loading <file> (<size>)…" label and a spinner."""
        yield Static(
            f"Loading {self._path.name} ({_format_size(self._path)})…",
            classes="fileloading--label",
        )
        yield LoadingIndicator()
