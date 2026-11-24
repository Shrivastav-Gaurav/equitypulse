"""Apply config/manual_data.yaml: explained gaps, explained ISIN links, missing demergers.

Everything here is hand-verified and committed, so a rebuilt database gets the same facts.
Applying is idempotent. Each gap and link explanation is recorded once in `data_audit`
(job "gap_review", status OK), because data_audit is where the gate looks for explanations.
"""

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.audit import begin_audit, finish_audit, new_run_id
from equitypulse.db.models import CorporateAction, DataAudit, IsinLink, Security

DEFAULT_PATH = Path("config/manual_data.yaml")
SOURCE = "manual"
AUDIT_JOB = "gap_review"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class GapNote(_Strict):
    isin: str
    symbol: str
    start: dt.date = Field(alias="from")
    end: dt.date = Field(alias="to")
    reason: str

    def covers(self, isin: str, day: dt.date) -> bool:
        return isin == self.isin and self.start <= day <= self.end

    def audit_text(self) -> str:
        return f"{self.symbol} {self.isin} {self.start}..{self.end}: {self.reason}"


class LinkNote(_Strict):
    old_isin: str
    new_isin: str
    symbol: str
    reason: str = Field(pattern=r"^[A-Z_]{3,32}$")
    note: str

    def audit_text(self) -> str:
        link = f"{self.old_isin}->{self.new_isin}"
        return f"{self.symbol} ISIN change {link}: {self.reason}. {self.note}"


class ManualAction(_Strict):
    isin: str
    symbol: str
    ex_date: dt.date
    action_type: Literal["DEMERGER", "RIGHTS", "OTHER"]  # never a price factor by hand
    details: str


class ManualData(_Strict):
    gaps: list[GapNote] = []
    isin_links: list[LinkNote] = []
    corporate_actions: list[ManualAction] = []


class ApplyResult(BaseModel):
    actions_added: int = 0
    links_updated: int = 0
    audit_rows_added: int = 0


def load_manual_data(path: Path = DEFAULT_PATH) -> ManualData:
    return ManualData.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def apply_manual_data(session: Session, data: ManualData) -> ApplyResult:
    """Write the file's facts. Refuses entries whose ISIN or link is not in the database."""
    result = ApplyResult()
    for a in data.corporate_actions:
        if session.get(Security, a.isin) is None:
            raise ValueError(f"manual action for unknown ISIN {a.isin} ({a.symbol})")
        exists = session.scalar(select(CorporateAction).where(
            CorporateAction.isin == a.isin, CorporateAction.ex_date == a.ex_date,
            CorporateAction.action_type == a.action_type,
        ))
        if exists is None:
            session.add(CorporateAction(isin=a.isin, ex_date=a.ex_date, action_type=a.action_type,
                                        details=a.details, adj_factor=None, source=SOURCE))
            result.actions_added += 1

    for n in data.isin_links:
        link = session.get(IsinLink, (n.old_isin, n.new_isin))
        if link is None:
            raise ValueError(f"no ISIN link {n.old_isin}->{n.new_isin} ({n.symbol}) to explain")
        if link.reason != n.reason:
            link.reason = n.reason
            result.links_updated += 1

    recorded = set(session.scalars(select(DataAudit.error).where(DataAudit.job == AUDIT_JOB)))
    notes: list[tuple[str, dt.date | None]] = [(g.audit_text(), g.start) for g in data.gaps]
    notes += [(n.audit_text(), None) for n in data.isin_links]
    for text, as_of in notes:
        if text in recorded:
            continue
        run_id = new_run_id()
        begin_audit(session, run_id=run_id, job=AUDIT_JOB, source=SOURCE, as_of=as_of)
        finish_audit(session, run_id=run_id, job=AUDIT_JOB, source=SOURCE, status="OK",
                     error=text)
        result.audit_rows_added += 1
    session.flush()
    return result
