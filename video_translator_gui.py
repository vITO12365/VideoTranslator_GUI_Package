import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import app


class QueueStream:
    def __init__(self, messages):
        self.messages = messages

    def write(self, text):
        if text:
            self.messages.put(("log", text))
        return len(text)

    def flush(self):
        return None

    def isatty(self):
        return False


class VideoTranslatorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VTuber 影片字幕翻譯器")
        self.geometry("900x700")
        self.minsize(760, 600)
        self.messages = queue.Queue()
        self.running = False

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="local")
        self.step_var = tk.StringVar(value="完整流程")
        self.model_var = tk.StringVar(value=app.OPENAI_GPT_MODEL)
        self.api_key_var = tk.StringVar(value=app.OPENAI_API_KEY)
        self.restart_var = tk.BooleanVar(value=False)
        self.visual_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="等待開始")

        self._build_ui()
        self.after(100, self._drain_messages)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(8, weight=1)

        ttk.Label(
            outer,
            text="VTuber 影片字幕翻譯器",
            font=("Microsoft JhengHei UI", 18, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))

        ttk.Label(outer, text="輸入影片").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.input_var).grid(
            row=1, column=1, sticky="ew", padx=10, pady=5
        )
        ttk.Button(outer, text="選擇…", command=self._choose_input).grid(
            row=1, column=2, sticky="ew", pady=5
        )

        ttk.Label(outer, text="輸出資料夾").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.output_var).grid(
            row=2, column=1, sticky="ew", padx=10, pady=5
        )
        ttk.Button(outer, text="選擇…", command=self._choose_output).grid(
            row=2, column=2, sticky="ew", pady=5
        )

        options = ttk.LabelFrame(outer, text="處理設定", padding=12)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        ttk.Label(options, text="執行內容").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.step_var,
            values=("完整流程", "只聽寫日文", "只翻譯", "只壓字幕"),
            state="readonly",
            width=16,
        ).grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Checkbutton(
            options,
            text="重新開始（覆蓋同名輸出）",
            variable=self.restart_var,
        ).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(
            options,
            text="改用畫面 OCR（較慢，僅適合影片本身有日文字幕）",
            variable=self.visual_var,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        mode = ttk.LabelFrame(outer, text="翻譯模式", padding=12)
        mode.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        mode.columnconfigure(1, weight=1)
        ttk.Radiobutton(
            mode,
            text="本機模式（Whisper + Sakura，可離線）",
            variable=self.mode_var,
            value="local",
            command=self._update_gpt_state,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(
            mode,
            text="GPT 增強模式（Sakura 初稿 + GPT 全片校對）",
            variable=self.mode_var,
            value="gpt",
            command=self._update_gpt_state,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 8))
        ttk.Label(mode, text="OpenAI API Key").grid(row=2, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(mode, textvariable=self.api_key_var, show="●")
        self.api_key_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(mode, text="GPT 模型").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.model_box = ttk.Combobox(
            mode,
            textvariable=self.model_var,
            values=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"),
            width=24,
        )
        self.model_box.grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        self._update_gpt_state()

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        actions.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(actions, text="開始處理", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="開啟輸出資料夾", command=self._open_output).grid(
            row=0, column=1
        )
        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=2, sticky="e")

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(outer, text="執行紀錄").grid(row=7, column=0, columnspan=3, sticky="w")

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#ffffff",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _choose_input(self):
        path = filedialog.askopenfilename(
            title="選擇影片",
            filetypes=(("影片檔案", "*.mp4 *.mkv *.mov *.webm *.avi"), ("所有檔案", "*.*")),
        )
        if path:
            self.input_var.set(path)
            if not self.output_var.get().strip():
                stem = os.path.splitext(os.path.basename(path))[0]
                self.output_var.set(os.path.join(app.APP_DIR, "results", stem))

    def _choose_output(self):
        path = filedialog.askdirectory(title="選擇輸出資料夾")
        if path:
            self.output_var.set(path)

    def _update_gpt_state(self):
        state = "normal" if self.mode_var.get() == "gpt" else "disabled"
        self.api_key_entry.configure(state=state)
        self.model_box.configure(state=state)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.replace("\r", "\n"))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_messages(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "done":
                    self._finish(value)
        except queue.Empty:
            pass
        self.after(100, self._drain_messages)

    def _validate(self):
        raw_input = self.input_var.get().strip().strip('"')
        raw_output = self.output_var.get().strip().strip('"')
        input_path = os.path.abspath(raw_input) if raw_input else ""
        if not os.path.isfile(input_path):
            messagebox.showerror("找不到影片", "請先選擇有效的影片檔案。")
            return None
        if not raw_output:
            messagebox.showerror("缺少輸出位置", "請選擇輸出資料夾。")
            return None
        if self.mode_var.get() == "gpt" and not self.api_key_var.get().strip():
            messagebox.showerror("缺少 API Key", "GPT 增強模式需要 OpenAI API Key。")
            return None
        return input_path, os.path.abspath(raw_output)

    def _start(self):
        if self.running:
            return
        values = self._validate()
        if not values:
            return
        input_path, output_dir = values
        if self.restart_var.get() and os.path.isdir(output_dir):
            names = ("output_ja.srt", "output_zh.srt", "output_subtitled.mp4")
            if any(os.path.exists(os.path.join(output_dir, name)) for name in names):
                if not messagebox.askyesno(
                    "確認重新開始",
                    "將覆蓋輸出資料夾中的同名字幕與影片。要繼續嗎？",
                ):
                    return

        settings = {
            "input_path": input_path,
            "output_dir": output_dir,
            "restart": bool(self.restart_var.get()),
            "visual": bool(self.visual_var.get()),
            "mode": self.mode_var.get(),
            "step": self.step_var.get(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip() or app.OPENAI_GPT_MODEL,
        }
        self.running = True
        self.start_button.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("處理中……")
        self._append_log("\n===== 開始新的處理 =====\n")
        threading.Thread(target=self._worker, args=(settings,), daemon=True).start()

    def _worker(self, settings):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = QueueStream(self.messages)
        success = False
        try:
            input_path = settings["input_path"]
            output_dir = settings["output_dir"]
            os.makedirs(output_dir, exist_ok=True)
            if settings["restart"]:
                for name in ("output_ja.srt", "output_zh.srt", "output_subtitled.mp4"):
                    path = os.path.join(output_dir, name)
                    if os.path.isfile(path):
                        os.remove(path)

            os.chdir(output_dir)
            app.INPUT_VIDEO = input_path
            app.JA_SRT = "output_ja.srt"
            app.ZH_SRT = "output_zh.srt"
            app.OUTPUT_VIDEO = "output_subtitled.mp4"
            app.GLOSSARY_PATH = os.path.join(app.APP_DIR, "glossary.json")
            input_glossary = os.path.join(os.path.dirname(input_path), "video_glossary.json")
            app.VIDEO_GLOSSARY_PATH = (
                input_glossary
                if os.path.isfile(input_glossary)
                else os.path.join(output_dir, "video_glossary.json")
            )
            app.LOCAL_TRANSLATION_CACHE_PATH = os.path.join(
                output_dir, "sakura_translation_cache.json"
            )
            app.GPT_ENHANCEMENT_CACHE_PATH = os.path.join(
                output_dir, "gpt_enhancement_cache.json"
            )
            step = {
                "完整流程": "all",
                "只聽寫日文": "transcribe",
                "只翻譯": "translate",
                "只壓字幕": "burn",
            }[settings["step"]]
            success = bool(
                app.run_pipeline(
                    step=step,
                    force_transcribe=settings["restart"],
                    mode="visual" if settings["visual"] else "audio",
                    gpt_enhance=settings["mode"] == "gpt",
                    openai_api_key=settings["api_key"],
                    gpt_model=settings["model"],
                )
            )
        except Exception:
            print("\n[錯誤] 執行時發生未預期問題：")
            traceback.print_exc()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
            self.messages.put(("done", success))

    def _finish(self, success):
        self.running = False
        self.progress.stop()
        self.start_button.configure(state="normal")
        if success:
            self.status_var.set("處理完成")
            messagebox.showinfo("完成", "字幕處理完成！")
        else:
            self.status_var.set("處理未完成")
            messagebox.showerror("未完成", "處理未完成，請查看執行紀錄。")

    def _open_output(self):
        path = self.output_var.get().strip().strip('"') or app.APP_DIR
        os.makedirs(path, exist_ok=True)
        os.startfile(os.path.abspath(path))

    def _on_close(self):
        if self.running:
            messagebox.showwarning("正在處理", "目前仍在處理影片，請完成後再關閉視窗。")
            return
        self.destroy()


if __name__ == "__main__":
    os.chdir(app.APP_DIR)
    gui = VideoTranslatorGUI()
    if "--smoke-test" in sys.argv:
        gui.withdraw()
        gui.update_idletasks()
        gui.destroy()
    else:
        gui.mainloop()
