-- 初始模型：跨版本引用使用复合外键；旧版本写保护由迁移器创建触发器。
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(name)>0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE drawing_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    sequence INTEGER NOT NULL CHECK(sequence>0), name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id,sequence), UNIQUE(id,project_id)
);
CREATE TABLE original_files (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL, project_id TEXT NOT NULL,
    name TEXT NOT NULL, relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(version_id,project_id) REFERENCES drawing_versions(id,project_id),
    UNIQUE(version_id,sha256), UNIQUE(id,version_id),
    CHECK(relative_path='projects/' || project_id || '/originals/' || sha256)
);
CREATE TABLE drawing_units (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    original_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('pdf_page','dwg_frame')),
    ordinal INTEGER NOT NULL CHECK(ordinal>=1), name TEXT NOT NULL,
    drawing_number TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    FOREIGN KEY(original_id,version_id) REFERENCES original_files(id,version_id),
    UNIQUE(original_id,kind,ordinal), UNIQUE(id,version_id)
);
CREATE TABLE facts (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    unit_id TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('看图识别','PDF文字','CAD文字','用户确认')),
    text TEXT NOT NULL, numeric_value TEXT, unit TEXT,
    x0 REAL NOT NULL CHECK(x0>=0 AND x0<=1), y0 REAL NOT NULL CHECK(y0>=0 AND y0<=1),
    x1 REAL NOT NULL CHECK(x1>=x0 AND x1<=1), y1 REAL NOT NULL CHECK(y1>=y0 AND y1<=1),
    FOREIGN KEY(unit_id,version_id) REFERENCES drawing_units(id,version_id), UNIQUE(id,version_id)
);
CREATE TABLE associations (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    left_unit_id TEXT NOT NULL, right_unit_id TEXT NOT NULL, kind TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    FOREIGN KEY(left_unit_id,version_id) REFERENCES drawing_units(id,version_id),
    FOREIGN KEY(right_unit_id,version_id) REFERENCES drawing_units(id,version_id)
);
CREATE TABLE issues (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    title TEXT NOT NULL,
    left_unit_id TEXT NOT NULL, left_source TEXT NOT NULL CHECK(left_source IN ('看图识别','PDF文字','CAD文字','用户确认')),
    left_text TEXT NOT NULL,
    left_x0 REAL NOT NULL CHECK(left_x0>=0 AND left_x0<=1), left_y0 REAL NOT NULL CHECK(left_y0>=0 AND left_y0<=1),
    left_x1 REAL NOT NULL CHECK(left_x1>=left_x0 AND left_x1<=1), left_y1 REAL NOT NULL CHECK(left_y1>=left_y0 AND left_y1<=1),
    right_unit_id TEXT NOT NULL, right_source TEXT NOT NULL CHECK(right_source IN ('看图识别','PDF文字','CAD文字','用户确认')),
    right_text TEXT NOT NULL,
    right_x0 REAL NOT NULL CHECK(right_x0>=0 AND right_x0<=1), right_y0 REAL NOT NULL CHECK(right_y0>=0 AND right_y0<=1),
    right_x1 REAL NOT NULL CHECK(right_x1>=right_x0 AND right_x1<=1), right_y1 REAL NOT NULL CHECK(right_y1>=right_y0 AND right_y1<=1),
    rule_result_json TEXT NOT NULL CHECK(json_valid(rule_result_json)),
    primary_opinion_json TEXT NOT NULL CHECK(json_valid(primary_opinion_json)),
    review_opinion_json TEXT NOT NULL CHECK(json_valid(review_opinion_json)),
    review_status TEXT NOT NULL DEFAULT '疑似' CHECK(review_status IN ('疑似','已确认','排除','需补充资料')),
    review_note TEXT NOT NULL DEFAULT '',
    comparison_tag TEXT CHECK(comparison_tag IN ('仍存在','已解决','新出现')),
    previous_issue_id TEXT REFERENCES issues(id),
    FOREIGN KEY(left_unit_id,version_id) REFERENCES drawing_units(id,version_id),
    FOREIGN KEY(right_unit_id,version_id) REFERENCES drawing_units(id,version_id), UNIQUE(id,version_id)
);
CREATE TABLE pending_confirmations (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    unit_id TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('看图识别','PDF文字','CAD文字','用户确认')),
    text TEXT NOT NULL, reason TEXT NOT NULL,
    x0 REAL NOT NULL CHECK(x0>=0 AND x0<=1), y0 REAL NOT NULL CHECK(y0>=0 AND y0<=1),
    x1 REAL NOT NULL CHECK(x1>=x0 AND x1<=1), y1 REAL NOT NULL CHECK(y1>=y0 AND y1<=1),
    status TEXT NOT NULL DEFAULT '待确认' CHECK(status IN ('待确认','已确认','无法确定')),
    confirmed_text TEXT,
    FOREIGN KEY(unit_id,version_id) REFERENCES drawing_units(id,version_id)
);
CREATE TABLE review_tasks (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    status TEXT NOT NULL DEFAULT '待开始' CHECK(status IN ('待开始','进行中','暂停','已完成','失败')),
    checkpoints_json TEXT NOT NULL CHECK(json_valid(checkpoints_json)),
    estimated_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK(estimated_cost_micros>=0),
    incomplete_json TEXT NOT NULL CHECK(json_valid(incomplete_json)),
    tolerance_snapshot_json TEXT NOT NULL CHECK(json_valid(tolerance_snapshot_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(id,version_id)
);
CREATE TABLE model_calls (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id), task_id TEXT NOT NULL,
    unit_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('primary','review')),
    provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK(input_tokens>=0), output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
    estimated_cost_micros INTEGER NOT NULL CHECK(estimated_cost_micros>=0),
    image_sha256 TEXT NOT NULL, prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(task_id,version_id) REFERENCES review_tasks(id,version_id),
    FOREIGN KEY(unit_id,version_id) REFERENCES drawing_units(id,version_id)
);
CREATE TABLE exports (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES drawing_versions(id), task_id TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('pdf','xlsx')), relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(task_id,version_id) REFERENCES review_tasks(id,version_id)
);
CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK(id=1), value_json TEXT NOT NULL CHECK(json_valid(value_json))
);
CREATE INDEX issues_by_version ON issues(version_id);
CREATE INDEX model_calls_by_time ON model_calls(created_at);
CREATE INDEX units_by_version ON drawing_units(version_id);
