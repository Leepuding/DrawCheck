"""Two-file reports with immutable source content and explicit incomplete coverage."""
from __future__ import annotations
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Rectangle, FreeText
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from backend.importer.diagnostics import present_import_diagnostics
from backend.store import Repository, StoreError, ErrorCode
from backend.store.paths import safe_path, ensure_writable
from backend.api.review_status import serialize_issue, resolve_unit_image
from backend.jobs.codes import CAT_OUT_OF_SCOPE


def report_font():
    """Embed a locally installed CJK TrueType subset; never rely on viewer language packs."""
    name = 'DrawCheckCJK'
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    candidates = [os.environ.get('DRAWCHECK_REPORT_FONT', ''),
        str(Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/'simsun.ttc'),
        str(Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/'msyh.ttc'),
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/Library/Fonts/Arial Unicode.ttf',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/arphic/uming.ttc']
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            font = TTFont(name, candidate, subfontIndex=0)
            if not all(ord(ch) in font.face.charToGlyph for ch in '图纸审查汇总'):
                continue
            pdfmetrics.registerFont(font)
            return name
        except Exception:
            continue
    raise StoreError(ErrorCode.INVALID_DATA, '缺少可嵌入的中文字体，暂时无法生成中文报告')


def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:100] or '图纸审查'
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}:
        name = '_' + name
    return name


def text_cell(cell, value):
    # Even values beginning =,+,-,@ are literal source evidence, never formulas.
    cell.value = str(value or '')
    cell.data_type = 's'


def page_box(page, box):
    """Map top-left normalized full-page render coordinates back to PDF user space."""
    w, h = float(page.mediabox.width), float(page.mediabox.height)
    ox, oy = float(page.mediabox.left), float(page.mediabox.bottom)
    rotation = page.rotation % 360
    points = []
    for x, y in ((box['x0'], box['y0']), (box['x1'], box['y1'])):
        if rotation == 90: px, py = y*w, x*h
        elif rotation == 180: px, py = (1-x)*w, y*h
        elif rotation == 270: px, py = (1-y)*w, (1-x)*h
        else: px, py = x*w, (1-y)*h
        points.append((px+ox, py+oy))
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


class ExportService:
    def __init__(self, repo: Repository, *, output_root: Path | None = None):
        self.repo = repo
        self.output_root = Path(output_root or os.environ.get('DRAWCHECK_EXPORT_DIR') or Path.home()/'Documents'/'图纸审查')

    def _snapshot(self, version_id):
        version = self.repo.get('drawing_versions', version_id)
        project = self.repo.get('projects', version['project_id'])
        tasks = self.repo.list_records('review_tasks', version_id=version_id)
        if not tasks:
            raise StoreError(ErrorCode.INVALID_DATA, '请先开始审查，再导出已有结果')
        task = tasks[-1]
        if task['status'] in ('待开始', '进行中'):
            raise StoreError(ErrorCode.INVALID_DATA, '审查进行中，请稍后导出')
        units = {u['id']:u for u in self.repo.list_records('drawing_units', version_id=version_id)}
        originals = {o['id']:o for o in self.repo.list_records('original_files', version_id=version_id)}
        manifest = safe_path(self.repo.root, f"projects/{project['id']}/cache/import-{version_id}.json")
        import_rows = json.loads(manifest.read_text()).get('unreadable', []) if manifest.exists() else []
        issues = self.repo.list_records('issues', version_id=version_id)
        incomplete = []
        scope_unit_ids = set()
        def add(label, reason):
            row = (str(label or '本次审查'), str(reason or '审查失败'))
            if row not in incomplete: incomplete.append(row)
        from backend.pending.current import current_incomplete, current_pending
        for row in current_incomplete(self.repo, version_id, task.get('incomplete') or []):
            if row.get('category') == CAT_OUT_OF_SCOPE:
                scope_unit_ids.add(row.get('unit_id'))
                continue
            unit = units.get(row.get('unit_id')) or {}
            original = originals.get(row.get('original_id')) or {}
            reason = row.get('reason')
            # The job prefixes the same manifest diagnostic with its filename.
            # Match its original ID and complete literal; never merge different
            # diagnostics merely because they belong to the same source file.
            for imported in import_rows:
                imported_reason = imported.get('reason') or '读不了'
                if (original and imported.get('original_id') == row.get('original_id')
                        and reason == f"{imported.get('name', '')}：{imported_reason}"):
                    reason = imported_reason
                    break
            add(original.get('name') or unit.get('drawing_number') or unit.get('name'), reason)
        pending = current_pending(self.repo, version_id)
        for row in pending:
            if row['status'] != '已确认':
                unit = units.get(row['unit_id'], {})
                add(unit.get('drawing_number') or unit.get('name'), '待你确认未填' if row['status']=='待确认' else '无法确定')
        for unit in units.values():
            meta = unit.get('metadata') or {}
            if unit['id'] in scope_unit_ids or meta.get('out_of_scope') or meta.get('review_scope') == 'out':
                incomplete.append((unit['drawing_number'] or unit['name'], '不在审查范围'))
        for row in import_rows:
            original = originals.get(row.get('original_id')) or {}
            add(original.get('name') or row.get('name'), row.get('reason') or '读不了')
        if task['status'] in ('失败', '暂停') and not incomplete:
            add(project['name'], '超费用上限未审' if task['status']=='暂停' else '审查失败')
        # Result projection supplies only differences and coverage, never matching rows.
        from backend.api.review_status import list_issues
        result = list_issues(self.repo, version_id)
        from backend.api.review_status import serialize_unit_declarations
        declarations = (result["unit_declarations"]
                        + [e for row in result["items"] for e in row["finding"]["unit_declarations"]]
                        + [e for row in result["comparison_details"] for e in row.get("unit_declarations", [])]
                        + serialize_unit_declarations([e for row in pending for e in row.get("unit_declarations", [])], units))
        declarations = list({(e["unit_id"], e["category"], e["unit"], e["text"]): e for e in declarations}.values())
        visible_ids = {row["id"] for row in result["items"]}
        issues = [row for row in issues if row["id"] in visible_ids]
        task = {**task, "verification_boundary": result["verification"], "unit_declarations": declarations,
                "export_counts": {key:result[key] for key in
                    ("incomplete_count", "unreadable_count", "pending_count", "out_of_scope_count")}}
        absent = [row["category"] for row in result["comparison_summary"] if not row["verified"]]
        if absent:
            add("核验边界", "尚未形成有效对照：" + "、".join(absent))
        for row in result["comparison_details"]:
            label = row["label"] if row.get("shared_block_mark") else row["category"]
            add(label, row.get("reason") or row.get("basis") or "未形成可信对照")
        presented = present_import_diagnostics({"drawings": units.values(), "unreadable": import_rows})
        summaries = {}
        for row in presented["unreadable"]:
            if notice := row.get("notice"):
                original = originals.get(row.get("original_id")) or {}
                key = (original.get("name") or row.get("name"), row.get("reason") or "读不了")
                summaries[key] = " ".join([notice["summary"], *notice["notes"], notice["action"]])
        incomplete = [(label, summaries.get((label, reason), reason)) for label, reason in incomplete]
        return version, project, task, units, originals, issues, incomplete

    def generate(self, version_id):
        from backend.pending.current import current_pending
        version, project, task, units, originals, issues, incomplete = self._snapshot(version_id)
        before = self.repo.verify_originals(version_id)
        directory = self.output_root / safe_name(project['name'])
        ensure_writable(directory)
        internal = safe_path(self.repo.root, f"projects/{project['id']}/exports/{uuid4().hex}")
        ensure_writable(internal)
        base = safe_name(project['name'])
        names = [f'{base}-标注图纸.pdf', f'{base}-问题清单.xlsx']
        if any((directory/n).exists() for n in names):
            suffix = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            names = [f'{base}-标注图纸-{suffix}.pdf', f'{base}-问题清单-{suffix}.xlsx']
        pdf_path, xlsx_path = [internal/n for n in names]
        # Write both successfully before publishing either file.
        self._pdf(pdf_path, project, task, units, issues, incomplete)
        self._xlsx(xlsx_path, task, units, originals, issues, incomplete)
        if before != self.repo.verify_originals(version_id):
            raise StoreError(ErrorCode.ORIGINAL_CHANGED, '导出期间原图发生变化')
        published = []
        try:
            for source in (pdf_path, xlsx_path):
                target = directory/source.name
                with target.open('xb') as out:
                    published.append(target)
                    with source.open('rb') as src:
                        shutil.copyfileobj(src, out)
        except OSError:
            for target in published: target.unlink(missing_ok=True)
            raise
        records = []
        for source in (pdf_path, xlsx_path):
            row = self.repo.record_export(task['id'], format=source.suffix[1:], relative_path=source.relative_to(self.repo.root).as_posix())
            records.append({'id':row['id'], 'name':source.name, 'url':f"/api/exports/{row['id']}/download"})
            # Destination mapping is local only, never accepted from HTTP callers.
            source.with_suffix(source.suffix+'.destination.json').write_text(json.dumps({'path':str(directory/source.name)},ensure_ascii=False))
        return {'files':records, 'pending_count':sum(1 for x in current_pending(self.repo, version_id) if x['status']!='已确认'), 'message':'已导出 2 个文件'}

    def _pdf(self, path, project, task, units, issues, incomplete):
        font = report_font()
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=(595,842))
        c.setFont(font, 20); c.drawString(42,790,'图纸一致性审查汇总')
        c.setFont(font, 11)
        counts = Counter(i['review_status'] for i in issues if i.get('comparison_tag') != '已解决')
        tolerance = task['tolerance_snapshot']
        export_counts = task.get('export_counts') or {}
        incomplete_count = (export_counts.get('incomplete_count', 0) + export_counts.get('unreadable_count', 0)
                            if export_counts else sum(label != '核验边界' for label, _ in incomplete))
        lines = [f"项目：{project['name']}", f"导出时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
                 f"图纸数：{len(units)}；问题数：{sum(counts.values())}",
                 '；'.join(f'{k}：{counts.get(k,0)}' for k in ('疑似','已确认','排除','需补充资料')),
                 f"未完成与读不了：{incomplete_count} 项，不代表这些图没有问题",
                 f"待你确认：{export_counts.get('pending_count', 0)} 项；不在审查范围：{export_counts.get('out_of_scope_count', 0)} 项",
                 f"本次容差：尺寸 {tolerance.get('dimension_tolerance_mm')} 毫米；标高 {tolerance.get('elevation_tolerance_m')} 米",
                 '本报告供人工复核，不代表完整性或工程合规结论。']
        boundary = task.get('verification_boundary') or {}
        if not any(i['review_status'] != '排除' and i.get('comparison_tag') != '已解决' for i in issues):
            lines.append(boundary.get('zero_difference_message', '尚未形成有效对照'))
        lines.append(boundary.get('scope_note', '未核验部分不能据此判断一致。'))
        scope = next((reason for label, reason in incomplete if label == '核验边界'), None)
        if scope:
            lines.append(scope)
        from backend.pending.current import INVALID_LOCATION_REASON
        lines.extend(f"{label}：{reason}" for label, reason in incomplete
                     if reason.startswith(INVALID_LOCATION_REASON))
        y = 745
        for line in lines:
            segment = ''
            for char in line:
                if segment and pdfmetrics.stringWidth(segment + char, font, 11) > 510:
                    if y < 60:
                        c.showPage(); c.setFont(font, 11); y = 790
                    c.drawString(42, y, segment); y -= 18; segment = ''
                segment += char
            if segment:
                if y < 60:
                    c.showPage(); c.setFont(font, 11); y = 790
                c.drawString(42, y, segment)
            y -= 28
        c.showPage(); c.save()
        writer = PdfWriter(); writer.append(PdfReader(buffer))
        marks = {}
        for index, issue in enumerate(issues,1):
            if issue['review_status']=='排除' or issue.get('comparison_tag')=='已解决': continue
            proof = serialize_issue(issue, units)["finding"]
            for side in ('left','right'):
                unit_id = issue[f'{side}_unit_id']
                box = proof[side]["bbox"]
                if box is None:
                    continue
                mark = (index,box,proof['reason'])
                if mark not in marks.setdefault(unit_id,[]): marks[unit_id].append(mark)
        for unit_id, boxes in marks.items():
            unit = units[unit_id]
            if unit['kind']=='pdf_page':
                reader = PdfReader(self.repo.original_path(unit['original_id']))
                writer.add_page(reader.pages[unit['ordinal']-1])
            else:
                image = resolve_unit_image(self.repo,unit_id)
                from PIL import Image
                with Image.open(image) as im: w,h = im.size
                # Keep the exact image aspect ratio; DWG note is an annotation outside evidence boxes.
                page_buf = io.BytesIO(); drawing = canvas.Canvas(page_buf,pagesize=(w,h))
                drawing.drawImage(str(image),0,0,width=w,height=h)
                drawing.setFont(font,max(10,w/100)); drawing.drawString(10,10,'由 DWG 渲染，非原 PDF')
                drawing.showPage(); drawing.save(); writer.add_page(PdfReader(page_buf).pages[0])
            page = writer.pages[-1]
            for number, box, reason in boxes:
                rect = page_box(page,box)
                annotation = Rectangle(rect=rect)
                from pypdf.generic import NameObject, ArrayObject, FloatObject, TextStringObject
                annotation[NameObject('/C')] = ArrayObject([FloatObject(1),FloatObject(0),FloatObject(0)])
                annotation[NameObject('/Contents')] = TextStringObject(f'DC-{number:03d}')
                annotation[NameObject('/Subj')] = TextStringObject(reason)
                writer.add_annotation(len(writer.pages)-1,annotation)
                label = FreeText(text=f'DC-{number:03d}',rect=(rect[0],rect[3],rect[0]+65,rect[3]+14),font_size='9pt',font_color='CC0000',border_color=None,background_color='FFFFFF')
                writer.add_annotation(len(writer.pages)-1,label)
        writer.write(path)

    def _xlsx(self,path,task,units,originals,issues,incomplete):
        book=Workbook(); sheet=book.active; sheet.title='问题清单'
        sheet.append(['问题编号','图号','图名','位置','问题类别','问题说明','涉及的其他图','状态','复核备注','来源文件名','模型复核','版本对比','图纸一标注','图纸二标注','差异原因','对照依据'])
        single = (task.get('checkpoints',{}).get('dual_review',{}).get('reason')=='single_model')
        for n,issue in enumerate(issues,1):
            unit=units[issue['left_unit_id']]; serial=serialize_issue(issue,units)
            review=issue.get('review_opinion') or {}
            model_status='单模型' if single else ('复核未完成' if not review or review.get('status') in ('failed','incomplete') else '双模型复核')
            values=[f'DC-{n:03d}',unit['drawing_number'],unit['name'],serial['location'],issue['title'],serial['description'],serial['related_label'],issue['review_status'],issue.get('review_note',''),originals[unit['original_id']]['name'],model_status,issue.get('comparison_tag') or '']
            finding = serial["finding"]
            values.extend([finding["left"]["value"], finding["right"]["value"], finding["reason"], finding["basis"]])
            for col,value in enumerate(values,1): text_cell(sheet.cell(n+1,col),value)
        other=book.create_sheet('未完成与读不了'); other.append(['文件或图号','原因'])
        for n,row in enumerate(incomplete,2):
            for col,value in enumerate(row,1): text_cell(other.cell(n,col),value)
        if declarations := task.get("unit_declarations"):
            notes = book.create_sheet("单位声明")
            notes.append(["类别", "单位", "卷册", "图号", "页或图框", "声明原文", "来源", "原图位置（左、上、右、下）"])
            for n, row in enumerate(declarations, 2):
                box = row.get("bbox") or {}
                coords = "，".join(f"{box[k]:.6f}" for k in ("x0", "y0", "x1", "y1")) if box else ""
                for col, value in enumerate((row["category"], row["unit"], row["volume"], row["drawing_number"],
                                              row["ordinal"], row["text"], row["source"], coords), 1):
                    text_cell(notes.cell(n, col), value)
        for ws in book:
            ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
            for cell in ws[1]: cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='284B63')
            for row in ws:
                for cell in row: cell.alignment=Alignment(vertical='top',wrap_text=True)
            from openpyxl.utils import get_column_letter
            for col in range(1,ws.max_column+1): ws.column_dimensions[get_column_letter(col)].width=24 if col not in (4,6) else 45
        book.save(path)

    def resolve(self, export_id):
        row=self.repo.get('exports',export_id)
        path=safe_path(self.repo.root,row['relative_path'])
        from backend.store.originals import sha256_file
        if not path.is_file() or sha256_file(path)!=row['sha256']:
            raise StoreError(ErrorCode.NOT_FOUND,'导出文件不存在或已改变，请重新导出')
        return path

    def reveal(self, export_id):
        path=self.resolve(export_id)
        mapping=path.with_suffix(path.suffix+'.destination.json')
        if mapping.exists():
            destination=Path(json.loads(mapping.read_text())['path'])
            if destination.is_file(): path=destination
        if sys.platform=='win32': subprocess.Popen(['explorer.exe','/select,',str(path)])
        elif sys.platform=='darwin': subprocess.Popen(['open','-R',str(path)])
        else: subprocess.Popen(['xdg-open',str(path.parent)])
        return {'ok':True}
