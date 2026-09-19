"""SQLAlchemy models. Portable column types only, so the same models run on Postgres."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (UniqueConstraint("run_id", "span_id", name="uq_span_per_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    service: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16))
    metric_name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(DateTime)  # UTC
