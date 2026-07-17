# 工地監工 Pro — 雲端設定與部署完整步驟

架構：文字紀錄存 **Supabase**（免費）、照片存你自己的 **Google Drive**（免費）、
App 跑在 **Streamlit Community Cloud**（免費）。全程不花錢，但需要照著做幾個一次性設定。

---

## 第一步：建立 Supabase 專案

1. 到 https://supabase.com，用 GitHub 或 Email 免費註冊/登入
2. 「New project」，隨便取名（如 `site-supervisor-pro`），資料庫密碼記下來，region 選離你最近的（如 Singapore）
3. 專案建立完成後，左側選單「SQL Editor」→「New query」，貼上這個資料夾裡的 `supabase_schema.sql` 全部內容 →「Run」，跑完應該顯示 Success
4. 左側選單「Project Settings」（齒輪圖示）→「API」，記下兩個值，等等要用：
   - **Project URL**（長得像 `https://xxxxx.supabase.co`）
   - **service_role secret**（在「Project API keys」裡，不是 anon key，要用 service_role 那組——這組權限很大，絕對不能外流）

---

## 第二步：建立 Google OAuth 用戶端（讓 App 能用你自己的帳號寫入 Drive）

⚠️ 這裡改用「你自己 Google 帳號的授權」而不是服務帳戶，因為 Google 規定服務帳戶本身
沒有儲存空間配額（storage quota），即使把資料夾分享給服務帳戶，一樣沒辦法上傳檔案。
改用你自己的帳號授權後，檔案就直接算在你自己的 15GB 免費額度裡，也不用再手動分享資料夾。

1. 到 https://console.cloud.google.com，用你的 Google 帳號登入（免費，不需要信用卡）
2. 上方選單建立一個新專案，隨便取名（如 `site-supervisor-pro`）
3. 左上「≡」→「APIs & Services」→「Enabled APIs & services」→「+ ENABLE APIS AND SERVICES」，搜尋 **Google Drive API**，點進去按「Enable」
4. 左側「OAuth 同意畫面」（OAuth consent screen）：
   - User Type 選「External」
   - 填 App name（隨便取，如 `工地監工 Pro`）、你的 Email 當 support email
   - 「Test users」加入你自己的 Google 帳號信箱
   - 一路儲存到完成（Publishing status 保持在「Testing」即可，不用送審）
5. 左側「Credentials」→「+ CREATE CREDENTIALS」→「OAuth client ID」
   - Application type 選 **Desktop app**，名稱隨便取
   - 建立完成後點「DOWNLOAD JSON」，存成這個專案資料夾裡的 `client_secret.json`
     （**這個檔案要保密，不要傳給別人、不要放進 GitHub**，`.gitignore` 已排除）

### 執行一次性授權工具

在終端機切到專案資料夾，執行：

```bash
python3 gdrive_oauth_setup.py
```

瀏覽器會自動打開 Google 登入頁，用你自己的帳號登入並同意授權（如果看到「這個應用程式未經
Google 驗證」的警告，這是正常的——因為這是你自己用的個人小工具，點「進階」→「前往（不安全）」
繼續即可）。完成後終端機會印出四行設定值，等一下要貼進 secrets。

同時，這支工具會自動在你的 Google Drive 根目錄建立一個叫「工地監工 Pro」的資料夾，
之後所有照片都會存在裡面，不用再手動建立或分享資料夾。

---

## 第三步：本機測試（先確認能跑再部署）

1. 在專案資料夾建立 `.streamlit/secrets.toml`（照 `secrets.toml.example` 的格式），填入：
   - 第一步拿到的 Supabase URL、service_role key
   - 第二步終端機印出的四行內容（root_folder_id、client_id、client_secret、refresh_token）
2. 安裝套件並啟動：
   ```bash
   pip install -r requirements.txt
   streamlit run app.py
   ```
3. 打開 App，先建立一個案場，隨便拍張照存一筆紀錄，去確認：
   - Supabase Dashboard →「Table Editor」→ `records` 表有沒有多一筆
   - 你 Google Drive 裡的「工地監工 Pro」資料夾裡有沒有出現照片

---

## 第四步：部署到 Streamlit Community Cloud

1. 把整包程式碼推上 **public** 的 GitHub repo（`app.py`、`requirements.txt`、`packages.txt`、
   `supabase_schema.sql`、`gdrive_oauth_setup.py`、`.gitignore` 都要推，`secrets.toml` 和
   `client_secret.json` 因為被 `.gitignore` 排除，不會、也不能推上去）：
   ```bash
   git init
   git add app.py requirements.txt packages.txt supabase_schema.sql gdrive_oauth_setup.py .gitignore .streamlit/secrets.toml.example
   git commit -m "工地監工 Pro 雲端版"
   git branch -M main
   git remote add origin https://github.com/你的帳號/site-supervisor-pro.git
   git push -u origin main
   ```
2. 到 https://share.streamlit.io 用 GitHub 帳號登入 →「Create app」→ 選這個 repo、branch `main`、Main file path 填 `app.py`
3. 部署頁面展開「Advanced settings」，找到 **Secrets** 欄位，把 `.streamlit/secrets.toml` 裡的完整內容（填好真實資料的那份，不是 example）貼進去
4. 「Deploy」，等 2-3 分鐘，拿到固定網址（如 `https://site-supervisor-pro.streamlit.app`），手機用 4G/5G 隨時打開都能用

之後改程式碼只要 `git push`，Streamlit Cloud 會自動重新部署。若要改 Secrets，回到 App 右下角「⋮」→「Settings」→「Secrets」修改即可，不用重新部署整個 repo。

---

## 常見問題

**PDF 匯出說「找不到中文字型」**
本機通常沒問題（Mac/Windows 都內建中文字型）。雲端版請確認 repo 裡有 `packages.txt`、內容是 `fonts-noto-cjk` 這一行，部署時 Streamlit Cloud 才會自動安裝中文字型。

**筆數多了以後 Google Drive 或 Supabase 會不會不夠用**
Supabase 純文字資料 500MB 免費額度非常夠用；Google Drive 免費 15GB（跟你的相簿、雲端硬碟共用額度），拍照存證通常也夠一般案場用一段時間，滿了可以到 Google 一站式帳戶管理清理舊檔或另外購買擴充空間。

**刪除紀錄後 Google Drive 裡的照片會不會也不見**
不會，「刪除」只會刪 Supabase 裡的那筆文字紀錄，照片仍留在 Google Drive 資料夾裡當備份。
