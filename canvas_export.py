#!/usr/bin/env python3
"""Export your Canvas coursework, one class at a time.

  python3 canvas_export.py list          # every class + export status
  python3 canvas_export.py next          # export the next class not yet done
  python3 canvas_export.py export <id>   # export (or re-run) one class
  python3 canvas_export.py skip <id>     # mark a class as skipped (orientation, etc.)

Options:
  --with-course-files   also download instructor-posted course files
  --out DIR             output folder (default: courses)

Environment:
  CANVAS_URL    your school's Canvas address, e.g. https://myschool.instructure.com
  CANVAS_TOKEN  personal access token (Canvas > Account > Settings > New Access Token)

What gets saved per class: syllabus, every assignment you submitted (all attempts,
files, text entries, links), grades, rubric scores, instructor feedback and its
attachments, and your own discussion posts. Re-running skips files already saved.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


# ---------- helpers ----------

def safe(name, maxlen=80):
    """Make a string safe for a folder name."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", str(name or "")).strip(" .-")
    return name[:maxlen].rstrip(" .-") or "untitled"


def safe_file(name, maxlen=100):
    """Like safe(), but keeps the file extension when truncating."""
    stem, dot, ext = str(name or "file").rpartition(".")
    if not dot or len(ext) > 10:
        return safe(name, maxlen)
    return f"{safe(stem, maxlen - len(ext) - 1)}.{safe(ext, 10)}"


def day(ts):
    return ts[:10] if ts else "—"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cell(text):
    return str(text if text not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


class BlockedHost(Exception):
    pass


# ---------- Canvas client ----------

class Canvas:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, url, params=None, stream=False):
        r = None
        for attempt in range(5):
            try:
                r = self.s.get(url, params=params, stream=stream, timeout=120)
            except requests.exceptions.ProxyError as e:
                failed = getattr(getattr(e, "request", None), "url", None) or url
                raise BlockedHost(urlparse(failed).hostname) from e
            limited = r.status_code == 429 or (
                r.status_code == 403 and "rate limit" in r.text[:300].lower())
            if not limited:
                return r
            time.sleep(2 ** attempt)
        return r

    def get(self, path, params=None):
        r = self._req(f"{self.base}/api/v1{path}", params)
        r.raise_for_status()
        return r.json()

    def paged(self, path, params=None):
        params = {"per_page": 100, **(params or {})}
        url = f"{self.base}/api/v1{path}"
        while url:
            r = self._req(url, params)
            r.raise_for_status()
            data = r.json()
            yield from (data if isinstance(data, list) else [data])
            url, params = r.links.get("next", {}).get("url"), None

    def download(self, url, dest: Path):
        if dest.exists() and dest.stat().st_size > 0:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._req(url, stream=True)
        r.raise_for_status()
        part = dest.with_name(dest.name + ".part")
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        part.replace(dest)
        return True


# ---------- courses ----------

def term_name(co):
    return (co.get("term") or {}).get("name") or "No term"


def course_label(co):
    code, name = co.get("course_code"), co.get("name") or f"Course {co['id']}"
    return f"{code} - {name}" if code and code != name else name


def sort_key(co):
    term = co.get("term") or {}
    return term.get("start_at") or co.get("start_at") or co.get("created_at") or "9999"


def list_courses(c):
    seen = {}
    for state in ("completed", "active"):
        params = {"enrollment_state": state, "include[]": ["term", "total_scores", "teachers"]}
        for co in c.paged("/courses", params):
            seen.setdefault(co["id"], co)
    return sorted(seen.values(), key=sort_key)


def course_dir(out, co):
    return out / safe(term_name(co)) / safe(course_label(co))


# ---------- progress ----------

def load_progress(out):
    p = out / "progress.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_progress(out, progress, courses):
    out.mkdir(parents=True, exist_ok=True)
    (out / "progress.json").write_text(json.dumps(progress, indent=2))
    lines = ["# Undergrad coursework", "",
             f"{sum(1 for v in progress.values() if v['status'] == 'done')} of "
             f"{len(courses)} classes exported. Updated {now_iso()[:10]}.", "",
             "| Term | Class | Status | Assignments | Files |", "|---|---|---|---|---|"]
    for co in courses:
        p = progress.get(str(co["id"]), {})
        st = p.get("stats", {})
        name = cell(course_label(co))
        if p.get("path"):
            rel = Path(p["path"]).relative_to(out).as_posix()
            name = f"[{name}]({rel.replace(' ', '%20')}/README.md)"
        lines.append(f"| {cell(term_name(co))} | {name} | {p.get('status', 'pending')} "
                     f"| {st.get('assignments', '—')} | {st.get('files', '—')} |")
    (out / "README.md").write_text("\n".join(lines) + "\n")


# ---------- export one course ----------

def fetch_submissions(c, cid):
    inc = ["assignment", "submission_history", "submission_comments", "rubric_assessment"]
    try:
        return list(c.paged(f"/courses/{cid}/students/submissions", {"include[]": inc}))
    except requests.HTTPError:
        pass  # some schools restrict the bulk endpoint; go assignment by assignment
    subs = []
    for a in c.paged(f"/courses/{cid}/assignments"):
        try:
            s = c.get(f"/courses/{cid}/assignments/{a['id']}/submissions/self",
                      {"include[]": inc[1:]})
        except requests.HTTPError:
            continue
        s["assignment"] = a
        subs.append(s)
    return subs


def walk_entries(entries):
    for e in entries or []:
        yield e
        yield from walk_entries(e.get("replies"))


def export_course(c, me, co, out, with_files):
    cid = co["id"]
    root = course_dir(out, co)
    root.mkdir(parents=True, exist_ok=True)
    stats = {"assignments": 0, "submitted": 0, "files": 0, "discussions": 0}
    errors, blocked = [], set()

    def grab(url, dest):
        if not url:
            return
        try:
            c.download(url, dest)
            stats["files"] += 1
        except BlockedHost as e:
            blocked.add(e.args[0])
            errors.append(f"blocked host {e.args[0]}: {dest.name}")
        except requests.RequestException as e:
            errors.append(f"{dest.relative_to(root)}: {e}")

    # syllabus + course details
    try:
        full = c.get(f"/courses/{cid}", {"include[]": ["syllabus_body", "term", "teachers"]})
    except requests.HTTPError:
        full = co
    if full.get("syllabus_body"):
        (root / "syllabus.html").write_text(full["syllabus_body"])

    # assignments + my submissions
    try:
        subs = fetch_submissions(c, cid)
    except requests.HTTPError as e:
        subs = []
        errors.append(f"submissions unavailable ({e.response.status_code})")
    subs.sort(key=lambda s: ((s.get("assignment") or {}).get("due_at") or "9999",
                             (s.get("assignment") or {}).get("position") or 0))
    rows = []
    for i, sub in enumerate(subs, 1):
        a = sub.get("assignment") or {}
        aname = a.get("name") or f"Assignment {sub.get('assignment_id')}"
        submitted = sub.get("workflow_state") not in (None, "unsubmitted")
        stats["assignments"] += 1
        stats["submitted"] += submitted
        adir = root / "assignments" / f"{i:02d} {safe(aname)}"
        has_content = submitted or sub.get("score") is not None or sub.get("submission_comments")
        if has_content:
            adir.mkdir(parents=True, exist_ok=True)
            for att in sub.get("submission_history") or [sub]:
                if att.get("workflow_state") == "unsubmitted":
                    continue
                n = att.get("attempt") or 1
                for f in att.get("attachments") or []:
                    grab(f.get("url"), adir / f"attempt-{n}" / safe_file(f.get("display_name") or f.get("filename")))
                if att.get("body"):
                    (adir / f"attempt-{n}").mkdir(exist_ok=True)
                    (adir / f"attempt-{n}" / "text-entry.html").write_text(att["body"])
                media = (att.get("media_comment") or {}).get("url")
                if media:
                    grab(media, adir / f"attempt-{n}" / "media-recording")
            for cm in sub.get("submission_comments") or []:
                for f in cm.get("attachments") or []:
                    grab(f.get("url"), adir / "feedback" / safe_file(f.get("display_name") or f.get("filename")))
            write_submission_md(adir, a, sub)
        rows.append((i, aname, a, sub, adir if has_content else None))

    # my discussion posts
    try:
        topics = list(c.paged(f"/courses/{cid}/discussion_topics"))
    except requests.HTTPError:
        topics = []
    for t in topics:
        try:
            view = c.get(f"/courses/{cid}/discussion_topics/{t['id']}/view")
        except requests.HTTPError:
            continue
        mine = [e for e in walk_entries(view.get("view"))
                if e.get("user_id") == me["id"] and not e.get("deleted")]
        if not mine:
            continue
        stats["discussions"] += 1
        ddir = root / "discussions"
        ddir.mkdir(exist_ok=True)
        md = [f"# {t.get('title')}", "", "## Prompt", "", t.get("message") or "—", "", "## My posts", ""]
        for e in mine:
            md += [f"### {day(e.get('created_at'))}", "", e.get("message") or "", ""]
            att = e.get("attachment")
            if att:
                grab(att.get("url"), ddir / "attachments" / safe_file(att.get("display_name")))
        (ddir / f"{safe(t.get('title'))}.md").write_text("\n".join(md))

    # optional: instructor course files
    if with_files:
        try:
            folders = {f["id"]: f.get("full_name", "") for f in c.paged(f"/courses/{cid}/folders")}
            for f in c.paged(f"/courses/{cid}/files"):
                parts = [safe(p) for p in folders.get(f.get("folder_id"), "").split("/")[1:]]
                grab(f.get("url"), root.joinpath("course-files", *parts, safe_file(f.get("display_name"))))
        except requests.HTTPError as e:
            errors.append(f"course files unavailable to students ({e.response.status_code})")

    write_course_readme(root, full, co, rows, stats, errors)
    return root, stats, errors, blocked


def write_submission_md(adir, a, sub):
    md = [f"# {a.get('name', 'Assignment')}", "",
          f"- **Due:** {day(a.get('due_at'))}",
          f"- **Submitted:** {day(sub.get('submitted_at'))}"
          + (" (late)" if sub.get("late") else ""),
          f"- **Score:** {cell(sub.get('score'))} / {cell(a.get('points_possible'))}",
          f"- **Grade:** {cell(sub.get('grade'))}",
          f"- **Attempts:** {len(sub.get('submission_history') or [sub])}"]
    if sub.get("url"):
        md.append(f"- **Submitted link:** {sub['url']}")
    if a.get("html_url"):
        md.append(f"- **Canvas:** {a['html_url']}")
    if a.get("description"):
        md += ["", "## Assignment brief", "", a["description"]]
    rubric, scores = a.get("rubric") or [], sub.get("rubric_assessment") or {}
    if rubric and scores:
        md += ["", "## Rubric", "", "| Criterion | Points | Comments |", "|---|---|---|"]
        for r in rubric:
            s = scores.get(r.get("id"), {})
            md.append(f"| {cell(r.get('description'))} | {cell(s.get('points'))} / {cell(r.get('points'))} "
                      f"| {cell(s.get('comments'))} |")
    comments = sub.get("submission_comments") or []
    if comments:
        md += ["", "## Feedback", ""]
        for cm in comments:
            md += [f"**{cm.get('author_name', 'Unknown')}** · {day(cm.get('created_at'))}", "",
                   cm.get("comment") or "", ""]
    (adir / "submission.md").write_text("\n".join(md) + "\n")


def write_course_readme(root, full, co, rows, stats, errors):
    enr = next((e for e in co.get("enrollments") or [] if e.get("type") == "student"), {})
    teachers = ", ".join(t.get("display_name", "") for t in full.get("teachers") or co.get("teachers") or [])
    md = [f"# {course_label(co)}", "",
          f"- **Term:** {term_name(co)}",
          f"- **Instructor:** {teachers or '—'}",
          f"- **Final grade:** {cell(enr.get('computed_final_grade'))} "
          f"({cell(enr.get('computed_final_score'))}%)",
          f"- **Exported:** {now_iso()[:10]}", ""]
    if (root / "syllabus.html").exists():
        md += ["[Syllabus](syllabus.html)", ""]
    md += ["## Assignments", "", "| # | Assignment | Due | Submitted | Score | Grade |", "|---|---|---|---|---|---|"]
    for i, name, a, sub, adir in rows:
        label = cell(name)
        if adir:
            label = f"[{label}]({adir.relative_to(root).as_posix().replace(' ', '%20')}/submission.md)"
        md.append(f"| {i} | {label} | {day(a.get('due_at'))} | {day(sub.get('submitted_at'))} "
                  f"| {cell(sub.get('score'))} / {cell(a.get('points_possible'))} | {cell(sub.get('grade'))} |")
    if stats["discussions"]:
        md += ["", "## Discussions", "", f"{stats['discussions']} threads with my posts → [discussions/](discussions/)"]
    if errors:
        md += ["", "## Not exported", ""] + [f"- {e}" for e in errors]
    (root / "README.md").write_text("\n".join(md) + "\n")


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Export Canvas coursework one class at a time.")
    ap.add_argument("command", choices=["list", "next", "export", "skip"])
    ap.add_argument("course_id", nargs="?", type=int)
    ap.add_argument("--with-course-files", action="store_true")
    ap.add_argument("--out", default="courses")
    args = ap.parse_args()

    base, token = os.environ.get("CANVAS_URL", "").strip(), os.environ.get("CANVAS_TOKEN", "").strip()
    if not base or not token:
        sys.exit("Set CANVAS_URL (e.g. https://myschool.instructure.com) and CANVAS_TOKEN first.")
    if not base.startswith("http"):
        base = "https://" + base
    c, out = Canvas(base, token), Path(args.out)

    try:
        me = c.get("/users/self")
        courses = list_courses(c)
    except BlockedHost as e:
        sys.exit(f"Network policy blocked {e.args[0]}. Add it to the environment's Allowed domains.")
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            sys.exit("Canvas rejected the token (401). Generate a new one and update CANVAS_TOKEN.")
        raise

    progress = load_progress(out)
    by_id = {co["id"]: co for co in courses}

    if args.command == "list":
        print(f"Signed in as {me.get('name')} · {len(courses)} classes\n")
        print(f"{'ID':>8}  {'Term':<20} {'Status':<9} Class")
        for co in courses:
            st = progress.get(str(co["id"]), {}).get("status", "pending")
            print(f"{co['id']:>8}  {term_name(co)[:20]:<20} {st:<9} {course_label(co)}")
        save_progress(out, progress, courses)
        return

    if args.command == "next":
        todo = [co for co in courses if str(co["id"]) not in progress]
        if not todo:
            print("Every class is exported or skipped.")
            return
        co = todo[0]
    else:
        if args.course_id not in by_id:
            sys.exit(f"No class with ID {args.course_id}. Run `list` to see IDs.")
        co = by_id[args.course_id]

    if args.command == "skip":
        progress[str(co["id"])] = {"status": "skipped", "at": now_iso()}
        save_progress(out, progress, courses)
        print(f"Skipped {course_label(co)}")
        return

    print(f"Exporting {term_name(co)} · {course_label(co)} …")
    root, stats, errors, blocked = export_course(c, me, co, out, args.with_course_files)
    progress[str(co["id"])] = {"status": "partial" if errors else "done", "path": str(root),
                               "at": now_iso(), "stats": stats, "errors": errors}
    save_progress(out, progress, courses)
    left = sum(1 for x in courses if str(x["id"]) not in progress)
    print(f"  {stats['assignments']} assignments ({stats['submitted']} submitted), "
          f"{stats['files']} files, {stats['discussions']} discussion threads → {root}")
    for e in errors:
        print(f"  ! {e}")
    if blocked:
        print(f"  Add to Allowed domains, then re-run `export {co['id']}`: {', '.join(sorted(blocked))}")
    print(f"  {left} classes left.")


if __name__ == "__main__":
    main()
