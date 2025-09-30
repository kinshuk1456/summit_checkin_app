# app.py — Summit Check-in & Occupancy (stable tabs version)

import os
from datetime import datetime
import sqlite3
from sqlite3 import IntegrityError

import pandas as pd
import streamlit as st

# ---------------------- App config ----------------------
st.set_page_config(
    page_title="Summit Check-in & Occupancy",
    page_icon="🧭",
    layout="wide",
)

DB_PATH = "checkins.db"
ROOMS_CSV = "rooms.csv"

# ---------------------- Database helpers ----------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                attending TEXT NOT NULL CHECK (attending IN ('Yes','No')),
                room TEXT NOT NULL,
                session TEXT NOT NULL,
                UNIQUE(email, room, session)
            )
            """
        )
        conn.commit()

def upsert_checkin(name: str, email: str, attending: str, room: str, session: str):
    """Insert or update a unique (email, room, session)."""
    now_utc = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    clean_email = email.strip().lower()
    clean_name = name.strip()
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                """
                INSERT INTO checkins (ts_utc, name, email, attending, room, session)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_utc, clean_name, clean_email, attending, room, session),
            )
        except IntegrityError:
            conn.execute(
                """
                UPDATE checkins
                SET ts_utc = ?, name = ?, attending = ?
                WHERE email = ? AND room = ? AND session = ?
                """,
                (now_utc, clean_name, attending, clean_email, room, session),
            )
        conn.commit()

def read_checkins() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM checkins", conn)
    return df

# ---------------------- Data helpers ----------------------
@st.cache_data
def load_rooms() -> pd.DataFrame:
    if not os.path.exists(ROOMS_CSV):
        st.error(f"Missing {ROOMS_CSV}. Add it to the repo/root with room_code,session,max_capacity.")
        return pd.DataFrame(columns=["room_code", "session", "max_capacity"])
    df = pd.read_csv(
        ROOMS_CSV,
        dtype={"room_code": str, "session": str, "max_capacity": int},
    )
    return df

def occupancy_counts(df_checkins: pd.DataFrame,
                     rooms_df: pd.DataFrame,
                     session_filter: str | None = None,
                     only_yes: bool = True) -> pd.DataFrame:
    """Return rooms with current counts and status."""
    base = rooms_df.copy()
    base["current"] = 0
    base["status"] = "OPEN"
    if df_checkins is None or df_checkins.empty:
        return _with_filter_and_sort(base, session_filter)

    data = df_checkins.copy()
    if only_yes:
        data = data[data["attending"] == "Yes"]

    # dedupe by (email,room,session) keep latest
    if not data.empty:
        data = data.sort_values("ts_utc").drop_duplicates(
            ["email", "room", "session"], keep="last"
        )

    grouped = (
        data.groupby(["room", "session"])
        .size()
        .reset_index(name="current")
    )

    merged = rooms_df.merge(
        grouped, left_on=["room_code", "session"], right_on=["room", "session"], how="left"
    )
    merged["current"] = merged["current"].fillna(0).astype(int)
    merged["status"] = merged.apply(
        lambda r: "FULL"
        if r["current"] >= r["max_capacity"]
        else ("ALMOST FULL" if r["current"] >= 0.9 * r["max_capacity"] else "OPEN"),
        axis=1,
    )
    merged = merged.drop(columns=["room"])
    return _with_filter_and_sort(merged, session_filter)

def _with_filter_and_sort(df: pd.DataFrame, session_filter: str | None) -> pd.DataFrame:
    out = df.copy()
    if session_filter:
        out = out[out["session"] == session_filter]
    return out.sort_values(["session", "room_code"])

# ---------------------- Start ----------------------
init_db()
rooms_df = load_rooms()

st.title("Emerging Careers Summit — Room Check-in & Occupancy")
st.caption("Scan → Name, Email, Yes/No. Room & Session come from the QR link. Dashboard shows Count vs Max.")

# Create tabs ONCE and refer to them by label (no numeric indices)
TAB_ORDER = ["📝 Check-in", "📊 Dashboard", "🔗 QR Links", "⚙️ Admin"]
tabs = st.tabs(TAB_ORDER)
tab = {name: tabs[i] for i, name in enumerate(TAB_ORDER)}

# ---------------------- 📝 Check-in ----------------------
with tab["📝 Check-in"]:
    st.subheader("Attendee Check-in")

    # Robustly read query params for both old/new Streamlit
    q = st.query_params
    def _qp(key, default=""):
        val = q.get(key, default)
        # if someone passes list-type, take the first
        if isinstance(val, (list, tuple)):
            return val[0] if val else default
        return val

    room = _qp("room", "")
    session = _qp("session", "")

    # Validate the pair exists in rooms.csv
    valid_pair = False
    if not rooms_df.empty and room and session:
        valid_pair = ((rooms_df["room_code"] == str(room)) & (rooms_df["session"] == str(session))).any()

    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name")
        email = st.text_input("Email", placeholder="you@ucr.edu")
    with col2:
        st.text_input("Room (auto)", value=room, disabled=True)
        st.text_input("Session (auto)", value=session, disabled=True)
        attending = st.radio("Are you attending this session?", ["Yes", "No"], index=0, horizontal=True)

    if not valid_pair:
        st.warning("This check-in needs a valid **room** and **session** in the URL. Please scan the correct room QR.")
    else:
        if st.button("Submit Check-in", type="primary"):
            if not name.strip() or not email.strip():
                st.error("Please provide your Name and Email.")
            else:
                upsert_checkin(name, email, attending, room, session)
                st.success("You're checked in. Thank you!")

# ---------------------- 📊 Dashboard ----------------------
with tab["📊 Dashboard"]:
    st.subheader("Floor Host Dashboard")
    if rooms_df.empty:
        st.info("Add rooms in rooms.csv to see the dashboard.")
    else:
        session_choices = ["All"] + sorted(rooms_df["session"].unique().tolist())
        pick = st.selectbox("Filter by session", session_choices, index=0)

        dfc = read_checkins()
        occ = occupancy_counts(dfc, rooms_df, None if pick == "All" else pick)

        # Quick search
        search = st.text_input("Search room (optional)", "")
        if search:
            occ = occ[occ["room_code"].str.contains(search, case=False, na=False)]

        st.dataframe(
            occ[["room_code", "session", "current", "max_capacity", "status"]].rename(
                columns={
                    "room_code": "Room",
                    "session": "Session",
                    "current": "Count",
                    "max_capacity": "Max",
                    "status": "Status",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

        # Export CSV
        if dfc is not None and not dfc.empty:
            st.download_button(
                "Download raw check-ins (CSV)",
                dfc.to_csv(index=False),
                file_name="checkins_raw.csv",
                mime="text/csv",
            )

# ---------------------- 🔗 QR Links ----------------------
with tab["🔗 QR Links"]:
    st.subheader("QR Links per Room × Session")
    base_hint = st.text_input(
        "Base app URL",
        value="http://localhost:8501",
        help="Set this to your deployed URL (e.g., https://your-app.streamlit.app or your custom domain).",
    )

    if rooms_df.empty:
        st.info("Add rooms in rooms.csv to generate links.")
    else:
        rows = []
        for _, r in rooms_df.iterrows():
            link = f"{base_hint.strip().rstrip('/')}/?room={r['room_code']}&session={r['session']}"
            rows.append((r["room_code"], r["session"], r["max_capacity"], link))
        links_df = pd.DataFrame(rows, columns=["Room", "Session", "Max", "Check-in URL"])
        st.dataframe(links_df, use_container_width=True, hide_index=True)
        st.caption("Copy these URLs and turn them into QR codes (e.g., with qrserver.com or in Google Sheets).")

# ---------------------- ⚙️ Admin ----------------------
with tab["⚙️ Admin"]:
    st.subheader("Admin")
    st.write("• Edit `rooms.csv` to change rooms/sessions/capacities.\n• Delete `checkins.db` to reset data.")
    if st.button("Clear database (delete all check-ins)"):
        try:
            os.remove(DB_PATH)
        except FileNotFoundError:
            pass
        init_db()
        st.success("Database cleared.")
