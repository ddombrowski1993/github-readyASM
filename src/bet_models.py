"""Canonical BET history models; independent of services and Streamlit pages."""
from sqlalchemy import (Boolean, CheckConstraint, Column, Date, DateTime, Index,
                        Integer, String, Text, UniqueConstraint, text)

from src.model_base import Base, TimestampMixin


class BetServiceCompletion(Base, TimestampMixin):
    """Work history, independent of a store's current territory ownership."""
    __tablename__ = "bet_service_completions"
    __table_args__ = (
        CheckConstraint("service_type in ('power_washing', 'lot_striping')", name="ck_bet_service_type"),
        CheckConstraint("service_year between 2000 and 2200", name="ck_bet_service_year"),
        Index("uq_bet_active_store_service_year", "store_id", "service_type", "service_year",
              unique=True, postgresql_where=text("active"), sqlite_where=text("active = 1")),
        Index("ix_bet_service_year_date", "service_year", "completed_date"),
    )
    id = Column(Integer, primary_key=True)
    # Keep the work snapshot even if a master store is later archived/deleted.
    store_id = Column(Integer, nullable=False, index=True)
    store_number = Column(String(80), nullable=False)
    service_type = Column(String(40), nullable=False)
    service_year = Column(Integer, nullable=False)
    completed_date = Column(Date, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    # IDs and names are historical snapshots, not live employee/team relationships.
    bet_employee_id = Column(Integer)
    bet_employee_name = Column(String(220), nullable=False, default="")
    bet_team_id = Column(Integer)
    bet_team_name = Column(String(120), nullable=False, default="")
    entered_by_user_id = Column(Integer, nullable=False)
    entered_by = Column(String(220), nullable=False)
    source = Column(String(80), nullable=False)
    work_order_number = Column(String(160))
    notes = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    voided_at = Column(DateTime(timezone=True))
    voided_by_user_id = Column(Integer)
    voided_by = Column(String(220))
    void_reason = Column(Text)


class BetServiceExpectation(Base, TimestampMixin):
    """Optional per-store/year applicability overrides; absent rows use V1 defaults."""
    __tablename__ = "bet_service_expectations"
    __table_args__ = (
        UniqueConstraint("store_id", "service_type", "service_year", name="uq_bet_expected_store_service_year"),
        CheckConstraint("service_type in ('power_washing', 'lot_striping')", name="ck_bet_expected_service_type"),
    )
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, nullable=False, index=True)
    service_type = Column(String(40), nullable=False)
    service_year = Column(Integer, nullable=False)
    applicable = Column(Boolean, nullable=False, default=True)
