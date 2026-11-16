"""DC-18: revision planning, conservative comparison and resumable project history."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re

from backend.importer.diagnostics import present_import_diagnostics
from backend.rules.rules_b import _canonical_code, _specific_identity
from backend.store import Repository, StoreError, ErrorCode
from backend.store.originals import sha256_file
from backend.store.paths import safe_path
from backend.title_identity import has_manual_number


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _normal_number(value):
    return re.sub(r"\s+", "", value or "").upper()


def _filename(value):
    stem = Path(value).stem
    stem = re.sub(r"[\s_\-（(]*(?:修订版|新版|改版)[）)]*$", "", stem)
    stem = re.sub(r"(?:[\s_\-（(]*(?:修订版?|版本|改版|新版|第|REV(?:ISION)?|VER(?:SION)?|V)[\s_.\-]*[A-Z0-9一二三四五六七八九十.]+版?[）)]*)$", "", stem, flags=re.I)
    stem = re.sub(r"[\s_\-（(]+[A-Z][版）)]*$", "", stem, flags=re.I)
    return re.sub(r"\s+", "", stem).casefold()


def _remap(value, mapping):
    if isinstance(value, dict):
        return {mapping.get(key, key): _remap(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, mapping) for item in value]
    return mapping.get(value, value) if isinstance(value, str) else value


class RevisionService:
    def __init__(self, store: Repository):
        self.store = store

    def _run(self, version_id):
        row = self.store._connection.execute("SELECT * FROM revision_runs WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "plan": json.loads(row["plan_json"]), "summary": json.loads(row["summary_json"])}

    def _manifest_path(self, version_id):
        version = self.store.get("drawing_versions", version_id)
        return safe_path(self.store.root, f"projects/{version['project_id']}/cache/import-{version_id}.json")

    def _manifest(self, version_id):
        path = self._manifest_path(version_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if (not isinstance(value, dict) or not isinstance(value.get("unreadable", []), list)
                    or any(not isinstance(row, dict) for row in value.get("unreadable", []))):
                raise ValueError("invalid import manifest")
            return value
        except (OSError, ValueError):
            # Keep projects accessible, without silently treating lost diagnostics as a clean import.
            return {"unreadable": [{"name": "导入记录", "code": "IMPORT_MANIFEST_UNREADABLE", "reason": "导入诊断记录读不了，部分导入范围无法确认"}]}

    def _inherit_import_diagnostics(self, version_id, previous_id, incoming):
        current = self._manifest(version_id)
        previous = self._manifest(previous_id)
        replaced = {_filename(self.store.get("original_files", unit["original_id"])["name"]) for unit in incoming}
        inherited = [row for row in previous.get("unreadable", []) if _filename(row.get("name", "")) not in replaced]
        merged, seen = [], set()
        for row in inherited + current.get("unreadable", []):
            key = _json(row)
            if key not in seen:
                seen.add(key)
                merged.append(row)
        current["unreadable"] = merged
        path = self._manifest_path(version_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".revision.tmp")
        temporary.write_text(_json(current), encoding="utf-8")
        temporary.replace(path)

    def create_revision(self, previous_version_id: str, name: str = "修订版") -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise StoreError(ErrorCode.INVALID_DATA, "请填写有效修订版名称")
        with self.store._transaction():
            previous = self.store._writable_version(previous_version_id)
            if (self._run(previous_version_id) or {}).get("status") == "draft":
                raise StoreError(ErrorCode.INVALID_DATA, "请先完成当前修订版导入")
            # Reusing a draft makes failed imports recoverable without freezing the source.
            draft = self.store._connection.execute(
                "SELECT version_id FROM revision_runs WHERE previous_version_id=? AND status='draft'", (previous_version_id,)).fetchone()
            if draft:
                version = self.store.get("drawing_versions", draft[0])
            else:
                sequence = self.store._one("SELECT MAX(sequence)+1 AS n FROM drawing_versions WHERE project_id=?", (previous["project_id"],))["n"]
                version = self.store._insert("drawing_versions", {"project_id": previous["project_id"], "sequence": sequence, "name": name.strip()})
                self.store._connection.execute("INSERT INTO revision_runs(version_id,previous_version_id) VALUES(?,?)", (version["id"], previous_version_id))
        return {"project_id": version["project_id"], "version_id": version["id"], "previous_version_id": previous_version_id, "status": "draft"}

    def _digest(self, unit):
        meta = unit.get("metadata") or {}
        relative = meta.get("review_relative_path") or meta.get("preview_relative_path")
        if relative:
            try:
                return ("image", sha256_file(safe_path(self.store.root, relative)))
            except (OSError, StoreError):
                pass
        original = self.store.get("original_files", unit["original_id"])
        return ("original", original["sha256"], unit["kind"], unit["ordinal"])

    def _number(self, unit):
        meta = unit.get("metadata") or {}
        number = _normal_number(unit.get("drawing_number"))
        # PDFParser's page ordinal is a display placeholder, never a recognized sheet ID.
        if (unit["kind"] == "pdf_page" and number == str(unit["ordinal"])
                and not has_manual_number(unit) and meta.get("extract_status") != "ok"):
            return ""
        return number

    def _match(self, incoming, old_units):
        originals = {r["id"]: r for r in self.store.list_records("original_files")}
        by_number, by_file = defaultdict(list), defaultdict(list)
        for unit in old_units:
            if self._number(unit):
                by_number[self._number(unit)].append(unit)
            by_file[(_filename(originals[unit["original_id"]]["name"]), unit["kind"], unit["ordinal"])].append(unit)
        pairs, claimed = [], set()
        for unit in incoming:
            number = self._number(unit)
            candidates = by_number.get(number, []) if number else by_file.get(
                (_filename(originals[unit["original_id"]]["name"]), unit["kind"], unit["ordinal"]), [])
            if len(candidates) > 1 or any(old["id"] in claimed for old in candidates):
                raise StoreError(ErrorCode.INVALID_DATA, "图号重复，无法确定修订对应关系，请先修正目录")
            old = candidates[0] if candidates else None
            if old:
                claimed.add(old["id"])
            pairs.append((old, unit))
        return pairs

    def _copy_recognition(self, previous_id, version_id, old, unit, mapping):
        id_map = dict(mapping)
        # Parser text may already be present on an identical newly imported sheet.
        for fact in self.store.list_records("facts", version_id=version_id):
            if fact["unit_id"] == unit["id"]:
                self.store._connection.execute("DELETE FROM facts WHERE id=?", (fact["id"],))
        for fact in self.store.list_records("facts", version_id=previous_id):
            if fact["unit_id"] == old["id"]:
                new = self.store._insert("facts", {key: value for key, value in fact.items() if key != "id"} | {"version_id": version_id, "unit_id": unit["id"]})
                id_map[fact["id"]] = new["id"]
        for pending in self.store.list_records("pending_confirmations", version_id=previous_id):
            if pending["unit_id"] == old["id"]:
                self.store._insert("pending_confirmations", {key: value for key, value in pending.items() if key != "id"} | {"version_id": version_id, "unit_id": unit["id"]})
        metadata = _remap(deepcopy(old["metadata"]), id_map)
        self.store._update("drawing_units", unit["id"], {"name": old["name"], "drawing_number": old["drawing_number"], "metadata_json": _json(metadata)})

    def _affected(self, previous_id, mapping, changed):
        affected = set(changed)
        changed_old = {old for old, new in mapping.items() if new in affected}
        for association in self.store.list_records("associations", version_id=previous_id):
            pair = {association["left_unit_id"], association["right_unit_id"]}
            if pair & changed_old:
                affected.update(mapping[item] for item in pair if item in mapping)
        return sorted(affected)

    def prepare(self, version_id: str) -> dict | None:
        self.store.get("drawing_versions", version_id)
        run = self._run(version_id)
        if run is None:
            return None
        if run["status"] != "draft":
            return {**run["plan"], "status": run["status"]}
        incoming = self.store.list_records("drawing_units", version_id=version_id)
        if not incoming:
            raise StoreError(ErrorCode.INVALID_DATA, "尚未导入可审查图纸，原项目仍可继续工作")
        previous_id = run["previous_version_id"]
        self.store._writable_version(previous_id)
        old_units = self.store.list_records("drawing_units", version_id=previous_id)
        pairs = self._match(incoming, old_units)
        mapping = {old["id"]: unit["id"] for old, unit in pairs if old}
        unchanged = [(old, unit) for old, unit in pairs if old and self._digest(old) == self._digest(unit)]
        reused = [unit["id"] for _, unit in unchanged]
        changed = [unit["id"] for unit in incoming if unit["id"] not in reused]
        with self.store._transaction():
            # Reserve read-only source references before review_session hashes its input set.
            # Sheet units are still deferred until post-recognition matching below.
            hashes = {row["sha256"] for row in self.store.list_records("original_files", version_id=version_id)}
            for original in self.store.list_records("original_files", version_id=previous_id):
                if original["sha256"] not in hashes:
                    self.store.original_path(original["id"])
                    self.store._insert("original_files", {key: value for key, value in original.items() if key not in {"id", "created_at"}} | {"version_id": version_id})
                    hashes.add(original["sha256"])
            for old, unit in unchanged:
                self._copy_recognition(previous_id, version_id, old, unit, mapping)
            plan = {"version_id": version_id, "previous_version_id": previous_id, "old_to_new": mapping,
                    "incoming_unit_ids": [unit["id"] for unit in incoming], "changed_unit_ids": changed,
                    "reused_unit_ids": reused, "affected_unit_ids": self._affected(previous_id, mapping, changed),
                    "local_rules_scope": "all", "match_complete": False}
            self._inherit_import_diagnostics(version_id, previous_id, incoming)
            self.store._connection.execute("UPDATE revision_runs SET status='prepared',plan_json=? WHERE version_id=?", (_json(plan), version_id))
        return {**plan, "status": "prepared"}

    def rematch(self, version_id: str) -> dict | None:
        """Run after incoming recognition/catalog sync and before linking/rules.

        Only now are unknown incoming sheet identities available. Missing old sheets are
        carried forward at this boundary, so a renamed replacement cannot leave duplicates.
        """
        run = self._run(version_id)
        if run is None:
            return None
        plan = run["plan"]
        if run["status"] == "draft":
            plan = {key: value for key, value in self.prepare(version_id).items() if key != "status"}
        if plan.get("match_complete"):
            return {**plan, "status": run["status"]}
        previous_id = run["previous_version_id"]
        incoming = [self.store.get("drawing_units", uid) for uid in plan["incoming_unit_ids"]]
        old_units = self.store.list_records("drawing_units", version_id=previous_id)
        pairs = self._match(incoming, old_units)
        mapping = {old["id"]: unit["id"] for old, unit in pairs if old}
        reused = list(plan["reused_unit_ids"])
        with self.store._transaction():
            for old in old_units:
                if old["id"] in mapping:
                    continue
                original = self.store.get("original_files", old["original_id"])
                self.store.original_path(original["id"])
                current = next((r for r in self.store.list_records("original_files", version_id=version_id) if r["sha256"] == original["sha256"]), None)
                if current is None:
                    current = self.store._insert("original_files", {key: value for key, value in original.items() if key not in {"id", "created_at"}} | {"version_id": version_id})
                unit = self.store.create_unit(version_id, current["id"], kind=old["kind"], ordinal=old["ordinal"], name=old["name"], drawing_number=old["drawing_number"], metadata=old["metadata"])
                mapping[old["id"]] = unit["id"]
                reused.append(unit["id"])
                self._copy_recognition(previous_id, version_id, old, unit, mapping)
            plan.update(old_to_new=mapping, reused_unit_ids=reused, match_complete=True,
                        affected_unit_ids=self._affected(previous_id, mapping, plan["changed_unit_ids"]))
            self.store._connection.execute("UPDATE revision_runs SET plan_json=? WHERE version_id=?", (_json(plan), version_id))
        return {**plan, "status": "prepared"}

    def _key(self, issue, reverse_mapping=None):
        rule = issue.get("rule_result") or {}
        category = str(rule.get("category") or issue["title"])
        pair = []
        for prefix in ("left_", "right_"):
            unit = issue[prefix + "unit_id"]
            record = self.store.get("drawing_units", unit)
            annotations = record.get("metadata", {}).get("fact_annotations", {})
            identities = set()
            for fact in self.store.list_records("facts", version_id=issue["version_id"]):
                if fact["unit_id"] != unit or any(abs(float(fact[axis]) - float(issue[prefix + axis])) > .000001 for axis in ("x0", "y0", "x1", "y1")):
                    continue
                annotation = annotations.get(fact["id"], {})
                object_id, comparison_key = annotation.get("object_id"), annotation.get("comparison_key")
                if annotation.get("identity_confirmed") and any(_specific_identity(value) for value in (object_id, comparison_key)):
                    identities.add((str(object_id or ""), str(comparison_key or "")))
            if reverse_mapping:
                unit = reverse_mapping.get(unit, unit)
            if len(identities) == 1:
                location = ("object", *next(iter(identities)))
            else:
                # A position can be reused by a different object in a revision.
                text = issue[prefix + "text"]
                number = _canonical_code(text) if category == "编号写法" else None
                reading = ("number", *number) if number else ("text", text)
                box = tuple(round(float(issue[prefix + axis]), 2) for axis in ("x0", "y0", "x1", "y1"))
                location = ("box", box, reading)
            pair.append((unit, location))
        defaults = tuple(sorted({(row["volume"], row["category"], row["unit"])
                                 for row in (issue.get("rule_result") or {}).get("unit_declarations") or []}))
        return (category, tuple(sorted(pair)), defaults)

    def invalidate_comparison(self, version_id: str, task_id: str) -> dict | None:
        """New tasks get a fresh comparison; resuming the same task preserves its progress."""
        run = self._run(version_id)
        if run is None:
            return None
        task = self.store.get("review_tasks", task_id)
        if task["version_id"] != version_id:
            raise StoreError(ErrorCode.INVALID_DATA, "任务与修订版本不匹配")
        if run["summary"].get("task_id") == task_id:
            return run["summary"]
        with self.store._transaction():
            # These are derived comparison rows; the original previous-version evidence stays intact.
            self.store._connection.execute("DELETE FROM issues WHERE version_id=? AND comparison_tag='已解决'", (version_id,))
            summary = {"task_id": task_id, "comparison_complete": False,
                       "counts": {"仍存在": 0, "已解决": 0, "新出现": 0}}
            self.store._connection.execute("UPDATE revision_runs SET status=?,summary_json=? WHERE version_id=?", (
                "draft" if run["status"] == "draft" else "prepared", _json(summary), version_id))
        return summary

    def _history_snapshot(self, version_id, task_id, comparison=None):
        task = self.store.get("review_tasks", task_id)
        if task["status"] != "已完成":
            return
        checkpoints = deepcopy(task.get("checkpoints") or {})
        meta = checkpoints.setdefault("_meta", {})
        if meta.get("history_snapshot"):
            return
        snapshot = {"issue_count": sum(issue.get("comparison_tag") != "已解决" for issue in self.store.list_records("issues", version_id=version_id)),
                    "is_revision": self._run(version_id) is not None, "created_at": task["created_at"],
                    "tolerance_snapshot": task.get("tolerance_snapshot") or {}, "status": task["status"]}
        if comparison:
            snapshot.update(comparison_counts=comparison.get("counts"), comparison_complete=comparison.get("comparison_complete"))
        meta["history_snapshot"] = snapshot
        self.store._update("review_tasks", task_id, {"checkpoints_json": _json(checkpoints)})

    def finalize(self, version_id: str, task_id: str) -> dict | None:
        task = self.store.get("review_tasks", task_id)
        if task["version_id"] != version_id:
            raise StoreError(ErrorCode.INVALID_DATA, "任务与修订版本不匹配")
        run = self._run(version_id)
        if run is None:
            self._history_snapshot(version_id, task_id)
            return None
        if run["status"] == "draft":
            raise StoreError(ErrorCode.INVALID_DATA, "请先完成修订导入")
        if run["status"] == "completed" and run["summary"].get("task_id") == task_id:
            self._history_snapshot(version_id, task_id, self.comparison(version_id))
            return self.comparison(version_id)
        if run["summary"].get("task_id") != task_id:
            self.invalidate_comparison(version_id, task_id)
            run = self._run(version_id)
        checkpoints = task.get("checkpoints") or {}
        from backend.pending.current import current_pending
        required = ("extract", "link", "rules_a", "rules_b")
        complete = (run["plan"].get("match_complete") and task["status"] == "已完成" and not task.get("incomplete")
                    and not self._manifest(version_id).get("unreadable")
                    and all((checkpoints.get(step) or {}).get("state") == "done" for step in required)
                    and not any(p["status"] != "已确认" for p in current_pending(self.store, version_id)))
        previous = [issue for issue in self.store.list_records("issues", version_id=run["previous_version_id"]) if issue.get("comparison_tag") != "已解决"]
        mapping = run["plan"]["old_to_new"]
        reverse = {new: old for old, new in mapping.items()}
        old_by_key = defaultdict(list)
        for issue in previous:
            old_by_key[self._key(issue)].append(issue)
        with self.store._transaction():
            seen = set()
            for issue in self.store.list_records("issues", version_id=version_id):
                if issue.get("comparison_tag") == "已解决":
                    continue
                candidates = old_by_key.get(self._key(issue, reverse), [])
                old = next((item for item in candidates if item["id"] not in seen), None)
                values = {"comparison_tag": "新出现", "previous_issue_id": None}
                if old:
                    seen.add(old["id"])
                    values.update(comparison_tag="仍存在", previous_issue_id=old["id"])
                    # Human changes made in this version, including condition-change reminders,
                    # must survive later tasks; only an unreviewed row inherits the old decision.
                    if (issue["review_status"] == "疑似" and not issue.get("previous_issue_id")
                            and not (issue.get("rule_result") or {}).get("evidence_changed_after_confirmation")):
                        values.update(review_status=old["review_status"], review_note=old["review_note"])
                self.store._update("issues", issue["id"], values)
            if complete:
                for old in previous:
                    if old["id"] in seen:
                        continue
                    # Resolved rows retain old evidence and point to corresponding current sheets.
                    if any(old[prefix + "unit_id"] not in mapping for prefix in ("left_", "right_")):
                        continue
                    row = {key: value for key, value in old.items() if key not in {"id", "rule_result", "primary_opinion", "review_opinion"}}
                    row.update(version_id=version_id, comparison_tag="已解决", previous_issue_id=old["id"],
                               left_unit_id=mapping[old["left_unit_id"]], right_unit_id=mapping[old["right_unit_id"]],
                               rule_result_json=_json(old["rule_result"]), primary_opinion_json=_json(old["primary_opinion"]), review_opinion_json=_json(old["review_opinion"]))
                    self.store._insert("issues", row)
            counts = {tag: sum(row.get("comparison_tag") == tag for row in self.store.list_records("issues", version_id=version_id)) for tag in ("仍存在", "已解决", "新出现")}
            summary = {"task_id": task_id, "counts": counts, "comparison_complete": complete,
                       "unresolved_previous_count": 0 if complete else len(previous) - len(seen)}
            self.store._connection.execute("UPDATE revision_runs SET status=?,summary_json=? WHERE version_id=?", ("completed" if complete else "prepared", _json(summary), version_id))
        comparison = self.comparison(version_id)
        self._history_snapshot(version_id, task_id, comparison)
        return comparison

    def comparison(self, version_id):
        self.store.get("drawing_versions", version_id)
        run = self._run(version_id)
        if run is None:
            return {"is_revision": False}
        counts = run["summary"].get("counts") or {"仍存在": 0, "已解决": 0, "新出现": 0}
        return {"is_revision": True, "status": run["status"], **run["plan"], **run["summary"], "counts": counts,
                "summary": f"对比上一版：仍存在 {counts['仍存在']} · 已解决 {counts['已解决']} · 新出现 {counts['新出现']}"}

    def history(self):
        items, projects = [], self.store.list_records("projects")
        for project in projects:
            versions = self.store.list_records("drawing_versions", project_id=project["id"])
            active = [v for v in versions if (self._run(v["id"]) or {}).get("status") != "draft"]
            latest_id = active[-1]["id"] if active else None
            for version in versions:
                run = self._run(version["id"])
                base = {"project_id": project["id"], "project_name": project["name"], "version_id": version["id"],
                        "version_name": version["name"], "sequence": version["sequence"], "latest_version_id": latest_id,
                        "is_revision": run is not None, "can_continue": version["id"] == latest_id,
                        "version_can_continue": version["id"] == latest_id}
                tasks = self.store.list_records("review_tasks", version_id=version["id"])
                if not tasks:
                    items.append({**base, "record_kind": "import", "task_id": None, "created_at": version["created_at"],
                                  "issue_count": sum(i.get("comparison_tag") != "已解决" for i in self.store.list_records("issues", version_id=version["id"])),
                                  "status": run["status"] if run else "original", "count_source": "current_version"})
                for task in tasks:
                    meta = (task.get("checkpoints") or {}).get("_meta") or {}
                    snapshot = meta.get("history_snapshot") or {}
                    issue_count = snapshot.get("issue_count", (meta.get("counts") or {}).get("issue_count"))
                    items.append({**base, "record_kind": "review", "task_id": task["id"], "created_at": task["created_at"],
                                  "issue_count": issue_count, "status": task["status"], "tolerance_snapshot": task["tolerance_snapshot"],
                                  "comparison_counts": snapshot.get("comparison_counts"),
                                  "count_source": "task_snapshot" if snapshot else "task_checkpoint" if issue_count is not None else "unrecorded"})
        return {"items": sorted(items, key=lambda item: (item["created_at"], item["sequence"]), reverse=True), "projects": projects}

    def open_project(self, project_id, version_id=None, source_version_id=None):
        project = self.store.get("projects", project_id)
        versions = self.store.list_records("drawing_versions", project_id=project_id)
        if any(requested and not any(version["id"] == requested for version in versions) for requested in (version_id, source_version_id)):
            raise StoreError(ErrorCode.NOT_FOUND, "项目记录不存在")
        active = [version for version in versions if (self._run(version["id"]) or {}).get("status") != "draft"]
        if not active:
            raise StoreError(ErrorCode.NOT_FOUND, "项目记录不存在")
        latest_id = active[-1]["id"]
        selected = next((version for version in versions if version["id"] == version_id), None) if version_id else active[-1]
        source_version_id, version_id = source_version_id or version_id, selected["id"]
        units = self.store.list_records("drawing_units", version_id=version_id)
        files = self.store.list_records("original_files", version_id=version_id)
        result = self._manifest(version_id)
        visible_originals = {unit["original_id"] for unit in units}
        visible_originals.update(row.get("original_id") for row in result.get("unreadable", []))
        file_count = sum(original["id"] in visible_originals for original in files)
        tasks = self.store.list_records("review_tasks", version_id=version_id)
        return present_import_diagnostics({**result, "project_id": project_id, "project_name": project["name"], "version_id": version_id,
                "version_name": selected["name"], "created_at": selected["created_at"], "sequence": selected["sequence"],
                "is_revision": self._run(version_id) is not None, "can_continue": version_id == latest_id, "source_version_id": source_version_id,
                "latest_version_id": latest_id, "drawings": [{**u, "unit_id": u["id"], **u["metadata"]} for u in units],
                "unreadable": result.get("unreadable", []), "file_count": file_count, "unit_count": len(units),
                "summary": f"已导入 {file_count} 个文件 · 拆出 {len(units)} 张图", "task_id": tasks[-1]["id"] if tasks else None})
