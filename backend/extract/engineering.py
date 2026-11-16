"""Read positioned engineering values and optical links from local drawing evidence."""
from collections import defaultdict
from itertools import combinations
import math
import re
import unicodedata

from backend.store.models import Source


NUMBER = r"[+\-±]?(?:\d{1,6}(?:\.\d{1,4})?|\.\d{1,4})"
UNIT = r"毫米|厘米|米|mm|cm|m"
DIMENSION = re.compile(rf"(?P<value>(?:[ΦØR])?{NUMBER}(?:[×xX]{NUMBER}){{0,2}})(?P<unit>{UNIT})?", re.I)
ROLE = r"标高|净高|高度|长度|宽度|厚度|间距|跨度|直径|埋深|深度"
STATED = re.compile(
    rf"(?P<name>[\w#±.\-]{{0,28}}?)(?P<role>{ROLE})(?:为|[:=])?"
    rf"(?P<value>{NUMBER}(?:[×xX]{NUMBER}){{0,2}})(?P<unit>{UNIT})?(?![\d.])", re.I,
)
DATUM = re.compile(r"绝对|相对|黄海|国家高程|吴淞|当地高程")
TAG = re.compile(r"\d?[A-Z]{2,7}[-]?\d{2,5}(?:[-][A-Z0-9]+)?")
UNITS = {"毫米": "mm", "厘米": "cm", "米": "m", "mm": "mm", "cm": "cm", "m": "m"}
RELATIVE = re.compile(r"^±0(?:\.0+)?$")
LEVEL = re.compile(rf"(?P<value>[+\-±]?\d{{1,5}}\.\d{{2,4}})(?P<unit>{UNIT})?(?:\((?P<label>[\u4e00-\u9fff]{{1,8}})\)?)?", re.I)


def clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).replace("−", "-"))


def measurement(category, word, value, unit="", label="", datum="", method="标注文字", **extra):
    return {"category": category, "value": value, "unit": unit, "label": label,
            "datum": datum, "method": method, "evidence": word, **extra}


def _overlap(first, second):
    return all(first[i] < second[i+2] and second[i] < first[i+2] for i in (0, 1))


def unique_words(words):
    kept = defaultdict(list)
    for word in sorted(words, key=lambda w: -w.score):
        matches = kept[clean(word.text)]
        if not any(_overlap(word.box, other.box) for other in matches):
            matches.append(word)
    return [word for matches in kept.values() for word in matches]


def _datum(words):
    candidates = set()
    for word in words:
        if word.score < .88:
            continue
        text = clean(word.text)
        candidates.update(m.group() for m in DATUM.finditer(text) if "高程" in text or "标高" in text)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _point_segment(point, a, b, tolerance):
    x, y = point
    if abs(a[0] - b[0]) < tolerance:
        return abs(x - a[0]) <= tolerance and min(a[1], b[1])-tolerance <= y <= max(a[1], b[1])+tolerance
    if abs(a[1] - b[1]) < tolerance:
        return abs(y - a[1]) <= tolerance and min(a[0], b[0])-tolerance <= x <= max(a[0], b[0])+tolerance
    return False


def _dimension_line(word, segments, aspect):
    x0,y0,x1,y1 = word.box
    starts, ends = (x0,y0*aspect), (x1,y1*aspect)
    axis = 0 if x1-x0 > (y1-y0)*aspect else 1
    cross = 1-axis
    along = (starts[axis]+ends[axis])/2
    low, high = starts[cross], ends[cross]
    scale = max(.001, high-low)
    for a,b in segments:
        length = abs(a[axis]-b[axis])
        if abs(a[cross]-b[cross]) > .0007 or length < .006:
            continue
        if not low-1.2*scale <= a[cross] <= high+1.2*scale:
            continue
        start,end = sorted((a[axis],b[axis]))
        text_size = ends[axis]-starts[axis]
        internal = start <= along <= end and text_size <= 1.55*length+.002
        external = (along < start or along > end) and min(abs(along-start),abs(along-end)) <= .06 and any(
            min(p[axis],q[axis]) <= along <= max(p[axis],q[axis])
            and max(abs(p[cross]-a[cross]),abs(q[cross]-a[cross])) < .0007
            for p,q in segments
        )
        if not internal and not external:
            continue
        if not all(any(
            _point_segment(point,p,q,.0027) and abs(p[cross]-q[cross]) >= .002
            and abs(p[axis]-q[axis]) <= .001
            for p,q in segments
        ) for point in (a,b)):
            continue
        if internal or all(any(
            min(math.dist(point,p),math.dist(point,q)) <= .0008
            and .001 < abs(p[cross]-q[cross]) < .008
            and .002 < abs(p[axis]-q[axis]) < .016
            for p,q in segments
        ) for point in (a,b)):
            return True
    return False


def elevation_guides(segments):
    rising = defaultdict(list)
    falling = []
    for start, end in segments:
        a,b = sorted((start,end))
        dx,dy = b[0]-a[0], b[1]-a[1]
        if not (.0016 <= dx <= .014 and .72 <= abs(dy)/dx <= 1.35):
            continue
        if dy < 0:
            rising[(round(a[0],4),round(a[1],4))].append(b)
        else:
            falling.append((a,b))
    guides = set()
    for a,b in falling:
        for c in rising.get((round(b[0],4),round(b[1],4)), ()):
            if abs(a[1]-c[1]) > .00035 or abs((b[0]-a[0])-(c[0]-b[0])) > .0006:
                continue
            if any(abs(d[1]-e[1]) < .00025 and _point_segment(a,d,e,.00045)
                   and _point_segment(c,d,e,.00045) for d,e in segments):
                guides.add((a[0], a[1], c[0]))
    return sorted(guides)


def read_values(words, *, segments=(), aspect=1, guides=(), graphical=True):
    datum = _datum(words)
    values = []
    for word in words:
        text = clean(word.text)
        if word.score < .76 or not text:
            continue
        count = len(values)
        for match in STATED.finditer(text):
            suffix = text[match.end():]
            if re.match(r"[A-Za-zμµ]", suffix) or suffix.lower().startswith(("以上", "以下", "左右", "~", "～", "至", "%", "‰", "kv", "^", "2", "3", "pa", "kn")):
                continue
            name = match.group("name").strip("：:，,。")
            role = match.group("role")
            category = "标高" if role == "标高" else "尺寸"
            explicit_datum = DATUM.search(name)
            reference = explicit_datum.group() if explicit_datum else datum
            unit = UNITS.get((match.group("unit") or "").lower(), "")
            values.append(measurement(category, word.evidence(), match.group("value"), unit,
                                      name + role, reference if category == "标高" else ""))
        if len(values) > count:
            continue
        level = LEVEL.fullmatch(text)
        supported_level = level and level.group("label") in {"女儿墙", "屋面标高", "地坪标高", "地面标高"}
        if level and (supported_level or any(
            -.002 <= y-word.box[3]*aspect <= .007
            and word.box[0] < right+.002
            and word.box[2] >= left-.012
            for left,y,right in guides
        )):
            unit = UNITS.get((level.group("unit") or "").lower(), "")
            values.append(measurement("标高", word.evidence(), level.group("value"), unit,
                                      (level.group("label") or "") + "标高", datum, method="标高符号"))
            continue
        numeric = DIMENSION.fullmatch(text)
        if not numeric:
            # A label can include the clearance note after its leading measured length.
            numeric = DIMENSION.match(text)
            if not numeric or not numeric.group("unit") or text[numeric.end():numeric.end()+1] not in {",", "，", "(", "（"}:
                continue
        value = numeric.group("value")
        unit = UNITS.get((numeric.group("unit") or "").lower(), "")
        if RELATIVE.fullmatch(value):
            values.append(measurement("标高", word.evidence(), value, unit,
                                      datum="相对", method="零标高符号"))
        elif graphical and not re.fullmatch(r"0\d+", value) and segments and .055 < word.x < .95 and .047 < word.y < .875 and _dimension_line(word, segments, aspect):
            values.append(measurement("尺寸", word.evidence(), value, unit, method="尺寸线"))
    return values


def page_geometry(path):
    """PDF paths in square coordinates, measured in page widths."""
    import pdfplumber
    from pdfminer.pdfexceptions import PDFException

    if path is None:
        return 1, [], []
    try:
        with pdfplumber.open(path) as doc:
            page = doc.pages[0]
            left, top, _, _ = page.bbox
            width = page.width
            aspect = page.height/width

            def point(x, y):
                return ((x-left)/width, (y-top)/width)

            segments, rectangles = [], []
            for obj in page.lines + page.rects + page.curves:
                pts = [point(*step[1]) for step in obj.get("path", ()) if step[0] in ("m", "l")]
                path_ops = [step[0] for step in obj.get("path", ())]
                x0, y0 = point(obj["x0"], obj["top"])
                x1, y1 = point(obj["x1"], obj["bottom"])
                w, h = x1-x0, y1-y0
                if (len(pts) in (4, 5) and all(op in {"m", "l", "h"} for op in path_ops)
                        and all(min(abs(x-x0), abs(x-x1), abs(y-y0), abs(y-y1)) < .0008 for x,y in pts)):
                    if min(w,h) > .006 and max(w,h) < .42 and all(
                        min(abs(x-x0),abs(x-x1)) < .0008 and min(abs(y-y0),abs(y-y1)) < .0008 for x,y in pts
                    ):
                        rectangles.append((x0,y0,x1,y1))
                    if min(w,h) < .002 and max(w,h) > .004:
                        # Filled narrow vector polygons encode some cable strokes.
                        middle = ((x0,(y0+y1)/2),(x1,(y0+y1)/2)) if w > h else (((x0+x1)/2,y0),((x0+x1)/2,y1))
                        segments.append(middle)
                        continue
                previous = start = None
                for op, *points in obj.get("path", ()):
                    if op == "m":
                        previous = start = point(*points[0])
                        continue
                    following = start if op == "h" else point(*points[-1]) if points else None
                    if op in ("l","h") and following and previous and math.dist(previous, following) >= .0015:
                        segments.append((previous, following))
                    previous = following
            return aspect, segments, list(dict.fromkeys(rectangles))
    except (PDFException, OSError, TypeError, ValueError, IndexError):
        return 1, [], []


def _bind_assets(words, rectangles, aspect, diagram):
    claims = defaultdict(dict)
    for word in words:
        token = re.split(r"[（(]", clean(word.text).upper(), maxsplit=1)[0]
        if not TAG.fullmatch(token) or word.score < .86:
            continue
        x, y = word.x, word.y*aspect
        scores = []
        for box in rectangles:
            a,b,c,d = box
            if diagram == "detail" and c-a > .075 and d-b > .07 and a+.005 < x < c-.005 and -.002 < b-y < .016:
                scores.append((b-y, box))
            if diagram == "overview" and .014 < c-a < .072 and .009 < d-b < .037 and abs(y-(b+d)/2) < .007 and -.002 < a-word.box[2] < .015:
                scores.append((abs(y-(b+d)/2)+a-word.box[2], box))
        if scores:
            _, box = min(scores)
            claims[box][token] = word
    assets = {}
    for box, tokens in claims.items():
        if len(tokens) == 1:
            token, word = next(iter(tokens.items()))
            assets[box] = (token, word)
    counts = defaultdict(int)
    for token, _ in assets.values():
        counts[token] += 1
    return {box: (token, word) for box,(token,word) in assets.items() if counts[token] == 1}


def _inside(point, box, tolerance=0):
    x,y = point
    a,b,c,d = box
    return a-tolerance < x < c+tolerance and b-tolerance < y < d+tolerance


def _outside_intervals(low, high, cuts):
    for start, end in sorted(cuts):
        if start >= high:
            break
        if end <= low:
            continue
        if low < start:
            yield low, start
        low = max(low, end)
    if low < high:
        yield low, high


def _wires_between(segments, boxes):
    tolerance = .00165
    links = []
    for a,b in segments:
        length = math.dist(a,b)
        if not .004 < length < .55 or min(abs(a[0]-b[0]),abs(a[1]-b[1])) > .0012:
            continue
        if min(a[0],b[0]) < .055 or max(a[0],b[0]) > .95:
            continue
        axis = 0 if abs(a[0]-b[0]) > abs(a[1]-b[1]) else 1
        cross = 1-axis
        lane = (a[cross]+b[cross])/2
        cuts = [(box[axis], box[axis+2]) for box in boxes
                if box[cross]-tolerance <= lane <= box[cross+2]+tolerance]
        for start, end in _outside_intervals(*sorted((a[axis],b[axis])), cuts):
            if end-start >= .003:
                links.append(((start,lane),(end,lane)) if axis == 0 else ((lane,start),(lane,end)))
    # Split only where an existing endpoint terminates on another stroke.
    # Interior/interior crossings deliberately do not create junctions.
    endpoints = [point for ends in links for point in ends]
    split = []
    for a,b in links:
        axis = 0 if abs(a[0]-b[0]) > abs(a[1]-b[1]) else 1
        cuts = {a[axis], b[axis]}
        low,high = sorted(cuts)
        for point in endpoints:
            if low+tolerance < point[axis] < high-tolerance and _point_segment(point,a,b,tolerance):
                cuts.add(point[axis])
        cross = 1-axis
        lane = (a[cross]+b[cross])/2
        ordered = sorted(cuts)
        split.extend(((start,lane),(end,lane)) if axis == 0 else ((lane,start),(lane,end))
                     for start,end in zip(ordered,ordered[1:]) if end-start > tolerance)
    links = split
    adjacent = defaultdict(set)
    cells = defaultdict(list)
    for index, ends in enumerate(links):
        for x, y in ends:
            i, j = math.floor(x/tolerance), math.floor(y/tolerance)
            for di in (-1,0,1):
                for dj in (-1,0,1):
                    for prior, point in cells[i+di,j+dj]:
                        if prior != index and math.dist(point,(x,y)) <= tolerance:
                            adjacent[prior].add(index)
                            adjacent[index].add(prior)
            cells[i,j].append((index,(x,y)))
    visited = set()
    for root in range(len(links)):
        pending = [root]
        component = set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            pending.extend(adjacent[node] - visited)
        if not component:
            continue
        contacts = {box: {index for index in component if any(_inside(point,box,tolerance) for point in links[index])}
                    for box in boxes}
        contacts = {box: nodes for box,nodes in contacts.items() if nodes}
        for first,second in combinations(sorted(contacts), 2):
            # Prove this particular route; unrelated branches do not add a
            # third endpoint to its evidence and no other enclosure is crossed.
            forbidden = set().union(*(nodes for box,nodes in contacts.items() if box not in {first,second}))
            starts, goals = contacts[first]-forbidden, contacts[second]-forbidden
            queue = [(node, [node]) for node in sorted(starts)]
            seen = set(starts)
            path = None
            while queue:
                node, route = queue.pop(0)
                if node in goals:
                    path = route
                    break
                for following in sorted(adjacent[node]-seen-forbidden):
                    seen.add(following)
                    queue.append((following, [*route, following]))
            if path:
                yield (first,second), [point for index in path for point in links[index]]


def optical_links(words, *, segments, rectangles, aspect, caption):
    caption = clean(caption)
    if not ("光纤" in caption or "光缆" in caption):
        return [], []
    diagram = "detail" if any(marker in caption for marker in ("熔接", "接续")) else (
        "overview" if any(marker in caption for marker in ("网络", "系统", "组网")) else "")
    if not diagram:
        return [], []
    assets = _bind_assets(words, rectangles, aspect, diagram)
    equipment_boxes = set(assets)
    # An unlabeled enclosure interrupts the path just like a recognized one.
    if assets:
        widths = [box[2]-box[0] for box in assets]
        heights = [box[3]-box[1] for box in assets]
        equipment_boxes.update(box for box in rectangles
            if .72*min(widths) <= box[2]-box[0] <= 1.3*max(widths)
            and .73*min(heights) <= box[3]-box[1] <= 1.3*max(heights))
    links = {}
    for (first, second), points in _wires_between(segments, equipment_boxes):
        if first not in assets or second not in assets:
            continue
        ends = sorted((assets[first], assets[second]), key=lambda item:item[0])
        a,b = (item[0] for item in ends)
        edge_box = {"x0": min(x for x,y in points), "y0": min(y for x,y in points)/aspect,
                    "x1": max(x for x,y in points), "y1": max(y for x,y in points)/aspect}
        edge_box["x1"] = min(1, max(edge_box["x1"], edge_box["x0"]+.001))
        edge_box["y1"] = min(1, max(edge_box["y1"], edge_box["y0"]+.001))
        evidence = {"text": f"{a} ↔ {b}", "source": Source.PDF_TEXT.value, "confidence": min(word.score for _,word in ends), "bbox": edge_box}
        links[(a,b)] = {"endpoints": [a,b], "domain": "光纤", "view": diagram, "evidence": evidence,
                        "endpoint_evidence": [word.evidence() for _,word in ends],
                        "relation": "continuous_vector_route",
                        "geometry": {"segments": [[points[index], points[index+1]] for index in range(0,len(points),2)],
                                     "junction_policy": "endpoint_termination_only",
                                     "intervening_equipment": []}}
    return list(links.values()), [{"tag": tag, "view": diagram, "evidence": word.evidence()} for tag,word in assets.values()]


_SIGNAL = re.compile(
    r"(?:(?:网络|通信|光纤|光缆|电缆|接线|线路|回路)(?:从|由)?|(?:从|由))"
    r"(?P<first>[A-Z0-9]+(?:-[A-Z0-9]+)*)"
    r"(?:接至|连至|连接至|至|到|↔|→)"
    r"(?P<second>[A-Z0-9]+(?:-[A-Z0-9]+)*)(?![A-Z0-9-])"
)


def stated_links(words):
    links = []
    for word in words:
        if word.score < .88:
            continue
        text = clean(word.text).upper()
        for match in _SIGNAL.finditer(text):
            endpoints = sorted(match.groups())
            if endpoints[0] == endpoints[1] or not all(TAG.fullmatch(tag) for tag in endpoints):
                continue
            prefix = match.group().split(match.group("first"), 1)[0]
            domain = "光纤" if any(kind in prefix for kind in ("光纤", "光缆")) else "电气" if "电缆" in prefix else ""
            if domain:
                links.append({"endpoints": endpoints, "domain": domain, "view": "statement",
                              "evidence": word.evidence()})
    return links


_ACCESS_START = re.compile(
    rf".*接入(?:至)?(?:利旧|新建|原有)?(?<![A-Z0-9-])(?P<first>{TAG.pattern})箱变低压侧开关[,，]$"
)
_ACCESS_CONTINUATION = re.compile(
    rf"^与(?P<via>{TAG.pattern})箱变连接后[,，]经(?:原有|利旧|新建)?(?P=via)箱变"
    rf"\d+(?:\.\d+)?KV出线电缆接入(?:原有|利旧|新建)?(?:一次舱)?(?P<target>{TAG.pattern})开关柜[.。]?$"
)
_UNCERTAIN_ACCESS = re.compile(r"不接入|未接入|不连接|未连接|预留|暂定|拟接|例如|举例|可接入|不适用")


def stated_access_paths(words):
    """Read an affirmative, positioned continuation as reachability, not adjacency.

    Both clauses remain independent original evidence. The intermediate device
    is recorded and never collapsed into a claim that the source is directly
    connected to the cabinet. No circuit number is invented from a cabinet tag.
    """
    words = unique_words([word for word in words if word.score >= .9 and word.source in {
        Source.CAD_TEXT.value, Source.PDF_TEXT.value, Source.USER.value}])
    paths = []
    for current in words:
        text = clean(current.text).upper()
        chain = _ACCESS_CONTINUATION.fullmatch(text)
        if not chain or _UNCERTAIN_ACCESS.search(text):
            continue
        prior = []
        for candidate in words:
            literal = clean(candidate.text).upper()
            start = _ACCESS_START.fullmatch(literal)
            if not start or _UNCERTAIN_ACCESS.search(literal) or candidate.source != current.source:
                continue
            height = max(current.box[3]-current.box[1],candidate.box[3]-candidate.box[1])
            if (not .8*height <= current.box[1]-candidate.box[1] <= 2.2*height
                    or abs(current.box[0]-candidate.box[0]) > max(.015,2*height)
                    or min(current.box[2],candidate.box[2])-max(current.box[0],candidate.box[0]) <
                       .5*min(current.box[2]-current.box[0],candidate.box[2]-candidate.box[0])):
                continue
            prior.append((candidate,start))
        if len(prior) != 1:
            continue
        beginning,start = prior[0]
        tags = [start["first"],chain["via"],chain["target"]]
        if len(set(tags)) != len(tags):
            continue
        paths.append({"source_tag":tags[0],"cabinet":tags[-1],"via":tags[1:-1],"path":tags,
                      "relation":"reachable_access", "view":"statement", "method":"明确肯定说明的相邻原文续行",
                      "evidence":current.evidence(), "endpoint_evidence":[beginning.evidence(),current.evidence()]})
    return paths


_POLE_DRAWING = re.compile(r"(?:视频监控|摄像[机头]|监控)立杆")
_POLE_HEIGHT = re.compile(rf"(?:立杆高度|高)(?P<value>{NUMBER})(?P<unit>{UNIT})$")
_POLE_LABEL = "视频监控室外立杆高度"


def pole_heights(values, materials, caption):
    rows = []
    if _POLE_DRAWING.search(caption):
        candidates = [item for item in values if item["category"] == "尺寸" and item["unit"] == "m"
                      and item["method"] == "尺寸线" and not item["label"]
                      and item["evidence"]["bbox"]["y1"] - item["evidence"]["bbox"]["y0"] >
                          2*(item["evidence"]["bbox"]["x1"] - item["evidence"]["bbox"]["x0"])]
        if len(candidates) == 1:
            candidates[0]["label"] = _POLE_LABEL
    if materials and "监控" in caption:
        for row in materials["rows"]:
            name, model = (row["fields"].get(key) for key in ("name", "model"))
            if not name or not model or clean(name["text"]) not in {"室外立杆", "监控立杆", "摄像机立杆"}:
                continue
            if match := _POLE_HEIGHT.search(clean(model["text"])):
                rows.append(measurement("尺寸", model, match.group("value"), UNITS[match.group("unit")],
                                        _POLE_LABEL, method="材料表规格"))
    return rows


_ASSET_SCOPE = re.compile(r"(?P<kind>箱变|变压器|开关柜|配电柜|逆变器)(?P<tags>[A-Z0-9-]+(?:[、,][A-Z0-9-]+)*)$")


def stated_assets(words, caption):
    if "接线" not in caption:
        return []
    assets = defaultdict(list)
    for word in words:
        text = clean(word.text).upper().rstrip("。.;")
        if not text.startswith(("本接线图", "本图适用", "本图表示")) and not re.match(r"^\d+[.、]本接线图", text):
            continue
        if match := _ASSET_SCOPE.search(text):
            for tag in re.split(r"[、,]", match.group("tags")):
                if TAG.fullmatch(tag):
                    assets[tag].append({"tag": tag, "type": match.group("kind"),
                                        "view": "equipment_scope", "evidence": word.evidence()})
    return [claims[0] for claims in assets.values() if len(claims) == 1]


def raster_segments(image):
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    scale = min(1., 2400/max(height,width))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        height, width = gray.shape
    edges = cv2.Canny(gray, 50, 160)
    lines = cv2.HoughLinesP(edges, 1, math.pi/180, threshold=max(12, round(width*.006)),
                            minLineLength=max(9, round(width*.004)), maxLineGap=max(2, round(width*.001)))
    if lines is None:
        return height/width, []
    rows = np.asarray(lines).reshape(-1, 4)[:6000]
    segments = [((int(x0)/width,int(y0)/width),(int(x1)/width,int(y1)/width)) for x0,y0,x1,y1 in rows]
    return height/width, segments


_CABLE_CODE = re.compile(r"\d?[A-Z]{2,8}[-]?\d{3,12}")
_CABLE_RANGE = re.compile(r"(?P<start>(?P<prefix>\d?[A-Z]{2,8}-?)(?P<number>\d{3,12}))[~至](?P<end>(?:\d?[A-Z]{2,8}-?)?\d{1,12})")


def cable_range(text):
    """Normalize written range endpoints without enumerating or correcting IDs."""
    literal=clean(text).upper().removeprefix("#")
    match=_CABLE_RANGE.fullmatch(literal)
    if not match:
        return None
    start,prefix,number,end=(match[key] for key in ("start","prefix","number","end"))
    if end.isdigit():
        if len(end)>len(number):
            return None
        final=number[:-len(end)]+end
    else:
        if not end.startswith(prefix) or not end[len(prefix):].isdigit() or len(end[len(prefix):])!=len(number):
            return None
        final=end[len(prefix):]
    if int(final)<int(number):
        return None
    return {"code":literal,"range_start":start,"range_end_literal":end,
            "range_key":[start,prefix+final]}

# A complete model including the conductor area. Partial model prefixes and bare
# sizes are not enough to identify a cable specification across drawings.
_CABLE_SPEC = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9.]+)+(?:[X×*]\d+(?:\.\d+)?){0,2}MM(?:2|²)"
_CABLE_NOUN = r"(?:光伏专用|直流|交流|逆变器出线|汇流箱出线)电缆"
_NAMED_CABLE = re.compile(rf"(?P<spec>{_CABLE_SPEC})(?P<name>{_CABLE_NOUN})", re.I)


def cable_specification(text):
    """Return only a complete cable model directly attached to its named use."""
    literal = clean(text).upper()
    if re.search(r"暂定|例如|举例|不采用|不得采用|不适用|或采用|替代|以厂家为准", literal):
        return []
    return [{"name": match["name"], "spec": match["spec"].replace("×", "X").replace("*", "X").replace("²", "2")}
            for match in _NAMED_CABLE.finditer(literal)]


def canonical_cable_spec(text):
    literal = clean(text).upper().replace("×", "X").replace("*", "X").replace("²", "2")
    return literal if re.fullmatch(_CABLE_SPEC, literal) else None


def table_cable_specification(text, serial, name, *, row_identity=None):
    """Read a whole saved table row without treating its serial as a model."""
    literal = clean(text).upper()
    prefix = clean(str(serial) + name).upper()
    if literal.startswith(prefix):
        offset=len(prefix)
    elif (row_identity and clean(row_identity).upper()==clean("序号"+str(serial)+name).upper()
          and literal.startswith(clean(name).upper())):
        # Newer visual records can retain the serial in the structured object
        # identity instead of repeating it in the row text. The caller still
        # requires the same physical CAD row and exact material noun.
        offset=len(clean(name))
    else:
        return None
    match = re.match(_CABLE_SPEC, literal[offset:], re.I)
    return canonical_cable_spec(match.group()) if match else None


def cad_cable_labels(words, caption, *, native_connections=()):
    """Read cable identities in a cabinet schedule or an electrical schematic."""
    headers = {name: [word for word in words if clean(word.text) == name]
               for name in ("电缆编号", "开关柜编号")}
    candidates = []
    for word in words:
        token = clean(word.text).removeprefix("#")
        if word.x > .08 and word.y < .86 and _CABLE_CODE.fullmatch(token):
            candidates.append((token,word))
    labels = defaultdict(list)
    if all(len(rows) == 1 for rows in headers.values()):
        cable, cabinet = (headers[name][0] for name in ("电缆编号", "开关柜编号"))
        if abs(cable.x-cabinet.x) > .025 or cable.y <= cabinet.y:
            return []
        cabinets = [word for word in words if abs(word.y-cabinet.y) <= .012 and word.x > cabinet.x+.04
                    and TAG.fullmatch(clean(word.text))]
        for token,word in candidates:
            if abs(word.y-cable.y) > .012:
                continue
            column = [cab for cab in cabinets if abs(word.x-cab.x) <= .025]
            if len(column) == 1:
                labels[token].append({"code": token, "view": "schedule", "cabinet": clean(column[0].text),
                                      "evidence": word.evidence()})
    elif any(title in caption for title in ("系统一次接线", "主接线图")):
        for token,word in candidates:
            labels[token].append({"code": token, "view": "schematic", "evidence": word.evidence()})
    records=[claims[0] for claims in labels.values() if len(claims) == 1]
    # Range-to-cabinet identity comes from source geometry, never the cable
    # code's prefix or a nearest-label guess.
    records.extend(record for record in native_connections if record.get("kind")=="cable_range")
    return records


def feeder_source_segments(values, *, excluded_handles=()):
    excluded=set(excluded_handles)
    for value in values:
        if not value.get('handle') or value['handle'] in excluded or value.get('layer')=='TK':
            continue
        kind=value.get('kind')
        if kind=='LINE':
            pairs=list(zip(value['points'],value['points'][1:]))
        elif kind=='LWPOLYLINE' and not value.get('closed') and value.get('linetype','').upper()=='DASHDOT2':
            pairs=list(zip(value['points'],value['points'][1:]))
        else:
            continue
        for index,(start,end) in enumerate(pairs):
            yield {'handle':value['handle'],'segment_index':index,'start':tuple(start),'end':tuple(end),
                   'linetype':value.get('linetype'),'layer':value.get('layer'),'color':value.get('color')}


class FeederWireGraph:
    """Exact endpoints and explicit endpoint-to-stroke terminations only.

    ``epsilon`` is an explicit CAD-coordinate rounding allowance. It is never
    increased to close an open switching symbol. No virtual device edge is added.
    """
    def __init__(self, segments, epsilon, *, additional_points=()):
        self.epsilon=epsilon;self.nodes=[];self.adj=defaultdict(list);self.edges=[];self.handles=defaultdict(set)
        grid=defaultdict(list);segments=list(segments);additional_points=list(additional_points)
        endpoints=[point for edge in segments for point in (edge['start'],edge['end'])]+additional_points
        buckets=[defaultdict(list),defaultdict(list)]
        for point in endpoints:
            for axis in (0,1):buckets[axis][round(point[axis]/epsilon)].append(point)
        def node(point):
            ix,iy=(math.floor(value/epsilon) for value in point)
            for di in (-1,0,1):
                for dj in (-1,0,1):
                    for number in grid[ix+di,iy+dj]:
                        if math.dist(point,self.nodes[number])<=epsilon:return number
            number=len(self.nodes);self.nodes.append(point);grid[ix,iy].append(number);return number
        for edge in segments:
            a,b=edge['start'],edge['end'];dx,dy=b[0]-a[0],b[1]-a[1];cuts=[a,b]
            length=dx*dx+dy*dy
            if length:
                for point in additional_points:
                    fraction=((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length
                    projected=(a[0]+fraction*dx,a[1]+fraction*dy)
                    if 0<fraction<1 and math.dist(point,projected)<=epsilon:
                        cuts.append(projected)
            if abs(dx)<epsilon or abs(dy)<epsilon:
                along=1 if abs(dx)<epsilon else 0;cross=1-along;lane=(a[cross]+b[cross])/2;low,high=sorted((a[along],b[along]))
                for off in (-1,0,1):
                    for point in buckets[cross][round(lane/epsilon)+off]:
                        if abs(point[cross]-lane)<=epsilon and low+epsilon<point[along]<high-epsilon:
                            projected=list(point);projected[cross]=lane;cuts.append(tuple(projected))
                cuts.sort(key=lambda point:point[along])
            else:
                cuts.sort(key=lambda point:math.dist(a,point))
            for start,end in zip(cuts,cuts[1:]):
                if math.dist(start,end)<=epsilon:continue
                i,j=node(start),node(end)
                if i==j:continue
                index=len(self.edges);self.edges.append({**edge,'start':start,'end':end})
                self.adj[i].append((j,index));self.adj[j].append((i,index));self.handles[edge['handle']].update((i,j))

    def route_between_handles(self, first, second):
        starts=self.handles.get(first,set());goals=self.handles.get(second,set())
        return self._route(starts,goals)

    def route_between_points(self, first, second):
        starts={number for number,point in enumerate(self.nodes) if math.dist(first,point)<=self.epsilon}
        goals={number for number,point in enumerate(self.nodes) if math.dist(second,point)<=self.epsilon}
        return self._route(starts,goals)

    def _route(self, starts, goals):
        from collections import deque
        queue=deque((number,[]) for number in sorted(starts));seen=set(starts)
        while queue:
            current,path=queue.popleft()
            if current in goals:return [self.edges[index] for index in path]
            for following,index in self.adj[current]:
                if following not in seen:seen.add(following);queue.append((following,[*path,index]))
        return None

