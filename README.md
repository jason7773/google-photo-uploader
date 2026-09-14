# Google Photos Takeout 遷移助手

把 Google Photos Takeout 匯出的照片與影片整理成可批次傳到 Pixel 的本機工具。程式會讀取 JSON sidecar、補回拍攝時間與 GPS metadata、建立批次，並透過 ADB 傳送到 Android 裝置。

> **目前狀態：Beta／Windows 優先驗證**
>
> ADB 顯示傳送成功，只代表檔案已傳到手機；不代表 Google Photos 已完成雲端備份。清理檔案前，必須先在手機與網頁版 Google Photos 確認照片真的已備份。

## 先看這裡：五分鐘試用

### 需要準備

- Windows 10/11（目前只完整驗證 Windows）。
- Python 3.11 以上，建議 64 位元。
- Google Takeout 下載的 ZIP 檔。第一次啟動需要網路安裝 Python 依賴。
- 如果要傳到手機：Pixel 或其他 Android 裝置、可傳資料的 USB 線，以及 ADB。

ExifTool 與 FFmpeg 的 Windows 版本已放在專案的 `gp-p1-mig/tools/` 子目錄中。ADB 因為由 Android SDK Platform-Tools 提供，請從 [Google 官方頁面下載 Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，把完整的 `platform-tools` 資料夾解壓到 `gp-p1-mig/tools/`。也可以將 ADB 加入 Windows PATH。

### 安裝與啟動

用 Git：

```powershell
git clone https://github.com/jason7773/google-photo-uploader.git
cd google-photo-uploader
.\start.bat
```

或在 GitHub 按 **Code → Download ZIP**，完整解壓後雙擊專案根目錄的 `start.bat`。

第一次啟動時，`start.bat` 會：

1. 建立 `gp-p1-mig/.venv`。
2. 安裝 `requirements-lock.txt` 指定的依賴。
3. 安裝本專案的可編輯版本。
4. 啟動本機 Web UI，預設網址是 `http://127.0.0.1:5000`。

不要只複製 `start.bat`；它需要整個專案目錄。若 5000 埠已被使用，可以執行：

```powershell
.\start.bat --port 5001
```

也可以使用 `gp-p1-mig/啟動網頁.bat`，它會呼叫同一套啟動流程。關閉程式時回到啟動視窗按 `Ctrl+C`。

### 第一次使用

瀏覽器開啟後，依序操作：

1. **Init**：建立工作區與 SQLite 資料庫。這不是清空操作；如果資料庫已存在，程式會沿用它。
2. **Ingest**：輸入一個或多個 Takeout ZIP 路徑。
3. **Reconcile**：將媒體檔與 JSON sidecar 配對。
4. **Patch**：將拍攝時間與 GPS metadata 寫入工作副本。
5. **Make Batch**：建立要傳送到手機的批次。
6. **Push**：確認手機已開啟 USB 偵錯後，傳送到預設的 `/sdcard/DCIM/Camera`。

第一次請先用少量照片測試，確認日期、GPS、檔案格式與 Google Photos 顯示結果符合預期，再處理完整 Takeout。

## Pixel 與 ADB 設定

1. 在手機開啟「設定 → 關於手機 → 版本號」連點七次，啟用開發人員選項。
2. 在開發人員選項開啟「USB 偵錯」。
3. 用可傳輸資料的 USB 線連接手機，並在手機上接受「允許 USB 偵錯」授權。
4. 將手機 USB 模式設為檔案傳輸，並確認只連接一台目標裝置。
5. 在 PowerShell 測試：

```powershell
gp-p1-mig\tools\platform-tools\adb.exe devices
```

應看到裝置狀態為 `device`。若顯示 `unauthorized`，查看手機螢幕並接受授權；若清單為空，請換 USB 線、USB 埠或安裝手機廠商的 Windows 驅動程式。

## 完整操作流程

### 1. 取得 Google Takeout

從 Google Takeout 匯出 Google Photos，下載所有 ZIP。不要把含有私人照片的工作資料夾上傳到 GitHub，也不要把整個使用中的 `data/` 資料夾分享給其他人。

### 2. Ingest

在 Web UI 的 Ingest 視窗貼上 ZIP 路徑。多個路徑可以換行或用分號分隔。程式會將檔案解壓到工作區、計算 SHA-256，並用雜湊值避免重複處理。

### 3. Reconcile

程式會嘗試精確檔名、標準化檔名、JSON title 與其他安全的檔名 fallback。無法安全判定的檔案會保留在 `NEW`，不會猜測錯誤的 sidecar。

### 4. Patch

Patch 使用工作副本寫入 metadata，原始 Takeout ZIP 與解壓原檔會保留。若 ExifTool 或 FFmpeg 找不到，請先看儀表板的工具狀態。

### 5. Make Batch 與 Push

Make Batch 會依檔案數量與大小建立批次。Push 只會將批次副本傳到 Android 裝置，預設路徑為 `/sdcard/DCIM/Camera`。

### 6. 確認 Google Photos 備份

在手機 Google Photos 確認檔案出現、日期與位置正確，並等待備份完成；再用網頁版 Google Photos 搜尋數個檔名確認雲端內容。這個步驟不能由 ADB 的成功訊息取代。

## Verify、CSV 與 Purge

程式將「確認雲端備份」與「整理本機副本」分開：

- Web UI 的 **Verify** 只會把已 Push 的批次標記為 `VERIFIED`，不會立即刪除檔案。
- CSV 驗證必須包含 `batch_id`、`content_id`、`result` 欄位。
- CSV 必須涵蓋整個批次，每列的 `result` 要明確填 `ok`、`pass` 或 `passed`。
- 空白、錯誤批次、未知檔案、重複項目或只有抽樣檔案的 CSV 都不能通過。
- Purge 前會顯示要移動的路徑，並要求再次確認。
- Purge 只會把批次副本與 patched 副本移到 `data/work/purge_trash/<唯一編號>/`，並產生 `manifest.json`。
- 原始 Takeout ZIP、解壓原檔、sidecar 與 SQLite 資料庫會保留。移到回收目錄不會立刻釋放磁碟空間。

如果要恢復副本，依 `manifest.json` 的 `source` 與 `destination` 對照移回即可；資料庫狀態不會自動回復，因此請在操作前保留額外備份。

## 既有使用者：如何保留本機紀錄

請關閉程式後更新程式檔案，不要刪除或替換 `gp-p1-mig/data/`。既有資料包含：

- `data/state/state.db`：媒體、批次、sidecar 配對與處理狀態。
- `data/work/`：解壓檔、patched 副本與批次副本。
- `data/exports/`：驗證清單與 CSV。
- `data/logs/`：本機執行紀錄。

程式會將設定保存到 `data/settings.json`，此檔案已被 Git 忽略。切換工作區不會搬移或清空舊資料庫，資料庫裡既有的絕對路徑也不會自動改寫。

更新前請關閉程式並備份整個 `data/`。SQLite 啟用 WAL 時，不要只複製單一 `.db` 檔案；請使用 SQLite backup 工具或先正常關閉程式。

## CLI 使用方式

偏好命令列時：

```powershell
cd gp-p1-mig
python scripts/bootstrap.py --setup-only
.venv\Scripts\gp-p1-mig.exe init
.venv\Scripts\gp-p1-mig.exe ingest D:\Takeout\takeout-001.zip
.venv\Scripts\gp-p1-mig.exe reconcile
.venv\Scripts\gp-p1-mig.exe patch
.venv\Scripts\gp-p1-mig.exe make-batch --max-bytes 2147483648 --max-files 500
.venv\Scripts\gp-p1-mig.exe push B0001 --device-path /sdcard/DCIM/Camera
```

CLI 預設使用目前目錄作為工作區。需要指定既有工作區時，請在命令中使用相同的 `--root` 與 `--db` 路徑；CLI 不會自動讀取 Web UI 的儲存設定。

## 常見問題

### 找不到 ExifTool 或 FFmpeg

確認完整工具檔案仍在 `gp-p1-mig/tools/`，或將工具加入 PATH。也可以設定：

```powershell
$env:GP_P1_MIG_TOOLS = 'D:\tools'
```

修改後重新整理 Web UI。

### Push 顯示找不到裝置

執行 `adb devices`，確認狀態為 `device`，不是 `unauthorized`；檢查 USB 偵錯授權、資料線、USB 模式與 Windows 驅動程式。

### 有些檔案仍是 NEW

通常代表 sidecar 尚未匯入或檔名無法安全配對。繼續匯入其他 Takeout ZIP，再重新執行 Reconcile。程式刻意不對不確定的檔案猜測 metadata。

### 第一次啟動安裝失敗

確認 Python 在 PATH、可連線到 Python 套件索引，並保留完整錯誤訊息後重新執行 `start.bat`。既有 `data/` 不會因安裝失敗被重設。

### 可以放到網路上給別人連線嗎？

不可以。這是本機單人 Web UI，設計為只監聽 `127.0.0.1`，不要改成公開 IP 或反向代理到網際網路。

## 開發與驗證

```powershell
cd gp-p1-mig
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m build --wheel
```

目前程式已驗證 Web UI、SQLite 資料保留、ZIP 安全檢查、metadata 修復、批次流程與安全清理流程。真實 Pixel 傳送與 Google Photos 雲端備份仍應由使用者用少量檔案先行驗證。

## 授權與第三方工具

本專案程式碼採 [MIT License](LICENSE)。ExifTool、FFmpeg、ADB 與 Socket.IO 各自依其原作者授權條款提供；請使用者自行閱讀並遵守第三方工具的授權。詳見 `gp-p1-mig/tools/` 與 `gp-p1-mig/src/gp_p1_mig/web/static/vendor/` 內的說明。
