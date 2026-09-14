# Google Photos Takeout 遷移助手

Windows 本機工具：匯入 Takeout ZIP、比對 sidecar、修復照片／影片 metadata，再透過 ADB 傳到 Pixel。CLI 和瀏覽器介面共用 SQLite 紀錄。

**狀態：Beta。ADB 傳送完成不代表 Google Photos 已完成雲端備份。**

## Windows 快速開始

1. 安裝 **Python 3.11 或更新版本（64 位元）**，安裝時勾選 Add python.exe to PATH。
2. 在本專案 GitHub 頁面選 **Code → Download ZIP**，完整解壓；或使用下方 Git 指令。
3. 雙擊最外層 **start.bat**。第一次需要網路，會在 `gp-p1-mig/.venv` 建立獨立環境並安裝固定版本依賴；之後直接啟動。不要只複製啟動檔。
4. 瀏覽器開啟 `http://127.0.0.1:5000`。新使用者先確認「設定」中的工作目錄，再按 **Init** 建立資料庫。
5. 依序操作 Ingest → Reconcile → Patch → Make Batch → Push。先用少量照片確認日期、GPS 和畫質符合預期。

```powershell
git clone https://github.com/jason7773/google-photo-uploader.git
cd google-photo-uploader
.\start.bat
```

現有 `gp-p1-mig/啟動網頁.bat` 也會呼叫同一個啟動流程。
若 5000 埠已使用，可執行 `start.bat --port 5001`。保留啟動視窗，使用完畢按 Ctrl+C 結束；請勿同時啟動多個程式操作同一份資料庫。

## 外部工具與手機準備

目前原始碼下載包含 Windows ExifTool 與 FFmpeg；首頁會顯示是否找到工具。
**ADB 需另外安裝**：[Google 官方 SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)。把下載的整個 `platform-tools` 資料夾解壓到 `gp-p1-mig/tools/`，不要只複製 adb.exe；也可以加入 PATH。

Pixel 必須開啟「開發人員選項 → USB 偵錯」，使用可傳資料的 USB 線，在手機上接受此電腦的授權。Push 時只連接一台目標裝置，並在手機的 Google Photos 開啟目標資料夾備份。缺少 Windows 裝置驅動時，依手機廠商的指引安裝。

自行安裝 wheel 或使用 macOS/Linux 時，需自行安裝 ExifTool、FFmpeg、ADB；目前只驗證 Windows。可用 `GP_P1_MIG_TOOLS` 指定外部工具目錄。這些工具保有各自授權，專案的 MIT 授權不涵蓋它們。

## 既有使用者：保留所有本機紀錄

- 直接更新程式檔案即可，**不要刪除或替換 `gp-p1-mig/data/`**。
- 原始碼啟動預設仍使用 `gp-p1-mig/data/state/state.db`。從其他目錄呼叫啟動器，不會另建一份空白紀錄。
- 設定保存在 `gp-p1-mig/data/settings.json`，不會上傳 Git。切換工作區不會搬移或清空舊資料庫；舊資料庫中的絕對路徑也不會自動改寫。
- 已有批次、狀態、sidecar 比對、個人未提交的修正與工作檔案都可沿用。Init 是建立／升級資料表，不是清空紀錄。
- 更新前關閉程式並備份整個 `data/`；若資料庫使用中，請用 SQLite backup API，不要只複製 DB 而漏掉 WAL。
- 請分享 GitHub 專案或乾淨的來源壓縮檔，**不要壓縮你使用中的整個資料夾給別人**；`.gitignore` 不會排除手動壓縮的個資。

wheel 安裝預設工作區是家目錄下的 `gp-p1-mig-workspace`，或目前目錄已有的 `data/state/state.db`。可先設定環境變數 `GP_P1_MIG_ROOT` 指定固定工作區；設定檔也放在該目錄的 `data/`。

## 備份確認與清理

1. Push 只確認 ADB 傳送成功。必須自行確認手機 Google Photos 顯示備份完成，並在網頁版檢查照片。
2. Web UI 的 Verify 是**人工確認整個批次已備份**；確認不會自動清理。
3. CSV 驗證使用匯出的完整 checklist，逐列填寫 `result=ok`（也接受 pass／passed）。批次、content_id 必須吻合且覆蓋全批次；空白、未知項目、重複項目及抽樣 CSV 都不能解鎖清理。
4. 清理前顯示副本路徑。新版 Purge 只把批次副本與 patched 副本移到 `data/work/purge_trash/<唯一編號>/`，並寫入 `manifest.json` 對照表。**原始 ZIP、解壓原檔、sidecar 與資料庫保留。**
5. 移至回收目錄不會釋放磁碟空間。確認額外備份有效後，才自行處理回收目錄。若需救回檔案，可依 manifest 的 source/destination 對照複製回原位置；這不會自動回復資料庫狀態。

舊版曾清理的檔案不會因升級而復原。請保留原始 Takeout ZIP 與獨立備份。

## CLI 與開發

```powershell
cd gp-p1-mig
python scripts/bootstrap.py --setup-only
.venv/Scripts/gp-p1-mig.exe --help
.venv/Scripts/gp-p1-mig.exe init
.venv/Scripts/gp-p1-mig.exe ingest D:/Takeout/takeout-001.zip
.venv/Scripts/gp-p1-mig.exe reconcile
.venv/Scripts/gp-p1-mig.exe patch
```

CLI 預設工作區是目前目錄；各命令可用 `--root`、`--db` 明確指定。CLI 不會讀取 Web 的儲存設定，因此自訂工作區時必須提供相同路徑。

測試與 wheel 建置：

```powershell
.venv/Scripts/python.exe -m pip install -e ".[dev]" build
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m build --wheel
```

`requirements-lock.txt` 是已驗證的 Windows 安裝版本。修改依賴後需同步更新並在乾淨環境驗證。Web 模板、JS、CSS 與 Socket.IO 都隨 wheel 附帶；處理照片不需連 CDN。EXE 建置腳本供開發用途，並非已發佈的免安裝產品。

此程式只供本機單人使用，請勿將服務開放到網際網路。
