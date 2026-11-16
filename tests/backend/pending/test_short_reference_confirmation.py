import json

import pytest

from backend.evidence_selection import selected_facts
from backend.linking import LinkingService
from backend.pending.current import current_incomplete, current_pending
from backend.pending.service import confirm_pending_item
from backend.references import AMBIGUOUS_REFERENCE_REASON, STRUCTURED_TARGET_CONFLICT_REASON
from backend.rules.rules_b import RulesBService
from backend.store import BBox, Evidence, Repository, Source, StoreError
from tests.backend.rules.test_reference_reliability import add, seed, unit


TARGET = "33-NZ16181S-T0103-04"
ANSWER = f"见图{TARGET}"
SHA = "a" * 64


@pytest.fixture
def positioned_short(tmp_path):
    with Repository(tmp_path / "data") as repo:
        version, original = seed(repo, tmp_path)
        source = unit(repo, version, original, "33-NZ16181S-T0102-03", 1, extract_image_sha256=SHA)
        unit(repo, version, original, TARGET, 2)
        fact = add(repo, version, source, "见图04", y=.2, category="详图索引断链",
                   target_drawing_number="04", target_detail_number="04")
        metadata = repo.get("drawing_units", source["id"])["metadata"]
        bbox = {key: fact[key] for key in ("x0", "y0", "x1", "y1")}
        proof = {"scope": "drawing_reference", "recovery_stage": "corroborated_short_reference",
                 "status": "unique_exact", "image_sha256": SHA, "recovered_text": "见图04",
                 "locations": [{"match_type": "short_reference_reread", "literal_occurrence_count": 1, "bbox": bbox}]}
        metadata["fact_annotations"][fact["id"]]["position_recovery"] = proof
        repo._update("drawing_units", source["id"], {"metadata_json": json.dumps(metadata)})
        question = repo.create_pending(version, Evidence(source["id"], Source.VISION, fact["text"], BBox(**bbox)),
                                       AMBIGUOUS_REFERENCE_REASON)
        add(repo, version, source, "基础详图见-04图", y=.7, category="详图索引断链")
        RulesBService(repo).run(version)
        incomplete = [{**row, "step": "link"} for row in LinkingService(repo).link_version(version, persist=False).incomplete]
        yield repo, version, source, fact, question, incomplete


def test_positioned_short_answer_updates_only_its_own_reference(positioned_short):
    repo, version, source, fact, question, before = positioned_short
    others = [row for row in current_pending(repo, version) if row["id"] != question["id"]]
    original_metadata = repo.get("drawing_units", source["id"])["metadata"]
    result = confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": ANSWER})
    assert result["model_calls_delta"] == 0
    assert result["pending_count"] == 1
    assert current_incomplete(repo, version, before) == [row for row in before if row["fact_id"] != fact["id"]]
    assert [row for row in current_pending(repo, version) if row["id"] != question["id"]] == others
    association, = repo.list_records("associations", version_id=version)
    assert association["details"]["reference"]["resolved_number"] == TARGET
    assert association["details"]["reference"]["fact_id"] == fact["id"]
    assert repo.get("facts", fact["id"]) == fact
    assert repo.get("drawing_units", source["id"])["metadata"] == original_metadata
    RulesBService(repo).run(version)
    result2 = confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": ANSWER})
    assert result["fact_id"] == result2["fact_id"]
    assert repo.list_records("associations", version_id=version) == [association]


@pytest.mark.parametrize("text", ["04", "见图04", "见-04图", "本图详图04", f"{ANSWER}或图{TARGET[:-2]}05",
                                   f"可能{ANSWER}", f"不{ANSWER}"])
def test_unresolved_words_do_not_close_original_question(positioned_short, text):
    repo, version, _, _, question, before = positioned_short
    with pytest.raises(StoreError, match="完整图号"):
        confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": text})
    assert repo.get("pending_confirmations", question["id"]) == question
    assert current_incomplete(repo, version, before) == before


def test_missing_target_and_unsure_remain_incomplete(positioned_short):
    repo, version, _, fact, question, before = positioned_short
    confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": TARGET})
    assert repo.list_records("associations", version_id=version)
    # A structurally valid suffix is not a complete identity.
    missing = confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": "T0103-04"})
    assert missing["pending_count"] == 1
    assert not repo.list_records("associations", version_id=version)
    gaps = [row for row in current_incomplete(repo, version, before) if row["fact_id"] == fact["id"]]
    assert len(gaps) == 1 and gaps[0]["reason"] != AMBIGUOUS_REFERENCE_REASON
    unsure = confirm_pending_item(repo, question["id"], {"status": "无法确定"})
    assert unsure["incomplete_pending_count"] == 2
    assert not any(item["id"] == fact["id"] for item in selected_facts(repo, version)[1])
    assert not repo.list_records("associations", version_id=version)
    assert not [row for row in current_incomplete(repo, version, before) if row["fact_id"] == fact["id"]]


def test_legacy_conflict_duplicate_retires_and_audit_survives(positioned_short):
    repo, version, source, fact, question, _ = positioned_short
    evidence = Evidence(source["id"], Source.USER, ANSWER,
                        BBox(*(fact[k] for k in ("x0", "y0", "x1", "y1"))))
    repo.confirm_pending(question["id"], status="已确认", confirmed_text=ANSWER)
    repo.add_fact(version, evidence)
    duplicate = repo.create_pending(version, evidence, STRUCTURED_TARGET_CONFLICT_REASON)
    assert duplicate["id"] not in {row["id"] for row in current_pending(repo, version)}
    assert repo.get("pending_confirmations", duplicate["id"]) == duplicate


def test_unverified_title_does_not_become_an_association(positioned_short):
    repo, version, _, fact, question, before = positioned_short
    target = next(u for u in repo.list_records("drawing_units", version_id=version) if u["drawing_number"] == TARGET)
    metadata = {**target["metadata"], "title_identity_uncertain": True}
    repo._update("drawing_units", target["id"], {"metadata_json": json.dumps(metadata)})
    confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": ANSWER})
    assert not repo.list_records("associations", version_id=version)
    gap, = [row for row in current_incomplete(repo, version, before) if row["fact_id"] == fact["id"]]
    assert gap["reason"] == "引用候选图纸身份尚未核实，需确认"


def test_changed_image_does_not_carry_old_answer_onto_identical_text_and_box(positioned_short):
    repo, version, source, fact, question, before = positioned_short
    confirm_pending_item(repo, question["id"], {"status": "已确认", "confirmed_text": ANSWER})
    metadata = repo.get("drawing_units", source["id"])["metadata"]
    metadata["extract_image_sha256"] = "b" * 64
    repo._update("drawing_units", source["id"], {"metadata_json": json.dumps(metadata)})
    replacement = add(repo, version, source, fact["text"], y=.2, category="详图索引断链",
                      target_drawing_number="04")
    items = selected_facts(repo, version)[1]
    assert not any(item["id"] == fact["id"] or item["source"] == Source.USER.value for item in items)
    assert replacement in [{k: item[k] for k in replacement} for item in items]
    assert repo.get("pending_confirmations", question["id"])["confirmed_text"] == ANSWER
    assert not any(row["fact_id"] == fact["id"] for row in current_incomplete(repo, version, before))
