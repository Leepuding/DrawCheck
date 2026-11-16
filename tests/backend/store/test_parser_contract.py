"""解析器仅是协议，能够描述后续解析的输入与归一化输出。"""
from backend.parsers.base import ParseInput, ParsedText, ParsedUnit, ParseResult, Parser
from backend.store import BBox, Source


def test_parser_contract_is_protocol_only(tmp_path):
    assert Parser._is_protocol
    request = ParseInput("原图标识", tmp_path / "只读副本", "a" * 64, tmp_path / "cache", "pdf")
    text = ParsedText("2400", Source.PDF_TEXT, BBox(0, 0, 1, 1))
    result = ParseResult([ParsedUnit("pdf_page", 1, "T0105(二）", "T0105", [text])], [])
    assert request.format == "pdf"
    assert result.units[0].texts[0].source == Source.PDF_TEXT
    assert result.incomplete == []
