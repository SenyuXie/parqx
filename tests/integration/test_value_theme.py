"""Theme changes reach the mounted value browser and its rendered content."""

import pyarrow as pa
from rich.color import Color as RichColor
from textual.app import App, ComposeResult
from textual.color import Color
from textual.strip import Strip
from textual.widgets import Button

from parqx.tui.widgets.arrow_table import ArrowTable
from parqx.tui.widgets.value_browser import BrowseRoot, ValueBrowser


def _luminance(color: RichColor) -> float:
    channels = [channel / 255 for channel in color.get_truecolor()]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(
        weight * channel
        for weight, channel in zip((0.2126, 0.7152, 0.0722), linear, strict=True)
    )


def _assert_readable(line: Strip) -> None:
    for segment in line:
        if not segment.text.strip():
            continue
        style = segment.style
        assert style is not None
        assert style.color is not None
        assert style.bgcolor is not None
        dark, light = sorted((_luminance(style.color), _luminance(style.bgcolor)))
        contrast = (light + 0.05) / (dark + 0.05)
        assert contrast >= 4.5, (segment.text, style.color, style.bgcolor, contrast)


async def test_theme_switch_updates_browser_surfaces_and_interaction_states() -> None:
    table = ArrowTable(pa.table({"count": [42]}))

    class BrowserApp(App[None]):
        CSS = "ValueBrowser { width: 60; }"

        def compose(self) -> ComposeResult:
            yield ValueBrowser()
            yield table

    app = BrowserApp()
    async with app.run_test(size=(80, 30)) as pilot:
        browser = app.query_one(ValueBrowser)
        browser.select(
            BrowseRoot(
                app,
                0,
                0,
                0,
                "record",
                pa.scalar(
                    {
                        "name": "Ada\n" * 60,
                        "count": 42,
                        "enabled": True,
                        "missing": None,
                        **{f"extra_{index}": index for index in range(30)},
                    }
                ),
            )
        )
        options = browser.options
        appearances: list[tuple[RichColor | None, ...]] = []
        for theme in ("textual-light", "textual-dark", "dracula"):
            app.theme = theme
            table.focus()
            await pilot.hover(table)
            await pilot.pause()
            variables = app.get_css_variables()
            surface = Color.parse(variables["surface"])
            for widget in (browser, options, browser.reader):
                assert widget.rich_style.bgcolor == surface.rich_color
            for scroll_view in (browser.crumbs, options, browser.reader):
                assert scroll_view.styles.scrollbar_color == Color.parse(
                    variables["scrollbar"]
                )
                assert scroll_view.styles.scrollbar_background == Color.parse(
                    variables["scrollbar-background"]
                )
            assert options.show_vertical_scrollbar
            active_cursor = table.get_component_rich_style("arrowtable--cursor")
            blurred_line = options.render_line(1)
            blurred = next(iter(blurred_line)).style
            options.focus()
            await pilot.pause()
            inactive_cursor = table.get_component_rich_style("arrowtable--cursor")
            focused_line = options.render_line(1)
            focused = next(iter(focused_line)).style
            for line, cursor in (
                (focused_line, active_cursor),
                (blurred_line, inactive_cursor),
            ):
                _assert_readable(line)
                assert all(
                    segment.style and segment.style.bgcolor == cursor.bgcolor
                    for segment in line
                )
            assert await pilot.hover(options, offset=(10, 2))
            hovered = next(iter(options.render_line(2))).style
            assert blurred is not None
            assert focused is not None
            assert hovered is not None
            assert len({blurred.bgcolor, focused.bgcolor, hovered.bgcolor}) == 3
            assert surface.rich_color not in {
                blurred.bgcolor,
                focused.bgcolor,
                hovered.bgcolor,
            }
            appearances.append(
                (surface.rich_color, blurred.bgcolor, focused.bgcolor, hovered.bgcolor)
            )

            # Unselected rows use a different foreground for each value type.
            await pilot.hover(table)
            options.action_move(4)
            colors: set[RichColor | None] = set()
            for row in range(1, 5):
                for segment in options.render_line(row):
                    if segment.style is not None and (
                        segment.text.startswith('"Ada')
                        or segment.text in {"42", "true", "null"}
                    ):
                        colors.add(segment.style.color)
                _assert_readable(options.render_line(row))
            assert len(colors) == 4
            for row in range(1, 5):
                assert await pilot.hover(options, offset=(10, row))
                _assert_readable(options.render_line(row))
            for focus_target in (table, options):
                focus_target.focus()
                await pilot.pause()
                options.action_first()
                for row in range(1, 5):
                    _assert_readable(options.render_line(row))
                    options.action_move(1)
            await pilot.press("home", "enter")
            assert browser.reader.text == "Ada\n" * 60
            assert browser.reader.show_vertical_scrollbar
            scalar_style = next(iter(browser.reader.render_line(0))).style
            assert scalar_style is not None
            assert scalar_style.bgcolor == browser.rich_style.bgcolor
            assert scalar_style.bgcolor != surface.rich_color
            _assert_readable(browser.reader.render_line(0))
            assert await pilot.click(browser.query_one(".value-crumb", Button))
            assert options.display
            assert options.highlighted == 1
        for state in zip(*appearances, strict=True):
            assert len(set(state)) == 3
