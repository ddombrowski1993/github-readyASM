from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class TimestampMixin:
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Team(Base, TimestampMixin):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    team_name = Column(String(120), unique=True, nullable=False)
    team_type = Column(String(80), default="Other")
    city = Column(String(120))
    state = Column(String(20))
    notes = Column(Text)
    active = Column(Boolean, default=True, nullable=False)


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    first_name = Column(String(100))
    last_name = Column(String(100))
    full_name = Column(String(220), nullable=False)
    employee_number = Column(String(80), unique=True)
    role = Column(String(100))
    team_id = Column(Integer, ForeignKey("teams.id"))
    phone = Column(String(50))
    email = Column(String(200))
    hire_date = Column(Date)
    truck_number = Column(String(80))
    home_address = Column(String(255))
    home_city = Column(String(120))
    home_state = Column(String(20))
    home_zip = Column(String(20))
    home_latitude = Column(Float)
    home_longitude = Column(Float)
    base_city = Column(String(120))
    base_state = Column(String(20))
    base_latitude = Column(Float)
    base_longitude = Column(Float)
    monthly_pmt_store_target = Column(Integer, default=10)
    active = Column(Boolean, default=True, nullable=False)
    inactive_reason = Column(Text)
    notes = Column(Text)
    color = Column(String(20))
    team = relationship("Team")


class Store(Base, TimestampMixin):
    __tablename__ = "stores"
    id = Column(Integer, primary_key=True)
    store_number = Column(String(80), unique=True, nullable=False)
    store_name = Column(String(200))
    address = Column(String(255))
    city = Column(String(120))
    state = Column(String(20))
    zip = Column(String(20))
    latitude = Column(Float)
    longitude = Column(Float)
    market = Column(String(120))
    district = Column(String(120))
    area = Column(String(120))
    assigned_pmt_employee_id = Column(Integer, ForeignKey("employees.id"))
    assigned_brand_employee_id = Column(Integer, ForeignKey("employees.id"))
    assigned_calibration_employee_id = Column(Integer, ForeignKey("employees.id"))
    assigned_pmt_team_id = Column(Integer, ForeignKey("teams.id"))
    assigned_brand_team_id = Column(Integer, ForeignKey("teams.id"))
    assigned_calibration_team_id = Column(Integer, ForeignKey("teams.id"))
    store_status = Column(String(80), default="Not Started")
    priority = Column(String(40), default="Medium")
    notes = Column(Text)
    active = Column(Boolean, default=True, nullable=False)


class StoreAssignment(Base, TimestampMixin):
    __tablename__ = "store_assignments"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"))
    team_id = Column(Integer, ForeignKey("teams.id"))
    assignment_type = Column(String(80), default="PMT")
    start_date = Column(Date)
    end_date = Column(Date)
    active = Column(Boolean, default=True, nullable=False)
    notes = Column(Text)


class MapArea(Base, TimestampMixin):
    __tablename__ = "map_areas"
    id = Column(Integer, primary_key=True)
    area_name = Column(String(180), nullable=False)
    area_type = Column(String(80), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"))
    assignment_type = Column(String(80))
    team_members = Column(Text)
    home_base = Column(String(255))
    geometry_json = Column(Text, nullable=False)
    assigned_store_ids = Column(Text)
    color = Column(String(20))
    active = Column(Boolean, default=True, nullable=False)


class CustomCityAnchor(Base, TimestampMixin):
    __tablename__ = "custom_city_anchors"
    __table_args__ = (UniqueConstraint("city", "state", name="uq_custom_city_anchor_city_state"),)
    id = Column(Integer, primary_key=True)
    city = Column(String(120), nullable=False)
    state = Column(String(20), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    notes = Column(Text)
    active = Column(Boolean, default=True, nullable=False)


class CalloffPTO(Base, TimestampMixin):
    __tablename__ = "calloff_pto"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    event_date = Column(Date, nullable=False)
    end_date = Column(Date)
    event_type = Column(String(80), nullable=False)
    status = Column(String(80), default="Logged")
    approved_by = Column(String(160))
    notes = Column(Text)


class Followup(Base, TimestampMixin):
    __tablename__ = "followups"
    id = Column(Integer, primary_key=True)
    followup_type = Column(String(80), default="Store / National Account")
    store_id = Column(Integer, ForeignKey("stores.id"))
    issue_title = Column(String(255), nullable=False)
    issue_description = Column(Text)
    category = Column(String(120))
    priority = Column(String(40), default="Medium")
    assigned_employee_id = Column(Integer, ForeignKey("employees.id"))
    related_person = Column(String(180))
    external_contact = Column(String(180))
    organization = Column(String(180))
    vendor = Column(String(180))
    status = Column(String(80), default="Open")
    date_opened = Column(Date)
    last_followup_date = Column(Date)
    next_followup_date = Column(Date)
    due_date = Column(Date)
    completed_date = Column(Date)
    resolution_notes = Column(Text)
    internal_notes = Column(Text)


class FollowupOption(Base, TimestampMixin):
    __tablename__ = "followup_options"
    __table_args__ = (UniqueConstraint("option_type", "option_value", name="uq_followup_option_type_value"),)
    id = Column(Integer, primary_key=True)
    option_type = Column(String(80), nullable=False)
    option_value = Column(String(180), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    notes = Column(Text)


class DeferredWorkOrder(Base, TimestampMixin):
    __tablename__ = "deferred_work_orders"
    id = Column(Integer, primary_key=True)
    work_order_number = Column(String(120), unique=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    title = Column(String(255), nullable=False)
    description = Column(Text)
    work_order_type = Column(String(120))
    priority = Column(String(40), default="Medium")
    assigned_employee_id = Column(Integer, ForeignKey("employees.id"))
    assigned_team_id = Column(Integer, ForeignKey("teams.id"))
    completed_team_id = Column(Integer, ForeignKey("teams.id"))
    status = Column(String(80), default="Available")
    date_created = Column(Date)
    assigned_date = Column(Date)
    due_date = Column(Date)
    completed_date = Column(Date)
    notes = Column(Text)


class SiteVisit(Base, TimestampMixin):
    __tablename__ = "site_visits"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    scheduled_date = Column(Date)
    visit_date = Column(Date)
    status = Column(String(80), default="Planned")
    visit_type = Column(String(120), default="Site Visit")
    lot_striping = Column(String(80))
    landscaping = Column(String(80))
    equipment_audit = Column(String(80))
    pest_issues = Column(String(80))
    fire_extinguishers = Column(String(80))
    comments = Column(Text)
    next_action = Column(Text)


class ConstructionProject(Base, TimestampMixin):
    __tablename__ = "construction_projects"
    id = Column(Integer, primary_key=True)
    project_key = Column(String(180), unique=True, nullable=False)
    project_number = Column(String(120))
    store_number = Column(String(80))
    project_type = Column(String(120))
    project_name = Column(String(255))
    address = Column(String(255))
    city = Column(String(120))
    state = Column(String(20))
    zip = Column(String(20))
    latitude = Column(Float)
    longitude = Column(Float)
    status = Column(String(120))
    start_date = Column(Date)
    end_date = Column(Date)
    notes = Column(Text)
    active = Column(Boolean, default=True, nullable=False)


class Schedule(Base, TimestampMixin):
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    schedule_name = Column(String(200), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"))
    schedule_type = Column(String(80), default="Weekly")
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    created_by = Column(String(160))
    status = Column(String(80), default="Draft")
    notes = Column(Text)


class ScheduleItem(Base, TimestampMixin):
    __tablename__ = "schedule_items"
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id"), nullable=False)
    schedule_date = Column(Date, nullable=False)
    sequence_number = Column(Integer, default=1)
    store_id = Column(Integer, ForeignKey("stores.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"))
    team_id = Column(Integer, ForeignKey("teams.id"))
    work_type = Column(String(100), default="Store Visit")
    schedule_source = Column(String(160))
    pmt_schedule_run_id = Column(Integer, ForeignKey("pmt_schedule_runs.id"))
    cycle_label = Column(String(160))
    deferred_work_order_id = Column(Integer, ForeignKey("deferred_work_orders.id"))
    planned_start_time = Column(String(20))
    planned_end_time = Column(String(20))
    status = Column(String(80), default="Scheduled")
    original_schedule_date = Column(Date)
    rescheduled_from_item_id = Column(Integer, ForeignKey("schedule_items.id"))
    rain_delay = Column(Boolean, default=False, nullable=False)
    weather_notes = Column(Text)
    completion_notes = Column(Text)


class PMTScheduleRun(Base, TimestampMixin):
    __tablename__ = "pmt_schedule_runs"
    id = Column(Integer, primary_key=True)
    run_name = Column(String(220), nullable=False)
    cycle_start = Column(Date, nullable=False)
    cycle_end = Column(Date, nullable=False)
    months = Column(Integer, default=6, nullable=False)
    default_monthly_target = Column(Integer, default=10, nullable=False)
    direction = Column(String(80), default="Closest to home first")
    schedule_mode = Column(String(120), default="Monthly store list only")
    distance_method = Column(String(120), default="Estimated straight-line distance")
    status = Column(String(80), default="Published")
    technician_count = Column(Integer, default=0)
    store_count = Column(Integer, default=0)
    unscheduled_count = Column(Integer, default=0)
    created_by = Column(String(160))
    notes = Column(Text)


class PMTScheduleBacklog(Base, TimestampMixin):
    __tablename__ = "pmt_schedule_backlog"
    id = Column(Integer, primary_key=True)
    pmt_schedule_run_id = Column(Integer, ForeignKey("pmt_schedule_runs.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    cycle_start = Column(Date, nullable=False)
    cycle_end = Column(Date)
    status = Column(String(80), default="Not Scheduled")
    reason = Column(String(240))
    cycles_missed = Column(Integer, default=1)
    last_scheduled_month = Column(Date)
    last_completed_month = Column(Date)
    last_completed_date = Column(Date)
    priority_score = Column(Integer, default=0)
    notes = Column(Text)
    __table_args__ = (
        UniqueConstraint("pmt_schedule_run_id", "employee_id", "store_id", "status", name="uq_pmt_backlog_run_employee_store_status"),
    )


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    id = Column(Integer, primary_key=True)
    related_table = Column(String(120), nullable=False)
    related_id = Column(Integer)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(80))
    file_path_or_url = Column(String(500), nullable=False)
    uploaded_by = Column(String(160))
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    notes = Column(Text)


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True)
    report_name = Column(String(255), nullable=False)
    report_type = Column(String(120))
    date_range_start = Column(Date)
    date_range_end = Column(Date)
    file_path_or_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(160))
    notes = Column(Text)


class PMCompletionReportRow(Base, TimestampMixin):
    __tablename__ = "pm_completion_report_rows"
    __table_args__ = (
        UniqueConstraint("report_week", "work_order_number", "employee_id", name="uq_pm_report_week_wo_employee"),
    )
    id = Column(Integer, primary_key=True)
    report_week = Column(Date, nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    technician_name = Column(String(220), nullable=False)
    employee_number = Column(String(80))
    work_order_number = Column(String(120), nullable=False)
    store_number = Column(String(80))
    category = Column(String(120))
    status = Column(String(80), default="Open")
    raw_status = Column(String(160))
    date_opened = Column(Date)
    completed_date = Column(Date)
    days_open = Column(Integer)
    notes = Column(Text)
    employee = relationship("Employee")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    action_type = Column(String(120), nullable=False)
    table_name = Column(String(120))
    record_id = Column(Integer)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
