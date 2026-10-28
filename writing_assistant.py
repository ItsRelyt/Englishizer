#!/usr/bin/env python3
"""
Writing Assistant v2 - offline, system-wide spelling & punctuation helper.

What's new in v2
  * Suggestions PERSIST until you fix, ignore, or leave the text field (no timeout).
  * Grammarly-style underlines:
      - in the floating card, your recent text is shown with underlined issues
        (works in every app, click an underlined word to select it);
      - in-place underlines drawn directly under the word on screen, in apps that
        expose their caret position (classic Windows apps, many macOS apps).
  * The card is always kept fully on-screen (multi-monitor + HiDPI aware),
    can be dragged, and collapses to a small pill.
  * Much better logic: a real text model (so fixes can be applied to ANY earlier
    word, not just the last one), idle-pause checking, URL/email/code skipping,
    proper-noun handling, user dictionary, a/an, "should of", repeated words,
    spacing + punctuation rules, "Fix all", and window-change detection.

100% local. No network, no API keys. Typed text is kept only in memory (last
~1500 chars of the current field).

Dictionaries: on first run a folder is created at  ~/WritingAssistant/dictionaries/
with a ready-to-edit  my_words.txt.  Every .txt / .dic / .csv file in that folder is
loaded (one word per line; "#" starts a comment; Hunspell .dic files and
"word count" frequency lists work too). Drop in any word list, or run
    python writing_assistant.py --import mylist.txt other.dic
Edits are picked up automatically within a few seconds, no restart needed.
"Add to dictionary" appends to my_words.txt.

Install:   pip install pynput pyspellchecker
Optional:  pip install screeninfo          (accurate multi-monitor clamping)
           macOS in-place underlines: pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa

Hotkeys (hold Ctrl+Alt):
  1 / 2 / 3  apply suggestion      N  next issue       D  ignore
  W  add word to dictionary        F  fix all          P  pause/resume
  O  open dictionary folder        Q  quit
(You can also just click the chips on the card.)
"""

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pynput import keyboard, mouse
from spellchecker import SpellChecker

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
IDLE_FLUSH_SECONDS = 0.9   # check the word you're on after this much pause
MAX_TEXT = 1500            # characters of the current field kept in memory
ENTER_RESETS = True        # True: Enter clears state (safe for chat apps that send on Enter)
                           # False: Enter counts as a newline (better for docs/email)
FLAG_DOUBLE_SPACE = True
PREVIEW_CHARS = 140        # how much recent text the card previews
MAX_CHIPS = 6              # other-issue chips shown on the card
CARD_MARGIN = 14
BOTTOM_MARGIN = 70         # keeps card clear of taskbar / dock
DICT_DIR = Path.home() / "WritingAssistant" / "dictionaries"
MY_WORDS = DICT_DIR / "my_words.txt"
DICT_EXTS = {".txt", ".dic", ".csv", ".tsv"}
DICT_RELOAD_SECONDS = 3
DEBUG = "--debug" in sys.argv or bool(os.environ.get("WA_DEBUG"))
ESTIMATE_UNDERLINES = True  # fallback when an app hides its caret: approximate underlines
FALLBACK_CHAR_W = 8.0       # px per character used by the estimate (tune to your font size)
FALLBACK_LINE_H = 22        # px line height used by the estimate
MAX_UNDERLINES = 24


def dbg(*a):
    if DEBUG:
        print("[wa]", *a, flush=True)


BG, PANEL, FG, MUTED = "#111827", "#1f2937", "#f9fafb", "#9ca3af"
RED, BLUE, AMBER, GREEN = "#ef4444", "#3b82f6", "#f59e0b", "#10b981"
KIND_COLOR = {"spelling": RED, "grammar": RED, "punct": BLUE, "style": AMBER}
WORD_KINDS = {"spelling", "grammar", "style"}

spell = SpellChecker()

# --------------------------------------------------------------------------
# Language data
# --------------------------------------------------------------------------
CONTRACTIONS = {
    "dont": "don't", "cant": "can't", "isnt": "isn't", "doesnt": "doesn't",
    "didnt": "didn't", "wasnt": "wasn't", "couldnt": "couldn't",
    "shouldnt": "shouldn't", "wouldnt": "wouldn't", "havent": "haven't",
    "hasnt": "hasn't", "arent": "aren't", "werent": "weren't",
    "im": "I'm", "ive": "I've", "youre": "you're", "thats": "that's",
    "theres": "there's", "theyre": "they're", "whats": "what's",
    "hadnt": "hadn't", "wouldve": "would've", "shouldve": "should've",
    "couldve": "could've", "youve": "you've", "theyve": "they've",
}
CONTRACTIONS_OK = {
    "don't", "can't", "isn't", "doesn't", "didn't", "wasn't", "couldn't",
    "shouldn't", "wouldn't", "haven't", "hasn't", "aren't", "weren't", "i'm",
    "i've", "i'll", "i'd", "you're", "you've", "you'll", "you'd", "that's",
    "there's", "they're", "they've", "they'll", "what's", "let's", "hadn't",
    "won't", "we're", "we've", "we'll", "he's", "she's", "it's", "who's",
    "how's", "here's", "where's", "ain't", "y'all", "would've", "should've",
    "could've", "must've", "might've", "o'clock", "ma'am", "he'll", "she'll",
    "it'll", "he'd", "she'd", "we'd", "they'd", "mustn't", "needn't", "mightn't",
}
SLANG_OK = {
    "gonna", "wanna", "gotta", "kinda", "sorta", "ok", "okay", "lol", "omg",
    "btw", "thx", "pls", "plz", "yeah", "yep", "nope", "hey", "hmm", "umm",
    "wow", "tbh", "imo", "fyi", "asap", "vs", "etc", "wifi", "email", "emails",
    "online", "app", "apps", "iphone", "android", "youtube", "gmail", "google",
}
ABBREV = {"e.g", "i.e", "etc", "dr", "mr", "mrs", "ms", "prof", "vs", "inc",
          "ltd", "jr", "sr", "st", "no", "approx", "fig", "cf", "al"}
DUP_OK = {"had", "that", "is", "do", "very", "so", "really", "can"}
MODAL_OF = {("should", "of"), ("could", "of"), ("would", "of"), ("must", "of"),
            ("might", "of")}
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
SPECIAL = set("@/\\_#<>{}=+*~^|$%&")


def is_word_char(c: str) -> bool:
    return c.isalnum() or c in "'’"


def match_case(original: str, suggestion: str) -> str:
    if original.isupper() and len(original) > 1:
        return suggestion.upper()
    if original[:1].isupper():
        return suggestion[:1].upper() + suggestion[1:]
    return suggestion


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (counts a swap as one edit)."""
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


def freq(w: str) -> int:
    try:
        return int(spell.word_frequency[w])
    except Exception:
        return 0


def rank_suggestions(word: str, limit: int = 3):
    lw = word.lower()
    scored = []
    for c in (spell.candidates(lw) or set()):
        if c != lw:
            scored.append(((edit_distance(lw, c), 0 if c[0] == lw[0] else 1, -freq(c)), c))
    # word-split candidates: "thankyou" -> "thank you"
    for i in range(1, len(lw)):
        a, b = lw[:i], lw[i:]
        if (len(a) > 1 or a in ("a", "i")) and (len(b) > 1 or b in ("a", "i")):
            if spell.known([a]) and spell.known([b]):
                scored.append(((1.5, 0, -min(freq(a), freq(b))), f"{a} {b}"))
    scored.sort(key=lambda x: x[0])
    out = []
    for _, c in scored:
        c = match_case(word, c)
        if c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Platform helpers
# --------------------------------------------------------------------------
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"

if WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def make_dpi_aware():
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def win_style(win, click_through=False):
    """Windows: make a Tk window non-activating (never steals focus)."""
    if not WIN:
        return
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        ex |= 0x08000000 | 0x00000080            # NOACTIVATE | TOOLWINDOW
        if click_through:
            ex |= 0x00000020                     # TRANSPARENT (LAYERED already set by -alpha)
        user32.SetWindowLongW(hwnd, -20, ex)
    except Exception:
        pass


def foreground():
    """(window id, pid) of the active window, best effort."""
    try:
        if WIN:
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return (hwnd, pid.value)
        if MAC:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return (app.processIdentifier(), app.processIdentifier())
        out = subprocess.check_output(["xdotool", "getactivewindow"], timeout=0.3,
                                      stderr=subprocess.DEVNULL)
        return (out.strip(), None)
    except Exception:
        return (None, None)


def window_origin():
    """Top-left of the active window (Windows only) so underlines follow moves."""
    if WIN:
        try:
            r = wintypes.RECT()
            user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(r))
            return (r.left, r.top)
        except Exception:
            pass
    return (0, 0)


class CaretLocator:
    """Screen position of the text caret: (x, y_top, height) or None."""

    def __init__(self):
        self.errors = 0

    def get(self):
        if self.errors > 25:
            return None
        try:
            if WIN:
                r = self._win()
            elif MAC:
                r = self._mac()
            else:
                r = None
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
            kAXBoundsForRangeParameterizedAttribute, kAXValueCFRangeType,
            kAXValueCGRectType)
        from CoreFoundation import CFRangeMake
        sysw = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(sysw, kAXFocusedUIElementAttribute, None)
        if err or el is None:
            return None
        err, rng = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute, None)
        if err or rng is None:
            return None
        ok, cf = AXValueGetValue(rng, kAXValueCFRangeType, None)
        if not ok or cf.location < 1:
            return None
        prev = AXValueCreate(kAXValueCFRangeType, CFRangeMake(cf.location - 1, 1))
        err, bounds = AXUIElementCopyParameterizedAttributeValue(
            el, kAXBoundsForRangeParameterizedAttribute, prev, None)
        if err or bounds is None:
            return None
        ok, rect = AXValueGetValue(bounds, kAXValueCGRectType, None)
        if not ok:
            return None
        return (rect.origin.x + rect.size.width, rect.origin.y, rect.size.height)


_mon_cache = (0.0, [])


def get_monitors(root):
    global _mon_cache
    if time.time() - _mon_cache[0] < 3 and _mon_cache[1]:
        return _mon_cache[1]
    ms = []
    try:
        from screeninfo import get_monitors as gm
        ms = [(m.x, m.y, m.width, m.height) for m in gm()]
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


MY_WORDS_TEMPLATE = """\
# Writing Assistant - your personal dictionary
# One word per line. Lines starting with # are ignored.
# Anything you add here is treated as correctly spelled (names, jargon, slang...).
# You can also drop other word lists (.txt / .dic / .csv) into this same folder -
# they are all loaded automatically, no restart needed.
"""


def ensure_dictionary_folder():
    """Create ~/WritingAssistant/dictionaries/ and my_words.txt if missing."""
    try:
        DICT_DIR.mkdir(parents=True, exist_ok=True)
        old = Path.home() / ".writing_assistant_words.txt"      # migrate v1/v2 file
        if not MY_WORDS.exists():
            MY_WORDS.write_text(MY_WORDS_TEMPLATE, encoding="utf-8")
            if old.exists():
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(old.read_text(encoding="utf-8", errors="ignore"))
    except Exception as ex:
        print("[wa] could not create dictionary folder:", ex)


def parse_dictionary_file(path):
    """Words from a .txt/.csv/.dic file. Tolerates comments, 'word count' lists
    and Hunspell 'word/FLAGS' entries."""
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
        if n == 0 and tok.isdigit():              # Hunspell word-count header
            continue
        if tok and not tok.isdigit() and len(tok) < 60:
            words.add(tok.lower().replace("’", "'"))
    return words


def dictionary_signature():
    try:
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                            for p in DICT_DIR.iterdir() if p.suffix.lower() in DICT_EXTS))
    except Exception:
        return ()


def load_all_dictionaries():
    words = set()
    try:
        for p in sorted(DICT_DIR.iterdir()):
            if p.suffix.lower() in DICT_EXTS:
                words |= parse_dictionary_file(p)
    except Exception:
        pass
    return words


def import_dictionaries(paths):
    """--import: copy word lists into the dictionary folder."""
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
        shutil.copy2(src, dst)
        print(f"  imported {src.name}: {len(parse_dictionary_file(dst))} words -> {dst}")


def open_folder(path):
    try:
        if WIN:
            os.startfile(str(path))
        elif MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as ex:
        print("[wa] could not open folder:", ex)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
@dataclass
class Issue:
    id: int
    kind: str            # spelling | grammar | punct | style
    label: str
    s: int               # edit span start (in text)
    e: int               # edit span end
    hs: int              # highlight span start
    he: int              # highlight span end
    word: str
    suggestions: list    # [(label, replacement)]
    rect: Optional[tuple] = None    # (x, y, width) screen underline
    origin: tuple = (0, 0)
    char_w: float = 7.0


class Assistant:
    """Tracks the text you type in the current field and finds issues in it."""

    def __init__(self, notify, toast, locator):
        self.notify, self.toast, self.locator = notify, toast, locator
        self.lock = threading.RLock()
        self.apply_lock = threading.Lock()
        self.kb = keyboard.Controller()
        self.text = ""
        self.issues = []
        self.selected = None
        self.samples = {}          # text index -> (x, y, h, ox, oy) caret samples
        self.checked_upto = 0
        self.flushed_len = -1
        self.last_key = 0.0
        self.next_id = 1
        self.ignored = set()
        ensure_dictionary_folder()
        self.user_words = set()
        self.dict_sig = None
        self._last_dict_check = 0.0
        self.reload_dictionaries(quiet=True)
        self.paused = False
        self.injecting = False
        self.mods = set()
        self.anchor = None         # last click position (for estimated underlines)
        self.sample_q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------- public state helpers
    def snapshot(self):
        with self.lock:
            return {
                "text": self.text, "selected": self.selected, "paused": self.paused,
                "issues": [dict(id=i.id, kind=i.kind, label=i.label, word=i.word,
                                hs=i.hs, he=i.he, suggestions=list(i.suggestions),
                                rect=i.rect, origin=i.origin)
                           for i in sorted(self.issues, key=lambda x: x.hs)],
            }

    def reset(self, anchor=None):
        with self.lock:
            self.anchor = anchor
            had = bool(self.issues or self.text)
            self.text, self.issues, self.selected = "", [], None
            self.samples.clear()
            self.checked_upto, self.flushed_len = 0, -1
        self.sample_q.put(0)           # caret position after a click/nav = start of buffer
        if had:
            self.notify()

    def drop_rects(self):
        with self.lock:
            for i in self.issues:
                i.rect = None
            self.samples.clear()
        self.notify()

    def select(self, iid):
        with self.lock:
            if any(i.id == iid for i in self.issues):
                self.selected = iid
        self.notify()

    def next_issue(self):
        with self.lock:
            ordered = sorted(self.issues, key=lambda x: x.hs)
            if not ordered:
                return
            ids = [i.id for i in ordered]
            idx = (ids.index(self.selected) + 1) % len(ids) if self.selected in ids else 0
            self.selected = ids[idx]
        self.notify()

    def _selected_issue(self):
        return next((i for i in self.issues if i.id == self.selected), None)

    def ignore(self, iid=None):
        with self.lock:
            iss = next((i for i in self.issues if i.id == (iid or self.selected)), None)
            if not iss:
                return
            if iss.kind == "spelling":
                lw = iss.word.lower()
                self.ignored.add(lw)
                self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            else:
                self.issues = [i for i in self.issues if i.id != iss.id]
            self._fix_selected()
        self.notify()

    def reload_dictionaries(self, quiet=False):
        sig = dictionary_signature()
        if sig == self.dict_sig:
            return
        first = self.dict_sig is None
        self.dict_sig = sig
        words = load_all_dictionaries()
        with self.lock:
            self.user_words = words
            # also teach the spell-checker so these words can be SUGGESTED, not just accepted
            try:
                spell.word_frequency.load_words(sorted(words))
            except Exception:
                pass
            # drop issues for words that are now in the dictionary
            self.issues = [i for i in self.issues
                           if not (i.kind == "spelling" and i.word.lower() in words)]
            self._fix_selected()
        dbg("dictionary loaded:", len(words), "words from", DICT_DIR)
        if not quiet and not first:
            self.toast(f"Dictionary reloaded ({len(words)} words)")
        self.notify()

    def add_word(self, iid=None):
        with self.lock:
            iss = next((i for i in self.issues if i.id == (iid or self.selected)), None)
            if not iss or iss.kind != "spelling":
                return
            lw = iss.word.lower()
            self.user_words.add(lw)
            try:
                ensure_dictionary_folder()
                with open(MY_WORDS, "a", encoding="utf-8") as f:
                    f.write(lw + "\n")
                self.dict_sig = dictionary_signature()     # we wrote it ourselves
            except Exception:
                pass
            self.issues = [i for i in self.issues if not (i.kind == "spelling" and i.word.lower() == lw)]
            self._fix_selected()
        self.notify()
        self.toast(f"Added “{lw}” to dictionary")

    def toggle_pause(self):
        self.paused = not self.paused
        self.reset()
        self.toast("Paused" if self.paused else "Resumed")

    def _fix_selected(self):
        if self.selected not in {i.id for i in self.issues}:
            self.selected = self.issues[-1].id if self.issues else None

    # ---------------- keystroke input
    def type_char(self, c):
        with self.lock:
            if self.paused:
                return
            self.last_key = time.time()
            self.flushed_len = -1
            n0 = len(self.text)
            if is_word_char(c):   # word is being extended -> its old verdict is stale
                self.issues = [i for i in self.issues if not (i.kind in WORD_KINDS and i.he == n0)]
            self.text += c
            self._trim()
            n = len(self.text)
            if is_word_char(c):
                self._rule_missing_space(c)
                if n == 1:
                    self.sample_q.put(1)
            else:
                self._rule_punct(c)
                if c.isspace():
                    self._analyze_range(self.checked_upto, n)
                    self.checked_upto = n
                self.sample_q.put(n)
            self._fix_selected()
        self.notify()

    def backspace(self):
        with self.lock:
            if self.paused or not self.text:
                return
            self.last_key = time.time()
            self.flushed_len = -1
            self.text = self.text[:-1]
            n = len(self.text)
            self.issues = [i for i in self.issues if i.he <= n and i.e <= n]
            self.samples = {k: v for k, v in self.samples.items() if k <= n}
            ts = n
            while ts > 0 and not self.text[ts - 1].isspace():
                ts -= 1
            self.checked_upto = min(self.checked_upto, ts)
            self._fix_selected()
        self.notify()

    def enter(self):
        if ENTER_RESETS:
            self.reset()
        else:
            self.type_char("\n")

    def _trim(self):
        if len(self.text) <= MAX_TEXT:
            return
        cut = len(self.text) - (MAX_TEXT - 200)
        self.text = self.text[cut:]
        keep = []
        for i in self.issues:
            if i.s >= cut and i.hs >= cut:
                i.s -= cut; i.e -= cut; i.hs -= cut; i.he -= cut
                keep.append(i)
        self.issues = keep
        self.samples = {k - cut: v for k, v in self.samples.items() if k >= cut}
        self.checked_upto = max(0, self.checked_upto - cut)

    # ---------------- rule engine
    def _add(self, kind, label, s, e, hs, he, suggs):
        word = self.text[hs:he]
        self.issues = [i for i in self.issues if not (i.hs == hs and i.he == he)]
        iss = Issue(self.next_id, kind, label, s, e, hs, he, word, suggs)
        self.next_id += 1
        self.issues.append(iss)
        self.selected = iss.id
        self._update_rect(iss)
        dbg("issue", kind, repr(word), [x[0] for x in suggs])

    def _rule_punct(self, c):
        t, n = self.text, len(self.text)
        if c == " " and FLAG_DOUBLE_SPACE and n >= 3 and t[n - 2] == " " and t[n - 3] not in " \t\n.!?":
            self._add("punct", "Extra space", n - 1, n, n - 2, n, [("remove extra space", "")])
        if c in ",.!?;" and n >= 3 and t[n - 2] == " " and (t[n - 3].isalnum() or t[n - 3] in ")\"'”’"):
            self._add("punct", f"Space before “{c}”", n - 2, n - 1, n - 2, n, [("remove space", "")])
        if c == "," and n >= 2 and t[n - 2] == ",":
            self._add("punct", "Repeated comma", n - 1, n, n - 2, n, [("remove extra comma", "")])

    def _rule_missing_space(self, c):
        t, n = self.text, len(self.text)
        if n < 3 or not c.isalpha() or not t[n - 3].isalpha():
            return
        p = t[n - 2]
        ok = p in ",;!?" or (p == "." and c.isupper() and t[n - 3].islower()
                             and n >= 5 and t[n - 4].isalpha() and t[n - 5].isalpha())
        if not ok:
            return
        a = n
        while a > 0 and not t[a - 1].isspace():
            a -= 1
        tok = t[a:n]
        if any(ch in SPECIAL or ch.isdigit() for ch in tok) or "://" in tok:
            return
        self._add("punct", f"Missing space after “{p}”", n - 1, n - 1, n - 3, n, [("add a space", " ")])

    def _analyze_range(self, a, b):
        for m in re.finditer(r"\S+", self.text[a:b]):
            self._analyze_token(a + m.start(), a + m.end())

    def _analyze_token(self, ts, te):
        # forget old word-level verdicts for words ending inside this token
        self.issues = [i for i in self.issues if not (i.kind in WORD_KINDS and ts <= i.he <= te)]
        cs, ce = ts, te
        while cs < ce and not self.text[cs].isalnum():
            cs += 1
        while ce > cs and not self.text[ce - 1].isalnum():
            ce -= 1
        core = self.text[cs:ce]
        if not core:
            return
        # URLs, emails, paths, code, numbers, filenames -> leave alone
        if (any(ch in SPECIAL or ch.isdigit() for ch in core) or "://" in core
                or "." in core or "www" in core.lower()):
            return
        for m in WORD_RE.finditer(core):
            self._analyze_word(cs + m.start(), cs + m.end())

    def _prev_word(self, s):
        t = self.text
        if s >= 2 and t[s - 1] == " " and t[s - 2].isalpha():
            pe = s - 1
            ps = pe
            while ps > 0 and (t[ps - 1].isalpha() or t[ps - 1] in "'’"):
                ps -= 1
            return ps, pe, t[ps:pe]
        return None

    def _sentence_start(self, s):
        t = self.text
        i = s - 1
        while i >= 0 and t[i] in "\"'“‘([":
            i -= 1
        if i < 0 or not t[i].isspace():
            return False
        while i >= 0 and t[i].isspace():
            i -= 1
        if i < 0 or t[i] not in ".!?":
            return False
        if t[i] == ".":
            k = i
            while k > 0 and (t[k - 1].isalpha() or t[k - 1] == "."):
                k -= 1
            abbr = t[k:i].lower().strip(".")
            if abbr in ABBREV or len(abbr) == 1:
                return False
        return True

    def _analyze_word(self, s, e):
        t = self.text
        w = t[s:e]
        lw = w.lower().replace("’", "'")
        if lw in self.ignored or lw in self.user_words:
            return
        prev = self._prev_word(s)
        strict_start = self._sentence_start(s)

        # --- missing apostrophe / lowercase I
        if lw in CONTRACTIONS:
            fix = CONTRACTIONS[lw] if lw in ("im", "ive") else match_case(w, CONTRACTIONS[lw])
            self._add("grammar", "Missing apostrophe", s, e, s, e, [(fix, fix)])
            return
        if w == "i":
            self._add("grammar", "Capitalize “I”", s, e, s, e, [("I", "I")])
            return
        if w[0] == "i" and lw in ("i'm", "i've", "i'll", "i'd"):
            fix = "I" + w[1:]
            self._add("grammar", "Capitalize “I”", s, e, s, e, [(fix, fix)])
            return

        # --- context rules using the previous word
        if prev:
            ps, pe, pw = prev
            pl = pw.lower()
            if pl == lw and lw.isalpha() and len(lw) > 1 and lw not in DUP_OK:
                self._add("grammar", "Repeated word", pe, e, s, e, [(f"remove “{w}”", "")])
                return
            if (pl, lw) in MODAL_OF:
                self._add("grammar", f"“{pw} {w}” → “{pw} have”", s, e, ps, e, [("have", "have")])
                return
            if (pl, lw) == ("your", "welcome"):
                fix = match_case(pw, "you're")
                self._add("grammar", "“your” → “you're”", ps, pe, ps, e, [(fix, fix)])
            elif pl == "a" and lw[0] in "aeio" and not w.isupper() and not lw.startswith(("one", "once", "eu", "ou")) \
                    and not (len(w) > 1 and w[0].islower() and w[1].isupper()):
                fix = match_case(pw, "an")
                self._add("grammar", "Use “an” before a vowel sound", ps, pe, ps, e, [(fix, fix)])
            elif pl == "an" and lw[0] in "bcdgjkmnpqrstvwyz" and not w.isupper():
                fix = match_case(pw, "a")
                self._add("grammar", "Use “a” before a consonant sound", ps, pe, ps, e, [(fix, fix)])

        # --- spelling
        if "'" in lw:
            return                                   # other apostrophe words: leave alone
        if len(lw) < 2 or w.isupper() or any(ch.isupper() for ch in w[1:]):
            return                                   # acronyms, CamelCase, iPhone ...
        if lw in SLANG_OK or spell.known([lw]):
            if strict_start and w[0].islower():
                fix = w[0].upper() + w[1:]
                self._add("style", "Capitalize first word of sentence", s, e, s, e, [(fix, fix)])
            return
        if w[0].isupper() and not (strict_start or s == 0 or t[s - 1] == "\n"):
            return                                   # probably a name
        sugg = rank_suggestions(w)
        self._add("spelling", "Possible misspelling", s, e, s, e, [(x, x) for x in sugg])

    # ---------------- caret sampling -> screen rectangles for underlines
    def _update_rect(self, iss):
        pts = sorted(self.samples.items())
        if len(pts) < 2:
            return
        before = [p for p in pts if p[0] <= iss.hs]
        after = [p for p in pts if p[0] >= iss.he]
        if before and after:
            (k1, v1), (k2, v2) = before[-1], after[0]
        elif len(after) >= 2:
            (k1, v1), (k2, v2) = after[0], after[1]
        elif len(before) >= 2:
            (k1, v1), (k2, v2) = before[-2], before[-1]
        else:
            return
        if k2 <= k1 or k2 - k1 > 250:
            return
        if (v1[3], v1[4]) != (v2[3], v2[4]) or abs(v2[1] - v1[1]) > max(6, v1[2] * 0.6):
            return
        avg = (v2[0] - v1[0]) / (k2 - k1)
        if not (1 < avg < 40):
            return
        x = v1[0] + avg * (iss.hs - k1)
        w = max(avg * (iss.he - iss.hs), 6)
        iss.rect = (int(x), int(v1[1] + v1[2] - 1), int(w))
        iss.origin = (v1[3], v1[4])
        iss.char_w = avg

    def _worker(self):
        while True:
            try:
                key = self.sample_q.get(timeout=0.2)
            except queue.Empty:
                key = None
            if key is not None:
                time.sleep(0.06)                     # let the app move its caret
                pos = self.locator.get()
                if not pos and ESTIMATE_UNDERLINES and self.anchor:
                    with self.lock:
                        ok = key <= len(self.text) and "\n" not in self.text[:key]
                    if ok:
                        ax, ay = self.anchor
                        pos = (ax + FALLBACK_CHAR_W * key, ay - FALLBACK_LINE_H / 2, FALLBACK_LINE_H)
                dbg("caret sample", key, pos)
                if pos:
                    ox, oy = window_origin()
                    with self.lock:
                        if key <= len(self.text):
                            self.samples[key] = (pos[0], pos[1], pos[2], ox, oy)
                            for i in self.issues:
                                if i.rect is None:
                                    self._update_rect(i)
                    self.notify()
            self._idle_check()
            if time.time() - self._last_dict_check > DICT_RELOAD_SECONDS:
                self._last_dict_check = time.time()
                try:
                    self.reload_dictionaries()
                except Exception as ex:
                    dbg("dictionary reload failed:", ex)

    def _idle_check(self):
        with self.lock:
            if self.paused or not self.text or time.time() - self.last_key < IDLE_FLUSH_SECONDS:
                return
            if self.flushed_len == len(self.text):
                return
            self.flushed_len = len(self.text)
            if self.checked_upto < len(self.text):
                self._analyze_range(self.checked_upto, len(self.text))
            self._fix_selected()
            n = len(self.text)
        self.sample_q.put(n)
        self.notify()

    # ---------------- applying fixes
    def apply_selected(self, idx):
        with self.lock:
            iss = self._selected_issue()
            if not iss or idx >= len(iss.suggestions):
                return
            plan = [(iss.id, iss.suggestions[idx][1])]
        threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def apply(self, iid, idx):
        with self.lock:
            iss = next((i for i in self.issues if i.id == iid), None)
            if not iss or idx >= len(iss.suggestions):
                return
            plan = [(iss.id, iss.suggestions[idx][1])]
        threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def fix_all(self):
        with self.lock:
            plan, limit = [], None
            for i in sorted(self.issues, key=lambda x: x.s, reverse=True):
                if not i.suggestions or (limit is not None and i.e > limit):
                    continue
                plan.append((i.id, i.suggestions[0][1]))
                limit = i.s
        if plan:
            threading.Thread(target=self._do_apply, args=(plan,), daemon=True).start()

    def _tap(self, key, n):
        for _ in range(n):
            self.kb.tap(key)
            time.sleep(0.004)

    def _do_apply(self, plan):
        deadline = time.time() + 2          # wait for Ctrl/Alt to be released
        while self.mods and time.time() < deadline:
            time.sleep(0.02)
        with self.apply_lock:
            self.injecting = True
            try:
                for iid, repl in plan:
                    with self.lock:
                        iss = next((i for i in self.issues if i.id == iid), None)
                        if not iss:
                            continue
                        s, e = iss.s, iss.e
                        n_left = len(self.text) - e
                    dbg("apply", repr(iss.word), "->", repr(repl), "left", n_left, "del", e - s)
                    self._tap(keyboard.Key.left, n_left)
                    self._tap(keyboard.Key.backspace, e - s)
                    if repl:
                        self.kb.type(repl)
                    self._tap(keyboard.Key.right, n_left)
                    with self.lock:
                        self._after_fix(iss, repl)
                time.sleep(0.25)
            finally:
                self.injecting = False
                self.last_key = time.time()
        self.notify()

    def _after_fix(self, iss, repl):
        delta = len(repl) - (iss.e - iss.s)
        self.text = self.text[:iss.s] + repl + self.text[iss.e:]
        keep = []
        for j in self.issues:
            if j.id == iss.id:
                continue
            if j.he <= iss.s and j.e <= iss.s:
                keep.append(j)
            elif j.s >= iss.e and j.hs >= iss.e:
                j.s += delta; j.e += delta; j.hs += delta; j.he += delta
                if j.rect and iss.rect and abs(j.rect[1] - iss.rect[1]) < 5:
                    j.rect = (int(j.rect[0] + delta * iss.char_w), j.rect[1], j.rect[2])
                else:
                    j.rect = None
                keep.append(j)
        self.issues = keep
        self.samples = {k: v for k, v in self.samples.items() if k <= iss.s}
        if self.checked_upto > iss.e:
            self.checked_upto = max(0, self.checked_upto + delta)
        self._fix_selected()


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
             keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r}
ALT_KEYS = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r}
MOD_KEYS = CTRL_KEYS | ALT_KEYS | {keyboard.Key.shift, keyboard.Key.shift_l,
                                   keyboard.Key.shift_r, keyboard.Key.caps_lock}
if hasattr(keyboard.Key, "alt_gr"):
    ALT_KEYS.add(keyboard.Key.alt_gr)
    MOD_KEYS.add(keyboard.Key.alt_gr)


MAC_VK = {18: "1", 19: "2", 20: "3", 45: "n", 2: "d", 13: "w", 3: "f", 35: "p", 12: "q"}


def key_name(key):
    """Lower-case letter/digit for a key press, robust to Ctrl modifying .char."""
    vk, ch = getattr(key, "vk", None), getattr(key, "char", None)
    if MAC and vk in MAC_VK:
        return MAC_VK[vk]
    if WIN and vk is not None and (48 <= vk <= 57 or 65 <= vk <= 90):
        return chr(vk).lower()
    if ch and len(ch) == 1:
        o = ord(ch)
        return chr(o + 96) if 1 <= o <= 26 else ch.lower()
    return None


class Keys:
    def __init__(self, model, ui_q):
        self.m, self.q = model, ui_q
        self.ui = None

    def on_press(self, key):
        m = self.m
        if key in MOD_KEYS:
            m.mods.add(key)
            return
        if m.injecting:
            return
        dbg("key", key)
        ctrl, alt = bool(m.mods & CTRL_KEYS), bool(m.mods & ALT_KEYS)
        if ctrl and alt:
            n = key_name(key)
            if n in ("1", "2", "3"):
                m.apply_selected(int(n) - 1)
            elif n == "n":
                m.next_issue()
            elif n == "d":
                m.ignore()
            elif n == "w":
                m.add_word()
            elif n == "o":
                ensure_dictionary_folder()
                open_folder(DICT_DIR)
            elif n == "f":
                m.fix_all()
            elif n == "p":
                m.toggle_pause()
            elif n == "q":
                self.q.put(("quit",))
            else:
                m.reset()          # e.g. AltGr-typed symbol: we can't track it
            return
        if m.paused:
            return
        if ctrl:                    # Ctrl/Cmd shortcuts change text unpredictably
            m.reset()
            return
        if key == keyboard.Key.space:
            m.type_char(" ")
        elif key == keyboard.Key.backspace:
            m.backspace()
        elif key == keyboard.Key.enter:
            m.enter()
        elif getattr(key, "char", None):
            if key.char.isprintable():
                m.type_char(key.char)
        else:                       # arrows, tab, delete, esc, F-keys ...
            m.reset()

    def on_release(self, key):
        self.m.mods.discard(key)

    def on_click(self, x, y, button, pressed):
        if not pressed or self.m.injecting:
            return
        r = self.ui.card_rect if self.ui else None
        if r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
            dbg("click on card - ignored by tracker")
            return                      # clicking OUR card must not clear the issues
        self.m.reset(anchor=(x, y))

    def on_scroll(self, *a):
        self.m.drop_rects()


def watch_focus(model, stop):
    last = None
    while not stop.is_set():
        fid, pid = foreground()
        if fid is not None and pid != os.getpid():
            if last is not None and fid != last:
                model.mods.clear()      # resync in case a key-release was missed
                model.reset()
            last = fid
        time.sleep(0.3)


# --------------------------------------------------------------------------
# UI (Tk on the main thread)
# --------------------------------------------------------------------------
class UI:
    def __init__(self, root, model):
        self.root, self.model = root, model
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.frame = tk.Frame(self.win, bg=BG, padx=12, pady=10,
                              highlightthickness=2, highlightbackground=GREEN)
        self.frame.pack(fill="both", expand=True)
        self.collapsed = False
        self.user_pos = None
        self.toast_text, self.toast_until = "", 0.0
        self.snap = None
        self.card_rect = None
        self.lines = []
        self._dx = self._dy = 0
        # Map each window once, off-screen, at startup so we can mark it non-activating
        # BEFORE it is ever shown over your document (so it can never steal focus).
        self.win.geometry("+-4000+-4000")
        self.win.deiconify()
        self.win.update()
        win_style(self.win)
        self.win.withdraw()
        self.lines = [self._make_line() for _ in range(MAX_UNDERLINES)]

    def _make_line(self):
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.99)
        except tk.TclError:
            pass
        w.geometry("1x1+-4000+-4000")
        w.update()
        win_style(w, click_through=True)
        w.withdraw()
        return w

    # ----- small widgets
    def chip(self, parent, text, cmd, bg=PANEL, fg=FG):
        lb = tk.Label(parent, text=text, bg=bg, fg=fg, padx=8, pady=3, cursor="hand2",
                      font=("Helvetica", 10, "bold"))
        lb.bind("<Button-1>", lambda e: cmd())
        return lb

    def _drag_bind(self, *widgets):
        for w in widgets:
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Button-3>", self._drag_reset)

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.user_pos = (e.x_root - self._dx, e.y_root - self._dy)
        self.place()

    def _drag_reset(self, e):
        self.user_pos = None
        self.place()

    # ----- placement (always fully on-screen)
    def place(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        ms = get_monitors(self.root)
        if self.user_pos:
            ax, ay = self.user_pos[0] + w // 2, self.user_pos[1] + h // 2
        else:
            anchor = self.snap and self.snap.get("anchor")
            ax, ay = anchor if anchor else self.root.winfo_pointerxy()
        mx, my, mw, mh = monitor_at(ms, ax, ay)
        if self.user_pos:
            x, y = self.user_pos
        else:
            x = mx + mw - w - CARD_MARGIN
            y = my + mh - h - BOTTOM_MARGIN
        x = max(mx + 4, min(x, mx + mw - w - 4))
        y = max(my + 4, min(y, my + mh - h - 4))
        if self.user_pos:
            self.user_pos = (x, y)
        self.win.geometry(f"+{int(x)}+{int(y)}")
        self.card_rect = (int(x), int(y), int(w), int(h))

    def _show(self):
        self.win.deiconify()
        self.win.lift()
        self.place()

    # ----- rendering
    def show_toast(self, text):
        self.toast_text, self.toast_until = text, time.time() + 2.2
        self.render(self.model.snapshot())

    def render(self, snap):
        issues = snap["issues"]
        ms = get_monitors(self.root)
        # anchor: where the user is typing, so the card appears on that monitor
        pts = [i["rect"] for i in issues if i["rect"]]
        snap["anchor"] = (pts[-1][0], pts[-1][1]) if pts else None
        self.snap = snap
        toast_on = time.time() < self.toast_until
        for w in self.frame.winfo_children():
            w.destroy()
        if not issues:
            if toast_on:
                tk.Label(self.frame, text=self.toast_text, bg=BG, fg=FG,
                         font=("Helvetica", 11, "bold")).pack()
                self._show()
            else:
                self.win.withdraw()
                self.card_rect = None
            self.draw_underlines()
            return

        f = self.frame
        n = len(issues)
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x")
        title = tk.Label(hdr, text="✎ Writing Assistant", bg=BG, fg=FG, font=("Helvetica", 11, "bold"))
        title.pack(side="left")
        count = tk.Label(hdr, text=f"  {n} issue{'s' if n != 1 else ''}", bg=BG, fg=RED,
                         font=("Helvetica", 11, "bold"))
        count.pack(side="left")
        self.chip(hdr, "+" if self.collapsed else "–", self.toggle_collapse).pack(side="right")
        self._drag_bind(hdr, title, count)
        if toast_on:
            tk.Label(f, text=self.toast_text, bg=BG, fg=GREEN, font=("Helvetica", 10)).pack(anchor="w")

        if not self.collapsed:
            self._render_body(f, snap)
        self._show()
        self.draw_underlines()

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.snap:
            self.render(self.snap)

    def _render_body(self, f, snap):
        text, issues = snap["text"], snap["issues"]
        sel = next((i for i in issues if i["id"] == snap["selected"]), issues[-1])

        # preview of recent text with underlined issues (works in every app)
        base = max(0, len(text) - PREVIEW_CHARS)
        tail = text[base:].replace("\n", "↵")
        t = tk.Text(f, width=50, height=3, wrap="word", bg=PANEL, fg=FG, bd=0,
                    highlightthickness=0, padx=6, pady=4, font=("Helvetica", 11), cursor="arrow")
        t.insert("1.0", tail)
        for i in issues:
            if i["hs"] < base:
                continue
            tag = f"i{i['id']}"
            a, b = i["hs"] - base, min(i["he"], len(text)) - base
            t.tag_add(tag, f"1.0+{a}c", f"1.0+{b}c")
            t.tag_config(tag, underline=True, foreground=KIND_COLOR.get(i["kind"], RED),
                         background="#374151" if i["id"] == sel["id"] else PANEL)
            t.tag_bind(tag, "<Button-1>", lambda e, iid=i["id"]: self.model.select(iid))
            t.tag_bind(tag, "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.config(cursor="arrow"))
        t.see("end")
        t.config(state="disabled")
        t.pack(fill="x", pady=(8, 8))

        color = KIND_COLOR.get(sel["kind"], RED)
        tk.Label(f, text=sel["label"], bg=BG, fg=color, font=("Helvetica", 10, "bold")).pack(anchor="w")
        tk.Label(f, text=f"“{sel['word'].strip() or '␠'}”", bg=BG, fg=FG,
                 font=("Helvetica", 14, "bold")).pack(anchor="w", pady=(0, 6))

        row = tk.Frame(f, bg=BG)
        row.pack(anchor="w", pady=(0, 8))
        if sel["suggestions"]:
            for idx, (lab, _rep) in enumerate(sel["suggestions"][:3]):
                self.chip(row, f"{idx + 1}  {lab}",
                          lambda idx=idx, iid=sel["id"]: self.model.apply(iid, idx),
                          bg="#065f46").pack(side="left", padx=(0, 6))
        else:
            tk.Label(row, text="No suggestions", bg=BG, fg=MUTED, font=("Helvetica", 10)).pack()

        acts = tk.Frame(f, bg=BG)
        acts.pack(anchor="w")
        self.chip(acts, "Ignore", self.model.ignore).pack(side="left", padx=(0, 6))
        if sel["kind"] == "spelling":
            self.chip(acts, "Add to dictionary", self.model.add_word).pack(side="left", padx=(0, 6))
        if len(issues) > 1:
            self.chip(acts, "Next", self.model.next_issue).pack(side="left", padx=(0, 6))
            self.chip(acts, f"Fix all ({sum(1 for i in issues if i['suggestions'])})",
                      self.model.fix_all, bg="#1e3a8a").pack(side="left")

        others = [i for i in issues if i["id"] != sel["id"]][:MAX_CHIPS]
        if others:
            orow = tk.Frame(f, bg=BG)
            orow.pack(anchor="w", pady=(8, 0))
            tk.Label(orow, text="Also:", bg=BG, fg=MUTED, font=("Helvetica", 9)).pack(side="left")
            for i in others:
                lb = tk.Label(orow, text=i["word"].strip() or "␠", bg=BG,
                              fg=KIND_COLOR.get(i["kind"], RED), cursor="hand2",
                              font=("Helvetica", 10, "underline"), padx=4)
                lb.bind("<Button-1>", lambda e, iid=i["id"]: self.model.select(iid))
                lb.pack(side="left")

        tk.Label(f, text="Ctrl+Alt: 1-3 apply · N next · D ignore · W add word · O open dictionary folder · F fix all · P pause · Q quit",
                 bg=BG, fg=MUTED, font=("Helvetica", 8), wraplength=380, justify="left"
                 ).pack(anchor="w", pady=(8, 0))

    # ----- in-place underlines
    def draw_underlines(self):
        items = []
        snap = self.snap
        if snap and not snap["paused"]:
            ox, oy = window_origin()
            ms = get_monitors(self.root)
            for i in snap["issues"]:
                if not i["rect"]:
                    continue
                x, y, w = i["rect"]
                x += ox - i["origin"][0]
                y += oy - i["origin"][1]
                if any(m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3] for m in ms):
                    items.append((int(x), int(y), int(w), KIND_COLOR.get(i["kind"], RED),
                                  3 if i["id"] == snap["selected"] else 2))
        items = items[:len(self.lines)]
        for w, (x, y, wd, color, th) in zip(self.lines, items):
            w.configure(bg=color)
            w.geometry(f"{wd}x{th}+{x}+{y}")
            w.deiconify()
        for w in self.lines[len(items):]:
            w.withdraw()

    def tick(self):
        self.draw_underlines()
        if self.toast_until and time.time() > self.toast_until:
            self.toast_until = 0.0
            self.render(self.model.snapshot())
        self.root.after(250, self.tick)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    if "--import" in sys.argv:
        paths = sys.argv[sys.argv.index("--import") + 1:]
        paths = [p for p in paths if not p.startswith("--")]
        print("Importing dictionaries...")
        import_dictionaries(paths)
        print("Done. They load automatically next time the assistant runs (or within seconds if it's running).")
        return
    make_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    ui_q = queue.Queue()
    model = Assistant(lambda: ui_q.put(("refresh",)),
                      lambda text: ui_q.put(("toast", text)),
                      CaretLocator())
    ui = UI(root, model)
    keys = Keys(model, ui_q)
    keys.ui = ui

    kb_listener = keyboard.Listener(on_press=keys.on_press, on_release=keys.on_release)
    ms_listener = mouse.Listener(on_click=keys.on_click, on_scroll=keys.on_scroll)
    kb_listener.start()
    ms_listener.start()
    stop = threading.Event()
    threading.Thread(target=watch_focus, args=(model, stop), daemon=True).start()

    def poll():
        refresh, toast = False, None
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg[0] == "refresh":
                    refresh = True
                elif msg[0] == "toast":
                    toast = msg[1]
                elif msg[0] == "quit":
                    stop.set()
                    kb_listener.stop()
                    ms_listener.stop()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        if toast:
            ui.show_toast(toast)
        elif refresh:
            ui.render(model.snapshot())
        root.after(60, poll)

    print(f"Dictionary folder: {DICT_DIR}  (drop word lists here; Ctrl+Alt+O opens it)")
    print("Writing Assistant v2 running.\n"
          "  Ctrl+Alt+1/2/3 apply | N next | D ignore | W add word | F fix all | P pause | Q quit")
    root.after(60, poll)
    root.after(250, ui.tick)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()