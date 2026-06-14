#!/usr/bin/python3
"""Sync ideas.csv → GitHub issues (one-way; the CSV is the source of truth).

For each row the script keeps a matching GitHub issue:
  title  = "#<n> <Feature>"
  body   = <Notes> (+ "Resolved in <sha>" when the Commit column is filled)
  labels = "ideas.csv" (provenance marker) + "<feasibility>"
  state  = closed when Status is Done, otherwise open

The link between a row and its issue is the Issue column, written back after
creation, so re-runs never duplicate. A per-row Hash lets unchanged rows be
skipped. Issues you file by hand are never touched — the script only acts on
issue numbers it recorded itself.

Filter in the GitHub UI:
  label:ideas.csv     → roadmap issues created by this script
  -label:ideas.csv    → issues filed by hand

Usage:
  extras/sync_issues.py            sync all rows
  extras/sync_issues.py --dry-run  show what would change, touch nothing
  extras/sync_issues.py --repo OWNER/NAME
"""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys

DEFAULT_REPO = "brokkoli71/sidemark"
SYNC_LABEL = "ideas.csv"            # provenance marker; filter hand-filed issues with -label:ideas.csv
CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ideas.csv")
FIELDS = ["#", "Feature", "Feasibility", "Status", "Notes", "Issue", "Hash", "Commit"]
FEASIBILITY_COLORS = {"easy": "0e8a16", "medium": "fbca04", "hard": "d93f0b"}


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def feasibility_label(value):
    return f"{value.strip().lower()}" if value.strip() else None


def is_done(row):
    return (row.get("Status") or "").strip().lower() == "done"


def build_title(row):
    return f"#{row['#']} {row['Feature'].strip()}"


_REF_RE = re.compile(r"#(\d+)")


def resolve_refs(text, idea_map):
    """Rewrite '#N' idea references into the GitHub issue they became.

    The CSV refers to other ideas by their idea number (e.g. 'pairs with
    #14'); idea #14 maps to some other GitHub issue number. idea_map is
    {idea_number_str: issue_number_str}. Unknown numbers are left untouched.
    """
    def repl(m):
        issue = idea_map.get(m.group(1))
        return f"#{issue}" if issue else m.group(0)
    return _REF_RE.sub(repl, text)


def build_body(row, idea_map):
    notes = resolve_refs((row.get("Notes") or "").strip(), idea_map)
    header = ("| | |\n|--|--|\n"
              f"| **Feasibility** | {(row.get('Feasibility') or '—').strip()} |\n"
              f"| **Status** | {(row.get('Status') or '—').strip()} |\n")
    body = f"{header}\n{notes}"
    commit = (row.get("Commit") or "").strip()
    if commit:
        body += f"\n\n---\nResolved in {commit}"
    body += (f"\n\n<!-- auto-synced from ideas.csv (idea #{row['#']}); "
             "edits here are overwritten on the next sync -->")
    return body


def payload_hash(row, idea_map):
    """Hash everything that lands on GitHub, so a re-render (e.g. once a
    cross-reference resolves) is detected and re-synced."""
    h = hashlib.sha256()
    h.update(build_title(row).encode())
    h.update(b"\0")
    h.update(build_body(row, idea_map).encode())
    h.update(b"\0")
    h.update(b"closed" if is_done(row) else b"open")
    h.update(b"\0")
    h.update(",".join(labels_for(row)).encode())
    return h.hexdigest()[:12]


def labels_for(row):
    labels = [SYNC_LABEL]
    fl = feasibility_label(row.get("Feasibility") or "")
    if fl:
        labels.append(fl)
    return labels


def ensure_labels(repo, rows, dry_run):
    needed = {SYNC_LABEL: "5319e7"}
    for row in rows:
        fl = feasibility_label(row.get("Feasibility") or "")
        if fl:
            key = (row["Feasibility"] or "").strip().lower().split("-")[0]
            needed[fl] = FEASIBILITY_COLORS.get(key, "ededed")
    for name, color in needed.items():
        if dry_run:
            continue
        # --force makes this create-or-update and idempotent across runs
        subprocess.run(["gh", "label", "create", name, "-R", repo,
                        "--color", color, "--force"],
                       text=True, capture_output=True)


def issue_state(repo, number):
    out = gh("issue", "view", number, "-R", repo, "--json", "state")
    import json
    return json.loads(out)["state"].lower()  # "open" / "closed"


def create_issue(repo, row, idea_map):
    url = gh("issue", "create", "-R", repo,
             "--title", build_title(row),
             "--body", build_body(row, idea_map),
             *sum((["--label", l] for l in labels_for(row)), []))
    return url.rstrip("/").rsplit("/", 1)[-1]


def update_issue(repo, number, row, idea_map):
    gh("issue", "edit", number, "-R", repo,
       "--title", build_title(row),
       "--body", build_body(row, idea_map),
       *sum((["--add-label", l] for l in labels_for(row)), []))


def sync_state(repo, number, row):
    want_closed = is_done(row)
    state = issue_state(repo, number)
    if want_closed and state == "open":
        gh("issue", "close", number, "-R", repo)
    elif not want_closed and state == "closed":
        gh("issue", "reopen", number, "-R", repo)


def validate_csv():
    """Abort before touching GitHub if the CSV is malformed.

    Hand-editing the file can leave a row with an unquoted comma (so Notes
    text bleeds into the Issue/Hash columns or overflows the header) or a
    duplicate idea number; either would create garbage issues. Trailing
    bookkeeping columns may be omitted on hand-added rows, so this checks the
    parsed values rather than a raw field count.
    """
    with open(CSV_PATH, newline="") as f:
        reader = csv.DictReader(f, restkey="_overflow")
        problems, seen = [], {}
        for line_no, row in enumerate(reader, start=2):
            num = (row.get("#") or "").strip()
            if not num:
                continue
            if row.get("_overflow"):
                problems.append(
                    f"  line {line_no} (#{num}): too many fields — "
                    "an unquoted comma in a field")
            issue = (row.get("Issue") or "").strip()
            if issue and not issue.isdigit():
                problems.append(
                    f"  line {line_no} (#{num}): Issue column is "
                    f"'{issue[:30]}' — expected a number (unquoted comma?)")
            h = (row.get("Hash") or "").strip()
            if h and not re.fullmatch(r"[0-9a-f]+", h):
                problems.append(
                    f"  line {line_no} (#{num}): Hash column is "
                    f"'{h[:30]}' — expected hex (unquoted comma?)")
            if num in seen:
                problems.append(
                    f"  line {line_no}: duplicate idea #{num} "
                    f"(also line {seen[num]})")
            seen[num] = line_no
    if problems:
        sys.exit("ideas.csv is malformed — fix these before syncing:\n"
                 + "\n".join(problems))


def main():
    ap = argparse.ArgumentParser(description="Sync ideas.csv → GitHub issues.")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", metavar="N", action="append",
                    help="sync only idea number N (repeatable); good for a test run")
    args = ap.parse_args()

    validate_csv()

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:                       # backfill columns added by this script
        for key in FIELDS:
            row.setdefault(key, "")
            if row[key] is None:
                row[key] = ""

    ensure_labels(args.repo, rows, args.dry_run)

    def selected(row):
        return (row.get("Feature") or "").strip() and (
            not args.only or row["#"] in args.only)

    # idea_map (idea# -> issue#) drives cross-reference resolution. Built from
    # rows already linked, then extended as Pass 1 creates the missing ones.
    idea_map = {r["#"]: r["Issue"].strip()
                for r in rows if (r.get("Issue") or "").strip()}

    created = updated = skipped = 0

    # ── Pass 1: create issues for rows that don't have one yet ───────────────
    for row in rows:
        if not selected(row) or (row.get("Issue") or "").strip():
            continue
        if args.dry_run:
            print(f"CREATE  {build_title(row)}  [{'closed' if is_done(row) else 'open'}]")
            created += 1
            continue
        number = create_issue(args.repo, row, idea_map)
        sync_state(args.repo, number, row)
        row["Issue"] = number
        idea_map[row["#"]] = number
        row["Hash"] = payload_hash(row, idea_map)
        print(f"created #{number}  {build_title(row)}")
        created += 1

    # ── Pass 2: sync body/title/state now that every #N reference resolves ────
    for row in rows:
        number = (row.get("Issue") or "").strip()
        if not selected(row) or not number:
            continue
        new_hash = payload_hash(row, idea_map)
        if row.get("Hash") == new_hash:
            skipped += 1
            continue
        if args.dry_run:
            print(f"UPDATE  #{number}  {build_title(row)}")
            updated += 1
            continue
        update_issue(args.repo, number, row, idea_map)
        sync_state(args.repo, number, row)
        row["Hash"] = new_hash
        print(f"updated #{number}  {build_title(row)}")
        updated += 1

    if not args.dry_run:
        tmp = CSV_PATH + ".tmp"
        with open(tmp, "w", newline="") as f:
            # force LF so the rewrite doesn't churn the whole file to CRLF
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore",
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, CSV_PATH)

    print(f"\n{created} created, {updated} updated, {skipped} unchanged"
          + ("  (dry run — nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
