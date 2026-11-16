"""rules 测试：默认断网；提供对照 answers.json 的播种工具。"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from backend.store import BBox, Evidence, Repository, Source


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, address):  # noqa: ANN001
            raise RuntimeError(f"网络已禁用（测试守卫）: {address}")

        def connect_ex(self, address):  # noqa: ANN001
            raise RuntimeError(f"网络已禁用（测试守卫）: {address}")

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    real_transport = httpx.HTTPTransport

    class BlockedTransport(real_transport):
        def handle_request(self, request):  # noqa: ANN001
            raise RuntimeError(f"httpx 网络已禁用: {request.url}")

    monkeypatch.setattr(httpx, "HTTPTransport", BlockedTransport)
    yield


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(root))
    return root


@pytest.fixture
def repo(data_dir):
    with Repository(data_dir) as store:
        yield store


@pytest.fixture
def answers():
    root = Path(__file__).resolve().parents[3]
    return json.loads((root / "fixtures" / "synthetic" / "answers.json").read_text(encoding="utf-8"))


def seed_rule_a_version(repo: Repository, tmp_path, answers: dict, *, dim_tol=None, elv_tol=None):
    """播种对照 answers 规则 A / 同名 的最小版本。"""
    source = tmp_path / f"rules-{dim_tol}-{elv_tol}.pdf"
    source.write_bytes(b"%PDF-RULES\n")
    project = repo.create_project(f"规则A测试-{source.name}")
    version = repo.create_version(project["id"], "第一版")
    original = repo.import_original(version["id"], source)["record"]
    vid = version["id"]

    settings = {}
    if dim_tol is not None:
        settings["dimension_tolerance_mm"] = dim_tol
    if elv_tol is not None:
        settings["elevation_tolerance_m"] = elv_tol
    if settings:
        repo.save_settings(settings)

    catalog = answers["catalog"]
    listed_missing = list(catalog.get("listed_missing") or [])
    omits = set(catalog.get("catalog_sheet_omits") or [])

    listed = []
    for row in catalog["A"]:
        dn = row["drawing_number"]
        if dn in omits or dn == "S-000":
            continue
        listed.append(dn)
    listed.extend(listed_missing)

    units = {}
    units["S-000"] = repo.create_unit(
        vid, original["id"], kind="pdf_page", ordinal=1, name="图纸目录", drawing_number="S-000",
        metadata={"extract_status":"ok", "catalog_complete":True, "is_catalog_sheet": True, "catalog_listed_numbers": listed, "expected_number_prefix": "S"},
    )

    present_rows = [r for r in catalog["A"] if r["drawing_number"] != "S-000"]
    ordinal = 2
    for row in present_rows:
        dn = row["drawing_number"]
        meta = {"extract_status":"ok", "expected_number_prefix": "S"}
        if dn == "S-PREFIX":
            meta["title_drawing_number"] = "AB-001"
            meta["title_bbox"] = {
                "x0": 0.8471296296296297,
                "y0": 0.8907220351664796,
                "x1": 0.9815211640211641,
                "y1": 0.9738683127572016,
            }
            meta["check_prefix"] = True
        if dn == "S-EXTRA":
            # Synthetic actual extra sheet has an independently confirmed title.
            meta["manual_drawing_number_confirmed"] = True
            meta["title_bbox"] = {
                "x0": 0.8471296296296297,
                "y0": 0.8907220351664796,
                "x1": 0.9815211640211641,
                "y1": 0.9738683127572016,
            }
        if dn == "S-001":
            meta["title_drawing_number"] = "S-001"
            meta["check_prefix"] = True
        if row.get("out_of_scope"):
            meta["out_of_scope"] = True
        units[dn] = repo.create_unit(
            vid, original["id"], kind="pdf_page", ordinal=ordinal,
            name=row["name"], drawing_number=dn, metadata=meta,
        )
        ordinal += 1

    def add_side(unit_dn, side, case):
        if not side:
            return
        u = units[unit_dn]
        bb = side.get("bbox") or {"x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0}
        fact = repo.add_fact(
            vid,
            Evidence(
                u["id"],
                Source.VISION,
                side["text"],
                BBox(float(bb["x0"]), float(bb["y0"]), float(bb["x1"]), float(bb["y1"])),
            ),
            numeric_value=side.get("numeric_value"),
            unit=side.get("unit"),
        )
        current = repo.get("drawing_units", u["id"])
        meta = current["metadata"]
        meta.setdefault("fact_annotations", {})[fact["id"]] = {"category": case["category"], "object_id": case["id"], "comparison_key": case["id"], "identity_confirmed": True}
        repo._update("drawing_units", u["id"], {"metadata_json": json.dumps(meta)})

    for issue in answers["issues"]:
        if issue.get("revision"):
            continue
        cat = issue.get("category")
        if cat in {"尺寸", "标高", "同名不同对象"}:
            dn = (issue.get("side_a") or {}).get("drawing_number")
            if dn and dn in units:
                add_side(dn, issue.get("side_a"), issue)
                add_side(dn, issue.get("side_b"), issue)

    task = repo.create_task(vid)
    return {
        "project": project,
        "version": version,
        "original": original,
        "units": units,
        "task": task,
        "listed": listed,
    }


@pytest.fixture
def seeded_rule_a(repo, tmp_path, answers):
    return seed_rule_a_version(repo, tmp_path, answers)


@pytest.fixture
def seed_rule_a(repo, tmp_path, answers):
    def _call(**kwargs):
        return seed_rule_a_version(repo, tmp_path, answers, **kwargs)
    return _call
