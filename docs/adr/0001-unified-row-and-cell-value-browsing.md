---
status: accepted
---

# Browse cells and rows through one value model

On 2026-10-31, we chose a right sidebar for inspecting values in both file tables and query results. Enter in the main table explicitly selects the browse root; moving the cursor does not update the sidebar. A row value is a special Struct whose fields correspond to the row's columns. Cells and rows share one browser, with drill-down navigation and breadcrumbs. Search is outside the initial scope.

This model gives cells and rows the same navigation rules. It requires explicit ownership of the selected root, browse path, and focus so that inspecting nested content remains stable while the table cursor moves.

## Selection and navigation

| Input | Behavior |
| --- | --- |
| Enter in the main table, cell cursor | Select the cell as the browse root and display it. Open the sidebar if necessary, keeping focus in the main table. |
| Enter in the main table, row cursor | Select the row value as the browse root, with the same opening and focus behavior. |
| Enter in the main table, column or none cursor | No effect. Preserve the sidebar's content, path, and visibility. |
| `v` in the table or sidebar | Toggle sidebar visibility without selecting a new root. Opening from the table keeps table focus; closing from the sidebar returns focus to the table. |
| Tab from the main table | Enter the open sidebar's current content. |
| Enter in the sidebar | Enter the selected field or element, replacing the body and updating the breadcrumbs. Scalars are terminal values. |
| Backspace in the sidebar | Return to the parent, restoring its selected item and scroll position. At the root, return focus to the main table. |
| Esc in the sidebar | Return focus to the main table while keeping the sidebar open. |
| Click a sidebar child or breadcrumb ancestor | Enter the child or return to the chosen ancestor. |

Selecting a different browse root with Enter resets the browse path and body scroll position. Moving the cursor, scrolling the table, or changing cursor mode preserves the selected root and its browsing state. Main-table mouse clicks only move the table cursor; clicking the current position again does not substitute for Enter or transfer focus.

The sidebar's current content follows the main table in focus order. Other controls retain their existing focus traversal, and the SQL editor retains Tab indentation and normal `v` input.

The sidebar header has no close button. Hiding and reopening the sidebar with `v` restores the last explicitly selected root, path, and scroll position for that tab. Before any value has been selected, show an empty state: "Press Enter in the table to select a cell or row."

## Layout and presentation

The sidebar shares the table's height. In a query workspace, the SQL editor remains full width above the table and sidebar. Each tab independently retains sidebar visibility and browsing state. The sidebar starts closed and initially occupies about 40% of the viewing area; validate the proportion in a prototype.

Maximize the table and sidebar together as one viewing area, preserving browsing state when restoring the layout. Maximizing the SQL editor still shows only the editor.

Use the visual conventions of a JSON viewer: braces and indentation for Struct and row values, prominent field names, subdued type information, explicit List indices, and type-colored values. Container children show a structural summary, type, and field or element count. Highlight the selected item's text through its trailing comma, when present. Hover shades the full row underneath the selection. Keep borders, spacing, and colors consistent with the Parqx theme.

Each container view shows only its direct children. Enter replaces that view with the selected child's contents. This visual language must preserve the input's types, precision, order, and duplicate fields; it does not require converting values to JSON.

Display Map values as ordered entries with key and value previews. Entering an entry exposes separate `key` and `value` fields that can each be explored. Preserve entry order and duplicate keys. An example path is `attributes > [2] > value`.

Scalar views keep the name, type, necessary units, and breadcrumbs above a complete, read-only, scrollable body. Preserve original line breaks and wrap long lines. Distinguish NULL, empty strings, and empty containers explicitly.

| Scalar type | Presentation |
| --- | --- |
| Integer and Decimal | Complete numeric value, preserving precision and Decimal scale. |
| Floating point | A representation that round-trips to the value, with distinct NaN, positive and negative infinity, and negative zero. |
| Boolean | Type-colored `true` or `false`. |
| Date, time, and timestamp | Readable formatting that preserves the precision and time-zone information supplied by the read layer. |
| Duration and Interval | Explicit units or component values. |
| String | Original text, including line breaks. JSON-looking strings remain strings rather than becoming navigable structures. |
| Binary | Hexadecimal bytes with offsets and a printable-character preview, adapting bytes per line to sidebar width. |

The component investigation identified [OptionList with Rich renderables](https://textual.textualize.io/widgets/option_list/#options-as-rich-renderables) and a [read-only TextArea](https://textual.textualize.io/widgets/text_area/#read-only-mode) as prototype candidates for child selection and scalar reading. Final colors, density, selection styling, long-text behavior, and breadcrumb overflow require visual validation in Textual.

## Result changes and large values

A successful query invalidates the previous result's browse root, path, and body scroll position. Preserve sidebar visibility and the current focus area. If the new result has rows, prompt for an Enter selection; if it has no rows, show an empty-result state. The new table's initial cursor never selects a value automatically. Failed execution preserves the previous result and selected value. Editing the SQL draft changes neither.

Child previews may be truncated, but entering a value must make its complete contents reachable through scrolling. Process large values on demand, show loading state, and keep returning or selecting another root responsive. Moving the table cursor alone does not cancel loading of the selected value. Late results for an earlier root must not overwrite a later selection. The initial scope excludes search, manual pagination, and expand-all controls.

Opening without moving focus, restoring selected content with `v`, and clearing the selection after a successful query are defaults derived from the explicit-selection model and existing focus rules. Complete access and large-value responsiveness remain prototype validation targets; the existing bounded table-preview formatter is insufficient as the detail reader.

## Data boundary

Type and precision preservation apply to the Arrow values supplied by the existing read layer. File reads through DuckDB can convert time units, decode dictionaries, and normalize column names; unsupported Decimal precision is rejected. The detail view must preserve the values it receives and cannot reconstruct information already lost during reading. Changing file-read semantics is a separate decision.

Accepted status records the agreed design direction. It does not imply that the feature is implemented or its performance has been validated.
