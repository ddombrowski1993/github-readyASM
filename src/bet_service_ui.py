"""Shared store-search and map-click entry controls for BET services."""
import logging

import streamlit as st

from src.bet_services import SERVICES, service_status_rows, working_today
from src.bet_service_store import current_service_actor, save_store_services, correct_store_service

LOGGER = logging.getLogger(__name__)
BET_ROLES = {"brand", "brand enhancement", "bet", "afm", "brand technician", "brand enhancement technician"}


def store_description(row):
    st.subheader(f"Store {row['store_number']}")
    st.write(row.get("address") or "Address unavailable")
    st.write(", ".join(str(row.get(k) or "") for k in ("city", "state")).strip(", ") or "City / state unavailable")
    st.write(f"Assigned BET Technician: **{row['bet_employee_name'] or 'Team-based assignment'}**")
    st.write(f"Assigned BET Team / Area: **{row['bet_team_name'] or 'Unassigned'}**")
    for service, label in SERVICES.items():
        if row[service + "_complete"]:
            st.write(f"{label}: **Completed {row[service + '_date']:%m/%d/%Y}** — credited to {row[service + '_credited_to']}")
        else:
            st.write(f"{label}: **{'Not Completed' if row[service + '_applicable'] else 'Not Applicable'}**")


def render_completion_form(row, data, key_prefix="search", source="Store search"):
    """Map and search use these identical controls and the same transactional save API."""
    actor = current_service_actor()
    year = data["year"]
    store_description(row)
    if not row["bet_assigned"]:
        st.info("Assign this store to a BET team or technician in Areas and Maps before logging completion.")
        return
    st.write(f"Credit new completion to: **{row['bet_employee_name'] or row['bet_team_name']}**")
    version = "_".join(str(row[s + "_record_id"] or 0) for s in SERVICES)
    scope = st.session_state.get("effective_account_slug") or st.session_state.get("active_account_slug") or st.session_state.get("account_slug") or "workspace"
    key = f"bet_{scope}_{key_prefix}_{year}_{row['id']}_{version}"
    with st.form(key):
        selected = []
        for service, label in SERVICES.items():
            checked = st.checkbox(label + " Completed", value=row[service + "_complete"],
                disabled=row[service + "_complete"] or not row[service + "_applicable"], key=key + service)
            if checked:
                selected.append(service)
        st.caption("Completed services stay checked. Use Undo / Correct Completion below to fix an entry.")
        default_date = working_today()
        if default_date.year != year:
            st.caption(f"Choose the actual completion date within service year {year}.")
        completed_date = st.date_input("Completed Date", value=default_date, key=key + "date")
        with st.expander("Optional details / attribution"):
            work_order = st.text_input("Work Order Number (optional)", key=key + "wo",
                help="Applies only to newly checked services. Save each service separately if they have different work orders.")
            notes = st.text_area("Notes (optional)", key=key + "notes")
            override = False
            override_credit = None
            reason = ""
            if actor.role in {"Admin", "Manager"}:
                override = st.checkbox("Override credited technician / team", key=key + "override")
                # Kept visible in the form because checkboxes only submit on Save.
                people = {None: "Use current technician"}
                people.update({e["id"]: e["name"] for e in data["employees"]
                               if e["active"] and str(e["role"] or "").strip().lower() in BET_ROLES})
                teams = {None: "Use current team"}
                teams.update({t["id"]: t["name"] for t in data["teams"] if t["active"] and t["type"] == "Brand Enhancement"})
                person = st.selectbox("Credit technician", list(people), format_func=people.get, key=key + "person")
                team = st.selectbox("Credit team / area", list(teams), format_func=teams.get, key=key + "team")
                reason = st.text_input("Reason for attribution override", key=key + "reason")
                if override:
                    override_credit = (person if person is not None else row["bet_employee_id"],
                                       team if team is not None else row["bet_team_id"])
        submitted = st.form_submit_button("Save Service Completion" if source == "Store search" else "Save Service Changes", type="primary")
    if submitted:
        try:
            if not any(s in selected and not row[s + "_complete"] for s in SERVICES):
                st.info("No new services selected. Existing completion records were kept.")
            else:
                created = save_store_services(store_id=row["id"], service_year=year, services=selected,
                    completed_date=completed_date, source=source, notes=notes, work_order_number=work_order,
                    credit_override=override_credit, override_reason=reason)
                st.session_state["bet_service_notice"] = f"Store {row['store_number']}: saved {len(created)} new service completion(s)."
                st.rerun()
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
        except Exception:
            LOGGER.exception("BET completion save failed")
            st.error("Could not save completion. Please retry; repeated saves do not duplicate completions.")
    records = [r for r in data["completions"] if r["store_id"] == row["id"] and r["active"]
               and (actor.role in {"Admin", "Manager"} or r["entered_by_user_id"] == actor.user_id)]
    if records:
        with st.expander("Undo / Correct Completion"):
            st.caption("Voiding keeps the original entry, author, credit, and correction reason in History. To change date or credit, void and log the corrected entry.")
            lookup = {r["id"]: f"{SERVICES[r['service_type']]} — {r['completed_date']:%m/%d/%Y}" for r in records}
            with st.form(key + "correct"):
                rid = st.selectbox("Completion to correct", list(lookup), format_func=lookup.get, key=key + "rid")
                correction_reason = st.text_input("Correction reason", key=key + "correction_reason")
                confirmed = st.checkbox("I confirm this completion should be voided", key=key + "confirm")
                correct = st.form_submit_button("Void Completion")
            if correct:
                try:
                    correct_store_service(rid, correction_reason, confirmed)
                    st.session_state["bet_service_notice"] = "Completion voided. The original entry remains in History."
                    st.rerun()
                except (ValueError, PermissionError) as exc:
                    st.error(str(exc))
                except Exception:
                    LOGGER.exception("BET completion correction failed")
                    st.error("Could not correct this entry. Please retry.")


def render_store_search(data):
    st.subheader("Find Store")
    query = st.text_input("Store Number", key="bet_store_search", placeholder="45555").strip()
    if not query:
        st.info("Search a store number to log Power Washing and/or Lot Striping.")
        return
    rows = service_status_rows(data)
    exact = [r for r in rows if str(r["store_number"]).strip().casefold() == query.casefold()]
    matches = exact or [r for r in rows if query.casefold() in str(r["store_number"]).casefold()]
    if not matches:
        st.warning("No active master store matches that number in this workspace.")
        return
    lookup = {r["id"]: r for r in matches}
    sid = st.selectbox("Matching store", list(lookup), format_func=lambda i: f"{lookup[i]['store_number']} — {lookup[i].get('city') or ''}")
    render_completion_form(lookup[sid], data)
