from types import SimpleNamespace

from backend.extract.engineering import _wires_between, elevation_guides, read_values, stated_assets
from backend.extract.local_sheet import Word
from backend.rules.engineering import run_engineering
from backend.store import Source


BOX = (.12, .13, .20, .15)


def word(text, box=BOX):
    return Word(text, box, 1., Source.CAD_TEXT.value)


def unit(uid, engineering, name="", source="same-file"):
    return {"id": uid, "original_id": source, "metadata": {"local_sheet": {
        "title": {"name": {"text": name}}, "engineering": engineering}}}


def test_dimensions_convert_units_and_do_not_pair_ambiguous_positions():
    a = unit("1", {"measurements": read_values([word("A-101基础宽度1000mm")])})
    b = unit("2", {"measurements": read_values([word("A-101基础宽度1m")])})
    c = unit("3", {"measurements": read_values([word("A-101基础宽度1m"), word("A-101基础宽度2m")])})
    result = run_engineering(SimpleNamespace(), "version", units=[a,b,c], persist=False)
    assert [(r["left_unit_id"],r["right_unit_id"],r["outcome"]) for r in result["comparisons"]] == [("1","2","match")]
    assert not result["issues"]


def test_elevations_require_same_datum_and_reject_ranges():
    words = [word("A-101黄海标高1.000m"), word("A-101黄海标高1.010m"),
             word("A-101吴淞标高1.000m"), word("A-101黄海标高1.000m以上")]
    units = [unit(str(i), {"measurements": read_values([text])}) for i, text in enumerate(words)]
    result = run_engineering(SimpleNamespace(), "version", units=units, persist=False)
    assert len(result["comparisons"]) == len(result["issues"]) == 1
    assert result["comparisons"][0]["outcome"] == "mismatch"


def test_triangle_reads_level_and_bare_decimal_is_ignored():
    strokes = [((.124,.151),(.128,.155)), ((.128,.155),(.132,.151)), ((.11,.151),(.132,.151))]
    guides = elevation_guides(strokes)
    words = [word("14.650"), word("2.650", (.3,.3,.32,.32))]
    results = read_values(words, segments=strokes, guides=guides)
    assert [(row["category"],row["value"],row["unit"]) for row in results] == [("标高","14.650","")]


def test_crossing_wire_strokes_keep_two_distinct_links():
    boxes = [(.1,.30,.2,.40), (.6,.30,.7,.40), (.35,.1,.45,.2), (.35,.5,.45,.6)]
    segments = [((.2,.35),(.6,.35)), ((.4,.2),(.4,.5))]
    pairs = {pair for pair, _ in _wires_between(segments, boxes)}
    assert pairs == {(boxes[0],boxes[1]), (boxes[2],boxes[3])}


def test_explicit_t_junction_proves_each_branch_without_crossing_another_device():
    boxes = [(.10,.30,.20,.40), (.60,.30,.70,.40), (.35,.60,.45,.70)]
    segments = [((.2,.35),(.6,.35)), ((.4,.35),(.4,.60))]
    pairs = {pair for pair, _ in _wires_between(segments, boxes)}
    assert pairs == {(boxes[0],boxes[1]), (boxes[0],boxes[2]), (boxes[2],boxes[1])}


def test_route_does_not_pass_through_unlabeled_intervening_enclosure():
    boxes = [(.10,.30,.20,.40), (.60,.30,.70,.40), (.35,.30,.45,.40)]
    segments = [((.2,.35),(.6,.35))]
    pairs = {pair for pair, _ in _wires_between(segments, boxes)}
    assert (boxes[0],boxes[1]) not in pairs
    assert pairs == {(boxes[0],boxes[2]), (boxes[2],boxes[1])}


def test_local_optical_path_does_not_override_manual_endpoint_uncertainty():
    anchors=[word("XB21").evidence(),word("XB22",(.6,.13,.68,.15)).evidence()]
    link={"domain":"光纤", "endpoints":["XB21","XB22"],"view":"overview",
          "evidence":word("XB21 ↔ XB22").evidence(),"endpoint_evidence":anchors}
    units=[unit("a",{"links":[link]}),unit("b",{"links":[{**link,"view":"detail"}]})]
    decision={"id":"human", "unit_id":"a", "text":"XB21", "status":"无法确定", **anchors[0]["bbox"]}
    store=SimpleNamespace(list_records=lambda table,**kwargs:[decision] if table=="pending_confirmations" else [])
    row,=run_engineering(store,"version",units=units,persist=False)["comparisons"]
    assert row["outcome"] == "unverified" and row["manual_conflicts"][0]["status"] == "无法确定"
    decision.update(status="已确认",confirmed_text="XB25")
    assert run_engineering(store,"version",units=units,persist=False)["comparisons"][0]["outcome"] == "unverified"
    decision["confirmed_text"]="XB21"
    assert run_engineering(store,"version",units=units,persist=False)["comparisons"][0]["outcome"] == "match"


def test_cad_same_signal_code_checks_wire_number_and_anchor():
    source = {"code": "SIG-1", "wire": "X23", "direction": "source", "evidence": word("SIG-1 X23").evidence()}
    destination = {**source, "wire": "X25", "direction": "destination", "evidence": word("SIG-1 X25").evidence()}
    units = [unit("a", {"signals":[source]}),unit("b", {"signals":[destination]})]
    result = run_engineering(SimpleNamespace(), "version", units=units, persist=False)
    assert result["comparisons"][0]["outcome"] == "mismatch"
    assert result["issues"][0]["category"] == "接线"


def test_cad_scope_requires_explicit_device_type_and_unique_tag():
    words = [word("5.本接线图表示新建2000kVA箱变1WBT06、1WBT07"), word("同名1WBT08")]
    assets = stated_assets(words,"箱变一次接线图")
    assert [(item["tag"],item["type"]) for item in assets] == [("1WBT06","箱变"),("1WBT07","箱变")]


def test_native_dwg_keeps_rendered_override_separate_from_geometry():
    import ezdxf
    from ezdxf import bbox
    from backend.parsers.dwg.engineering import frame_dimensions, native_dimensions

    doc = ezdxf.new()
    layout = doc.modelspace()
    dim = layout.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(59, 0), text="100")
    dim.render()
    rows = frame_dimensions(list(native_dimensions(layout, bbox.Cache())), (-10, -10, 100, 20))
    assert len(rows) == 1
    assert rows[0]["value"] == "100"
    assert rows[0]["geometry_value"] == 59
    assert rows[0]["evidence"]["source"] == Source.CAD_TEXT.value


def test_raster_fallback_handles_opencv_segment_array():
    from PIL import Image, ImageDraw
    from backend.extract.engineering import raster_segments

    img = Image.new("L", (600,400), color=255)
    ImageDraw.Draw(img).line((80,60,420,60), fill=0, width=2)
    aspect, segments = raster_segments(img)
    assert aspect == 400/600
    assert segments
    assert any(abs(a[0]-b[0]) > .5 and abs(a[1]-b[1]) < .01 for a,b in segments)


def test_cad_roof_marks_require_same_building_and_landmark_position():
    from backend.parsers.base import ParsedText
    from backend.parsers.dwg.engineering import frame_elevations
    from backend.store import BBox

    def roof(building, value, x_offset=0):
        texts = [
            ParsedText(building + "屋面布置图", Source.CAD_TEXT, BBox(.7,.91,.86,.93)),
            ParsedText("电梯机房", Source.CAD_TEXT, BBox(.25,.31,.30,.32)),
            ParsedText("女儿墙", Source.CAD_TEXT, BBox(.40,.45,.44,.47)),
            ParsedText(value, Source.CAD_TEXT, BBox(.385+x_offset,.26,.407+x_offset,.275)),
        ]
        strokes = [
            ((.401+x_offset,.72),(.406+x_offset,.715)),
            ((.406+x_offset,.715),(.411+x_offset,.72)),
            ((.380+x_offset,.72),(.411+x_offset,.72)),
        ]
        result = frame_elevations(strokes,(0,0,1,1),texts)
        assert len(result) == 1
        return {"measurements": result}

    units = [
        unit("a",roof("检修库","14.650")), unit("b",roof("检修库","14.651")),
        unit("c",roof("危废库","14.650")), unit("d",roof("检修库","14.650",.10)),
    ]
    result = run_engineering(SimpleNamespace(), "version", units=units, persist=False)
    assert [(row["left_unit_id"],row["right_unit_id"],row["outcome"])
            for row in result["comparisons"]] == [("a","b","unverified")]
    assert not result["issues"]


def test_cad_cable_schedule_pair_uses_explicit_column_and_preserves_code():
    from backend.extract.engineering import cad_cable_labels

    schedule = [
        word("开关柜编号", (.14,.30,.20,.31)), word("1BAA04", (.40,.30,.44,.31)),
        word("电缆编号", (.14,.60,.20,.61)), word("1BAA040001", (.385,.60,.44,.61)),
        word("1BAA040011", (.385,.65,.44,.66)),
    ]
    schematic = [word("#1BAA040001", (.34,.37,.40,.38)), word("#1BAA040011", (.41,.38,.48,.39))]
    assert not cad_cable_labels([word("CSC-830")], "2000kVA箱变一次接线图")
    units = [
        unit("a", {"cables": cad_cable_labels(schematic,"电气系统一次接线图")}),
        unit("b", {"cables": cad_cable_labels(schedule,"35kV配电装置接线图")}),
    ]
    result = run_engineering(SimpleNamespace(), "version", units=units, persist=False)
    assert [(r["label"],r["cabinet"],r["outcome"]) for r in result["comparisons"]] == [
        ("电缆编号 1BAA040001","1BAA04","match"),
    ]


def test_native_schedule_requires_actual_leader_arrow_and_column_bounds():
    from backend.parsers.dwg.engineering import frame_connections
    # Independent drawing-space inputs: one ruled cabinet column, its cable,
    # an outgoing triangle, and a two-segment annotation leader.
    def text(handle, literal, box):
        a,b,c,d = box
        return {"kind":"TEXT", "handle":handle, "text":literal, "bounds":(a,1-d,c,1-b)}
    def path(handle, points, closed=False, kind="LWPOLYLINE"):
        xs,ys=zip(*points)
        return {"kind":kind, "handle":handle, "points":[(x,1-y) for x,y in points], "closed":closed,
                "bounds":(min(xs),1-max(ys),max(xs),1-min(ys))}
    values = [text("cab-head","开关柜编号",(.12,.37,.20,.38)),
              text("wire-head","电缆编号",(.12,.67,.20,.68)),
              text("cab","TA19",(.46,.37,.48,.38)), text("cable","TA190001",(.45,.67,.47,.68)),
              text("note","接入新建2500kVA箱变XB21、XB22",(.32,.775,.45,.788)),
              path("left",[(.43,.18),(.43,.71)],kind="LINE"),
              path("right",[(.50,.18),(.50,.71)],kind="LINE"),
              path("arrow",[(.456,.72),(.46,.729),(.464,.72)],closed=True),
              path("leader",[(.46,.745),(.448,.785),(.32,.785)])]
    row, = frame_connections(values,(0,0,1,1))
    assert (row["circuit_code"],row["cabinet"],row["endpoint_tags"]) == ("TA190001","TA19",["XB21","XB22"])
    assert {"left","right","arrow","leader","cable","cab","note"} <= set(row["geometry"]["handles"])
    for missing in ("arrow","leader","left","right"):
        assert frame_connections([value for value in values if value["handle"] != missing],(0,0,1,1)) == []


def test_geometry_connection_never_pairs_just_a_code_and_preserves_partial_coverage():
    record = {"circuit_code":"TA190001", "cabinet":"TA19", "endpoint_tags":["XB21"], "view":"schedule",
              "evidence":word("TA190001").evidence(), "endpoint_evidence":[word("XB21").evidence()],
              "geometry":{"handles":["real-source-handle"],"paths":[]}}
    units=[unit("a",{"connections":[record]}),unit("b",{"cables":[{"code":"TA190001","view":"schematic","evidence":word("TA190001").evidence()}]})]
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    assert not result["comparisons"]
    assert result["connection_coverage"][0]["status"] == "one_sided_or_ambiguous"
    units[1]["metadata"]["local_sheet"]["engineering"]["connections"]=[{**record,"view":"schematic"}]
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    row,=result["comparisons"]
    assert row["domain"] == "电气线路端点" and row["outcome"] == "match"
    record["endpoint_tags"] = ["XB22"]
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    assert result["comparisons"][0]["outcome"] == "unverified" and not result["issues"]
    record["endpoint_set_complete"] = True
    units[1]["metadata"]["local_sheet"]["engineering"]["connections"][0]["endpoint_set_complete"] = True
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    assert result["comparisons"][0]["outcome"] == "mismatch" and len(result["issues"]) == 1


def test_explicit_access_continuation_preserves_intermediate_device_and_two_anchors():
    from backend.extract.engineering import stated_access_paths
    first=word("本区逆变器经汇流箱最终接入至利旧XB21箱变低压侧开关，",(.1,.4,.6,.41))
    second=word("与XB22箱变连接后，经原有XB22箱变35kV出线电缆接入原有一次舱TA19开关柜。",(.105,.416,.61,.426))
    row,=stated_access_paths([first,second,first])
    assert row["path"] == ["XB21","XB22","TA19"]
    assert row["relation"] == "reachable_access" and row["via"] == ["XB22"]
    assert len(row["endpoint_evidence"]) == 2 and "circuit_code" not in row and "geometry" not in row
    for invalid in [word(second.text,(.4,.416,.9,.426)),
                    word(second.text,(.105,.5,.61,.51)),
                    word(second.text.replace("原有XB22","原有XB23"),second.box),
                    word(second.text.replace("连接后","未连接后"),second.box)]:
        assert stated_access_paths([first,invalid]) == []
    ambiguous=word(first.text.replace("XB21","XB25"),first.box)
    assert stated_access_paths([first,ambiguous,second]) == []


def test_normal_access_comparison_is_one_member_reachability_not_direct_or_complete_network():
    first=word("本区最终接入至利旧XB21箱变低压侧开关，",(.1,.4,.6,.41))
    second=word("与XB22箱变连接后，经原有XB22箱变35kV出线电缆接入原有一次舱TA19开关柜。",(.105,.416,.61,.426))
    record={"circuit_code":"TA190001", "cabinet":"TA19", "endpoint_tags":["XB21","XB23"], "view":"schedule",
            "evidence":word("TA190001").evidence(), "endpoint_evidence":[word("TA19").evidence(),word("接入箱变XB21、XB23").evidence()],
            "geometry":{"handles":["leader","arrow","borders"],"paths":[]}}
    units=[{**unit("left",{}),"drawing_number":"LAB-A101-01","name":"电气系统一次接线图"},
           {**unit("right",{"connections":[record]}),"drawing_number":"LAB-A101-02","name":"配电装置接线图"}]
    facts=[{"id":str(index),"unit_id":"left","text":text.text,"source":text.source,"confidence":1.,**text.evidence()["bbox"]}
           for index,text in enumerate([first,second],1)]
    tables={"drawing_units":units,"facts":facts,"pending_confirmations":[]}
    store=SimpleNamespace(list_records=lambda name,**kwargs:tables.get(name,[]))
    result=run_engineering(store,"version",units=units,persist=False)
    row,=result["comparisons"]
    assert row["relation"] == "reachable_from_cabinet" and row["outcome"] == "match"
    assert row["statement_path"] == ["XB21","XB22","TA19"]
    assert len(row["endpoint_evidence"]["left"]) == 2
    assert {x["source_tag"]:x["outcome"] for x in result["reachable_access_coverage"]} == {"XB21":"match","XB23":"unverified"}
    assert result["connection_coverage"][0]["status"] == "partially_paired"
    geometric={"kind":"feeder_reachability","relation":"reachable_from_cabinet","cabinet":"TA19","source_tag":"XB21",
               "view":"schematic","evidence":word("XB21").evidence(),"endpoint_evidence":[word("XB21").evidence()],
               "geometry":{"handles":["independent-port-symbols"],"open_switch_components":[{"drawn_state":"open"}]},
               "wire_segment_markers":[{"text":"TA190001"},{"text":"TA190004"}]}
    units[0]["metadata"]["native_connections"]=[geometric]
    merged=run_engineering(store,"version",units=units,persist=False)
    assert len(merged["comparisons"])==1 and len(merged["comparisons"][0]["supporting_sources"])==2
    assert len(merged["comparisons"][0]["wire_segment_markers"])==2
    tables["pending_confirmations"]=[{"id":"human","unit_id":"left","text":"XB21","status":"无法确定",**word("XB21").evidence()["bbox"]}]
    assert run_engineering(store,"version",units=units,persist=False)["comparisons"][0]["outcome"]=="unverified"
    tables["pending_confirmations"]=[]
    units[0]["metadata"].pop("native_connections")
    # The intermediate device is not silently registered as another matching
    # endpoint, and an unrelated source file/volume cannot furnish the side.
    record["endpoint_tags"]=["XB22","XB23"]
    assert not run_engineering(store,"version",units=units,persist=False)["comparisons"]
    record["endpoint_tags"]=["XB21","XB23"]
    units[1]["original_id"]="other-file"
    assert not run_engineering(store,"version",units=units,persist=False)["comparisons"]
    units[1]["original_id"]="same-file"
    units[1]["drawing_number"]="LAB-A102-02"
    assert not run_engineering(store,"version",units=units,persist=False)["comparisons"]


def test_native_inventory_keeps_exactly_axis_aligned_source_wires():
    import ezdxf
    from ezdxf import bbox
    from backend.parsers.dwg.engineering import native_connections
    doc=ezdxf.new();model=doc.modelspace()
    first=model.add_line((0,0),(100,0));second=model.add_line((0,0),(0,100))
    values=list(native_connections(model,bbox.Cache()))
    assert {row["handle"] for row in values} == {first.dxf.handle,second.dxf.handle}
    assert len(values)==2 and all("points" in row for row in values)


def test_schematic_range_requires_rotated_cabinet_frame_and_actual_circuit_stroke():
    from backend.parsers.dwg.engineering import frame_connections
    def text(handle,literal,box):
        a,b,c,d=box
        return {"kind":"TEXT","handle":handle,"text":literal,"bounds":(a,1-d,c,1-b)}
    def line(handle,start,end):
        return {"kind":"LINE","handle":handle,"points":[(start[0],1-start[1]),(end[0],1-end[1])],"closed":False,
                "bounds":(min(start[0],end[0]),1-max(start[1],end[1]),max(start[0],end[0]),1-min(start[1],end[1]))}
    values=[text("cabinet","QA21",(.151,.14,.155,.162)),text("range","#TA190001~0003",(.205,.061,.25,.067)),
            line("left",(.15,.06),(.15,.18)),line("top",(.15,.06),(.185,.06)),
            line("bottom",(.15,.18),(.185,.18)),line("wire",(.204,.083),(.204,.15))]
    row,=frame_connections(values,(0,0,1,1))
    assert row["kind"]=="cable_range" and row["cabinet"]=="QA21" # The code prefix is not the cabinet identity.
    assert row["evidence"]["text"]=="#TA190001~0003"
    assert {"cabinet","range","left","top","bottom","wire"}==set(row["geometry"]["handles"])
    for missing in ("left","top","bottom","wire"):
        assert frame_connections([row for row in values if row["handle"]!=missing],(0,0,1,1))==[]
    assert frame_connections([*values,text("other-cabinet","QB22",(.151,.10,.155,.122))],(0,0,1,1))==[]


def test_range_literal_comparison_requires_same_cabinet_and_field_preserves_raw_endings():
    from backend.extract.engineering import cable_range
    def reading(literal,view,cabinet="QA21"):
        return {"kind":"cable_range",**cable_range(literal),"cabinet":cabinet,"view":view,
                "evidence":word(literal).evidence(),"endpoint_evidence":[word(cabinet).evidence()],
                "geometry":{"handles":["frame","wire"],"paths":[]}}
    left=reading("#TA190001~0003","schematic");right=reading("TA190001~1003","schedule")
    units=[{**unit("a",{"connections":[left]}),"drawing_number":"LAB-A101-01"},
           {**unit("b",{"connections":[right]}),"drawing_number":"LAB-A101-02"}]
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    row,=result["comparisons"]
    assert row["outcome"]=="mismatch" and row["left_value"]=="#TA190001~0003" and row["right_value"]=="TA190001~1003"
    assert len(result["issues"])==1 and "疑似" in result["issues"][0]["title"]
    right.update(cable_range("TA190001～TA190003"))
    result=run_engineering(SimpleNamespace(),"version",units=units,persist=False)
    assert result["comparisons"][0]["outcome"]=="match" and not result["issues"]
    right["cabinet"]="QA22"
    assert not run_engineering(SimpleNamespace(),"version",units=units,persist=False)["comparisons"]
    right["cabinet"]="QA21";right["geometry"]["handles"]=[]
    assert not run_engineering(SimpleNamespace(),"version",units=units,persist=False)["comparisons"]
    for malformed in ("TA190001~0000","TA190001~QB190003","TA190001~0003~0004","TA190001"):
        assert cable_range(malformed) is None
