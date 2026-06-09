"""
YouVersion Bible Parallel Text Dataset Builder  (English ↔ Local language)
===========================================================================
Scrapes VERSE-level text for each Bible version and writes only PARALLEL pairs
where BOTH the English verse and the local-language verse are present.

OUTPUT LAYOUT
-------------
    {OUTPUT_ROOT}/                        ← "bob_dataset" or whatever you name it
        progress.json
        testament_status.json
        english_cache.csv                 ← shared English verse cache (verse_key, eng)
        {LANG_NAME}_{LANG_CODE}.csv       ← one CSV per language  (verse_key, version_id, eng, local)

CSV naming example:  Asante_Twi_twi.csv,  Ewe_ee.csv,  Ga_gaa.csv …

PARALLELISM
-----------
Parallel at the CHAPTER level across a pool of Chrome browsers (NUM_WORKERS).
Each worker borrows one driver from the queue for a full chapter, then returns it.
Progress is flushed once per finished chapter (resume-safe).

CSV columns in versions file:  version_id, lang_code, lang_name, abbr  (abbr optional)
Requires: selenium, Chrome + chromedriver.
"""

import csv
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
# CONFIG — edit these
# ─────────────────────────────────────────────

VERSIONS_CSV = "youversion_ghana_versions.csv"

# Fixed English reference version  https://www.bible.com/bible/37/GEN.1.1.CEB
ENGLISH_VERSION_NUM = 37
ENGLISH_ABBR        = "CEB"

VERSE_SELECTOR = "p.text-17"   # CSS selector used for BOTH English and local pages

# Number of parallel Chrome workers. Each is ~300–500 MB RAM.
# Keep modest to avoid rate-limiting / IP blocks from bible.com.
NUM_WORKERS = 8

ALL_BOOK_CODES = [
    "GEN","EXO","LEV","NUM","DEU","JOS","JDG","RUT","1SA","2SA",
    "1KI","2KI","1CH","2CH","EZR","NEH","EST","JOB","PSA","PRO",
    "ECC","SNG","ISA","JER","LAM","EZK","DAN","HOS","JOL","AMO",
    "OBA","JON","MIC","NAM","HAB","ZEP","HAG","ZEC","MAL",
    "MAT","MRK","LUK","JHN","ACT","ROM","1CO","2CO","GAL","EPH",
    "PHP","COL","1TH","2TH","1TI","2TI","TIT","PHM","HEB","JAS",
    "1PE","2PE","1JN","2JN","3JN","JUD","REV",
]

BOOK_CHAPTERS = {
    "GEN":50,"EXO":40,"LEV":27,"NUM":36,"DEU":34,"JOS":24,"JDG":21,
    "RUT":4,"1SA":31,"2SA":24,"1KI":22,"2KI":25,"1CH":29,"2CH":36,
    "EZR":10,"NEH":13,"EST":10,"JOB":42,"PSA":150,"PRO":31,"ECC":12,
    "SNG":8,"ISA":66,"JER":52,"LAM":5,"EZK":48,"DAN":12,"HOS":14,
    "JOL":3,"AMO":9,"OBA":1,"JON":4,"MIC":7,"NAM":3,"HAB":3,"ZEP":3,
    "HAG":2,"ZEC":14,"MAL":4,
    "MAT":28,"MRK":16,"LUK":24,"JHN":21,"ACT":28,"ROM":16,"1CO":16,
    "2CO":13,"GAL":6,"EPH":6,"PHP":4,"COL":4,"1TH":5,"2TH":3,"1TI":6,
    "2TI":4,"TIT":3,"PHM":1,"HEB":13,"JAS":5,"1PE":5,"2PE":3,"1JN":5,
    "2JN":1,"3JN":1,"JUD":1,"REV":22,
}

OUTPUT_ROOT           = "./bible_parallel_text_datasets"
PROGRESS_FILE         = os.path.join(OUTPUT_ROOT, "progress.json")
TESTAMENT_STATUS_FILE = os.path.join(OUTPUT_ROOT, "testament_status.json")

# Shared English cache — one CSV in the root, not per-language folders
ENGLISH_CACHE_CSV     = os.path.join(OUTPUT_ROOT, "english_cache.csv")

# CSV columns for language output files
CSV_FIELDNAMES = ["verse_key", "version_id", "eng", "local"]

HEADLESS = True
PAGE_WAIT = 1
RETRY_WAIT = 2
MAX_RETRIES = 2

STOP_AFTER_EMPTY_VERSES = 2
MAX_VERSES_PER_CHAPTER  = 200

EN_MISSING_SUFFIX  = "_missing"   # key suffix stored in memory to mark absent verses
CHAPTER_DONE_SUFFIX = ".__done__"

# ── Locks ─────────────────────────────────────────────────────────────────────
PROG_LOCK    = threading.Lock()
EN_CSV_LOCK  = threading.Lock()   # guards english_cache.csv

# Per-language CSV locks: created on first use
_CSV_LOCKS:      dict[str, threading.Lock] = {}
_CSV_LOCKS_META = threading.Lock()

def get_lang_csv_lock(csv_name: str) -> threading.Lock:
    with _CSV_LOCKS_META:
        if csv_name not in _CSV_LOCKS:
            _CSV_LOCKS[csv_name] = threading.Lock()
        return _CSV_LOCKS[csv_name]


# ─────────────────────────────────────────────
# TEXT CLEANING
# ─────────────────────────────────────────────

def clean_text(text: str) -> str:
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\d+', '', text)
    lines = text.splitlines()
    processed = []
    for line in lines:
        line = line.strip()
        if line:
            if line[-1] not in ['.', '!', '?', ':', ';']:
                line += '.'
            processed.append(line)
    text = ' '.join(processed)
    text = re.sub(r'[\"\'""''\(\)\[\]\{\}]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[,.]{2,}', '.', text)
    text = re.sub(r'([,.!?;:])\.', '.', text)
    if text and not text.endswith('.'):
        text += '.'
    return text


# ─────────────────────────────────────────────
# ENGLISH CACHE  (CSV-backed, shared across all languages)
# ─────────────────────────────────────────────

# In-memory mirror so we don't re-read the CSV on every verse
_en_cache: dict[str, str] = {}        # verse_key → cleaned text  (empty string = missing)
_en_cache_loaded = False
_en_cache_lock   = threading.Lock()

def _load_en_cache_once():
    global _en_cache_loaded
    with _en_cache_lock:
        if _en_cache_loaded:
            return
        if os.path.exists(ENGLISH_CACHE_CSV):
            with open(ENGLISH_CACHE_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    _en_cache[row["verse_key"]] = row.get("eng", "")
        _en_cache_loaded = True

def _append_en_cache_row(verse_key: str, eng: str):
    """Append one row to english_cache.csv and update in-memory dict."""
    with EN_CSV_LOCK:
        os.makedirs(OUTPUT_ROOT, exist_ok=True)
        write_header = not os.path.exists(ENGLISH_CACHE_CSV)
        with open(ENGLISH_CACHE_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["verse_key", "eng"])
            if write_header:
                writer.writeheader()
            writer.writerow({"verse_key": verse_key, "eng": eng})
        _en_cache[verse_key] = eng


# ─────────────────────────────────────────────
# VERSE SCRAPING
# ─────────────────────────────────────────────

def get_verse_text(driver, wait, version_num: int, book: str, chapter: int,
                   verse: int, abbr: str | None = None) -> str | None:
    suffix = f".{abbr}" if abbr else ""
    url = f"https://www.bible.com/bible/{version_num}/{book}.{chapter}.{verse}{suffix}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            time.sleep(PAGE_WAIT)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, VERSE_SELECTOR)))
            paras = driver.find_elements(By.CSS_SELECTOR, VERSE_SELECTOR)
            texts = [p.text.strip() for p in paras if p.text.strip()]
            if texts:
                return "\n".join(texts)
            return None
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)
            else:
                return None
    return None


def get_english_verse(driver, wait, book: str, chapter: int, verse: int) -> str | None:
    """Return cleaned English text for a verse, using CSV cache. Scrapes on first miss."""
    _load_en_cache_once()
    key = f"{book}.{chapter}.{verse}"

    with _en_cache_lock:
        if key in _en_cache:
            return _en_cache[key] or None  # empty string → was missing

    # Not in cache yet — scrape
    raw     = get_verse_text(driver, wait, ENGLISH_VERSION_NUM, book, chapter, verse, ENGLISH_ABBR)
    cleaned = clean_text(raw) if raw and raw.strip() else ""
    _append_en_cache_row(key, cleaned)
    return cleaned or None


# ─────────────────────────────────────────────
# PROGRESS
# ─────────────────────────────────────────────

def load_global_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    return {}

def save_global_progress_locked(progress: dict):
    """Caller must hold PROG_LOCK."""
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    out = {str(k): v for k, v in progress.items()}
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)

def mark_verse_done(version_num, key, progress_dict, done_set):
    with PROG_LOCK:
        done_set.add(key)
        progress_dict[version_num] = list(done_set)

def is_done(key, done_set) -> bool:
    with PROG_LOCK:
        return key in done_set

def is_chapter_done(book, chapter, done_set) -> bool:
    with PROG_LOCK:
        return f"{book}.{chapter}{CHAPTER_DONE_SUFFIX}" in done_set

def mark_chapter_done(version_num, book, chapter, progress_dict, done_set):
    with PROG_LOCK:
        done_set.add(f"{book}.{chapter}{CHAPTER_DONE_SUFFIX}")
        progress_dict[version_num] = list(done_set)

def flush_progress(progress_dict):
    with PROG_LOCK:
        save_global_progress_locked(progress_dict)


# ─────────────────────────────────────────────
# TESTAMENT STATUS
# ─────────────────────────────────────────────

def load_testament_status() -> dict:
    if os.path.exists(TESTAMENT_STATUS_FILE):
        with open(TESTAMENT_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    return {}

def save_testament_status(status: dict):
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    out = {str(k): v for k, v in status.items()}
    with open(TESTAMENT_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


# ─────────────────────────────────────────────
# CSV NAME HELPER
# ─────────────────────────────────────────────

def lang_csv_name(lang_name: str, lang_code: str) -> str:
    """e.g. 'Asante Twi' + 'twi' → 'Asante_Twi_twi.csv'"""
    return f"{lang_name}_{lang_code}".replace(" ", "_").replace("/", "-") + ".csv"

def lang_csv_path(lang_name: str, lang_code: str) -> str:
    return os.path.join(OUTPUT_ROOT, lang_csv_name(lang_name, lang_code))


# ─────────────────────────────────────────────
# SAVE A PARALLEL PAIR  →  language CSV
# ─────────────────────────────────────────────

def save_parallel_pair(key: str, version_num: int, en_text: str,
                       local_text: str, csv_path: str) -> bool:
    lock = get_lang_csv_lock(csv_path)
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    with lock:
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "verse_key":  key,
                "version_id": version_num,
                "eng":        en_text,
                "local":      local_text,
            })
    return True


# ─────────────────────────────────────────────
# VERSE HANDLER
# ─────────────────────────────────────────────

def try_save_verse(book, chapter, verse, version_num, driver, wait, abbr,
                   csv_path, progress_dict, done_set, stats) -> str:
    """Returns 'pair' | 'missing' | 'empty'."""
    key = f"{book}.{chapter}.{verse}"

    raw_local  = get_verse_text(driver, wait, version_num, book, chapter, verse, abbr)
    local_text = clean_text(raw_local) if raw_local else ""
    if not local_text:
        return "empty"

    en_text = get_english_verse(driver, wait, book, chapter, verse)
    if not en_text:
        mark_verse_done(version_num, key, progress_dict, done_set)
        stats["missing"] += 1
        return "missing"

    save_parallel_pair(key, version_num, en_text, local_text, csv_path)
    mark_verse_done(version_num, key, progress_dict, done_set)
    stats["parallel"] += 1
    print(f"    ✅ {key}")
    return "pair"


# ─────────────────────────────────────────────
# CHAPTER WORKER
# ─────────────────────────────────────────────

def process_chapter(book, chapter, version_num, abbr, csv_path,
                    progress_dict, done_set, driver_queue):
    stats = {"parallel": 0, "skipped": 0, "missing": 0}
    driver, wait     = driver_queue.get()
    chapter_finished = False
    try:
        consecutive_empty = 0
        for verse in range(1, MAX_VERSES_PER_CHAPTER + 1):
            key = f"{book}.{chapter}.{verse}"
            if is_done(key, done_set):
                stats["skipped"] += 1
                consecutive_empty = 0
                continue
            result = try_save_verse(book, chapter, verse, version_num, driver,
                                    wait, abbr, csv_path, progress_dict, done_set, stats)
            if result == "empty":
                consecutive_empty += 1
                if consecutive_empty >= STOP_AFTER_EMPTY_VERSES:
                    chapter_finished = True
                    break
            else:
                consecutive_empty = 0
    finally:
        driver_queue.put((driver, wait))

    if chapter_finished:
        mark_chapter_done(version_num, book, chapter, progress_dict, done_set)
    flush_progress(progress_dict)
    return stats


# ─────────────────────────────────────────────
# PROBE TESTAMENT
# ─────────────────────────────────────────────

def probe_testament(label, probe_books, version_num, progress_dict, done_set,
                    stats, driver, wait, csv_path, abbr) -> bool:
    book      = probe_books[0]
    confirmed = 0
    for verse in (1, 2):
        key = f"{book}.1.{verse}"
        if is_done(key, done_set):
            if os.path.exists(csv_path):
                print(f"  ⏭️  [{label} probe] {key} already done.")
                confirmed += 1
            continue
        print(f"  🔍 [{label} probe] {key}")
        result = try_save_verse(book, 1, verse, version_num, driver, wait, abbr,
                                csv_path, progress_dict, done_set, stats)
        if result == "pair":
            confirmed += 1
        elif result == "empty":
            mark_verse_done(version_num, key, progress_dict, done_set)
            stats["missing"] += 1
    return confirmed > 0


# ─────────────────────────────────────────────
# MAIN PER-VERSION PROCESSING
# ─────────────────────────────────────────────

def build_dataset_for_bible(version_num, lang_code, lang_name, abbr,
                            driver_queue, progress_dict, testament_status):
    print(f"\n{'='*60}")
    print(f"  Processing: {lang_name} ({lang_code}) — version {version_num}"
          f"{' / ' + abbr if abbr else ''}")
    print(f"{'='*60}")

    csv_path = lang_csv_path(lang_name, lang_code)
    done_set = set(progress_dict.get(version_num, []))
    stats    = {"parallel": 0, "skipped": 0, "missing": 0}

    OT_BOOKS = ALL_BOOK_CODES[:39]
    NT_BOOKS = ALL_BOOK_CODES[39:]
    cached   = testament_status.get(version_num)

    # ── Probe phase ──────────────────────────────────────────────────────────
    probe_driver, probe_wait = driver_queue.get()
    try:
        if cached and "ot" in cached:
            ot_ok = cached["ot"]
            print(f"\n  📜 OT probe cached ({'✅' if ot_ok else '🛑'}).")
        else:
            print(f"\n  📜 Probing OT (GEN 1:1–1:2)...")
            ot_ok = probe_testament("OT", OT_BOOKS, version_num, progress_dict,
                                    done_set, stats, probe_driver, probe_wait,
                                    csv_path, abbr)
            testament_status.setdefault(version_num, {})["ot"] = ot_ok
            save_testament_status(testament_status)

        if cached and "nt" in cached:
            nt_ok = cached["nt"]
            print(f"  ✝️  NT probe cached ({'✅' if nt_ok else '🛑'}).")
        else:
            print(f"  ✝️  Probing NT (MAT 1:1–1:2)...")
            nt_ok = probe_testament("NT", NT_BOOKS, version_num, progress_dict,
                                    done_set, stats, probe_driver, probe_wait,
                                    csv_path, abbr)
            testament_status.setdefault(version_num, {})["nt"] = nt_ok
            save_testament_status(testament_status)
    finally:
        driver_queue.put((probe_driver, probe_wait))

    flush_progress(progress_dict)
    print(f"  OT: {'process' if ot_ok else 'skip'} | NT: {'process' if nt_ok else 'skip'}")

    if not ot_ok and not nt_ok:
        print(f"  ⛔ No content found — skipping {lang_name} ({lang_code}).")
        return stats

    # ── Chapter task list ─────────────────────────────────────────────────────
    tasks = []
    skipped_chapters = 0
    for book in ALL_BOOK_CODES:
        in_ot = book in OT_BOOKS
        if (in_ot and not ot_ok) or (not in_ot and not nt_ok):
            continue
        for chapter in range(1, BOOK_CHAPTERS.get(book, 0) + 1):
            if is_chapter_done(book, chapter, done_set):
                skipped_chapters += 1
            else:
                tasks.append((book, chapter))

    if skipped_chapters:
        print(f"  ⏭️  Skipped {skipped_chapters} already-completed chapters")

    # ── Parallel chapter processing ───────────────────────────────────────────
    workers = max(1, min(NUM_WORKERS, driver_queue.qsize()))
    print(f"  🚀 Processing {len(tasks)} chapters across {workers} workers...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_chapter, book, chapter, version_num, abbr,
                        csv_path, progress_dict, done_set, driver_queue):
                (book, chapter)
            for book, chapter in tasks
        }
        for fut in as_completed(futures):
            book, chapter = futures[fut]
            try:
                cs = fut.result()
                stats["parallel"] += cs["parallel"]
                stats["skipped"]  += cs["skipped"]
                stats["missing"]  += cs["missing"]
                print(f"  📘 {book}.{chapter} done "
                      f"(+{cs['parallel']} pairs, {cs['missing']} missing)")
            except Exception as e:
                print(f"  ❌ {book}.{chapter} failed: {e}")

    flush_progress(progress_dict)
    print(f"\n  📊 {lang_name} ({lang_code}) v{version_num} Summary:")
    print(f"     ✅ Parallel pairs saved  : {stats['parallel']}")
    print(f"     ⏭️  Already done          : {stats['skipped']}")
    print(f"     ⛔ Missing on one side   : {stats['missing']}")
    print(f"     📄 Output CSV            : {csv_path}")
    return stats


# ─────────────────────────────────────────────
# DRIVER POOL & VERSIONS CSV
# ─────────────────────────────────────────────

def make_driver(driver_path):
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    for binary in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
        if os.path.exists(binary):
            options.binary_location = binary
            break
    return webdriver.Chrome(service=Service(driver_path), options=options)

def build_driver_pool(n) -> Queue:
    driver_path = ChromeDriverManager().install()
    q = Queue()
    for i in range(n):
        d = make_driver(driver_path)
        q.put((d, WebDriverWait(d, 15)))
        print(f"  🧩 Browser {i+1}/{n} ready")
    return q

def load_versions_csv(csv_path: str) -> list:
    entries = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vid = row["version_id"].strip()
            if not vid.isdigit():
                continue
            # Skip rows explicitly marked not viable by the scanner
            if row.get("viable", "").strip().lower() == "false":
                continue
            abbr = row.get("abbr", "").strip() or None
            entries.append((int(vid), row["lang_code"].strip(),
                            row["lang_name"].strip(), abbr))
    return entries


# ─────────────────────────────────────────────
# INTERACTIVE LANGUAGE SELECTION
# ─────────────────────────────────────────────

def prompt_language_selection(entries: list) -> list:
    """
    Display a numbered list of available languages and prompt the user
    to select one or more. Returns the chosen subset of entries.
    Supports single picks, comma-separated picks, and ranges (e.g. 1-3).
    """
    print("\n" + "=" * 60)
    print("  Available Languages")
    print("=" * 60)
    print(f"  {'#':<5} {'Language':<30} {'Code':<10} {'Version ID'}")
    print(f"  {'-'*5} {'-'*30} {'-'*10} {'-'*10}")
    for i, (version_num, lang_code, lang_name, abbr) in enumerate(entries, start=1):
        abbr_str = f" ({abbr})" if abbr else ""
        print(f"  {i:<5} {lang_name + abbr_str:<30} {lang_code:<10} {version_num}")
    print("=" * 60)
    print("\n  Enter number(s) to select — examples:")
    print("    3          → pick language #3")
    print("    1,4,7      → pick languages #1, #4, and #7")
    print("    2-5        → pick languages #2 through #5")
    print("    1,3-5,8    → mixed selection")
    print()

    while True:
        raw = input("  Your selection: ").strip()
        if not raw:
            print("  ⚠️  No input provided. Please enter at least one number.\n")
            continue

        selected_indices = set()
        valid = True
        parts = [p.strip() for p in raw.split(",")]
        for part in parts:
            if not part:
                continue
            if "-" in part:
                # Range like "2-5"
                bounds = part.split("-", 1)
                try:
                    lo, hi = int(bounds[0].strip()), int(bounds[1].strip())
                    if lo < 1 or hi > len(entries) or lo > hi:
                        print(f"  ⚠️  Range '{part}' is out of bounds "
                              f"(valid: 1–{len(entries)}).\n")
                        valid = False
                        break
                    selected_indices.update(range(lo, hi + 1))
                except ValueError:
                    print(f"  ⚠️  Could not parse range '{part}'.\n")
                    valid = False
                    break
            else:
                try:
                    n = int(part)
                    if n < 1 or n > len(entries):
                        print(f"  ⚠️  Number {n} is out of range "
                              f"(valid: 1–{len(entries)}).\n")
                        valid = False
                        break
                    selected_indices.add(n)
                except ValueError:
                    print(f"  ⚠️  '{part}' is not a valid number.\n")
                    valid = False
                    break

        if not valid or not selected_indices:
            if valid:
                print("  ⚠️  No valid selections found. Please try again.\n")
            continue

        # Show confirmation summary
        chosen = [entries[i - 1] for i in sorted(selected_indices)]
        print("\n  You selected:")
        for version_num, lang_code, lang_name, abbr in chosen:
            abbr_str = f" ({abbr})" if abbr else ""
            print(f"    • {lang_name}{abbr_str}  [{lang_code}]  version {version_num}")
        print()

        confirm = input("  Confirm? [y/n]: ").strip().lower()
        if confirm in ("y", "yes"):
            return chosen
        else:
            print("\n  Selection cancelled — please choose again.\n")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    # ── Load and display available languages ─────────────────────────────────
    all_entries = load_versions_csv(VERSIONS_CSV)
    if not all_entries:
        print("❌ No viable versions found in CSV. Exiting.")
        return

    print(f"📋 Loaded {len(all_entries)} viable language version(s) from {VERSIONS_CSV}")

    # ── Interactive selection ─────────────────────────────────────────────────
    selected_entries = prompt_language_selection(all_entries)
    if not selected_entries:
        print("❌ No languages selected. Exiting.")
        return

    print(f"\n🧰 Spinning up {NUM_WORKERS} browsers...")
    driver_queue = build_driver_pool(NUM_WORKERS)

    progress         = load_global_progress()
    testament_status = load_testament_status()
    grand_total      = 0

    try:
        for version_num, lang_code, lang_name, abbr in selected_entries:
            stats = build_dataset_for_bible(
                version_num, lang_code, lang_name, abbr,
                driver_queue, progress, testament_status,
            )
            grand_total += stats["parallel"]
    finally:
        while not driver_queue.empty():
            d, _ = driver_queue.get()
            try:
                d.quit()
            except Exception:
                pass

    print(f"\n🎉 All done!  Total parallel pairs across all selected versions: {grand_total}")
    print(f"   Output root      : {os.path.abspath(OUTPUT_ROOT)}")
    print(f"   English cache    : {os.path.abspath(ENGLISH_CACHE_CSV)}")
    print(f"   Progress file    : {os.path.abspath(PROGRESS_FILE)}")


if __name__ == "__main__":
    main()
