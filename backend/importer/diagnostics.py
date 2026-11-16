"""User-facing text for DWG import diagnostics, including saved project history."""
from collections import defaultdict


def preview_counts(drawings):
    """Count saved previews separately from extracted drawing units."""
    counts = defaultdict(lambda: [0, 0])
    for drawing in drawings:
        original_id = drawing.get("original_id")
        if original_id:
            counts[original_id][0] += 1
            metadata = drawing.get("metadata") or drawing
            counts[original_id][1] += bool(metadata.get("preview_relative_path"))
    return counts


def is_dwg(name):
    return isinstance(name, str) and name.lower().endswith(".dwg")


def dwg_notice(reason, counts=(0, 0)):
    total, previews = counts
    if not total:
        summary = "暂时无法读取这份 DWG。"
    elif not previews:
        summary = "已提取部分图纸内容，暂无可用预览。"
    elif previews < total:
        summary = f"已生成 {previews} 张预览，另有 {total - previews} 张暂无预览。"
    else:
        summary = f"已生成 {previews} 张预览。"

    reason = str(reason or "")
    missing_block = "缺少块定义" in reason
    missing_reference = "存在外部参照" in reason or "未加载外部文件" in reason
    missing_font = any(text in reason for text in ("缺字体", "缺大字体", "字体缺失", "字体格式不受"))
    substituted = "已用本机" in reason or ("uses explicit fallback font" in reason and "无可用中文字体" not in reason)
    missing_glyph = "中文字形无法渲染" in reason or "无可用中文字体" in reason
    timeout = "超时" in reason
    component_missing = "DWG 解析组件不可用" in reason or "无法启动" in reason

    notes = []
    if not total and component_missing:
        notes.append("本机的 DWG 读取组件无法运行。")
    elif not total and "文件为空" in reason:
        notes.append("文件没有可读取的内容。")
    elif timeout:
        notes.append("读取超时，部分内容还没处理完。" if total else "文件读取超时。")
    elif not total and ("损坏" in reason or "版本不支持" in reason):
        notes.append("文件可能损坏，或当前版本无法打开该文件。")
    if total:
        if missing_reference:
            notes.append("有外部参照未读取，相关图形可能缺失。")
        if missing_block:
            notes.append("有图块的定义未读到，相关图形及受影响图框待核对。")
        if substituted and missing_glyph:
            notes.append("部分文字使用了替代字体，仍有字形无法显示。")
        elif missing_glyph:
            notes.append("部分文字无法显示。")
        elif substituted:
            notes.append("部分文字使用了替代字体，字形和位置可能与原图不同。")
        elif missing_font:
            notes.append("缺少可用字体，部分文字可能显示不全。")
        if "无可靠图框" in reason or "块名不是常见图框" in reason:
            notes.append("部分图框需要在图纸目录中核对。")
        if "文字无法定位" in reason or "CAD 文字精确定位未完成" in reason:
            notes.append("部分文字尚未定位到原图。")
        if not notes:
            notes.append("部分内容可能未读完整。")

    if total and (missing_reference or missing_block or missing_font or substituted or missing_glyph):
        materials = []
        if missing_block:
            materials.append("含完整图块定义的 DWG")
        if missing_reference:
            materials.append("已绑定外部参照的 DWG")
        if missing_font or substituted or missing_glyph:
            materials.append("原制图字体")
        action = f"请制图方补充{'、'.join(materials)}。也可用含相关内容的同版 PDF 核对。"
    elif not total and component_missing:
        action = "请检查 DWG 读取组件，或改用同版 PDF。"
    elif not total and "文件为空" in reason:
        action = "请重新获取原文件后导入。"
    elif timeout:
        action = "可重新导入该文件，或改用同版 PDF。"
    else:
        action = "请在 CAD 中核对原文件，或请制图方提供同版 PDF。"
    return {"summary": summary, "notes": notes, "action": action}


def present_import_diagnostics(result):
    """Add presentation fields without rewriting raw diagnostics or statuses."""
    if not result:
        return result
    counts = preview_counts(result.get("drawings") or [])
    problems = []
    for row in result.get("unreadable", []):
        if row.get("code") == "DWG_INCOMPLETE" or (
                is_dwg(row.get("name")) and row.get("code") in {"FILE_CORRUPT", "EMPTY_FILE"}):
            row = {**row, "notice": dwg_notice(row.get("reason"), counts.get(row.get("original_id"), (0, 0)))}
        problems.append(row)
    projected = {**result, "unreadable": problems}
    if notice := result.get("project_code_notice"):
        visible = {row.get("unit_id") or row.get("id") for row in result.get("drawings") or []}
        evidence = [row for row in notice["evidence"] if row["unit_id"] in visible]
        projected["project_code_notice"] = {**notice, "evidence": evidence} if len(evidence) >= 2 else None
    if isinstance(result.get("files"), list):
        files = []
        for row in result["files"]:
            if (row.get("format") == "dwg" or is_dwg(row.get("name"))) and row.get("status") == "partial":
                total, previews = counts.get(row.get("original_id"), (0, 0))
                label = ("可预览，内容待核对" if previews and previews == total else
                         "部分图纸可预览" if previews else "已提取部分内容")
                row = {**row, "status_label": label}
            files.append(row)
        projected["files"] = files
    return projected
