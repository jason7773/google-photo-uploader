# Sample Dataset 說明（示意）

本資料夾不放真實照片，只提供欄位與檔名規格：

- `takeout_stub/`：模擬 Google Takeout 解壓後結構
  - `Takeout/Google Photos/Album A/IMG_0001.jpg`
  - `Takeout/Google Photos/Album A/IMG_0001.jpg.json`
  - `Takeout/Google Photos/Album A/VID_0001.mp4`
  - `Takeout/Google Photos/Album A/VID_0001.mp4.json`
- `results_stub/`：人工驗證回填 CSV 範本
  - `B0001_sample_items_result.csv`（含 `result` 欄位，OK/FAIL）

可用此結構自行壓縮成 zip，測試 `ingest`。
