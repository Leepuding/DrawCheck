from backend.review import ReviewService
from backend.store import BBox, Evidence, Source
from tests.backend.review.test_review import Client, drawing


def test_review_progress_counts_real_eligible_items_including_cache_hits(repo, drawing):
    version, unit, issue, task = drawing
    for n in range(2):
        repo.create_issue(version["id"], f"追加{n}",
            Evidence(unit["id"], Source.VISION, "100mm", BBox(.1,.2,.2,.3)),
            Evidence(unit["id"], Source.VISION, "200mm", BBox(.3,.2,.4,.3)),
            rule_result=issue["rule_result"])
    events = []
    service = ReviewService(repo, task_id=task["id"], primary_model={"provider":"custom","model":"one"},
        review_model={"provider":"custom","model":"two"}, client=Client(),
        progress_callback=lambda done,total,**kwargs: events.append((done,total)))
    result = service.run(version["id"])
    assert events == [(0,3),(1,3),(2,3),(3,3)]
    assert result.reviewed == 3 and result.cache_hits == 2 and result.model_calls == 1
