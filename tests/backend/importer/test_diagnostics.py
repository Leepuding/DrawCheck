from backend.importer.diagnostics import dwg_notice, present_import_diagnostics


def test_historical_dwg_warning_reports_preview_coverage_and_keeps_raw_record():
    reason = "LibreDWG 转换日志含 4906 条警告或错误；INSERT B6880 缺少块定义"
    problem = {"code": "DWG_INCOMPLETE", "name": "样图.dwg", "original_id": "dwg", "reason": reason}
    pdf = {"code": "PDF_INCOMPLETE", "name": "样图.pdf", "reason": "PDF 页面无法读取"}
    snapshot = {
        "unreadable": [problem, pdf],
        "drawings": [{"original_id": "dwg", "metadata": {"preview_relative_path": "preview.png"}},
                     {"original_id": "dwg", "metadata": {}}],
        "files": [{"name": "样图.dwg", "original_id": "dwg", "status": "partial"}],
    }
    projected = present_import_diagnostics(snapshot)
    new = projected["unreadable"][0]
    assert new["notice"]["summary"] == "已生成 1 张预览，另有 1 张暂无预览。"
    assert "图块" in new["notice"]["action"]
    assert "4906" not in str(new["notice"])
    assert new["reason"] == reason and "notice" not in problem
    assert projected["unreadable"][1] == pdf
    assert projected["files"][0]["status"] == "partial"


def test_missing_preview_and_missing_fallback_are_not_described_as_available():
    unreadable = dwg_notice("DWG 解析组件不可用")
    assert "读取组件无法运行" in " ".join(unreadable["notes"])
    assert "同版 PDF" in unreadable["action"]
    text_only = dwg_notice("缺字体；本机无可用中文字体，未能替代，字形与字宽和原图不同", (1, 0))
    assert "暂无可用预览" in text_only["summary"]
    assert text_only["notes"] == ["部分文字无法显示。"]
