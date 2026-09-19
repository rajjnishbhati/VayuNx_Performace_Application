"""SQLAlchemy models. Portable column types only, so the same models run on Postgres."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Row ids of the high-volume tables: BIGINT on Postgres (INTEGER tops out at 2.1 billion rows). SQLite keeps
# INTEGER, which is its 64-bit rowid alias - a BIGINT primary key would not autoincrement there.
RowId = BigInteger().with_variant(Integer, "sqlite")


class Run(Base):
    """One profiling session against one version of the code."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(256))
    phase: Mapped[str] = mapped_column(String(16))  # "baseline" | "remediated"
    created_at: Mapped[datetime] = mapped_column(DateTime)  # UTC
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    # v2 (Phase 2). phase stays for v1: a Lab reference variant is stored as "baseline", candidates as "remediated".
    variant: Mapped[str | None] = mapped_column(String(256), nullable=True)  # e.g. "Argon2id m=64 MiB t=3 p=4"
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trial_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(8), default="app", server_default="app")  # "lab" | "app"
    env_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # environment fingerprint
    project_id: Mapped[str] = mapped_column(String(64), default="default", server_default="default", index=True)


class Experiment(Base):
    """A set of runs compared together: Lab trials of several variants, or selected app runs."""

    __tablename__ = "experiments"

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(8))  # "lab" | "app"
    label: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16))  # queued | running | complete | failed | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    reference_preset: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    current_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str] = mapped_column(String(64), default="default", server_default="default", index=True)


class TrialResult(Base):
    """Summary of one Lab trial (one fresh worker subprocess). One row per Lab run."""

    __tablename__ = "trial_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), index=True)
    preset_id: Mapped[str] = mapped_column(String(64))
    trial_index: Mapped[int] = mapped_column(Integer)
    concurrency: Mapped[int] = mapped_column(Integer)
    ops: Mapped[int] = mapped_column(BigInteger)
    wall_s: Mapped[float] = mapped_column(Float)
    ops_per_s: Mapped[float] = mapped_column(Float)
    cpu_user_s: Mapped[float] = mapped_column(Float)
    cpu_system_s: Mapped[float] = mapped_column(Float)
    cpu_s_per_op: Mapped[float] = mapped_column(Float)
    cores_busy: Mapped[float] = mapped_column(Float)
    rss_before_bytes: Mapped[int] = mapped_column(BigInteger)
    peak_rss_bytes: Mapped[int] = mapped_column(BigInteger)
    peak_rss_method: Mapped[str] = mapped_column(String(32))
    threads_max: Mapped[int] = mapped_column(Integer)
    ctx_switches: Mapped[int] = mapped_column(BigInteger)
    timer_overhead_ns: Mapped[float] = mapped_column(Float)
    measure_start: Mapped[datetime] = mapped_column(DateTime)
    measure_end: Mapped[datetime] = mapped_column(DateTime)
    quiet_json: Mapped[str] = mapped_column(Text)
    noisy: Mapped[int] = mapped_column(Integer)  # 0/1
    other_cores_busy_median: Mapped[float | None] = mapped_column(Float, nullable=True)  # from 100 ms samples (display)
    other_cores_busy_trial: Mapped[float | None] = mapped_column(Float, nullable=True)  # from cumulative counters (decides noisy)
    sampler_overhead_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_json: Mapped[str] = mapped_column(Text)


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (UniqueConstraint("run_id", "span_id", name="uq_span_per_run"),)

    id: Mapped[int] = mapped_column(RowId, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    span_id: Mapped[str] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16))  # "general" | "cryptographic"
    span_name: Mapped[str] = mapped_column(String(256))
    start_time: Mapped[datetime] = mapped_column(DateTime)  # UTC
    end_time: Mapped[datetime] = mapped_column(DateTime)  # UTC
    duration_ms: Mapped[float] = mapped_column(Float)
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")


class OpStat(Base):
    """Interval summary of a fast-path operation: many calls aggregated in the SDK (latency histogram)."""

    __tablename__ = "op_stats"

    id: Mapped[int] = mapped_column(RowId, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    service: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16))
    op_name: Mapped[str] = mapped_column(String(256))
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")
    interval_start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    interval_end: Mapped[datetime] = mapped_column(DateTime)  # UTC
    count: Mapped[int] = mapped_column(BigInteger)
    sum_ns: Mapped[int] = mapped_column(BigInteger)
    min_ns: Mapped[int] = mapped_column(BigInteger)
    max_ns: Mapped[int] = mapped_column(BigInteger)
    p50_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p95_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p99_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    histogram_json: Mapped[str] = mapped_column(Text)
    sdk_overhead_ns: Mapped[float | None] = mapped_column(Float, nullable=True)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(RowId, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    service: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16))
    metric_name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(DateTime)  # UTC


# ----------------------------------------------------------------------------- Phase 4: sign-in and API tokens


class User(Base):
    """Someone who signed in through the organisation's identity provider (OIDC)."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_user_identity"),)

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))  # the IdP's stable id ("sub"), not the email
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthSession(Base):
    """A browser sign-in. Only a SHA-256 of the cookie value is stored, so a database leak is no session leak."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiToken(Base):
    """A bearer token for SDKs and CI. Only its SHA-256 is stored; the value is shown once, at creation."""

    __tablename__ = "api_tokens"

    token_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # first characters, to recognise a token in lists
    name: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = any project the user can use


# ----------------------------------------------------------------------------- Phase 4: projects, teams, roles

ROLES = ("viewer", "editor", "admin")  # each includes the ones before it
DEFAULT_PROJECT_ID = "default"


class Project(Base):
    """Holds runs and experiments. `default_role` is what every signed-in person gets without a team grant."""

    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # also the URL slug
    name: Mapped[str] = mapped_column(String(128))
    default_role: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None | viewer | editor
    created_at: Mapped[datetime] = mapped_column(DateTime)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = keep forever (the default)
    retention_last_purge_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), primary_key=True)


class ProjectGrant(Base):
    """A team's role on a project."""

    __tablename__ = "project_grants"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))


class ShareLink(Base):
    """A read-only link to one comparison (Phase 4). Only a SHA-256 of the link's token is stored."""

    __tablename__ = "share_links"

    share_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    target_json: Mapped[str] = mapped_column(Text)  # {"experiment_id" | "run_ids", "reference"}
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # user_id; None when sign-in is off
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
