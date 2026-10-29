#!/usr/bin/env python3
"""
Writing Assistant 6 - system-wide spelling & grammar helper powered by a local T5 model.

Runs in the background and checks the WHOLE text field you are in (not just what
you typed): it reads the focused field through the OS accessibility APIs, so text
that was already there, pasted text, autocorrect and mouse edits are all seen, and
every fix is verified against the real field before and after it is applied.
Where an app doesn't expose its text, it falls back to following your typing, and
Ctrl+Alt+A can scan such a field on demand. A small floating card (plus in-place
underlines where possible) offers fixes you apply by click or hotkey.

The brain is AventIQ-AI/T5-small-grammar-correction (Hugging Face), run locally with
PyTorch. Each sentence is rewritten by the model and the differences become
suggestions. Nothing you type leaves your machine: the only network access is the
one-off model download (~120 MB, then cached by Hugging Face and used offline).
Field text is kept only in memory (wiped after a few idle minutes), never written
to disk, and password fields are never read.

What changed in v6
  * The hand-written rules, word lists and typo tables are gone. The T5 model is the
    only grammar/spelling engine; check_text() is still a pure function of the text.
  * The model's edits pass through guard rails: numbers, URLs/code, acronyms, names
    and words in your dictionary are never changed; if the model rewrites a sentence
    too heavily it is ignored; "Fix all" only applies small, high-confidence fixes
    (capital letters, apostrophes, one-letter typos).
  * Sentences are cached, so unchanged text is never re-checked, and long texts are
    checked progressively (nearest the caret first) so typing never stalls.
  * Kept: five exact whitespace/punctuation-spacing checks (double space, space
    before a comma, ...) because the model cannot see whitespace.

Install:   pip install pynput transformers torch sentencepiece
Optional:  pip install pyspellchecker  (labels typos as "spelling" instead of "grammar")
           pip install screeninfo     (multi-monitor placement)
           Windows: pip install uiautomation   (reads browsers, Word, modern apps)
           macOS underlines: pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa
Without the model's requirements the app still runs, but only the spacing checks work.

Hotkeys (hold the modifiers from config, default Ctrl+Alt):
  1 / 2 / 3  apply suggestion     N / B  next / previous issue
  D  ignore                        W  add word to dictionary
  F  fix all (safe fixes only)     P  pause / resume
  C  collapse / expand card        V  check the clipboard text
  A  scan this field now (copies it via Ctrl+A/Ctrl+C - only for apps that can't be read;
     your clipboard is restored and the caret ends at the end of the field)
  O  open dictionary folder        Q  quit

Dictionaries: ~/WritingAssistant/dictionaries/ - every .txt/.dic/.csv file is
loaded (one word per line, '#' comments; Hunspell and "word count" lists work)
and reloaded automatically. `python writing_assistant.py --import list.txt`.
Settings live in ~/WritingAssistant/config.json.

Limits worth knowing: which apps can be read depends on the OS. Windows: classic
edit controls always; browsers/Word/modern apps need `pip install uiautomation`.
macOS: native Cocoa text fields/views (grant Accessibility permission); Electron
and some browsers don't expose text. Linux: no field reading yet (typed tracking +
Ctrl+Alt+A). For unreadable fields it follows your typing and resets whenever it
cannot be sure where your caret is. Wayland-native apps do not deliver key events.
"""
from __future__ import annotations

import difflib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:                       # headless / no Tk: engine + --check still work
    tk = tkfont = None
try:
    from pynput import keyboard, mouse
except Exception:
    keyboard = mouse = None

VERSION = "6.0"
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
DEBUG = "--debug" in sys.argv or bool(os.environ.get("WA_DEBUG"))


def dbg(*a):
    """Debug log. Never prints typed characters."""
    if DEBUG:
        print("[wa]", *a, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
HOME_DIR = Path.home() / "WritingAssistant"
DICT_DIR = HOME_DIR / "dictionaries"
MY_WORDS = DICT_DIR / "my_words.txt"
CONFIG_PATH = HOME_DIR / "config.json"
DICT_EXTS = {".txt", ".dic", ".csv", ".tsv"}

DEFAULT_IGNORE_APPS = [
    # terminals (typing there is commands, and cursor injection is unsafe)
    "cmd", "powershell", "pwsh", "windowsterminal", "wt", "conhost", "terminal",
    "iterm2", "iterm", "gnome-terminal", "gnome-terminal-server", "konsole", "xterm",
    "alacritty", "kitty", "wezterm", "wezterm-gui", "tilix", "ghostty", "putty",
    # password managers / secrets
    "keepass", "keepassxc", "1password", "bitwarden", "lastpass", "dashlane",
    "enpass", "keychain access", "passwords",
]
DEFAULTS = {
    "idle_check_seconds": 0.5,        # re-check this long after the last keystroke
    "check_trailing_word_seconds": 1.2,  # ...and include the word you are still typing
    "forget_after_seconds": 300,      # wipe the in-memory buffer after this much idle
    "enter_resets": True,             # Enter starts a fresh buffer (safe for chat apps)
    "model_name": "AventIQ-AI/T5-small-grammar-correction",   # Hugging Face id (or a local folder)
    "model_device": "auto",           # auto | cpu | cuda | mps
    "num_beams": 5,                   # beam-search width (the model card uses 5); 1 = faster
    "max_check_seconds": 3.0,         # model time per pass: long texts are checked progressively
    "underlines": True,               # in-place marks where the app exposes its caret
    "theme": "dark",                  # dark | light
    "hotkey_modifiers": ["ctrl", "alt"],
    "ignore_apps": DEFAULT_IGNORE_APPS,
    "max_buffer": 1800,               # typed-only tracking window (apps whose text can't be read)
    "read_field": True,               # read the focused field via accessibility APIs (ground truth)
    "max_field_chars": 8000,          # analyse at most this much of one field (window around the caret)
    "windows_filter_injected": True,  # Windows: treat other programs' synthetic keys as "unknown edit"
}


def _type_ok(v, dv) -> bool:
    if isinstance(dv, bool):
        return isinstance(v, bool)
    if isinstance(dv, (int, float)):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, type(dv))


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for k, dv in DEFAULTS.items():
                    v = raw.get(k, dv)
                    ok = _type_ok(v, dv)
                    if ok:
                        cfg[k] = v
        else:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
    except Exception as ex:
        print("[wa] config problem, using defaults:", ex)
    if cfg["model_device"] not in ("auto", "cpu", "cuda", "mps"):
        cfg["model_device"] = "auto"
    if not str(cfg["model_name"]).strip():
        cfg["model_name"] = DEFAULTS["model_name"]
    cfg["num_beams"] = max(1, min(int(cfg["num_beams"]), 8))
    cfg["max_check_seconds"] = max(0.5, min(float(cfg["max_check_seconds"]), 30.0))
    if cfg["theme"] not in ("dark", "light"):
        cfg["theme"] = "dark"
    mods = [m for m in cfg["hotkey_modifiers"] if m in ("ctrl", "alt", "shift", "cmd")]
    cfg["hotkey_modifiers"] = mods or ["ctrl", "alt"]
    cfg["max_buffer"] = max(300, min(int(cfg["max_buffer"]), 20000))
    return cfg


CFG = dict(DEFAULTS)     # replaced by load_config() in main()

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def match_case(original: str, repl: str) -> str:
    if not repl:
        return repl
    if len(original) > 1 and original.isupper() and original.isalpha():
        return repl.upper()
    if original[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (a transposition costs 1)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def minimal_edit(old: str, new: str):
    """(offset, delete_len, insert_text) turning old into new with the fewest keystrokes."""
    p = 0
    m = min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    q = 0
    while q < m - p and old[len(old) - 1 - q] == new[len(new) - 1 - q]:
        q += 1
    return p, len(old) - p - q, new[p:len(new) - q]


def text_is_safe_to_navigate(s: str) -> bool:
    """Arrow keys move one grapheme; only trust our counting for plain characters."""
    for c in s:
        o = ord(c)
        if o > 0xFFFF or 0x300 <= o <= 0x36F or o in (0x200D, 0xFE0F) or (o < 32 and c not in "\n\t"):
            return False
    return True

# --------------------------------------------------------------------------
# Personal dictionary (words you never want "corrected")
# --------------------------------------------------------------------------
class Lexicon:
    """The words from your dictionary folder. The model's edits to these words are dropped."""

    def __init__(self):
        self.user: set = set()

    def set_user_words(self, words) -> None:
        self.user = set(words)


LEX = Lexicon()


# --------------------------------------------------------------------------
# User dictionaries (folder of word lists, hot-reloaded)
# --------------------------------------------------------------------------
MY_WORDS_TEMPLATE = """\
# Writing Assistant - your personal dictionary
# One word per line. Lines starting with # are ignored.
# Anything here is treated as correctly spelled (names, jargon, slang...).
# Drop other word lists (.txt / .dic / .csv) into this folder: they all load
# automatically, no restart needed.
"""


def ensure_dictionary_folder() -> None:
    try:
        DICT_DIR.mkdir(parents=True, exist_ok=True)
        if not MY_WORDS.exists():
            MY_WORDS.write_text(MY_WORDS_TEMPLATE, encoding="utf-8")
    except Exception as ex:
        print("[wa] could not create dictionary folder:", ex)


def parse_dictionary_file(path) -> set:
    """Words from .txt/.csv/.dic; tolerates comments, 'word count' and Hunspell 'word/FLAGS'."""
    words = set()
    try:
        lines = Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except Exception:
        return words
    for n, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = re.split(r"[\s,;\t]+", line)[0].split("/")[0].strip("\"'")
        if n == 0 and tok.isdigit():
            continue
        if tok and not tok.isdigit() and len(tok) < 60:
            words.add(tok.lower().replace("\u2019", "'"))
    return words


def dictionary_signature():
    try:
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                            for p in DICT_DIR.iterdir() if p.suffix.lower() in DICT_EXTS))
    except Exception:
        return ()


def load_all_dictionaries() -> set:
    words = set()
    try:
        for p in sorted(DICT_DIR.iterdir()):
            if p.suffix.lower() in DICT_EXTS:
                words |= parse_dictionary_file(p)
    except Exception:
        pass
    return words


IGNORED_FILE = HOME_DIR / "ignored.txt"


def load_ignored() -> list:
    """Issues you chose to ignore (one 'rule|text' per line); delete the file to reset."""
    try:
        return [l.strip() for l in IGNORED_FILE.read_text(encoding="utf-8").splitlines() if "|" in l][:5000]
    except Exception:
        return []


def save_ignored(tag: str) -> None:
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        with open(IGNORED_FILE, "a", encoding="utf-8") as f:
            f.write(tag.replace("\n", " ") + "\n")
    except Exception as ex:
        dbg("could not save ignore:", ex)


def import_dictionaries(paths) -> None:
    import shutil
    ensure_dictionary_folder()
    for src in paths:
        src = Path(src).expanduser()
        if not src.is_file():
            print(f"  skipped (not a file): {src}")
            continue
        dst = DICT_DIR / src.name
        if dst.exists() and dst.resolve() != src.resolve():
            dst = DICT_DIR / f"{src.stem}_{int(time.time())}{src.suffix}"
        if dst.resolve() != src.resolve():
            shutil.copy2(src, dst)
        print(f"  imported {src.name}: {len(parse_dictionary_file(dst))} words -> {dst}")


def open_folder(path) -> None:
    try:
        if WIN:
            os.startfile(str(path))                      # type: ignore[attr-defined]
        elif MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as ex:
        print("[wa] could not open folder:", ex)

# --------------------------------------------------------------------------
# Grammar engine: AventIQ-AI/T5-small-grammar-correction, running locally.
#
#   text -> sentences -> the model rewrites each sentence -> word-level diff ->
#   Issue objects. The card, underlines, hotkeys and the fix/verify machinery
#   further down only ever see Issues, so they don't care where they came from.
#
# check_text() is a pure function of (text, model output): no state to drift.
# --------------------------------------------------------------------------
KIND_PRIORITY = {"spelling": 0, "grammar": 1, "punct": 2}


@dataclass
class Issue:
    key: str
    kind: str                 # spelling | grammar | punct
    rule: str
    message: str
    s: int                    # edit span [s, e) that suggestions replace
    e: int
    hs: int                   # highlight span
    he: int
    word: str                 # highlighted text
    sugg: list = field(default_factory=list)
    auto: bool = False        # safe to apply in "Fix all"
    rect: Optional[tuple] = None
    origin: tuple = (0, 0)


TASK_PREFIX = "correct grammar: "     # the prefix this model was fine-tuned with (see its model card)
MAX_INPUT_TOKENS = 100                # trained on single sentences (generation max_length is 128)
MAX_OUTPUT_TOKENS = 128
CACHE_SIZE = 3000                     # sentences remembered, so unchanged sentences cost nothing
MIN_SIMILARITY = 0.5                  # below this the model rewrote the sentence: don't trust it
ENGINE_MAX_ERRORS = 5                 # consecutive model failures before the engine switches itself off
TOK_RE = re.compile(r"\w+(?:['\u2019]\w+)*|[^\w\s]")
WORDLIKE_RE = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*")
ABBREV = {"e.g", "i.e", "etc", "dr", "mr", "mrs", "ms", "prof", "vs", "inc", "ltd", "jr", "sr", "st",
          "approx", "fig", "cf", "corp", "dept"}
SPECIAL = set("@/\\_#<>{}=+*~^|$%&`")
CHUNK_RE = re.compile(r"\S+")
DOMAINISH = re.compile(r"[A-Za-z0-9]\.[a-z]{2,4}(?:\b|/)")
SENT_END_RE = re.compile(r"[.!?]+[\"')\]\u201d\u2019]*(?=[ \t])")


def quote_norm(s: str) -> str:
    """Curly -> straight quotes (1:1, so offsets are preserved). The model sees only straight ones."""
    return s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def tokens(s: str) -> list:
    return [(m.start(), m.end(), m.group()) for m in TOK_RE.finditer(s)]


class GrammarEngine:
    """Loads the T5 model once and corrects one sentence at a time. Results are cached."""

    def __init__(self):
        self.ready = False
        self.loading = False
        self.failed: Optional[str] = None
        self.name = ""
        self.device = "cpu"
        self.beams = 5
        self.spell = None                 # optional pyspellchecker: only used to tell typos from grammar
        self._tok = self._model = self._torch = None
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._errors = 0

    # ---- loading -----------------------------------------------------------
    @staticmethod
    def _pick_device(torch, want: str) -> str:
        if want in ("auto", "cuda") and torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if want == "mps" and mps is not None and mps.is_available():
            return "mps"
        return "cpu"

    def load(self, cfg: dict) -> bool:
        """Blocking. Safe to call from a background thread. Returns True when the model is usable."""
        with self._lock:
            if self.ready:
                return True
            self.loading, self.failed = True, None
        try:
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            import torch
            from transformers import T5ForConditionalGeneration
            name = cfg["model_name"]
            try:
                from transformers import T5Tokenizer
                tok = T5Tokenizer.from_pretrained(name)
            except Exception as ex:
                dbg("T5Tokenizer failed, trying AutoTokenizer:", repr(ex))
                from transformers import AutoTokenizer
                tok = AutoTokenizer.from_pretrained(name)
            model = T5ForConditionalGeneration.from_pretrained(name)
            device = self._pick_device(torch, cfg.get("model_device", "auto"))
            # The checkpoint is stored in fp16; T5 is unreliable in fp16, so always run in fp32
            # (the model is only ~60M parameters).
            model = model.float().to(device).eval()
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            with self._lock:
                self._torch, self._tok, self._model = torch, tok, model
                self.name, self.device, self.beams = name, device, int(cfg["num_beams"])
                self._cache.clear()
                self._errors = 0
            if self._generate("She dont like pizza.") is None:    # end-to-end self test
                raise RuntimeError("the model returned nothing for a test sentence")
            try:
                from spellchecker import SpellChecker
                self.spell = SpellChecker()
            except Exception as ex:
                dbg("pyspellchecker not available (typos will be labelled as grammar):", repr(ex))
            self.ready = True
            return True
        except Exception as ex:
            self.ready = False
            self.failed = f"{type(ex).__name__}: {ex}"
            return False
        finally:
            self.loading = False

    # ---- inference ---------------------------------------------------------
    def _generate(self, sent: str) -> Optional[str]:
        torch, tok, model = self._torch, self._tok, self._model
        enc = tok(TASK_PREFIX + sent, return_tensors="pt")
        ids = enc["input_ids"]
        if ids.shape[1] > MAX_INPUT_TOKENS:
            return None                                   # too long for a model trained on sentences
        unk = getattr(tok, "unk_token_id", None)
        if unk is not None and bool((ids == unk).any()):
            return None                                   # characters it can't read: don't guess
        kw = dict(max_length=MAX_OUTPUT_TOKENS, num_beams=self.beams)
        if self.beams > 1:
            kw["early_stopping"] = True
        with torch.inference_mode():
            out = model.generate(input_ids=ids.to(self.device),
                                 attention_mask=enc["attention_mask"].to(self.device), **kw)
        seq = out[0]
        eos = tok.eos_token_id
        if eos is not None and int(seq[-1]) != eos:
            return None                                   # cut off at the length limit: unreliable
        text = tok.decode(seq, skip_special_tokens=True).strip()
        return text or None

    def cached(self, sent: str):
        """(hit, value) without running the model."""
        key = quote_norm(sent)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return True, self._cache[key]
        return False, None

    def correct(self, sent: str) -> Optional[str]:
        """The model's corrected version of one sentence, or None (can't/shouldn't check it)."""
        if not self.ready:
            return None
        key = quote_norm(sent)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            try:
                res = self._generate(key)
                self._errors = 0
            except Exception as ex:
                dbg("model error:", repr(ex))
                self._errors += 1
                if self._errors >= ENGINE_MAX_ERRORS:
                    self.ready, self.failed = False, f"too many model errors (last: {ex!r})"
                    print("[wa] grammar model switched off:", self.failed)
                return None
            self._cache[key] = res
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return res

    def is_unknown_word(self, lw: str) -> Optional[bool]:
        """True/False when a word list is available, None when it is not."""
        if self.spell is None:
            return None
        try:
            return not self.spell.known([lw])
        except Exception:
            return None


ENGINE = GrammarEngine()


# --------------------------------------------------------------------------
# Text -> sentences
# --------------------------------------------------------------------------
def split_sentences(text: str) -> list:
    """[(start, end)] of sentence-like units: split at line breaks and after . ! ? (but not after
    abbreviations, initials or ellipses)."""
    spans = []

    def emit(a, b):
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append((a, b))

    for line in re.finditer(r"[^\n]+", text):
        a0, seg = line.start(), line.group()
        start = 0
        for m in SENT_END_RE.finditer(seg):
            core = m.group().rstrip("\"')]\u201d\u2019")
            if len(core) >= 2 and core.strip(".") == "":
                continue                                    # ellipsis: the sentence goes on
            if core == ".":
                k = m.start()
                while k > 0 and (seg[k - 1].isalpha() or seg[k - 1] == "."):
                    k -= 1
                tok = seg[k:m.start()].lower().strip(".")
                if len(tok) == 1 or tok in ABBREV:
                    continue                                # "e.g.", "Dr.", "J. Smith"
            emit(a0 + start, a0 + m.end())
            start = m.end()
        emit(a0 + start, a0 + len(seg))
    return spans


def _build_mask(text: str) -> bytearray:
    """1 = inside a URL / path / code / number chunk: the model's edits there are dropped."""
    mask = bytearray(len(text))
    for m in CHUNK_RE.finditer(text):
        ch = m.group().strip(".,;:!?\"'()[]\u201c\u201d\u2018\u2019")
        if (any(c in SPECIAL or c.isdigit() for c in ch) or "://" in ch or ch.lower().startswith("www.")
                or DOMAINISH.search(ch) or re.search(r"\w\(\)", ch)):
            mask[m.start():m.end()] = b"\x01" * (m.end() - m.start())
    return mask


# --------------------------------------------------------------------------
# Model rewrite -> word-level edits
# --------------------------------------------------------------------------
@dataclass
class Edit:
    op: str                   # replace | insert | delete
    s: int                    # span (sentence-relative) that `new` replaces
    e: int
    hs: int                   # span to highlight
    he: int
    new: str
    old: list                 # original tokens that change (empty for a pure insertion)
    added: list               # tokens the model added
    first: int                # index of the first original token involved
    at_end: bool              # the edit reaches the end of the sentence


def _edit_for(sent, fixed, ot, nt, i1, i2, j1, j2) -> Optional[Edit]:
    """One difflib opcode -> an Edit whose replacement text keeps the model's own spacing."""
    old = [t[2] for t in ot[i1:i2]]
    added = [t[2] for t in nt[j1:j2]]
    at_end = i2 >= len(ot)
    if i2 > i1 and j2 > j1:                                     # replace
        s, e = ot[i1][0], ot[i2 - 1][1]
        return Edit("replace", s, e, s, e, fixed[nt[j1][0]:nt[j2 - 1][1]], old, added, i1, at_end)
    if i2 == i1:                                                # insert: fold in a neighbouring word
        a = ot[i1 - 1] if i1 > 0 else None
        b = ot[i1] if i1 < len(ot) else None
        if a is not None and j1 > 0 and nt[j1 - 1][1] == nt[j1][0]:
            new, span = fixed[nt[j1 - 1][0]:nt[j2 - 1][1]], a   # glued to the word before (a comma...)
        elif b is not None and j2 < len(nt):
            new, span = fixed[nt[j1][0]:nt[j2][1]], b           # "going" -> "am going"
        elif a is not None and j1 > 0:
            new, span = fixed[nt[j1 - 1][0]:nt[j2 - 1][1]], a
        else:
            return None
        return Edit("insert", span[0], span[1], span[0], span[1], new, [], added, i1, at_end)
    # delete: also remove one adjoining space so the words don't end up stuck together
    d_s, d_e = ot[i1][0], ot[i2 - 1][1]
    a = ot[i1 - 1] if i1 > 0 else None
    b = ot[i2] if i2 < len(ot) else None
    prev_ws = a is None or sent[a[1]:d_s] != ""
    next_ws = b is not None and sent[d_e:b[0]] != ""
    if b is None:
        if a is None:
            return None
        s, e = (a[1], d_e) if prev_ws else (d_s, d_e)
    elif prev_ws and next_ws:
        s, e = d_s, b[0]
    elif prev_ws and a is not None and not any(c.isalnum() for c in b[2]):
        s, e = a[1], d_e                                        # "please really." -> "please."
    elif prev_ws or next_ws:
        s, e = d_s, d_e
    else:
        return None                                             # would glue two words together
    return Edit("delete", s, e, d_s, d_e, "", old, [], i1, at_end)


def diff_edits(sent: str, fixed: str) -> list:
    """Word-level differences between a sentence and the model's rewrite. [] if it rewrote too much."""
    ot, nt = tokens(sent), tokens(fixed)
    if not ot or not nt:
        return []
    ok = [quote_norm(t[2]) for t in ot]
    nk = [quote_norm(t[2]) for t in nt]
    if ok == nk:
        return []
    sm = difflib.SequenceMatcher(None, ok, nk, autojunk=False)
    if sm.ratio() < MIN_SIMILARITY:
        return []
    edits = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            ed = _edit_for(sent, fixed, ot, nt, i1, i2, j1, j2)
            if ed:
                edits.append(ed)
    return edits


def _acceptable(ed: Edit, lex: Lexicon, fragment: bool) -> bool:
    """Guard rails against the model's typical mistakes (it was trained on essays, not on your notes)."""
    if fragment and ed.first == 0:
        return False                                  # buffer may start mid-sentence: first word is unreliable
    if any(any(c.isdigit() for c in t) for t in ed.old + ed.added):
        return False                                  # never touch numbers
    if any(quote_norm(t).lower() in lex.user for t in ed.old):
        return False                                  # your dictionary wins
    if ed.op == "insert" and ed.at_end and all(t in (".", "!", "?") for t in ed.added):
        return False                                  # no nagging about the full stop of a line you're typing
    one_word = ed.op == "replace" and len(ed.old) == 1 and len(ed.added) == 1
    for t in ed.old:
        if len(t) > 1 and WORDLIKE_RE.fullmatch(t) and (t.isupper() or any(c.isupper() for c in t[1:])):
            return False                              # acronyms and CamelCase names are left alone
    if one_word:
        o, n = ed.old[0], ed.added[0]
        if ed.first > 0 and len(o) > 1 and o[0].isupper() and o.lower() != n.lower():
            return False                              # a capitalised word mid-sentence is probably a name
    return True


def _classify(ed: Edit, eng: GrammarEngine):
    """(kind, rule, message, auto) for an edit. `auto` = safe enough for Fix all."""
    alnum = lambda t: any(c.isalnum() for c in t)
    if not any(alnum(t) for t in ed.old + ed.added):
        msg = {"insert": f"Add “{''.join(ed.added)}”",
               "delete": f"Remove “{''.join(ed.old)}”"}.get(ed.op, "Punctuation")
        return "punct", "punct-" + ed.op, msg, False
    if ed.op == "insert":
        return "grammar", "grammar-add", f"Missing: “{' '.join(ed.added)}”", False
    if ed.op == "delete":
        return "grammar", "grammar-cut", f"Unneeded: “{' '.join(ed.old)}”", False
    if len(ed.old) == 1 and len(ed.added) == 1 and WORDLIKE_RE.fullmatch(ed.old[0]) \
            and WORDLIKE_RE.fullmatch(ed.added[0]):
        o, n = ed.old[0], ed.added[0]
        lo, ln = quote_norm(o).lower(), quote_norm(n).lower()
        if lo == ln:                                            # capitalisation only
            gain = o[:1].islower() and n[:1].isupper() and o[1:] == n[1:]
            return "grammar", "grammar-case", "Capitalization", gain
        if ln.replace("'", "") == lo:                           # dont -> don't, im -> I'm
            return "grammar", "grammar-fix", "Missing apostrophe", True
        if eng.is_unknown_word(lo):                             # not a word at all: a typo
            d = edit_distance(lo, ln)
            if d <= 3:
                return "spelling", "spell", "Possible misspelling", \
                    (d <= 1 or (d == 2 and len(lo) >= 6)) and not o[:1].isupper()
    return "grammar", "grammar-fix", "Grammar", False


def ignore_tag(it) -> str:
    return f"{it.rule}|{it.word.lower()}|{(it.sugg[0] if it.sugg else '').lower()}"


# --------------------------------------------------------------------------
# Whitespace / punctuation spacing: the model can't see whitespace, so these few
# exact, deterministic checks stay.
# --------------------------------------------------------------------------
def _spacing_issues(text: str, mask: bytearray) -> list:
    out: list = []

    def add(rule, msg, s, e, hs, he, sugg):
        if any(mask[hs:max(he, hs + 1)]):
            return
        out.append(Issue("", "punct", rule, msg, s, e, hs, he, text[hs:he], list(sugg), True))

    for m in re.finditer(r"(?<=[^\s.!?:;])( {2,})(?=\S)", text):
        add("double-space", "Extra space", m.start(), m.end(), m.start(), m.end(), [" "])
    for m in re.finditer(r"(?<=[A-Za-z0-9)\"\u201d\u2019'])([ \t]+)(?=,|[.!?](?![.!?\w]))", text):
        add("space-before-punct", "Space before punctuation", m.start(), m.end(), m.start(), m.end(), [""])
    for m in re.finditer(r",{2,}", text):
        add("repeat-comma", "Repeated comma", m.start(), m.end(), m.start(), m.end(), [","])
    for rid, pat in (("missing-space-comma", r"(?<=[A-Za-z ]),(?=[A-Za-z])"),
                     ("missing-space-stop", r"(?<=[a-z]{2})\.(?=[A-Z][a-z])"),
                     ("missing-space-mark", r"(?<=[A-Za-z])[!?;](?=[A-Za-z])")):
        for m in re.finditer(pat, text):
            add(rid, f"Missing space after “{m.group()}”", m.end(), m.end(), m.start(), m.end() + 1, [" "])
    return out


# --------------------------------------------------------------------------
# check_text
# --------------------------------------------------------------------------
def check_text(text: str, *, lex: Lexicon = None, cfg: dict = None, start_is_sentence: bool = False,
               active: Optional[tuple] = None, ignored=frozenset(), focus: Optional[int] = None,
               progress: Optional[dict] = None) -> list:
    """All issues in `text`, sorted by position.

    active    span being typed right now (skipped)
    focus     caret offset: sentences nearest to it are checked first
    progress  if given, progress["complete"] is set False when the time budget (max_check_seconds)
              ran out before every new sentence was checked: call again to continue (results are cached)
    """
    lex = lex or LEX
    cfg = cfg or CFG
    if progress is not None:
        progress["complete"] = True
    n = len(text)
    if not n:
        return []
    mask = _build_mask(text)
    out: list = _spacing_issues(text, mask)

    eng = ENGINE
    if eng.ready:
        spans = split_sentences(text)
        order = list(range(len(spans)))
        if focus is not None:
            order.sort(key=lambda i: abs((spans[i][0] + spans[i][1]) // 2 - focus))
        deadline = time.time() + float(cfg.get("max_check_seconds", 3.0))
        for idx in order:
            ss, se = spans[idx]
            sent = text[ss:se]
            if sum(mask[ss:se]) > 0.25 * (se - ss):
                continue                                    # mostly a URL / path / code
            if sum(1 for m in TOK_RE.finditer(sent) if any(c.isalnum() for c in m.group())) < 2:
                continue                                    # one word isn't a sentence yet
            hit, fixed = eng.cached(sent)
            if not hit:
                if time.time() > deadline:
                    if progress is not None:
                        progress["complete"] = False
                    continue
                fixed = eng.correct(sent)
            if not fixed:
                continue
            fragment = idx == 0 and not start_is_sentence
            for ed in diff_edits(sent, fixed):
                if not _acceptable(ed, lex, fragment):
                    continue
                s, e, hs, he = ss + ed.s, ss + ed.e, ss + ed.hs, ss + ed.he
                if ed.new == text[s:e] or any(mask[hs:max(he, hs + 1)]) or any(mask[s:max(e, s + 1)]):
                    continue
                kind, rule, msg, auto = _classify(ed, eng)
                out.append(Issue("", kind, rule, msg, s, e, hs, he, text[hs:he], [ed.new], auto))

    # ---- finalize: ignore, active word, overlaps, keys
    res = []
    for it in out:
        if ignore_tag(it) in ignored:
            continue
        if active and ((it.hs < active[1] and it.he > active[0]) or (it.s < active[1] and it.e > active[0])):
            continue
        res.append(it)
    res.sort(key=lambda i: (KIND_PRIORITY[i.kind], i.hs, -(i.he - i.hs)))
    kept: list = []
    for it in res:
        if any((it.hs < k.he and it.he > k.hs) or (it.s < k.e and it.e > k.s) for k in kept):
            continue
        kept.append(it)
    kept.sort(key=lambda i: (i.hs, i.he))
    seen: dict = {}
    for it in kept:
        base = f"{it.rule}|{it.word.lower()}"
        seen[base] = seen.get(base, 0) + 1
        it.key = f"{base}|{seen[base]}"
    return kept


# --------------------------------------------------------------------------
# Platform helpers (everything best-effort: failure must never crash the app)
# --------------------------------------------------------------------------
import ctypes

if WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def make_dpi_aware() -> None:
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def win_style(win, click_through: bool = False) -> None:
    """Windows: make a Tk window non-activating so clicking it never steals focus."""
    if not WIN:
        return
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        ex |= 0x08000000 | 0x00000080                # NOACTIVATE | TOOLWINDOW
        if click_through:
            ex |= 0x00000020 | 0x00080000            # TRANSPARENT | LAYERED
        user32.SetWindowLongW(hwnd, -20, ex)
    except Exception:
        pass


@dataclass
class Target:
    wid: object = None
    pid: Optional[int] = None
    name: str = ""


def foreground() -> Target:
    """The active window: (id, pid, lower-case process/app name). Best effort."""
    try:
        if WIN:
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ""
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(520)
                    size = wintypes.DWORD(520)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = Path(buf.value).stem.lower()
                finally:
                    kernel32.CloseHandle(h)
            return Target(hwnd, pid.value, name)
        if MAC:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return Target(app.processIdentifier(), app.processIdentifier(),
                          str(app.localizedName() or "").lower())
        out = subprocess.check_output(["xdotool", "getactivewindow"], timeout=0.4,
                                      stderr=subprocess.DEVNULL).decode().strip()
        pid, name = None, ""
        try:
            pid = int(subprocess.check_output(["xdotool", "getwindowpid", out], timeout=0.4,
                                              stderr=subprocess.DEVNULL).decode().strip())
            name = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        except Exception:
            pass
        return Target(out, pid, name)
    except Exception:
        return Target()


def activate(t: Target) -> bool:
    """Give focus back to a previously active window (used after clicking our own card)."""
    try:
        if t.wid is None:
            return False
        if WIN:
            return bool(user32.SetForegroundWindow(t.wid))
        if MAC:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(t.pid)
            return bool(app and app.activateWithOptions_(2))
        subprocess.check_call(["xdotool", "windowactivate", str(t.wid)], timeout=0.6,
                              stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def app_is_ignored(name: str, ignore) -> bool:
    n = (name or "").lower().replace(".exe", "")
    if not n:
        return False
    for ig in ignore:
        ig = str(ig).lower().replace(".exe", "")
        if n == ig or (len(ig) >= 6 and ig in n):
            return True
    return False


def window_origin():
    """Top-left of the active window (Windows) so marks can follow window moves."""
    if WIN:
        try:
            r = wintypes.RECT()
            user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(r))
            return (r.left, r.top)
        except Exception:
            pass
    return (0, 0)


class CaretLocator:
    """Screen position of the text caret as (x, y_top, height) or None (Windows & macOS)."""

    def __init__(self):
        self.errors = 0

    def get(self):
        if self.errors > 40 or not (WIN or MAC):
            return None
        try:
            r = self._win() if WIN else self._mac()
            self.errors = 0
            return r
        except Exception:
            self.errors += 1
            return None

    @staticmethod
    def _win():
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndCaret:
            return None
        rc = info.rcCaret
        if rc.left == rc.right and rc.top == rc.bottom:
            return None
        pt = wintypes.POINT(rc.left, rc.top)
        user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
        return (pt.x, pt.y, max(rc.bottom - rc.top, 12))

    @staticmethod
    def _mac():
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            AXUIElementCopyParameterizedAttributeValue, AXValueGetValue, AXValueCreate,
            kAXFocusedUIElementAttribute, kAXSelectedTextRangeAttribute,
            kAXBoundsForRangeParameterizedAttribute, kAXValueCFRangeType, kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        err, el = AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        if err or rng is None:
            return None
        ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
        if not ok or cf.location < 1:
            return None
        prev = AXValueCreate(kAXValueCFRangeType, CFRangeMake(cf.location - 1, 1))
        err, bounds = AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute, prev, None)
        if err or bounds is None:
            return None
        ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
        if not ok:
            return None
        return (rect.origin.x + rect.size.width, rect.origin.y, rect.size.height)


_mon_cache = (0.0, [])


def get_monitors(root):
    """[(x, y, w, h)] for each monitor (cached a few seconds)."""
    global _mon_cache
    if time.time() - _mon_cache[0] < 3 and _mon_cache[1]:
        return _mon_cache[1]
    ms = []
    try:
        from screeninfo import get_monitors as gm
        ms = [(m.x, m.y, m.width, m.height) for m in gm()]
    except Exception:
        pass
    if not ms and WIN:
        try:
            gsm = user32.GetSystemMetrics
            ms = [(gsm(76), gsm(77), gsm(78), gsm(79))]      # virtual desktop
        except Exception:
            pass
    if not ms:
        ms = [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
    _mon_cache = (time.time(), ms)
    return ms


def monitor_at(ms, px, py):
    for m in ms:
        if m[0] <= px < m[0] + m[2] and m[1] <= py < m[1] + m[3]:
            return m
    return ms[0]


# --------------------------------------------------------------------------
# Reading the focused text field (ground truth)
# --------------------------------------------------------------------------
@dataclass
class FieldSnap:
    text: str                           # normalized: "\n" line breaks
    caret: Optional[int] = None         # offset of the caret in `text`; None = unknown
    selection: int = 0                  # > 0: something is selected (caret not reliable)
    bounds: Optional[object] = None     # callable(a, b) -> (x, y, w, h) screen rect, or None


def _norm_nl(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


class FieldBackend:
    """read() -> FieldSnap | None (None = unsupported, empty, or a password field)."""

    def thread_context(self):
        import contextlib
        return contextlib.nullcontext()

    def read(self) -> Optional[FieldSnap]:
        return None


class WinFieldBackend(FieldBackend):
    """Win32 edit/rich-edit controls (Notepad, WordPad, many dialogs), then UI Automation
    (optional `pip install uiautomation`) for browsers, Word, modern apps."""
    CLASSIC = {"Edit", "RichEdit", "RichEdit20W", "RichEdit20A", "RICHEDIT50W", "RichEditD2DPT"}

    def __init__(self):
        self.uia_errors = 0
        self._auto = None
        try:
            import uiautomation as auto
            self._auto = auto
        except Exception:
            dbg("uiautomation not installed: only classic Win32 edit controls can be read")
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                               ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def thread_context(self):
        if self._auto is not None and hasattr(self._auto, "UIAutomationInitializerInThread"):
            return self._auto.UIAutomationInitializerInThread()
        return super().thread_context()

    def _send(self, hwnd, msg, wp=0, lp=0):
        res = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(hwnd, msg, wp, lp, 0x2, 250, ctypes.byref(res))   # ABORTIFHUNG
        return res.value if ok else None

    def read(self):
        snap = self._win32()
        if snap is not None:
            return snap if snap is not False else None
        if self._auto is not None and self.uia_errors < 6:
            try:
                s = self._uia()
                self.uia_errors = 0
                return s
            except Exception as ex:
                self.uia_errors += 1
                dbg("uia read failed:", repr(ex))
        return None

    def _win32(self):
        """FieldSnap, False (refuse: password), or None (not a classic edit control)."""
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
            return None
        h = info.hwndFocus
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if buf.value not in self.CLASSIC:
            return None
        if user32.GetWindowLongW(h, -16) & 0x20 and buf.value == "Edit":     # ES_PASSWORD
            return False
        n = self._send(h, 0x000E)                                           # WM_GETTEXTLENGTH
        maxc = CFG["max_field_chars"]
        if n is None or n > maxc * 4:
            return None
        text = ctypes.create_unicode_buffer(n + 2)
        if self._send(h, 0x000D, n + 1, ctypes.addressof(text)) is None:    # WM_GETTEXT
            return None
        raw = text.value
        a, b = wintypes.DWORD(0), wintypes.DWORD(0)
        caret, sel = None, 0
        if self._send(h, 0x00B0, ctypes.addressof(a), ctypes.addressof(b)) is not None:   # EM_GETSEL
            lo, hi = min(a.value, b.value), max(a.value, b.value)
            if hi <= len(raw):
                caret = len(_norm_nl(raw[:hi]))
                sel = len(_norm_nl(raw[lo:hi]))
        return FieldSnap(_norm_nl(raw), caret, sel, None)

    def _uia(self):
        auto = self._auto
        ctrl = auto.GetFocusedControl()
        if ctrl is None or getattr(ctrl, "IsPassword", False):
            return None
        kind = ctrl.ControlTypeName
        if not (kind == "EditControl" or (kind == "DocumentControl"
                                          and foreground().name in ("winword", "wordpad"))):
            return None
        maxc = CFG["max_field_chars"]
        tp = ctrl.GetTextPattern()
        if tp:
            doc = tp.DocumentRange
            raw = doc.GetText(maxc + 1)
            caret, sel = None, 0
            sels = tp.GetSelection()
            if sels:
                r = doc.Clone()
                r.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, sels[0],
                                      auto.TextPatternRangeEndpoint.Start)
                caret = len(_norm_nl(r.GetText(maxc + 1)))
                sel = len(sels[0].GetText(5000))
            fn = None
            if "\r" not in raw:
                def fn(a, b, doc=doc):
                    try:
                        rg = doc.Clone()
                        rg.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start, auto.TextUnit.Character, a)
                        rg.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, rg,
                                               auto.TextPatternRangeEndpoint.Start)
                        rg.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End, auto.TextUnit.Character, b - a)
                        rects = rg.GetBoundingRectangles()
                        if rects:
                            r0 = rects[0]
                            return (r0.left, r0.top, r0.right - r0.left, r0.bottom - r0.top)
                    except Exception:
                        pass
                    return None
            return FieldSnap(_norm_nl(raw), caret, sel, fn)
        vp = ctrl.GetValuePattern()
        if vp:
            return FieldSnap(_norm_nl(str(vp.Value or "")), None, 0, None)
        return None


class MacFieldBackend(FieldBackend):
    """Accessibility API (Cocoa text fields/views). Needs pyobjc-framework-ApplicationServices."""
    ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}

    def read(self):
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
            AXUIElementCopyParameterizedAttributeValue, AXValueGetValue, AXValueCreate,
            kAXFocusedUIElementAttribute, kAXRoleAttribute, kAXSubroleAttribute, kAXValueAttribute,
            kAXSelectedTextRangeAttribute, kAXBoundsForRangeParameterizedAttribute,
            kAXValueCFRangeType, kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        err, el = AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, role = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
        err2, sub = AXUIElementCopyAttributeValue(el, kAXSubroleAttribute, None)
        if err or str(role) not in self.ROLES or (not err2 and "Secure" in str(sub)):
            return None
        err, val = AXUIElementCopyAttributeValue(el, kAXValueAttribute, None)
        if err or val is None:
            return None
        text = _norm_nl(str(val))
        if len(text) > CFG["max_field_chars"] * 2:
            return None
        caret, sel = None, 0
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        astral = any(ord(c) > 0xFFFF for c in text)
        if not err and rng is not None and not astral:
            ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
            if ok:
                caret, sel = int(cf.location), int(cf.length)
                if caret > len(text):
                    caret = None

        def fn(a, b):
            try:
                r = AXValueCreate(kAXValueCFRangeType, CFRangeMake(a, max(b - a, 1)))
                e, bounds = AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute, r, None)
                if e or bounds is None:
                    return None
                ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
                return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height) if ok else None
            except Exception:
                return None
        return FieldSnap(text, caret, sel, None if astral else fn)


def make_field_backend() -> Optional[FieldBackend]:
    if not CFG["read_field"]:
        return None
    try:
        if WIN:
            return WinFieldBackend()
        if MAC:
            return MacFieldBackend()
    except Exception as ex:
        dbg("no field backend:", repr(ex))
    return None


# --------------------------------------------------------------------------
# Model: follows the typed text, owns the issue list, applies fixes
# --------------------------------------------------------------------------
MAX_STATIC = 20000


class Assistant:
    """Tracks text typed in the current field (live mode) or a clipboard snapshot (static mode).

    Invariant (live mode): `text` is a contiguous run of characters typed in the focused field
    and `caret` is the real caret position inside it. Anything that could break that invariant
    (mouse click, arrow past the edges, Ctrl shortcuts, focus change, foreign key injection)
    calls reset() - we never guess.
    """

    def __init__(self, notify, toast, kb, keys, locator=None, lex: Lexicon = None, backend=None):
        self.notify, self.toast, self.kb, self.K, self.locator = notify, toast, kb, keys, locator
        self.backend = backend
        self.lex = lex or LEX
        self.lock = threading.RLock()
        self.inject_lock = threading.Lock()
        self.stop = threading.Event()
        self.mode = "live"                   # live | clipboard
        self.text, self.caret = "", 0
        self.start_sentence = False          # is buffer start known to be a sentence start?
        self.issues: list = []
        self.selected: Optional[str] = None
        self.ignored: set = set(load_ignored())
        self.rev = 0
        self.checked_part = self.checked_full = -1
        self.last_edit = time.time()
        self.paused = False
        self.injecting = False
        self.inject_until = 0.0
        self.mods: set = set()
        self.app_ignored: Optional[str] = None
        self.target = Target()
        self.samples: dict = {}
        self.sample_q: queue.Queue = queue.Queue()
        self._dict_sig = None
        self._dict_checked = 0.0
        self.disk_check = 0.0
        # field sync state (backend present): the real field text is ground truth
        self.stale = False                   # model may differ from the field: re-read before trusting
        self.complete = False                # `text` covers the whole field (edges are real edges)
        self.caret_known = True
        self.field_ok: Optional[bool] = None # can the focused field be read? (None = not tried yet)
        self.base = 0                        # offset of `text` inside a very long field
        self.want_sync = 0.0
        self.verified_rev = -1
        self.last_verify = 0.0
        self.expect_toast = False
        self.read_backoff_until = 0.0
        self.sync_event = threading.Event()
        self.sync_done = threading.Event()
        self.bounds_fn = None
        self.bounds_due = 0.0
        self.sync_gen = 0
        self._reading = False
        ensure_dictionary_folder()
        self.reload_dictionaries(quiet=True)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)

    def start(self):
        self._thread.start()
        if self.backend:
            self._sync_thread.start()
            self.want_sync = time.time() + 0.2

    # ---------------- state
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode, "text": self.text, "caret": self.caret, "paused": self.paused,
                "app_ignored": self.app_ignored, "selected": self.selected, "rev": self.rev,
                "busy": self.injecting, "stale": self.stale, "synced": bool(self.field_ok),
                "issues": [dict(key=i.key, kind=i.kind, rule=i.rule, message=i.message, hs=i.hs,
                                he=i.he, word=i.word, sugg=list(i.sugg), auto=i.auto,
                                rect=i.rect, origin=i.origin)
                           for i in sorted(self.issues, key=lambda x: (x.hs, x.he))],
            }

    def _bump(self):
        self.rev += 1
        self.last_edit = time.time()

    def reset(self, new_line: bool = False, new_field: bool = False):
        with self.lock:
            if self.mode != "live":
                return
            had = bool(self.text or self.issues)
            self.text, self.caret, self.issues, self.selected = "", 0, [], None
            self.start_sentence = new_line
            self.stale, self.complete, self.caret_known, self.base = False, False, True, 0
            if new_field:
                self.field_ok = None
            self.samples.clear()
            self.bounds_fn = None
            if self.backend:
                self.want_sync = time.time() + 0.1
            self._bump()
        if had:
            self.notify()

    def invalidate(self):
        """Something happened that may have changed the field in ways we can't follow."""
        with self.lock:
            if self.mode != "live":
                return
            if self.backend and self.field_ok is not False and not self.paused and not self.app_ignored:
                self.stale = True
                self.want_sync = time.time() + 0.1
                self.last_edit = time.time()
            else:
                self.reset()
                return
        self.notify()

    def _gate(self) -> bool:
        """May a key event edit the model right now? (call with the lock held)"""
        if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
            return False
        if self.stale or not self.caret_known:
            self.last_edit = time.time()
            if not self.stale:
                self.invalidate()
            return False
        return True

    def _live_ok(self) -> bool:
        return self.mode == "live" and not self.paused and not self.app_ignored and not self.injecting

    # ---------------- selection / dismissal
    def _find(self, key):
        return next((i for i in self.issues if i.key == key), None)

    def select(self, key):
        with self.lock:
            if self._find(key):
                self.selected = key
        self.notify()

    def step(self, d: int = 1):
        with self.lock:
            order = sorted(self.issues, key=lambda x: (x.hs, x.he))
            if not order:
                return
            keys = [i.key for i in order]
            i = (keys.index(self.selected) + d) % len(keys) if self.selected in keys else 0
            self.selected = keys[i]
        self.notify()

    def _ensure_selection(self):
        if self.selected in {i.key for i in self.issues}:
            return
        order = sorted(self.issues, key=lambda x: x.hs)
        if not order:
            self.selected = None
            return
        before = [i for i in order if i.hs <= self.caret]
        self.selected = (before[-1] if before and self.mode == "live" else order[0]).key

    def ignore(self, key=None):
        with self.lock:
            it = self._find(key or self.selected)
            if not it:
                return
            tag = ignore_tag(it)
            self.ignored.add(tag)
            save_ignored(tag)
            self.issues = [i for i in self.issues if ignore_tag(i) != tag]
            self._ensure_selection()
        self.notify()

    def add_word(self, key=None):
        with self.lock:
            it = self._find(key or self.selected)
            if not it or it.kind != "spelling":
                return
            lw = it.word.lower().replace("\u2019", "'")
            try:
                ensure_dictionary_folder()
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(lw + "\n")
                self._dict_sig = dictionary_signature()
            except Exception as ex:
                dbg("could not save word:", ex)
            self.lex.set_user_words(self.lex.user | {lw})
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            self._ensure_selection()
        self.notify()
        self.toast(f"Added “{lw}” to your dictionary")

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
            p = self.paused
        self.reset()
        self.toast("Paused — press the hotkey again to resume" if p else "Checking resumed")
        self.notify()

    def recheck(self):
        """Forget what has been checked so far (e.g. the grammar model just finished loading)."""
        with self.lock:
            self.checked_part = self.checked_full = -1
        self.notify()

    def reload_dictionaries(self, quiet: bool = False):
        sig = dictionary_signature()
        if sig == self._dict_sig:
            return
        first = self._dict_sig is None
        self._dict_sig = sig
        words = load_all_dictionaries()
        with self.lock:
            self.lex.set_user_words(words)
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() in words)]
            self._bump()
            self.checked_part = self.checked_full = -1
        dbg("dictionary loaded:", len(words), "words")
        if not quiet and not first:
            self.toast(f"Dictionary reloaded ({len(words):,} words)")
        self.notify()

    # ---------------- key events (live mode)
    def _shift(self, a: int, b: int, ins_len: int, ins_is_word: bool = False):
        """Text [a,b) was replaced by ins_len characters: move or drop issue spans accordingly."""
        delta = ins_len - (b - a)
        keep = []
        for i in self.issues:
            lo, hi = min(i.s, i.hs), max(i.e, i.he)
            if hi < a or (hi == a and not ins_is_word):
                keep.append(i)                        # entirely before the edit: still valid
            elif lo >= b:
                i.s += delta; i.e += delta; i.hs += delta; i.he += delta
                i.rect = None
                keep.append(i)                        # entirely after: slide along
            # overlapping the edit: verdict is stale, drop it
        self.issues = keep
        self.samples = {k: v for k, v in self.samples.items() if k <= a}

    def type_char(self, c: str):
        with self.lock:
            if not self._gate():
                return
            p = self.caret
            self.text = self.text[:p] + c + self.text[p:]
            self.caret = p + 1
            self._shift(p, p, 1, ins_is_word=c.isalnum() or c in "'\u2019")
            self._trim()
            self._bump()
            self.sample_q.put(self.caret)
            self._ensure_selection()
        self.notify()

    def backspace(self):
        with self.lock:
            if not self._gate():
                return
            p = self.caret
            if p == 0:
                if not self.complete:
                    self.start_sentence = False   # deleting text before our window
                return
            self.text = self.text[:p - 1] + self.text[p:]
            self.caret = p - 1
            self._shift(p - 1, p, 0)
            self._bump()
            self.sample_q.put(self.caret)
            self._ensure_selection()
        self.notify()

    def delete_fwd(self):
        with self.lock:
            if not self._gate() or self.caret >= len(self.text):
                return
            p = self.caret
            self.text = self.text[:p] + self.text[p + 1:]
            self._shift(p, p + 1, 0)
            self._bump()
            self._ensure_selection()
        self.notify()

    def move(self, d: int):
        """Left/Right arrow. Leaving a partial buffer means the caret is unknown: re-sync."""
        with self.lock:
            if not self._gate():
                return
            n = self.caret + d
            if 0 <= n <= len(self.text):
                self.caret = n
                self.last_edit = time.time()
                self.sample_q.put(n)
                lost = False
            else:
                lost = not self.complete          # at a real field edge the key does nothing
        if lost:
            self.invalidate()
        else:
            self.notify()

    def enter(self):
        if self.backend and self.field_ok:
            self.invalidate()                     # chat boxes clear, editors add a line: read the truth
        elif CFG["enter_resets"]:
            self.reset(new_line=True)
        else:
            self.type_char("\n")

    def drop_marks(self):
        with self.lock:
            self.samples.clear()
            self.bounds_due = time.time() + 0.3
            if not self.bounds_fn:
                for i in self.issues:
                    i.rect = None
        self.notify()

    def _trim(self):
        cap = CFG["max_field_chars"] if self.complete else CFG["max_buffer"]
        if len(self.text) <= cap:
            return
        cut = len(self.text) - (cap - 200)
        self.text = self.text[cut:]
        self.caret -= cut
        self.start_sentence = False
        self.complete = False
        keep = []
        for i in self.issues:
            if min(i.s, i.hs) >= cut:
                i.s -= cut; i.e -= cut; i.hs -= cut; i.he -= cut
                keep.append(i)
        self.issues = keep
        self.samples = {k - cut: v for k, v in self.samples.items() if k >= cut}
        if self.caret < 0:
            self.text, self.caret, self.issues = "", 0, []

    # ---------------- clipboard (static) mode
    def load_static(self, text: str):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            self.toast("The clipboard has no text to check")
            return
        trunc = len(text) > MAX_STATIC
        with self.lock:
            self.mode = "clipboard"
            self.text, self.caret = text[:MAX_STATIC], 0
            self.start_sentence = True
            self.issues, self.selected = [], None
            self.samples.clear()
            self._bump()
            self.checked_part = self.checked_full = -1
        if trunc:
            self.toast(f"Checking the first {MAX_STATIC:,} characters")
        self.notify()

    def exit_static(self):
        with self.lock:
            if self.mode != "clipboard":
                return
            self.mode = "live"
            self.text, self.issues, self.selected, self.caret = "", [], None, 0
            self.start_sentence = False
            self._bump()
        self.notify()

    def static_text(self) -> str:
        with self.lock:
            return self.text

    # ---------------- analysis worker
    def _active_span(self):
        t, c = self.text, self.caret
        wc = lambda ch: ch.isalnum() or ch in "'\u2019"
        a = c
        while a > 0 and wc(t[a - 1]):
            a -= 1
        b = c
        while b < len(t) and wc(t[b]):
            b += 1
        return (a, b) if b > a else None

    def _maybe_analyze(self):
        with self.lock:
            if self.injecting or not self.text or (self.stale and self.mode == "live"):
                return
            if self.mode == "live" and (self.paused or self.app_ignored):
                return
            rev = self.rev
            if rev == self.checked_full:
                return
            idle = time.time() - self.last_edit
            live = self.mode == "live"
            full = (not live) or idle >= CFG["check_trailing_word_seconds"]
            if not full and not (idle >= CFG["idle_check_seconds"] and rev != self.checked_part):
                return
            active = None if full else self._active_span()
            text, start, ignored = self.text, self.start_sentence, frozenset(self.ignored)
            focus = self.caret if live else None
        prog: dict = {}
        try:
            found = check_text(text, lex=self.lex, cfg=CFG, start_is_sentence=start, active=active,
                               ignored=ignored, focus=focus, progress=prog)
        except Exception as ex:                       # a checker bug must never kill the worker
            dbg("check failed:", repr(ex))
            with self.lock:
                self.checked_part = self.checked_full = rev
            return
        with self.lock:
            if self.rev != rev or self.injecting:
                return                                # text changed while checking: try again
            self.issues = found
            if prog.get("complete", True):            # else: time budget hit, carry on next loop (cached)
                self.checked_part = rev
                if full or active is None:
                    self.checked_full = rev
            self._ensure_selection()
            self._refresh_rects()
            self.bounds_due = time.time()
        self.notify()

    def _worker(self):
        last_forget_check = 0.0
        while not self.stop.is_set():
            try:
                pos = self.sample_q.get(timeout=0.08)
            except queue.Empty:
                pos = None
            try:
                if pos is not None:
                    if self.sample_q.qsize() > 8:
                        while not self.sample_q.empty():
                            self.sample_q.get_nowait()
                    self._take_sample(pos)
                self._schedule_sync()
                self._maybe_analyze()
                now = time.time()
                if now - last_forget_check > 2:
                    last_forget_check = now
                    if now - self.disk_check > 3:
                        self.disk_check = now
                        self.reload_dictionaries()
                    with self.lock:
                        stale = (self.mode == "live" and self.text and not self.injecting
                                 and now - self.last_edit > CFG["forget_after_seconds"])
                    if stale:
                        self.reset()
            except Exception as ex:
                dbg("worker error:", repr(ex))

    # ---------------- reading the real field (ground truth)
    def _schedule_sync(self):
        if not self.backend:
            return
        now = time.time()
        with self.lock:
            if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                return
            if now < self.read_backoff_until:
                return
            idle = now - self.last_edit
            go = False
            if self.want_sync and now >= self.want_sync and idle >= 0.12:
                self.want_sync, go = 0.0, True
            elif self.field_ok and idle >= 0.45 and (self.verified_rev != self.rev or now - self.last_verify > 5.0):
                go = True
        if go:
            self.sync_event.set()

    def _sync_loop(self):
        with self.backend.thread_context():
            while not self.stop.is_set():
                if self.sync_event.wait(0.4):
                    self.sync_event.clear()
                    self._read_cycle()
                elif self.bounds_fn and self.bounds_due and time.time() >= self.bounds_due:
                    self._bounds_pass()

    def _read_cycle(self):
        snap = None
        try:
            with self.lock:
                if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                    return
                rev0 = self.rev
                self._reading = True
            t0 = time.time()
            try:
                snap = self.backend.read()
            except Exception as ex:
                dbg("field read failed:", repr(ex))
            if time.time() - t0 > 1.5:
                self.read_backoff_until = time.time() + 10      # slow/hanging app: leave it alone
            self._adopt(snap, rev0)
            with self.lock:
                self.bounds_fn = snap.bounds if snap else None
                self.bounds_due = time.time() if self.bounds_fn else 0.0
            if self.bounds_fn:
                self._bounds_pass()
        except Exception as ex:
            dbg("read cycle error:", repr(ex))
        finally:
            with self.lock:
                self._reading = False
                self.sync_gen += 1

    def _sync_now(self, timeout: float = 1.5):
        """Force a read and wait for it (used right before injecting keystrokes)."""
        with self.lock:
            target = self.sync_gen + (2 if self._reading else 1)
        self.sync_event.set()
        end = time.time() + timeout
        while time.time() < end and self.sync_gen < target:
            time.sleep(0.02)

    def _adopt(self, snap, rev0: int):
        now = time.time()
        mismatch = False
        with self.lock:
            if self.mode != "live" or self.paused or self.app_ignored or self.injecting:
                return
            self.last_verify = now
            if snap is None:
                self.field_ok = False
                if self.stale:                    # this field cannot be followed: typed-only tracking
                    self.stale = False
                    self.text, self.caret, self.issues, self.selected = "", 0, [], None
                    self.complete, self.caret_known, self.start_sentence, self.base = False, True, False, 0
                    self._bump()
                self.verified_rev = self.rev
            else:
                self.field_ok = True
                if not self.stale and self.rev != rev0:
                    return                        # the user typed during the read: verify again later
                text, caret, sel = snap.text, snap.caret, snap.selection
                maxc, base, complete = CFG["max_field_chars"], 0, True
                if len(text) > maxc:
                    c = caret if caret is not None else 0
                    base = max(0, min(c - maxc // 2, len(text) - maxc))
                    text, complete = text[base:base + maxc], False
                    if caret is not None:
                        caret -= base
                        if not 0 <= caret <= len(text):
                            caret = None
                known = caret is not None and sel == 0
                if text == self.text and base == self.base:      # same text: keep issues, fix the caret
                    self.caret_known = known
                    if known:
                        self.caret = caret
                    self.stale, self.complete = False, complete
                    self.verified_rev, self.expect_toast = self.rev, False
                else:                                           # the field is the truth: adopt it
                    mismatch = self.expect_toast and not self.stale
                    self.text, self.base = text, base
                    self.caret = caret if caret is not None else len(text)
                    self.caret_known, self.complete = known, complete
                    self.start_sentence = base == 0
                    self.issues, self.selected = [], None
                    self.samples.clear()
                    self.stale = False
                    self._bump()
                    self.checked_part = self.checked_full = -1
                    self.verified_rev, self.expect_toast = self.rev, False
        if mismatch:
            self.toast("The text didn't change as expected — re-synced")
        self.notify()

    def adopt_probe(self, text: str):
        """Whole-field text obtained by the explicit clipboard scan (caret is at the end)."""
        text = _norm_nl(text)[:CFG["max_field_chars"]]
        with self.lock:
            if self.mode != "live":
                return
            self.text, self.caret, self.base = text, len(text), 0
            self.complete, self.caret_known, self.stale, self.start_sentence = True, True, False, True
            self.issues, self.selected = [], None
            self.samples.clear()
            self._bump()
            self.checked_part = self.checked_full = -1
        self.notify()
        self.toast(f"Scanned {len(text):,} characters")

    def _bounds_pass(self):
        fn = self.bounds_fn
        if not fn:
            return
        with self.lock:
            rev, base = self.rev, self.base
            items = [(i.key, i.hs + base, i.he + base) for i in self.issues[:40] if i.he > i.hs]
            self.bounds_due = 0.0
        ox, oy = window_origin()
        res = {}
        for key, a, b in items:
            r = fn(a, b)
            if r and r[2] > 2:
                res[key] = (int(r[0]), int(r[1] + r[3] - 1), int(r[2]))
        with self.lock:
            if self.rev != rev:
                return
            for i in self.issues:
                i.rect = res.get(i.key)
                i.origin = (ox, oy)
        self.notify()

    # ---------------- caret samples -> underline rectangles
    def _take_sample(self, pos: int):
        if not (self.locator and CFG["underlines"]):
            return
        time.sleep(0.04)                              # let the app move its caret
        loc = self.locator.get()
        if not loc:
            return
        ox, oy = window_origin()
        with self.lock:
            if self.mode != "live" or pos != self.caret or pos > len(self.text) or self.injecting:
                return
            prev = self.samples.get(pos - 1)
            if prev and abs(prev[1] - loc[1]) > loc[2] * 0.6:
                ws = pos
                while ws > 0 and not self.text[ws - 1].isspace():
                    ws -= 1                           # the word that wrapped moved: forget its samples
                self.samples = {k: v for k, v in self.samples.items() if k < ws}
            self.samples[pos] = (loc[0], loc[1], loc[2], ox, oy)
            self._refresh_rects()
        self.notify()

    def _sample_at(self, pos: int):
        s = self.samples.get(pos)
        if s:
            return s
        lo = hi = None
        for k in self.samples:
            if k < pos and (lo is None or k > lo):
                lo = k
            elif k > pos and (hi is None or k < hi):
                hi = k
        if lo is None or hi is None or hi - lo > 60:
            return None
        a, b = self.samples[lo], self.samples[hi]
        if (a[3], a[4]) != (b[3], b[4]) or abs(a[1] - b[1]) > max(6, a[2] * 0.6):
            return None
        t = (pos - lo) / (hi - lo)
        return (a[0] + (b[0] - a[0]) * t, a[1], a[2], a[3], a[4])

    def _refresh_rects(self):
        if self.bounds_fn:
            return                                # exact rectangles come from the accessibility API
        for it in self.issues:
            it.rect = None
            if self.mode != "live" or it.he <= it.hs or not self.samples:
                continue
            a, b = self._sample_at(it.hs), self._sample_at(it.he)
            if not a or not b or (a[3], a[4]) != (b[3], b[4]) or abs(a[1] - b[1]) > max(6, a[2] * 0.6):
                continue
            w = b[0] - a[0]
            if 3 <= w <= 1500:
                it.rect = (int(a[0]), int(a[1] + a[2] - 1), int(w))
                it.origin = (a[3], a[4])

    # ---------------- applying fixes
    def apply(self, key, idx: int):
        self._start_apply([(key, idx)])

    def apply_selected(self, idx: int):
        with self.lock:
            key = self.selected
        if key:
            self.apply(key, idx)

    NEAR = 1500                                       # in long texts, fix-all works near the caret

    def fix_all(self):
        with self.lock:
            autos = [i for i in self.issues if i.auto and i.sugg]
            far = 0
            if self.mode == "live" and len(self.text) > 4000:
                near = [i for i in autos if abs(i.s - self.caret) <= self.NEAR]
                far, autos = len(autos) - len(near), near
            plan = [(i.key, 0) for i in autos]
        if far:
            self.toast(f"Fixing the issues near your cursor; {far} farther away are left for you")
        if not plan:
            self.toast("Nothing to fix automatically")
            return
        self._start_apply(plan)

    def _start_apply(self, plan):
        if self.mode == "clipboard":
            self._apply_static(plan)
        else:
            threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def _choose(self, plan):
        """[(issue, replacement)] sorted right-to-left with overlaps removed. Call with lock held."""
        items = []
        for key, idx in plan:
            it = self._find(key)
            if it and 0 <= idx < len(it.sugg):
                items.append((it, it.sugg[idx]))
        items.sort(key=lambda t: (-t[0].s, -t[0].e))
        chosen, limit = [], None
        for it, rp in items:
            if limit is not None and it.e > limit:
                continue
            chosen.append((it, rp))
            limit = it.s
        return chosen

    def _apply_static(self, plan):
        with self.lock:
            chosen = self._choose(plan)
            for it, rp in chosen:
                self.text = self.text[:it.s] + rp + self.text[it.e:]
            self.issues = []
            self._bump()
            self.checked_part = self.checked_full = -1
        self.notify()
        if chosen:
            self.toast(f"Fixed {len(chosen)} issue{'s' if len(chosen) != 1 else ''}")

    def _tap(self, key, n: int):
        for i in range(n):
            self.kb.tap(key)
            if i % 12 == 11:
                time.sleep(0.004)                     # keystrokes queue up in the app; just don't flood

    def _goto(self, pos: int):
        d = pos - self.caret
        if d:
            self._tap(self.K.right if d > 0 else self.K.left, abs(d))
            with self.lock:
                self.caret = pos

    def _do_apply(self, plan):
        deadline = time.time() + 1.5                  # wait until the hotkey modifiers are released
        while self.mods and time.time() < deadline:
            time.sleep(0.02)
        if self.mods:
            self.toast("Release the modifier keys, then try again")
            return
        if not self.inject_lock.acquire(blocking=False):
            return
        done = 0
        try:
            if self.backend and self.field_ok:
                self._sync_now()                      # pre-flight: is our picture of the field still right?
            with self.lock:
                if not self._live_ok():
                    return
                if self.stale or not self.caret_known:
                    self.toast("Can't place the cursor yet (text selected or still syncing) — try again")
                    return
                chosen = self._choose(plan)
                if not chosen:
                    self.toast("The text changed meanwhile — suggestions refreshed")
                    return
                c0 = self.caret
                lo = min([c0] + [it.s for it, _ in chosen])
                hi = max([c0] + [it.e for it, _ in chosen])
                if hi - lo > 6000:
                    self.toast("That fix is far from your cursor — click near it and try again")
                    return
                if not text_is_safe_to_navigate(self.text[lo:hi]):
                    self.toast("Can't fix safely here (emoji or special characters in the way)")
                    return
                self.injecting = True
            self._ensure_focus()
            for it, rp in chosen:
                with self.lock:
                    old = self.text[it.s:it.e]
                    off, dl, ins = minimal_edit(old, rp)
                    p = it.s + off
                    end = p + dl
                self._goto(end)
                self._tap(self.K.backspace, dl)
                if ins:
                    self.kb.type(ins)
                with self.lock:
                    self.text = self.text[:p] + ins + self.text[end:]
                    self.caret = p + len(ins)
                    self.issues = [i for i in self.issues if i.key != it.key]
                    self._shift(p, end, len(ins))
                    if c0 >= end:
                        c0 += len(ins) - dl
                    elif c0 > p:
                        c0 = p + len(ins)
                done += 1
            self._goto(max(0, min(c0, len(self.text))))
            time.sleep(0.15)
        except Exception as ex:
            dbg("apply failed:", repr(ex))
            self.injecting = False
            self.invalidate() if self.backend else self.reset()
            self.toast("Stopped for safety — re-checking the field")
            return
        finally:
            self.inject_until = time.time() + 0.12
            with self.lock:
                self.injecting = False
                self._bump()
                self.checked_part = self.checked_full = -1
                self.verified_rev = -1                # verify the result against the real field
                self.expect_toast = bool(done and self.backend and self.field_ok)
            self.inject_lock.release()
        self.notify()
        if done:
            self.toast(f"Fixed {done} issue{'s' if done != 1 else ''}")

    def _ensure_focus(self):
        """If our own card has focus (e.g. macOS after a click), hand it back before typing."""
        cur = foreground()
        if cur.pid is not None and cur.pid == os.getpid() and self.target.wid is not None:
            if activate(self.target):
                time.sleep(0.15)
            else:
                raise RuntimeError("could not return focus to the target window")


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
MAC_VK = {18: "1", 19: "2", 20: "3", 45: "n", 11: "b", 2: "d", 13: "w", 3: "f", 35: "p",
          12: "q", 8: "c", 9: "v", 31: "o", 0: "a"}
IGNORED_KEY_NAMES = {"caps_lock", "num_lock", "scroll_lock", "print_screen", "pause",
                     "media_volume_up", "media_volume_down", "media_volume_mute",
                     "media_play_pause", "media_next", "media_previous"}


def key_name(key) -> Optional[str]:
    """Lower-case letter/digit of a key, robust to Ctrl/Alt changing .char."""
    vk, ch = getattr(key, "vk", None), getattr(key, "char", None)
    if MAC and vk in MAC_VK:
        return MAC_VK[vk]
    if vk is not None:
        if WIN and (48 <= vk <= 57 or 65 <= vk <= 90):
            return chr(vk).lower()
        if not WIN and not MAC and (48 <= vk <= 57 or 65 <= vk <= 90 or 97 <= vk <= 122):
            return chr(vk).lower()
    if ch and len(ch) == 1:
        o = ord(ch)
        return chr(o + 96) if 1 <= o <= 26 else ch.lower()
    return None


class Keys:
    """Global keyboard/mouse listener callbacks. Never raises into pynput's thread."""

    def __init__(self, model: Assistant, ui_q: queue.Queue):
        self.m, self.q = model, ui_q
        self.ui = None
        Key = keyboard.Key

        def ks(*names):
            return {getattr(Key, n) for n in names if hasattr(Key, n)}
        self.groups = {
            "ctrl": ks("ctrl", "ctrl_l", "ctrl_r"),
            "alt": ks("alt", "alt_l", "alt_r"),
            "shift": ks("shift", "shift_l", "shift_r"),
            "cmd": ks("cmd", "cmd_l", "cmd_r"),
        }
        self.altgr = ks("alt_gr")
        self.mod_keys = set().union(*self.groups.values()) | self.altgr
        self.Key = Key

    def held(self, group: str) -> bool:
        return bool(self.m.mods & self.groups[group])

    def chord_active(self) -> bool:
        if self.m.mods & self.altgr:
            return False                       # AltGr (= Ctrl+Alt on Windows) types characters
        return all(self.held(g) for g in CFG["hotkey_modifiers"])

    def on_press(self, key):
        try:
            self._press(key)
        except Exception as ex:
            dbg("key handler error:", repr(ex))
            try:
                self.m.reset()
            except Exception:
                pass

    def _press(self, key):
        m, Key = self.m, self.Key
        if key in self.mod_keys:
            m.mods.add(key)
            return
        if m.injecting or time.time() < m.inject_until:
            return                             # echo of our own injected keystrokes
        if self.chord_active():
            if self._hotkey(key_name(key)):
                return
            m.invalidate()                     # some other Ctrl+Alt combo: unknown effect
            return
        if m.mode != "live" or m.paused or m.app_ignored:
            return
        altgr = bool(m.mods & self.altgr)
        if not altgr:
            if self.held("ctrl") or self.held("cmd"):
                m.invalidate()                 # Ctrl/Cmd shortcuts (paste, undo...) change text: re-read
                return
            if self.held("alt") and not MAC:
                m.invalidate()
                return
        name = getattr(key, "name", "")
        if name in IGNORED_KEY_NAMES:
            return
        if MAC and self.held("alt") and not (getattr(key, "char", None) and key.char.isprintable()):
            m.invalidate()                     # Option+Arrow / Option+Backspace jump by words
            return
        shift = self.held("shift")
        if key == Key.space:
            m.type_char(" ")
        elif key == Key.backspace:
            m.backspace()
        elif key == Key.delete:
            m.delete_fwd()
        elif key == Key.enter:
            m.enter()
        elif key in (Key.left, Key.right) and not shift:
            m.move(-1 if key == Key.left else 1)
        elif getattr(key, "char", None):
            if key.char.isprintable():
                m.type_char(key.char)
            else:
                m.invalidate()
        else:
            m.invalidate()                     # Shift+arrows, Up/Down, Tab, Home/End, F-keys...

    def _hotkey(self, n: Optional[str]) -> bool:
        m = self.m
        if n in ("1", "2", "3"):
            m.apply_selected(int(n) - 1)
        elif n == "n":
            m.step(1)
        elif n == "b":
            m.step(-1)
        elif n == "d":
            m.ignore()
        elif n == "w":
            m.add_word()
        elif n == "f":
            m.fix_all()
        elif n == "p":
            m.toggle_pause()
        elif n == "a":
            self.q.put(("scan",))
        elif n == "c":
            self.q.put(("collapse",))
        elif n == "v":
            self.q.put(("clipboard",))
        elif n == "o":
            ensure_dictionary_folder()
            open_folder(DICT_DIR)
        elif n == "q":
            self.q.put(("quit",))
        else:
            return False
        return True

    def on_release(self, key):
        self.m.mods.discard(key)

    def on_click(self, x, y, button, pressed):
        try:
            if not pressed or self.m.injecting:
                return
            if self.ui and self.ui.point_in_card(x, y):
                return                         # clicking OUR card must not clear the issues
            self.m.invalidate()
        except Exception as ex:
            dbg("click handler error:", repr(ex))

    def on_scroll(self, *a):
        try:
            self.m.drop_marks()
        except Exception:
            pass


def watch_focus(model: Assistant, stop: threading.Event):
    """Reset on window change, remember the target window, flag ignored apps."""
    last = None
    while not stop.wait(0.25):
        t = foreground()
        if t.wid is None or t.pid == os.getpid():
            continue
        if last is not None and t.wid != last:
            model.mods.clear()                 # resync in case a key-release was missed
            model.reset(new_field=True)
        last = t.wid
        model.target = t
        ig = t.name if app_is_ignored(t.name, CFG["ignore_apps"]) else None
        if ig != model.app_ignored:
            model.app_ignored = ig
            model.reset(new_field=True)
            model.notify()


# --------------------------------------------------------------------------
# UI (Tk, main thread only)
# --------------------------------------------------------------------------
THEMES = {
    "dark": dict(bg="#111827", panel="#1f2937", border="#374151", fg="#f3f4f6", muted="#9ca3af",
                 chip="#273449", chip_hover="#334155", primary="#2563eb", primary_hover="#1d4ed8",
                 sel="#374151", good="#34d399"),
    "light": dict(bg="#ffffff", panel="#f3f4f6", border="#d1d5db", fg="#111827", muted="#6b7280",
                  chip="#e5e7eb", chip_hover="#d1d5db", primary="#2563eb", primary_hover="#1d4ed8",
                  sel="#dbeafe", good="#059669"),
}
KIND_COLOR = {"spelling": "#ef4444", "grammar": "#f59e0b", "punct": "#3b82f6", "style": "#8b5cf6"}
KIND_LABEL = {"spelling": "SPELLING", "grammar": "GRAMMAR", "punct": "PUNCTUATION", "style": "STYLE"}
PREVIEW_CHARS = 150
MAX_UNDERLINES = 24
CARD_MARGIN, BOTTOM_MARGIN = 16, 72


def pick_font_family(root) -> str:
    try:
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Noto Sans", "DejaVu Sans",
              "Helvetica", "Arial"):
        if f in fams:
            return f
    return "TkDefaultFont"


class UI:
    def __init__(self, root, model: Assistant, q=None):
        self.root, self.model, self.q = root, model, q
        self.th = THEMES[CFG["theme"]]
        fam = pick_font_family(root)
        self.f_ui = (fam, 10)
        self.f_bold = (fam, 10, "bold")
        self.f_small = (fam, 9)
        self.f_tiny = (fam, 8)
        self.f_big = (fam, 15, "bold")
        self.f_title = (fam, 11, "bold")
        self.collapsed = False
        self.user_pos = None
        self.card_rect = None
        self.snap = None
        self.toast_text, self.toast_until = "", 0.0
        self._sig = None
        self._rendered_at = 0.0
        self._pending = None
        self._drag = (0, 0)
        self._anchor = None
        self._line_state: dict = {}
        mods = "+".join(m.capitalize() for m in CFG["hotkey_modifiers"])
        self.hint = (f"{mods} + 1-3 apply · N/B next/prev · D ignore · W add word · F fix all · "
                     f"A scan field · V clipboard · C collapse · P pause · Q quit")

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        self.outer = tk.Frame(self.win, bg=self.th["border"], padx=1, pady=1)
        self.outer.pack(fill="both", expand=True)
        self.frame = tk.Frame(self.outer, bg=self.th["bg"], padx=12, pady=10)
        self.frame.pack(fill="both", expand=True)
        # map once off-screen so the window can be flagged non-activating BEFORE it is ever shown
        self.win.geometry("+-4000+-4000")
        self.win.deiconify()
        self.win.update()
        win_style(self.win)
        self.win.withdraw()
        self.lines = [self._make_line() for _ in range(MAX_UNDERLINES)] if CFG["underlines"] and (WIN or MAC) else []

    def _make_line(self):
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry("1x1+-4000+-4000")
        w.update()
        win_style(w, click_through=True)
        w.withdraw()
        return w

    # ---------------- small widgets
    def label(self, parent, text, font=None, fg=None, bg=None, **kw):
        return tk.Label(parent, text=text, font=font or self.f_ui, fg=fg or self.th["fg"],
                        bg=bg or self.th["bg"], anchor="w", justify="left", **kw)

    def button(self, parent, text, cmd, kind="chip"):
        th = self.th
        bg, hover, fg = {
            "chip": (th["chip"], th["chip_hover"], th["fg"]),
            "primary": (th["primary"], th["primary_hover"], "#ffffff"),
            "ghost": (th["bg"], th["chip"], th["muted"]),
        }[kind]
        lb = tk.Label(parent, text=text, bg=bg, fg=fg, padx=10, pady=4, cursor="hand2",
                      font=self.f_bold if kind == "primary" else self.f_ui)
        lb.bind("<Button-1>", lambda e: self.root.after_idle(cmd))
        lb.bind("<Enter>", lambda e: lb.configure(bg=hover))
        lb.bind("<Leave>", lambda e: lb.configure(bg=bg))
        return lb

    def point_in_card(self, x, y) -> bool:
        r = self.card_rect
        return bool(r and r[0] - 2 <= x < r[0] + r[2] + 2 and r[1] - 2 <= y < r[1] + r[3] + 2)

    # ---------------- placement
    def _drag_bind(self, *widgets):
        for w in widgets:
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Button-3>", self._drag_reset)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        self.user_pos = (e.x_root - self._drag[0], e.y_root - self._drag[1])
        self.place()

    def _drag_reset(self, e):
        self.user_pos = None
        self.place()

    def place(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        ms = get_monitors(self.root)
        if self.user_pos:
            ax, ay = self.user_pos[0] + w // 2, self.user_pos[1] + h // 2
        else:
            if self._anchor is None:
                self._anchor = self.root.winfo_pointerxy()
            ax, ay = self._anchor
        mx, my, mw, mh = monitor_at(ms, ax, ay)
        if self.user_pos:
            x, y = self.user_pos
        else:
            x, y = mx + mw - w - CARD_MARGIN, my + mh - h - BOTTOM_MARGIN
        x = max(mx + 4, min(x, mx + mw - w - 4))
        y = max(my + 4, min(y, my + mh - h - 4))
        if self.user_pos:
            self.user_pos = (x, y)
        self.win.geometry(f"+{int(x)}+{int(y)}")
        self.card_rect = (int(x), int(y), int(w), int(h))

    def _show(self):
        if self.win.state() == "withdrawn":
            self._anchor = None
            self.win.deiconify()
        self.win.lift()
        self.place()

    def _hide(self):
        if self.win.state() != "withdrawn":
            self.win.withdraw()
        self.card_rect = None
        self._anchor = None

    # ---------------- toasts / clipboard / collapse
    def show_toast(self, text):
        self.toast_text, self.toast_until = text, time.time() + 2.6
        self.render(self.model.snapshot(), force=True)

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.snap:
            self.render(self.snap, force=True)

    def toggle_clipboard(self):
        if self.model.mode == "clipboard":
            self.model.exit_static()
            return
        try:
            txt = self.root.clipboard_get()
        except Exception:
            txt = ""
        self.model.load_static(txt)

    # ---------------- explicit field scan (for apps whose text can't be read directly)
    def _on_ui(self, fn, timeout=2.0):
        """Run fn on the Tk thread from another thread and return its result."""
        done, box = threading.Event(), []

        def run():
            try:
                box.append(fn())
            except Exception:
                box.append(None)
            done.set()
        self.q.put(("call", run))
        done.wait(timeout)
        return box[0] if box else None

    def _clip_get(self):
        try:
            return self.root.clipboard_get()
        except Exception:
            return None

    def _clip_set(self, text):
        self.root.clipboard_clear()
        if text:
            self.root.clipboard_append(text)
        self.root.update()
        return True

    def scan_field(self):
        m = self.model
        if m.mode != "live" or m.app_ignored or m.paused:
            self.show_toast("Can't scan here (paused, ignored app, or clipboard mode)")
            return
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        m, K = self.model, self.model.K
        end = time.time() + 1.5
        while m.mods and time.time() < end:
            time.sleep(0.02)
        if m.mods:
            m.toast("Release the modifier keys, then try again")
            return
        if not m.inject_lock.acquire(blocking=False):
            return
        old = self._on_ui(self._clip_get)
        marker = f"\u200bwa-probe-{time.time()}"
        got = None
        try:
            self._on_ui(lambda: self._clip_set(marker))
            m.injecting = True
            with m.kb.pressed(K.ctrl):
                m.kb.tap("a")
            time.sleep(0.12)
            with m.kb.pressed(K.ctrl):
                m.kb.tap("c")
            time.sleep(0.25)
            got = self._on_ui(self._clip_get)
            m.kb.tap(K.right)                       # collapse the selection: caret goes to the end
            time.sleep(0.08)
        except Exception as ex:
            dbg("scan failed:", repr(ex))
        finally:
            m.injecting = False
            m.inject_until = time.time() + 0.5
            m.inject_lock.release()
            self._on_ui(lambda: self._clip_set(old))
        if got is None or got == marker:
            m.toast("Couldn't read this field")
            return
        m.adopt_probe(got)

    def copy_fixed(self):
        txt = self.model.static_text()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self.root.update()
            self.show_toast("Copied to the clipboard")
        except Exception:
            self.show_toast("Couldn't copy to the clipboard")

    def close_card(self):
        if self.model.mode == "clipboard":
            self.model.exit_static()
        else:
            self.model.reset()

    # ---------------- rendering
    def render(self, snap, force=False):
        self.snap = snap
        now = time.time()
        toast_on = now < self.toast_until
        issues = snap["issues"]
        static = snap["mode"] == "clipboard"
        show = static or toast_on or (bool(issues) and not snap["paused"] and not snap["app_ignored"])
        if not show:
            self._sig = None
            self._hide()
            self.draw_underlines()
            return
        sel_key = snap["selected"]
        sig = (static, sel_key, self.collapsed, toast_on, snap["paused"],
               tuple((i["key"], i["hs"], i["he"], tuple(i["sugg"]), i["message"]) for i in issues),
               len(snap["text"]) if static else snap["text"][-PREVIEW_CHARS:])
        visible = self.win.state() != "withdrawn"
        if sig == self._sig and visible and not force:
            self.draw_underlines()
            return
        if not force and visible and now - self._rendered_at < 0.2:
            self._pending = snap                       # rebuild at most ~5x/second while typing
            self.draw_underlines()
            return
        self._sig, self._rendered_at, self._pending = sig, now, None
        for w in self.frame.winfo_children():
            w.destroy()
        if not issues and not static:
            self.label(self.frame, "✓  " + (self.toast_text or "All clear"), self.f_title, self.th["good"]).pack()
        else:
            self._build(snap, toast_on)
        self._show()
        self.draw_underlines()

    def _build(self, snap, toast_on):
        th, f = self.th, self.frame
        issues, static = snap["issues"], snap["mode"] == "clipboard"
        sel = next((i for i in issues if i["key"] == snap["selected"]), issues[0] if issues else None)

        hdr = tk.Frame(f, bg=th["bg"])
        hdr.pack(fill="x")
        title = self.label(hdr, "✎  " + ("Clipboard check" if static else "Writing Assistant"), self.f_title)
        title.pack(side="left")
        n = len(issues)
        if n:
            badge = tk.Label(hdr, text=f" {n} ", bg=KIND_COLOR.get(sel["kind"], "#ef4444") if sel else "#ef4444",
                             fg="#ffffff", font=self.f_bold)
            badge.pack(side="left", padx=(8, 0))
        else:
            badge = None
        self.button(hdr, "✕", self.close_card, "ghost").pack(side="right")
        self.button(hdr, "+" if self.collapsed else "–", self.toggle_collapse, "ghost").pack(side="right")
        if not static:
            self.button(hdr, "▶" if snap["paused"] else "⏸", self.model.toggle_pause, "ghost").pack(side="right")
        self._drag_bind(hdr, title, *([badge] if badge else []))
        if toast_on:
            self.label(f, self.toast_text, self.f_small, th["good"]).pack(anchor="w", pady=(4, 0))
        if self.collapsed:
            return
        if not sel:
            self.label(f, "No issues found — nice writing.", self.f_ui, th["good"]).pack(anchor="w", pady=(10, 4))
            if static:
                acts = tk.Frame(f, bg=th["bg"])
                acts.pack(anchor="w", pady=(6, 0))
                self.button(acts, "Copy text", self.copy_fixed, "primary").pack(side="left")
            return

        self._preview(f, snap, issues, sel, static)

        color = KIND_COLOR.get(sel["kind"], "#ef4444")
        meta = tk.Frame(f, bg=th["bg"])
        meta.pack(fill="x", pady=(10, 0))
        tk.Label(meta, text=f" {KIND_LABEL.get(sel['kind'], 'ISSUE')} ", bg=color, fg="#ffffff",
                 font=self.f_tiny).pack(side="left")
        self.label(meta, sel["message"], self.f_small, th["muted"], wraplength=300).pack(side="left", padx=(8, 0))

        word = sel["word"].strip() or "␠"
        top = sel["sugg"][0] if sel["sugg"] else None
        line = tk.Frame(f, bg=th["bg"])
        line.pack(fill="x", pady=(6, 0))
        if len(word) > 40:
            word = word[:38] + "…"
        self.label(line, word, (self.f_big[0], 13, "overstrike"), th["muted"]).pack(side="left")
        if top is not None:
            self.label(line, "  →  ", self.f_big, th["muted"]).pack(side="left")
            self.label(line, (top if top.strip() else "(remove)")[:40], self.f_big, th["good"]).pack(side="left")

        row = tk.Frame(f, bg=th["bg"])
        row.pack(anchor="w", pady=(8, 0))
        if sel["sugg"]:
            for idx, s in enumerate(sel["sugg"][:3]):
                txt = s if s.strip() else "remove"
                self.button(row, f"{idx + 1}   {txt[:28]}",
                            lambda idx=idx, k=sel["key"]: self.model.apply(k, idx),
                            "primary" if idx == 0 else "chip").pack(side="left", padx=(0, 6))
        else:
            self.label(row, "No automatic fix — rewrite it yourself, or dismiss.", self.f_small, th["muted"]).pack()

        acts = tk.Frame(f, bg=th["bg"])
        acts.pack(fill="x", pady=(8, 0))
        self.button(acts, "Ignore", self.model.ignore, "ghost").pack(side="left")
        if sel["kind"] == "spelling":
            self.button(acts, "Add to dictionary", self.model.add_word, "ghost").pack(side="left")
        if len(issues) > 1:
            pos = [i["key"] for i in issues].index(sel["key"]) + 1
            self.button(acts, "‹", lambda: self.model.step(-1), "ghost").pack(side="left", padx=(8, 0))
            self.label(acts, f"{pos}/{len(issues)}", self.f_small, th["muted"]).pack(side="left")
            self.button(acts, "›", lambda: self.model.step(1), "ghost").pack(side="left")
        auto = sum(1 for i in issues if i["auto"] and i["sugg"])
        if auto > 1 or (static and auto):
            self.button(acts, f"Fix all ({auto})", self.model.fix_all, "chip").pack(side="right")
        if static:
            self.button(acts, "Copy text", self.copy_fixed, "chip").pack(side="right", padx=(0, 6))
        self.label(f, self.hint, self.f_tiny, th["muted"], wraplength=360).pack(anchor="w", pady=(8, 0))

    def _preview(self, f, snap, issues, sel, static):
        th, text = self.th, snap["text"]
        if static:
            base = max(0, sel["hs"] - 110)
            end = min(len(text), base + 300)
        else:
            end = len(text)
            base = max(0, end - PREVIEW_CHARS)
            if sel["hs"] < base:
                base = max(0, sel["hs"] - 30)
                end = min(end, base + 260)
        shown = text[base:end].replace("\n", "↵")
        t = tk.Text(f, width=46, height=4 if static else 3, wrap="word", bg=th["panel"], fg=th["fg"],
                    bd=0, highlightthickness=0, padx=8, pady=6, font=(self.f_ui[0], 11), cursor="arrow")
        t.insert("1.0", shown)
        for i in issues:
            a, b = max(i["hs"], base) - base, min(i["he"], end) - base
            if b <= a:
                continue
            tag = "i" + str(abs(hash(i["key"])))
            t.tag_add(tag, f"1.0+{a}c", f"1.0+{b}c")
            t.tag_config(tag, underline=True, foreground=KIND_COLOR.get(i["kind"], "#ef4444"),
                         background=th["sel"] if i["key"] == sel["key"] else th["panel"])
            t.tag_bind(tag, "<Button-1>", lambda e, k=i["key"]: self.model.select(k))
            t.tag_bind(tag, "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.config(cursor="arrow"))
        if not static:
            t.see("end")
        t.config(state="disabled")
        t.pack(fill="x", pady=(10, 0))

    # ---------------- in-place underlines
    def draw_underlines(self):
        if not self.lines:
            return
        want = []
        snap = self.snap
        if snap and snap["mode"] == "live" and not snap["paused"] and not snap["app_ignored"]:
            ox, oy = window_origin()
            ms = get_monitors(self.root)
            for i in snap["issues"]:
                if not i["rect"]:
                    continue
                x, y, w = i["rect"]
                x += ox - i["origin"][0]
                y += oy - i["origin"][1] + 1
                if any(m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3] for m in ms):
                    want.append((int(x), int(y), max(int(w), 6), KIND_COLOR.get(i["kind"], "#ef4444"),
                                 4 if i["key"] == snap["selected"] else 3))
        want.sort(key=lambda it: (it[1], it[0]))
        want = want[:len(self.lines)]
        for idx, w in enumerate(self.lines):
            try:
                spec = want[idx] if idx < len(want) else None
                state = self._line_state.get(idx)
                if spec is None:
                    if state is not None:
                        w.withdraw()
                        self._line_state[idx] = None
                    continue
                if state != spec:
                    x, y, wd, color, th = spec
                    w.configure(bg=color)
                    w.geometry(f"{wd}x{th}+{x}+{y}")          # geometry BEFORE mapping: WMs ignore 1x1 maps
                    if state is None:
                        w.deiconify()
                        w.lift()
                    self._line_state[idx] = spec
            except tk.TclError:
                self._line_state[idx] = None

    def tick(self):
        try:
            self.draw_underlines()
            now = time.time()
            if self.toast_until and now > self.toast_until:
                self.toast_until = 0.0
                self.render(self.model.snapshot(), force=True)
            elif self._pending is not None and now - self._rendered_at >= 0.2:
                self.render(self._pending, force=True)
        except Exception as ex:
            dbg("tick error:", repr(ex))
        self.root.after(250, self.tick)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
MODEL_HELP = ("Install the model's requirements with:  pip install transformers torch sentencepiece\n"
              "(the first run downloads the model, ~120 MB, from Hugging Face; after that it runs offline)")


def init_runtime() -> None:
    CFG.clear()
    CFG.update(load_config())
    ensure_dictionary_folder()
    LEX.set_user_words(load_all_dictionaries())


def run_check(argv) -> int:
    """`--check "some text"` (or text on stdin): print the issues, no GUI needed."""
    rest = [a for a in argv[argv.index("--check") + 1:] if not a.startswith("--")]
    text = " ".join(rest) if rest else sys.stdin.read()
    init_runtime()
    print(f"Loading {CFG['model_name']} ...", flush=True)
    if not ENGINE.load(CFG):
        print(f"Could not load the grammar model: {ENGINE.failed}\n{MODEL_HELP}")
        return 1
    while True:                                        # long texts are checked in time-limited passes
        prog: dict = {}
        issues = check_text(text, start_is_sentence=True, progress=prog)
        if prog.get("complete", True):
            break
    print(f"[{ENGINE.name} on {ENGINE.device}]")
    if not issues:
        print("No issues found.")
    for i in issues:
        sug = " | ".join(repr(s) for s in i.sugg[:3]) or "(no automatic fix)"
        print(f"{i.kind:9} {i.hs:4}-{i.he:<4} {i.word!r:28} {i.message}  ->  {sug}"
              + ("  [auto]" if i.auto else ""))
    return 0


def main() -> int:
    argv = sys.argv
    if "--version" in argv:
        print("Writing Assistant", VERSION)
        return 0
    if "--import" in argv:
        paths = [p for p in argv[argv.index("--import") + 1:] if not p.startswith("--")]
        print("Importing dictionaries...")
        import_dictionaries(paths)
        print("Done. They load automatically (within seconds if the assistant is running).")
        return 0
    if "--check" in argv:
        return run_check(argv)
    if tk is None or keyboard is None or mouse is None:
        print("Missing GUI dependencies. Install with:  pip install pynput pyspellchecker\n"
              "(On Linux you also need Tk: e.g. sudo apt install python3-tk, and an X11 session.)\n"
              "You can still try the checker without a GUI:  python writing_assistant.py --check \"teh text\"")
        return 1
    if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("[wa] Wayland session detected: global key capture only works for XWayland apps. "
              "Log in with an X11 session for full support.")

    make_dpi_aware()
    init_runtime()
    import types
    Key = keyboard.Key
    root = tk.Tk()
    root.withdraw()
    ui_q: queue.Queue = queue.Queue()
    backend = make_field_backend()
    model = Assistant(lambda: ui_q.put(("refresh",)), lambda t: ui_q.put(("toast", t)),
                      keyboard.Controller(),
                      types.SimpleNamespace(left=Key.left, right=Key.right, backspace=Key.backspace,
                                            ctrl=Key.cmd if MAC else Key.ctrl),
                      CaretLocator(), backend=backend)
    ui = UI(root, model, ui_q)
    keys = Keys(model, ui_q)
    keys.ui = ui

    kw = {}
    if WIN and CFG["windows_filter_injected"]:
        def injected_filter(msg, data):
            try:
                if data.flags & 0x10:                  # LLKHF_INJECTED
                    if not (model.injecting or time.time() < model.inject_until):
                        model.reset()                  # another program typed: we can't follow it
                    return False
            except Exception:
                pass
            return True
        kw["win32_event_filter"] = injected_filter
    kb_listener = keyboard.Listener(on_press=keys.on_press, on_release=keys.on_release, **kw)
    ms_listener = mouse.Listener(on_click=keys.on_click, on_scroll=keys.on_scroll)
    stop = threading.Event()

    def shutdown():
        stop.set()
        model.stop.set()
        for lst in (kb_listener, ms_listener):
            try:
                lst.stop()
            except Exception:
                pass
        with model.lock:
            model.text, model.issues = "", []          # drop the typed-text buffer
        try:
            root.destroy()
        except Exception:
            pass

    def poll():
        refresh, toasts, actions, quit_ = False, [], [], False
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg[0] == "refresh":
                    refresh = True
                elif msg[0] == "toast":
                    toasts.append(msg[1])
                elif msg[0] in ("collapse", "clipboard", "scan"):
                    actions.append(msg[0])
                elif msg[0] == "call":
                    try:
                        msg[1]()
                    except Exception as ex:
                        dbg("ui call failed:", repr(ex))
                elif msg[0] == "quit":
                    quit_ = True
        except queue.Empty:
            pass
        if quit_:
            shutdown()
            return
        try:
            for a in actions:
                {"collapse": ui.toggle_collapse, "clipboard": ui.toggle_clipboard, "scan": ui.scan_field}[a]()
            if toasts:
                ui.show_toast(toasts[-1])
            elif refresh:
                ui.render(model.snapshot())
        except Exception as ex:
            dbg("poll error:", repr(ex))
        root.after(50, poll)

    mods = "+".join(m.capitalize() for m in CFG["hotkey_modifiers"])
    print(f"Writing Assistant {VERSION} running.  Grammar model: {CFG['model_name']}")
    print(f"  Hotkeys ({mods} +): 1/2/3 apply · N/B next/prev · D ignore · W add word · F fix all · "
          f"A scan field · V check clipboard · C collapse · P pause · O dictionary folder · Q quit")
    if backend is None:
        print("  Field reading: off (typed-text tracking only).")
    else:
        extra = "" if not WIN or getattr(backend, "_auto", None) else "  (pip install uiautomation to also read browsers/Word)"
        print("  Field reading: on - checks the whole text field you are in." + extra)
    print(f"  Dictionary folder: {DICT_DIR}\n  Settings:          {CONFIG_PATH}")
    if MAC:
        print("  macOS: grant Accessibility + Input Monitoring to your terminal/Python in System Settings.")
    def load_engine():
        print(f"  Loading the grammar model (first run downloads it, ~120 MB)...", flush=True)
        ui_q.put(("toast", "Loading the grammar model…"))
        if ENGINE.load(CFG):
            print(f"  Grammar model ready ({ENGINE.device}).", flush=True)
            if ENGINE.spell is None:
                print("  Tip: pip install pyspellchecker  lets typos be labelled as spelling (optional).")
            ui_q.put(("toast", "Grammar model ready"))
            model.recheck()
        else:
            print(f"  Could not load the grammar model: {ENGINE.failed}\n  {MODEL_HELP}", flush=True)
            ui_q.put(("toast", "Grammar model failed to load — see the console"))

    model.start()
    threading.Thread(target=load_engine, daemon=True).start()
    kb_listener.start()
    ms_listener.start()
    threading.Thread(target=watch_focus, args=(model, stop), daemon=True).start()
    root.after(50, poll)
    root.after(250, ui.tick)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
