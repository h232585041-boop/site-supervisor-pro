# -*- coding: utf-8 -*-
"""
工地監工 Pro — 在地化工地缺失管理工具（雲端版）
=====================================
架構：
  - 案場（專案）先建立一次，之後的每筆缺失紀錄都歸在案場底下
  - 文字紀錄（日期、工種、廠商、狀態、備註）存 Supabase Postgres
  - 照片（原圖＋標註圖）存使用者自己的 Google Drive（OAuth 個人帳戶授權，見 gdrive_oauth_setup.py）
  - 部署在 Streamlit Community Cloud，手機用行動網路隨時可連，不依賴電腦開機

功能：
  1. 案場列表／建立（先選定或新增案場，才能開始記錄）
  2. 現場拍照 + 畫布標註（畫圈 / 線條 / 方框 / 自由畫）
  3. 側邊欄多重篩選 + 網格相冊管理視圖
  4. fpdf2 一鍵匯出專業 PDF 報告（自動嵌入中文字型），同時上傳一份到 Google Drive
     並提供瀏覽器下載

執行（本機測試）：streamlit run app.py
必要設定：.streamlit/secrets.toml（見 secrets.toml.example 與部署說明文件）
"""

import datetime
import io
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from fpdf import FPDF
from streamlit_drawable_canvas import st_canvas

from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as GoogleUserCredentials
from supabase import Client, ClientOptions, create_client

DRIVE_TIMEOUT = (10, 30)  # (連線逾時, 讀取逾時) 秒

# ----------------------------------------------------------------------
# 常數
# ----------------------------------------------------------------------
STATUS_OPTIONS = ["待處理", "修繕中", "已驗收"]
STATUS_DOT_COLOR = {
    "待處理": "#C6FF00",  # Neon Lime
    "修繕中": "#7E57C2",  # Digital Violet
    "已驗收": "#25262B",  # Deep Charcoal
}
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def status_dot_html(status: str) -> str:
    color = STATUS_DOT_COLOR.get(status, "#999999")
    if status == "待處理":
        # 螢光萊姆綠在淺色底上單獨一個小點不夠顯眼，改成深碳灰底、萊姆綠細邊的
        # 小標籤，兩色對比更強、也更符合「待處理」該被優先注意到的語意。
        return (
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'background:#25262B;color:{color};border:1px solid {color};'
            f'border-radius:999px;padding:1px 10px;font-weight:600;'
            f'font-size:0.85rem;">{status}</span>'
        )
    return (
        '<span style="display:inline-flex;align-items:center;gap:6px;">'
        f'<span style="width:8px;height:8px;border-radius:50%;'
        f'background:{color};display:inline-block;"></span>{status}</span>'
    )


# ----------------------------------------------------------------------
# 設定檢查
# ----------------------------------------------------------------------
def check_secrets() -> list[str]:
    """回傳缺少的設定項目清單；空清單代表設定齊全。"""
    missing = []
    if "supabase" not in st.secrets or "url" not in st.secrets.get("supabase", {}) \
            or "service_role_key" not in st.secrets.get("supabase", {}):
        missing.append("supabase.url / supabase.service_role_key")
    gdrive = st.secrets.get("gdrive", {})
    for key in ("root_folder_id", "client_id", "client_secret", "refresh_token"):
        if key not in gdrive:
            missing.append(f"gdrive.{key}")
    return missing


# ----------------------------------------------------------------------
# Supabase 用戶端
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    # postgrest-py 內部固定用 HTTP/2，在部分網路環境下會偶發
    # httpx.ReadError: [Errno 35] Resource temporarily unavailable，改用 HTTP/1.1 較穩定。
    return create_client(
        st.secrets["supabase"]["url"],
        st.secrets["supabase"]["service_role_key"],
        options=ClientOptions(httpx_client=httpx.Client(http2=False, timeout=30.0)),
    )


# ----------------------------------------------------------------------
# Google Drive 用戶端與工具函式
# ----------------------------------------------------------------------
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


def get_drive_credentials() -> GoogleUserCredentials:
    """回傳這個瀏覽器 session 專用、可重複使用的憑證物件。

    存在 st.session_state（每個使用者各自獨立一份，不會跨 session/執行緒共用），
    避免每次操作都重新交換 access token（省一趟網路來回），同時不會重蹈
    先前用 @st.cache_resource 全域共用連線物件、導致偶發卡死的覆轍。"""
    if "gdrive_creds" not in st.session_state:
        gdrive = st.secrets["gdrive"]
        st.session_state["gdrive_creds"] = GoogleUserCredentials(
            token=None,
            refresh_token=gdrive["refresh_token"],
            client_id=gdrive["client_id"],
            client_secret=gdrive["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=DRIVE_SCOPES,
        )
    return st.session_state["gdrive_creds"]


def get_drive_service() -> AuthorizedSession:
    """用你自己 Google 帳號的 OAuth 授權連線（見 gdrive_oauth_setup.py）。
    改用個人帳戶而非服務帳戶，是因為服務帳戶本身沒有儲存空間配額，
    就算資料夾分享給它也一樣無法上傳檔案（storageQuotaExceeded）。

    注意：這裡直接用 requests 呼叫 Drive REST API，不用 googleapiclient 預設的
    httplib2 傳輸層——httplib2 已經很久沒更新，在部分網路環境下 connect() 會
    直接卡死、完全不理會逾時設定；requests/urllib3 對逾時的處理穩定得多。"""
    return AuthorizedSession(get_drive_credentials())


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def drive_find_or_create_folder(session: AuthorizedSession, name: str, parent_id: str) -> str:
    q = (
        f"name='{_escape(name)}' and '{parent_id}' in parents "
        f"and mimeType='{FOLDER_MIME}' and trashed=false"
    )
    res = session.get(
        f"{DRIVE_API}/files", params={"q": q, "fields": "files(id,name)", "pageSize": 1},
        timeout=DRIVE_TIMEOUT,
    )
    res.raise_for_status()
    files = res.json().get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    res = session.post(
        f"{DRIVE_API}/files", params={"fields": "id"}, json=meta, timeout=DRIVE_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()["id"]


def ensure_site_date_folder(session: AuthorizedSession, site_name: str, d: datetime.date) -> str:
    """{根資料夾}/{案場}/{日期}/ 巢狀資料夾，結果快取在 session_state 減少 API 呼叫。"""
    cache_key = f"folder::{site_name}::{d.isoformat()}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    root_id = st.secrets["gdrive"]["root_folder_id"]
    site_folder_id = drive_find_or_create_folder(session, sanitize(site_name), root_id)
    date_id = drive_find_or_create_folder(session, d.strftime("%Y-%m-%d"), site_folder_id)
    st.session_state[cache_key] = date_id
    return date_id


def drive_list_filenames(session: AuthorizedSession, folder_id: str) -> set[str]:
    """列出資料夾裡目前的檔名，只查一次，供同一批次要存的多個檔案在本機算不重複檔名，
    不用每存一個檔案就重新查一次 Drive（一次要存好幾張照片時，這樣快很多）。"""
    q = f"'{folder_id}' in parents and trashed=false"
    res = session.get(
        f"{DRIVE_API}/files", params={"q": q, "fields": "files(name)", "pageSize": 1000},
        timeout=DRIVE_TIMEOUT,
    )
    res.raise_for_status()
    return {f["name"] for f in res.json().get("files", [])}


def unique_name(existing: set[str], stem: str, suffix: str = ".jpg") -> str:
    """在（可變的）existing 集合裡算出不重複的檔名，並把算出來的名字加進 existing，
    這樣同一批次接下來要取名時才不會撞名；純本機運算，不呼叫 Drive API。"""
    name = f"{stem}{suffix}"
    i = 2
    while name in existing:
        name = f"{stem}_{i}{suffix}"
        i += 1
    existing.add(name)
    return name


def drive_unique_filename(session: AuthorizedSession, folder_id: str, stem: str, suffix: str = ".jpg") -> str:
    return unique_name(drive_list_filenames(session, folder_id), stem, suffix)


def _drive_upload_multipart(
    session: AuthorizedSession, meta: dict, content: bytes, mimetype: str,
) -> str:
    boundary = "gongdijiangongpro"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mimetype}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--".encode("utf-8")
    headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
    res = session.post(
        f"{DRIVE_UPLOAD_API}/files", params={"uploadType": "multipart", "fields": "id"},
        data=body, headers=headers, timeout=DRIVE_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()["id"]


def drive_upload_image(session: AuthorizedSession, folder_id: str, filename: str, image: Image.Image) -> str:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=92)
    meta = {"name": filename, "parents": [folder_id]}
    return _drive_upload_multipart(session, meta, buf.getvalue(), "image/jpeg")


def drive_upload_pdf_bytes(session: AuthorizedSession, folder_id: str, filename: str, pdf_bytes: bytes) -> str:
    meta = {"name": filename, "parents": [folder_id]}
    return _drive_upload_multipart(session, meta, pdf_bytes, "application/pdf")


def _drive_upload_multipart_with_token(
    access_token: str, folder_id: str, filename: str, image: Image.Image,
) -> str:
    """跟 _drive_upload_multipart 邏輯一樣，但只用不可變的 access token 字串發請求，
    不共用 session/連線物件，這樣才能安全地在多個執行緒平行上傳。"""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=92)
    boundary = "gongdijiangongpro"
    meta = {"name": filename, "parents": [folder_id]}
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + buf.getvalue() + f"\r\n--{boundary}--".encode("utf-8")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
    res = requests.post(
        f"{DRIVE_UPLOAD_API}/files", params={"uploadType": "multipart", "fields": "id"},
        data=body, headers=headers, timeout=DRIVE_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()["id"]


def upload_photos_parallel(folder_id: str, jobs: list[tuple[str, str, Image.Image]]) -> dict[str, str]:
    """平行上傳多張照片。jobs 是 (job_id, filename, image) 的清單，
    回傳 {job_id: file_id}。同一批次要存好幾張照片時，比一張一張依序傳快很多。"""
    creds = get_drive_credentials()
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    token = creds.token

    def _do(job):
        job_id, filename, image = job
        return job_id, _drive_upload_multipart_with_token(token, folder_id, filename, image)

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        for job_id, file_id in executor.map(_do, jobs):
            results[job_id] = file_id
    return results


@st.cache_data(show_spinner=False, ttl=3600)
def drive_download_image_bytes(file_id: str) -> bytes | None:
    """下載 Drive 圖片 bytes，用 file_id 當快取 key（圖片上傳後不會變動，適合長快取）。

    直接用 access token 字串（不可變，可安全跨執行緒共用）發請求，而不是共用
    同一個 session/連線物件，這樣 prefetch_images() 平行下載多張照片時才安全。"""
    try:
        creds = get_drive_credentials()
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        headers = {"Authorization": f"Bearer {creds.token}"}
        res = requests.get(
            f"{DRIVE_API}/files/{file_id}", params={"alt": "media"},
            headers=headers, timeout=DRIVE_TIMEOUT,
        )
        res.raise_for_status()
        return res.content
    except Exception as e:
        print(f"Drive 下載失敗 file_id={file_id}: {type(e).__name__}: {e}")
        return None


def prefetch_images(file_ids: list[str]) -> None:
    """平行預先下載多張照片（暖化 drive_download_image_bytes 的快取），
    大幅縮短網格顯示、PDF 匯出等需要一次讀很多張照片時的等待時間。"""
    ids = [f for f in dict.fromkeys(file_ids) if f]
    if not ids:
        return
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(drive_download_image_bytes, ids))


# ----------------------------------------------------------------------
# 檔案/字串工具
# ----------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name.strip())
    return name or "未命名"


# ----------------------------------------------------------------------
# Supabase CRUD — 案場
# ----------------------------------------------------------------------
def fetch_sites() -> list[dict]:
    sb = get_supabase()
    res = sb.table("sites").select("*").order("created_at", desc=True).execute()
    return res.data


def create_site(name: str) -> dict:
    sb = get_supabase()
    res = sb.table("sites").insert({"name": name}).execute()
    return res.data[0]


# ----------------------------------------------------------------------
# Supabase CRUD — 缺失紀錄（一筆紀錄可以對應多張照片）
# ----------------------------------------------------------------------
def insert_record(*, site_id: int, record_date: str, trade: str, vendor: str,
                   status: str, note: str, photos: list[dict]) -> None:
    """photos：[{"photo_file_id": ..., "annotated_file_id": ...}, ...]，依序存 sort_order。"""
    sb = get_supabase()
    res = sb.table("records").insert({
        "site_id": site_id, "record_date": record_date, "trade": trade,
        "vendor": vendor, "status": status, "note": note,
    }).execute()
    record_id = res.data[0]["id"]
    rows = [
        {
            "record_id": record_id,
            "photo_file_id": p["photo_file_id"],
            "annotated_file_id": p.get("annotated_file_id"),
            "sort_order": i,
        }
        for i, p in enumerate(photos)
    ]
    sb.table("record_photos").insert(rows).execute()


def record_photo_ids(record_photos: list[dict] | None) -> list[str]:
    """回傳一筆紀錄底下所有照片的顯示用 file_id（優先用標註圖），依 sort_order 排序。"""
    photos = sorted(record_photos or [], key=lambda p: p.get("sort_order", 0))
    return [p["annotated_file_id"] or p["photo_file_id"] for p in photos]


def fetch_records(site_id: int, vendors=None, statuses=None, trades=None) -> pd.DataFrame:
    sb = get_supabase()
    q = sb.table("records").select("*, record_photos(*)").eq("site_id", site_id)
    if vendors:
        q = q.in_("vendor", vendors)
    if statuses:
        q = q.in_("status", statuses)
    if trades:
        q = q.in_("trade", trades)
    q = q.order("record_date", desc=True).order("id", desc=True)
    res = q.execute()
    return pd.DataFrame(res.data)


def distinct_values(col: str, site_id: int) -> list[str]:
    sb = get_supabase()
    res = sb.table("records").select(col).eq("site_id", site_id).execute()
    vals = {r[col] for r in res.data if r.get(col)}
    return sorted(vals)


def update_status(rec_id: int, status: str) -> None:
    sb = get_supabase()
    sb.table("records").update({"status": status}).eq("id", rec_id).execute()


def delete_record(rec_id: int) -> None:
    """只刪 Supabase 紀錄，Google Drive 上的照片保留（當備份）。"""
    sb = get_supabase()
    sb.table("records").delete().eq("id", rec_id).execute()


# ----------------------------------------------------------------------
# PDF 匯出（fpdf2 + 自動尋找系統中文字型）
# ----------------------------------------------------------------------
FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjh.ttf",
    r"C:\Windows\Fonts\mingliu.ttc", r"C:\Windows\Fonts\kaiu.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    # Linux（Streamlit Community Cloud：packages.txt 要加 fonts-noto-cjk）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]


@st.cache_resource(show_spinner=False)
def find_cjk_font() -> str | None:
    for cand in FONT_CANDIDATES:
        if not os.path.exists(cand):
            continue
        if cand.lower().endswith(".ttc"):
            try:
                from fontTools.ttLib import TTFont
                out = Path(tempfile.gettempdir()) / "系統中文字型.ttf"
                if not out.exists():
                    TTFont(cand, fontNumber=0).save(str(out))
                return str(out)
            except Exception:
                continue
        return cand
    return None


class ReportPDF(FPDF):
    def header(self):
        self.set_font("CJK", size=9)
        self.set_text_color(130, 130, 130)
        self.cell(0, 6, "工地監工 Pro — 缺失追蹤報告", align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("CJK", size=9)
        self.set_text_color(130, 130, 130)
        self.cell(0, 8, f"第 {self.page_no()} / {{nb}} 頁", align="C")


def _pdf_photo_row(pdf: FPDF, file_ids: list[str], tmp_files: list[str]) -> None:
    """把一筆紀錄底下的所有照片，依張數自動算寬度橫向並排畫出來（無外框）。
    1 張：放大置中顯示（設上限避免撐破版面）；2 張以上：等寬平分整個可印刷寬度。"""
    if not file_ids:
        return
    avail_w = pdf.w - pdf.l_margin - pdf.r_margin
    gap = 4
    n = len(file_ids)
    if n == 1:
        widths = [min(avail_w, 130)]
        xs = [pdf.l_margin + (avail_w - widths[0]) / 2]
        max_h = 95
    else:
        w = (avail_w - gap * (n - 1)) / n
        widths = [w] * n
        xs = [pdf.l_margin + j * (w + gap) for j in range(n)]
        max_h = 60

    y0 = pdf.get_y()
    row_h = 0
    for j, fid in enumerate(file_ids):
        img_bytes = drive_download_image_bytes(fid)
        slot_w = widths[j]
        if img_bytes:
            tf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tf.write(img_bytes)
            tf.close()
            tmp_files.append(tf.name)
            with Image.open(tf.name) as im:
                w_mm = slot_w
                h_mm = w_mm * im.height / im.width
                if h_mm > max_h:
                    h_mm, w_mm = max_h, max_h * im.width / im.height
            x = xs[j] + (slot_w - w_mm) / 2
            pdf.image(tf.name, x=x, y=y0, w=w_mm, h=h_mm)
            row_h = max(row_h, h_mm)
        else:
            pdf.set_xy(xs[j], y0)
            pdf.set_text_color(190, 60, 60)
            pdf.set_font("CJK", size=9)
            pdf.cell(slot_w, 8, "（讀取失敗）", align="C")
            pdf.set_text_color(40, 40, 40)
            row_h = max(row_h, 8)
    pdf.set_xy(pdf.l_margin, y0 + row_h)


def _pdf_block_height_estimate(file_ids: list[str], note: str) -> float:
    n = len(file_ids)
    photo_h = 95 if n <= 1 else 60
    note_lines = max(1, -(-len(note) // 46))
    return 8 + 8 + note_lines * 5.5 + 6 + photo_h + 8


def export_pdf_bytes(site_name: str, df: pd.DataFrame) -> bytes:
    font_path = find_cjk_font()
    if font_path is None:
        raise RuntimeError(
            "找不到中文字型。本機請確認系統有中文字型；部署到 Streamlit Cloud "
            "請在 repo 加入 packages.txt，內容寫一行 fonts-noto-cjk 後重新部署。"
        )

    pdf = ReportPDF(orientation="P", unit="mm", format="A4")
    pdf.add_font("CJK", "", font_path)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ---- 頂部資訊區：純白底、雙欄並排、淡灰小字，留白呼吸感 ----
    pdf.set_font("CJK", size=20)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 12, f"{site_name} 缺失追蹤報告", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    counts = df["status"].value_counts().to_dict()
    left_lines = [
        f"案場名稱：{site_name}",
        f"紀錄期間：{df['record_date'].min()} ～ {df['record_date'].max()}",
    ]
    right_lines = [
        f"報告日期：{datetime.date.today():%Y-%m-%d}",
        f"缺失總數：{len(df)} 筆（待處理 {counts.get('待處理', 0)}｜"
        f"修繕中 {counts.get('修繕中', 0)}｜已驗收 {counts.get('已驗收', 0)}）",
    ]
    pdf.set_font("CJK", size=9.5)
    pdf.set_text_color(80, 80, 80)
    y0 = pdf.get_y()
    col_w = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
    pdf.set_xy(pdf.l_margin, y0)
    for line in left_lines:
        pdf.cell(col_w, 6.5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(pdf.l_margin + col_w, y0)
    for line in right_lines:
        pdf.cell(col_w, 6.5, line, new_x="LEFT", new_y="LAST")
        pdf.set_xy(pdf.l_margin + col_w, pdf.get_y() + 6.5)
    pdf.set_y(y0 + max(len(left_lines), len(right_lines)) * 6.5 + 6)
    pdf.set_text_color(30, 30, 30)

    all_file_ids = [
        fid for _, r in df.iterrows() for fid in record_photo_ids(r["record_photos"])
    ]
    prefetch_images(all_file_ids)

    tmp_files = []
    try:
        for _, r in df.iterrows():
            file_ids = record_photo_ids(r["record_photos"])
            note = (r["note"] or "").strip() or "（無備註）"

            if pdf.get_y() + _pdf_block_height_estimate(file_ids, note) > pdf.h - 22:
                pdf.add_page()

            # 極細淡灰分隔線，把每筆紀錄自然斷開成一個個區塊
            pdf.set_draw_color(220, 220, 220)
            pdf.set_line_width(0.2)
            y = pdf.get_y()
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(5)

            # 工種 │ 廠商 │ 狀態
            pdf.set_font("CJK", size=11.5)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(0, 7, f"{r['trade']}    │    {r['vendor']}    │    {r['status']}",
                     new_x="LMARGIN", new_y="NEXT")

            # 備註：靠左、留白
            pdf.ln(1.5)
            pdf.set_font("CJK", size=9.5)
            pdf.set_text_color(100, 100, 100)
            pdf.multi_cell(0, 5.5, note, align="L")
            pdf.set_text_color(30, 30, 30)
            pdf.ln(3)

            _pdf_photo_row(pdf, file_ids, tmp_files)
            pdf.ln(8)
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    return bytes(pdf.output())


# ----------------------------------------------------------------------
# UI：全域樣式（白底、Pantone 色票、無圖案 icon 的簡約風格）
# ----------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        :root {
            --bg: #F4F4F4;
            --charcoal: #25262B;
            --violet: #7E57C2;
            --lime: #C6FF00;
            --muted-gray: #E0E0E0;
            --field-bg: #FAFAFA;
            --label-gray: #57575D;
        }

        .stApp { background: var(--bg); }

        /* ---- 拿掉頁面最上方 Streamlit 內建的橘黃漸層跑動線 ---- */
        [data-testid="stDecoration"] { display: none !important; }

        /* ---- 拿掉標題旁自動出現的錨點連結圖示（滑鼠移過去才會用到，平常不需要） ---- */
        h1 a, h2 a, h3 a, [data-testid="stHeaderActionElements"] { display: none !important; }

        /* ---- 執行中右上角「跑步人 GIF + Running... 文字」換成簡單的圓圈轉圈動畫 ----
           實際抓到的節點結構是 <img alt="Running..."> + <label>Running...</label> +
           <span><button>Stop</button></span>，不是 svg，也不是純文字，要精準點名。 */
        [data-testid="stStatusWidget"] {
            display: inline-flex !important; align-items: center !important; gap: 6px !important;
        }
        [data-testid="stStatusWidget"] img,
        [data-testid="stStatusWidget"] label { display: none !important; }
        [data-testid="stStatusWidget"]::before {
            content: "";
            width: 14px; height: 14px; flex: none;
            border: 2px solid var(--muted-gray);
            border-top-color: var(--violet);
            border-radius: 50%;
            animation: wg-spin 0.8s linear infinite;
        }
        @keyframes wg-spin { to { transform: rotate(360deg); } }

        /* ---- 「Stop」文字換成簡約的方形停止符號 ---- */
        [data-testid="stStatusWidget"] button {
            font-size: 0 !important;
            min-width: auto !important; width: auto !important; min-height: auto !important;
            padding: 6px !important; border-radius: 6px !important;
        }
        [data-testid="stStatusWidget"] button::before {
            content: "";
            display: inline-block;
            width: 10px; height: 10px;
            background: var(--charcoal);
            border-radius: 2px;
        }

        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "PingFang TC",
                "Microsoft JhengHei", system-ui, sans-serif;
        }

        /* ---- 標題：輕盈、不搶戲 ---- */
        h1, h2, h3 { color: var(--charcoal); }
        h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; }
        h2 { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.01em; }
        h3, .section-eyebrow {
            font-size: 0.95rem; font-weight: 600;
            letter-spacing: 0.01em; margin: 1.1rem 0 0.4rem 0;
        }

        /* ---- 表單標籤：小、優雅深灰、微字距 ---- */
        [data-testid="stWidgetLabel"] p,
        .stSelectbox label, .stTextInput label, .stTextArea label,
        .stDateInput label, .stRadio label, .stMultiSelect label {
            font-size: 0.85rem !important;
            color: var(--label-gray) !important;
            letter-spacing: 0.02em;
            font-weight: 500;
        }

        /* ---- 輸入框／下拉選單：扁平、低飽和、細邊框，focus 時邊框轉數位紫 ---- */
        div[data-testid="stTextInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stDateInput"] input,
        div[data-testid="stNumberInput"] input {
            background: var(--field-bg);
            border: 1px solid var(--muted-gray);
            border-radius: 8px;
            padding: 0.45rem 0.7rem;
        }
        div[data-testid="stTextInput"] input:focus,
        div[data-testid="stTextArea"] textarea:focus,
        div[data-testid="stDateInput"] input:focus,
        div[data-testid="stNumberInput"] input:focus {
            border-color: var(--violet) !important;
            box-shadow: 0 0 0 1px var(--violet) !important;
        }
        div[data-baseweb="select"] > div {
            background: var(--field-bg) !important;
            border: 1px solid var(--muted-gray) !important;
            border-radius: 8px !important;
            min-height: 2.3rem;
        }
        div[data-baseweb="select"]:focus-within > div {
            border-color: var(--violet) !important;
            box-shadow: 0 0 0 1px var(--violet) !important;
        }

        /* ---- 元件間距：縮小預設留白，讓版面更緊湊 ---- */
        div[data-testid="stVerticalBlock"] { gap: 0.55rem; }

        /* ---- 按鈕 ---- */
        .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
            width: 100%;
            min-height: 2.7rem;
            font-size: 0.95rem;
            font-weight: 600;
            border-radius: 8px;
        }

        /* ---- 分頁切換：把原生單選圓點改造成分段控制器 ---- */
        div[role="radiogroup"] {
            display: inline-flex;
            width: 100%;
            background: var(--muted-gray);
            border-radius: 10px;
            padding: 3px;
            gap: 2px;
        }
        div[role="radiogroup"] label[data-baseweb="radio"] {
            flex: 1;
            margin: 0;
            justify-content: center;
            padding: 0.45rem 0.6rem;
            border-radius: 7px;
            transition: background 0.15s ease;
        }
        div[role="radiogroup"] label[data-baseweb="radio"] > div:first-child {
            display: none;
        }
        div[role="radiogroup"] label[data-baseweb="radio"] p {
            font-size: 0.88rem; font-weight: 500; margin: 0; color: #6b6b6b;
        }
        div[role="radiogroup"] label[data-baseweb="radio"]:has(input:checked) {
            background: var(--violet);
        }
        div[role="radiogroup"] label[data-baseweb="radio"]:has(input:checked) p {
            font-weight: 600; color: #FFFFFF;
        }

        /* ---- 版面留白 ---- */
        .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 760px; }
        @media (max-width: 640px) {
            .block-container { padding-left: 1.25rem; padding-right: 1.25rem; }
        }
        img { border-radius: 4px; }
        hr { border-color: var(--muted-gray); }

        /* ---- 提示區塊：淡化，不做突兀的飽和色塊 ---- */
        div[data-testid="stAlert"] {
            background: var(--field-bg) !important;
            border: 1px solid var(--muted-gray) !important;
            color: #6b6b6b !important;
        }
        div[data-testid="stAlert"] svg { opacity: 0.55; }

        .empty-state {
            text-align: center; color: #9a9a9a; font-size: 0.9rem;
            padding: 2.2rem 0;
        }

        /* ---- 案場列表 ---- */
        .site-row {
            display: flex; align-items: center; justify-content: space-between;
            padding: 0.9rem 0; border-bottom: 1px solid var(--muted-gray);
        }
        .site-row .site-name { font-size: 1.02rem; font-weight: 600; color: var(--charcoal); }
        .site-row .site-meta { font-size: 0.8rem; color: #9a9a9a; }

        /* ---- 幽靈按鈕（切換案場）：深碳灰細邊框，hover 轉淡紫底 ---- */
        .st-key-switch_site_btn { display: flex; justify-content: flex-end; }
        .st-key-switch_site_btn button {
            width: auto; min-height: auto; white-space: nowrap;
            padding: 0.35rem 0.9rem; font-size: 0.82rem; font-weight: 500;
            background: transparent !important; border: 1px solid var(--charcoal) !important;
            color: var(--charcoal) !important; border-radius: 999px;
            transition: background 0.15s ease;
        }
        .st-key-switch_site_btn button:hover {
            background: rgba(126, 87, 194, 0.1) !important;
            border-color: var(--violet) !important;
            color: var(--violet) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _inject_datalist(label: str, key: str, history: list[str]) -> None:
    """幫輸入框接上瀏覽器原生的 <datalist>：打字時常用值會用瀏覽器內建的
    下拉選單顯示、可以直接點選。因為 st.text_input 沒有原生支援 datalist，
    這裡用 components.html 開一個 iframe 執行 JS，
    直接把 <datalist> 掛到外層頁面（st.markdown 裡的 <script> 不會被執行，
    只有透過 components.html 的 iframe 才能真的跑 JS）。"""
    datalist_id = f"datalist_{key}"
    options_html = "".join(f"<option value=\"{h}\"></option>" for h in history)
    label_escaped = label.replace('"', '\\"')
    components.html(
        f"""
        <script>
        (function() {{
            var doc = window.parent.document;
            var dl = doc.getElementById("{datalist_id}");
            if (!dl) {{
                dl = doc.createElement("datalist");
                dl.id = "{datalist_id}";
                doc.body.appendChild(dl);
            }}
            dl.innerHTML = {options_html!r};
            var inputs = doc.querySelectorAll('input[aria-label="{label_escaped}"]');
            inputs.forEach(function(el) {{ el.setAttribute("list", "{datalist_id}"); }});
        }})();
        </script>
        """,
        height=0,
    )


def suggest_input(label: str, col_name: str, key: str, site_id: int) -> str:
    """單一輸入框：直接打字，沒有的值打了就直接算新增，不用另外跳一格選擇。
    打字時常用值會用瀏覽器原生下拉選單顯示（datalist）供點選。"""
    text_key = f"{key}_txt"
    value = st.text_input(label, key=text_key, placeholder="輸入或點下方常用值")
    history = distinct_values(col_name, site_id)
    if history:
        _inject_datalist(label, key, history)
    return value.strip()


# ----------------------------------------------------------------------
# 頁面零：案場列表 / 建立
# ----------------------------------------------------------------------
def page_site_picker() -> None:
    st.title("工地監工 Pro")
    st.caption("文字紀錄存於 Supabase，照片存於你的 Google Drive")
    st.markdown("---")

    st.markdown("##### 新增案場")
    with st.form("new_site_form", clear_on_submit=True):
        name = st.text_input(
            "案場名稱", label_visibility="collapsed",
            placeholder="輸入案場名稱，例如：桃園晴天大樓",
        )
        submitted = st.form_submit_button("建立案場", type="primary")
    if submitted:
        if not name.strip():
            st.error("請輸入案場名稱。")
        else:
            try:
                site = create_site(name.strip())
                st.session_state["active_site"] = site
                st.rerun()
            except Exception as e:
                st.error(f"建立失敗：{e}")

    sites = fetch_sites()
    if not sites:
        st.info("目前還沒有案場，請先在上面新增一個。")
        return

    st.markdown("##### 選擇案場")
    for s in sites:
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(
                f'<div class="site-row"><div>'
                f'<div class="site-name">{s["name"]}</div>'
                f'<div class="site-meta">建立於 {s["created_at"][:10]}</div>'
                f"</div></div>",
                unsafe_allow_html=True,
            )
        with c2:
            if st.button("進入", key=f"enter_{s['id']}"):
                st.session_state["active_site"] = s
                st.rerun()


# ----------------------------------------------------------------------
# 頁面一：現場拍照 / 標註
# ----------------------------------------------------------------------
def _init_capture_state() -> None:
    st.session_state.setdefault("capture_photos", [])
    st.session_state.setdefault("seen_uploads", set())
    st.session_state.setdefault("camera_counter", 0)
    st.session_state.setdefault("uploader_counter", 0)
    st.session_state.setdefault("active_photo_id", None)


def page_capture(site: dict) -> None:
    st.subheader("現場拍照與標註")
    _init_capture_state()

    c1, c2 = st.columns(2)
    with c1:
        rec_date = st.date_input("日期", datetime.date.today())
        trade = suggest_input("工種", "trade", "trade", site["id"])
    with c2:
        vendor = suggest_input("廠商", "vendor", "vendor", site["id"])
        status = st.selectbox("缺失狀態", STATUS_OPTIONS)
    note = st.text_area("備註", height=100, placeholder="例：3F 樑柱保護層不足，需補修")

    st.markdown("---")

    photos: list[dict] = st.session_state["capture_photos"]

    src = st.radio("照片來源", ["現場拍照", "上傳照片（可多選）"], horizontal=True)
    if src == "現場拍照":
        cam_file = st.camera_input(
            "對準缺失處拍照", key=f"camera_{st.session_state['camera_counter']}"
        )
        if cam_file is not None:
            photos.append({
                "id": f"cam_{st.session_state['camera_counter']}",
                "original": Image.open(cam_file).convert("RGB"),
                "annotated": None,
            })
            st.session_state["camera_counter"] += 1
            st.rerun()
    else:
        uploads = st.file_uploader(
            "選擇照片", type=["jpg", "jpeg", "png"], accept_multiple_files=True,
            label_visibility="collapsed", key=f"uploader_{st.session_state['uploader_counter']}",
        )
        for f in uploads or []:
            sig = (f.name, f.size)
            if sig not in st.session_state["seen_uploads"]:
                st.session_state["seen_uploads"].add(sig)
                photos.append({
                    "id": f"up_{f.name}_{f.size}",
                    "original": Image.open(f).convert("RGB"),
                    "annotated": None,
                })

    if not photos:
        st.info("請先拍照或上傳照片，可以一次加入多張。")
        return

    st.markdown(f"##### 已加入 {len(photos)} 張照片，點縮圖底下的「標註」逐張標記")
    thumb_cols = st.columns(min(len(photos), 4))
    for idx, p in enumerate(photos):
        with thumb_cols[idx % len(thumb_cols)]:
            st.image(p["annotated"] or p["original"], use_column_width=True)
            label = "✓ 已標註" if p["annotated"] is not None else "標註"
            if st.button(label, key=f"open_{p['id']}", use_container_width=True):
                st.session_state["active_photo_id"] = p["id"]
                st.rerun()
            if st.button("移除", key=f"remove_{p['id']}", use_container_width=True):
                st.session_state["capture_photos"] = [x for x in photos if x["id"] != p["id"]]
                if st.session_state["active_photo_id"] == p["id"]:
                    st.session_state["active_photo_id"] = None
                st.rerun()

    active_id = st.session_state["active_photo_id"]
    active_photo = next((p for p in photos if p["id"] == active_id), None)

    if active_photo is not None:
        st.markdown("---")
        st.markdown("##### 在照片上標註（手指或滑鼠直接畫）")
        original = active_photo["original"]
        t1, t2, t3 = st.columns([2, 1, 1])
        with t1:
            mode_label = st.radio(
                "工具", ["畫圈", "箭頭・線", "方框", "自由畫"],
                horizontal=True, label_visibility="collapsed", key=f"mode_{active_id}",
            )
        with t2:
            color = st.color_picker("顏色", "#FF0000", key=f"color_{active_id}")
        with t3:
            width = st.slider("線寬", 2, 15, 5, key=f"width_{active_id}")
        mode = {"畫圈": "circle", "箭頭・線": "line",
                "方框": "rect", "自由畫": "freedraw"}[mode_label]

        canvas_w = 690
        canvas_h = int(canvas_w * original.height / original.width)
        if canvas_h > 900:
            canvas_h = 900
            canvas_w = int(canvas_h * original.width / original.height)
        bg = original.resize((canvas_w, canvas_h))

        canvas = st_canvas(
            fill_color="rgba(255, 0, 0, 0)",
            stroke_width=width,
            stroke_color=color,
            background_image=bg,
            drawing_mode=mode,
            width=canvas_w,
            height=canvas_h,
            key=f"canvas_{active_id}",
        )

        if st.button("完成這張標註", type="primary"):
            if canvas.image_data is not None and np.any(canvas.image_data[:, :, 3] > 0):
                overlay = Image.fromarray(
                    canvas.image_data.astype("uint8"), "RGBA"
                ).resize(original.size)
                active_photo["annotated"] = Image.alpha_composite(
                    original.convert("RGBA"), overlay
                ).convert("RGB")
            st.session_state["active_photo_id"] = None
            st.rerun()

    st.markdown("---")
    if st.button(f"儲存紀錄（{len(photos)} 張照片，上傳到 Google Drive）", type="primary"):
        if not trade or not vendor:
            st.error("「工種、廠商」為必填欄位。")
            return

        try:
            with st.spinner("上傳照片到 Google Drive 中…"):
                service = get_drive_service()
                folder_id = ensure_site_date_folder(service, site["name"], rec_date)
                stem_base = f"{rec_date.strftime('%Y%m%d')}_{sanitize(trade)}_{sanitize(vendor)}"

                # 檔名只查一次 Drive、其餘在本機算不重複檔名，再平行上傳，
                # 取代原本「每張照片各自查檔名 + 依序上傳」的做法，大幅減少等待時間。
                existing_names = drive_list_filenames(service, folder_id)
                jobs = []
                job_map = []  # (photo_index, "orig"/"anno")
                for idx, p in enumerate(photos):
                    orig_name = unique_name(existing_names, stem_base)
                    jobs.append((f"{idx}_orig", orig_name, p["original"]))
                    job_map.append((idx, "orig"))
                    if p["annotated"] is not None:
                        anno_name = unique_name(existing_names, Path(orig_name).stem + "_標註")
                        jobs.append((f"{idx}_anno", anno_name, p["annotated"]))

                uploaded = upload_photos_parallel(folder_id, jobs)
                photo_rows = []
                for idx, p in enumerate(photos):
                    photo_rows.append({
                        "photo_file_id": uploaded[f"{idx}_orig"],
                        "annotated_file_id": uploaded.get(f"{idx}_anno"),
                    })

            insert_record(
                site_id=site["id"], record_date=rec_date.strftime("%Y-%m-%d"),
                trade=trade, vendor=vendor, status=status, note=note,
                photos=photo_rows,
            )
            drive_download_image_bytes.clear()
            st.session_state["capture_photos"] = []
            st.session_state["seen_uploads"] = set()
            st.session_state["active_photo_id"] = None
            st.session_state["uploader_counter"] += 1
            st.success("已儲存！照片已上傳到你的 Google Drive。")
            st.balloons()
            st.rerun()
        except Exception as e:
            st.error(f"儲存失敗：{e}")


# ----------------------------------------------------------------------
# 頁面二：篩選與管理（網格相冊）＋ PDF 匯出
# ----------------------------------------------------------------------
def page_manage(site: dict) -> None:
    st.subheader("篩選與管理")

    f1, f2, f3 = st.columns(3)
    with f1:
        f_trades = st.multiselect("工種", distinct_values("trade", site["id"]))
    with f2:
        f_vendors = st.multiselect("廠商", distinct_values("vendor", site["id"]))
    with f3:
        f_status = st.multiselect("缺失狀態", STATUS_OPTIONS)

    df = fetch_records(site["id"], f_vendors, f_status, f_trades)
    if df.empty:
        st.markdown(
            '<div class="empty-state">目前沒有符合條件的紀錄</div>',
            unsafe_allow_html=True,
        )
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("筆數", len(df))
    m2.metric("待處理", int((df["status"] == "待處理").sum()))
    m3.metric("修繕中", int((df["status"] == "修繕中").sum()))
    m4.metric("已驗收", int((df["status"] == "已驗收").sum()))

    if st.button("匯出報告（PDF）", type="primary"):
        try:
            with st.spinner("產生 PDF 中…"):
                pdf_bytes = export_pdf_bytes(site["name"], df)
                service = get_drive_service()
                root_id = st.secrets["gdrive"]["root_folder_id"]
                site_folder = drive_find_or_create_folder(service, sanitize(site["name"]), root_id)
                report_folder = drive_find_or_create_folder(service, "報告", site_folder)
                fname = f"{sanitize(site['name'])}_缺失報告_{datetime.datetime.now():%Y%m%d_%H%M%S}.pdf"
                drive_upload_pdf_bytes(service, report_folder, fname, pdf_bytes)
            st.success("報告已產生，並備份一份到 Google Drive。")
            st.download_button(
                "下載 PDF", data=pdf_bytes,
                file_name=fname, mime="application/pdf",
            )
        except RuntimeError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"匯出失敗：{e}")

    st.markdown("---")

    all_file_ids = [
        fid for _, r in df.iterrows() for fid in record_photo_ids(r["record_photos"])
    ]
    prefetch_images(all_file_ids)

    cols_per_row = 2
    rows = [df.iloc[i:i + cols_per_row] for i in range(0, len(df), cols_per_row)]
    for chunk in rows:
        cols = st.columns(cols_per_row)
        for col, (_, r) in zip(cols, chunk.iterrows()):
            with col:
                file_ids = record_photo_ids(r["record_photos"])
                if not file_ids:
                    st.warning("這筆紀錄沒有照片")
                else:
                    cover_bytes = drive_download_image_bytes(file_ids[0])
                    if cover_bytes:
                        st.image(cover_bytes, use_column_width=True)
                    else:
                        st.warning("照片讀取失敗（Google Drive）")
                    if len(file_ids) > 1:
                        # 剩餘照片按比例排成等寬縮圖列，讓多張照片的紀錄一眼看出張數。
                        extra_cols = st.columns(len(file_ids) - 1)
                        for extra_col, fid in zip(extra_cols, file_ids[1:]):
                            with extra_col:
                                extra_bytes = drive_download_image_bytes(fid)
                                if extra_bytes:
                                    st.image(extra_bytes, use_column_width=True)
                st.markdown(
                    f"{status_dot_html(r['status'])}｜{r['record_date']}<br>"
                    f"{r['trade']}｜{r['vendor']}",
                    unsafe_allow_html=True,
                )
                with st.expander("詳情 / 更新"):
                    st.write(r["note"] or "（無備註）")
                    new_status = st.selectbox(
                        "更新狀態", STATUS_OPTIONS,
                        index=STATUS_OPTIONS.index(r["status"]),
                        key=f"st_{r['id']}",
                    )
                    b1, b2 = st.columns(2)
                    if b1.button("更新", key=f"upd_{r['id']}"):
                        update_status(int(r["id"]), new_status)
                        st.rerun()
                    if b2.button("刪除紀錄", key=f"del_{r['id']}"):
                        delete_record(int(r["id"]))
                        st.rerun()


# ----------------------------------------------------------------------
# 主程式
# ----------------------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title="工地監工 Pro",
        layout="centered",
        menu_items={
            "Get Help": None,
            "Report a bug": None,
            "About": "工地監工 Pro — 工地缺失紀錄管理工具",
        },
    )
    inject_css()

    missing = check_secrets()
    if missing:
        st.error(
            "尚未完成雲端設定，缺少：\n\n"
            + "\n".join(f"- `{m}`" for m in missing)
            + "\n\n請參考「Supabase設定步驟.md」完成 Supabase 與 Google Drive OAuth 授權設定，"
            "並在 `.streamlit/secrets.toml`（本機）或 Streamlit Cloud 的 App Settings → "
            "Secrets（雲端）填入對應內容。"
        )
        st.stop()

    if "active_site" not in st.session_state:
        page_site_picker()
        return

    site = st.session_state["active_site"]
    h1, h2 = st.columns([3, 2], vertical_alignment="center")
    with h1:
        st.markdown(f'<h2 style="margin:0;">{site["name"]}</h2>', unsafe_allow_html=True)
    with h2:
        with st.container(key="switch_site_btn"):
            switch_clicked = st.button("← 切換案場")
    st.markdown('<hr style="margin-top:0.8rem;margin-bottom:1.5rem;">', unsafe_allow_html=True)
    if switch_clicked:
        del st.session_state["active_site"]
        st.rerun()

    tab = st.radio(
        "功能", ["拍照紀錄", "篩選管理"],
        horizontal=True, label_visibility="collapsed",
    )
    if tab == "拍照紀錄":
        page_capture(site)
    else:
        page_manage(site)


if __name__ == "__main__":
    main()
