from __future__ import annotations

from pydantic import BaseModel, Field

from app.schema.enums import IssueCode, Severity


class QAIssue(BaseModel):
    code: IssueCode
    severity: Severity
    page_no: int | None = None
    detail: str
    action_taken: str | None = None


class QAReport(BaseModel):
    job_id: str | None = None
    summary: str = ""
    issues: list[QAIssue] = Field(default_factory=list)

    @property
    def blocking(self) -> list[QAIssue]:
        return [i for i in self.issues if i.severity == Severity.BLOCKING]

    @property
    def cosmetic(self) -> list[QAIssue]:
        return [i for i in self.issues if i.severity == Severity.COSMETIC]
