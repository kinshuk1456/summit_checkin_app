import streamlit as st
import pandas as pd
import sqlite3
from sqlite3 import IntegrityError
from datetime import datetime
import os

DB_PATH = 'checkins.db'
ROOMS_CSV = 'rooms.csv'

st.set_page_config(page_title="Summit Check-in & Occupancy", page_icon="🧭", layout="wide")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
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
        ''')
        conn.commit()

def upsert_checkin(name, email, attending, room, session):
    now_utc = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute('''
                INSERT INTO checkins (ts_utc, name, email, attending, room, session)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (now_utc, name.strip(), email.strip().lower(), attending, room, session))
        except IntegrityError:
            conn.execute('''
                UPDATE checkins
                SET ts_utc = ?, name = ?, attending = ?
                WHERE email = ? AND room = ? AND session = ?
            ''', (now_utc, name.strip(), attending, email.strip().lower(), room, session))
        conn.commit()

def read_checkins():
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query('SELECT * FROM checkins', conn)
    return df

@st.cache_data
def load_rooms():
    if not os.path.exists(ROOMS_CSV):
        st.error(f"Missing {ROOMS_CSV}. Please add your rooms list.")
        return pd.DataFrame(columns=['room_code','session','max_capacity'])
    df = pd.read_csv(ROOMS_CSV, dtype={'room_code':str,'session':str,'max_capacity':int})
    return df

def occupancy_counts(df_checkins, rooms_df, session_filter=None, only_yes=True):
    if df_checkins is None or df_checkins.empty:
        base = rooms_df.copy()
        base['current'] = 0
        base['status'] = 'OPEN'
        return base

    data = df_checkins.copy()
    if only_yes:
        data = data[data['attending'] == 'Yes']

    data = data.sort_values('ts_utc').drop_duplicates(['email','room','session'], keep='last')
    grouped = data.groupby(['room','session']).size().reset_index(name='current')
    merged = rooms_df.merge(grouped, left_on=['room_code','session'], right_on=['room','session'], how='left')
    merged['current'] = merged['current'].fillna(0).astype(int)
    merged['status'] = merged.apply(lambda r: 'FULL' if r['current'] >= r['max_capacity']
                                    else ('ALMOST FULL' if r['current'] >= 0.9*r['max_capacity']
                                          else 'OPEN'), axis=1)
    merged = merged.drop(columns=['room'])
    if session_filter:
        merged = merged[merged['session'] == session_filter]
    return merged.sort_values(['session','room_code'])

init_db()
rooms_df = load_rooms()

st.title("Emerging Careers Summit — Room Check-in & Occupancy")

tabs = st.tabs(["📝 Check-in", "📊 Dashboard", "🔗 QR Links", "⚙️ Admin"])

with tabs[0]:
    st.subheader("Attendee Check-in")
    qs = st.query_params
    room = qs.get('room', '')
    session = qs.get('session', '')

    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name", key="name")
        email = st.text_input("Email", key="email", placeholder="you@ucr.edu")
    with col2:
        st.text_input("Room (auto)", value=room, disabled=True)
        st.text_input("Session (auto)", value=session, disabled=True)
        attending = st.radio("Are you attending this session?", ["Yes", "No"], index=0, horizontal=True)

    valid_pair = False
    if not rooms_df.empty and room and session:
        valid_pair = ((rooms_df['room_code'] == str(room)) & (rooms_df['session'] == str(session))).any()

    if (room == "" or session == "") or not valid_pair:
        st.warning("This check-in needs a valid room & session in the URL (scan the correct room QR).")
    else:
        if st.button("Submit Check-in", type="primary"):
            if name.strip() == "" or email.strip() == "":
                st.error("Please provide your Name and Email.")
            else:
                upsert_checkin(name, email, attending, room, session)
                st.success("You're checked in. Thank you!")

with tabs[1]:
    st.subheader("Floor Host Dashboard")
    if rooms_df.empty:
        st.info("Add rooms in rooms.csv to see the dashboard.")
    else:
        session_choices = ["All"] + sorted(rooms_df['session'].unique().tolist())
        pick = st.selectbox("Filter by session", session_choices, index=0)
        dfc = read_checkins()
        occ = occupancy_counts(dfc, rooms_df, None if pick=="All" else pick)
        st.dataframe(
            occ[['room_code','session','current','max_capacity','status']].rename(columns={
                'room_code':'Room','session':'Session','current':'Count','max_capacity':'Max','status':'Status'
            }),
            use_container_width=True, hide_index=True
        )

with tabs[2]:
    st.subheader("QR Links per Room × Session")
    base_hint = st.text_input("Base app URL", value="http://localhost:8501")
    if not rooms_df.empty:
        rows = []
        for _, r in rooms_df.iterrows():
            link = f"{base_hint.strip().rstrip('/')}/?room={r['room_code']}&session={r['session']}"
            rows.append((r['room_code'], r['session'], r['max_capacity'], link))
        st.dataframe(pd.DataFrame(rows, columns=["Room","Session","Max","Check-in URL"]), use_container_width=True, hide_index=True)

with tabs[3]:
    st.subheader("Admin")
    st.write("• Edit rooms.csv to change rooms/sessions/capacities.\n• Delete checkins.db to reset data.")
    if st.button("Clear database (delete all check-ins)"):
        try:
            os.remove(DB_PATH)
            init_db()
            st.success("Database cleared.")
        except FileNotFoundError:
            init_db()
            st.success("Database initialized (no prior data found).")
