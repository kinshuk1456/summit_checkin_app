# app.py — Summit Check-in & Occupancy (roles, move logic, nearby, kiosk, banner image)

import os
from datetime import datetime
import sqlite3
from typing import Optional, List

import pandas as pd
import streamlit as st

# ---------------------- App config ----------------------
st.set_page_config(page_title="Summit Check-in & Occupancy", page_icon="🧭", layout="wide")

DB_PATH = "checkins.db"
ROOMS_CSV = "rooms.csv"

# Turn on ONLY if you've added gspread/google-auth to requirements.txt and set secrets
USE_SHEETS = False  # set True after secrets + deps are configured

# ---------------------- Roles / modes ----------------------
def get_mode_and_auth():
    qs = st.query_params
    mode = (qs.get("mode") or "checkin").lower()  # 'checkin' | 'dashboard' | 'admin'
    provided_key = qs.get("key") or ""
    admin_key = st.secrets.get("ADMIN_KEY", "")
    is_admin = (mode == "admin" and admin_key and provided_key == admin_key)
    return mode, is_admin

mode, is_admin = get_mode_and_auth()

# Kiosk polish for student view
if mode == "checkin":
    st.markdown("""
        <style>
          #MainMenu, header, footer {visibility: hidden;}
        </style>
    """, unsafe_allow_html=True)

# ---------------------- Google Sheets (optional live sync) ----------------------
def _get_sheet():
    if not USE_SHEETS:
        return None, {}
    import gspread
    from google.oauth2.service_account import Credentials
    sa_info = st.secrets.get("gcp_service_account", None)
    sheet_id = st.secrets.get("SHEET_ID", "")
    if not (sa_info and sheet_id):
        return None, {}
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Checkins")
    except Exception:
        ws = sh.add_worksheet(title="Checkins", rows=1000, cols=10)
        ws.update("A1:F1", [["ts_utc", "name", "email", "attending", "room", "session"]])
    head = ws.row_values(1)
    idx = {name: (i + 1) for i, name in enumerate(head)}
    return ws, idx

def sheet_upsert(email: str, row: list):
    if not USE_SHEETS:
        return
    try:
        ws, idx = _get_sheet()
        if not ws:
            return
        col_email = idx.get("email", 3)
        col_vals = ws.col_values(col_email)
        row_num = None
        for i, v in enumerate(col_vals):
            if i == 0:
                continue  # header
            if v.lower() == email.lower():
                row_num = i + 1
                break
        if row_num:
            ws.update(f"A{row_num}:F{row_num}", [row])
        else:
            ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception:
        st.toast("Sheets sync error (saved locally).", icon="⚠️")

# ---------------------- Database helpers ----------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                attending TEXT NOT NULL CHECK (attending IN ('Yes','No')),
                room TEXT NOT NULL,
                session TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email ON checkins(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_room_session ON checkins(room, session)")
        conn.commit()

def upsert_checkin(name: str, email: str, attending: str, room: str, session: str):
    """
    Keep only ONE active placement per email (latest wins).
    If the student checks into another room or session, they are MOVED there.
    """
    now_utc = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    clean_email = (email or "").strip().lower()
    clean_name = (name or "").strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM checkins WHERE email = ?", (clean_email,))
        conn.execute("""
            INSERT INTO checkins (ts_utc, name, email, attending, room, session)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_utc, clean_name, clean_email, attending, room, session))
        conn.commit()
    sheet_upsert(clean_email, [now_utc, clean_name, clean_email, attending, room, session])

def read_checkins() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("SELECT * FROM checkins", conn)

# ---------------------- Data helpers ----------------------
@st.cache_data
def load_rooms() -> pd.DataFrame:
    if not os.path.exists(ROOMS_CSV):
        st.error(f"Missing {ROOMS_CSV}. Add columns: room_code, session, max_capacity[, nearby]")
        return pd.DataFrame(columns=["room_code", "session", "max_capacity", "nearby"])
    df = pd.read_csv(ROOMS_CSV, dtype={"room_code": str, "session": str, "max_capacity": int})
    if "nearby" not in df.columns:
        df["nearby"] = ""  # optional, pipe-separated alternates
    return df

def occupancy_counts(df_checkins: pd.DataFrame, rooms_df: pd.DataFrame,
                     session_filter: Optional[str] = None) -> pd.DataFrame:
    base = rooms_df.copy()
    base["current"] = 0
    base["status"] = "OPEN"
    if df_checkins is None or df_checkins.empty:
        return _with_filter_and_sort(base, session_filter)

    data = df_checkins.copy()
    data = data[data["attending"] == "Yes"]  # count only Yes
    grouped = data.groupby(["room", "session"]).size().reset_index(name="current")
    merged = rooms_df.merge(grouped, left_on=["room_code", "session"], right_on=["room", "session"], how="left")
    merged["current"] = merged["current"].fillna(0).astype(int)
    merged["status"] = merged.apply(
        lambda r: "FULL" if r["current"] >= r["max_capacity"]
        else ("ALMOST FULL" if r["current"] >= 0.9 * r["max_capacity"] else "OPEN"), axis=1
    )
    merged = merged.drop(columns=["room"])
    return _with_filter_and_sort(merged, session_filter)

def _with_filter_and_sort(df: pd.DataFrame, session_filter: Optional[str]) -> pd.DataFrame:
    out = df.copy()
    if session_filter:
        out = out[out["session"] == session_filter]
    return out.sort_values(["session", "room_code"])

def nearby_list(nearby_str: str) -> List[str]:
    return [x.strip() for x in str(nearby_str).split("|") if str(x).strip()]

# ---------------------- Start ----------------------
init_db()
rooms_df = load_rooms()

st.title("Emerging Careers Summit — Room Check-in & Occupancy")
st.caption("Scan → Name, Email, Yes/No. Room & Session from QR. Latest check-in moves the student to that room/session.")

# Build tabs based on role (label-based, no numeric indexes)
TAB_ORDER = {
    "checkin":  ["📝 Check-in"],
    "dashboard": ["📝 Check-in", "📊 Dashboard", "🔗 QR Links"],
    "admin":    ["📝 Check-in", "📊 Dashboard", "🔗 QR Links", "⚙️ Admin"] if is_admin else ["📝 Check-in"]
}
allowed_labels = TAB_ORDER.get(mode, ["📝 Check-in"])
tabs = st.tabs(allowed_labels)
tab = {label: tabs[i] for i, label in enumerate(allowed_labels)}

# ---------------------- 📝 Check-in ----------------------
with tab["📝 Check-in"]:
    st.subheader("Attendee Check-in")

    # query params (string or list)
    q = st.query_params
    def _qp(key, default=""):
        v = q.get(key, default)
        return (v[0] if isinstance(v, list) and v else v) or default

    room = _qp("room")
    session = _qp("session")

    # validate pair
    valid_pair = False
    if not rooms_df.empty and room and session:
        valid_pair = ((rooms_df["room_code"] == str(room)) & (rooms_df["session"] == str(session))).any()

    # full-room nearby suggestions
    dfc = read_checkins()
    occ_df = occupancy_counts(dfc, rooms_df, session_filter=session) if session else None
    this_row = None
    if occ_df is not None and not occ_df.empty:
        m = occ_df[occ_df["room_code"] == room]
        this_row = m.iloc[0] if not m.empty else None
        if this_row is not None and this_row["status"] == "FULL":
            nby = nearby_list(this_row["nearby"])
            msg = "This room is FULL."
            if nby:
                msg += " Nearby rooms: " + ", ".join(nby)
            st.error(msg)

    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name")
        email = st.text_input("Email", placeholder="you@ucr.edu")
    with col2:
        st.text_input("Room (auto)", value=room, disabled=True)
        st.text_input("Session (auto)", value=session, disabled=True)
        attending = st.radio("Are you attending this session?", ["Yes", "No"], index=0, horizontal=True)

    # Email policy
    def valid_email(e: str) -> bool:
        e = (e or "").strip().lower()
        return e.endswith("@ucr.edu") and " " not in e and "@" in e and len(e) >= 10

    if not valid_pair:
        st.warning("This check-in needs a valid room & session in the URL. Please scan the correct room QR.")
    else:
        if st.button("Submit Check-in", type="primary"):
            if not name.strip() or not email.strip():
                st.error("Please provide your Name and Email.")
            elif not valid_email(email):
                st.error("Please use your UCR email address (must end with @ucr.edu).")
            elif this_row is not None and this_row["status"] == "FULL" and attending == "Yes":
                st.error("This room is already FULL. Please choose a nearby room.")
            else:
                upsert_checkin(name, email, attending, room, session)
                st.success("You're checked in. Thank you! (If you checked in elsewhere, you’ve been moved here.)")

# ---------------------- 📊 Dashboard ----------------------
if "📊 Dashboard" in tab:
    with tab["📊 Dashboard"]:
        st.subheader("Floor Host Dashboard")
        if rooms_df.empty:
            st.info("Add rooms in rooms.csv to see the dashboard.")
        else:
            session_choices = ["All"] + sorted(rooms_df["session"].unique().tolist())
            pick = st.selectbox("Filter by session", session_choices, index=0)

            dfc = read_checkins()
            occ = occupancy_counts(dfc, rooms_df, None if pick == "All" else pick)

            # quick search
            search = st.text_input("Search room (optional)", "")
            if search:
                occ = occ[occ["room_code"].str.contains(search, case=False, na=False)]

            show = occ[["room_code","session","current","max_capacity","status","nearby"]].rename(columns={
                "room_code":"Room","session":"Session","current":"Count","max_capacity":"Max","status":"Status","nearby":"Nearby"
            })
            st.dataframe(show, use_container_width=True, hide_index=True)

            # export CSV
            if dfc is not None and not dfc.empty:
                st.download_button(
                    "Download raw check-ins (CSV)",
                    dfc.to_csv(index=False),
                    file_name="checkins_raw.csv",
                    mime="text/csv",
                )

# ---------------------- 🔗 QR Links ----------------------
if "🔗 QR Links" in tab:
    with tab["🔗 QR Links"]:
        st.subheader("QR Links per Room × Session")
        base_hint = st.text_input(
            "Base app URL",
            value="https://summitcheckin.streamlit.app",  # set to your deployed URL
            help="Set this to your deployed URL or custom domain."
        )
        if rooms_df.empty:
            st.info("Add rooms in rooms.csv to generate links.")
        else:
            rows = []
            for _, r in rooms_df.iterrows():
                student_link = f"{base_hint.strip().rstrip('/')}/?room={r['room_code']}&session={r['session']}&mode=checkin"
                rows.append((r["room_code"], r["session"], r["max_capacity"], student_link))
            links_df = pd.DataFrame(rows, columns=["Room", "Session", "Max", "Check-in URL (student)"])
            st.dataframe(links_df, use_container_width=True, hide_index=True)
            st.caption("Make QR codes from these student links. Hosts bookmark ?mode=dashboard. Admin uses ?mode=admin&key=YOUR_KEY.")

# ---------------------- ⚙️ Admin ----------------------
if "⚙️ Admin" in tab:
    with tab["⚙️ Admin"]:
        if not is_admin:
            st.error("Unauthorized (ADMIN mode requires a valid key).")
        else:
            st.subheader("Admin")
            st.write("• Edit `rooms.csv` to change rooms/sessions/capacities (optional `nearby` column with pipe-separated rooms).")
            st.write("• Clear the local database if you need a hard reset.")
            if st.button("Clear database (delete all check-ins)"):
                try:
                    os.remove(DB_PATH)
                except FileNotFoundError:
                    pass
                init_db()
                st.success("Database cleared.")
