"""Native DXF dimensions as displayed, with their raw measurements kept separate."""
from dataclasses import asdict
import math
import re

from backend.extract.engineering import DIMENSION, UNITS, clean, measurement
from backend.store.models import Source

from .frames import _bounds, _center_inside, normalized_box


def _dimensions(entities):
    from ezdxf.protocols import virtual_entities

    for entity in entities:
        kind = entity.dxftype()
        if kind == "DIMENSION":
            yield entity
        elif kind == "INSERT" and entity.block() is not None:
            if entity.mcount > 1:
                yield from _dimensions(entity.multi_insert())
            else:
                yield from _dimensions(virtual_entities(entity))


def native_dimensions(layout, cache):
    for entity in _dimensions(layout):
        if entity.dimtype not in (0, 1, 3, 4, 6):
            continue
        override = entity.dxf.get("text", "")
        if override and not override.strip():
            continue
        rendered = []
        for graphic in entity.virtual_entities():
            if graphic.dxftype() in ("TEXT", "MTEXT"):
                text = graphic.plain_text() if graphic.dxftype() == "MTEXT" else graphic.dxf.text
                box = _bounds([graphic], cache)
                if text and box:
                    rendered.append((text, box))
        shown = [(text,box) for text,box in rendered if DIMENSION.fullmatch(clean(text))]
        direct = DIMENSION.fullmatch(clean(override))
        if direct:
            box = shown[0][1] if len(shown) == 1 else _bounds([entity], cache)
            label = override
        elif len(shown) == 1 and (not override or "<>" in override):
            label, box = shown[0]
        else:
            continue
        parsed = DIMENSION.fullmatch(clean(label))
        if not box or not parsed:
            continue
        try:
            geometry = entity.get_measurement()
            geometry = float(geometry) if math.isfinite(float(geometry)) else None
        except (TypeError, ValueError, AttributeError, OverflowError):
            geometry = None
        yield label, parsed.group("value"), UNITS.get((parsed.group("unit") or "").lower(), ""), box, geometry


def frame_dimensions(values, bounds):
    rows = []
    seen = set()
    for label, value, unit, box, geometry in values:
        if not _center_inside(box, bounds):
            continue
        anchor = normalized_box(bounds, box)
        key = (value, unit, anchor)
        if key in seen:
            continue
        seen.add(key)
        evidence = {"text": label, "source": Source.CAD_TEXT.value, "confidence": 1., "bbox": asdict(anchor)}
        rows.append(measurement("尺寸", evidence, value, unit, method="DWG 尺寸对象", geometry_value=geometry))
    return rows


_SIGNAL_BLOCK = re.compile(r"HA[0-9]([SD])[1-4]", re.I)


def native_signals(layout, cache):
    # AutoCAD Electrical assigns SIGCODE to a directional wire signal symbol.
    # A generic text label with the same value does not establish a wire link.
    for entity in layout.query("INSERT"):
        kind = _SIGNAL_BLOCK.fullmatch(entity.dxf.name)
        if not kind:
            continue
        code = entity.get_attrib_text("SIGCODE").strip()
        wire = entity.get_attrib_text("WIRENO").strip()
        box = _bounds([entity], cache)
        if 0 < len(code) <= 32 and box:
            yield ("source" if kind.group(1).upper() == "S" else "destination", code, wire, box)


def frame_signals(values, bounds):
    return [
        {"direction": direction, "code": code, "wire": wire,
         "evidence": {"text": f"{code} · {wire}" if wire else code, "source": Source.CAD_TEXT.value,
                      "confidence": 1., "bbox": asdict(normalized_box(bounds, box))}}
        for direction, code, wire, box in values if _center_inside(box, bounds)
    ]


def native_connections(layout, cache):
    """Keep original CAD handles and paths for frame-local circuit binding.

    This is a read-only inventory. It does not join crossing strokes or bridge
    gaps through switches, arrowheads, or a missing block.
    """
    from .frames import _safe_decompose
    from .render import rendered_text_bounds
    entities = list(_safe_decompose(layout))
    text_entities = [entity for entity in entities if entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB"}]
    text_boxes, _ = rendered_text_bounds(layout.doc, text_entities)
    positioned = {id(entity): box for entity, box in zip(text_entities, text_boxes)}
    for entity in entities:
        kind = entity.dxftype()
        if kind not in {"TEXT", "MTEXT", "ATTRIB", "LINE", "LWPOLYLINE", "CIRCLE", "ARC"}:
            continue
        style=entity.dxf.get("linetype","BYLAYER").upper()
        if style=="BYLAYER":
            style=layout.doc.layers.get(entity.dxf.layer).dxf.linetype.upper()
        item = {"kind": kind, "handle": entity.dxf.get("handle"),"linetype":style,
                "layer":entity.dxf.layer,"color":entity.dxf.get("color",256)}
        if kind in {"TEXT", "MTEXT", "ATTRIB"}:
            box = positioned.get(id(entity))
            if not box:
                continue
            item["text"] = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
        elif kind == "LINE":
            item["points"] = [tuple(entity.dxf.start)[:2], tuple(entity.dxf.end)[:2]]
            item["closed"] = False
        elif kind=="LWPOLYLINE":
            # Bulges are curved geometry; never flatten one into a fictitious
            # straight leader or triangle.
            if any(abs(vertex[2]) > 1e-9 for vertex in entity.get_points("xyb")):
                continue
            item["points"] = [tuple(point) for point in entity.get_points("xy")]
            item["closed"] = entity.closed
        else:
            item["center"]=tuple(entity.dxf.center)[:2]
            item["radius"]=entity.dxf.radius
            x,y=item["center"];radius=item["radius"]
            box=x-radius,y-radius,x+radius,y+radius
            if kind=="ARC":
                item.update(start_angle=entity.dxf.start_angle,end_angle=entity.dxf.end_angle)
        if "points" in item:
            if not item["points"]:
                continue
            xs,ys = zip(*item["points"])
            # A legitimate horizontal/vertical wire has a zero-height/width
            # bbox. The text-box helper intentionally rejects those; geometry
            # must use its actual endpoints rather than lose the whole stroke.
            box = min(xs),min(ys),max(xs),max(ys)
        item["bounds"] = box
        yield item


_CONNECTION_NOTE = re.compile(
    r"^接入(?:利旧|新建|原有)?(?:\d+(?:\.\d+)?KVA)?(?:箱变|变压器)"
    r"(?P<tags>\d?[A-Z]{2,7}-?\d{2,5}(?:[、,]\d?[A-Z]{2,7}-?\d{2,5})*)$"
)


def _frame_cable_ranges(items):
    from backend.extract.engineering import TAG, cable_range
    texts=[item for item in items if "text" in item]
    lines=[item for item in items if item["kind"]=="LINE" and len(item["points"])==2 and item.get("handle")]
    def cx(item):return (item["box"]["x0"]+item["box"]["x1"])/2
    def cy(item):return (item["box"]["y0"]+item["box"]["y1"])/2
    def token(item):return clean(item["text"]).upper().removeprefix("#")
    def proof(item):return {"text":item["text"],"source":Source.CAD_TEXT.value,"confidence":1.,"bbox":item["box"],"source_handle":item.get("handle")}
    vertical=[item for item in lines if abs(item["points"][0][0]-item["points"][1][0])<.00005]
    horizontal=[item for item in lines if abs(item["points"][0][1]-item["points"][1][1])<.00005]
    heads={name:[item for item in texts if token(item)==name] for name in ("开关柜编号","电缆编号")}
    rows=[]
    for label in texts:
        parsed=cable_range(label["text"])
        if not parsed:
            continue
        bindings=[]
        if all(len(found)==1 for found in heads.values()):
            ch,wh=(heads[name][0] for name in ("开关柜编号","电缆编号"))
            if abs(cy(label)-cy(wh))<.012 and cy(ch)<cy(wh):
                borders=[line for line in vertical if line["box"]["y0"]<cy(ch) and line["box"]["y1"]>label["box"]["y1"]]
                left=max((line["points"][0][0] for line in borders if line["points"][0][0]<cx(label)),default=None)
                right=min((line["points"][0][0] for line in borders if line["points"][0][0]>cx(label)),default=None)
                if left is not None and right is not None:
                    cabinets=[item for item in texts if TAG.fullmatch(token(item)) and left<cx(item)<right and abs(cy(item)-cy(ch))<.012]
                    if len(cabinets)==1:
                        borders=[next(line for line in borders if abs(line["points"][0][0]-x)<1e-7) for x in (left,right)]
                        bindings.append((cabinets[0],"schedule",borders,[ch,wh],"电缆编号物理行与封闭柜列"))
        else:
            # A schematic cabinet has a rotated identifier in the narrow left
            # strip of its technical-data frame. Bind the range at that frame's
            # top to the actual adjacent vertical circuit stroke. No code prefix
            # or nearest cabinet text participates in this association.
            for side in vertical:
                box=side["box"];height=box["y1"]-box["y0"]
                if not .05<height<.3:
                    continue
                tops=[line for line in horizontal if abs(line["box"]["x0"]-box["x0"])<.0002 and abs(line["box"]["y0"]-box["y0"])<.0002 and .015<line["box"]["x1"]-box["x0"]<.10]
                bottoms=[line for line in horizontal if abs(line["box"]["x0"]-box["x0"])<.0002 and abs(line["box"]["y0"]-box["y1"])<.0002]
                for top in tops:
                    bottom=next((line for line in bottoms if abs(line["box"]["x1"]-top["box"]["x1"])<.003),None)
                    width=top["box"]["x1"]-box["x0"]
                    if not bottom or not top["box"]["x1"]-.005<label["box"]["x0"]<top["box"]["x1"]+1.5*width or abs(label["box"]["y0"]-box["y0"])>min(.01,.1*height):
                        continue
                    cabinets=[item for item in texts if TAG.fullmatch(token(item)) and
                              item["box"]["y1"]-item["box"]["y0"]>2*(item["box"]["x1"]-item["box"]["x0"]) and
                              abs(cx(item)-box["x0"])<.006 and box["y0"]<cy(item)<box["y1"]]
                    wires=[line for line in vertical if abs(line["box"]["x0"]-label["box"]["x0"])<.003 and
                           0<=line["box"]["y0"]-label["box"]["y1"]<.025 and
                           line["box"]["y1"]>box["y0"]+.4*height and line["box"]["y1"]<=box["y1"]+.005]
                    if len(cabinets)==1 and len(wires)==1:
                        bindings.append((cabinets[0],"schematic",[side,top,bottom,wires[0]],[],"装置框的竖排柜号、顶部编号标注及相邻实际回路导线"))
        # Coincident duplicated frame strokes can repeat one identical binding.
        unique={(token(cabinet),view): (cabinet,view,geometry,headers,method) for cabinet,view,geometry,headers,method in bindings}
        if len(unique)!=1:
            continue
        cabinet,view,geometry,headers,method=next(iter(unique.values()))
        rows.append({"kind":"cable_range",**parsed,"cabinet":token(cabinet),"view":view,"method":method,
                     "evidence":proof(label),"endpoint_evidence":[proof(cabinet),*(proof(header) for header in headers)],
                     "geometry":{"handles":sorted({item["handle"] for item in [label,cabinet,*headers,*geometry] if item.get("handle")}),
                                 "paths":[{"handle":item["handle"],"points":item["points"],"closed":False} for item in geometry]}})
    return rows


def frame_connections(values, bounds):
    """Bind schedule notes by a real leader, arrow, and ruled cabinet column."""
    from backend.extract.engineering import TAG, _CABLE_CODE
    values=list(values)
    items = []
    x0, y0, x1, y1 = bounds
    for original in values:
        if not _center_inside(original["bounds"], bounds):
            continue
        item = {**original, "box": asdict(normalized_box(bounds, original["bounds"]))}
        item["points"] = [((x-x0)/(x1-x0), (y1-y)/(y1-y0)) for x,y in original.get("points", ())]
        items.append(item)
    texts = [item for item in items if "text" in item]
    range_rows = _frame_cable_ranges(items)+frame_feeder_routes(values,bounds)

    def token(item):
        return clean(item["text"]).upper().removeprefix("#")

    def cx(item):
        return (item["box"]["x0"]+item["box"]["x1"])/2

    def cy(item):
        return (item["box"]["y0"]+item["box"]["y1"])/2

    headers = {name: [item for item in texts if token(item) == name]
               for name in ("开关柜编号", "电缆编号")}
    if not all(len(rows) == 1 for rows in headers.values()):
        return range_rows
    cabinet_header, cable_header = (headers[name][0] for name in ("开关柜编号", "电缆编号"))
    if cy(cabinet_header) >= cy(cable_header):
        return range_rows
    cabinets = [item for item in texts if TAG.fullmatch(token(item)) and abs(cy(item)-cy(cabinet_header)) < .012
                and cx(item) > cx(cabinet_header)+.04]
    cables = [item for item in texts if _CABLE_CODE.fullmatch(token(item)) and abs(cy(item)-cy(cable_header)) < .012
              and cx(item) > cx(cable_header)+.04]
    vertical = [item for item in items if item["kind"] == "LINE" and len(item["points"]) == 2
                and abs(item["points"][0][0]-item["points"][1][0]) < .00015
                and min(point[1] for point in item["points"]) < cy(cabinet_header)
                and max(point[1] for point in item["points"]) > cy(cable_header)-.02]
    triangles = [item for item in items if item.get("closed") and len(item["points"]) == 3
                 and .002 < item["box"]["x1"]-item["box"]["x0"] < .02
                 and .003 < item["box"]["y1"]-item["box"]["y0"] < .025]
    leaders = [item for item in items if item["kind"] == "LWPOLYLINE" and not item.get("closed")
               and len(item["points"]) == 3 and item.get("handle")]
    rows = []

    def evidence(item):
        return {"text": item["text"], "source": Source.CAD_TEXT.value, "confidence": 1., "bbox": item["box"],
                "source_handle": item.get("handle")}

    for cable in cables:
        borders = sorted({round(item["points"][0][0], 6) for item in vertical})
        lower = max((x for x in borders if x < cx(cable)), default=None)
        upper = min((x for x in borders if x > cx(cable)), default=None)
        if lower is None or upper is None:
            continue
        column = [item for item in cabinets if lower < cx(item) < upper]
        same_cell = [item for item in cables if lower < cx(item) < upper]
        if len(column) != 1 or len(same_cell) != 1:
            continue
        arrows = [item for item in triangles if abs(cx(item)-cx(cable)) < .002
                  and .015 < item["box"]["y0"]-cable["box"]["y1"] < .055]
        if len(arrows) != 1:
            continue
        arrow = arrows[0]
        bound = []
        for leader in leaders:
            for points in (leader["points"], list(reversed(leader["points"]))):
                near, elbow, end = points
                if (abs(near[0]-cx(arrow)) > .001 or not .003 < near[1]-arrow["box"]["y1"] < .025
                        or abs(elbow[1]-end[1]) > .0002 or abs(end[0]-elbow[0]) < .025
                        or elbow[1] <= near[1]):
                    continue
                notes = [item for item in texts if _CONNECTION_NOTE.fullmatch(token(item))
                         and abs(item["box"]["x0"]-min(end[0], elbow[0])) < .003
                         and -.018 < item["box"]["y0"]-end[1] < .016]
                if not notes:
                    continue
                bound.append((leader, notes))
        if len(bound) != 1:
            continue
        leader, notes = bound[0]
        tags = sorted({tag for note in notes for tag in re.split(r"[、,]", _CONNECTION_NOTE.fullmatch(token(note))["tags"])})
        borders_used = [item for item in vertical if round(item["points"][0][0], 6) in {lower, upper}]
        geometry_items = [leader, arrow, *borders_used]
        rows.append({"circuit_code": token(cable), "cabinet": token(column[0]), "endpoint_tags": tags,
                     "view": "schedule", "method": "CAD引线、出线箭头与封闭柜列表格",
                     "evidence": evidence(cable), "endpoint_evidence": [evidence(column[0]), *(evidence(note) for note in notes)],
                     "geometry": {"handles": sorted({item["handle"] for item in [cable, column[0], *notes, *geometry_items] if item.get("handle")}),
                                  "paths": [{"handle": item.get("handle"), "points": item["points"], "closed": item.get("closed", False)}
                                            for item in geometry_items if item.get("handle")]}})
    return rows+range_rows


_ELEVATION_LAYER = re.compile(r"(?:^|[|])(?:DIM[_-]?ELEV|标高|高程)$", re.I)
_ROOF = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9#-]{2,28}(?:库|房|楼|车间|建筑|站|馆|舍))屋[顶面].*图$")
_LANDMARK = re.compile(r"^(?:[\u4e00-\u9fff]{0,8}机房|[\u4e00-\u9fff]{0,8}楼梯间|女儿墙)$")


def native_level_strokes(layout):
    for entity in layout.query("LINE"):
        if _ELEVATION_LAYER.search(entity.dxf.layer):
            yield tuple(entity.dxf.start)[:2], tuple(entity.dxf.end)[:2]


def frame_elevations(strokes, bounds, texts):
    from backend.extract.engineering import LEVEL, elevation_guides

    if not strokes:
        return []
    x0,y0,x1,y1 = bounds
    width, height = x1-x0, y1-y0
    # One scale on both axes preserves the triangle angles.
    segments = [
        (((a-x0)/width, (y1-b)/width), ((c-x0)/width, (y1-d)/width))
        for (a,b),(c,d) in strokes
        if x0 <= min(a,c) <= max(a,c) <= x1 and y0 <= min(b,d) <= max(b,d) <= y1
    ]
    guides = elevation_guides(segments)
    if not guides:
        return []
    aspect = height/width
    buildings = set()
    landmarks = {}
    candidates = []
    for text in texts:
        literal = clean(text.text)
        box = text.bbox
        if box.y0 > .84 and box.x0 > .54 and (match := _ROOF.fullmatch(literal)):
            buildings.add(match.group(1))
        if box.y0 < .85 and _LANDMARK.fullmatch(literal):
            landmarks.setdefault(literal, []).append(box)
        if match := LEVEL.fullmatch(literal):
            candidates.append((match, text))
    unique_landmarks = [(name, boxes[0]) for name,boxes in sorted(landmarks.items()) if len(boxes) == 1]
    # A matching building caption and two translated structural landmarks are
    # required for comparison. Geometry, rather than the height text, keys the mark.
    building = next(iter(buildings)) if len(buildings) == 1 and len(unique_landmarks) >= 2 else None
    rows = []
    for n,(left,y,right) in enumerate(guides, 1):
        hits = [(match,text) for match,text in candidates
                if -.001 <= y-text.bbox.y1*aspect <= .012
                and text.bbox.x0 < right and left-.035 <= text.bbox.x1 <= right+.008]
        if len(hits) != 1:
            continue
        match, text = hits[0]
        label = match.group("label") or next((name for name,box in unique_landmarks
            if -.003 <= box.x0-text.bbox.x1 <= .008 and abs(box.y0-text.bbox.y0) <= .0015), "")
        evidence = {"text": text.text, "source": Source.CAD_TEXT.value, "confidence": 1., "bbox": asdict(text.bbox)}
        context = {}
        if building:
            marker_x = (left+right)/2
            position = tuple((name, round(marker_x-(box.x0+box.x1)/2,4),
                              round(y-(box.y0+box.y1)*aspect/2,4)) for name,box in unique_landmarks)
            context = {"building": building, "position_key": repr(position)}
        unit = UNITS.get((match.group("unit") or "").lower(), "")
        rows.append(measurement("标高", evidence, match.group("value"), unit,
                                f"{label}标高" if label else f"标高点{n}",
                                datum="同一屋面底图" if context else "", method="DWG 标高符号", **context))
    return rows


def frame_feeder_routes(values, bounds):
    """Read feeder membership through explicit wires and recognized device ports.

    Wire edges never bridge an open switch. The separate component relations
    identify the apparatus behind that upstream input, and retain drawn state.
    """
    from collections import defaultdict
    from backend.extract.engineering import TAG, _CABLE_CODE, FeederWireGraph, feeder_source_segments
    values=[item for item in values if _center_inside(item['bounds'],bounds)]
    texts=[item for item in values if 'text' in item and item.get('handle')]
    token=lambda item:clean(item['text']).upper().removeprefix('#')
    rotated=[item for item in texts if TAG.fullmatch(token(item)) and
             item['bounds'][3]-item['bounds'][1]>2*(item['bounds'][2]-item['bounds'][0])]
    if not rotated:
        return []
    width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
    epsilon=width*.00003
    lines=[]
    for item in values:
        if item['kind']=='LINE' or (item['kind']=='LWPOLYLINE' and not item.get('closed') and item.get('linetype')=='DASHDOT2'):
            for index,(a,b) in enumerate(zip(item['points'],item['points'][1:])):
                lines.append({**item,'points':[a,b],'segment_index':index,
                              'bounds':(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))})
    vertical=[item for item in lines if abs(item['points'][0][0]-item['points'][1][0])<epsilon]
    horizontal=[item for item in lines if abs(item['points'][0][1]-item['points'][1][1])<epsilon]
    diagonal=[item for item in lines if min(abs(item['points'][0][0]-item['points'][1][0]),abs(item['points'][0][1]-item['points'][1][1]))>epsilon]
    circles=[item for item in values if item['kind']=='CIRCLE']
    arcs=[item for item in values if item['kind']=='ARC']
    if not circles:
        return []
    midpoint=lambda item:((item['points'][0][0]+item['points'][1][0])/2,(item['points'][0][1]+item['points'][1][1])/2)

    def proof(item):
        return {'text':item['text'],'source':Source.CAD_TEXT.value,'confidence':1.,'source_handle':item['handle'],
                'bbox':asdict(normalized_box(bounds,item['bounds']))}

    def unique_geometry(items):
        unique={}
        for item in items:
            key=tuple(sorted(tuple(round(value/epsilon) for value in point) for point in item['points']))
            unique.setdefault(key,item)
        return list(unique.values())

    def cover_horizontal(y,left,right):
        candidates=sorted((item for item in horizontal if abs(item['bounds'][1]-y)<epsilon and
                           item['bounds'][2]>=left-epsilon and item['bounds'][0]<=right+epsilon),key=lambda item:item['bounds'][0])
        end=left;used=[]
        for item in candidates:
            if item['bounds'][0]>end+epsilon:
                break
            if item['bounds'][2]>end:
                used.append(item['handle']);end=item['bounds'][2]
        return used if end>=right-epsilon else []

    # Rectangular device bodies are recognized as symbols and excluded from the
    # wire graph. Walking their perimeters would invent a conductive bypass.
    rectangles={}
    for left in vertical:
        x,low,_,high=left['bounds']
        if not width*.002<high-low<width*.04:
            continue
        for right in vertical:
            X=right['bounds'][0]
            if (not width*.0007<X-x<width*.018 or abs(right['bounds'][1]-low)>epsilon or
                    abs(right['bounds'][3]-high)>epsilon or not .75<(high-low)/(X-x)<6):
                continue
            top,bottom=cover_horizontal(high,x,X),cover_horizontal(low,x,X)
            if top and bottom:
                key=tuple(round(value/epsilon) for value in (x,low,X,high))
                rectangles[key]={'bounds':(x,low,X,high),'handles':[left['handle'],right['handle'],*top,*bottom]}
    rectangles=list(rectangles.values())

    def winding_motifs(circle):
        center,radius=circle['center'],circle['radius'];positions=[];adjacent=defaultdict(set);handles=defaultdict(set)
        def node(point):
            for index,prior in enumerate(positions):
                if math.dist(point,prior)<epsilon:
                    return index
            positions.append(point);return len(positions)-1
        for line in lines:
            if (not all(math.dist(point,center)<radius*.96 for point in line['points']) or
                    math.dist(*line['points'])<=radius*.3):
                continue
            first,second=(node(point) for point in line['points'])
            adjacent[first].add(second);adjacent[second].add(first)
            handles[min(first,second),max(first,second)].add(line['handle'])
        seen=set();motifs=[]
        for root in adjacent:
            if root in seen:
                continue
            pending=[root];component=set()
            while pending:
                current=pending.pop()
                if current in component:
                    continue
                component.add(current);seen.add(current);pending.extend(adjacent[current]-component)
            degrees=sorted(len(adjacent[index]) for index in component)
            name='delta' if degrees==[2,2,2] else 'star' if degrees==[1,1,1,3] else None
            if name:
                motifs.append({'winding':name,'handles':sorted({handle for (i,j),group in handles.items()
                                                              if i in component and j in component for handle in group})})
        return motifs

    devices=[]
    for label in texts:
        if not TAG.fullmatch(token(label)) or label['bounds'][2]-label['bounds'][0]<=2*(label['bounds'][3]-label['bounds'][1]):
            continue
        placements={tuple(round(value/epsilon) for value in candidate['bounds']) for candidate in texts if token(candidate)==token(label)}
        if len(placements)!=1:
            continue
        L,B,R,T=label['bounds'];cy=(B+T)/2;candidates=[]
        for rectangle in rectangles:
            x0,y0,x1,y1=rectangle['bounds'];rh,rw=y1-y0,x1-x0;dx=max(x0-R,L-x1,0);dy=(y0+y1)/2-cy
            if -rh<dy<6*rh and dx<max(R-L,4*rw):
                candidates.append(rectangle)
        if len(candidates)!=1:
            continue
        rectangle=candidates[0];x0,y0,x1,y1=rectangle['bounds'];rw,rh=x1-x0,y1-y0;axis=(x0+x1)/2
        nearby=[circle for circle in circles if abs(circle['center'][0]-axis)<rw and y0-8*rh<circle['center'][1]<y0
                and rw*.75<circle['radius']<rh*2]
        pairs=[]
        for index,first in enumerate(nearby):
            for second in nearby[:index]:
                radius=first['radius'];dy=abs(first['center'][1]-second['center'][1])
                if (abs(radius-second['radius'])<epsilon and abs(first['center'][0]-second['center'][0])<rw*.2 and
                        .4*radius<dy<1.8*radius):
                    pairs.append([first,second])
        if len(pairs)!=1:
            continue
        winding=[{**circle,'motifs':winding_motifs(circle)} for circle in pairs[0]]
        if {motif['winding'] for circle in winding for motif in circle['motifs']}!={'delta','star'}:
            continue
        incoming=[]
        for wire in vertical:
            if wire.get('linetype')!='DASHDOT2':
                continue
            x=midpoint(wire)[0];end=wire['bounds'][1]
            if x0-rw*.5<x<x1+rw*.5 and 0<end-y1<8*rh:
                incoming.append((end-y1,wire))
        if not incoming:
            continue
        nearest=min(delta for delta,_ in incoming)
        inputs=unique_geometry([wire for delta,wire in incoming if delta-nearest<epsilon])
        if len(inputs)!=1:
            continue
        switches=[]
        for blade in diagonal:
            if blade.get('linetype')!='CONTINUOUS':
                continue
            low,high=sorted(blade['points'],key=lambda point:point[1]);dx,dy=abs(high[0]-low[0]),high[1]-low[1]
            if not rw*.4<dx<rw*2 or not rh*.6<dy<rh*1.7 or abs(low[0]-axis)>rw*.15 or not y1<low[1]<y1+4*rh:
                continue
            parts=[line for line in horizontal if
                   (min(abs(line['points'][0][0]-axis),abs(line['points'][1][0]-axis))<rw*.3 or abs(midpoint(line)[0]-axis)<rw*.3)
                   and rw*.15<math.dist(*line['points'])<rw*1.8 and abs(midpoint(line)[1]-high[1])<rh*.35]
            capgroups=defaultdict(list)
            for part in parts:
                capgroups[round(midpoint(part)[1]/epsilon)].append(part)
            contacts=[]
            for group in capgroups.values():
                left=min(line['bounds'][0] for line in group);right=max(line['bounds'][2] for line in group);y=midpoint(group[0])[1]
                if left<axis<right and rw*.45<right-left<rw*2.2:
                    contacts.append({'points':[(left,y),(right,y)],'handles':[line['handle'] for line in group]})
            stems=[wire for wire in vertical if abs(wire['bounds'][0]-low[0])<epsilon and
                   min(math.dist(point,low) for point in wire['points'])<epsilon]
            if len(contacts)!=1 or not stems:
                continue
            contact=contacts[0];gap=min(math.dist(high,point) for point in contact['points'])
            if gap<=epsilon*20:
                continue
            switches.append({'blade_handle':blade['handle'],'contact_handles':contact['handles'],
                             'contact_arc_handles':[arc['handle'] for arc in arcs if abs(arc['center'][0]-axis)<rw*.3 and
                                                    abs(arc['center'][1]-midpoint(contact)[1])<rh*.08 and arc['radius']<rw],
                             'stem_handles':[wire['handle'] for wire in stems],'upper_port_cad':midpoint(contact),'lower_port_cad':low,
                             'drawn_state':'open','drawn_gap_cad':gap,'geometry_gap_bridged':False})
        # Coincident duplicated source symbols are one component, not a second
        # switch or a second independent reading.
        switches=list({(tuple(round(value/epsilon) for value in switch['lower_port_cad']),
                        tuple(round(value/epsilon) for value in switch['upper_port_cad'])):switch for switch in switches}.values())
        if len(switches)!=2:
            continue
        ordered_switches=sorted(switches,key=lambda switch:switch['upper_port_cad'][1],reverse=True)
        incoming_terminal=min(inputs[0]['points'],key=lambda point:point[1])
        # The switch's *internal* free blade gap is a declared component-port
        # relation. Its external cable must actually terminate at the highest
        # stationary contact; proximity to the apparatus axis is insufficient.
        if math.dist(incoming_terminal,ordered_switches[0]['upper_port_cad'])>epsilon:
            continue
        primary=max(winding,key=lambda circle:circle['center'][1]);terminals=[]
        for line in lines:
            for index,point in enumerate(line['points']):
                if (abs(math.dist(point,primary['center'])-primary['radius'])<=epsilon and point[1]>primary['center'][1] and
                        abs(point[0]-axis)<rw and line['points'][1-index][1]>point[1]+epsilon):
                    terminals.append({'position_cad':point,'wire_handle':line['handle'],'symbol_handle':primary['handle']})
        unique_terminals=[]
        for terminal in terminals:
            if not any(math.dist(terminal['position_cad'],prior['position_cad'])<epsilon for prior in unique_terminals):
                unique_terminals.append(terminal)
        if len(unique_terminals)!=1:
            continue
        breaker_ports={}
        for name,level in (('upper',y1),('lower',y0)):
            contacts=[]
            for line in lines:
                first,second=line['points'];dy=second[1]-first[1]
                if abs(dy)<=epsilon:
                    continue
                fraction=max(0,min(1,(level-first[1])/dy))
                point=(first[0]+fraction*(second[0]-first[0]),first[1]+fraction*dy)
                if abs(point[1]-level)<=epsilon and x0+.2*rw<point[0]<x1-.2*rw:
                    if not any(math.dist(point,prior['position_cad'])<=epsilon for prior in contacts):
                        contacts.append({'position_cad':point,'source_handle':line['handle']})
            if len(contacts)!=1:
                break
            breaker_ports[name]=contacts[0]
        if len(breaker_ports)!=2:
            continue
        devices.append({'label':label,'rectangle':rectangle,'incoming':inputs[0],'windings':winding,
                        'switches':ordered_switches,'primary_terminal':unique_terminals[0],
                        'external_input_contact_cad':incoming_terminal,'breaker_ports':breaker_ports})
    if not devices:
        return []
    cabinet_bindings=[]
    for label in rotated:
        L,B,R,T=label['bounds'];cx,cy=(L+R)/2,(B+T)/2;bindings=[]
        for side in vertical:
            x0,y0,x1,y1=side['bounds'];h=y1-y0
            if not width*.05<h<width*.3 or abs(cx-(x0+x1)/2)>max(T-B,R-L) or not y0<cy<y1:
                continue
            for top in horizontal:
                if abs(top['bounds'][1]-y1)>epsilon or abs(top['bounds'][0]-x0)>epsilon:
                    continue
                body_width=top['bounds'][2]-x0
                if not width*.007<body_width<width*.1:
                    continue
                bottoms=[line for line in horizontal if abs(line['bounds'][1]-y0)<epsilon and abs(line['bounds'][0]-x0)<epsilon]
                if not bottoms:
                    continue
                bottom=max(bottoms,key=lambda line:line['bounds'][2]);right=max(top['bounds'][2],bottom['bounds'][2]);body_width=right-x0
                if abs(cx-x0)>min(body_width*.5,(T-B)*.3):
                    continue
                outgoing=unique_geometry([wire for wire in vertical if wire.get('linetype')=='DASHDOT2' and
                                          right<wire['bounds'][0]<right+body_width and y0<wire['bounds'][3]<y1 and wire['bounds'][1]<y0])
                if len(outgoing)==1:
                    bindings.append({'label':label,'outgoing':outgoing[0],'frame_handles':[side['handle'],top['handle'],bottom['handle']]})
        unique={binding['outgoing']['handle']:binding for binding in bindings}
        if len(unique)==1:
            cabinet_bindings.append(next(iter(unique.values())))
    if not cabinet_bindings:
        return []
    excluded={handle for rectangle in rectangles for handle in rectangle['handles']}
    port_points=[]
    for device in devices:
        for switch in device['switches']:
            port_points.extend([switch['upper_port_cad'],switch['lower_port_cad']])
        port_points.extend([device['breaker_ports']['upper']['position_cad'],device['breaker_ports']['lower']['position_cad'],device['primary_terminal']['position_cad']])
    graph=FeederWireGraph(feeder_source_segments(lines,excluded_handles=excluded),epsilon,additional_points=port_points)
    connected_devices=[]
    for device in devices:
        switches=device['switches'];x0,y0,x1,y1=device['rectangle']['bounds'];axis=(x0+x1)/2;rw=x1-x0
        links=[(switches[index]['lower_port_cad'],switches[index+1]['upper_port_cad']) for index in range(len(switches)-1)]
        links.extend([(switches[-1]['lower_port_cad'],device['breaker_ports']['upper']['position_cad']),
                      (device['breaker_ports']['lower']['position_cad'],device['primary_terminal']['position_cad'])])
        external=[]
        for first,second in links:
            route=graph.route_between_points(first,second)
            # These are local apparatus interconnects, not alternate routes
            # elsewhere in the feeder. Only the two identified switch gaps may
            # remain open; every external connection needs a real source path.
            if route is None or any(not (axis-2*rw-epsilon<=point[0]<=axis+2*rw+epsilon and
                                         min(first[1],second[1])-epsilon<=point[1]<=max(first[1],second[1])+epsilon)
                                    for edge in route for point in (edge['start'],edge['end'])):
                break
            external.append({'from_cad':first,'to_cad':second,'source_handles':list(dict.fromkeys(edge['handle'] for edge in route))})
        else:
            device['external_component_links']=external
            connected_devices.append(device)
    devices=connected_devices
    if not devices:
        return []
    cable_labels=[label for label in texts if _CABLE_CODE.fullmatch(token(label))]

    def label_distance(label,line):
        x0,y0,x1,y1=label['bounds'];a,b=line['points']
        if abs(a[0]-b[0])<epsilon and min(a[1],b[1])-epsilon<=(y0+y1)/2<=max(a[1],b[1])+epsilon:
            return max(x0-a[0],a[0]-x1,0)
        if abs(a[1]-b[1])<epsilon and min(a[0],b[0])-epsilon<=(x0+x1)/2<=max(a[0],b[0])+epsilon:
            return max(y0-a[1],a[1]-y1,0)
        return math.inf
    marker_bindings=[]
    for label in cable_labels:
        candidates=sorted(((label_distance(label,line),line) for line in lines if line.get('linetype')=='DASHDOT2'),key=lambda pair:pair[0])
        if not candidates or candidates[0][0]>3*(label['bounds'][3]-label['bounds'][1]):
            continue
        nearest=unique_geometry([line for delta,line in candidates if delta-candidates[0][0]<epsilon])
        if len(nearest)==1:
            marker_bindings.append((label,nearest[0]))
    def normal(point):return [(point[0]-bounds[0])/width,(bounds[3]-point[1])/height]
    results=[]
    for cabinet in cabinet_bindings:
        for device in devices:
            route=graph.route_between_handles(cabinet['outgoing']['handle'],device['incoming']['handle'])
            if route is None:
                continue
            ordered=list(dict.fromkeys([cabinet['outgoing']['handle'],*(edge['handle'] for edge in route),device['incoming']['handle']]))
            markers=[{'text':label['text'],'text_evidence':proof(label),'wire_handle':line['handle']} for label,line in marker_bindings if line['handle'] in ordered]
            if not markers:
                continue
            markers.sort(key=lambda marker:ordered.index(marker['wire_handle']))
            handles=set(ordered+cabinet['frame_handles']+device['rectangle']['handles']+[cabinet['label']['handle'],device['label']['handle']])
            for winding in device['windings']:
                handles.add(winding['handle'])
                for motif in winding['motifs']:handles.update(motif['handles'])
            for switch in device['switches']:
                handles.update([switch['blade_handle'],*switch['contact_handles'],*switch['contact_arc_handles'],*switch['stem_handles']])
            results.append({'kind':'feeder_reachability','view':'schematic','relation':'reachable_from_cabinet',
                'cabinet':token(cabinet['label']),'source_tag':token(device['label']),
                'method':'独立柜框与出线端口、真实导线/T节点及具名箱变开关组件端口',
                'evidence':proof(device['label']),
                'endpoint_evidence':[proof(cabinet['label']),proof(device['label']),*(marker['text_evidence'] for marker in markers)],
                'wire_segment_markers':markers,
                'geometry':{'handles':sorted(handles),'cad_rounding_epsilon':epsilon,'wire_junction_policy':'endpoint_termination_only',
                            'wire_paths':[{'handle':edge['handle'],'points':[normal(edge['start']),normal(edge['end'])],
                                           'linetype':edge.get('linetype')} for edge in route],
                            'cabinet_frame_handles':cabinet['frame_handles'],'cabinet_outgoing_handle':cabinet['outgoing']['handle'],
                            'upstream_component_input_handle':device['incoming']['handle'],
                            'external_input_contact_cad':device['external_input_contact_cad'],
                            'external_component_links':device['external_component_links'],
                            'breaker_ports_cad':device['breaker_ports'],
                            'breaker_symbol_handles':device['rectangle']['handles'],'open_switch_components':device['switches'],
                            'transformer_windings':[{'handle':circle['handle'],'center_cad':circle['center'],'radius_cad':circle['radius'],
                                                     'connection_motifs':circle['motifs']} for circle in device['windings']],
                            'transformer_primary_terminal':device['primary_terminal']},
                'scope_limit':'馈线接入/设备归属；开关图示断开，不核实当前导通、单根电缆直接端点或完整终点清单'})
    return results
