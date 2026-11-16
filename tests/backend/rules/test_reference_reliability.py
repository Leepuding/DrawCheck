"""Sheet existence needs a closed identity scope; matching needs literal evidence."""
import json

import pytest

from backend.linking import LinkingService
from backend.references import AMBIGUOUS_REFERENCE_REASON, drawing_reference, resolve_reference, absence_scope
from backend.rules import RulesBService
from backend.rules.catalog_titleblock import run_catalog_titleblock
from backend.rules.rules_b import NO_REFERENCE_REASON, visible_detail_hint
from backend.store import BBox, Evidence, Source


def seed(repo, tmp_path):
    path = tmp_path / 'references.pdf'
    path.write_bytes(b'%PDF-test')
    project = repo.create_project('reference boundary')
    version = repo.create_version(project['id'], 'v1')
    original = repo.import_original(version['id'], path)['record']
    return version['id'], original['id']


def unit(repo, vid, oid, number, ordinal, **metadata):
    return repo.create_unit(vid, oid, kind='pdf_page', ordinal=ordinal, name=number,
                            drawing_number=number, metadata={'extract_status':'ok', **metadata})


def add(repo, vid, u, text, *, numeric=None, measurement=None, y=.1, source=Source.VISION, **annotation):
    fact = repo.add_fact(vid, Evidence(u['id'], source, text, BBox(.1,y,.3,y+.01)),
                         numeric_value=numeric, unit=measurement)
    current = repo.get('drawing_units', u['id'])
    metadata = current['metadata']
    metadata.setdefault('fact_annotations', {})[fact['id']] = annotation
    repo._update('drawing_units', u['id'], {'metadata_json':json.dumps(metadata)})
    return fact


def complete_catalog(repo, vid, oid, numbers):
    return unit(repo,vid,oid,'S-000',90,is_catalog_sheet=True,catalog_complete=True,
                catalog_listed_numbers=numbers)


@pytest.mark.parametrize('target_status', ['incomplete', 'ok'])
def test_failed_or_unlisted_visual_identity_cannot_prove_absence(repo,tmp_path,target_status):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    unit(repo,vid,oid,'11#',2,extract_status=target_status,title_identity_uncertain=False)
    complete_catalog(repo,vid,oid,['P-T0105-05','P-T0105-03'])
    add(repo,vid,source,'做法详见T0105-03',category='详图索引断链',target_drawing_number='T0105-03')
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues
    assert result.incomplete and result.pendings
    assert not result.detail['reference_identity_scope']['closed']
    directory=run_catalog_titleblock(repo,vid,persist=False)
    assert directory['incomplete'] and not directory['issues']


def test_complete_catalog_with_reliably_identified_arrivals_can_report_genuine_missing(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    complete_catalog(repo,vid,oid,['P-T0105-05','P-T0105-03'])
    add(repo,vid,source,'做法详见T0105-03',category='详图索引断链')
    result=RulesBService(repo).run(vid,persist=False)
    assert len(result.issues)==1
    assert result.detail['reference_identity_scope']['closed']
    assert 'P-T0105-03' in result.issues[0]['rule_result']['basis']
    directory=run_catalog_titleblock(repo,vid,persist=False)
    assert any(issue['fixture_id']=='ISSUE-CAT-MISSING' for issue in directory['issues'])


@pytest.mark.parametrize('text,reported,detail', [
    ('做法详见T0103-03','T0103','03'), ('节点索引：T0103-03','T0103-03','03'),
    ('详见33-NZ16181S-T0103-03','33-NZ16181S-T0103-03',None),
])
def test_visible_unique_suffix_resolves_without_inventing_internal_detail(repo,tmp_path,text,reported,detail):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0103-07',1)
    target=unit(repo,vid,oid,'33-NZ16181S-T0103-03',2)
    fact=add(repo,vid,source,text,category='详图索引断链',target_drawing_number=reported,
             target_detail_number=detail)
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues and not result.pendings
    reference=result.detail['references'][0]
    assert reference['resolved_number']==target['drawing_number']
    assert reference['detail'] is None and reference['identity_only']
    links=LinkingService(repo).link_version(vid,persist=False)
    edge=next(e for e in links.associations if e['kind']=='detail_index')
    assert edge['details']['reference']['fact_id']==fact['id']
    assert edge['details']['reference']['text']==text
    assert edge['details']['reference']['source']==Source.VISION.value


@pytest.mark.parametrize('raw', [
    '见04', '详见图3', '详见T0103', '详见-3图', '详见-03图',
    '详见-03～-05图', '详见-04、-05', '详见-04和-05', '详见（-04、-05）图',
])
def test_local_numeric_shorthand_needs_one_unambiguous_hyphenated_target(repo,tmp_path,raw):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0102-03',1)
    unit(repo,vid,oid,'P-T0102-04',2)
    add(repo,vid,source,raw,category='详图索引断链',target_drawing_number='04',identity_confirmed=True)
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues and result.incomplete
    assert not LinkingService(repo).link_version(vid,persist=False).associations


def test_single_shorthand_links_by_verified_home_and_keeps_old_api_unchanged(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0102-03',1)
    target=unit(repo,vid,oid,'P-T0102-04',2)
    unit(repo,vid,oid,'Q-T0102-04',3)
    raw = '基础详图见-04图。'
    add(repo,vid,source,raw,category='详图索引断链',target_drawing_number='04',target_detail_number='04')
    assert drawing_reference({'text': raw})['target'] is None
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues and not result.pendings and not result.incomplete
    reference, = result.detail['references']
    assert reference['unit_ids'] == [target['id']]
    assert reference['match_basis'] == 'same_volume_shorthand' and reference['detail'] is None
    edge, = LinkingService(repo).link_version(vid,persist=False).associations
    assert edge['details']['reference']['target'] == 'P-T0102-04'


@pytest.mark.parametrize('case,status', [
    ('home_unverified', 'unverified'), ('target_unverified', 'unverified'),
    ('duplicate_even_outside_scope', 'ambiguous'), ('other_project_only', 'missing'),
    ('delimiter_suffix_only', 'missing'), ('directory_only', 'missing'),
])
def test_shorthand_requires_exact_unique_actual_title(repo,tmp_path,case,status):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0102-03',1,title_identity_uncertain=(case=='home_unverified'))
    if case == 'other_project_only':
        unit(repo,vid,oid,'Q-T0102-04',2)
    elif case == 'delimiter_suffix_only':
        unit(repo,vid,oid,'Q-P-T0102-04',2)
    elif case == 'directory_only':
        complete_catalog(repo,vid,oid,['P-T0102-03','P-T0102-04'])
    else:
        unit(repo,vid,oid,'P-T0102-04',2,title_identity_uncertain=(case=='target_unverified'))
    if case == 'duplicate_even_outside_scope':
        unit(repo,vid,oid,'P-T0102-04',3,out_of_scope=True)
    add(repo,vid,source,'基础详图见-04图')
    result=RulesBService(repo).run(vid,persist=False)
    reference, = result.detail['references']
    assert reference['status'] == status
    assert not any(edge['kind'] == 'detail_index' for edge in LinkingService(repo).link_version(vid,persist=False).associations)
    if case == 'directory_only':
        assert len(result.issues) == 1 and not result.pendings
        assert result.issues[0]['rule_result']['reference_target'] == 'P-T0102-04'
    else:
        assert result.pendings and not result.issues


def test_ambiguous_suffix_unknown_target_and_character_difference_are_not_resolved():
    fact={'id':'f','source':'视觉识别','text':'详见T0103-03'}
    reference=drawing_reference(fact)
    units={str(i):{'id':str(i),'drawing_number':n,'metadata':{}} for i,n in enumerate(
        ['33-NZ16181S-T0103-03','33-NC09811S-T0103-03'])}
    assert resolve_reference(reference,units)['status']=='ambiguous'
    units['1']['metadata']['title_identity_uncertain']=True
    assert resolve_reference(reference,units)['status']=='ambiguous'
    reference=drawing_reference({**fact,'text':'详见33-NZ161815-T0103-03'})
    assert resolve_reference(reference,units)['status']=='missing'


def test_dimension_material_and_bare_model_target_are_not_reference_evidence():
    for fact in [
        {'text':'U40X60X2.5','category':'材料规格','target_drawing_number':'U40X60X2.5'},
        {'text':'T0103-03','category':'尺寸','target_drawing_number':'T0103-03'},
        {'text':'3000','category':'详图索引断链','target_drawing_number':'T0103-03'},
        {'text':'T0103-03','category':'详图索引断链','target_drawing_number':'T0103-03'},
    ]:
        reference=drawing_reference(fact)
        assert reference is None or reference['target'] is None
    assert drawing_reference({'text':'详见T0103-03','category':'尺寸'})['target']=='T0103-03'


def test_two_local_rows_and_generic_support_name_never_prove_same_object(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    u=unit(repo,vid,oid,'S-001',1)
    for text,y in [('U40X60X2.5',.2),('U40X40X2.5',.4)]:
        add(repo,vid,u,text,y=y,category='材料规格',object_id='支架',comparison_key='规格',identity_confirmed=True)
    for spec,value,y in [('U40X40X2.5','440',.6),('U40X60X2.5','2618',.7)]:
        add(repo,vid,u,f'序号1 支架 {spec} {value} 热镀锌',numeric=value,measurement='m',y=y,
            category='数量与材料表',object_id='1_地面支架材料表',comparison_key='序号_名称',
            identity_confirmed=True,quantity_role='material_table')
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues and not result.pendings


@pytest.mark.parametrize('object_id', ['构件B1','A轴/1轴支架','ISSUE-SPEC'])
def test_specific_identity_cross_unit_comparison_is_preserved(repo,tmp_path,object_id):
    vid,oid=seed(repo,tmp_path)
    a=unit(repo,vid,oid,'S-001',1); b=unit(repo,vid,oid,'S-002',2)
    for u,text in [(a,'U40X60X2.5'),(b,'U40X40X2.5')]:
        add(repo,vid,u,text,category='材料规格',object_id=object_id,comparison_key='规格',identity_confirmed=True)
    for u,value in [(a,'440'),(b,'441')]:
        add(repo,vid,u,f'长度{value}m',numeric=value,measurement='m',y=.5,
            category='数量与材料表',object_id=object_id,comparison_key='长度',identity_confirmed=True,
            quantity_role='material_table')
    result=RulesBService(repo).run(vid,persist=False)
    assert len(result.issues)==2
    qty=next(i for i in result.issues if i['rule_result']['category']=='数量与材料表')
    assert '单位 m' in qty['rule_result']['basis'] and '不推断为件数' in qty['rule_result']['basis']


def test_explicit_manual_identity_can_close_scope_but_legacy_flag_cannot():
    catalog={'id':'c','drawing_number':'S-000','metadata':{'is_catalog_sheet':True,'extract_status':'ok',
             'catalog_complete':True,'catalog_listed_numbers':['S-001','S-003']}}
    arrival={'id':'a','drawing_number':'S-002','metadata':{'extract_status':'ok','manual_correction':True}}
    assert not absence_scope({'c':catalog,'a':arrival},[])['closed']
    arrival['metadata']['manual_drawing_number_confirmed']=True
    assert absence_scope({'c':catalog,'a':arrival},[])['closed']


def test_lower_confidence_shared_code_does_not_erase_reference_provenance(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    a=unit(repo,vid,oid,'S-001',1); b=unit(repo,vid,oid,'S-002',2)
    add(repo,vid,a,'见详图 S-002')
    add(repo,vid,a,'XB03'); add(repo,vid,b,'XB03')
    links=LinkingService(repo).link_version(vid,persist=False)
    assert len(links.associations)==1
    assert links.associations[0]['kind']=='detail_index'
    assert links.associations[0]['details']['reference']['target']=='S-002'


def test_rule_reconciliation_removes_obsolete_suspected_but_keeps_manual_audit(repo,tmp_path):
    from backend.rules.registry import reconcile_rule_results
    vid,oid=seed(repo,tmp_path)
    u=unit(repo,vid,oid,'S-001',1)
    complete_catalog(repo,vid,oid,['S-001'])
    left=add(repo,vid,u,'U40X60X2.5',category='材料规格',object_id='支架',identity_confirmed=True)
    right=add(repo,vid,u,'U40X40X2.5',category='材料规格',object_id='支架',identity_confirmed=True,y=.5)
    ev=lambda f: Evidence(u['id'],Source.VISION,f['text'],BBox(*[f[k] for k in ('x0','y0','x1','y1')]))
    old=[]
    for index in range(2):
        old.append(repo.create_issue(vid,'旧误配',ev(left),ev(right),
            rule_result={'category':'材料规格','dedupe_key':f'old{index}'},primary_opinion={'engine':'rules_b'}))
    repo.update_issue(old[1]['id'],review_status='排除',review_note='人工原图核实为不同对象')
    result=reconcile_rule_results(repo,version_id=vid)
    assert result['removed_suspected']==1
    remaining=repo.list_records('issues',version_id=vid)
    assert len(remaining)==1 and remaining[0]['id']==old[1]['id']
    assert remaining[0]['review_status']=='排除'
    assert remaining[0]['review_note'].startswith('人工原图核实为不同对象')
    assert remaining[0]['left_text']==left['text'] and remaining[0]['right_text']==right['text']
    assert len(repo.list_records('facts',version_id=vid))==2


def test_catalogued_but_missing_alternative_does_not_make_suffix_unique():
    units={'a':{'id':'a','drawing_number':'NZ-T0103-03','metadata':{'extract_status':'ok'}},
           'c':{'id':'c','drawing_number':'CAT','metadata':{'is_catalog_sheet':True,'extract_status':'ok',
                'catalog_listed_numbers':['NZ-T0103-03','NC-T0103-03']}}}
    short=drawing_reference({'text':'详见T0103-03'})
    assert resolve_reference(short,units)['status']=='ambiguous'
    full=drawing_reference({'text':'详见NZ-T0103-03'})
    assert resolve_reference(full,units)['status']=='resolved'


@pytest.mark.parametrize('code',['DWG_INCOMPLETE','PDF_INCOMPLETE','PDF_PAGE_FAILED'])
def test_source_failure_with_no_unit_cannot_be_reported_as_missing_sheet(repo,tmp_path,code):
    from backend.revisions.service import RevisionService
    vid,oid=seed(repo,tmp_path)
    u=unit(repo,vid,oid,'S-001',1)
    complete_catalog(repo,vid,oid,['S-001','S-002'])
    add(repo,vid,u,'详见S-002')
    path=RevisionService(repo)._manifest_path(vid)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'unreadable':[{'code':code,'reason':'源文件有未完成范围'}]}))
    b=RulesBService(repo).run(vid,persist=False)
    a=run_catalog_titleblock(repo,vid,persist=False)
    assert not b.issues and b.incomplete and not b.detail['reference_identity_scope']['closed']
    assert not a['issues'] and a['incomplete']


NON_REFERENCE_NOTES = [
    'M12预埋螺栓详图',
    '节点详图中螺栓长度由支架厂家放样确定，应符合有关设计规定。',
    '该节点中长圆孔位置的螺栓连接采用大规格垫片',
    '普通光伏支架钢筋混凝土支墩防水节点平面图',
]


@pytest.mark.parametrize('text', NON_REFERENCE_NOTES)
@pytest.mark.parametrize('unclear', [False, True])
def test_detail_titles_and_construction_notes_do_not_create_reference_questions(repo,tmp_path,text,unclear):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    # Wrong model fields cannot supply a missing relationship, even if unclear.
    add(repo,vid,source,text,category='详图索引断链',target_drawing_number='S-999',
        identity_confirmed=True,unclear=unclear)
    assert drawing_reference({'text':text,'category':'详图索引断链','target_drawing_number':'S-999'}) is None
    assert not visible_detail_hint({'text':text})
    for _ in range(2):
        result=RulesBService(repo).run(vid,persist=False)
        assert not result.pendings and not result.issues
    assert len(repo.list_records('facts',version_id=vid))==1


@pytest.mark.parametrize('text', ['参见', '详见', '做法见图', '节点索引：？', '详图索引：'])
@pytest.mark.parametrize('category', ['', '图名', '材料规格'])
def test_visible_reference_with_unreadable_target_still_needs_verification(repo,tmp_path,text,category):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    add(repo,vid,source,text,category=category)
    assert visible_detail_hint({'text':text,'category':category})
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues
    assert [row['reason'] for row in result.pendings]==[NO_REFERENCE_REASON]


@pytest.mark.parametrize('text', ['参见S-002', '详见S-002', '见图S-002', '节点索引：S-002'])
def test_literal_reference_survives_wrong_model_category(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    unit(repo,vid,oid,'S-002',2)
    add(repo,vid,source,text,category='材料规格')
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.pendings and not result.issues
    assert result.detail['references'][0]['status']=='resolved'


def test_bare_model_reference_has_no_verified_graphical_index_context(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    add(repo,vid,source,'T0103-03',category='详图索引断链',target_drawing_number='T0103-03')
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.pendings and not result.issues and not result.detail['references']
    assert not LinkingService(repo).link_version(vid,persist=False).associations


def test_reference_index_description_without_an_index_marker_is_not_a_reference():
    assert not visible_detail_hint({'text':'详图索引说明'})
    assert not visible_detail_hint({'text':'该节点索引内容由厂家整理'})
    assert visible_detail_hint({'text':'节点索引'})


@pytest.mark.parametrize('text', [
    '钢材：详见构件表；焊条：E43。',
    '主要受力杆件采用轻型钢材，钢材：详见一览表；螺栓采用8.8级。',
    '组件压块尺寸、数量、连接位置等相关要求详见组件厂家要求。',
    '场地标高详见本工程总平面布置图。',
    '常见节点构造',
])
def test_readable_named_target_is_not_a_missing_sheet_reference(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    add(repo,vid,source,text,category='详图索引断链',target_drawing_number='S-999')
    assert not visible_detail_hint({'text':text})
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.pendings and not result.issues


WRAPPED_REFERENCES = [
    '节点索引：（T0103-03）', '详见（T0103-03）', '详见图纸T0103-03',
    '参见(T0103-03)', '见图纸（T0103-03）', '详见图号：T0103-03',
    '详见（图纸T0103-03）', '详见图纸编号：T0103-03',
]


@pytest.mark.parametrize('text', WRAPPED_REFERENCES)
def test_literal_reference_wrappers_preserve_target_and_full_provenance(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    target=unit(repo,vid,oid,'P-T0103-03',2)
    f=add(repo,vid,source,text,category='图名')
    literal=drawing_reference({**f,'category':'图名'})
    assert literal['target']=='T0103-03' and literal['text']==text
    assert literal['bbox']=={key:f[key] for key in ('x0','y0','x1','y1')}
    assert visible_detail_hint({'text':text})
    result=RulesBService(repo).run(vid,persist=False)
    assert not result.issues and not result.pendings
    assert result.detail['references'][0]['resolved_number']==target['drawing_number']
    edge=LinkingService(repo).link_version(vid,persist=False).associations[0]
    assert edge['details']['reference']['text']==text


@pytest.mark.parametrize('text', ['详见（？）', '节点索引：（？）', '详见图纸（？）'])
def test_wrapped_unreadable_target_is_retained_as_unverified(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    add(repo,vid,source,text,category='图名')
    result=RulesBService(repo).run(vid,persist=False)
    assert [row['reason'] for row in result.pendings]==[NO_REFERENCE_REASON]


@pytest.mark.parametrize('text', ['详见（构件表）', '详见图纸（厂家资料）', '节点详图（M12螺栓）'])
def test_wrapper_does_not_invent_a_sheet_target_for_named_material(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    add(repo,vid,source,text,category='详图索引断链',target_drawing_number='T0103-03')
    assert drawing_reference({'text':text}) is None
    assert not visible_detail_hint({'text':text})
    assert not RulesBService(repo).run(vid,persist=False).pendings


def test_parenthesized_multiple_targets_do_not_choose_first_sheet():
    text='详见（T0103-03、T0104-03）'
    ref=drawing_reference({'text':text})
    assert ref['target'] is None and ref['reason']==AMBIGUOUS_REFERENCE_REASON
    assert 'T0103-03' in ref['literal_target'] and 'T0104-03' in ref['literal_target']


@pytest.mark.parametrize('text', ['详见（D0501卷册）', '详见（卷册D0501）', '参见(D0501图册)'])
def test_wrapped_volume_reference_is_not_reclassified_as_a_missing_sheet(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    add(repo,vid,source,text,category='详图索引断链',target_drawing_number='D0501')
    assert drawing_reference({'text':text}) is None
    assert not RulesBService(repo).run(vid,persist=False).pendings


def test_wrapped_volume_does_not_hide_explicit_sheet_reference_in_same_note():
    ref=drawing_reference({'text':'材料详见（D0501卷册），基础详见（T0103-03）'})
    assert ref['target']=='T0103-03'
