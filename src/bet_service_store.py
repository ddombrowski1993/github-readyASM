"""Workspace-aware adapter for BET services. Production always uses the app's PostgreSQL engine."""
from contextlib import contextmanager

import streamlit as st
from sqlalchemy.orm import Session

from src.auth import authenticated_user_id, current_account_schema, using_hosted_database
from src.database import get_database_url, get_engine, _apply_workspace_search_path
from src.bet_schema import require_bet_schema
from src.bet_services import ServiceActor, read_bet_service_data, save_bet_service_completion, void_bet_service_completion


def current_service_actor():
    user_id = authenticated_user_id()
    if not st.session_state.get("authenticated") or not user_id:
        raise PermissionError("Sign in to use BET Service Tracker.")
    name = " ".join(str(st.session_state.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return ServiceActor(int(user_id), name or st.session_state.get("username") or "Signed-in user",
                        st.session_state.get("account_role", "User"))


@contextmanager
def bet_service_session():
    current_service_actor()
    schema = current_account_schema()
    if using_hosted_database() and not schema:
        raise PermissionError("Select a specific workspace before using BET Service Tracker.")
    if st.session_state.get("manager_rollup_active"):
        raise PermissionError("Select one workspace instead of All Managed Users to use BET Service Tracker.")
    engine = get_engine(get_database_url(), schema=schema)
    with Session(engine) as session:
        with session.begin():
            connection = session.connection()
            _apply_workspace_search_path(connection, schema)
            require_bet_schema(connection, schema)
            yield session


def load_bet_services(service_year):
    with bet_service_session() as session:
        return read_bet_service_data(session, service_year)


def save_store_services(**kwargs):
    with bet_service_session() as session:
        result = save_bet_service_completion(session, actor=current_service_actor(), **kwargs)
    st.cache_data.clear()
    return result


def correct_store_service(completion_id, reason, confirmed):
    with bet_service_session() as session:
        changed = void_bet_service_completion(session, completion_id=completion_id,
            actor=current_service_actor(), reason=reason, confirmed=confirmed)
    st.cache_data.clear()
    return changed
