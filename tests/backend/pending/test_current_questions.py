import pytest
from types import SimpleNamespace

from backend.linking import LinkingService
from backend.linking.service import SAME_NAME_PENDING_REASON
from backend.pending import list_pending
from backend.pending.current import current_pending
from backend.references import AMBIGUOUS_REFERENCE_REASON, drawing_reference
from backend.rules import RulesAService, RulesBService
from backend.rules.dimension import NO_UNIT_REASON
from backend.rules.rules_b import INVALID_NUMBER_REASON, NO_REFERENCE_REASON, OPEN_SCOPE_REASON, UNPAIRED_REASON
from backend.store import BBox, Evidence, Repository, Source
from tests.backend.rules.test_reference_reliability import NON_REFERENCE_NOTES, WRAPPED_REFERENCES, add, seed, unit


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "data") as store:
        yield store


def test_retired_machine_questions_leave_human_history_and_original_facts(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    a = unit(repo, vid, oid, "D0103-01", 1)
    cases = [
        ("A", SAME_NAME_PENDING_REASON),
        ("6.检修、危废库部分详见D0501卷册", AMBIGUOUS_REFERENCE_REASON),
        ("DXN-35Q", NO_UNIT_REASON),
        ("ZC-YJV-26/35kV-3x70", UNPAIRED_REASON),
        ("0", INVALID_NUMBER_REASON),
        ("基础F1", SAME_NAME_PENDING_REASON),
    ]
    for y, (text, reason) in enumerate(cases, 1):
        ev = Evidence(a["id"], Source.CAD_TEXT, text, BBox(.1, y*.1, .2, y*.1+.025))
        repo.add_fact(vid, ev)
        repo.create_pending(vid, ev, reason)
    # A second rendition at the same location is not another object.
    repo.add_fact(vid, Evidence(a["id"], Source.VISION, "基础F1", BBox(.11,.601,.21,.629)))
    human = repo.create_pending(vid, Evidence(a["id"], Source.CAD_TEXT, "旧结论", BBox(.2,.7,.3,.72)), UNPAIRED_REASON)
    human = repo.confirm_pending(human["id"], status="已确认", confirmed_text="已经核实")
    manual = repo.create_pending(vid, Evidence(a["id"], Source.VISION, "2?00", BBox(.1,.8,.2,.83)), "看不清")
    before = repo.list_records("pending_confirmations", version_id=vid)
    for _ in range(2):
        bundle = list_pending(repo, vid)
        assert bundle["pending_count"] == 1
        assert {item["id"] for item in bundle["items"]} == {human["id"], manual["id"]}
        assert repo.list_records("pending_confirmations", version_id=vid) == before
    assert not LinkingService(repo).link_version(vid, persist=False).same_name_pending
    assert not RulesAService(repo).run(vid, persist=False).pendings
    assert not RulesBService(repo).run(vid, persist=False).pendings


def test_real_comparison_and_ambiguous_sheet_reference_still_need_attention(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    a = unit(repo, vid, oid, "D0103-01", 1)
    b = unit(repo, vid, oid, "D0103-02", 2)
    add(repo, vid, a, "100", category="尺寸", object_id="基础F1", identity_confirmed=True)
    add(repo, vid, b, "101mm", category="尺寸", object_id="基础F1", identity_confirmed=True)
    add(repo, vid, a, "详见04图", y=.2, category="详图索引断链", target_drawing_number="04")
    result = RulesAService(repo).run(vid)
    RulesBService(repo).run(vid)
    assert any("尺寸单位未核实" in row["reason"] for row in result.incomplete)
    assert {item["reason"] for item in list_pending(repo, vid)["items"]} == {AMBIGUOUS_REFERENCE_REASON}


def test_six_stored_machine_questions_leave_one_volume_reference():
    units = [{"id": uid, "drawing_number": f"33-NZ16181S-T0102-{seq}", "metadata": {"extract_status": "ok"}}
             for uid, seq in (("a","02"), ("b","03"), ("target","04"))]
    rows = []
    for index, (uid, text, y) in enumerate([
        ("a", "5.基础详见-04图。", .2), ("a", "基础详见-04图", .201),
        ("b", "基础详见-04图", .2), ("b", "基础详见-04图。", .201),
        ("b", "具体定位坐标详见T0103。", .4), ("b", "1. 具体定位坐标详见T0103", .401),
    ]):
        rows.append({"id": str(index), "unit_id": uid, "status": "待确认", "text": text,
                     "source": Source.VISION.value, "reason": AMBIGUOUS_REFERENCE_REASON,
                     "x0": .1, "y0": y, "x1": .4, "y1": y + .03})
    fake = SimpleNamespace(list_records=lambda table, **_: {
        "drawing_units": units, "pending_confirmations": rows, "facts": rows,
    }[table])
    row, = current_pending(fake, "v")
    assert row["id"] == "4" and "T0103" in row["text"]
    assert len(rows) == 6 and current_pending(fake, "v") == [row]


def test_overlap_merges_only_same_machine_question_at_iou_threshold(repo,tmp_path):
    vid, oid = seed(repo,tmp_path)
    a, b = (unit(repo,vid,oid,f'S-00{i}',i) for i in (1,2))
    def pending(text, box, *, sheet=a, source=Source.VISION, reason="看不清"):
        return repo.create_pending(vid, Evidence(sheet["id"], source, text, BBox(*box)), reason)["id"]
    origin = pending("5. 定位见-04图。", (.125,.125,.625,.375))
    at_half = pending("定位见-04图", (.125,.125,.375,.375))
    almost_half = pending("定位见-04图", (.125,.125,.373,.375))
    below = pending("定位见-04图", (.625,.125,.875,.375))
    different_digit = pending("定位见-05图", (.125,.125,.625,.375))
    negated = pending("定位不见-04图", (.125,.125,.625,.375))
    interior = pending("定位见－04图", (.125,.125,.625,.375))
    other_page = pending("定位见-04图", (.125,.125,.625,.375), sheet=b)
    other_reason = pending("定位见-04图", (.125,.125,.625,.375), reason="另有原因")
    human = pending("定位见-04图", (.125,.125,.625,.375), source=Source.USER)
    bad = pending("定位见-04图", (.125,.125,.125,.375))
    snapshot = repo.list_records("pending_confirmations", version_id=vid)
    visible = {row["id"] for row in current_pending(repo,vid)}
    assert visible == {origin, almost_half, below, different_digit, negated, interior, other_page, other_reason, human}
    assert not {at_half, bad} & visible
    assert repo.list_records("pending_confirmations", version_id=vid) == snapshot


def test_volume_reference_does_not_discard_an_explicit_sheet_in_the_same_note():
    reference = drawing_reference({"text": "材料详见D0501卷册，基础详见T0103-03"})
    assert reference["target"] == "T0103-03"


def test_obsolete_reference_questions_retire_but_human_history_and_real_questions_remain(repo,tmp_path):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'P-T0105-05',1)
    retired=[]
    for index,text in enumerate(NON_REFERENCE_NOTES):
        fact=add(repo,vid,source,text,y=.1+index*.1,category='详图索引断链',
                 target_drawing_number='S-999')
        ev=Evidence(source['id'],Source.VISION,text,BBox(*[fact[k] for k in ('x0','y0','x1','y1')]))
        for _ in range(2):
            retired.append(repo.create_pending(vid,ev,NO_REFERENCE_REASON))
    human=[]
    for index,status in enumerate(['已确认','无法确定']):
        text=NON_REFERENCE_NOTES[index]
        ev=Evidence(source['id'],Source.VISION,text,BBox(.1,.1+index*.1,.3,.11+index*.1))
        row=repo.create_pending(vid,ev,NO_REFERENCE_REASON)
        human.append(repo.confirm_pending(row['id'],status=status,
            **({'confirmed_text':text} if status=='已确认' else {})))
    real_fact=add(repo,vid,source,'详见04图',y=.6,category='图名')
    real=repo.create_pending(vid,Evidence(source['id'],Source.VISION,real_fact['text'],
        BBox(*[real_fact[k] for k in ('x0','y0','x1','y1')])),AMBIGUOUS_REFERENCE_REASON)
    unread=repo.create_pending(vid,Evidence(source['id'],Source.VISION,'原文未读清',BBox(.1,.8,.3,.81)),'看不清')
    facts_before=repo.list_records('facts',version_id=vid)
    history_before=repo.list_records('pending_confirmations',version_id=vid)
    for _ in range(2):
        bundle=list_pending(repo,vid)
        assert bundle['pending_count']==2
        assert bundle['incomplete_pending_count']==3
        assert {row['id'] for row in bundle['items']}=={real['id'],unread['id'],*[h['id'] for h in human]}
        assert not {row['id'] for row in retired} & {row['id'] for row in bundle['items']}
        assert repo.list_records('facts',version_id=vid)==facts_before
        assert repo.list_records('pending_confirmations',version_id=vid)==history_before


@pytest.mark.parametrize('text', ['参见', '详见', '节点索引：？'])
def test_genuine_missing_reference_target_survives_repeated_current_projection(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    add(repo,vid,source,text,category='图名',target_drawing_number='S-999')
    for _ in range(2):
        result=RulesBService(repo).run(vid)
        assert len(result.pendings)==1 and result.pendings[0]['reason']==NO_REFERENCE_REASON
        bundle=list_pending(repo,vid)
        assert bundle['pending_count']==1
        assert bundle['items'][0]['text']==text
        assert bundle['items'][0]['reason']==NO_REFERENCE_REASON
    assert len(repo.list_records('pending_confirmations',version_id=vid))==1
    assert len(repo.list_records('facts',version_id=vid))==1


@pytest.mark.parametrize('text', WRAPPED_REFERENCES)
def test_old_reference_question_with_visible_target_wrapper_is_not_retired(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    f=add(repo,vid,source,text,category='详图索引断链',target_drawing_number='T0103-03')
    ev=Evidence(source['id'],Source.VISION,text,BBox(*[f[k] for k in ('x0','y0','x1','y1')]))
    old=repo.create_pending(vid,ev,NO_REFERENCE_REASON)
    facts=repo.list_records('facts',version_id=vid)
    history=repo.list_records('pending_confirmations',version_id=vid)
    for _ in range(2):
        view=list_pending(repo,vid)
        assert view['pending_count']==1 and view['items'][0]['id']==old['id']
        assert len(RulesBService(repo).run(vid,persist=False).pendings)==1
        assert repo.list_records('facts',version_id=vid)==facts
        assert repo.list_records('pending_confirmations',version_id=vid)==history


@pytest.mark.parametrize('text', WRAPPED_REFERENCES[:3])
def test_normal_rule_recompute_supersedes_only_legacy_question_not_history(repo,tmp_path,text):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    f=add(repo,vid,source,text,category='图名')
    old=repo.create_pending(vid,Evidence(source['id'],Source.VISION,text,
        BBox(*[f[k] for k in ('x0','y0','x1','y1')])),NO_REFERENCE_REASON)
    assert list_pending(repo,vid)['pending_count']==1
    for _ in range(2):
        result=RulesBService(repo).run(vid)
        view=list_pending(repo,vid)
        assert len(result.pendings)==1 and result.pendings[0]['reason']==OPEN_SCOPE_REASON
        assert view['pending_count']==1
        assert view['items'][0]['id']==result.pendings[0]['id']
        assert view['items'][0]['reason']==OPEN_SCOPE_REASON
        assert repo.get('pending_confirmations',old['id'])==old
        assert len(repo.list_records('pending_confirmations',version_id=vid))==2


@pytest.mark.parametrize('status', ['已确认','无法确定'])
def test_new_reference_reason_keeps_processed_human_fallback_history(repo,tmp_path,status):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    text=WRAPPED_REFERENCES[0]
    f=add(repo,vid,source,text,category='详图索引断链')
    ev=Evidence(source['id'],Source.VISION,text,BBox(*[f[k] for k in ('x0','y0','x1','y1')]))
    old=repo.create_pending(vid,ev,NO_REFERENCE_REASON)
    old=repo.confirm_pending(old['id'],status=status,
        **({'confirmed_text':text} if status=='已确认' else {}))
    newer=repo.create_pending(vid,ev,OPEN_SCOPE_REASON)
    before=repo.list_records('pending_confirmations',version_id=vid)
    view=list_pending(repo,vid)
    # An unsure human decision can also suppress its machine facts through the
    # existing evidence-selection policy. Its processed history must remain.
    assert old['id'] in {row['id'] for row in view['items']}
    assert next(row for row in view['items'] if row['id']==old['id'])['status']==status
    assert repo.list_records('pending_confirmations',version_id=vid)==before


@pytest.mark.parametrize('difference', ['unit','text','x0','y0','x1','y1'])
def test_new_reference_reason_does_not_supersede_an_independent_anchor(repo,tmp_path,difference):
    vid,oid=seed(repo,tmp_path)
    source=unit(repo,vid,oid,'S-001',1)
    text=WRAPPED_REFERENCES[0]
    f=add(repo,vid,source,text,category='图名')
    old_ev=Evidence(source['id'],Source.VISION,text,BBox(*[f[k] for k in ('x0','y0','x1','y1')]))
    old=repo.create_pending(vid,old_ev,NO_REFERENCE_REASON)
    target_unit=unit(repo,vid,oid,'S-002',2) if difference=='unit' else source
    target_text=WRAPPED_REFERENCES[1] if difference=='text' else text
    other_box={key:f[key] for key in ('x0','y0','x1','y1')}
    if difference in other_box:
        other_box[difference]+=.001
    other=repo.add_fact(vid,Evidence(target_unit['id'],Source.VISION,target_text,BBox(**other_box)))
    newer=repo.create_pending(vid,Evidence(target_unit['id'],Source.VISION,target_text,BBox(**other_box)),OPEN_SCOPE_REASON)
    view=list_pending(repo,vid)
    assert {row['id'] for row in view['items']}=={old['id'],newer['id']}
    assert view['pending_count']==2
