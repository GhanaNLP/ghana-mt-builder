"""
scan_viable_versions.py
=======================
Pre-scrape viability scanner.  For every version in your versions CSV, probes
a small set of Bible verses to decide whether that version actually has content
on YouVersion.  Results are written back to the CSV with a 'viable' column and
a 'viable_detail' column so you know *why* a version was rejected.

The main scraper (youversion_parallel_text_builder.py) already reads the
'viable' column and skips rows marked False — so once you've run this scan
you never waste time discovering dead languages mid-run.

HOW IT WORKS
------------
For each version the scanner checks:
  • GEN 1:1 and GEN 1:2   (Old Testament probe)
  • MAT 1:1 and MAT 1:2   (New Testament probe)

A version is considered viable if at least ONE of those verses returns text.
Results:
  viable = True   → at least OT or NT confirmed  (scraper will use it)
  viable = False  → no content found              (scraper will skip it)
  viable = ot     → only OT confirmed
  viable = nt     → only NT confirmed
  viable = both   → both OT and NT confirmed

The 'viable' column values 'ot', 'nt', and 'both' all count as True for the
scraper — only the string 'false' (case-insensitive) causes a skip.

USAGE
-----
    python scan_viable_versions.py

Set VERSIONS_CSV, OUTPUT_CSV, NUM_WORKERS, and RESCAN_ALL below.
If OUTPUT_CSV == VERSIONS_CSV the file is updated in-place.
If RESCAN_ALL is False, already-scanned rows (viable != '') are skipped.

OUTPUT CSV
----------
Same columns as input, plus:
  viable        : 'both' | 'ot' | 'nt' | 'false'
  viable_detail : human-readable note, e.g. "OT✅ NT✅" or "No content found"
"""

import csv
import os
import re
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ── Config ────────────────────────────────────────────────────────────────────

VERSIONS_CSV = "youversion_ghana_versions.csv"
OUTPUT_CSV   = "youversion_ghana_versions.csv"   # overwrite in-place (or set a new path)

NUM_WORKERS  = 6     # browsers to spin up for parallel scanning
HEADLESS     = True

# Set True to re-probe every row even if viable column is already filled.
RESCAN_ALL = False

# Probing both OT and NT probe points per version
OT_PROBE_VERSES = [("GEN", 1, 1), ("GEN", 1, 2)]
NT_PROBE_VERSES = [("MAT", 1, 1), ("MAT", 1, 2)]

VERSE_SELECTOR = "p.text-17"
PAGE_WAIT   = 1
RETRY_WAIT  = 2
MAX_RETRIES = 2

# ─────────────────────────────────────────────────────────────────────────────

PRINT_LOCK = threading.Lock()

def log(msg: str):
    with PRINT_LOCK:
        print(msg)


def make_driver(driver_path: str):
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


def build_driver_pool(n: int) -> Queue:
    driver_path = ChromeDriverManager().install()
    q = Queue()
    for i in range(n):
        d = make_driver(driver_path)
        q.put((d, WebDriverWait(d, 15)))
        print(f"  🧩 Browser {i+1}/{n} ready")
    return q


def probe_verse(driver, wait, version_num: int, book: str, chapter: int,
                verse: int, abbr: str | None) -> bool:
    """Return True if the verse URL returns non-empty text."""
    suffix = f".{abbr}" if abbr else ""
    url = f"https://www.bible.com/bible/{version_num}/{book}.{chapter}.{verse}{suffix}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            time.sleep(PAGE_WAIT)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, VERSE_SELECTOR)))
            paras = driver.find_elements(By.CSS_SELECTOR, VERSE_SELECTOR)
            texts = [p.text.strip() for p in paras if p.text.strip()]
            return bool(texts)
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)
    return False


def scan_version(row: dict, driver_queue: Queue) -> dict:
    """Probe one version and return the updated row dict."""
    version_num = int(row["version_id"])
    lang_name   = row["lang_name"]
    lang_code   = row["lang_code"]
    abbr        = row.get("abbr", "").strip() or None
    label       = f"{lang_name} ({lang_code}) v{version_num}"

    driver, wait = driver_queue.get()
    try:
        # OT probe — stop as soon as one verse confirms
        ot_ok = False
        for book, ch, v in OT_PROBE_VERSES:
            if probe_verse(driver, wait, version_num, book, ch, v, abbr):
                ot_ok = True
                break

        # NT probe
        nt_ok = False
        for book, ch, v in NT_PROBE_VERSES:
            if probe_verse(driver, wait, version_num, book, ch, v, abbr):
                nt_ok = True
                break
    finally:
        driver_queue.put((driver, wait))

    if ot_ok and nt_ok:
        viable        = "both"
        viable_detail = "OT✅ NT✅"
        icon = "✅"
    elif ot_ok:
        viable        = "ot"
        viable_detail = "OT✅ NT❌"
        icon = "⚠️ "
    elif nt_ok:
        viable        = "nt"
        viable_detail = "OT❌ NT✅"
        icon = "⚠️ "
    else:
        viable        = "false"
        viable_detail = "No content found"
        icon = "❌"

    log(f"  {icon}  {label:50s}  {viable_detail}")

    updated = dict(row)
    updated["viable"]        = viable
    updated["viable_detail"] = viable_detail
    return updated


def load_versions(path: str) -> tuple[list[dict], list[str]]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            rows.append(dict(row))

    # Ensure viable columns exist
    if "viable" not in fieldnames:
        fieldnames.append("viable")
    if "viable_detail" not in fieldnames:
        fieldnames.append("viable_detail")

    return rows, fieldnames


def save_versions(path: str, rows: list[dict], fieldnames: list[str]):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def main():
    print("=" * 60)
    print("  YouVersion Viability Scanner")
    print("=" * 60)

    rows, fieldnames = load_versions(VERSIONS_CSV)

    # Determine which rows need scanning
    to_scan = []
    already_done = []
    for row in rows:
        vid = row.get("version_id", "").strip()
        if not vid.isdigit():
            already_done.append(row)
            continue
        existing_viable = row.get("viable", "").strip()
        if not RESCAN_ALL and existing_viable:
            already_done.append(row)
        else:
            to_scan.append(row)

    print(f"\n📋 Versions to scan    : {len(to_scan)}")
    print(f"   Already scanned     : {len(already_done)}")
    print(f"   Total in CSV        : {len(rows)}")

    if not to_scan:
        print("\n✅ All versions already scanned. Use RESCAN_ALL=True to re-probe.")
        return

    print(f"\n🧰 Spinning up {NUM_WORKERS} browsers...")
    driver_queue = build_driver_pool(NUM_WORKERS)

    # Scan concurrently
    results: dict[str, dict] = {}   # version_id → updated row
    print(f"\n🔍 Scanning {len(to_scan)} version(s)...\n")

    try:
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futures = {
                pool.submit(scan_version, row, driver_queue): row["version_id"]
                for row in to_scan
            }
            for fut in as_completed(futures):
                vid = futures[fut]
                try:
                    updated = fut.result()
                    results[vid] = updated
                except Exception as e:
                    log(f"  ❌ version {vid} scan failed: {e}")
                    # Keep original row but mark as unknown
                    orig = next(r for r in to_scan if r["version_id"] == vid)
                    orig["viable"]        = "error"
                    orig["viable_detail"] = str(e)
                    results[vid] = orig
    finally:
        while not driver_queue.empty():
            d, _ = driver_queue.get()
            try:
                d.quit()
            except Exception:
                pass

    # Merge results back preserving original row order
    scanned_map = {r["version_id"]: r for r in to_scan}
    final_rows = []
    for row in rows:
        vid = row.get("version_id", "").strip()
        if vid in results:
            final_rows.append(results[vid])
        else:
            final_rows.append(row)

    save_versions(OUTPUT_CSV, final_rows, fieldnames)

    # ── Summary ───────────────────────────────────────────────────────────────
    scanned = [r for r in final_rows if r.get("version_id", "").isdigit()
               and r.get("viable", "")]
    viable_both  = [r for r in scanned if r.get("viable") == "both"]
    viable_ot    = [r for r in scanned if r.get("viable") == "ot"]
    viable_nt    = [r for r in scanned if r.get("viable") == "nt"]
    not_viable   = [r for r in scanned if r.get("viable") == "false"]

    print(f"\n{'='*60}")
    print(f"  Scan complete!  Results written to: {OUTPUT_CSV}")
    print(f"{'='*60}")
    print(f"  ✅ Both OT + NT : {len(viable_both)}")
    print(f"  ⚠️  OT only      : {len(viable_ot)}")
    print(f"  ⚠️  NT only      : {len(viable_nt)}")
    print(f"  ❌ No content   : {len(not_viable)}")
    print(f"\n  Not-viable versions:")
    for r in not_viable:
        print(f"     {r['version_id']:>6}  {r['lang_name']} ({r['lang_code']})")

    print(f"\n💡 Tip: The main scraper reads the 'viable' column and skips rows")
    print(f"        where viable == 'false'. Your list is now pre-cleaned.")


if __name__ == "__main__":
    main()
