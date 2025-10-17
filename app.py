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

# Banner image shown under the title on the Check-in tab (Google Forms style)
BANNER_URL = "https://raw.githubusercontent.com/kinshuk1456/summit_checkin_app/main/assets/bg.png"

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
