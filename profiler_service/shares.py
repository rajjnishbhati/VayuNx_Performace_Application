"""Share links (Phase 4): a read-only link to one comparison that works without signing in.

The link carries a random token (only its SHA-256 is stored). Opening it shows that comparison - with the
reference it was shared with - and its machine view and exports, nothing else: the viewer gets the viewer
role on that one project, only for the experiment or runs named in the link. Links expire (1-90 days) and
can be revoked; data removed since (for example by retention) answers 410 Gone.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from profiler_service.access import Access, access_for, load_experiment
from profiler_service.api_v2 import compare_v2, experiment_timeseries
from profiler_service.auth import sha256
from profiler_service.exports import compare_pdf_bytes, scorecard_rows, SCORECARD, _csv
from profiler_service.models import Experiment, Run, ShareLink

router = APIRouter(prefix="/v2", tags=["shares"])
TOKEN_PREFIX = "vxs_"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _problem(status: int, error: str, fix: str):
    return HTTPException(status, {"error": error, "fix": fix})


class ShareIn(BaseModel):
    experiment_id: str | None = None
    run_ids: list[str] | None = Field(default=None, max_length=50)
    reference: str | None = None
    expires_days: int = Field(default=7, ge=1, le=90)

    @model_validator(mode="after")
    def one_target(self):
        if bool(self.experiment_id) == bool(self.run_ids):
            raise ValueError("share either an experiment_id or run_ids")
        return self


def _share_out(link: ShareLink) -> dict:
    target = json.loads(link.target_json)
    return {"share_id": link.share_id, "project_id": link.project_id, "target": target, "created_at": _iso(link.created_at),
            "expires_at": _iso(link.expires_at), "revoked": link.revoked_at is not None, "views": link.views,
            "created_by": link.created_by}


@router.post("/shares", status_code=201)
def create_share(request: Request, body: ShareIn) -> dict:
    with request.app.state.sessionmaker() as s:
        access = access_for(request, s)
        if body.experiment_id:
            project_id = load_experiment(s, access, body.experiment_id).project_id
        else:
            runs = [s.get(Run, r) for r in body.run_ids]
            if any(r is None or not access.can(r.project_id) for r in runs):
                raise _problem(404, "One or more runs do not exist.", "Pick runs from GET /v2/runs.")
            projects = {r.project_id for r in runs}
            if len(projects) != 1:
                raise _problem(422, "A share link covers runs from one project.", "Pick runs from the same project.")
            project_id = projects.pop()
        # check now that the comparison can be built at all (and the reference is valid)
        compare_v2(s, access, body.experiment_id, ",".join(body.run_ids) if body.run_ids else None, body.reference, 100.0, None)
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        who = getattr(request.state, "identity", None)
        link = ShareLink(share_id=uuid.uuid4().hex, token_hash=sha256(raw), project_id=project_id,
                         target_json=json.dumps({"experiment_id": body.experiment_id, "run_ids": body.run_ids,
                                                 "reference": body.reference}),
                         created_by=who.user_id if who else None, created_at=_now(),
                         expires_at=_now() + timedelta(days=body.expires_days), views=0)
        s.add(link)
        s.commit()
        return {**_share_out(link), "url": f"/shared/{raw}", "note": "Anyone with this link can see this comparison "
                                                                      "until it expires. It is not shown again."}


@router.get("/shares")
def list_shares(request: Request) -> list[dict]:
    with request.app.state.sessionmaker() as s:
        access = access_for(request, s)
        q = select(ShareLink).order_by(ShareLink.created_at.desc())
        if not access.everything:
            who = request.state.identity
            q = q.where(ShareLink.created_by == who.user_id)
        return [_share_out(x) for x in s.scalars(q)]


@router.delete("/shares/{share_id}", status_code=204)
def revoke_share(request: Request, share_id: str):
    with request.app.state.sessionmaker() as s:
        access = access_for(request, s)
        link = s.get(ShareLink, share_id)
        who = getattr(request.state, "identity", None)
        mine = link is not None and (access.everything or (who is not None and link.created_by == who.user_id)
                                     or access.can(link.project_id, "admin"))
        if not mine:
            raise _problem(404, "No such share link.", "Pick one from GET /v2/shares.")
        if link.revoked_at is None:
            link.revoked_at = _now()
            s.commit()
    return Response(status_code=204)


# ----------------------------------------------------------------------------- opening a link (no sign-in)


def _open(request: Request, s, token: str, count: bool = True) -> tuple[ShareLink, dict, Access]:
    link = s.scalar(select(ShareLink).where(ShareLink.token_hash == sha256(token)))
    if link is None:
        raise _problem(404, "This share link does not exist.", "Ask the person who shared it for a new link.")
    if link.revoked_at is not None or link.expires_at <= _now():
        raise _problem(410, "This share link has expired or was revoked.", "Ask the person who shared it for a new link.")
    target = json.loads(link.target_json)
    gone = (target["experiment_id"] and s.get(Experiment, target["experiment_id"]) is None) or \
        (target["run_ids"] and any(s.get(Run, r) is None for r in target["run_ids"]))
    if gone:
        raise _problem(410, "The data behind this link no longer exists.", "It may have been removed by data retention.")
    if count:
        link.views += 1
        s.commit()
    # read-only, one project, and only what the link names (checked by the callers below)
    return link, target, Access(everything=False, roles={link.project_id: "viewer"})


def _shared_result(request: Request, token: str, count: bool = True) -> dict:
    with request.app.state.sessionmaker() as s:
        link, t, access = _open(request, s, token, count)
        result = compare_v2(s, access, t["experiment_id"], ",".join(t["run_ids"]) if t["run_ids"] else None,
                            t["reference"], 100.0, None)
        result["shared"] = {"expires_at": _iso(link.expires_at), "created_at": _iso(link.created_at)}
        return result


@router.get("/shared/{token}")
def open_share(request: Request, token: str) -> dict:
    if token.endswith((".pdf", ".csv")):
        return _export(request, token)
    return _shared_result(request, token)


@router.get("/shared/{token}/timeseries")
def shared_timeseries(request: Request, token: str) -> dict:
    with request.app.state.sessionmaker() as s:
        _, t, access = _open(request, s, token, count=False)
        if not t["experiment_id"]:
            raise _problem(404, "App comparisons have no machine time series.", "Open the comparison itself.")
        return experiment_timeseries(t["experiment_id"], s, access)


def _export(request: Request, name: str) -> Response:
    token, kind = name.rsplit(".", 1)
    result = _shared_result(request, token, count=False)
    if kind == "pdf":
        return Response(compare_pdf_bytes(result), media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="vayunx-shared-comparison.pdf"'})
    return _csv(scorecard_rows(result), SCORECARD, "vayunx-shared-comparison.csv")
