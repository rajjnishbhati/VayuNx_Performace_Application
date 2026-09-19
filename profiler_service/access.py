"""Projects, teams and roles (Phase 4).

A person's role on a project is the highest of: the project's `default_role` (what every signed-in person
gets), and the roles granted to the teams they belong to. Global administrators (VAYUNX_ADMIN_EMAILS) are
admin everywhere. With sign-in off there are no accounts, so everyone is admin - projects then only
organise data. An API token bound to a project reaches that project only, at its owner's role there.

Anything a person cannot view answers 404, exactly like something that does not exist.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from profiler_service.models import (DEFAULT_PROJECT_ID, ROLES, Experiment, Project, ProjectGrant, Run, Team,
                                     TeamMember, User)

RANK = {r: i for i, r in enumerate(ROLES)}  # viewer 0 < editor 1 < admin 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _problem(status: int, error: str, fix: str):
    return HTTPException(status, {"error": error, "fix": fix})


@dataclass
class Access:
    everything: bool  # sign-in off, or a global administrator (and not a project-bound token)
    roles: dict[str, str]  # project_id -> role (ignored when everything)
    user_id: str | None = None
    is_global_admin: bool = False
    token_project: str | None = None

    def role(self, project_id: str) -> str | None:
        return "admin" if self.everything else self.roles.get(project_id)

    def can(self, project_id: str, need: str = "viewer") -> bool:
        r = self.role(project_id)
        return r is not None and RANK[r] >= RANK[need]

    def visible(self) -> list[str] | None:
        """Project ids this identity may view; None means all."""
        return None if self.everything else sorted(self.roles)

    def require(self, project_id: str, need: str = "viewer", what: str = "run") -> None:
        if not self.can(project_id, "viewer"):
            raise _problem(404, f"No such {what}.", "Pick one from a list you can see.")
        if not self.can(project_id, need):
            raise _problem(403, f"You need the {need} role on project {project_id!r} for this.",
                           "Ask a project admin to grant your team that role.")

    def write_project(self, requested: str | None, session: Session) -> str:
        """The project new data goes into: the one requested, else the token's, else Default. Needs editor."""
        if requested and self.token_project and requested != self.token_project:
            raise _problem(403, f"This API token is bound to project {self.token_project!r}.",
                           "Send the data without a project, or use a token for the other project.")
        project_id = requested or self.token_project or DEFAULT_PROJECT_ID
        if session.get(Project, project_id) is None or not self.can(project_id, "viewer"):
            raise _problem(404, f"No such project: {project_id!r}.", "Pick a project from GET /v2/projects.")
        self.require(project_id, "editor", "project")
        return project_id


def access_for(request: Request, session: Session) -> Access:
    cached = getattr(request.state, "access", None)
    if cached is not None:
        return cached
    cfg = request.app.state.auth
    who = getattr(request.state, "identity", None)
    if not cfg.enabled or who is None:
        acc = Access(everything=True, roles={})
    else:
        token_project = None
        if who.token_id:
            from profiler_service.models import ApiToken
            token_project = session.get(ApiToken, who.token_id).project_id
        if who.is_admin and token_project is None:
            acc = Access(everything=True, roles={}, user_id=who.user_id, is_global_admin=True)
        else:
            roles: dict[str, str] = {}
            for p in session.scalars(select(Project)):
                if p.default_role in RANK:
                    roles[p.project_id] = p.default_role
            q = select(ProjectGrant.project_id, ProjectGrant.role).join(
                TeamMember, TeamMember.team_id == ProjectGrant.team_id).where(TeamMember.user_id == who.user_id)
            for project_id, role in session.execute(q):
                if RANK.get(role, -1) > RANK.get(roles.get(project_id), -1):
                    roles[project_id] = role
            if who.is_admin:
                roles = {p: "admin" for p in [*roles, token_project]}
            if token_project is not None:
                roles = {token_project: roles[token_project]} if token_project in roles else {}
            acc = Access(everything=False, roles=roles, user_id=who.user_id, is_global_admin=who.is_admin,
                         token_project=token_project)
    request.state.access = acc
    return acc


def scope_query(query, access: Access, model, project: str | None = None, session: Session | None = None):
    """Limit a query on Run/Experiment to visible projects, or to one named project (404 if not visible)."""
    if project:
        if not access.can(project) or (session is not None and session.get(Project, project) is None):
            raise _problem(404, f"No such project: {project!r}.", "Pick a project from GET /v2/projects.")
        return query.where(model.project_id == project)
    ids = access.visible()
    return query if ids is None else query.where(model.project_id.in_(ids))


def load_run(session: Session, access: Access, run_id: str, need: str = "viewer") -> Run:
    run = session.get(Run, run_id)
    if run is None or not access.can(run.project_id):
        raise HTTPException(404, f"run {run_id!r} not found")
    access.require(run.project_id, need)
    return run


def load_experiment(session: Session, access: Access, exp_id: str, need: str = "viewer") -> Experiment:
    exp = session.get(Experiment, exp_id)
    if exp is None or not access.can(exp.project_id):
        raise _problem(404, f"No experiment with id {exp_id!r}.", "Pick one from GET /v2/experiments.")
    access.require(exp.project_id, need, "experiment")
    return exp


# ----------------------------------------------------------------------------- management API

router = APIRouter(prefix="/v2")


def _session(request: Request) -> Session:
    return request.app.state.sessionmaker()


def _require_global_admin(access: Access) -> None:
    if not access.everything or access.token_project is not None:
        raise _problem(403, "Only administrators can do this.", "Ask someone listed in VAYUNX_ADMIN_EMAILS.")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "project"


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    default_role: str | None = Field(default=None, pattern="^(viewer|editor)$")


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    default_role: str | None = Field(default=None, pattern="^(none|viewer|editor)$")


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class GrantIn(BaseModel):
    team_id: str
    role: str = Field(pattern="^(viewer|editor|admin)$")


def _project_out(p: Project, access: Access, session: Session) -> dict:
    out = {"project_id": p.project_id, "name": p.name, "default_role": p.default_role, "my_role": access.role(p.project_id)}
    if access.can(p.project_id, "admin"):
        grants = session.execute(select(ProjectGrant.team_id, ProjectGrant.role, Team.name)
                                 .join(Team, Team.team_id == ProjectGrant.team_id)
                                 .where(ProjectGrant.project_id == p.project_id)).all()
        out["grants"] = [{"team_id": t, "team": n, "role": r} for t, r, n in grants]
    return out


@router.get("/projects")
def list_projects(request: Request) -> list[dict]:
    with _session(request) as s:
        access = access_for(request, s)
        ids = access.visible()
        q = select(Project).order_by(Project.project_id != DEFAULT_PROJECT_ID, Project.name)
        if ids is not None:
            q = q.where(Project.project_id.in_(ids))
        return [_project_out(p, access, s) for p in s.scalars(q)]


@router.post("/projects", status_code=201)
def create_project(request: Request, body: ProjectIn) -> dict:
    with _session(request) as s:
        access = access_for(request, s)
        _require_global_admin(access)
        pid = slugify(body.name)
        if s.get(Project, pid) is not None:
            raise _problem(409, f"A project called {pid!r} already exists.", "Choose another name.")
        p = Project(project_id=pid, name=body.name.strip(), default_role=body.default_role, created_at=_now())
        s.add(p)
        s.commit()
        return _project_out(p, access, s)


@router.patch("/projects/{project_id}")
def update_project(request: Request, project_id: str, body: ProjectPatch) -> dict:
    with _session(request) as s:
        access = access_for(request, s)
        p = s.get(Project, project_id)
        if p is None or not access.can(project_id):
            raise _problem(404, f"No such project: {project_id!r}.", "Pick a project from GET /v2/projects.")
        access.require(project_id, "admin", "project")
        if body.name is not None:
            p.name = body.name.strip()
        if body.default_role is not None:
            p.default_role = None if body.default_role == "none" else body.default_role
        s.commit()
        request.state.access = None
        return _project_out(p, access_for(request, s), s)


@router.put("/projects/{project_id}/grants")
def put_grant(request: Request, project_id: str, body: GrantIn) -> dict:
    with _session(request) as s:
        access = access_for(request, s)
        if s.get(Project, project_id) is None or not access.can(project_id):
            raise _problem(404, f"No such project: {project_id!r}.", "Pick a project from GET /v2/projects.")
        access.require(project_id, "admin", "project")
        if s.get(Team, body.team_id) is None:
            raise _problem(404, "No such team.", "Pick a team from GET /v2/teams.")
        g = s.get(ProjectGrant, (project_id, body.team_id))
        if g is None:
            s.add(ProjectGrant(project_id=project_id, team_id=body.team_id, role=body.role))
        else:
            g.role = body.role
        s.commit()
        return {"project_id": project_id, "team_id": body.team_id, "role": body.role}


@router.delete("/projects/{project_id}/grants/{team_id}", status_code=204)
def delete_grant(request: Request, project_id: str, team_id: str):
    with _session(request) as s:
        access = access_for(request, s)
        if s.get(Project, project_id) is None or not access.can(project_id):
            raise _problem(404, f"No such project: {project_id!r}.", "Pick a project from GET /v2/projects.")
        access.require(project_id, "admin", "project")
        g = s.get(ProjectGrant, (project_id, team_id))
        if g is not None:
            s.delete(g)
            s.commit()
    return Response(status_code=204)


@router.get("/teams")
def list_teams(request: Request) -> list[dict]:
    with _session(request) as s:
        access = access_for(request, s)
        teams = s.scalars(select(Team).order_by(Team.name)).all()
        out = []
        for t in teams:
            members = s.execute(select(User.user_id, User.email, User.name).join(TeamMember, TeamMember.user_id == User.user_id)
                                .where(TeamMember.team_id == t.team_id)).all()
            if not access.everything and access.user_id not in {m[0] for m in members}:
                continue  # people see the teams they are in; administrators see all
            out.append({"team_id": t.team_id, "name": t.name,
                        "members": [{"user_id": u, "email": e, "name": n} for u, e, n in members]})
        return out


@router.post("/teams", status_code=201)
def create_team(request: Request, body: TeamIn) -> dict:
    with _session(request) as s:
        _require_global_admin(access_for(request, s))
        if s.scalar(select(Team).where(Team.name == body.name.strip())) is not None:
            raise _problem(409, f"A team called {body.name!r} already exists.", "Choose another name.")
        t = Team(team_id=uuid.uuid4().hex, name=body.name.strip(), created_at=_now())
        s.add(t)
        s.commit()
        return {"team_id": t.team_id, "name": t.name, "members": []}


@router.post("/teams/{team_id}/members", status_code=201)
def add_member(request: Request, team_id: str, body: MemberIn) -> dict:
    with _session(request) as s:
        _require_global_admin(access_for(request, s))
        if s.get(Team, team_id) is None:
            raise _problem(404, "No such team.", "Pick a team from GET /v2/teams.")
        user = s.scalar(select(User).where(User.email == body.email.strip().lower()))
        if user is None:
            raise _problem(404, f"Nobody with the email {body.email!r} has signed in yet.",
                           "Ask them to sign in once, then add them.")
        if s.get(TeamMember, (team_id, user.user_id)) is None:
            s.add(TeamMember(team_id=team_id, user_id=user.user_id))
            s.commit()
        return {"team_id": team_id, "user_id": user.user_id, "email": user.email}


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
def remove_member(request: Request, team_id: str, user_id: str):
    with _session(request) as s:
        _require_global_admin(access_for(request, s))
        m = s.get(TeamMember, (team_id, user_id))
        if m is not None:
            s.delete(m)
            s.commit()
    return Response(status_code=204)


@router.get("/users")
def list_users(request: Request) -> list[dict]:
    with _session(request) as s:
        _require_global_admin(access_for(request, s))
        return [{"user_id": u.user_id, "email": u.email, "name": u.name, "is_admin": u.is_admin}
                for u in s.scalars(select(User).order_by(User.email))]
