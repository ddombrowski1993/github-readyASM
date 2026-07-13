import hashlib
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

import streamlit as st
from sqlalchemy import create_engine, inspect, text


APP_DIR = Path(__file__).resolve().parents[1]
LEGACY_DATABASE_PATH = APP_DIR / "asm_command_center.db"


class DatabaseUnavailable(RuntimeError):
    pass


def _local_streamlit_secrets_file_exists():
    candidates = [
        Path.cwd() / ".streamlit" / "secrets.toml",
        APP_DIR / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    return any(path.exists() for path in candidates)


def _secret_or_env(name, default=""):
    value = os.getenv(name)
    if value not in (None, ""):
        return str(value).strip()
    if _local_streamlit_secrets_file_exists():
        try:
            if name in st.secrets:
                return str(st.secrets[name] or "").strip()
        except Exception:
            pass
    return str(default or "").strip()


def configured_database_url():
    return _secret_or_env("DATABASE_URL")


def deployment_environment():
    return (
        _secret_or_env("FIELD_PLANNER_ENV")
        or _secret_or_env("APP_ENV")
        or _secret_or_env("ENVIRONMENT")
        or ""
    ).strip().lower()


def is_production():
    return deployment_environment() in {"prod", "production", "streamlit", "hosted"}


def using_hosted_database():
    return bool(configured_database_url())


def _safe_identifier(value, prefix="fp"):
    cleaned = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not cleaned:
        cleaned = "default"
    if cleaned[0].isdigit():
        cleaned = f"{prefix}_{cleaned}"
    return cleaned[:55]


def current_account_schema():
    if not using_hosted_database():
        return None
    account_slug = st.session_state.get("active_account_slug") or st.session_state.get("account_slug")
    if not account_slug:
        return None
    return _safe_identifier(f"fp_{account_slug}")


def data_dir():
    configured = _secret_or_env("FIELD_PLANNER_DATA_DIR")
    path = Path(configured).expanduser() if configured else APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


AUTH_DATABASE_PATH = Path(_secret_or_env("FIELD_PLANNER_AUTH_DB") or (data_dir() / "field_planner_users.db")).expanduser()
ACCOUNT_DATABASE_DIR = Path(_secret_or_env("FIELD_PLANNER_ACCOUNT_DB_DIR") or (data_dir() / "account_databases")).expanduser()
SESSION_TRANSIENT_KEYS = {
    "store_import_summary",
    "employee_import_summary",
    "dwo_import_summary",
    "pm_report_import_summary",
    "schedule_preview",
    "schedule_preview_signature",
    "pmt_schedule_draft",
    "pmt_schedule_draft_settings",
    "calibration_schedule_preview",
}
SESSION_TRANSIENT_PREFIXES = (
    "auto_assign_",
)


def clear_transient_session_state():
    for key in list(st.session_state.keys()):
        if key in SESSION_TRANSIENT_KEYS or any(key.startswith(prefix) for prefix in SESSION_TRANSIENT_PREFIXES):
            st.session_state.pop(key, None)


def _auth_database_url():
    hosted_url = configured_database_url()
    if hosted_url:
        return hosted_url
    if is_production():
        raise DatabaseUnavailable(
            "Production requires DATABASE_URL. The app stopped before creating a local SQLite database."
        )
    AUTH_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{AUTH_DATABASE_PATH.as_posix()}"


@st.cache_resource(show_spinner=False)
def _auth_engine(url):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)


def _engine():
    return _auth_engine(_auth_database_url())


def _row_dict(row):
    return dict(row._mapping) if row else None


def _rows_dict(rows):
    return [dict(row._mapping) for row in rows]


def _verify_database_identity(engine):
    if not using_hosted_database():
        return
    expected = _secret_or_env("FIELD_PLANNER_DATABASE_INSTANCE_ID")
    allow_bootstrap = _secret_or_env("FIELD_PLANNER_ALLOW_DATABASE_METADATA_BOOTSTRAP").lower() == "true"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                create table if not exists app_metadata (
                    metadata_key varchar(120) primary key,
                    metadata_value text not null,
                    updated_at timestamp not null
                )
                """
            )
        )
        saved = conn.execute(
            text("select metadata_value from app_metadata where metadata_key = 'database_instance_id'")
        ).scalar()
        if not expected:
            if not saved:
                conn.execute(
                    text(
                        """
                        insert into app_metadata (metadata_key, metadata_value, updated_at)
                        values ('application_name', 'Field Planner', :now),
                               ('environment', :environment, :now)
                        on conflict (metadata_key) do update
                        set metadata_value = excluded.metadata_value, updated_at = excluded.updated_at
                        """
                    ),
                    {"environment": deployment_environment() or "production", "now": datetime.utcnow()},
                )
            return
        if not saved:
            if allow_bootstrap:
                now = datetime.utcnow()
                conn.execute(
                    text(
                        """
                        insert into app_metadata (metadata_key, metadata_value, updated_at)
                        values ('application_name', 'Field Planner', :now),
                               ('environment', :environment, :now),
                               ('database_instance_id', :expected, :now)
                        on conflict (metadata_key) do update
                        set metadata_value = excluded.metadata_value, updated_at = excluded.updated_at
                        """
                    ),
                    {"expected": expected, "environment": deployment_environment() or "production", "now": now},
                )
                return
            raise DatabaseUnavailable(
                "Connected database is missing the Field Planner production identifier. Refusing to write to an unknown database."
            )
        if saved != expected:
            raise DatabaseUnavailable(
                "Connected database identifier does not match FIELD_PLANNER_DATABASE_INSTANCE_ID. Refusing to use the wrong database."
            )


def init_auth_db():
    if not using_hosted_database():
        ACCOUNT_DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    engine = _engine()
    _verify_database_identity(engine)
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                text(
                    """
                    create table if not exists app_users (
                        id serial primary key,
                        username text not null unique,
                        first_name text not null default '',
                        last_name text not null default '',
                        email text not null,
                        password_hash text not null,
                        account_slug text not null unique,
                        created_at timestamp not null
                    )
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    create table if not exists app_users (
                id integer primary key autoincrement,
                username text not null unique,
                first_name text not null default '',
                last_name text not null default '',
                email text not null,
                password_hash text not null,
                account_slug text not null unique,
                created_at text not null
            )
            """
                )
            )
    existing_columns = {column["name"] for column in inspect(engine).get_columns("app_users")}
    column_specs = {
        "first_name": "text not null default ''",
        "last_name": "text not null default ''",
        "secret_question": "text not null default ''",
        "secret_answer_hash": "text not null default ''",
        "account_role": "text not null default 'User'",
        "manager_user_id": "integer",
        "active": "integer not null default 1",
        "updated_at": "timestamp" if engine.dialect.name == "postgresql" else "text",
        "last_login": "timestamp" if engine.dialect.name == "postgresql" else "text",
        "position_title": "text not null default ''",
        "s_number": "text not null default ''",
        "street_address": "text not null default ''",
        "city": "text not null default ''",
        "state": "text not null default ''",
        "zip_code": "text not null default ''",
        "home_latitude": "real",
        "home_longitude": "real",
    }
    with engine.begin() as conn:
        for column_name, column_type in column_specs.items():
            if column_name not in existing_columns:
                conn.execute(text(f"alter table app_users add column {column_name} {column_type}"))
        if "first_name" not in existing_columns:
            pass
        conn.execute(
            text(
                """
            update app_users
            set account_role = 'Admin', active = 1
            where lower(email) = lower(:email)
            """,
            ),
            {"email": "daniel.dombrowski@7-11.com"},
        )


def user_count():
    init_auth_db()
    with _engine().connect() as conn:
        return int(conn.execute(text("select count(*) from app_users")).scalar() or 0)


def slugify(username):
    slug = re.sub(r"[^a-z0-9]+", "_", username.lower()).strip("_")
    return slug or "account"


def account_db_path(account_slug):
    return ACCOUNT_DATABASE_DIR / f"{account_slug}.db"


def auth_storage_status():
    init_auth_db()
    hosted = using_hosted_database()
    app_dir_resolved = APP_DIR.resolve()
    auth_resolved = AUTH_DATABASE_PATH.resolve()
    local_app_storage = False if hosted else app_dir_resolved in auth_resolved.parents or auth_resolved == app_dir_resolved
    return {
        "auth_database": "Hosted SQL database" if hosted else str(AUTH_DATABASE_PATH),
        "account_database_dir": "PostgreSQL account schemas" if hosted else str(ACCOUNT_DATABASE_DIR),
        "user_count": user_count(),
        "account_database_count": user_count() if hosted else len(list(ACCOUNT_DATABASE_DIR.glob("*.db"))) if ACCOUNT_DATABASE_DIR.exists() else 0,
        "local_app_storage": local_app_storage,
        "configured_data_dir": _secret_or_env("FIELD_PLANNER_DATA_DIR") or "",
        "hosted_database": hosted,
        "environment": deployment_environment() or "local",
    }


def current_account_db_path():
    if using_hosted_database():
        return None
    account_slug = st.session_state.get("active_account_slug") or st.session_state.get("account_slug")
    if not account_slug:
        return None
    return account_db_path(account_slug)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        salt, expected = stored_hash.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), f"{salt}${expected}")


def normalize_secret_answer(answer):
    return " ".join(str(answer or "").strip().lower().split())


def normalize_state(value):
    return str(value or "").strip().upper()[:2]


def normalize_zip(value):
    return str(value or "").strip()


def normalize_s_number(value):
    return str(value or "").strip().upper().replace(" ", "")


def create_user(
    first_name,
    last_name,
    username,
    email,
    password,
    secret_question="",
    secret_answer="",
    position_title="",
    s_number="",
    street_address="",
    city="",
    state="",
    zip_code="",
):
    init_auth_db()
    first_name = first_name.strip()
    last_name = last_name.strip()
    username = username.strip()
    email = email.strip().lower()
    position_title = str(position_title or "").strip()
    s_number = normalize_s_number(s_number)
    street_address = str(street_address or "").strip()
    city = str(city or "").strip()
    state = normalize_state(state)
    zip_code = normalize_zip(zip_code)
    if len(first_name) < 1:
        return False, "Enter your first name."
    if len(last_name) < 1:
        return False, "Enter your last name."
    if len(position_title) < 1:
        return False, "Please enter your position / job title before creating your account."
    if len(s_number) < 1:
        return False, "Please enter your S Number before creating your account."
    if len(street_address) < 1:
        return False, "Please enter your street address before creating your account."
    if len(city) < 1:
        return False, "Please enter your city before creating your account."
    if len(state) < 2:
        return False, "Please enter a two-letter state before creating your account."
    if len(zip_code) < 1:
        return False, "Please enter your ZIP code before creating your account."
    if len(username) < 3:
        return False, "Username needs at least 3 characters."
    if "@" not in email or "." not in email:
        return False, "Enter a valid email address."
    if len(password) < 6:
        return False, "Password needs at least 6 characters."
    if len(secret_question.strip()) < 3:
        return False, "Enter a password recovery question."
    if len(normalize_secret_answer(secret_answer)) < 1:
        return False, "Enter a password recovery answer."

    base_slug = slugify(username)
    account_slug = base_slug
    engine = _engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("select 1 from app_users where lower(username) = lower(:username)"),
            {"username": username},
        ).fetchone()
        if existing:
            return False, "That username already exists."
        existing_email = conn.execute(text("select 1 from app_users where lower(email) = lower(:email)"), {"email": email}).fetchone()
        if existing_email:
            return False, "That email address already has an account."
        existing_s = conn.execute(text("select 1 from app_users where upper(coalesce(s_number, '')) = upper(:s_number)"), {"s_number": s_number}).fetchone()
        if existing_s:
            return False, "That S Number is already used by another account."
        suffix = 2
        while conn.execute(text("select 1 from app_users where account_slug = :account_slug"), {"account_slug": account_slug}).fetchone():
            account_slug = f"{base_slug}_{suffix}"
            suffix += 1
        account_role = "Admin" if email.lower() == "daniel.dombrowski@7-11.com" else "User"
        now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
        conn.execute(
            text(
                """
                insert into app_users (
                    username, first_name, last_name, email, password_hash, account_slug, created_at,
                    secret_question, secret_answer_hash, account_role, active, updated_at,
                    position_title, s_number, street_address, city, state, zip_code
                )
                values (
                    :username, :first_name, :last_name, :email, :password_hash, :account_slug, :created_at,
                    :secret_question, :secret_answer_hash, :account_role, :active, :updated_at,
                    :position_title, :s_number, :street_address, :city, :state, :zip_code
                )
                """
            ),
            {
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "password_hash": hash_password(password),
                "account_slug": account_slug,
                "created_at": now,
                "secret_question": secret_question.strip(),
                "secret_answer_hash": hash_password(normalize_secret_answer(secret_answer)),
                "account_role": account_role,
                "active": 1,
                "updated_at": now,
                "position_title": position_title,
                "s_number": s_number,
                "street_address": street_address,
                "city": city,
                "state": state,
                "zip_code": zip_code,
            },
        )

    db_path = account_db_path(account_slug)
    if not using_hosted_database() and user_count() == 1 and LEGACY_DATABASE_PATH.exists() and not db_path.exists():
        shutil.copy2(LEGACY_DATABASE_PATH, db_path)
    return True, "Account created."


def update_user_profile(user_id, first_name, last_name, position_title, s_number, street_address, city, state, zip_code):
    init_auth_db()
    first_name = str(first_name or "").strip()
    last_name = str(last_name or "").strip()
    position_title = str(position_title or "").strip()
    s_number = normalize_s_number(s_number)
    street_address = str(street_address or "").strip()
    city = str(city or "").strip()
    state = normalize_state(state)
    zip_code = normalize_zip(zip_code)
    required = [
        (first_name, "Please enter your first name."),
        (last_name, "Please enter your last name."),
        (position_title, "Please enter your position / job title."),
        (s_number, "Please enter your S Number."),
        (street_address, "Please enter your street address."),
        (city, "Please enter your city."),
        (zip_code, "Please enter your ZIP code."),
    ]
    for value, message in required:
        if not value:
            return False, message
    if len(state) < 2:
        return False, "Please enter a two-letter state."
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        duplicate = conn.execute(
            text("select id from app_users where upper(coalesce(s_number, '')) = upper(:s_number) and id <> :user_id"),
            {"s_number": s_number, "user_id": int(user_id)},
        ).fetchone()
        if duplicate:
            return False, "That S Number is already used by another account."
        conn.execute(
            text(
                """
            update app_users
            set first_name = :first_name, last_name = :last_name, position_title = :position_title, s_number = :s_number,
                street_address = :street_address, city = :city, state = :state, zip_code = :zip_code, updated_at = :updated_at
            where id = :user_id
            """,
            ),
            {
                "first_name": first_name,
                "last_name": last_name,
                "position_title": position_title,
                "s_number": s_number,
                "street_address": street_address,
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "updated_at": now,
                "user_id": int(user_id),
            },
        )
    return True, "Profile updated."


def authenticate(username, password):
    init_auth_db()
    login = username.strip()
    engine = _engine()
    with engine.connect() as conn:
        user = conn.execute(
            text(
                """
            select *
            from app_users
            where lower(username) = lower(:login)
               or lower(email) = lower(:login)
            """,
            ),
            {"login": login},
        ).fetchone()
    user = _row_dict(user)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    if int(user.get("active", 1)) != 1:
        return None
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(text("update app_users set last_login = :last_login where id = :user_id"), {"last_login": now, "user_id": int(user["id"])})
    user["last_login"] = now
    return user


def list_app_users():
    init_auth_db()
    with _engine().connect() as conn:
        rows = conn.execute(
            text(
                """
            select u.id, u.username, u.first_name, u.last_name, u.email, u.account_slug,
                   coalesce(u.position_title, '') as position_title,
                   coalesce(u.s_number, '') as s_number,
                   coalesce(u.street_address, '') as street_address,
                   coalesce(u.city, '') as city,
                   coalesce(u.state, '') as state,
                   coalesce(u.zip_code, '') as zip_code,
                   u.home_latitude, u.home_longitude,
                   coalesce(u.account_role, 'User') as account_role, coalesce(u.active, 1) as active,
                   u.manager_user_id, u.last_login, u.updated_at,
                   m.email as manager_email, m.first_name || ' ' || m.last_name as manager_name,
                   u.created_at
            from app_users u
            left join app_users m on m.id = u.manager_user_id
            order by u.account_role, u.email
            """
            )
        ).fetchall()
    return _rows_dict(rows)


def get_user_by_id(user_id):
    init_auth_db()
    with _engine().connect() as conn:
        user = conn.execute(text("select * from app_users where id = :user_id"), {"user_id": int(user_id)}).fetchone()
    return _row_dict(user)


def update_user_access(user_id, account_role, manager_user_id=None):
    init_auth_db()
    account_role = account_role if account_role in ("Admin", "Manager", "User") else "User"
    manager_user_id = int(manager_user_id) if manager_user_id else None
    if account_role in ("Admin", "Manager"):
        manager_user_id = None
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("update app_users set account_role = :account_role, manager_user_id = :manager_user_id, updated_at = :updated_at where id = :user_id"),
            {"account_role": account_role, "manager_user_id": manager_user_id, "updated_at": now, "user_id": int(user_id)},
        )


def update_user_status(user_id, active):
    init_auth_db()
    user = get_user_by_id(user_id)
    if user and user.get("email", "").lower() == "daniel.dombrowski@7-11.com" and not active:
        return False, "The owner admin account cannot be disabled."
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("update app_users set active = :active, updated_at = :updated_at where id = :user_id"),
            {"active": 1 if active else 0, "updated_at": now, "user_id": int(user_id)},
        )
    return True, "Account reactivated." if active else "Account disabled."


def claim_user_for_manager(user_id, manager_user_id):
    init_auth_db()
    target = get_user_by_id(user_id)
    manager = get_user_by_id(manager_user_id)
    if not target:
        return False, "User account was not found."
    if not manager:
        return False, "Manager account was not found."
    if int(target.get("id")) == int(manager.get("id")):
        return False, "You cannot claim your own account."
    if int(target.get("active", 1)) != 1:
        return False, "Only active users can be claimed."
    if target.get("account_role") not in ("User", "Admin"):
        return False, "Only User or Admin accounts can be claimed by a manager."
    if manager.get("account_role") not in ("Admin", "Manager"):
        return False, "Only Manager or Admin accounts can claim users."
    existing_manager_id = target.get("manager_user_id")
    if existing_manager_id and int(existing_manager_id) != int(manager_user_id):
        return False, "That user is already assigned to another manager."
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("update app_users set manager_user_id = :manager_user_id, updated_at = :updated_at where id = :user_id"),
            {"manager_user_id": int(manager_user_id), "updated_at": now, "user_id": int(user_id)},
        )
    return True, "User claimed. Their workspace will now appear in your sidebar workspace switcher."


def release_user_from_manager(user_id, manager_user_id, admin_override=False):
    init_auth_db()
    target = get_user_by_id(user_id)
    if not target:
        return False, "User account was not found."
    existing_manager_id = target.get("manager_user_id")
    if not existing_manager_id:
        return False, "That user is not assigned to a manager."
    if not admin_override and int(existing_manager_id) != int(manager_user_id):
        return False, "You can only release users assigned to you."
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("update app_users set manager_user_id = null, updated_at = :updated_at where id = :user_id"),
            {"updated_at": now, "user_id": int(user_id)},
        )
    return True, "User released."


def accessible_accounts_for_current_user():
    init_auth_db()
    current_user_id = st.session_state.get("user_id")
    if not current_user_id:
        return []
    current_role = st.session_state.get("account_role", "User")
    users = list_app_users()
    def descendant_ids(manager_id):
        found = set()
        pending = [int(manager_id)]
        while pending:
            manager_id = pending.pop()
            children = [
                int(user["id"])
                for user in users
                if user.get("manager_user_id") and int(user["manager_user_id"]) == int(manager_id)
            ]
            for child_id in children:
                if child_id not in found:
                    found.add(child_id)
                    pending.append(child_id)
        return found

    if current_role == "Admin":
        allowed = users
    elif current_role == "Manager":
        allowed_ids = {int(current_user_id)} | descendant_ids(current_user_id)
        allowed = [
            user for user in users
            if int(user["id"]) in allowed_ids
        ]
    else:
        allowed = [user for user in users if int(user["id"]) == int(current_user_id)]
    return allowed


def can_access_account_slug(account_slug):
    return any(user["account_slug"] == account_slug for user in accessible_accounts_for_current_user())


def find_user_by_email(email):
    init_auth_db()
    with _engine().connect() as conn:
        user = conn.execute(
            text("select * from app_users where lower(email) = lower(:email)"),
            {"email": email.strip()},
        ).fetchone()
    return _row_dict(user)


def reset_password_with_secret(email, secret_answer, new_password):
    init_auth_db()
    if len(new_password) < 6:
        return False, "Password needs at least 6 characters."
    user = find_user_by_email(email)
    if not user:
        return False, "No account found for that email address."
    if not user.get("secret_answer_hash"):
        return False, "This account does not have a recovery question set."
    if not verify_password(normalize_secret_answer(secret_answer), user["secret_answer_hash"]):
        return False, "Secret answer did not match."
    engine = _engine()
    now = datetime.utcnow() if engine.dialect.name == "postgresql" else datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("update app_users set password_hash = :password_hash, updated_at = :updated_at where id = :user_id"),
            {"password_hash": hash_password(new_password), "updated_at": now, "user_id": int(user["id"])},
        )
    return True, f"Password reset. Sign in with username {user['username']} or email {user['email']}."


def sign_in(user):
    clear_transient_session_state()
    st.session_state["authenticated"] = True
    st.session_state["user_id"] = int(user["id"])
    st.session_state["username"] = user["username"]
    st.session_state["first_name"] = user.get("first_name", "")
    st.session_state["last_name"] = user.get("last_name", "")
    st.session_state["position_title"] = user.get("position_title", "")
    st.session_state["s_number"] = user.get("s_number", "")
    st.session_state["street_address"] = user.get("street_address", "")
    st.session_state["city"] = user.get("city", "")
    st.session_state["state"] = user.get("state", "")
    st.session_state["zip_code"] = user.get("zip_code", "")
    st.session_state["user_email"] = user["email"]
    st.session_state["account_slug"] = user["account_slug"]
    st.session_state["active_account_slug"] = user["account_slug"]
    st.session_state["active_account_label"] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["email"]
    st.session_state["account_role"] = user.get("account_role", "User")
    st.session_state["manager_user_id"] = user.get("manager_user_id")


def sign_out():
    clear_transient_session_state()
    for key in [
        "authenticated",
        "user_id",
        "username",
        "first_name",
        "last_name",
        "position_title",
        "s_number",
        "street_address",
        "city",
        "state",
        "zip_code",
        "user_email",
        "account_slug",
        "active_account_slug",
        "active_account_label",
        "account_role",
        "manager_user_id",
    ]:
        st.session_state.pop(key, None)
