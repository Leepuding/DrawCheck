# Local BIGFONT adapter provenance

`backend/parsers/dwg/fonts.py` is an in-repository Python adapter for locally installed SHX fonts. It reads fonts in place; no SHX files are distributed with this code.

The BIGFONT index layout (u16 record size/count/range count, pairs of u16 lead-byte bounds, u16 code/u16 length/u32 absolute offset), optional glyph prefixes and ordinary/extended subshape distinction were studied from:

- https://github.com/mlightcad/shx-parser/blob/main/src/contentParser.ts
- https://github.com/mlightcad/shx-parser/blob/main/src/shapeParser.ts
- https://help.autodesk.com/cloudhelp/2025/ENU/AutoCAD-Customization/files/GUID-DE0CCC57-AC55-4CDC-887E-730FC90364E4.htm

The Python implementation reuses the installed ezdxf 1.4.5 `ShapeRenderer`, `parse_shape_codes`, `GlyphCache`, `_CachedFont` and `UnifiedTextRenderer`; it does not modify dependency files. Ordinary subshapes are inlined before ezdxf execution to retain location-stack/pen/scale state. GBK conversion is explicit and checked against declared lead-byte ranges and actual index entries. Unknown encodings, extended subshape placement, unbalanced stacks, missing characters and vertical styles fail closed. Complex MTEXT font runs use an explicitly diagnosed fallback. This does not assert that locally installed fonts have the same bytes as the designer's fonts or provide native CAD comparison.

mlightcad/shx-parser license follows (`font-shx-parser-LICENSE.txt`). ezdxf is MIT licensed, Copyright (c) 2010-2025 Manfred Moitzi, and remains an installed runtime dependency with its original license.

## CJK MTEXT layout adapter

The MTEXT layout construction in `render.py:_cjk_mtext_renderer` is adapted from ezdxf 1.4.5 `ezdxf/render/abstract_mtext_renderer.py`, MIT, Copyright (c) 2021-2025 Manfred Moitzi. The copied MIT notice is preserved in `font-ezdxf-LICENSE.txt`. The original word-token layout treats unspaced Chinese sentences as single indivisible cells. The adapter adds zero-width break opportunities between Chinese characters, keeps Latin model/unit tokens intact, preserves parser contexts/explicit paragraphs/columns/attachment/rotation, and measures cells with the exact same text engine used to draw them. It does not change the original DXF/MTEXT contents. This is horizontal CJK wrapping within the original MTEXT column width; it is not a vertical-font implementation or a claim of a complete Unicode line-breaking implementation.
