from backend.extract.engineering import FeederWireGraph, feeder_source_segments
from backend.parsers.dwg.engineering import frame_feeder_routes


def line(handle,a,b,style="CONTINUOUS"):
    return {"kind":"LINE","handle":handle,"points":[a,b],"closed":False,"linetype":style,"layer":"electrical",
            "bounds":(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))}


def text(handle,literal,box):
    return {"kind":"TEXT","handle":handle,"text":literal,"bounds":box,"layer":"annotations"}


def circle(handle,center,radius,kind="CIRCLE"):
    x,y=center
    return {"kind":kind,"handle":handle,"center":center,"radius":radius,"bounds":(x-radius,y-radius,x+radius,y+radius)}


def drawing():
    values=[text("cabinet","CS20",(100.5,727,104,747)),text("device","XT31",(210,485,228,488)),
            text("outgoing-code","CS200001",(132,650,154,653)),text("branch-code","CS200002",(205,550,227,553)),
            line("cab-left",(100,700),(100,800)),line("cab-top",(100,800),(120,800)),line("cab-bottom",(100,700),(120,700)),
            line("cab-wire",(130,720),(130,600),"DASHDOT2"),line("feeder",(130,600),(200,600),"DASHDOT2"),
            line("device-input",(200,600),(200,521),"DASHDOT2"),
            line("box-left",(198.5,500),(198.5,506)),line("box-right",(201.5,500),(201.5,506)),
            line("box-top",(198.5,506),(201.5,506)),line("box-bottom",(198.5,500),(201.5,500)),
            circle("primary-winding",(200,486),4),circle("secondary-winding",(200,480),4),
            line("delta-a",(200,488.4),(197.9,484.6)),line("delta-b",(197.9,484.6),(202.1,484.6)),line("delta-c",(202.1,484.6),(200,488.4)),
            line("star-a",(200,480),(197.6,482.4)),line("star-b",(200,480),(202.4,482.4)),line("star-c",(200,480),(200,477.2)),
            line("primary-terminal",(200,500),(200,490)),
            line("upper-blade",(200,516),(202,521.3)),line("upper-cap",(199.2,521),(200.8,521)),line("upper-stem",(200,516),(200,512)),
            line("lower-blade",(200,508),(202.2,513.4)),line("lower-cap",(198.8,512),(201.2,512)),line("lower-stem",(200,508),(200,506)),
            circle("lower-contact-arc",(200,512),.6,kind="ARC")]
    return values


def test_generic_feeder_reads_component_ports_and_preserves_two_distinct_cable_markers():
    rows=frame_feeder_routes(drawing(),(0,0,1000,1000))
    row,=rows
    assert (row["cabinet"],row["source_tag"],row["relation"])==("CS20","XT31","reachable_from_cabinet")
    assert {marker["text"] for marker in row["wire_segment_markers"]}=={"CS200001","CS200002"}
    assert len(row["geometry"]["open_switch_components"])==2
    assert all(component["drawn_state"]=="open" and not component["geometry_gap_bridged"] for component in row["geometry"]["open_switch_components"])
    assert row["geometry"]["transformer_primary_terminal"]["wire_handle"]=="primary-terminal"
    assert not any(path["handle"].startswith("box-") for path in row["geometry"]["wire_paths"])


def test_missing_symbol_part_does_not_gain_a_feeder_claim_from_nearby_text():
    for missing in ("primary-winding","delta-a","star-a","upper-blade","lower-cap","primary-terminal","cab-left","cab-wire"):
        assert frame_feeder_routes([item for item in drawing() if item["handle"]!=missing],(0,0,1000,1000))==[]
    duplicate=text("ambiguous-device","XT31",(250,485,268,488))
    assert frame_feeder_routes([*drawing(),duplicate],(0,0,1000,1000))==[]


def test_external_cable_and_every_component_interconnect_must_really_touch_its_port():
    cuts={"device-input":[(200,600),(200,541)],
          "upper-stem":[(200,516),(200,514)],
          "lower-stem":[(200,508),(200,507)],
          "primary-terminal":[(200,498),(200,490)]}
    assert len(frame_feeder_routes(drawing(),(0,0,1000,1000)))==1
    for handle,points in cuts.items():
        values=drawing();wire=next(item for item in values if item["handle"]==handle)
        wire.update(points=points,bounds=(min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)))
        assert frame_feeder_routes(values,(0,0,1000,1000))==[],handle


def test_code_prefix_does_not_assign_the_cabinet():
    values=drawing()
    next(item for item in values if item["handle"]=="cabinet")["text"]="QA22"
    row,=frame_feeder_routes(values,(0,0,1000,1000))
    assert row["cabinet"]=="QA22"


def test_wire_graph_keeps_crossings_and_open_contacts_disconnected():
    crossing=[line("horizontal",(0,0),(2,0)),line("vertical",(1,-1),(1,1))]
    assert FeederWireGraph(feeder_source_segments(crossing),.025).route_between_handles("horizontal","vertical") is None
    branch=[line("horizontal",(0,0),(2,0)),line("vertical",(1,0),(1,1))]
    assert FeederWireGraph(feeder_source_segments(branch),.025).route_between_handles("horizontal","vertical") is not None
    opened=[line("wire",(0,3),(0,2)),line("blade",(0,0),(1,2)),line("fixed-contact",(-.5,2),(.5,2))]
    graph=FeederWireGraph(feeder_source_segments(opened),.025)
    assert graph.route_between_handles("wire","blade") is None


def test_open_cable_polyline_is_allowed_but_closed_group_and_leader_are_not_wires():
    values=[{"kind":"LWPOLYLINE","handle":"cable","points":[(0,0),(1,0),(1,1)],"closed":False,"linetype":"DASHDOT2"},
            {"kind":"LWPOLYLINE","handle":"group","points":[(0,0),(1,0),(1,1),(0,1)],"closed":True,"linetype":"DASH"},
            {"kind":"LWPOLYLINE","handle":"leader","points":[(0,0),(1,0),(1,1)],"closed":False,"linetype":"CONTINUOUS"}]
    assert {edge["handle"] for edge in feeder_source_segments(values)}=={"cable"}
