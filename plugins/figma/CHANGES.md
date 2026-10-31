# Figma: changes

## 0.2.0 — 2026-10-31

- A `.fig` is entered like a folder: one `.svg` a page, in the order the tabs
  are in. F3 draws the page under the cursor, the strip walks them, and a page
  copies out as a real SVG.

## 0.1.0 — 2026-10-31

- Figma designs and FigJam boards: a page drawn from its own shapes, the
  preview Figma saved, and a listing of what the file holds.
- The three formats a `.fig` is made of, including a zstd reader, because the
  Python the application ships has none.
