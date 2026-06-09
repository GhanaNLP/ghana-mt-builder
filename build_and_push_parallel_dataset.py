"""
Build Twi-English parallel text dataset from Bible scraper output (flat CSV mode),
merge with existing CSV, and push to HuggingFace.

Steps:
  1. Read {OUTPUT_ROOT}/{LANG_NAME}_{LANG_CODE}.csv  (or scan all *.csv files)
  2. Load existing parallel_sentences.csv
  3. Merge, deduplicate on (eng, local), aggregate count + source_files
  4. Push to HuggingFace
  5. Upload README / dataset card

Flat CSV layout (produced by scraper v3):
    bible_parallel_text_datasets/
        english_cache.csv              ← shared, not used here
        Asante_Twi_twi.csv             ← columns: verse_key, version_id, eng, local
        Ewe_ee.csv
        …
"""

import os
from pathlib import Path

import pandas as pd
from datasets import Dataset, DatasetDict
from huggingface_hub import HfApi, login

# ── Config ────────────────────────────────────────────────────────────────────

BIBLE_ROOT   = Path("./bible_parallel_text_datasets")
EXISTING_CSV = Path("/home/owusus/Documents/GitHub/archives2data/data/twi/parallel_sentences.csv")
OUTPUT_CSV   = Path("./twi_english_parallel_merged.csv")

# Target language CSV name (without .csv). Set to None to merge ALL language CSVs.
# e.g. LANG_CSV = "Asante_Twi_twi"
LANG_CSV = "Asante_Twi_twi"

# Files in BIBLE_ROOT to skip when scanning all CSVs
SKIP_CSVS = {"english_cache.csv"}

HF_TOKEN   = "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"
HF_REPO_ID = "ghananlpcommunity/twi-english-parallel-text"

README_TEXT = """\
---
language:
  - tw
  - en
license: cc-by-4.0
task_categories:
  - translation
task_ids:
  - machine-translation
multilinguality:
  - translation
size_categories:
  - 10K<n<100K
source_datasets:
  - original
tags:
  - twi
  - asante-twi
  - akan
  - ghana
  - bible
  - parallel-corpus
  - low-resource
pretty_name: Twi-English Parallel Text
dataset_info:
  features:
    - name: twi
      dtype: string
    - name: eng
      dtype: string
    - name: count
      dtype: int64
    - name: source_files
      dtype: string
  splits:
    - name: train
---

# Twi–English Parallel Text Dataset

A parallel corpus of **Asante Twi** and **English** sentence pairs compiled
by [Ghana NLP Community](https://huggingface.co/ghananlpcommunity).

## Sources

| Source | Description |
|--------|-------------|
| `youversion_bible` | Verse-level parallel pairs scraped from YouVersion Bible aligned with the CEB English Bible |
| Dictionary / archive sources | Sentence pairs from historical dictionaries and digitised archives (see `source_files` column) |

## Columns

| Column | Type | Description |
|--------|------|-------------|
| `twi` | string | Asante Twi sentence |
| `eng` | string | Corresponding English sentence |
| `count` | int | Number of independent sources that confirmed this pair |
| `source_files` | string | Semicolon-separated list of source identifiers |

## Usage

```python
from datasets import load_dataset

ds = load_dataset("ghananlpcommunity/twi-english-parallel-text")
print(ds["train"][0])
```

## License

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
"""

# ── Step 1: Collect Bible pairs from flat CSVs ────────────────────────────────

def collect_bible_pairs(bible_root: Path, lang_csv: str | None) -> pd.DataFrame:
    """
    Read one or all language CSVs from the flat bible_root directory.
    Input columns  : verse_key, version_id, eng, local
    Output columns : twi, eng, count, source_files
    """
    if lang_csv:
        csv_files = [bible_root / f"{lang_csv}.csv"]
    else:
        csv_files = [
            p for p in sorted(bible_root.glob("*.csv"))
            if p.name not in SKIP_CSVS
        ]

    found = [p for p in csv_files if p.exists()]
    if not found:
        print(f"⚠️  No language CSVs found in {bible_root}")
        return pd.DataFrame(columns=["twi", "eng", "count", "source_files"])

    frames = []
    for p in found:
        df = pd.read_csv(p, dtype={"version_id": str})
        print(f"  📄 {p.name}  →  {len(df):,} rows")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)

    # Normalise: rename 'local' → 'twi', deduplicate within Bible data
    combined = combined.rename(columns={"local": "twi"})
    combined["_twi_norm"] = combined["twi"].str.strip().str.lower()
    combined["_eng_norm"] = combined["eng"].str.strip().str.lower()

    def agg_group(g):
        srcs = sorted({f"youversion_bible_v{vid}" for vid in g["version_id"]})
        return pd.Series({
            "twi":          g["twi"].iloc[0],
            "eng":          g["eng"].iloc[0],
            "count":        len(srcs),
            "source_files": "; ".join(srcs),
        })

    result = (
        combined
        .groupby(["_twi_norm", "_eng_norm"], sort=False)
        .apply(agg_group)
        .reset_index(drop=True)
    )

    print(f"✅ Bible pairs collected: {len(result):,}")
    return result


# ── Step 2: Load existing CSV ─────────────────────────────────────────────────

def load_existing_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"count": "Int64"})
    df = df[["eng", "twi", "count", "source_files"]].copy()
    df = df[["twi", "eng", "count", "source_files"]]
    df["count"]        = df["count"].fillna(1).astype(int)
    df["source_files"] = df["source_files"].fillna("").astype(str)
    print(f"✅ Existing CSV loaded: {len(df):,} rows")
    return df


# ── Step 3: Merge & deduplicate ───────────────────────────────────────────────

def merge_datasets(bible_df: pd.DataFrame, existing_df: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([existing_df, bible_df], ignore_index=True)
    combined["_twi_norm"] = combined["twi"].str.strip().str.lower()
    combined["_eng_norm"] = combined["eng"].str.strip().str.lower()

    def merge_group(g):
        total = int(g["count"].sum())
        srcs  = set()
        for s in g["source_files"]:
            for part in str(s).split(";"):
                part = part.strip()
                if part:
                    srcs.add(part)
        return pd.Series({
            "twi":          g["twi"].iloc[0],
            "eng":          g["eng"].iloc[0],
            "count":        total,
            "source_files": "; ".join(sorted(srcs)),
        })

    merged = (
        combined
        .groupby(["_twi_norm", "_eng_norm"], sort=False)
        .apply(merge_group)
        .reset_index(drop=True)
    )

    merged = merged.sort_values("count", ascending=False).reset_index(drop=True)
    print(f"✅ Merged dataset: {len(merged):,} unique pairs")
    return merged


# ── Step 4: Push to HuggingFace ──────────────────────────────────────────────

def push_to_hf(df: pd.DataFrame, repo_id: str, token: str):
    print(f"\n🚀 Logging in to HuggingFace...")
    login(token=token)
    api = HfApi()

    print(f"📦 Creating/ensuring repo: {repo_id}")
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, private=False)

    print("📝 Uploading README.md...")
    api.upload_file(
        path_or_fileobj=README_TEXT.encode("utf-8"),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Add dataset card",
    )

    print(f"⬆️  Pushing {len(df):,} rows...")
    ds_dict = DatasetDict({"train": Dataset.from_pandas(df.reset_index(drop=True))})
    ds_dict.push_to_hub(repo_id, token=token,
                        commit_message="Add Twi-English parallel text dataset")

    print(f"\n🎉 Dataset available at: https://huggingface.co/datasets/{repo_id}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Twi-English Parallel Text Dataset Builder  (flat CSV mode)")
    print("=" * 60)

    bible_df    = collect_bible_pairs(BIBLE_ROOT, LANG_CSV)
    existing_df = load_existing_csv(EXISTING_CSV)
    merged_df   = merge_datasets(bible_df, existing_df)

    merged_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n💾 Merged CSV saved to: {OUTPUT_CSV}")
    print(f"   Rows    : {len(merged_df):,}")
    print(f"   Columns : {list(merged_df.columns)}")

    push_to_hf(merged_df, HF_REPO_ID, HF_TOKEN)


if __name__ == "__main__":
    main()
