# gp-p1-mig

Google Photos Takeout 分包遷移工具：
Takeout Zip -> metadata patch -> batch -> adb push 到 Pixel 1 -> 另一帳戶匯入驗證。

## 目錄結構（Repo Layout）

```text
gp-p1-mig/
├─ pyproject.toml
├─ README.md
├─ sql/
│  └─ init.sql
├─ scripts/
│  ├─ smoke_test.py
│  ├─ verify_p0.py
│  └─ build_windows_exe.bat
├─ tests/
│  ├─ conftest.py
│  ├─ test_db.py
│  ├─ test_tools.py
│  └─ test_workflow.py
├─ tools/                  ← exiftool / ffmpeg / adb 放這裡
│  └─ (platform binaries)
└─ src/
   └─ gp_p1_mig/
      ├─ __init__.py
      ├─ db.py
      ├─ workflow.py
      ├─ cli.py
      ├─ tools.py
      ├─ ui.py             ← Tkinter UI (legacy)
      ├─ web_ui.py          ← Web UI launcher
      └─ web/
         ├─ app.py
         ├─ templates/
         │  └─ index.html
         └─ static/
            ├─ css/style.css
            └─ js/app.js
```

## 安裝需求

- Python 3.11+
- `exiftool`（圖片 metadata）
- `ffmpeg`（影片 metadata）
- `adb`（推送到 Pixel）

```bash
pip install -e .
```

## CLI 指令

- `gp-p1-mig init`
- `gp-p1-mig ingest <takeout.zip>`
- `gp-p1-mig reconcile`
- `gp-p1-mig patch`
- `gp-p1-mig make-batch --max-bytes 2147483648 --max-files 500`
- `gp-p1-mig push <batch-id> --device-path /sdcard/DCIM/Camera`
- `gp-p1-mig export-verify <batch-id>`
- `gp-p1-mig import-verify <batch-id> <result.csv>`
- `gp-p1-mig purge <batch-id>`

所有步驟都會輸出可讀結果，錯誤時會顯示 stderr 摘要。

## 資料庫設計

SQLite schema 位於 `sql/init.sql`，核心資料表：
- `media_items`：媒體、sidecar、預期 metadata、patch 狀態
- `duplicates`：content_id 重複記錄
- `batches` / `batch_items`：分包、推送、驗證、清理狀態
- `ingest_runs`：每次 ingest run 追蹤

## 基本流程

1. 初始化
   ```bash
   gp-p1-mig init
   ```
2. 匯入 zip（會解壓到 `data/work/extracted/<run-id>/<zip-name>/`）
   ```bash
   gp-p1-mig ingest D:\takeout\takeout-part1.zip
   ```
3. 對帳 sidecar + 解析拍攝時間/GPS
   ```bash
   gp-p1-mig reconcile
   ```
4. patch metadata（輸出到 `data/work/patched/<content_id>.<ext>`）
   ```bash
   gp-p1-mig patch
   ```
5. 建 batch（輸出 `batch_manifest.csv`、`push_plan.json`）
   ```bash
   gp-p1-mig make-batch --max-bytes 2147483648 --max-files 500
   ```
6. adb 推送到 Pixel
   ```bash
   gp-p1-mig push B0001 --device-path /sdcard/DCIM/Camera
   ```
7. 匯出人工驗證檔
   ```bash
   gp-p1-mig export-verify B0001
   ```
8. 匯入驗證結果（OK/PASS 代表通過）
   ```bash
   gp-p1-mig import-verify B0001 data/exports/B0001_sample_items_result.csv
   ```
9. 僅 VERIFIED 才可 purge
   ```bash
   gp-p1-mig purge B0001 --purge-patched
   ```

## Pixel 端手動設定步驟

1. Pixel 1 登入「目標 Google 帳戶」。
2. Google Photos 開啟「備份」，使用原畫質（或你希望策略）。
3. 開發者選項開啟 USB 偵錯，PC 端 `adb devices` 確認連線。
4. 建議先小 batch 試跑，確認匯入與時間/GPS 正確再擴大。

## Windows UI（可打包成免安裝 EXE）

- 啟動 UI（原始碼模式）
  ```bash
  gp-p1-mig-ui
  ```
- 打包單檔執行檔（給最終使用者可直接雙擊）
  ```bat
  scripts\build_windows_exe.bat
  ```
  產物：`dist\gp-p1-mig-ui.exe`

## Sample dataset 與 Smoke Test

- 範例結構說明：`data/sample/README.md`
- End-to-end smoke（建立假資料 + zip + ingest + reconcile）
  ```bash
  python scripts/smoke_test.py
  ```
