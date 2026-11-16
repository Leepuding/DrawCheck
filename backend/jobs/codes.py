"""审查引擎文案与错误码（Q-02 / Q-06 / Q-10 定稿句不得改写）。"""
from __future__ import annotations

# 管道步骤顺序（对齐 MVP；后段可 no-op / skip）
STEPS: tuple[str, ...] = (
    "extract",
    "semantic",
    "link",
    "rules_a",
    "rules_b",
    "dual_review",
    "finalize",
)

# 产品可见定稿句
MSG_RUNNING = "正在审查图纸，请稍候"
MSG_REVIEW_DISCLOSURE_REQUIRED = "请重新点「开始审查」并确认模型告知。"
MSG_RESUME_PROBE = "上次审查未完成，点「开始审查」接着审"
MSG_COST_PAUSED = "本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。"

# 任务 outcome（产品层；DB status 仍受 store CHECK 约束）
OUTCOME_PENDING = "pending"
OUTCOME_RUNNING = "running"
OUTCOME_COMPLETED = "completed"
OUTCOME_PARTIAL = "partial"
OUTCOME_COST_PAUSED = "cost_paused"
OUTCOME_FAILED = "failed"

# incomplete 分类（供 DC-15 计数）
CAT_INCOMPLETE = "incomplete"
CAT_UNREADABLE = "unreadable"
CAT_OUT_OF_SCOPE = "out_of_scope"

# 引擎错误码（与 DC-08 对齐处复用同名）
ERR_NO_MODEL = "NO_MODEL"
ERR_LIMIT_REQUIRED = "LIMIT_REQUIRED"
ERR_COST_EXCEEDED = "COST_EXCEEDED"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_INVALID = "INVALID_TASK"
ERR_SAME_MODEL_DUAL = "SAME_MODEL"
ERR_REVIEW_DISCLOSURE_REQUIRED = "REVIEW_DISCLOSURE_REQUIRED"
ERR_STEP_FAILED = "STEP_FAILED"
ERR_DATA_DIR = "DATA_DIR_NOT_WRITABLE"


class JobError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message or code)

    def as_error(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
