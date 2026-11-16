"""Located design parameter differences for human review of applicability."""
from collections import defaultdict
from itertools import combinations
import re

from backend.references import verified_sheet_number
from backend.title_identity import project_code
from .common import comparison_row
from .unit_declaration import _compact, valid_positioned_statement

_SCOPE = r"(?:(?:本工程|本项目)?(?:光伏支架|支架|建构筑物|建筑物|构筑物|基础|钢结构)|本工程结构|本结构|本工程)"
_LIFE = re.compile(rf"^(?P<scope>{_SCOPE})(?:的)?(?:结构)?设计(?:的)?(?:使用)?年限(?:为|是|:)(?P<value>[0-9]{{1,3}})年$")
_LEVEL = re.compile(
    rf"^(?:(?P<scope>{_SCOPE})(?:的)?)?(?:结构(?:的)?)?安全等级(?:为|是|:)(?P<value>[一二三123])级$"
)
_GENERIC = {"本工程结构", "本结构", "本工程"}


def _scope(raw):
    if raw in _GENERIC:
        return "工程结构"
    return re.sub(r"^(?:本工程|本项目)", "", raw)


def _parameters(text):
    raw = _compact(text)
    if re.search(r"是否|假设|暂按|[?]", raw):
        return
    previous_scope = None
    for clause in re.split(r"[，,；;。]", raw):
        clause = re.sub(r"^[0-9]{1,2}[.、]", "", clause)
        # A later clause can use the scope only if the immediately preceding
        # clause named it, as in "光伏支架…25年，结构的安全等级为三级".
        if match := _LIFE.fullmatch(clause):
            previous_scope = _scope(match["scope"])
            yield previous_scope, "设计使用年限", int(match["value"])
            continue
        if (match := _LEVEL.fullmatch(clause)) and (scope := match["scope"] or previous_scope):
            level = "一二三".find(match["value"]) + 1
            yield _scope(scope), "结构安全等级", level or int(match["value"])
        previous_scope = None


def design_parameter_candidates(units, facts):
    projects = {uid: project_code(verified_sheet_number(unit)) for uid, unit in units.items()}
    groups = defaultdict(lambda: defaultdict(dict))
    for fact in facts:
        uid = fact["unit_id"]
        if not (code := projects.get(uid)) or not valid_positioned_statement(fact):
            continue
        for scope, category, value in _parameters(fact["text"]):
            groups[code, scope, category][uid].setdefault(value, fact)
    rows = []
    for (_, scope, category), by_sheet in groups.items():
        # Competing readings on one sheet cannot supply a trustworthy value.
        readings = [next(iter(group.items())) for group in by_sheet.values() if len(group) == 1]
        pair = next(((a, b) for a, b in combinations(readings, 2) if a[0] != b[0]), None)
        if not pair:
            continue
        (a, left), (b, right) = pair
        unit = "年" if category == "设计使用年限" else "级"
        rows.append(comparison_row(left, right, category="设计参数",
            label=f"待确认候选 · {scope}{category}", outcome="unverified",
            left_value=f"{a} {unit}", right_value=f"{b} {unit}",
            basis=f"两处「{scope}」分别写 {a} {unit}、{b} {unit}；请核对是否适用于同一构件及应采用的参数。"))
    return rows
