VTuber 影片字幕翻譯器（視窗版）
================================

使用方式
1. 雙擊 VideoTranslator.exe。
2. 按「選擇…」挑選影片。
3. 確認輸出資料夾。
4. 選擇「本機模式」或「GPT 增強模式」。
5. 按「開始處理」。

本機模式
- Whisper 聽寫日文。
- Sakura 在本機翻成臺灣繁體中文。
- 可離線執行，不需要 API Key。

GPT 增強模式
- 先由 Sakura 產生初稿，再由 GPT 參考前後文、日文字幕與 glossary 全片校對。
- 需要網路與 OpenAI API Key，會產生 API 使用費。
- API Key 只在這次程式執行期間使用，不會寫入檔案。

輸出檔案
- output_ja.srt：日文字幕
- output_zh.srt：繁體中文字幕
- output_subtitled.mp4：壓好字幕的影片

術語表
- 共用術語：程式資料夾內的 glossary.json
- 單部影片術語：把 video_glossary.json 放在原始影片旁邊
- video_glossary.example.json 是格式範例

其他選項
- 「只翻譯」需要輸出資料夾裡已經有 output_ja.srt。
- 「只壓字幕」需要輸出資料夾裡已經有 output_zh.srt。
- 「畫面 OCR」只適合原影片本身已有清楚日文字幕，速度會較慢。
- 勾選「重新開始」會覆蓋所選輸出資料夾內的同名輸出，但不會刪除原始影片。

注意
- 請保留 VideoTranslator.exe 旁邊的 _internal 與 tools 資料夾。
- 預設會把每部影片放到 results\影片名稱 的獨立資料夾。
