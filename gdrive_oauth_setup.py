# -*- coding: utf-8 -*-
"""
工地監工 Pro — Google Drive 個人帳戶授權（一次性本機工具）
=====================================
用途：
  改用「你自己的 Google 帳號」直接操作 Drive，取代原本的服務帳戶。
  服務帳戶本身沒有儲存空間配額，即使資料夾分享給它，一樣沒辦法上傳檔案
  （Google 官方限制：Service Accounts do not have storage quota）。
  改成用你自己的帳號授權後，檔案就直接算在你自己的 15GB 免費額度裡。

使用方式：
  1. 到 Google Cloud Console →「API 和服務」→「憑證」→
     「建立憑證」→「OAuth 用戶端 ID」，應用程式類型選「桌面應用程式」，
     建立後下載 JSON 金鑰檔，存成這個資料夾裡的 client_secret.json
  2. 執行：python3 gdrive_oauth_setup.py
  3. 瀏覽器會自動打開，登入你自己的 Google 帳號並同意授權
  4. 完成後終端機會印出一組設定，複製貼進 .streamlit/secrets.toml 的 [gdrive] 區塊
"""

import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_SECRET_FILE = "client_secret.json"
ROOT_FOLDER_NAME = "工地監工 Pro"


def main() -> None:
    if not Path(CLIENT_SECRET_FILE).exists():
        raise SystemExit(
            f"找不到 {CLIENT_SECRET_FILE}。請先到 Google Cloud Console 建立「OAuth 用戶端 ID」"
            "（應用程式類型選「桌面應用程式」），下載 JSON 後存成這個檔名，放在同一個資料夾。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=False,
        authorization_prompt_message=(
            "\n請把下面這個網址複製貼到瀏覽器（Chrome/Safari 都可以）打開：\n\n{url}\n"
        ),
    )
    print("\n授權成功！正在建立根資料夾…")

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    meta = {"name": ROOT_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}
    folder = service.files().create(body=meta, fields="id").execute()
    folder_id = folder["id"]

    with open(CLIENT_SECRET_FILE, encoding="utf-8") as f:
        client_info = json.load(f)["installed"]

    print(
        "\n已在你的 Google Drive 根目錄建立資料夾「工地監工 Pro」，"
        f"之後所有照片都會存在裡面。\n"
        "\n把下面內容複製貼進 .streamlit/secrets.toml 的 [gdrive] 區塊"
        "（或部署到 Streamlit Cloud 的 Secrets 欄位）：\n"
    )
    print("-" * 60)
    print(f'root_folder_id = "{folder_id}"')
    print(f'client_id = "{client_info["client_id"]}"')
    print(f'client_secret = "{client_info["client_secret"]}"')
    print(f'refresh_token = "{creds.refresh_token}"')
    print("-" * 60)


if __name__ == "__main__":
    main()
