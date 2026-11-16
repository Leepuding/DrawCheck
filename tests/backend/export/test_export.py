import hashlib
import json
from pathlib import Path
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from openpyxl import load_workbook
from backend.export.service import ExportService, page_box
from backend.store import Repository, Evidence, BBox, Source

@pytest.fixture
def report(tmp_path):
    source=tmp_path/'原图.pdf'
    c=canvas.Canvas(str(source),pagesize=(300,200)); c.drawString(40,80,'Original evidence'); c.showPage()
    c.drawString(40,80,'Related evidence'); c.showPage(); c.save()
    repo=Repository(tmp_path/'data'); project=repo.create_project('测试项目')
    version=repo.create_version(project['id'],'第一版'); v=version['id']
    original=repo.import_original(v,source)['record']
    for name in ('left.png', 'right.png'):
        Image.new('RGB', (10, 10)).save(repo.root / name)
    unit=repo.create_unit(v,original['id'],kind='pdf_page',ordinal=1,name='基础',drawing_number='S-001',
                          metadata={'review_relative_path':'left.png'})
    related=repo.create_unit(v,original['id'],kind='pdf_page',ordinal=2,name='基础详图',drawing_number='S-002',
                             metadata={'review_relative_path':'right.png'})
    evidence=Evidence(unit['id'],Source.USER,'=HYPERLINK("bad")',BBox(.2,.3,.4,.5))
    other=Evidence(related['id'],Source.USER,'2502mm',BBox(.2,.3,.4,.5))
    issue=repo.create_issue(v,'尺寸不一致',evidence,other,rule_result={'summary':'尺寸差 2 毫米','category':'尺寸','left_value':'2500mm','right_value':'2502mm',
                                                          'basis':'基础 F1 的同一标注','max_abs_diff':'2'})
    pending=repo.create_pending(v,evidence,'看不清')
    task=repo.create_task(v)
    repo.update_task(task['id'],status='已完成',checkpoints={'dual_review':{'state':'skipped','reason':'single_model'}},incomplete=[],estimated_cost_micros=0)
    yield repo,v,unit,issue,source,tmp_path/'output'
    repo.close()

def test_two_reports_literal_excel_source_immutable(report):
    repo,v,unit,issue,source,out=report
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    service=ExportService(repo,output_root=out)
    result=service.generate(v)
    assert len(result['files'])==2 and result['pending_count']==1
    files=list((out/'测试项目').iterdir()); assert sorted(p.suffix for p in files)==['.pdf','.xlsx']
    pdf=PdfReader(next(p for p in files if p.suffix=='.pdf'))
    assert len(pdf.pages)==3
    assert pdf.pages[1].get_contents().get_data()==PdfReader(source).pages[0].get_contents().get_data()
    assert list(pdf.pages[1]['/Annots'][0].get_object()['/Rect'])==pytest.approx([60,100,120,140])
    assert pdf.pages[2]['/Annots'][0].get_object()['/Contents']=='DC-001'
    book=load_workbook(next(p for p in files if p.suffix=='.xlsx'))
    assert book.sheetnames==['问题清单','未完成与读不了']
    assert book['问题清单']['D2'].data_type=='s'
    assert book['问题清单']['K2'].value=='单模型'
    assert book['问题清单']['M1'].value=='图纸一标注'
    assert book['问题清单']['M2'].data_type=='s'
    assert book['问题清单']['N2'].value=='2502mm'
    assert '相差 2 mm' in book['问题清单']['O2'].value
    assert book['问题清单']['P2'].value == '基础 F1 的同一标注'
    assert book['未完成与读不了']['B2'].value=='待你确认未填'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    service.generate(v)
    assert len(list((out/'测试项目').iterdir()))==4

def test_excluded_and_resolved_not_marked(report):
    repo,v,unit,issue,source,out=report
    repo.update_issue(issue['id'],review_status='排除')
    result=ExportService(repo,output_root=out).generate(v)
    pdf=PdfReader(out/'测试项目'/result['files'][0]['name']); assert len(pdf.pages)==1
    book=load_workbook(out/'测试项目'/result['files'][1]['name'])
    assert book['问题清单']['H2'].value=='排除'

@pytest.mark.parametrize('rotation,expected',[(0,[60,100,120,140]),(90,[90,40,150,80]),(180,[180,60,240,100]),(270,[150,120,210,160])])
def test_rotation_mapping(rotation,expected):
    page=PdfWriter().add_blank_page(300,200); page.rotate(rotation)
    assert page_box(page,dict(x0=.2,y0=.3,x1=.4,y1=.5))==pytest.approx(expected)


def test_report_boundary_and_annotation_reason(report):
    repo, v, unit, issue, source, out = report
    service = ExportService(repo, output_root=out)
    result = service.generate(v)
    pdf = PdfReader(service.resolve(result['files'][0]['id']))
    assert '相差 2 mm' in pdf.pages[1]['/Annots'][0].get_object()['/Subj']
    book = load_workbook(service.resolve(result['files'][1]['id']))
    boundary = [row for row in book['未完成与读不了'].values if row[0] == '核验边界']
    assert len(boundary) == 1 and '尚未形成有效对照' in boundary[0][1]
    assert '相符' not in str(list(book['问题清单'].values))


def test_unavailable_second_preview_moves_difference_to_incomplete(report):
    repo, v, _, issue, _, out = report
    (repo.root / 'right.png').unlink()
    service = ExportService(repo, output_root=out)
    result = service.generate(v)
    pdf = PdfReader(service.resolve(result['files'][0]['id']))
    assert len(pdf.pages) == 1
    book = load_workbook(service.resolve(result['files'][1]['id']))
    assert book['问题清单'].max_row == 1
    assert any('图纸二的原图预览不可用' in row[1] for row in book['未完成与读不了'].values)
    assert repo.get('issues', issue['id'])['title'] == '尺寸不一致'


def test_coverage_note_is_not_an_extra_processing_failure(report):
    repo, v, _, _, _, out = report
    for pending in repo.list_records('pending_confirmations', version_id=v):
        repo.confirm_pending(pending['id'], status='已确认', confirmed_text=pending['text'])
    from backend.api.review_status import list_issues
    view = list_issues(repo, v)
    assert view['incomplete_count'] == view['unreadable_count'] == 0
    service = ExportService(repo, output_root=out)
    result = service.generate(v)
    text = PdfReader(service.resolve(result['files'][0]['id'])).pages[0].extract_text()
    assert '未完成与读不了：0 项' in text
    assert '尚未形成有效对照' in text
    workbook = load_workbook(service.resolve(result['files'][1]['id']))
    assert len([row for row in workbook['未完成与读不了'].values if row[0] == '核验边界']) == 1


def _source_diagnostic(report, *, manifest_reason='字体缺失'):
    repo, v, _, _, source, _ = report
    original = repo.list_records('original_files', version_id=v)[0]
    version = repo.get('drawing_versions', v)
    manifest = repo.root / f"projects/{version['project_id']}/cache/import-{v}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({'unreadable': [{'original_id': original['id'],
        'name': source.name, 'reason': manifest_reason}]}, ensure_ascii=False))
    task = repo.list_records('review_tasks', version_id=v)[-1]
    repo.update_task(task['id'], status='已完成', checkpoints=task['checkpoints'],
        incomplete=[{'unit_id': None, 'original_id': original['id'], 'step': 'extract',
                     'category': 'unreadable', 'reason': f'{source.name}：字体缺失'}], estimated_cost_micros=0)
    return original


def test_same_import_diagnostic_is_exported_once_and_count_matches_api(report):
    repo, v, _, _, source, out = report
    _source_diagnostic(report)
    from backend.api.review_status import list_issues
    view = list_issues(repo, v)
    service = ExportService(repo, output_root=out)
    result = service.generate(v)
    workbook = load_workbook(service.resolve(result['files'][1]['id']))
    rows = list(workbook['未完成与读不了'].values)[1:]
    assert len([row for row in rows if row == (source.name, '字体缺失')]) == 1
    assert not any(f'{source.name}：字体缺失' in row[1] for row in rows)
    text = PdfReader(service.resolve(result['files'][0]['id'])).pages[0].extract_text()
    expected = view['incomplete_count'] + view['unreadable_count']
    assert f'未完成与读不了：{expected} 项' in text
    assert '待你确认：1 项' in text


def test_distinct_diagnostics_from_one_source_are_preserved(report):
    repo, v, _, _, source, out = report
    _source_diagnostic(report, manifest_reason='缺少块定义')
    incomplete = ExportService(repo, output_root=out)._snapshot(v)[-1]
    assert (source.name, '缺少块定义') in incomplete
    assert (source.name, f'{source.name}：字体缺失') in incomplete


def test_missing_manifest_reason_keeps_the_same_unreadable_default(report):
    repo, v, _, _, source, out = report
    original = _source_diagnostic(report, manifest_reason=None)
    task = repo.list_records('review_tasks', version_id=v)[-1]
    repo.update_task(task['id'], status='已完成', checkpoints=task['checkpoints'],
        incomplete=[{'unit_id':None, 'original_id':original['id'], 'step':'extract',
                     'category':'unreadable', 'reason':f'{source.name}：读不了'}], estimated_cost_micros=0)
    incomplete = ExportService(repo, output_root=out)._snapshot(v)[-1]
    assert incomplete.count((source.name, '读不了')) == 1
