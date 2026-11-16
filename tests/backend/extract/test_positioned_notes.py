import copy
from itertools import permutations

import pytest

from backend.extract.positioned_notes import VERSION, locate_note, locate_reference, normalized_text

SHA = "a" * 64
TEXT = "安装前必须完成结构验算并满足规定的荷载要求。"


def observation(text=TEXT, *, box=(.1,.2,.7,.22), index="one", crop=(0,0,1,1)):
    return {"text":text,"bbox":dict(zip(("x0","y0","x1","y1"),box)), "confidence":.99,
            "image_sha256":SHA,"observation_id":index,"crop":crop}


def locate(rows, text=TEXT, **kwargs):
    return locate_note(text,{"version":VERSION,"image_sha256":SHA,"observations":rows},image_sha256=SHA,**kwargs)


def test_only_whitespace_and_chinese_terminal_marks_are_normalized():
    assert normalized_text(" 条件 1：不得安装；\n") == "条件1：不得安装"
    assert normalized_text("条件1；不得安装") != normalized_text("条件1不得安装")
    assert normalized_text("１.0kN") != normalized_text("1.0kn")


def test_exact_position_preserves_original_reading():
    original = "安装前必须完成结构验算并满足规定的荷载要求；"
    result = locate([observation()],original)
    assert result["status"] == "unique_exact"
    assert result["original_text"] == original and result["reading_status"] == "unchanged"
    assert result["locations"][0]["bbox"]["y0"] == .2


@pytest.mark.parametrize("changed", [
    TEXT.replace("必须","不必"), TEXT.replace("规定的","规定1kN的"),
    TEXT.replace("荷载","荷成载"), TEXT.replace("要求。","要求，按厂家。"),
])
def test_internal_character_number_unit_or_condition_difference_is_not_a_match(changed):
    assert locate([observation(changed)])["status"] == "missing"


@pytest.mark.parametrize("change", [
    {"bbox":{"x0":.1,"y0":.2,"x1":.7,"y1":.2}},
    {"bbox":{"x0":-.1,"y0":.2,"x1":.7,"y1":.22}},
    {"bbox_clamped":True}, {"region_bbox_unlocated":True}, {"unclear":True},
    {"confidence":.89}, {"confidence":float("nan")}, {"confidence":True},
    {"image_sha256":"b"*64}, {"crop":[0,0,.2,.3]}, {"observation_id":""},
])
def test_invalid_independent_observations_cannot_restore_a_location(change):
    raw=observation();raw.update(change)
    result=locate([raw])
    assert result["status"] == "missing" and result["rejected_observations"] == 1


def test_bundle_source_and_version_must_match():
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation()]}
    assert locate_note(TEXT,bundle,image_sha256="b"*64)["status"] == "invalid"
    bundle["version"]="other"
    assert locate_note(TEXT,bundle,image_sha256=SHA)["status"] == "invalid"


def test_repeated_text_at_distinct_locations_returns_every_position():
    rows=[observation(),observation(box=(.1,.5,.7,.52),index="two")]
    result=locate(rows)
    assert result["status"] == "ambiguous" and len(result["locations"]) == 2


def test_crop_duplicates_require_same_text_and_same_physical_extent():
    a=observation();b=observation(box=(.101,.2001,.701,.2201),index="two",crop=(.05,.1,.8,.5))
    result=locate([a,b])
    assert result["status"] == "unique_exact"
    assert result["locations"][0]["observation_ids"] == ["one","two"]
    b["bbox"]["y0"],b["bbox"]["y1"]=.23,.25
    assert locate([a,b])["status"] == "ambiguous"


def test_multiline_requires_same_crop_adjacent_aligned_continuation():
    first=observation("安装前必须完成结构验算并满足规定的",box=(.1,.2,.7,.22))
    second=observation("荷载要求。",box=(.105,.225,.3,.245),index="two")
    original=copy.deepcopy([first,second])
    assert locate([first,second])["status"] == "unique_exact"
    assert [first,second] == original
    second["crop"]=(.05,0,1,1)
    assert locate([first,second])["status"] == "missing"
    second.update(crop=(0,0,1,1),text="2.荷载要求。")
    assert locate([first,second])["status"] == "missing"


def test_prefix_opt_in_does_not_verify_truncated_text_or_guess_missing_characters():
    prefix="5.本工程设备自身应能承受相应风雪荷载自重等荷载"
    raw=observation(prefix+"，强度应由厂家核实。")
    assert locate([raw],prefix)["status"] == "missing"
    found=locate([raw],prefix,allow_prefix=True)
    assert found["status"] == "unique_prefix" and found["reading_status"] == "unchanged"
    assert found["locations"][0]["match_type"] == "prefix_position_only"
    raw["text"]=raw["text"].replace("应能承受","应能成承受")
    assert locate([raw],prefix,allow_prefix=True)["status"] == "missing"


def test_short_numeric_or_partial_phrase_does_not_locate():
    assert locate([observation("5.荷载要求")],"5.荷载要求")["status"] == "invalid"


def test_observer_uses_existing_engine_and_hashes_the_actual_image(tmp_path, monkeypatch):
    import hashlib
    from PIL import Image
    from backend.extract import local_sheet
    from backend.extract.positioned_notes import read_note_observations

    path=tmp_path/"page.png"
    Image.new("RGB",(100,100),"white").save(path)
    supplied=object()
    def observe(engine,image,region):
        assert engine is supplied
        return [local_sheet.Word(TEXT,(.2,.2,.3,.22),.99,"看图识别")]
    def forbidden():
        raise AssertionError("Provided engine must avoid lazy engine creation")
    monkeypatch.setattr(local_sheet,"_image_words",observe)
    monkeypatch.setattr(local_sheet,"_engine",forbidden)
    result=read_note_observations(path,engine=supplied)
    assert result["image_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(result["observations"]) == 15
    assert len({r["observation_id"] for r in result["observations"]}) == 15
    assert max(r["crop"][3] for r in result["observations"]) == .99


def test_image_changed_during_observation_cannot_return_a_valid_bundle(tmp_path, monkeypatch):
    from PIL import Image
    from backend.extract import local_sheet
    from backend.extract.positioned_notes import read_note_observations

    path=tmp_path/"page.png"
    Image.new("RGB",(100,100),"white").save(path)
    def observe(engine,image,region):
        Image.new("RGB",(100,100),"black").save(path)
        return []
    monkeypatch.setattr(local_sheet,"_image_words",observe)
    with pytest.raises(OSError,match="changed"):
        read_note_observations(path,engine=object())


@pytest.mark.parametrize("query, observed", [
    ("基础详图见-04图。","5. 基础详图见-04图。"),
    ("做法详见T0104-03","支座连接做法详见T0104-03。"),
    ("A-A剖面位置详见-03～-05图。","13.A-A剖面位置详见-03～-05图。"),
])
def test_literal_reference_can_use_complete_substring_but_keeps_source_line_context(query, observed):
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(observed)]}
    found=locate_reference(query,bundle,image_sha256=SHA)
    assert found["status"] == "unique_exact"
    assert found["locations"][0]["observed_text"] == observed
    assert found["locations"][0]["bbox"] == observation()["bbox"]
    assert found["reading_status"] == "unchanged"


@pytest.mark.parametrize("query", ["见04","基础详图","M12预埋螺栓详图","详见T0104-03"])
def test_short_or_nonliteral_reference_never_selects_a_position(query):
    assert locate_reference(query,{"version":VERSION,"image_sha256":SHA,"observations":[observation(query)]},
                            image_sha256=SHA)["status"] == "invalid"


def test_reference_substring_does_not_change_endpoints_or_drop_minus_signs():
    query="A-A剖面位置详见-03~-05图。"
    for other in [query.replace("-03","03"),query.replace("-05","-06"),query.replace("-03~-05","—03—05")]:
        bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(other)]}
        assert locate_reference(query,bundle,image_sha256=SHA)["status"] == "missing"


@pytest.mark.parametrize("query, observed",[
    ("基础详图见-04图。","5.基础详图见—04图。"),
    ("基础详图见—04图。","5.基础详图见-04图。"),
    ("A-A剖面位置详见-03～-05图。","13.A-A剖面位置详见-03~-05图。"),
    ("做法详见T0104-03","支座连接做法详见T0104—03。"),
])
def test_reference_typography_only_recovers_location_with_complete_literal_context(query,observed):
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(observed)]}
    found=locate_reference(query,bundle,image_sha256=SHA)
    assert found["status"]=="unique_format_equivalent"
    assert found["original_text"]==query and found["reading_status"]=="unchanged"
    assert found["locations"][0]["observed_text"]==observed
    assert found["locations"][0]["match_type"]=="reference_separator_typography"


@pytest.mark.parametrize("query, observed",[
    ("荷载为-04kN，基础详图见-04图。","荷载为—04kN，基础详图见—04图。"),
    ("M12-M16螺栓做法详见T0104-03。","M12—M16螺栓做法详见T0104—03。"),
    ("基础详图见-04图。","5.基础详图见—4图。"),
    ("做法详见T0104-03","做法详见TO104—03。"),
    ("做法详见T0104-03","T0104—03"),
])
def test_reference_typography_never_changes_load_spec_leading_zero_case_or_action(query,observed):
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(observed)]}
    assert locate_reference(query,bundle,image_sha256=SHA)["status"]=="missing"


def test_two_occurrences_even_inside_one_wide_observation_are_not_unique():
    query="基础详图见-04图"
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(query+"；"+query)]}
    assert locate_reference(query,bundle,image_sha256=SHA)["status"] == "ambiguous"


def test_reference_crop_context_differences_merge_only_at_the_same_proven_physical_line():
    query="A-A剖面位置详见-03~-05图。"
    rows=[observation("13."+query),observation("3."+query,box=(.101,.2,.7,.22),index="two",crop=(.05,0,1,1))]
    bundle={"version":VERSION,"image_sha256":SHA,"observations":rows}
    found=locate_reference(query,bundle,image_sha256=SHA)
    assert found["status"]=="unique_exact"
    assert [v["observed_text"] for v in found["locations"][0]["observed_variants"]]==["13."+query,"3."+query]
    rows[1]["bbox"].update(y0=.5,y1=.52)
    assert locate_reference(query,bundle,image_sha256=SHA)["status"]=="ambiguous"


def test_preceding_title_is_not_a_second_occurrence_of_a_complete_reference_line():
    query="具体定位坐标详见T0103"
    rows=[observation("基础定位坐标点位置图",box=(.1,.2,.4,.22)),
          observation(query,box=(.1,.23,.4,.25),index="two")]
    bundle={"version":VERSION,"image_sha256":SHA,"observations":rows}
    found=locate_reference(query,bundle,image_sha256=SHA)
    assert found["status"]=="unique_exact"
    assert found["locations"][0]["bbox"]==rows[1]["bbox"]


@pytest.mark.parametrize("target",["T0104-030","T0104-03A","T0104-03-1","T0104-03/1",
                                    "T0104-03.1","T0104-03_1","T0104-03~05","T0104-003",
                                    "T0104—030","T0104—03A"])
def test_reference_substring_requires_the_complete_target_token(target):
    query="做法详见T0104-03"
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation("支座连接做法详见"+target+"。") ]}
    before=copy.deepcopy(bundle)
    assert locate_reference(query,bundle,image_sha256=SHA)["status"]=="missing"
    assert bundle==before


@pytest.mark.parametrize("ending",["。","；","图。","（本图）","，按该图施工。"])
def test_true_target_boundary_before_chinese_context_or_terminal_punctuation_remains_valid(ending):
    query="做法详见T0104-03"
    observed="支座连接"+query+ending
    bundle={"version":VERSION,"image_sha256":SHA,"observations":[observation(observed)]}
    assert locate_reference(query,bundle,image_sha256=SHA)["status"]=="unique_exact"


@pytest.mark.parametrize("different_context",[False,True])
def test_nontransitive_three_ocr_frames_are_never_greedily_collapsed_in_any_order(different_context):
    query="做法详见T0104-03"
    rows=[observation((prefix if different_context else "")+query,box=box,index=str(i),crop=(0,0,1,1))
          for i,(prefix,box) in enumerate(zip(("支座连接","组件连接","结构连接"),
              ((.1,.2,.7,.22),(.2,.2,.8,.22),(.3,.2,.9,.22))))]
    outputs=[]
    for order in permutations(rows):
        bundle={"version":VERSION,"image_sha256":SHA,"observations":list(order)}
        before=copy.deepcopy(bundle)
        result=locate_reference(query,bundle,image_sha256=SHA)
        assert result["status"]=="ambiguous" and len(result["locations"])==3
        assert bundle==before
        outputs.append(result)
    assert all(result==outputs[0] for result in outputs)


def test_complete_three_observation_group_has_order_independent_unique_position():
    query="做法详见T0104-03"
    rows=[observation(prefix+query,box=box,index=str(i)) for i,(prefix,box) in enumerate(zip(
        ("支座连接","组件连接","结构连接"),((.1,.2,.7,.22),(.101,.2,.701,.22),(.102,.2,.702,.22))))]
    results=[]
    for order in permutations(rows):
        bundle={"version":VERSION,"image_sha256":SHA,"observations":list(order)}
        before=copy.deepcopy(bundle)
        result=locate_reference(query,bundle,image_sha256=SHA)
        assert result["status"]=="unique_exact" and len(result["locations"])==1
        assert bundle==before
        results.append(result)
    assert all(result==results[0] for result in results)


def test_local_cache_checks_content_source_and_image_without_provider_calls(tmp_path,monkeypatch):
    import hashlib,json
    from PIL import Image
    from backend.extract import positioned_notes
    image=tmp_path/"review.png"
    Image.new("RGB",(100,100),"white").save(image)
    calls=[]
    def observe(path,engine=None):
        calls.append(str(path))
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        return {"version":VERSION,"image_sha256":sha,"observations":[]}
    monkeypatch.setattr(positioned_notes,"read_note_observations",observe)
    first=positioned_notes.cached_note_observations(image,cache_root=tmp_path)
    assert positioned_notes.cached_note_observations(image,cache_root=tmp_path)==first and len(calls)==1
    cache=next((tmp_path/"cache"/"positioned-notes").glob("*.json"))
    raw=json.loads(cache.read_text());raw["bundle"]["observations"]=[observation()]
    cache.write_text(json.dumps(raw))
    assert positioned_notes.cached_note_observations(image,cache_root=tmp_path)==first and len(calls)==2
    Image.new("RGB",(100,100),"black").save(image)
    assert positioned_notes.cached_note_observations(image,cache_root=tmp_path)["image_sha256"]!=first["image_sha256"]
    assert len(calls)==3
