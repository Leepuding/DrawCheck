CREATE TABLE revision_runs (
    version_id TEXT PRIMARY KEY REFERENCES drawing_versions(id),
    previous_version_id TEXT NOT NULL REFERENCES drawing_versions(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','prepared','completed')),
    plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json)),
    summary_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(summary_json))
);
