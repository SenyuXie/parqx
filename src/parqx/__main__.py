"""Parqx: A Parquet TUI inspector."""

from parqx.cli import app

# The content of `__main__.py` typically isn't fenced with an `if __name__ == '__main__'` block.
# Reference: https://docs.python.org/3.12/library/__main__.html#id1
app(prog_name="parqx")
