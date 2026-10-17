#!/usr/bin/env python3
"""
Parse tier1_companies.md and check for duplicate company entries.
Detects exact duplicates and fuzzy/near-duplicates.
"""

import re
import sys
from collections import defaultdict


def normalize(name: str) -> str:
    """Normalize a company name for fuzzy comparison."""
    s = name.lower().strip()
    # strip common suffixes / noise words
    for word in [
        "inc.", "inc", "ltd.", "ltd", "llc", "plc", "corp.", "corp",
        "corporation", "group", "holdings", "holding", "co.", "co",
        "& company", "& co", "the", "sa", "ag", "se", "nv",
    ]:
        s = re.sub(rf"\b{re.escape(word)}\b", "", s)
    # collapse parenthetical aliases  e.g. "Block (Square/Cash App)" -> "block square cash app"
    s = s.replace("(", " ").replace(")", " ").replace("/", " ")
    # strip all non-alphanumeric (keep spaces)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_companies(filepath: str) -> list[tuple[str, str]]:
    """
    Return a list of (company_name, section_header) tuples
    parsed from markdown tables in the file.
    """
    entries = []
    current_section = "Unknown"

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Track whether we've seen the header separator row (|---|---|)
    in_table = False

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if stripped.startswith("## ") or stripped.startswith("### "):
            current_section = stripped.lstrip("#").strip()
            in_table = False
            continue

        # Skip the SUMMARY table
        if current_section == "SUMMARY":
            continue

        # Detect table rows
        if stripped.startswith("|") and stripped.endswith("|"):
            # Skip separator rows  |---|---|
            if re.match(r"^\|[\s\-|]+\|$", stripped):
                in_table = True
                continue
            # Skip header rows (the row right before the separator)
            if not in_table:
                continue

            # Parse the first column as company name
            cols = [c.strip() for c in stripped.split("|")]
            # split gives ['', col1, col2, ..., '']
            cols = [c for c in cols if c]
            if cols:
                company = cols[0].strip()
                if company and company.lower() != "company":
                    entries.append((company, current_section))
        else:
            # Non-table line resets table state
            if stripped == "":
                in_table = False

    return entries


def find_exact_duplicates(entries: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Find companies listed with the exact same name."""
    seen = defaultdict(list)
    for name, section in entries:
        seen[name].append(section)
    return {name: sections for name, sections in seen.items() if len(sections) > 1}


def find_fuzzy_duplicates(
    entries: list[tuple[str, str]],
) -> list[tuple[str, str, str, str]]:
    """
    Find near-duplicate pairs using normalized names.
    Returns list of (name1, section1, name2, section2).
    """
    # Group by normalized form
    norm_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, section in entries:
        n = normalize(name)
        if n:
            norm_groups[n].append((name, section))

    fuzzy_hits = []
    for norm, group in norm_groups.items():
        if len(group) > 1:
            # Report all pairs
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    name1, sec1 = group[i]
                    name2, sec2 = group[j]
                    # Skip if it's an exact match (already caught above)
                    if name1 == name2:
                        continue
                    fuzzy_hits.append((name1, sec1, name2, sec2))

    # Also check if one normalized name is a substring of another (catch partial overlaps)
    all_norms = list(norm_groups.keys())
    for i in range(len(all_norms)):
        for j in range(i + 1, len(all_norms)):
            a, b = all_norms[i], all_norms[j]
            # Only flag if one is a meaningful substring of the other (>= 5 chars)
            if len(a) >= 5 and len(b) >= 5:
                if a in b or b in a:
                    for name_a, sec_a in norm_groups[a]:
                        for name_b, sec_b in norm_groups[b]:
                            fuzzy_hits.append((name_a, sec_a, name_b, sec_b))

    return fuzzy_hits


def main():
    filepath = sys.argv[1] if len(sys.argv) > 1 else "tier1_companies.md"

    print(f"Parsing: {filepath}\n")
    entries = parse_companies(filepath)
    print(f"Total entries found: {len(entries)}\n")

    # --- Exact duplicates ---
    exact = find_exact_duplicates(entries)
    if exact:
        print("=" * 60)
        print(f"EXACT DUPLICATES ({len(exact)} found)")
        print("=" * 60)
        for name, sections in sorted(exact.items()):
            print(f"\n  \"{name}\" appears {len(sections)} times:")
            for s in sections:
                print(f"    - {s}")
    else:
        print("=" * 60)
        print("EXACT DUPLICATES: None found")
        print("=" * 60)

    # --- Fuzzy / near duplicates ---
    fuzzy = find_fuzzy_duplicates(entries)
    if fuzzy:
        # Deduplicate pairs
        seen_pairs = set()
        unique_fuzzy = []
        for n1, s1, n2, s2 in fuzzy:
            key = tuple(sorted([(n1, s1), (n2, s2)]))
            if key not in seen_pairs:
                seen_pairs.add(key)
                unique_fuzzy.append((n1, s1, n2, s2))

        print(f"\n{'=' * 60}")
        print(f"FUZZY / NEAR DUPLICATES ({len(unique_fuzzy)} pairs found)")
        print("=" * 60)
        for n1, s1, n2, s2 in unique_fuzzy:
            print(f"\n  \"{n1}\"  [{s1}]")
            print(f"  \"{n2}\"  [{s2}]")
    else:
        print(f"\n{'=' * 60}")
        print("FUZZY / NEAR DUPLICATES: None found")
        print("=" * 60)

    # --- Summary ---
    total_issues = len(exact) + (len(fuzzy) if fuzzy else 0)
    print(f"\n{'=' * 60}")
    if total_issues == 0:
        print("CLEAN -- no duplicates detected.")
    else:
        print(f"Review the {total_issues} issue(s) above.")
        print("Note: Some cross-section listings may be intentional")
        print("(e.g., parent in one category, subsidiary in another).")
    print("=" * 60)


if __name__ == "__main__":
    main()
