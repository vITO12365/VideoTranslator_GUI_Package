import os
import re
import json
import sys
import time
import threading
import subprocess
import urllib.request
import hashlib
import random
import tempfile
import shutil
from difflib import SequenceMatcher

import ollama
from faster_whisper import WhisperModel
from tqdm import tqdm

# Windows PowerShell 的舊版主控台可能使用 CP950；模型偶爾輸出生僻字時，
# 連 print 警告都會觸發 UnicodeEncodeError，導致整條 Pipeline 中斷。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:
    import opencc
except ImportError:
    opencc = None


# ============================================================
# 檔案路徑與基本設定
# ============================================================

MODEL_DIR = r"C:\whisper_models\medium"

# Windows 上 faster-whisper/CTranslate2 的 GPU 版只支援 NVIDIA CUDA。
# AMD 顯示卡改由本機編譯的 whisper.cpp Vulkan 執行；若 Vulkan 工具或模型
# 不存在、或執行失敗，才自動退回上面的 CPU Medium，避免整條流程中斷。
APP_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
FFMPEG_EXE = (
    os.path.join(APP_DIR, "tools", "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(os.path.join(APP_DIR, "tools", "ffmpeg", "ffmpeg.exe"))
    else "ffmpeg"
)
FFPROBE_EXE = (
    os.path.join(APP_DIR, "tools", "ffmpeg", "ffprobe.exe")
    if os.path.isfile(os.path.join(APP_DIR, "tools", "ffmpeg", "ffprobe.exe"))
    else "ffprobe"
)
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "vulkan").strip().lower()
WHISPER_VULKAN_CLI = os.path.join(
    APP_DIR,
    "tools", "whisper-vulkan", "whisper-cli.exe",
)
WHISPER_VULKAN_MODEL = os.path.join(
    APP_DIR,
    "tools", "whisper-models", "ggml-medium.bin",
)
WHISPER_VULKAN_THREADS = 8

INPUT_VIDEO = "input.mp4"

JA_SRT = "output_ja.srt"
ZH_SRT = "output_zh.srt"

OUTPUT_VIDEO = "output_subtitled.mp4"

GLOSSARY_PATH = "glossary.json"
VIDEO_GLOSSARY_PATH = "video_glossary.json"

LOCAL_TRANSLATION_CACHE_PATH = "sakura_translation_cache.json"
GPT_ENHANCEMENT_CACHE_PATH = "gpt_enhancement_cache.json"


# ============================================================
# 本地模型
# ============================================================

# 主翻譯：Sakura
OLLAMA_MODEL = "hf.co/SakuraLLM/Sakura-14B-Qwen2.5-v1.0-GGUF"

# VTuber 口語化
STYLE_MODEL = "qwen3:8b"

# 本地批次翻譯設定。Sakura 模型較大，所以每批四句，減少呼叫次數的同時
# 避免一次輸出太長而被截斷。
LOCAL_TRANSLATE_MODEL = OLLAMA_MODEL
LOCAL_TRANSLATE_TIMEOUT = 180
LOCAL_TRANSLATE_MAX_RETRIES = 3
LOCAL_TRANSLATE_BATCH_SIZE = 4


# ============================================================
# Pipeline 開關
# ============================================================

# 主翻譯後端："sakura_single"（預設，本地逐句可靠翻譯）、
# "sakura"（實驗性批次）或 "qwen"（本地通用模型）。
TRANSLATE_BACKEND = "sakura_single"

# VTuber 口語化
# 本地 Sakura 初稿預設不再額外跑一次 Qwen，以免拖慢速度或讓意思漂移。
USE_STYLE_PASS = False

# 自動建立 glossary
AUTO_GLOSSARY = False


# GPT 增強校對（只有選擇 GPT 模式時才連網）。API Key 不會寫入檔案。
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_GPT_MODEL = os.getenv("OPENAI_GPT_MODEL", "gpt-5.6-terra").strip()
OPENAI_API_ENDPOINT = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT = 180
OPENAI_MAX_RETRIES = 3
OPENAI_BATCH_SIZE = 24


# ============================================================
# Whisper
# ============================================================

CHUNK_LENGTH_SEC = 30   # 120 秒實測仍會整分鐘漏聽；30 秒只增加少量啟動成本，
                         # 卻能避免一次解碼失敗吞掉很長一段內容。
CHUNK_OVERLAP_SEC = 2   # 每段前後多聽兩秒，再依時間中點去重，避免切點截斷句子。

MAX_SUBTITLE_DURATION_SEC = 6.5
MAX_SUBTITLE_CHARS = 34

# 畫面字幕 OCR
VISION_MODEL = "minicpm-v4.5:latest"
VISION_SAMPLE_FPS = 2
VISION_BATCH_SIZE = 8
VISION_TIMEOUT = 180

# Medium 偶爾會在單一句子裡吐出突兀英文或解碼亂碼。不要整段改跑 OCR，
# 只把這種明顯壞句連同前後少量音訊，用較仔細的設定重聽一次。
ASR_RETRY_CORRUPT_SEGMENTS = True
ASR_RETRY_PADDING_SEC = 2.0
ASR_RETRY_BEAM_SIZE = 5


# ============================================================
# Ollama
# ============================================================

CALL_TIMEOUT = 40
WARMUP_TIMEOUT = 180

MAX_RETRIES = 3

KEEP_ALIVE = "30m"

CONTEXT_TURNS = 3

STYLE_TIMEOUT = 30


# ============================================================
# Sakura 官方 Prompt
# ============================================================

SAKURA_SYSTEM_PROMPT = (
    "你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。"
    "忠实翻译当前句，不补充原文没有的食物、动作或解释；术语必须保留指定译名。"
)


# ============================================================
# VTuber 口語化
# ============================================================

VTUBER_STYLE_PROMPT = """你是字幕潤稿員。

請把使用者提供的中文句子，改寫成更口語自然的台灣繁體中文。

嚴格規則：

1. 不要新增原句沒有的語氣詞、感嘆詞或流行用語。
2. 不要新增「笑死」「草」「超有梗」等原文沒有的東西。
3. 不要誇大或渲染原本沒有的情緒。
4. 只調整用字遣詞，句意與語氣強度必須保持一致。
5. 人名、地名、店名、品牌、遊戲名稱、團體名稱等專有名詞必須原封不動。
6. 數字、金額、貨幣不得擅自修改。
7. 不要加入原文沒有的資訊。
8. 只輸出最後的句子。
9. 不要輸出說明、引號或前綴。

只輸出改寫後的句子。"""


# ============================================================
# GPT 最終字幕校對
# ============================================================

GPT_ENHANCEMENT_PROMPT = """你是專業的日文 VTuber 字幕總編輯。
你會收到按時間排序的日文 Whisper 聽寫和 Sakura 中文初稿。請利用同批前後文：
1. 修正上下文能確定的明顯日文同音誤聽，但不確定時保留原日文，不可編造。
2. 把每句整理成自然、簡潔、可直接上片的台灣繁體中文。
3. 禁用中國大陸用語；使用「影片、程式、網路、滑鼠、品質、資訊、按讚」等台灣用法。
4. glossary 的譯名優先級最高，不可自行改名。
5. 保留原編號、語氣、數字和笑聲；不得合併、漏句或加入原文沒有的內容。
6. zh 必須是中文，不可留下未翻譯的日文（專有名詞除外）。
只回傳指定 JSON 結構，不要解釋。"""


# ============================================================
# OpenCC
# ============================================================

_S2TWP_CONVERTER = (
    opencc.OpenCC("s2twp")
    if opencc
    else None
)


# ============================================================
# 一般工具
# ============================================================

def format_timestamp(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    msecs = int((seconds % 1) * 1000)

    return (
        f"{hrs:02d}:{mins:02d}:"
        f"{secs:02d},{msecs:03d}"
    )


def parse_srt(srt_path: str):
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        return []

    blocks = re.split(r"\n\s*\n", content)

    subtitles = []

    for block in blocks:
        lines = block.strip().splitlines()

        if len(lines) >= 3:
            index = lines[0].strip()
            time_str = lines[1].strip()
            text = " ".join(
                line.strip()
                for line in lines[2:]
                if line.strip()
            )

            subtitles.append({
                "index": index,
                "time": time_str,
                "text": text.strip()
            })

    return subtitles


def parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(
        r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*",
        value,
    )
    if not match:
        raise ValueError(f"無法解析 SRT 時間：{value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def clean_ai_response(text: str) -> str:
    if not text:
        return ""

    # 清掉 Qwen3 think
    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL
    ).strip()

    # 如果 think 沒閉合
    if "<think>" in text:
        text = text.split("<think>", 1)[0].strip()

    text = re.sub(
        r"^(翻譯結果|譯文|翻譯|好的|以下是翻譯)[：:\s]*",
        "",
        text
    )

    text = re.sub(
        r"^\[?(前文|當前日文|後文|原文)\]?[：:].*$",
        "",
        text,
        flags=re.MULTILINE
    )

    text = text.strip()

    if (
        len(text) >= 2
        and (
            (text.startswith("“") and text.endswith("”"))
            or
            (text.startswith('"') and text.endswith('"'))
            or
            (text.startswith("「") and text.endswith("」"))
        )
    ):
        text = text[1:-1].strip()

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return ""

    return lines[0]


# ============================================================
# Ollama
# ============================================================

def _call_ollama(
    model: str,
    messages: list,
    keep_alive: str = None,
    think: bool = None,
    num_predict: int = None,
    format_json: bool = False
) -> str:

    kwargs = {
        "model": model,
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.1,
            "top_p": 0.8,
            "num_predict": num_predict or (
                600 if think is not None else 200
            ),
        },
        "messages": messages,
    }

    if think is not None:
        kwargs["think"] = think

    if format_json:
        kwargs["format"] = "json"

    response = ollama.chat(**kwargs)

    content = (
        response["message"]["content"]
        .strip()
    )

    if (
        "<think>" in content
        and "</think>" not in content
    ):
        raise ValueError(
            "Qwen think 輸出被截斷"
        )

    return content


def call_with_hard_timeout(
    model: str,
    messages: list,
    timeout: float,
    keep_alive: str = None,
    think: bool = None,
    num_predict: int = None,
    format_json: bool = False
):
    result = {}

    def target():
        try:
            result["value"] = _call_ollama(
                model,
                messages,
                keep_alive,
                think,
                num_predict,
                format_json
            )
        except Exception as e:
            result["error"] = e

    t = threading.Thread(
        target=target,
        daemon=True
    )

    t.start()
    t.join(timeout)

    if t.is_alive():
        return None, TimeoutError(
            f"Ollama 呼叫超過 {timeout} 秒"
        )

    if "error" in result:
        return None, result["error"]

    return result.get("value"), None


def warmup_model(
    model: str,
    timeout: float,
    think: bool = None
):
    print(
        f">>> 正在載入模型：{model}"
    )

    _, err = call_with_hard_timeout(
        model,
        [
            {
                "role": "user",
                "content": "測試"
            }
        ],
        timeout,
        keep_alive=KEEP_ALIVE,
        think=think
    )

    if err:
        print(
            f"[警告] 模型暖機失敗：{err}"
        )
    else:
        print(
            f">>> {model} 已就緒"
        )


# ============================================================
# JSON
# ============================================================

def load_json_list(path: str, default=None):
    if default is None:
        default = []

    if not os.path.exists(path):
        return default

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            content = f.read().strip()

        if not content:
            return default

        data = json.loads(content)

        if not isinstance(data, list):
            return default

        return data

    except Exception:
        print(
            f"[警告] 無法讀取 {path}，"
            "暫時當成空資料。"
        )
        return default


def load_json_dict(path: str):
    if not os.path.exists(path):
        return {}

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            content = f.read().strip()

        if not content:
            return {}

        data = json.loads(content)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_json(path: str, data):
    tmp_path = path + ".tmp"

    with open(
        tmp_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(tmp_path, path)


# ============================================================
# Glossary
# ============================================================

def load_glossary(path: str):
    if not os.path.exists(path):

        example = [
            {
                "src": "スパチャ",
                "dst": "SC",
                "note": "超級留言/斗內"
            },
            {
                "src": "草",
                "dst": "笑死",
                "note": "彈幕梗"
            }
        ]

        save_json(
            path,
            example
        )

        print(
            f">>> 已建立 {path}"
        )

        return example

    return load_json_list(
        path,
        default=[]
    )


def load_video_glossary(path: str):
    """
    載入單片設定。檔案不存在時使用乾淨的通用模式，不自動建立。

    格式：
    {
      "asr_prompt_terms": ["影片中確定會出現的日文名稱"],
      "glossary": [{"src": "日文", "dst": "繁中", "note": "說明"}]
    }
    """
    empty = {"asr_prompt_terms": [], "glossary": []}
    if not os.path.exists(path):
        return empty

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("最外層必須是 JSON 物件")

        terms = data.get("asr_prompt_terms", [])
        glossary = data.get("glossary", [])
        if not isinstance(terms, list) or not all(isinstance(x, str) for x in terms):
            raise ValueError("asr_prompt_terms 必須是字串陣列")
        if not isinstance(glossary, list):
            raise ValueError("glossary 必須是陣列")

        valid_glossary = [
            item for item in glossary
            if isinstance(item, dict)
            and isinstance(item.get("src"), str)
            and isinstance(item.get("dst"), str)
            and item["src"].strip()
            and item["dst"].strip()
        ]
        return {
            "asr_prompt_terms": list(dict.fromkeys(x.strip() for x in terms if x.strip())),
            "glossary": valid_glossary,
        }
    except Exception as exc:
        print(f"[警告] 無法讀取 {path}：{exc}。本次只使用共用 glossary。")
        return empty


def merge_glossaries(common_glossary: list, video_glossary: list):
    """單片條目若與共用條目同名，以單片譯名為準。"""
    merged = []
    position_by_src = {}
    for item in [*common_glossary, *video_glossary]:
        src = item.get("src", "").strip()
        if not src:
            continue
        normalized = dict(item)
        normalized["src"] = src
        normalized["dst"] = item.get("dst", "").strip()
        if src in position_by_src:
            merged[position_by_src[src]] = normalized
        else:
            position_by_src[src] = len(merged)
            merged.append(normalized)
    return merged


_KATAKANA_RUN = r"[ァ-ヴー]"


_KATAKANA_RUN = r"[ァ-ヴー]"


def _term_matches_with_boundary(
    term: str,
    curr_txt: str
) -> bool:
    """
    純片假名的詞（外來語、VTuber 暱稱常見寫法），前後如果接的也是
    片假名字元，代表只是命中了某個更長複合外來語的一部分，不算真正
    命中（例如詞彙表裡的「エトラ」，不該命中「メトラノーム」這種
    完全無關的長片假名詞中間剛好帶有相同子字串的情況）。

    純平假名的詞、以及含漢字的詞，一律用單純子字串比對，不套用
    邊界限制。原因：日文書寫沒有空格，人名後面接的助詞／です／
    ちゃん／さん幾乎必然是平假名，如果比照片假名也要求「前後不能
    是同類假名」，等於要求人名不能被任何助詞接住——但這在自然的
    日文句子裡幾乎不可能發生，會讓所有純假名暱稱條目永遠比對不到
    （實測：「青霧高校のたまこです」里的「たまこ」就是因為緊接在
    「の」和「です」這兩個平假名旁邊，才會被誤判成沒命中）。
    平假名詞條的誤觸發風險本來就比片假名外來語低（不會有大量平假名
    複合詞彙緊密相連的情況），交給後面的 fuzzy 比對跟人工校對抓漏
    就好，不值得用邊界限制換來「幾乎全部命中不了」的代價。
    """
    if not re.fullmatch(
        _KATAKANA_RUN + "+",
        term
    ):
        return term in curr_txt

    pattern = (
        f"(?<!{_KATAKANA_RUN})"
        f"{re.escape(term)}"
        f"(?!{_KATAKANA_RUN})"
    )

    return re.search(
        pattern,
        curr_txt
    ) is not None


def _to_hiragana(s: str) -> str:
    """
    片假名轉平假名，讓「タオク」跟「たおく」這種同音不同寫法的字串
    在算編輯距離前先統一形式，不然逐字元比對會把片假名/平假名的
    不同編碼點誤判成完全不同的字，距離會虛高、比對不到。
    """
    return "".join(
        chr(ord(ch) - 0x60) if "\u30a1" <= ch <= "\u30f6" else ch
        for ch in s
    )


def _levenshtein(a: str, b: str) -> int:
    """
    標準編輯距離（插入/刪除/替換各算一步）。
    不需要額外套件，字串通常很短（人名/暱稱），純 Python 算就夠快。
    """
    a, b = _to_hiragana(a), _to_hiragana(b)
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,       # 刪除
                curr[j - 1] + 1,   # 插入
                prev[j - 1] + cost,  # 替換
            )
        prev = curr
    return prev[-1]


# 名字/暱稱後面常見的敬稱，用來抓出「這一段可能是個名字」的候選詞
_HONORIFIC_PATTERN = re.compile(
    r"([ぁ-んァ-ヴー一-龥]{2,8})(?:先輩|せんぱい|センパイ|さん|ちゃん|くん|様|氏|先生|たん|で[ーす]{1,3})"
)


def _find_fuzzy_glossary_matches(glossary: list, curr_txt: str):
    """
    音近似比對：專門解決 Whisper 把同一個暱稱每次聽成不同錯字的問題
    （例如「たまこ」被聽成「タオク」「タワコ」「たべこ」「たまご」等）。

    做法：
    1. 只跟詞彙表裡標記為「vtuber的名字」「暱稱」且本身是純假名的條目比對
       （純假名代表那是「讀音」，不是漢字寫法，才適合拿來算音近似）。
    2. 只從句子裡「敬稱前面那一小段」抓候選詞（先輩/さん/ちゃん/くん前面的字），
       這種位置幾乎一定是人名，安全性高，不會亂比對到普通詞彙。
    3. 候選詞跟已知暱稱讀音的編輯距離夠近（差 1~2 個字以內，依長度調整門檻），
       就當作同一個人，不需要你手動窮舉每一種聽錯的寫法。

    回傳格式跟 build_glossary_block 的 matched list 相容，可以直接合併使用。
    """
    name_entries = [
        g for g in glossary
        if g.get("src") and g.get("dst")
        and re.fullmatch(r"[ぁ-んァ-ヴー]+", g["src"])  # 純假名條目才是「讀音」
        and ("vtuber" in g.get("note", "") or "暱稱" in g.get("note", "") or "名字" in g.get("note", ""))
    ]
    if not name_entries:
        return []

    candidates = set(_HONORIFIC_PATTERN.findall(curr_txt))
    if not candidates:
        return []

    fuzzy_matched = []
    seen_dst = set()

    for candidate in candidates:
        # 兩字短名太容易碰巧相近，例如「トラ」曾被錯配成「エトラ」。
        # 短暱稱必須靠明確 glossary 條目，不能做模糊推測。
        if len(candidate) < 3:
            continue

        best_entry = None
        best_dist = None

        for g in name_entries:
            # 已經精確比對過的不用再模糊比對一次
            if g["src"] == candidate:
                continue

            dist = _levenshtein(candidate, g["src"])
            # 門檻依長度調整：字數愈少愈容易「碰巧差不多」（尤其是真實世界的
            # 通用詞彙，例如「いちらん」(一蘭，知名連鎖拉麵店) 跟某個 VTuber
            # 暱稱「ローラン」轉平假名後只差 2 個字，因為兩邊都只有 4 個字，
            # 2 個字的差距等於一半都不一樣，太容易誤判)。實測發現 4 字這個
            # 級距門檻設太寬鬆，收緊到跟 3 字以下一樣只容許差 1 個字；
            # 5 字以上代表詞本身夠長、資訊量夠大，差 2 個字才維持原本寬鬆一點
            # 的容許度。
            threshold = 1 if max(len(candidate), len(g["src"])) <= 4 else 2

            if dist <= threshold and (best_dist is None or dist < best_dist):
                best_entry = g
                best_dist = dist

        if best_entry and best_entry["dst"] not in seen_dst:
            seen_dst.add(best_entry["dst"])
            fuzzy_matched.append({
                "src": candidate,
                "dst": best_entry["dst"],
                "note": (
                    f"音近似比對，可能是「{best_entry['src']}」的聽寫變形"
                    + (f"（{best_entry['note']}）" if best_entry.get("note") else "")
                ),
            })

    return fuzzy_matched


def build_glossary_block(
    glossary: list,
    curr_txt: str
) -> str:

    matched = [
        g
        for g in glossary
        if (
            g.get("src")
            and _term_matches_with_boundary(
                g["src"],
                curr_txt
            )
        )
    ]

    matched += _find_fuzzy_glossary_matches(glossary, curr_txt)

    if not matched:
        return ""

    lines = []

    for g in matched:

        note = g.get("note", "")

        if note:
            lines.append(
                f"{g['src']}->{g['dst']} #{note}"
            )
        else:
            lines.append(
                f"{g['src']}->{g['dst']}"
            )

    return "\n".join(lines)


def get_matched_dst_terms(
    glossary: list,
    curr_txt: str
):
    exact = [
        g["dst"]
        for g in glossary
        if (
            g.get("src")
            and g.get("dst")
            and _term_matches_with_boundary(
                g["src"],
                curr_txt
            )
        )
    ]
    fuzzy = [g["dst"] for g in _find_fuzzy_glossary_matches(glossary, curr_txt)]
    return list(dict.fromkeys(exact + fuzzy))  # 保序去重


def get_exact_matched_glossary_entries(
    glossary: list,
    curr_txt: str
):
    """
    只回傳「精確匹配」（非 fuzzy 音近似猜測）的詞條，用於翻譯前
    直接替換原文。fuzzy 比對本身是猜測，拿猜測結果去改原文风险
    較高，只適合用在 prompt 提示，不適合直接替換文字。
    """
    return [
        g
        for g in glossary
        if (
            g.get("src")
            and g.get("dst")
            and _term_matches_with_boundary(
                g["src"],
                curr_txt
            )
        )
    ]


def get_matched_glossary_entries(
    glossary: list,
    curr_txt: str
):
    exact = get_exact_matched_glossary_entries(
        glossary,
        curr_txt
    )
    fuzzy = _find_fuzzy_glossary_matches(glossary, curr_txt)
    seen = {g["src"] for g in exact}
    return exact + [g for g in fuzzy if g["src"] not in seen]


def fuzzy_fix_glossary_term(final_text: str, dst: str, max_diff_ratio: float = 0.34):
    """
    強制替換（用日文原文找）打不中的情況，多半是模型生成專有名詞時
    「打錯一兩個字」（例如「栗駒小丸」被生成成「慓駒小丸」），這種錯字
    沒辦法靠原文比對修，因為日文原文本來就不會出現在已翻譯完的中文句子裡。
    這裡改成在 final_text 裡滑動視窗找一段「長度跟 dst 一樣、但只差一兩個字」
    的片段，直接修正成正確的 dst。
    只在術語表原文/正確譯名都完全找不到時才會被呼叫，屬於最後一道修復手段，
    只處理錯字等級的差異，不會亂改整段不相關的文字。
    回傳 (修正後的 final_text, 是否有修正)。
    """
    n = len(dst)
    if n == 0 or len(final_text) < n:
        return final_text, False

    # 模型偶爾會把三、四字專名的字序交換，例如「漢堡王」輸出成
    # 「漢王堡」。這不是一般錯字，Hamming distance 會算成兩處差異，
    # 原本的一字容錯抓不到。只有字元完全相同、單純順序不同時才修正，
    # 且此函式只會對當句原文確實命中的 glossary 詞執行。
    if n >= 3:
        for i in range(len(final_text) - n + 1):
            window = final_text[i:i + n]
            if window != dst and sorted(window) == sorted(dst):
                fixed = final_text[:i] + dst + final_text[i + n:]
                return fixed, True

    max_diff = max(1, int(n * max_diff_ratio))
    best_pos, best_diff = None, max_diff + 1

    for i in range(len(final_text) - n + 1):
        window = final_text[i:i + n]
        diff = sum(1 for a, b in zip(window, dst) if a != b)
        if diff < best_diff:
            best_diff, best_pos = diff, i

    if best_pos is not None and best_diff <= max_diff:
        fixed = final_text[:best_pos] + dst + final_text[best_pos + n:]
        return fixed, True

    return final_text, False


def build_asr_prompt(asr_prompt_terms=None):
    # 不把整份 glossary 塞給 Whisper；只使用共用表中明確標成安全的
    # 正式名稱，再加上單片設定列出的名稱。
    return "、".join(asr_prompt_terms or [])


def get_common_asr_prompt_terms(glossary: list):
    return list(dict.fromkeys(
        item["src"].strip()
        for item in glossary
        if item.get("asr_prompt") is True
        and isinstance(item.get("src"), str)
        and item["src"].strip()
    ))


# ============================================================
# 本地批次翻譯共用解析與快取
# ============================================================

def _parse_numbered_batch_response(raw: str):
    """解析批次 JSON；兼容陣列、items 包裝和編號對照表。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)

    if isinstance(data, dict):
        for key in ("translations", "items", "results", "subtitles", "data"):
            if key in data:
                data = data[key]
                break

    if isinstance(data, dict) and "index" in data and "text" in data:
        data = [data]
    elif isinstance(data, dict):
        data = [
            {"index": str(index), "text": translated}
            for index, translated in data.items()
            if isinstance(translated, str)
        ]

    if not isinstance(data, list):
        raise ValueError("批次翻譯結果不是可辨識的 JSON 字幕清單")

    result = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        index = str(item.get("index", "")).strip()
        translated = clean_ai_response(str(item.get("text", "")))
        if index and translated:
            result[index] = translated
    return result


def translation_cache_key(text: str):
    prompt = (
        SAKURA_BATCH_PROMPT
        if TRANSLATE_BACKEND == "sakura"
        else LOCAL_TRANSLATE_PROMPT
    )
    payload = f"{LOCAL_TRANSLATE_MODEL}\0{prompt}\0{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

LOCAL_TRANSLATE_PROMPT = """你是專業的日文 VTuber 字幕翻譯員。
把每句日文翻成可直接上片的自然台灣繁體中文。根據前後句修正明顯的語音辨識
同音錯字，但不確定時不可亂猜。忠實保留原意、數字、語氣和專有名詞，不新增
笑死、草或其他原文沒有的用語。每個 index 必須原樣保留，不得合併或漏句。
只能輸出 JSON 陣列，每項格式為 {"index":"原編號","text":"翻譯"}，不要說明。
/no_think"""

SAKURA_BATCH_PROMPT = SAKURA_SYSTEM_PROMPT + """

请逐行翻译，不得合并、漏句或自行增加信息。术语表优先级最高。
严格保留每行开头的 [编号]，每个输入行只输出一行，格式为 [编号] 译文。
不要输出解释、JSON、Markdown 或其他内容。"""


def _parse_marker_batch_response(raw: str, expected: set):
    """解析 Sakura 最擅長遵循的「[編號] 譯文」純文字格式。"""
    result = {}
    current_index = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^[\[【](\d+)[\]】][：:\s]*(.*)$", line)
        if match and match.group(1) in expected:
            current_index = match.group(1)
            translated = match.group(2).strip()
            if translated:
                result[current_index] = translated
        elif current_index and current_index in result:
            # 容忍模型偶爾把同一譯文折成下一行。
            result[current_index] += line

    return result


def local_translate_batch(history_pairs, batch, glossary):
    """用本地 Sakura/Qwen 小批次翻譯；失敗時拆小，絕不寫入假結果。"""
    rows = []
    required_by_index = {}

    for sub in batch:
        curr_txt = sub["text"]
        source = curr_txt
        for g in get_exact_matched_glossary_entries(glossary, curr_txt):
            source = source.replace(g["src"], g["dst"])

        row = {"index": str(sub["index"]), "ja": source}
        glossary_block = build_glossary_block(glossary, curr_txt)
        if glossary_block:
            row["glossary"] = glossary_block
        rows.append(row)
        required_by_index[str(sub["index"])] = get_matched_dst_terms(
            glossary, curr_txt
        )

    use_sakura = TRANSLATE_BACKEND == "sakura"
    context = [
        {"ja": ja, "zh": zh}
        for ja, zh in history_pairs[-CONTEXT_TURNS:]
    ]
    user_content = ""
    if use_sakura:
        if context:
            user_content += "前文参考：\n" + "\n".join(
                f"日文：{item['ja']}\n中文：{item['zh']}" for item in context
            ) + "\n\n"

        glossary_lines = []
        for row in rows:
            if row.get("glossary"):
                glossary_lines.extend(row["glossary"].splitlines())
        if glossary_lines:
            user_content += "术语表：\n" + "\n".join(
                dict.fromkeys(glossary_lines)
            ) + "\n\n"

        user_content += "将下面每行日文翻译成中文，只输出对应行：\n"
        user_content += "\n".join(
            f"[{row['index']}] {row['ja']}" for row in rows
        )
    else:
        if context:
            user_content += "【前文參考】\n" + json.dumps(
                context, ensure_ascii=False
            ) + "\n"
        user_content += "【待翻譯字幕】\n" + json.dumps(rows, ensure_ascii=False)

    expected = {str(sub["index"]) for sub in batch}
    last_error = None
    model = OLLAMA_MODEL if use_sakura else LOCAL_TRANSLATE_MODEL
    system_prompt = SAKURA_BATCH_PROMPT if use_sakura else LOCAL_TRANSLATE_PROMPT
    think = None if use_sakura else False

    for attempt in range(1, LOCAL_TRANSLATE_MAX_RETRIES + 1):
        raw, err = call_with_hard_timeout(
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            LOCAL_TRANSLATE_TIMEOUT,
            keep_alive=KEEP_ALIVE,
            think=think,
            num_predict=900,
            format_json=not use_sakura,
        )

        if err is None and raw:
            try:
                parsed = (
                    _parse_marker_batch_response(raw, expected)
                    if use_sakura
                    else _parse_numbered_batch_response(raw)
                )
                missing_indexes = expected - set(parsed)
                missing_terms = {
                    index: [
                        term for term in required_by_index[index]
                        if (
                            term not in parsed.get(index, "")
                            and term not in convert_to_traditional(
                                parsed.get(index, "")
                            )
                        )
                    ]
                    for index in expected
                }
                missing_terms = {k: v for k, v in missing_terms.items() if v}
                if not missing_indexes and not missing_terms:
                    return parsed
                last_error = ValueError(
                    f"缺少編號 {sorted(missing_indexes)}；未套用術語 {missing_terms}"
                )
            except Exception as e:
                last_error = e
        else:
            last_error = err or RuntimeError("本地模型沒有回傳內容")

        label = "Sakura" if use_sakura else "Qwen"
        print(f"\n[{label} 批次] 第 {attempt} 次失敗：{last_error}")
        if attempt < LOCAL_TRANSLATE_MAX_RETRIES:
            time.sleep(1)

    if len(batch) > 1:
        middle = len(batch) // 2
        left_batch = batch[:middle]
        right_batch = batch[middle:]
        print(
            f"\n[{'Sakura' if use_sakura else 'Qwen'} 批次] 自動拆成 {len(left_batch)} + "
            f"{len(right_batch)} 句重試。"
        )
        left = local_translate_batch(history_pairs, left_batch, glossary)
        right_history = history_pairs + [
            (sub["text"], left[str(sub["index"])]) for sub in left_batch
        ]
        right = local_translate_batch(right_history, right_batch, glossary)
        return {**left, **right}

    label = "Sakura" if use_sakura else "Qwen"
    raise RuntimeError(f"本地 {label} 連單句翻譯也失敗：{last_error}")


# ============================================================
# Sakura 翻譯
# ============================================================

def translate_sakura(
    history_pairs,
    curr_txt,
    glossary
):

    messages = [
        {
            "role": "system",
            "content":
                SAKURA_SYSTEM_PROMPT
        }
    ]

    for ja, zh in history_pairs[
        -CONTEXT_TURNS:
    ]:

        messages.append({
            "role": "user",
            "content":
                f"将下面的日文文本翻译成中文：{ja}"
        })

        messages.append({
            "role": "assistant",
            "content": zh
        })

    glossary_block = build_glossary_block(
        glossary,
        curr_txt
    )

    required_terms = get_matched_dst_terms(
        glossary,
        curr_txt
    )

    # 精確匹配到的詞，直接在送進 Sakura 前把原文換成中文譯名，
    # 不只靠 prompt 講道理——像「むじな」這種本身也是普通名詞、
    # 模型會忍不住自己音譯的情況，光靠指示常常無效，直接把答案
    # 寫進原文，模型只需要順手保留、翻完其他部分就好。
    # 只對精確匹配做這件事，fuzzy 音近似的猜測不夠可靠，
    # 不適合直接改原文，只放進 glossary_block 當提示就好。
    source_for_translation = curr_txt

    for g in get_exact_matched_glossary_entries(
        glossary,
        curr_txt
    ):
        source_for_translation = (
            source_for_translation.replace(
                g["src"],
                g["dst"]
            )
        )

    if glossary_block:

        user_content = (
            "根据以下术语表：\n"
            f"{glossary_block}\n\n"
            "将下面的日文文本根据上述术语表"
            "翻译成中文："
            f"{source_for_translation}"
        )

    else:

        user_content = (
            "将下面的日文文本翻译成中文："
            f"{source_for_translation}"
        )

    messages.append({
        "role": "user",
        "content": user_content
    })

    last_result = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        raw, err = call_with_hard_timeout(
            OLLAMA_MODEL,
            messages,
            CALL_TIMEOUT,
            keep_alive=KEEP_ALIVE
        )

        if err is None and raw:

            cleaned = clean_ai_response(
                raw
            )

            if not cleaned:
                continue

            # Sakura 被要求只輸出簡體中文，但 glossary 的 dst
            # 是您用繁體打的（例如「一蘭」「音靈魂子」）。簡轉繁
            # 是最後 OpenCC 那步才做的事，這裡如果直接拿繁體 dst
            # 去比對 Sakura 的簡體輸出，即使它已經翻對了（輸出
            # 「一兰」這種簡體字），字串也永遠對不上，會被誤判
            # 成「沒套用」而白白重試、印假警告。所以這裡多轉一份
            # 繁體版本一起比對，兩邊有一邊對得上就算數。
            cleaned_traditional = (
                convert_to_traditional(cleaned)
                if opencc
                else cleaned
            )

            missing = [
                t
                for t in required_terms
                if (
                    t not in cleaned
                    and t not in cleaned_traditional
                )
            ]

            if not missing:
                return cleaned

            last_result = cleaned

            if attempt < MAX_RETRIES:

                messages[-1] = {
                    "role": "user",
                    "content": (
                        user_content
                        + "\n\n"
                        + "特別注意："
                        + str(missing)
                        + " 必須完全按照術語表翻譯。"
                    )
                }

                continue

            print(
                f"\n[警告] 詞彙表未完全套用：{missing} "
                f"沒有出現在翻譯結果中，已重試 {MAX_RETRIES} 次仍未修正，"
                f"建議人工檢查這句：{cleaned}"
            )
            return cleaned

        if attempt < MAX_RETRIES:
            time.sleep(1)

    if last_result is None:
        print(
            f"\n[警告] 該句翻譯重試 {MAX_RETRIES} 次都沒有取得有效結果，"
            f"保留原文字幕: {curr_txt}"
        )

    return (
        last_result
        if last_result
        else curr_txt
    )


# ============================================================
# OpenCC
# ============================================================

def convert_to_traditional(
    simplified_text: str
):

    if _S2TWP_CONVERTER is None:
        return simplified_text

    return _S2TWP_CONVERTER.convert(
        simplified_text
    )


# ============================================================
# VTuber Style
# ============================================================

def localize_vtuber_style(
    traditional_text: str
):

    if not USE_STYLE_PASS:
        return traditional_text

    messages = [
        {
            "role": "system",
            "content":
                VTUBER_STYLE_PROMPT
        },
        {
            "role": "user",
            "content":
                traditional_text
        }
    ]

    raw, err = call_with_hard_timeout(
        STYLE_MODEL,
        messages,
        STYLE_TIMEOUT
    )

    if err is not None or not raw:
        return traditional_text

    result = clean_ai_response(
        raw
    )

    if not result:
        return traditional_text

    return result


# ============================================================
# Whisper
# ============================================================

def get_media_duration(
    path: str
):

    cmd = [
        FFPROBE_EXE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True
    )

    return float(
        result.stdout.strip()
    )


def extract_audio_chunk(
    input_path,
    start_sec,
    length_sec,
    out_path
):

    cmd = [
        FFMPEG_EXE,
        "-y",
        "-ss",
        str(start_sec),
        "-t",
        str(length_sec),
        "-i",
        input_path,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        out_path
    ]

    subprocess.run(
        cmd,
        check=True,
        capture_output=True
    )


def split_whisper_segment(segment, time_offset=0.0):
    """
    使用 Whisper 的逐字時間戳重新切字幕。模型偶爾會把幾十秒口播全部
    塞進一個 Segment；不能直接相信 Segment 邊界，否則翻譯與壓字幕
    都會得到一整坨文字。
    """
    words = [w for w in (getattr(segment, "words", None) or []) if w.word.strip()]
    if not words:
        return [(
            segment.start + time_offset,
            segment.end + time_offset,
            segment.text.strip(),
        )]

    pieces = []
    pos = 0

    while pos < len(words):
        start_time = words[pos].start
        char_count = 0
        hard_cut = pos + 1
        comma_cut = None
        sentence_cut = None

        for j in range(pos, len(words)):
            token = words[j].word
            next_chars = char_count + len(token.strip())
            duration = words[j].end - start_time

            if j > pos and (
                duration > MAX_SUBTITLE_DURATION_SEC
                or next_chars > MAX_SUBTITLE_CHARS
            ):
                break

            char_count = next_chars
            hard_cut = j + 1

            if re.search(r"[。！？!?]$", token.strip()):
                sentence_cut = j + 1
                if duration >= 1.2 and char_count >= 8:
                    break
            elif re.search(r"[、,，]$", token.strip()):
                comma_cut = j + 1

        cut = sentence_cut or comma_cut or hard_cut
        if cut <= pos:
            cut = pos + 1

        selected = words[pos:cut]
        text = "".join(w.word for w in selected).strip()
        if text:
            pieces.append((
                selected[0].start + time_offset,
                selected[-1].end + time_offset,
                text,
            ))
        pos = cut

    return pieces


def asr_corruption_score(text: str) -> int:
    """只抓很明顯的解碼損壞；一般日文外來語不應被誤判。"""
    text = text or ""
    score = text.count("\ufffd") * 20
    score += len(re.findall(r"[A-Za-z]{4,}", text)) * 6
    score += len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text)) * 20
    return score


def strip_asr_overlap(previous_text: str, candidate_text: str) -> str:
    """移除補聽時因前置 2 秒而重複到上一句的字。"""
    previous = re.sub(r"\s+", "", previous_text or "")
    candidate = re.sub(r"\s+", "", candidate_text or "")
    max_overlap = min(len(previous), len(candidate), 30)
    for length in range(max_overlap, 3, -1):
        if previous.endswith(candidate[:length]):
            return candidate[length:].lstrip("、。，,. ")
    return candidate


def merge_incomplete_asr_fragments(segments):
    """接回 Whisper 明顯錯切的半句，避免翻譯模型對殘句亂猜。"""
    if not segments:
        return []

    merged = []
    last_was_merged = False
    incomplete_ending = re.compile(
        r"(?:[、,，]|を|が|は|の|に|へ|と|も|で|て|から|けど|ので|って)$"
    )

    for start, end, text in segments:
        if not merged:
            merged.append((start, end, text))
            last_was_merged = False
            continue

        prev_start, prev_end, prev_text = merged[-1]
        combined_duration = end - prev_start
        combined_chars = len(re.sub(r"\s+", "", prev_text + text))
        should_merge = incomplete_ending.search(prev_text.strip())
        if prev_text.strip().endswith("待って"):
            should_merge = False

        if (
            should_merge
            and not last_was_merged
            and start - prev_end <= 0.5
            and combined_duration <= MAX_SUBTITLE_DURATION_SEC
            and combined_chars <= MAX_SUBTITLE_CHARS
        ):
            merged[-1] = (prev_start, end, prev_text.rstrip() + text.lstrip())
            last_was_merged = True
        else:
            merged.append((start, end, text))
            last_was_merged = False

    return merged


def retry_corrupt_asr_piece(model, input_path, piece, asr_prompt, previous_text=""):
    """重聽單一明顯壞句；只有錯誤分數確實下降才採用。"""
    start_sec, end_sec, original_text = piece
    original_score = asr_corruption_score(original_text)
    if not ASR_RETRY_CORRUPT_SEGMENTS or original_score <= 0:
        return piece, False

    media_duration = get_media_duration(input_path)
    retry_start = max(0.0, start_sec - ASR_RETRY_PADDING_SEC)
    retry_end = min(media_duration, end_sec + ASR_RETRY_PADDING_SEC)
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    retry_wav = handle.name
    handle.close()

    try:
        extract_audio_chunk(
            input_path,
            retry_start,
            retry_end - retry_start,
            retry_wav,
        )
        retry_segments, _ = model.transcribe(
            retry_wav,
            beam_size=ASR_RETRY_BEAM_SIZE,
            language="ja",
            vad_filter=False,
            condition_on_previous_text=True,
            initial_prompt=asr_prompt or None,
            temperature=0,
        )

        candidates = []
        for segment in retry_segments:
            global_start = retry_start + segment.start
            global_end = retry_start + segment.end
            midpoint = (global_start + global_end) / 2
            if start_sec <= midpoint <= end_sec:
                text = segment.text.strip()
                if text:
                    candidates.append(text)

        candidate_text = strip_asr_overlap(previous_text, " ".join(candidates))
        candidate_score = asr_corruption_score(candidate_text)
        if candidate_text and candidate_score < original_score:
            print(
                "\n[Whisper 自動補聽] "
                f"{format_timestamp(start_sec)} ~ {format_timestamp(end_sec)}\n"
                f"    原本：{original_text}\n"
                f"    補聽：{candidate_text}"
            )
            return (start_sec, end_sec, candidate_text), True
    except Exception as exc:
        print(f"\n[警告] Whisper 單句補聽失敗，保留原文：{exc}")
    finally:
        try:
            os.remove(retry_wav)
        except OSError:
            pass

    return piece, False


def transcribe_video(
    model,
    input_path,
    asr_prompt_terms=None
):

    asr_prompt = build_asr_prompt(
        asr_prompt_terms
    )

    common_kwargs = {
        # 3 比原本的 greedy/beam 1 穩定不少，但不像 beam 5 那樣明顯拖慢 CPU。
        "beam_size": 3,
        "language": "ja",
        # 這支影片全程有人聲與背景音樂，VAD 實測會吞掉大量真正台詞。
        "vad_filter": False,
        # 每個 30 秒區塊獨立解碼，避免錯誤文字跨區塊連鎖重複。
        "condition_on_previous_text": False,
        "initial_prompt":
            asr_prompt
            if asr_prompt
            else None
    }

    if CHUNK_LENGTH_SEC <= 0:

        segments, info = (
            model.transcribe(
                input_path,
                **common_kwargs
            )
        )

        results = []

        total_duration = round(
            info.duration,
            2
        )

        with tqdm(
            total=total_duration,
            unit="秒",
            desc="Whisper 聽寫進度"
        ) as pbar:

            for seg in segments:

                results.append((seg.start, seg.end, seg.text.strip()))

                pbar.update(
                    max(
                        0,
                        min(
                            round(
                                seg.end,
                                2
                            ),
                            total_duration
                        ) - pbar.n
                    )
                )

        return results

    total_duration = get_media_duration(
        input_path
    )

    results = []

    chunk_starts = list(
        range(
            0,
            int(total_duration) + 1,
            CHUNK_LENGTH_SEC
        )
    )

    with tqdm(
        total=round(total_duration, 2),
        unit="秒",
        desc="Whisper 聽寫進度"
    ) as pbar:

        for chunk_start in chunk_starts:

            owned_end = min(
                chunk_start + CHUNK_LENGTH_SEC,
                total_duration
            )
            extract_start = max(
                0,
                chunk_start - CHUNK_OVERLAP_SEC
            )
            extract_end = min(
                total_duration,
                owned_end + CHUNK_OVERLAP_SEC
            )
            chunk_len = extract_end - extract_start

            if chunk_len <= 0:
                continue

            tmp_wav = (
                f"_chunk_{chunk_start}.wav"
            )

            extract_audio_chunk(
                input_path,
                extract_start,
                chunk_len,
                tmp_wav
            )

            try:

                segments, _ = (
                    model.transcribe(
                        tmp_wav,
                        **common_kwargs
                    )
                )

                owned_pieces = []
                for seg in segments:
                    piece = (
                        seg.start + extract_start,
                        seg.end + extract_start,
                        seg.text.strip(),
                    )
                    midpoint = (piece[0] + piece[1]) / 2
                    # 重疊區由時間中點所在的正式 30 秒區塊負責，
                    # 因此既不漏掉切點句，也不會重複寫兩次。
                    if chunk_start <= midpoint < owned_end:
                        owned_pieces.append(piece)

                for piece in owned_pieces:
                    previous_text = results[-1][2] if results else ""
                    repaired_piece, _ = retry_corrupt_asr_piece(
                        model,
                        input_path,
                        piece,
                        asr_prompt,
                        previous_text,
                    )
                    results.append(repaired_piece)

                pbar.update(
                    max(0, round(owned_end, 2) - pbar.n)
                )

            finally:

                if os.path.exists(
                    tmp_wav
                ):
                    os.remove(
                        tmp_wav
                    )

    return results


def transcribe_video_vulkan(
    input_path,
    asr_prompt_terms=None,
    start_sec=0.0,
    duration_sec=None,
):
    """用 whisper.cpp + Vulkan 在 Windows AMD GPU 上聽寫。"""
    if not os.path.isfile(WHISPER_VULKAN_CLI):
        raise FileNotFoundError(f"找不到 Vulkan Whisper：{WHISPER_VULKAN_CLI}")
    if not os.path.isfile(WHISPER_VULKAN_MODEL):
        raise FileNotFoundError(f"找不到 Vulkan Medium 模型：{WHISPER_VULKAN_MODEL}")

    media_duration = get_media_duration(input_path)
    clip_start = max(0.0, float(start_sec or 0.0))
    available = max(0.0, media_duration - clip_start)
    clip_duration = (
        min(float(duration_sec), available)
        if duration_sec is not None
        else available
    )
    if clip_duration <= 0:
        raise ValueError("指定的 Whisper 時間範圍沒有影片內容")

    asr_prompt = build_asr_prompt(asr_prompt_terms)
    temp_dir = tempfile.mkdtemp(prefix="whisper_vulkan_", dir=APP_DIR)
    wav_path = os.path.join(temp_dir, "audio.wav")
    output_base = os.path.join(temp_dir, "result")
    srt_path = output_base + ".srt"

    try:
        print(
            f">>> 音訊範圍：{clip_start:.1f} ~ "
            f"{clip_start + clip_duration:.1f} 秒"
        )
        extract_audio_chunk(
            input_path,
            clip_start,
            clip_duration,
            wav_path,
        )

        cmd = [
            WHISPER_VULKAN_CLI,
            "-m", WHISPER_VULKAN_MODEL,
            "-f", wav_path,
            "-l", "ja",
            "-t", str(WHISPER_VULKAN_THREADS),
            "-bs", "3",
            "-bo", "3",
            # 不沿用上一段的文字，減少重複台詞與錯字連鎖。
            "-mc", "0",
            # RX 9000 系列先關閉 flash attention，穩定性優先。
            "-nfa",
            # whisper.cpp 在這裡用 UTF-8 bytes 計算長度；日文通常一字三 bytes。
            # 乘三才能接近上面的 34 個字，不會把「ご飯」切成「ご／飯」。
            "-ml", str(MAX_SUBTITLE_CHARS * 3),
            "-sow",
            "-osrt",
            "-of", output_base,
            "-pp",
        ]
        if asr_prompt:
            cmd.extend(["--prompt", asr_prompt])

        print(">>> 使用 AMD GPU：whisper.cpp Vulkan / Medium")
        subprocess.run(cmd, check=True)
        if not os.path.isfile(srt_path):
            raise RuntimeError("Vulkan Whisper 沒有產生 SRT")

        results = []
        for subtitle in parse_srt(srt_path):
            time_parts = re.split(r"\s*-->\s*", subtitle["time"])
            if len(time_parts) != 2:
                continue
            start = parse_srt_timestamp(time_parts[0]) + clip_start
            end = parse_srt_timestamp(time_parts[1]) + clip_start
            text = subtitle["text"].strip()
            if text:
                results.append((start, end, text))
        if not results:
            raise RuntimeError("Vulkan Whisper 產生了空白字幕")
        return results
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# 畫面字幕 OCR
# ============================================================

def _parse_vision_batch_response(raw: str, count: int):
    result = {}
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    for line in lines:
        match = re.match(r"^\s*\[?(\d+)\]?[：:\s]+(.+?)\s*$", line)
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= count:
            text = match.group(2).strip().strip('"「」')
            result[number] = "" if text.upper() in ("<NONE>", "NONE", "無") else text

    # 單張圖片時，模型偶爾只回字幕本身，仍可安全接受。
    if count == 1 and 1 not in result and len(lines) == 1:
        text = lines[0].strip().strip('"「」')
        result[1] = "" if text.upper() in ("<NONE>", "NONE", "無") else text

    return result


def recognize_subtitle_images(image_paths):
    prompt = (
        f"共有 {len(image_paths)} 張依序排列的影片畫面。"
        "讀取每張畫面底部最大、粗體、有黑色外框的日文對話字幕。"
        "忽略人物、聊天室、Logo、裝飾和固定介面。不要翻譯或改寫。"
        "每張必須輸出一行，格式為 [編號] 字幕原文；若沒有對話字幕則輸出"
        " [編號] <NONE>。編號從 1 開始，不要解釋。"
    )
    messages = [{
        "role": "user",
        "content": prompt,
        "images": image_paths,
    }]

    raw, err = call_with_hard_timeout(
        VISION_MODEL,
        messages,
        VISION_TIMEOUT,
        keep_alive=KEEP_ALIVE,
        num_predict=max(300, len(image_paths) * 80),
    )
    if err is not None or not raw:
        raise RuntimeError(f"畫面 OCR 失敗：{err}")

    parsed = _parse_vision_batch_response(raw, len(image_paths))
    missing = [i for i in range(1, len(image_paths) + 1) if i not in parsed]

    # 批次若漏掉某張，只重試漏掉的單張，不浪費整批時間。
    for number in missing:
        single_messages = [{
            "role": "user",
            "content": (
                "讀取畫面底部最大、粗體、有黑色外框的日文對話字幕。"
                "只輸出字幕原文；若沒有則輸出 <NONE>。不要翻譯或解釋。"
            ),
            "images": [image_paths[number - 1]],
        }]
        single_raw, single_err = call_with_hard_timeout(
            VISION_MODEL,
            single_messages,
            VISION_TIMEOUT,
            keep_alive=KEEP_ALIVE,
            num_predict=120,
        )
        if single_err is not None or not single_raw:
            raise RuntimeError(f"第 {number} 張畫面 OCR 失敗：{single_err}")
        parsed[number] = _parse_vision_batch_response(single_raw, 1).get(1, "")

    return [parsed[i] for i in range(1, len(image_paths) + 1)]


def _normalize_ocr_text(text: str):
    return re.sub(r"[\s、。！？!?～〜ー…・,，]", "", text or "")


def _ocr_texts_are_same(a: str, b: str):
    na = _normalize_ocr_text(a)
    nb = _normalize_ocr_text(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return min(len(na), len(nb)) >= 3
    return SequenceMatcher(None, na, nb).ratio() >= 0.88


def merge_ocr_observations(observations, interval, total_duration):
    # 單一影格偶爾漏讀時，以前後相同字幕補回，避免切成兩條。
    texts = [item[1] for item in observations]
    for i in range(1, len(texts) - 1):
        if not texts[i] and _ocr_texts_are_same(texts[i - 1], texts[i + 1]):
            texts[i] = texts[i - 1]

    merged = []
    current_text = ""
    current_start = 0.0
    current_end = 0.0

    for (timestamp, _), text in zip(observations, texts):
        if not text:
            if current_text:
                merged.append((current_start, max(current_end, timestamp), current_text))
                current_text = ""
            continue

        if current_text and _ocr_texts_are_same(current_text, text):
            if len(_normalize_ocr_text(text)) > len(_normalize_ocr_text(current_text)):
                current_text = text
            current_end = timestamp + interval
            continue

        if current_text:
            merged.append((current_start, timestamp, current_text))
        current_text = text
        current_start = timestamp
        current_end = timestamp + interval

    if current_text:
        merged.append((current_start, min(total_duration, current_end), current_text))

    return [item for item in merged if item[1] - item[0] >= 0.25 and item[2].strip()]


def step_visual_transcribe(start_sec=0.0, duration_sec=None, force=False):
    if os.path.exists(JA_SRT) and not force:
        print(f">>> 偵測到 {JA_SRT}，直接沿用。")
        return parse_srt(JA_SRT)

    media_duration = get_media_duration(INPUT_VIDEO)
    start_sec = max(0.0, float(start_sec or 0.0))
    available = max(0.0, media_duration - start_sec)
    duration = min(float(duration_sec), available) if duration_sec else available
    if duration <= 0:
        raise ValueError("指定的畫面 OCR 時間範圍沒有影片內容")

    temp_dir = tempfile.mkdtemp(prefix="vtuber_ocr_")
    frame_pattern = os.path.join(temp_dir, "frame_%06d.jpg")
    crop_filter = (
        f"fps={VISION_SAMPLE_FPS},"
        "crop=iw:trunc(ih*0.30/2)*2:0:ih-trunc(ih*0.30/2)*2"
    )
    cmd = [
        FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
        "-ss", str(start_sec), "-t", str(duration), "-i", INPUT_VIDEO,
        "-vf", crop_filter, "-q:v", "3", "-y", frame_pattern,
    ]

    try:
        print(f">>> 擷取畫面字幕區：{start_sec:.1f} ~ {start_sec + duration:.1f} 秒")
        subprocess.run(cmd, check=True)
        image_paths = sorted(
            os.path.join(temp_dir, name)
            for name in os.listdir(temp_dir)
            if name.lower().endswith(".jpg")
        )
        if not image_paths:
            raise RuntimeError("沒有擷取到任何 OCR 畫面")

        interval = 1.0 / VISION_SAMPLE_FPS
        observations = []
        for offset in tqdm(
            range(0, len(image_paths), VISION_BATCH_SIZE),
            desc="MiniCPM-V 畫面 OCR"
        ):
            batch = image_paths[offset:offset + VISION_BATCH_SIZE]
            texts = recognize_subtitle_images(batch)
            for local_index, text in enumerate(texts):
                timestamp = (offset + local_index) * interval
                observations.append((timestamp, text.strip()))

        segments = merge_ocr_observations(observations, interval, duration)
        with open(JA_SRT, "w", encoding="utf-8") as f:
            for index, (start, end, text) in enumerate(segments, start=1):
                f.write(
                    f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n"
                    f"{text}\n\n"
                )

        print(f">>> 畫面 OCR 完成：{JA_SRT}（{len(segments)} 句）")
        return parse_srt(JA_SRT)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# Transcribe Step
# ============================================================

def step_transcribe(
    glossary,
    force=False,
    asr_prompt_terms=None,
    start_sec=0.0,
    duration_sec=None,
):

    if os.path.exists(JA_SRT) and not force:

        print(
            f">>> 偵測到 {JA_SRT}，"
            "直接沿用。"
        )

        return parse_srt(
            JA_SRT
        )

    raw_segments = None
    if WHISPER_BACKEND == "vulkan":
        try:
            raw_segments = transcribe_video_vulkan(
                INPUT_VIDEO,
                asr_prompt_terms,
                start_sec=start_sec,
                duration_sec=duration_sec,
            )
        except Exception as exc:
            print(f"\n[警告] AMD Vulkan 聽寫失敗：{exc}")
            print(">>> 自動退回 CPU faster-whisper Medium。")

    if raw_segments is None:
        if start_sec or duration_sec is not None:
            clip_start = max(0.0, float(start_sec or 0.0))
            available = max(0.0, get_media_duration(INPUT_VIDEO) - clip_start)
            clip_duration = (
                min(float(duration_sec), available)
                if duration_sec is not None
                else available
            )
            fallback_wav = os.path.join(APP_DIR, "_whisper_cpu_test.wav")
            extract_audio_chunk(INPUT_VIDEO, clip_start, clip_duration, fallback_wav)
            cpu_input = fallback_wav
        else:
            clip_start = 0.0
            fallback_wav = None
            cpu_input = INPUT_VIDEO

        print(">>> 載入 Whisper Medium（CPU）...")
        model = WhisperModel(
            MODEL_DIR,
            device="cpu",
            compute_type="int8",
            cpu_threads=16,
        )
        try:
            raw_segments = transcribe_video(
                model,
                cpu_input,
                asr_prompt_terms,
            )
            if clip_start:
                raw_segments = [
                    (start + clip_start, end + clip_start, text)
                    for start, end, text in raw_segments
                ]
        finally:
            if fallback_wav and os.path.exists(fallback_wav):
                os.remove(fallback_wav)

    raw_segments = merge_incomplete_asr_fragments(raw_segments)

    check_transcript_gaps(raw_segments)

    subtitles = []

    with open(
        JA_SRT,
        "w",
        encoding="utf-8"
    ) as f:

        for i, (
            start_sec,
            end_sec,
            text
        ) in enumerate(
            raw_segments,
            start=1
        ):

            start = format_timestamp(
                start_sec
            )

            end = format_timestamp(
                end_sec
            )

            f.write(
                f"{i}\n"
                f"{start} --> {end}\n"
                f"{text}\n\n"
            )

            subtitles.append({
                "index": str(i),
                "time":
                    f"{start} --> {end}",
                "text": text
            })

    return subtitles


def check_transcript_gaps(raw_segments, gap_threshold_sec: float = 30.0):
    """
    檢查聽寫結果有沒有「整段消失」的狀況（Whisper 偶爾會在長片段中途
    當機式放棄，後面沒辨識完的內容整段不見、也不會報任何錯誤）。
    掃描所有相鄰片段之間的時間間隔，超過門檻就印出明確警告，
    不用像手動比對時間軸那樣土法煉鋼才找得到。
    """
    if not raw_segments:
        return

    warnings = []
    suspicious_segments = []
    corrupt_segments = []

    for start, end, text in raw_segments:
        if asr_corruption_score(text) > 0:
            corrupt_segments.append((start, end, text))
        duration = end - start
        # 正常 Whisper 字幕通常只有數秒。超長但字很少，多半是時間戳被
        # 音樂/靜音拉長，或整段只辨識到一小部分。
        chars_per_sec = len(text.strip()) / duration if duration > 0 else 0
        if duration >= 20.0 and chars_per_sec < 1.5:
            suspicious_segments.append((start, end, duration, text))

    for i in range(1, len(raw_segments)):
        prev_end = raw_segments[i - 1][1]
        curr_start = raw_segments[i][0]
        gap = curr_start - prev_end
        if gap > gap_threshold_sec:
            warnings.append((prev_end, curr_start, gap))

    if warnings:
        print(f"\n[警告] 偵測到 {len(warnings)} 處疑似漏段（間隔超過 {gap_threshold_sec:.0f} 秒沒有任何字幕）：")
        for prev_end, curr_start, gap in warnings:
            print(
                f"    {format_timestamp(prev_end)} ~ {format_timestamp(curr_start)}"
                f"（消失了約 {gap/60:.1f} 分鐘）"
            )
        print(
            "    這些區間很可能是 Whisper 中途放棄辨識，內容整段遺漏。\n"
            "    建議：手動檢查這幾段對應的原始影片內容，必要時可以縮短\n"
            "    CHUNK_LENGTH_SEC 後刪掉 output_ja.srt 重新聽寫。"
        )

    if suspicious_segments:
        print(f"\n[警告] 偵測到 {len(suspicious_segments)} 條異常超長字幕：")
        for start, end, duration, text in suspicious_segments:
            preview = text[:45] + ("…" if len(text) > 45 else "")
            print(
                f"    {format_timestamp(start)} ~ {format_timestamp(end)} "
                f"（{duration:.1f} 秒）：{preview}"
            )
        print("    這通常代表時間戳拉長或區間內有漏聽，建議優先人工抽查。")

    if corrupt_segments:
        print(f"\n[警告] 仍有 {len(corrupt_segments)} 句含突兀英文或解碼亂碼：")
        for start, end, text in corrupt_segments:
            print(
                f"    {format_timestamp(start)} ~ {format_timestamp(end)}：{text}"
            )
        print("    這些句子不適合直接翻譯，請先核對 output_ja.srt。")


# ============================================================
# Existing Translation
# ============================================================

def load_existing_translations(
    zh_srt_path
):

    if not os.path.exists(
        zh_srt_path
    ):
        return {}

    done = {}

    for sub in parse_srt(
        zh_srt_path
    ):

        done[
            sub["index"]
        ] = sub["text"]

    return done


def finalize_translation_text(index, curr_txt, translated_text, glossary):
    """把模型譯文轉成可直接寫入 SRT 的台灣繁中，並保護術語。"""
    traditional = convert_to_traditional(translated_text)
    final_text = localize_vtuber_style(traditional)
    matched_entries = get_matched_glossary_entries(glossary, curr_txt)

    protected_terms = [g["dst"] for g in matched_entries]
    if any(
        term in traditional and term not in final_text
        for term in protected_terms
    ):
        print(f"\n[警告] 第 {index} 句：Style 修改了受保護術語，退回原譯文。")
        final_text = traditional

    for g in matched_entries:
        src = g["src"]
        dst = g["dst"]
        if dst in final_text:
            continue
        if src in final_text:
            final_text = final_text.replace(src, dst)
            print(f"\n[glossary 強制替換] 第 {index} 句：{src} -> {dst}")
            continue

        fixed_text, fixed = fuzzy_fix_glossary_term(final_text, dst)
        if fixed:
            final_text = fixed_text
            print(f"\n[glossary 模糊修正] 第 {index} 句：已修正為「{dst}」")
        else:
            print(
                f"\n[警告] 第 {index} 句：術語「{src}->{dst}」未出現在最終翻譯，"
                f"請人工核對：\nJA: {curr_txt}\nZH: {final_text}"
            )

    return final_text


def append_translation_batch(batch, translated, glossary):
    """每批完成立即寫入 SRT，確保中斷時成果仍可見、可續跑。"""
    mode = "a" if os.path.exists(ZH_SRT) else "w"
    with open(ZH_SRT, mode, encoding="utf-8") as f_zh:
        for sub in batch:
            index = str(sub["index"])
            final_text = finalize_translation_text(
                index,
                sub["text"],
                translated[index],
                glossary,
            )
            f_zh.write(
                f"{index}\n{sub['time']}\n{final_text}\n\n"
            )
        f_zh.flush()
        try:
            os.fsync(f_zh.fileno())
        except Exception:
            pass


# ============================================================
# OpenAI GPT 增強校對
# ============================================================

def _extract_openai_output_text(response):
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"].strip()
    return ""


def _openai_subtitle_request(api_key, model, payload, expected_indexes):
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "string"},
                        "ja": {"type": "string"},
                        "zh": {"type": "string"},
                    },
                    "required": ["index", "ja", "zh"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    body = json.dumps({
        "model": model,
        "instructions": GPT_ENHANCEMENT_PROMPT,
        "input": json.dumps(payload, ensure_ascii=False),
        "reasoning": {"effort": "none"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "subtitle_review",
                "strict": True,
                "schema": schema,
            },
        },
        "store": False,
        "max_output_tokens": max(3000, len(expected_indexes) * 220),
    }, ensure_ascii=False).encode("utf-8")

    last_error = None
    for attempt in range(1, OPENAI_MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(
                OPENAI_API_ENDPOINT,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
            output_text = _extract_openai_output_text(result)
            if not output_text:
                raise ValueError("GPT 沒有回傳字幕內容")
            parsed = json.loads(output_text)
            items = parsed.get("items", [])
            by_index = {
                str(item.get("index")): item
                for item in items
                if isinstance(item, dict)
            }
            if set(by_index) != set(expected_indexes):
                raise ValueError(
                    f"GPT 回傳編號不完整：預期 {expected_indexes}，實際 {sorted(by_index)}"
                )
            for index, item in by_index.items():
                if not str(item.get("ja", "")).strip() or not str(item.get("zh", "")).strip():
                    raise ValueError(f"GPT 第 {index} 句是空白")
            return by_index
        except Exception as error:
            last_error = error
            detail = ""
            if hasattr(error, "read"):
                try:
                    detail = error.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
            print(f"\n[GPT 增強] 第 {attempt} 次失敗：{error} {detail}".rstrip())
            if attempt < OPENAI_MAX_RETRIES:
                time.sleep(min(20, 2 ** attempt) + random.uniform(0.2, 1.0))
    raise RuntimeError(f"GPT 增強無法完成：{last_error}")


def _gpt_cache_key(model, rows, glossary):
    payload = json.dumps(
        {
            "model": model,
            "prompt": GPT_ENHANCEMENT_PROMPT,
            "rows": rows,
            "glossary": glossary,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def step_gpt_enhance(glossary, api_key, model=OPENAI_GPT_MODEL):
    if not api_key:
        print("[錯誤] GPT 增強需要 OpenAI API Key。")
        return False
    if not os.path.exists(JA_SRT) or not os.path.exists(ZH_SRT):
        print(f"[錯誤] GPT 增強需要 {JA_SRT} 與 {ZH_SRT}。")
        return False

    ja_subtitles = parse_srt(JA_SRT)
    zh_subtitles = parse_srt(ZH_SRT)
    if [item["index"] for item in ja_subtitles] != [item["index"] for item in zh_subtitles]:
        print("[錯誤] 日文與中文字幕編號不一致，不能執行 GPT 增強。")
        return False

    cache = load_json_dict(GPT_ENHANCEMENT_CACHE_PATH)
    reviewed = {}
    all_rows = [
        {
            "index": ja["index"],
            "ja": ja["text"],
            "draft_zh": zh["text"],
        }
        for ja, zh in zip(ja_subtitles, zh_subtitles)
    ]
    glossary_rows = [
        {"src": item["src"], "dst": item["dst"]}
        for item in glossary
        if item.get("src") and item.get("dst")
    ]

    print(f">>> GPT 增強模型：{model}")
    for start in tqdm(range(0, len(all_rows), OPENAI_BATCH_SIZE), desc="GPT 增強校對"):
        end = min(len(all_rows), start + OPENAI_BATCH_SIZE)
        context_start = max(0, start - 2)
        context_end = min(len(all_rows), end + 2)
        target_indexes = [row["index"] for row in all_rows[start:end]]
        context_rows = all_rows[context_start:context_end]
        cache_key = _gpt_cache_key(model, context_rows, glossary_rows)
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and set(cached) == set(target_indexes):
            batch_result = cached
        else:
            payload = {
                "glossary": glossary_rows,
                "target_indexes": target_indexes,
                "subtitles": context_rows,
            }
            try:
                batch_result = _openai_subtitle_request(
                    api_key, model, payload, target_indexes
                )
            except RuntimeError as error:
                print(f"\n[錯誤] {error}")
                print(">>> 已完成的 GPT 批次保存在快取，重跑可接續。")
                return False
            cache[cache_key] = batch_result
            save_json(GPT_ENHANCEMENT_CACHE_PATH, cache)
        reviewed.update(batch_result)

    ja_temp = JA_SRT + ".gpt.tmp"
    zh_temp = ZH_SRT + ".gpt.tmp"
    with open(ja_temp, "w", encoding="utf-8") as ja_file, open(
        zh_temp, "w", encoding="utf-8"
    ) as zh_file:
        for ja, zh in zip(ja_subtitles, zh_subtitles):
            item = reviewed[ja["index"]]
            final_ja = str(item["ja"]).strip()
            final_zh = finalize_translation_text(
                ja["index"], final_ja, str(item["zh"]).strip(), glossary
            )
            ja_file.write(f"{ja['index']}\n{ja['time']}\n{final_ja}\n\n")
            zh_file.write(f"{zh['index']}\n{zh['time']}\n{final_zh}\n\n")
    os.replace(ja_temp, JA_SRT)
    os.replace(zh_temp, ZH_SRT)
    print(f">>> GPT 增強完成：{len(reviewed)} 句")
    return True


# ============================================================
# Translate Step
# ============================================================

def step_translate(
    glossary,
    limit=None
):

    if not os.path.exists(
        JA_SRT
    ):

        print(
            f"[錯誤] 找不到 {JA_SRT}"
        )

        return False

    subtitles = parse_srt(
        JA_SRT
    )

    if limit:

        subtitles = subtitles[
            :limit
        ]

        print(
            f">>> 測試模式："
            f"只翻前 {limit} 句"
        )

    already_done = (
        load_existing_translations(
            ZH_SRT
        )
    )

    print(
        f">>> 已完成："
        f"{len(already_done)} 句"
    )

    # ----------------------------
    # 模型暖機
    # ----------------------------

    if TRANSLATE_BACKEND == "qwen":
        warmup_model(
            LOCAL_TRANSLATE_MODEL,
            WARMUP_TIMEOUT,
            think=False
        )
    else:
        warmup_model(
            OLLAMA_MODEL,
            WARMUP_TIMEOUT
        )

    if USE_STYLE_PASS:
        warmup_model(
            STYLE_MODEL,
            WARMUP_TIMEOUT
        )

    if opencc is None:

        print(
            "[警告] 沒有 OpenCC。"
            "請安裝："
            "pip install opencc-python-reimplemented"
        )

    history_pairs = []
    batch_results = {}
    translation_cache = (
        load_json_dict(LOCAL_TRANSLATION_CACHE_PATH)
        if TRANSLATE_BACKEND == "qwen"
        else {}
    )

    def translate_batch_with_cache(items, current_history):
        translated = {}
        uncached = []

        for item in items:
            cached = translation_cache.get(translation_cache_key(item["text"]))
            if isinstance(cached, str) and cached.strip():
                translated[str(item["index"])] = cached
            else:
                uncached.append(item)

        if uncached:
            fresh = local_translate_batch(
                current_history, uncached, glossary
            )
            translated.update(fresh)
            if TRANSLATE_BACKEND != "sakura":
                for item in uncached:
                    translation_cache[translation_cache_key(item["text"])] = fresh[
                        str(item["index"])
                    ]
                save_json(LOCAL_TRANSLATION_CACHE_PATH, translation_cache)

        return translated

    # 主翻譯先以小段落批次處理。這裡仍按照字幕原順序建立 history，
    # 所以斷點續跑時，已完成的字幕一樣會成為後續批次的前文。
    if TRANSLATE_BACKEND in ("qwen", "sakura"):
        pending_batch = []
        batch_history = []
        total_pending = sum(
            1 for sub in subtitles if sub["index"] not in already_done
        )

        backend_label = {
            "sakura": "Sakura 本地批次翻譯",
            "qwen": "Qwen 本地批次翻譯",
        }[TRANSLATE_BACKEND]
        with tqdm(total=total_pending, desc=backend_label) as batch_bar:
            for sub in subtitles:
                index = sub["index"]
                if index in already_done:
                    batch_history.append((sub["text"], already_done[index]))
                    continue

                pending_batch.append(sub)
                batch_size = LOCAL_TRANSLATE_BATCH_SIZE
                if len(pending_batch) < batch_size:
                    continue

                try:
                    translated = translate_batch_with_cache(
                        pending_batch, batch_history
                    )
                except RuntimeError as e:
                    print(f"\n[錯誤] {e}")
                    print(">>> 已完成的字幕已寫入 output_zh.srt，稍後可直接續跑。")
                    return False
                append_translation_batch(
                    pending_batch, translated, glossary
                )
                batch_results.update(translated)
                for item in pending_batch:
                    batch_history.append(
                        (item["text"], translated[str(item["index"])])
                    )
                batch_bar.update(len(pending_batch))
                pending_batch = []

            if pending_batch:
                try:
                    translated = translate_batch_with_cache(
                        pending_batch, batch_history
                    )
                except RuntimeError as e:
                    print(f"\n[錯誤] {e}")
                    print(">>> 已完成的字幕已寫入 output_zh.srt，稍後可直接續跑。")
                    return False
                append_translation_batch(
                    pending_batch, translated, glossary
                )
                batch_results.update(translated)
                for item in pending_batch:
                    batch_history.append(
                        (item["text"], translated[str(item["index"])])
                    )
                batch_bar.update(len(pending_batch))

        # 批次模式已在每一批完成時直接寫入 output_zh.srt，不要再走下面的
        # 舊逐句流程，否則會重複寫入相同字幕。
        return True

    mode = (
        "a"
        if already_done
        else "w"
    )

    with open(
        ZH_SRT,
        mode,
        encoding="utf-8"
    ) as f_zh:

        for position, sub in enumerate(tqdm(
            subtitles,
            desc="翻譯進度"
        )):

            index = sub["index"]

            curr_txt = (
                sub["text"]
            )

            # ------------------------
            # 斷點續跑
            # ------------------------

            if index in already_done:

                history_pairs.append(
                    (
                        curr_txt,
                        already_done[index]
                    )
                )

                continue

            # ------------------------
            # 1. 本地主翻譯
            # ------------------------

            if TRANSLATE_BACKEND in ("qwen", "sakura"):
                sakura = batch_results.get(index, curr_txt)
            else:
                sakura = translate_sakura(
                    history_pairs,
                    curr_txt,
                    glossary
                )

            history_pairs.append(
                (
                    curr_txt,
                    sakura
                )
            )

            proofread = sakura

            # ------------------------
            # 4. 簡轉繁
            # ------------------------

            traditional = (
                convert_to_traditional(
                    proofread
                )
            )

            # ------------------------
            # 5. VTuber 口語化
            # ------------------------

            final_text = (
                localize_vtuber_style(
                    traditional
                )
            )

            # ------------------------
            # 6. glossary 保護 + 強制檢查
            # ------------------------

            matched_entries = (
                get_matched_glossary_entries(
                    glossary,
                    curr_txt
                )
            )

            protected_terms = [
                g["dst"]
                for g in matched_entries
            ]

            broken = any(
                term in traditional
                and term not in final_text
                for term in protected_terms
            )

            if broken:

                print(
                    f"\n[警告] 第 {index} 句："
                    "Style 模型修改了受保護術語，"
                    "退回 Style 前版本。"
                )

                final_text = traditional

            # 就算沒被 Style 破壞，也可能從一開始
            # Sakura 從一開始就可能漏掉術語。
            # 逐一強制檢查，缺漏就先嘗試直接替換，
            # 換不了就印警告，方便回頭核對 glossary
            # 的 src 拼法是否跟 output_ja.srt 一致。

            for g in matched_entries:

                src = g["src"]
                dst = g["dst"]

                if dst in final_text:
                    continue

                if src in final_text:

                    final_text = final_text.replace(
                        src,
                        dst
                    )

                    print(
                        f"\n[glossary 強制替換] "
                        f"第 {index} 句："
                        f"{src} -> {dst}"
                    )

                    continue

                fixed_text, fixed = fuzzy_fix_glossary_term(
                    final_text,
                    dst
                )

                if fixed:

                    print(
                        f"\n[glossary 模糊修正] "
                        f"第 {index} 句："
                        f"疑似打錯字，已修正為「{dst}」"
                    )

                    final_text = fixed_text

                else:

                    print(
                        f"\n[警告] 第 {index} 句："
                        f"術語「{src}->{dst}」"
                        "未出現在最終翻譯，"
                        "且找不到原文可直接替換，"
                        "請人工核對："
                        f"\nJA: {curr_txt}"
                        f"\nZH: {final_text}"
                    )

            # ------------------------
            # 寫入
            # ------------------------

            f_zh.write(
                f"{index}\n"
                f"{sub['time']}\n"
                f"{final_text}\n\n"
            )

            f_zh.flush()

            try:
                os.fsync(
                    f_zh.fileno()
                )
            except Exception:
                pass

    return True


# ============================================================
# Burn Subtitle
# ============================================================

def step_burn(clip_start=None, clip_duration=None):

    if not os.path.exists(
        ZH_SRT
    ):

        print(
            f"[錯誤] 找不到 {ZH_SRT}"
        )

        return False

    if not os.path.exists(JA_SRT):
        print(f"[錯誤] 找不到 {JA_SRT}，無法確認中文字幕是否完整。")
        return False

    ja_subtitles = parse_srt(JA_SRT)
    zh_subtitles = parse_srt(ZH_SRT)
    ja_indexes = [sub["index"] for sub in ja_subtitles]
    zh_indexes = [sub["index"] for sub in zh_subtitles]

    if zh_indexes != ja_indexes:
        missing = [index for index in ja_indexes if index not in set(zh_indexes)]
        print(
            f"[錯誤] {ZH_SRT} 尚未翻譯完整："
            f"日文 {len(ja_indexes)} 句，中文 {len(zh_indexes)} 句。"
        )
        if missing:
            preview = ", ".join(missing[:10])
            suffix = "..." if len(missing) > 10 else ""
            print(f"       缺少字幕編號：{preview}{suffix}")
        print(">>> 不會執行 FFmpeg，請先完成翻譯。")
        return False

    print(
        ">>> 使用 FFmpeg 壓制繁中字幕..."
    )

    vf_option = (
        f"subtitles={ZH_SRT}:"
        "force_style="
        "'FontSize=20,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=2'"
    )

    cmd = [FFMPEG_EXE, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    if clip_start is not None:
        cmd.extend(["-ss", str(clip_start)])
    if clip_duration is not None:
        cmd.extend(["-t", str(clip_duration)])
    cmd.extend([
        "-i", INPUT_VIDEO,
        "-vf",
        vf_option,
        "-c:a",
        "copy",
        OUTPUT_VIDEO
    ])

    subprocess.run(
        cmd,
        check=True
    )

    print(
        f">>> 完成："
        f"{OUTPUT_VIDEO}"
    )

    return True


# ============================================================
# Main Pipeline
# ============================================================

def run_pipeline(
    step="all",
    limit=None,
    force_transcribe=False,
    mode="audio",
    start_sec=0.0,
    duration_sec=None,
    gpt_enhance=False,
    openai_api_key="",
    gpt_model=OPENAI_GPT_MODEL,
):

    global JA_SRT, ZH_SRT, OUTPUT_VIDEO

    if duration_sec:
        start_label = f"{start_sec:g}".replace(".", "p")
        duration_label = f"{duration_sec:g}".replace(".", "p")
        prefix = (
            f"test_{duration_label}s"
            if float(start_sec) == 0
            else f"test_{start_label}s_{duration_label}s"
        )
        JA_SRT = f"{prefix}_ja.srt"
        ZH_SRT = f"{prefix}_zh.srt"
        OUTPUT_VIDEO = f"{prefix}_subtitled.mp4"

    if step == "glossary":

        load_glossary(
            GLOSSARY_PATH
        )

        return

    common_glossary = load_glossary(GLOSSARY_PATH)
    video_profile = load_video_glossary(VIDEO_GLOSSARY_PATH)
    glossary = merge_glossaries(
        common_glossary,
        video_profile["glossary"],
    )
    common_asr_terms = get_common_asr_prompt_terms(common_glossary)
    asr_prompt_terms = list(dict.fromkeys(
        common_asr_terms + video_profile["asr_prompt_terms"]
    ))

    print(
        f">>> Glossary：共用 {len(common_glossary)} 條"
        f"／本片 {len(video_profile['glossary'])} 條"
    )
    if asr_prompt_terms:
        print(
            f">>> Whisper 提示詞：共用安全名稱 {len(common_asr_terms)} 條"
            f"／本片追加 {len(video_profile['asr_prompt_terms'])} 條"
        )
    else:
        print(">>> Whisper 本片提示詞：0 條（乾淨通用模式）")

    # ----------------------------
    # Step 1
    # ----------------------------

    if step in (
        "transcribe",
        "all"
    ):

        print(
            "\n=========="
        )

        print(
            "[1/3] MiniCPM-V 畫面字幕 OCR"
            if mode == "visual"
            else "[1/3] Whisper 聽寫"
        )

        print(
            "=========="
        )

        if mode == "visual":
            step_visual_transcribe(
                start_sec=start_sec,
                duration_sec=duration_sec,
                force=force_transcribe
            )
        else:
            step_transcribe(
                glossary,
                force=force_transcribe,
                asr_prompt_terms=asr_prompt_terms,
                start_sec=start_sec,
                duration_sec=duration_sec,
            )

    # ----------------------------
    # Step 2
    # ----------------------------

    if step in (
        "translate",
        "all"
    ):

        print(
            "\n=========="
        )

        print(
            "[2/3] 翻譯"
        )

        print(
            "=========="
        )

        translation_ok = step_translate(
            glossary,
            limit=limit
        )
        if not translation_ok:
            print("\n>>> Pipeline 已停止：翻譯未完成，不會進行字幕壓制。")
            return False

        if gpt_enhance:
            print(
                "\n==========\n"
                "[GPT] 增強校對\n"
                "=========="
            )
            if not step_gpt_enhance(glossary, openai_api_key, gpt_model):
                print("\n>>> Pipeline 已停止：GPT 增強未完成，不會進行字幕壓制。")
                return False

    # ----------------------------
    # Step 3
    # ----------------------------

    if step in (
        "burn",
        "all"
    ):

        print(
            "\n=========="
        )

        print(
            "[3/3] FFmpeg 壓字幕"
        )

        print(
            "=========="
        )

        burn_ok = step_burn(
            clip_start=start_sec if duration_sec else None,
            clip_duration=duration_sec
        )
        if not burn_ok:
            print("\n>>> Pipeline 已停止：字幕壓制未完成。")
            return False

    # ----------------------------
    # Done
    # ----------------------------

    if step == "all":

        print(
            "\n"
            "===================================="
        )

        print(
            "處理完成！"
        )

        print(
            f"日文字幕：{JA_SRT}"
        )

        print(
            f"繁中字幕：{ZH_SRT}"
        )

        print(
            f"影片：{OUTPUT_VIDEO}"
        )

        print(
            "===================================="
        )

    return True


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    import argparse
    import getpass

    frozen_interactive = getattr(sys, "frozen", False) and len(sys.argv) == 1
    if getattr(sys, "frozen", False):
        os.chdir(APP_DIR)

    if frozen_interactive:
        print(
            "====================================\n"
            " VTuber 影片字幕翻譯器\n"
            "====================================\n"
            "1. 本機模式（Whisper + Sakura，可離線）\n"
            "2. GPT 增強模式（Sakura 初稿 + GPT 全片校對）\n"
            "3. 離開\n"
        )
        choice = input("請輸入 1、2 或 3：").strip()
        if choice == "2":
            sys.argv.append("--gpt-enhance")
        elif choice != "1":
            raise SystemExit(0)

    parser = argparse.ArgumentParser(
        description=(
            "VTuber 日文字幕翻譯 Pipeline\n"
            "Whisper 聽寫 -> Sakura 翻譯 -> 可選 GPT 增強 -> FFmpeg"
        )
    )

    parser.add_argument(
        "--step",
        choices=[
            "all",
            "transcribe",
            "translate",
            "burn",
            "glossary"
        ],
        default="all"
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "翻譯測試模式，例如 --limit 30"
        )
    )

    parser.add_argument(
        "--force-transcribe",
        action="store_true",
        help="忽略既有 output_ja.srt，使用逐字時間戳重新聽寫與切句"
    )

    parser.add_argument(
        "--mode",
        choices=["audio", "visual"],
        default="audio",
        help="audio 使用 Whisper；visual 使用 MiniCPM-V 讀取畫面日文字幕"
    )

    parser.add_argument(
        "--start",
        type=float,
        default=0.0,
        help="測試片段的開始秒數，預設 0"
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="audio/visual 都只處理指定秒數，例如 --duration 60"
    )

    parser.add_argument(
        "--gpt-enhance",
        action="store_true",
        help="Sakura 翻譯完成後，使用 OpenAI GPT 做全片增強校對"
    )

    parser.add_argument(
        "--gpt-model",
        default=OPENAI_GPT_MODEL,
        help=f"GPT 增強使用的模型，預設 {OPENAI_GPT_MODEL}"
    )

    args = parser.parse_args()

    api_key = OPENAI_API_KEY
    if args.gpt_enhance and not api_key:
        print(
            "\nGPT 增強需要 OpenAI API Key。"
            "輸入內容只會留在記憶體，不會寫進任何檔案。"
        )
        api_key = getpass.getpass("請貼上 API Key（輸入時不會顯示）：").strip()

    success = run_pipeline(
        step=args.step,
        limit=args.limit,
        force_transcribe=args.force_transcribe,
        mode=args.mode,
        start_sec=args.start,
        duration_sec=args.duration,
        gpt_enhance=args.gpt_enhance,
        openai_api_key=api_key,
        gpt_model=args.gpt_model,
    )

    if frozen_interactive:
        input("\n處理完成，按 Enter 關閉視窗……" if success else "\n處理未完成，按 Enter 關閉視窗……")
