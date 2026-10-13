"""Session source identities and lightweight schemas in a native tree."""

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import Tree

from parqx.catalog import SourceEntry


class SourcesTree(Tree[str]):
    """Inspect sources independently of their mounted file views."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "collapse_source", "Collapse", show=False),
        Binding("right", "expand_source", "Expand", show=False),
    ]

    def __init__(self, entries: tuple[SourceEntry, ...]) -> None:
        """List the session's sources in their established input order."""
        super().__init__("Sources", id="sources")
        self.border_title = "Sources"
        self.show_root = False
        self.auto_expand = False
        self.root.expand()
        for entry in entries:
            self.root.add("", data=entry.spec.source_id)
            self.update_source(entry)

    def update_source(self, entry: SourceEntry) -> None:
        """Refresh one source's availability and schema without replacing the tree."""
        node = next(
            node for node in self.root.children if node.data == entry.spec.source_id
        )
        node.set_label(Text(f"{entry.spec.display_name} · {entry.state}"))
        node.remove_children()
        node.add_leaf(Text(f"File: {entry.spec.path}"))
        node.add_leaf(Text(f"SQL: {entry.spec.quoted_name}"))
        if entry.schema is not None:
            for field in entry.schema:
                node.add_leaf(Text(f"{field.name}: {field.type}"))
        elif entry.issue is not None:
            node.add_leaf(Text(f"Error: {entry.issue.message}"))
        else:
            node.add_leaf("Loading schema…")

    def action_expand_source(self) -> None:
        """Expand the highlighted source, or move into its structure."""
        node = self.cursor_node
        if node is not None and node.allow_expand:
            if node.is_expanded and node.children:
                self.move_cursor(node.children[0])
            else:
                node.expand()

    def action_collapse_source(self) -> None:
        """Collapse the highlighted source, or return to its file item."""
        node = self.cursor_node
        if node is not None:
            if node.is_expanded:
                node.collapse()
            elif node.parent is not None and not node.parent.is_root:
                self.move_cursor(node.parent)
