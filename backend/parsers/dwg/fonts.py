"""Local SHX + GBK BIGFONT strokes, without copying or redistributing font files.

BIGFONT table/prefix/subshape interpretation references mlightcad/shx-parser (MIT).
See docs/third-party/font-bigfont.md. Unknown encodings/instructions fail closed.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import math
from pathlib import Path
import struct

from ezdxf import path
from ezdxf.fonts import fonts, shapefile
from ezdxf.fonts.glyphs import Glyphs
from ezdxf.math import Matrix44
from ezdxf.npshapes import NumpyPath2d


class FontUnsupported(ValueError):
    pass


# Read installed fonts in place. No font files are bundled, downloaded, or copied.
LOCAL_FONT_DIRS = (
    Path('/Applications/gstarcad.app/Contents/Resources/Fonts'),
    Path('C:/Program Files/Autodesk/AutoCAD 2025/Fonts'),
)


def exact_font_path(name: str) -> Path | None:
    requested = Path(name)
    if requested.is_file():
        return requested.resolve()
    if fonts.font_manager.has_font(name):
        try:
            result = Path(fonts.font_manager._font_cache.get(name, name).file_path)
            if result.is_file():
                return result.resolve()
        except (KeyError, AttributeError):
            pass
    basename = name.replace('\\', '/').rsplit('/', 1)[-1].casefold()
    for directory in LOCAL_FONT_DIRS:
        if directory.is_dir():
            for candidate in directory.iterdir():
                if candidate.name.casefold() == basename and candidate.is_file():
                    return candidate.resolve()
    return None


class Bigfont:
    """Strict horizontal GBK adapter for AutoCAD-86 BIGFONT 1.0.

    Character codes are GBK bytes, never Unicode code points. Extended subshape
    placement and vertical layout intentionally raise until independently verified.
    """
    def __init__(self, data: bytes):
        signature = b'AutoCAD-86 bigfont 1.0\r\n\x1a'
        if not data.startswith(signature):
            raise FontUnsupported('unsupported BIGFONT header')
        self.data = data
        start = len(signature)
        try:
            size, count, ranges = struct.unpack_from('<HHH', data, start)
            if size != 8 or not count or count > 65535:
                raise FontUnsupported('invalid BIGFONT index')
            index = start + 6 + ranges * 4
            if index + count * 8 > len(data):
                raise FontUnsupported('truncated BIGFONT index')
            self.ranges = [struct.unpack_from('<HH', data, start + 6 + i*4) for i in range(ranges)]
            self.entries = {}
            for i in range(count):
                code, length, offset = struct.unpack_from('<HHI', data, index + i*8)
                if not (code or length or offset):
                    continue  # unused hash-table slot
                if not length or offset < index + count*8 or offset+length > len(data):
                    raise FontUnsupported('invalid BIGFONT entry bounds')
                if code in self.entries:
                    raise FontUnsupported('duplicate BIGFONT character code')
                self.entries[code] = data[offset:offset+length]
            info = self.entries[0].split(b'\0', 1)[1]
            if len(info) not in (3, 4, 5) or info[0] == 0:
                raise FontUnsupported('unsupported BIGFONT metrics')
            self.above, self.below, mode = info[:3]
            if mode not in (0, 2):
                raise FontUnsupported('vertical BIGFONT metrics')
            self._cache = {}
        except (struct.error, KeyError, IndexError) as exc:
            raise FontUnsupported('malformed BIGFONT') from exc

    def code_for(self, char: str) -> int:
        try:
            encoded = char.encode('gbk')
        except UnicodeEncodeError as exc:
            raise FontUnsupported(f'not GBK: U+{ord(char):04X}') from exc
        if len(encoded) != 2 or not any(a <= encoded[0] <= b for a, b in self.ranges):
            raise FontUnsupported(f'not in BIGFONT lead-byte ranges: U+{ord(char):04X}')
        code = int.from_bytes(encoded, 'big')
        if code not in self.entries:
            raise FontUnsupported(f'missing BIGFONT glyph: U+{ord(char):04X}')
        return code

    def _codes(self, code, active=()):
        if code in active or len(active) >= 16:
            raise FontUnsupported('recursive BIGFONT subshape')
        try:
            raw = self.entries[code]
        except KeyError as exc:
            raise FontUnsupported(f'missing BIGFONT subshape {code}') from exc
        # SHX entries contain an optional ASCII shape name followed by NUL.
        # Some BIGFONT variants prefix parent glyphs by their big-endian code.
        prefix = code.to_bytes(2, 'big')
        if code > 255 and raw.startswith(prefix):
            raw = raw[2:]
            if raw.startswith(b'\0'):
                raw = raw[1:]
        else:
            try:
                raw = raw.split(b'\0', 1)[1]
            except IndexError as exc:
                raise FontUnsupported('missing BIGFONT glyph name terminator') from exc
        try:
            codes = shapefile.parse_shape_codes(shapefile.DataReader(raw))
        except (IndexError, struct.error, EOFError) as exc:
            raise FontUnsupported('truncated BIGFONT bytecode') from exc
        # Decode the already typed stream by command arity. Inline ordinary
        # subshapes so ezdxf retains the same pen, vector scale and location stack.
        result, index = [], 0
        while index < len(codes):
            op = codes[index]; index += 1
            if op == 0:
                return tuple(result)
            if op == 15:
                raise FontUnsupported('unknown BIGFONT opcode 15')
            if op == 14 and index < len(codes) and codes[index] == 7:
                raise FontUnsupported('vertical-only BIGFONT subshape not verified')
            if op == 7:
                child = codes[index]; index += 1
                if child == 0:
                    raise FontUnsupported('extended BIGFONT subshape placement not verified')
                result.extend(self._codes(child, active + (code,)))
                continue
            result.append(op)
            count = {3: 1, 4: 1, 8: 2, 10: 2, 11: 5, 12: 3}.get(op, 0)
            if op in (9, 13):
                while True:
                    x, y = codes[index:index+2]; index += 2
                    result.extend((x, y))
                    if x == y == 0:
                        break
                    if op == 13:
                        result.append(codes[index]); index += 1
            else:
                result.extend(codes[index:index+count]); index += count
        raise FontUnsupported('unterminated BIGFONT glyph')

    def glyph(self, char):
        code = self.code_for(char)
        if code not in self._cache:
            codes = (2,) + self._codes(code) + (0,)
            renderer = shapefile.ShapeRenderer(path.Path(), lambda _: codes)
            try:
                renderer.render(code)
            except (IndexError, ZeroDivisionError, shapefile.StackUnderflow, AssertionError) as exc:
                raise FontUnsupported('invalid BIGFONT drawing state') from exc
            if renderer._location_stack:
                raise FontUnsupported('unbalanced BIGFONT location stack')
            glyph = NumpyPath2d(renderer.p)
            if not math.isfinite(glyph.end.x) or glyph.end.x <= 0:
                raise FontUnsupported('BIGFONT glyph has no positive advance')
            if not char.isspace() and len(glyph) == 0:
                raise FontUnsupported('BIGFONT glyph has no strokes')
            self._cache[code] = glyph
        return self._cache[code].clone()


class CompositeGlyphs(Glyphs):
    def __init__(self, primary: Path, big: Path | None):
        self.primary = shapefile.readfile(str(primary))
        self.base = shapefile.GlyphCache(self.primary)
        self.big = Bigfont(big.read_bytes()) if big else None
        self.font_measurements = self.base.font_measurements
        self.space_width = self.base.space_width
        self._primary_glyphs = {}

    def get_scaling_factor(self, height):
        return height / self.font_measurements.cap_height

    def glyph(self, char):
        if ord(char) in self.primary.shapes:
            code = ord(char)
            if code not in self._primary_glyphs:
                # GlyphCache substitutes missing subshapes with tofu and ignores
                # stack underflow; neither behavior is evidence of font support.
                def get_codes(number):
                    if number not in self.primary.shapes:
                        raise FontUnsupported(f'missing primary SHX subshape {number}')
                    return self.primary.get_codes(number)
                renderer = shapefile.ShapeRenderer(path.Path(), get_codes)
                try:
                    renderer.render(code)
                except (shapefile.StackUnderflow, IndexError, ZeroDivisionError,
                        RecursionError, AssertionError) as exc:
                    raise FontUnsupported('invalid primary SHX glyph') from exc
                if renderer._location_stack:
                    raise FontUnsupported('unbalanced primary SHX location stack')
                glyph = NumpyPath2d(renderer.p)
                if not char.isspace() and len(glyph) == 0:
                    raise FontUnsupported('primary SHX glyph has no strokes')
                if not all(math.isfinite(v) for point in glyph.control_vertices() for v in point):
                    raise FontUnsupported('primary SHX glyph has non-finite geometry')
                self._primary_glyphs[code] = glyph
            return self._primary_glyphs[code].clone()
        if self.big is None:
            raise FontUnsupported(f'missing SHX glyph U+{ord(char):04X}')
        glyph = self.big.glyph(char)
        factor = self.font_measurements.cap_height / self.big.above
        glyph.transform_inplace(Matrix44.scale(factor, factor, 1))
        return glyph

    def get_text_length(self, text, cap_height, width_factor=1):
        return sum(self.glyph(c).end.x for c in text) * self.get_scaling_factor(cap_height) * width_factor

    def get_text_glyph_paths(self, text, cap_height=1, width_factor=1):
        sy = self.get_scaling_factor(cap_height)
        sx = sy * width_factor
        result, cursor = [], 0.0
        for char in text:
            glyph = self.glyph(char)
            advance = glyph.end.x
            matrix = Matrix44.scale(sx, sy, 1)
            matrix[3, 0] = cursor
            glyph.transform_inplace(matrix)
            if char != ' ':
                result.append(glyph)
            cursor += advance*sx
        return result


class LocalShapeFont(fonts._CachedFont):
    font_render_type = fonts.FontRenderType.STROKE

    def __init__(self, cache):
        self._local_cache = cache
        super().__init__('drawcheck-local-shx', 1.0)

    def create_cache(self, name):
        return self._local_cache


@lru_cache(maxsize=64)
def _load_font(primary, primary_mtime, big, big_mtime):
    try:
        return LocalShapeFont(CompositeGlyphs(Path(primary), Path(big) if big else None))
    except (shapefile.ShapeFileException, IndexError, ZeroDivisionError, struct.error) as exc:
        raise FontUnsupported(f'cannot load local SHX: {type(exc).__name__}') from exc


def font_for_style(style):
    """Exact local stroke font for width + rendering; None for non-SHX styles."""
    name = style.dxf.get('font', '')
    if Path(name).suffix.lower() not in ('.shx', '.shp'):
        return None
    if style.dxf.get('flags', 0) & 4:
        raise FontUnsupported('vertical text style not verified')
    primary = exact_font_path(name)
    big_name = style.dxf.get('bigfont', '')
    big = exact_font_path(big_name) if big_name else None
    if primary is None:
        raise FontUnsupported('local primary font file missing')
    # A STYLE can declare a secondary font that none of its text uses.
    # With only the primary font loaded, glyph() still rejects every character
    # that would require the missing file.
    return _load_font(str(primary), primary.stat().st_mtime_ns, str(big) if big else '', big.stat().st_mtime_ns if big else 0)


def style_text_width(style, text, height, width_factor=1):
    font = font_for_style(style)
    if font is None:
        return None
    return font.text_width_ex(text, height, width_factor)


def font_evidence(style):
    result = []
    for slot in ('font', 'bigfont'):
        name = style.dxf.get(slot, '')
        local = exact_font_path(name) if name else None
        if local:
            result.append({'slot': slot, 'name': local.name, 'path': str(local),
                           'sha256': hashlib.sha256(local.read_bytes()).hexdigest()})
    return result
