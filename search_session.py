#!/usr/bin/env python3
"""Unified opencode session search & transcript tool.

Supports BOTH opencode database schemas and auto-detects which one to use:

  v1 (legacy, <= v1.x):  tables  session / message / part
  v2 (current, >= v2.0): tables  session_v2 / session_message

Detection rules
---------------
* Schema availability is read from sqlite_master, so the tool works against a DB
  that only has v1 tables, only has v2 tables, or (the normal case after an
  upgrade) has both.
* When v2 tables are present they are preferred, because opencode migrates every
  existing v1 session into `session_v2`/`session_message` — v2 is a full superset
  of v1. A session that exists only in v1 still falls back to the v1 tables.
* Every command reports or relies on the per-session type (`v1` / `v2`), so a
  single command works for old and new sessions alike.

Compaction never deletes stored history, so full pre-compaction transcripts are
recoverable in both schemas.

Usage:
    search_session.py schemas
    search_session.py list
    search_session.py title PATTERN
    search_session.py search KEYWORD
    search_session.py info SESSION_ID
    search_session.py tools SESSION_ID
    search_session.py patches SESSION_ID
    search_session.py transcript SESSION_ID [OUTPUT_PATH]

Discovery output is capped by a character budget (SS_MAX_CHARS, default 12000;
0 = unlimited), not by an arbitrary result count.
"""

import datetime
import json
import os
import sqlite3
import sys
import tempfile

# Override with OPENCODE_DB to point at a copy (useful for testing / other installs).
DB = os.path.expanduser(
    os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db")
)

# Session id to hide from discovery commands (list/title/search). The `ss`
# wrapper sets this to the current session unless --include-current is given.
EXCLUDE = os.environ.get("SS_EXCLUDE", "").strip()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# Default output length budget for discovery commands (list/title/search). It
# caps the *volume* of output, not the number of results, so medium result sets
# are never filtered just because an arbitrary count was reached. 0 disables
# the budget; SS_MAX_CHARS overrides.
DEFAULT_MAX_CHARS = 12000
MAX_CHARS = _env_int("SS_MAX_CHARS", DEFAULT_MAX_CHARS)

# Search presentation. Grouping keeps one session header and at most
# PER_SESSION snippets per session; SS_GROUP=0 restores the flat listing.
GROUP = os.environ.get("SS_GROUP", "1") != "0"
PER_SESSION = max(1, _env_int("SS_PER_SESSION", 3))
COLOR = os.environ.get("SS_COLOR", "auto").lower()

_raw_snippet = os.environ.get("SS_SNIPPET", "").strip()
try:
    SNIPPET = int(_raw_snippet) if _raw_snippet else None
except ValueError:
    SNIPPET = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ts(ms):
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def trunc(s, n=300):
    s = "" if s is None else str(s)
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + "…"


def snippet_around(text, kw, width=300):
    text = "" if text is None else str(text).replace("\n", " ⏎ ")
    if width <= 0:  # 0 / negative means: keep the full snippet
        return text
    i = text.lower().find(kw.lower())
    if i < 0:
        return trunc(text, width)
    start = max(0, i - width // 3)
    seg = text[start : start + width]
    pre = "…" if start else ""
    post = "…" if start + width < len(text) else ""
    return pre + seg + post


def tables(con):
    return {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def detect(con):
    """Return (has_v1, has_v2) based on which schema tables exist."""
    t = tables(con)
    has_v1 = {"session", "message", "part"} <= t
    has_v2 = {"session_v2", "session_message"} <= t
    return has_v1, has_v2


def session_type(con, sid, has_v1, has_v2):
    """Return 'v2', 'v1', or None for a specific session id."""
    if (
        has_v2
        and con.execute(
            "SELECT 1 FROM session_v2 WHERE id=? LIMIT 1", (sid,)
        ).fetchone()
    ):
        return "v2"
    if (
        has_v1
        and con.execute("SELECT 1 FROM session WHERE id=? LIMIT 1", (sid,)).fetchone()
    ):
        return "v1"
    return None


def v2_message_text(d):
    """Flatten a v2 session_message `data` blob into searchable plain text."""
    if not isinstance(d, dict):
        return ""
    for key in ("text", "summary"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            # compaction: prefix so it is obvious where the text came from
            return ("[compaction summary] " if key == "summary" else "") + v
    parts = []
    for c in d.get("content") or []:
        if not isinstance(c, dict):
            continue
        ct = c.get("type")
        if ct in ("text", "reasoning") and c.get("text"):
            parts.append(c["text"])
        elif ct == "tool":
            st = c.get("state") or {}
            parts.append(f"[tool:{c.get('name')}] " + tool_output(st))
            try:
                parts.append(json.dumps(st.get("input", {}), ensure_ascii=False))
            except (TypeError, ValueError):
                pass
    return " ".join(p for p in parts if p)


def tool_output(state):
    """Best-effort tool output across both schemas."""
    if not isinstance(state, dict):
        return ""
    out = state.get("output")  # v1
    if isinstance(out, str) and out.strip():
        return out
    ch = state.get("content")  # v2
    if isinstance(ch, list):
        return "\n".join(
            x.get("text", "") for x in ch if isinstance(x, dict) and x.get("text")
        )
    return ""


def die(msg):
    print(msg, file=sys.stderr)
    return 1


class Emit:
    """Print result blocks until the character budget is reached.

    The first block always prints, even if it alone exceeds the budget, so a
    single large hit is never hidden.
    """

    def __init__(self, max_chars=None):
        self.max_chars = MAX_CHARS if max_chars is None else max_chars
        self.used = 0
        self.truncated = False
        self.count = 0

    def block(self, lines):
        text = "\n".join(lines)
        size = len(text) + 1
        if self.max_chars > 0 and self.used > 0 and self.used + size > self.max_chars:
            self.truncated = True
            return False
        print(text)
        self.used += size
        self.count += 1
        return True

    def done(self):
        if self.truncated:
            print(
                f"(output truncated at {self.max_chars} chars — refine the keyword "
                "or pass --max-chars 0 for everything)"
            )


class Style:
    """ANSI helpers that stay silent unless stdout is an interactive terminal.

    SS_COLOR=auto (default) enables color only on a TTY and honors NO_COLOR;
    always/never force the choice. Agent shell calls are not TTYs, so their
    output stays plain.
    """

    def __init__(self, mode=None):
        mode = (mode or COLOR or "auto").lower()
        self.on = mode == "always" or (
            mode == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        )

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.on else str(text)

    def dim(self, text):
        return self._wrap("2", text)

    def bold(self, text):
        return self._wrap("1", text)

    def cyan(self, text):
        return self._wrap("36", text)

    def yellow(self, text):
        return self._wrap("33", text)

    def green(self, text):
        return self._wrap("32", text)

    def red(self, text):
        return self._wrap("31", text)


def highlight(text, kw, sty):
    """Wrap every case-insensitive occurrence of kw in red (TTY only)."""
    if not sty.on or not kw:
        return text
    low, key = text.lower(), kw.lower()
    out, i = [], 0
    while True:
        j = low.find(key, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(sty.red(text[j : j + len(kw)]))
        i = j + len(kw)
    return "".join(out)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_schemas(con):
    has_v1, has_v2 = detect(con)
    print(f"db: {DB}")
    if has_v1:
        n = con.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        print(f"v1 (session/message/part):  yes   sessions={n} messages={m}")
    else:
        print("v1 (session/message/part):  no")
    if has_v2:
        n = con.execute("SELECT COUNT(*) FROM session_v2").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM session_message").fetchone()[0]
        print(f"v2 (session_v2/session_message):  yes   sessions={n} messages={m}")
    else:
        print("v2 (session_v2/session_message):  no")
    active = "v2" if has_v2 else ("v1" if has_v1 else "none")
    print(f"active schema: {active}")
    return 0


def cmd_list(con):
    _, has_v2 = detect(con)
    tbl = "session_v2" if has_v2 else "session"
    sql = f"SELECT id, title, directory, time_created FROM {tbl} "
    params = []
    if EXCLUDE:
        sql += "WHERE id != ? "
        params.append(EXCLUDE)
    sql += "ORDER BY time_created DESC"
    em = Emit()
    for r in con.execute(sql, params):
        line = f"{ts(r['time_created'])}  {r['id']}  {trunc(r['title'], 70)}"
        if not em.block([line]):
            break
    em.done()
    return 0


def cmd_title(con, pattern):
    _, has_v2 = detect(con)
    tbl = "session_v2" if has_v2 else "session"
    sql = f"SELECT id, title, time_created FROM {tbl} WHERE title LIKE ?"
    params = [f"%{pattern}%"]
    if EXCLUDE:
        sql += " AND id != ?"
        params.append(EXCLUDE)
    sql += " ORDER BY time_created DESC"
    em = Emit()
    for r in con.execute(sql, params):
        line = f"{ts(r['time_created'])}  {r['id']}  {trunc(r['title'], 70)}"
        if not em.block([line]):
            break
    if em.count == 0:
        print(f"(no session titles match: {pattern})")
    em.done()
    return 0


def _flat_search(con, kw, has_v2, em, sty):
    snip_w = SNIPPET if SNIPPET is not None else 300
    if has_v2:
        sql = (
            "SELECT sm.session_id AS sid, s.title AS title, sm.time_created AS t, "
            "       sm.type AS mtype, sm.data AS data "
            "FROM session_message sm JOIN session_v2 s ON s.id = sm.session_id "
            "WHERE sm.data LIKE ?"
        )
        params = [f"%{kw}%"]
        if EXCLUDE:
            sql += " AND sm.session_id != ?"
            params.append(EXCLUDE)
        sql += " ORDER BY sm.time_created DESC"
        for r in con.execute(sql, params):
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                d = {}
            block = [
                f"{sty.dim(ts(r['t']))}  {sty.dim('[' + str(r['mtype']) + ']')}  "
                f"{sty.bold(trunc(r['title'], 55))}  {sty.cyan(r['sid'])}",
                f"    {highlight(snippet_around(v2_message_text(d), kw, snip_w), kw, sty)}",
            ]
            if not em.block(block):
                break
    else:
        sql = (
            "SELECT s.title AS title, s.id AS sid, p.time_created AS t, "
            "       json_extract(p.data,'$.text') AS text "
            "FROM part p JOIN session s ON p.session_id = s.id "
            "WHERE json_extract(p.data,'$.type') = 'text' "
            "  AND json_extract(p.data,'$.text') LIKE ?"
        )
        params = [f"%{kw}%"]
        if EXCLUDE:
            sql += " AND s.id != ?"
            params.append(EXCLUDE)
        sql += " ORDER BY s.time_created DESC"
        for r in con.execute(sql, params):
            block = [
                f"{sty.dim(ts(r['t']))}  {sty.bold(trunc(r['title'], 55))}  "
                f"{sty.cyan(r['sid'])}",
                f"    {highlight(snippet_around(r['text'], kw, snip_w), kw, sty)}",
            ]
            if not em.block(block):
                break
    if em.count == 0:
        print(f"(no matches: {kw})")


def _grouped_search(con, kw, has_v2, em, sty):
    snip_w = SNIPPET if SNIPPET is not None else 180
    if has_v2:
        count_sql = (
            "SELECT sm.session_id AS sid, s.title AS title, COUNT(*) AS n, "
            "       MAX(sm.time_created) AS t "
            "FROM session_message sm JOIN session_v2 s ON s.id = sm.session_id "
            "WHERE sm.data LIKE ?"
        )
        count_params = [f"%{kw}%"]
        if EXCLUDE:
            count_sql += " AND sm.session_id != ?"
            count_params.append(EXCLUDE)
        count_sql += " GROUP BY sm.session_id ORDER BY t DESC"
        detail_sql = (
            "SELECT time_created AS t, type AS mtype, data AS data "
            "FROM session_message WHERE session_id = ? AND data LIKE ? "
            "ORDER BY time_created ASC LIMIT ?"
        )
    else:
        count_sql = (
            "SELECT s.id AS sid, s.title AS title, COUNT(*) AS n, "
            "       MAX(p.time_created) AS t "
            "FROM part p JOIN session s ON p.session_id = s.id "
            "WHERE json_extract(p.data,'$.type') = 'text' "
            "  AND json_extract(p.data,'$.text') LIKE ?"
        )
        count_params = [f"%{kw}%"]
        if EXCLUDE:
            count_sql += " AND s.id != ?"
            count_params.append(EXCLUDE)
        count_sql += " GROUP BY s.id ORDER BY t DESC"
        detail_sql = (
            "SELECT p.time_created AS t, json_extract(p.data,'$.text') AS text "
            "FROM part p WHERE p.session_id = ? "
            "  AND json_extract(p.data,'$.type') = 'text' "
            "  AND json_extract(p.data,'$.text') LIKE ? "
            "ORDER BY p.time_created ASC LIMIT ?"
        )

    groups = con.execute(count_sql, count_params).fetchall()
    if not groups:
        print(f"(no matches: {kw})")
        return None

    total = sum(g["n"] for g in groups)
    ordered = sorted(groups, key=lambda g: g["t"] or 0)  # oldest -> newest

    for g in ordered:
        rows = con.execute(detail_sql, (g["sid"], f"%{kw}%", PER_SESSION)).fetchall()
        plural = "" if g["n"] == 1 else "es"
        lines = [
            f"{sty.cyan('▌')} {sty.dim(ts(g['t']))}  "
            f"{sty.bold(trunc(g['title'], 58))}  {sty.cyan(g['sid'])}  "
            f"{sty.yellow('[' + str(g['n']) + ' match' + plural + ']')}"
        ]
        for r in rows:
            if has_v2:
                try:
                    d = json.loads(r["data"])
                except (TypeError, ValueError):
                    d = {}
                snip = snippet_around(v2_message_text(d), kw, snip_w)
                role = r["mtype"]
            else:
                snip = snippet_around(r["text"], kw, snip_w)
                role = "text"
            lines.append(f"    {sty.dim(ts(r['t']))}  {sty.dim('[' + str(role) + ']')}")
            lines.append(f"        {highlight(snip, kw, sty)}")
        more = g["n"] - len(rows)
        if more > 0:
            lines.append(
                f"    {sty.yellow(f'+{more} more match' + ('' if more == 1 else 'es'))}"
            )
        if not em.block(lines):
            break

    return ordered, total


def _print_summary(groups, total, kw, sty):
    top = sorted(groups, key=lambda g: (g["n"], g["t"] or 0), reverse=True)[:10]
    print()
    print(sty.dim("─" * 64))
    print(
        f"{sty.bold('Summary')} — {sty.red(chr(34) + kw + chr(34))}: "
        f"{sty.bold(len(groups))} session{'s' if len(groups) != 1 else ''}, "
        f"{sty.bold(total)} match{'es' if total != 1 else ''}"
    )
    print(sty.dim("Top 10 sessions by match count:"))
    for g in top:
        print(
            f"  {sty.dim(ts(g['t']))}  {trunc(g['title'], 46)}  "
            f"{sty.cyan(g['sid'])}  {sty.yellow('(' + str(g['n']) + ')')}"
        )


def cmd_search(con, kw):
    _, has_v2 = detect(con)
    sty = Style()
    em = Emit()
    if GROUP:
        result = _grouped_search(con, kw, has_v2, em, sty)
        em.done()
        if result:
            _print_summary(result[0], result[1], kw, sty)
    else:
        _flat_search(con, kw, has_v2, em, sty)
        em.done()
    return 0


def cmd_info(con, sid):
    has_v1, has_v2 = detect(con)
    st = session_type(con, sid, has_v1, has_v2)
    if not st:
        return die(f"session not found: {sid}")
    in_v1 = bool(
        has_v1
        and con.execute("SELECT 1 FROM session WHERE id=? LIMIT 1", (sid,)).fetchone()
    )
    in_v2 = bool(
        has_v2
        and con.execute(
            "SELECT 1 FROM session_v2 WHERE id=? LIMIT 1", (sid,)
        ).fetchone()
    )
    where = (
        "both (v2 is the migrated copy)"
        if in_v1 and in_v2
        else ("v2 only" if in_v2 else "v1 only")
    )
    tbl = "session_v2" if st == "v2" else "session"
    r = con.execute(f"SELECT * FROM {tbl} WHERE id=?", (sid,)).fetchone()
    print(f"session: {sid}")
    print(f"type: {st}    (present in: {where})")
    print(f"title: {r['title']}")
    print(f"directory: {r['directory']}")
    print(f"created: {ts(r['time_created'])}")
    print(f"updated: {ts(r['time_updated'])}")
    if st == "v2":
        msgs = "session_message"
        rows = con.execute(
            f"SELECT type, COUNT(*) c FROM {msgs} WHERE session_id=? GROUP BY type "
            "ORDER BY c DESC",
            (sid,),
        ).fetchall()
        print("messages:")
        for x in rows:
            print(f"  {x['type']:<16} {x['c']}")
    else:
        rows = con.execute(
            "SELECT json_extract(data,'$.role') role, COUNT(*) c FROM message "
            "WHERE session_id=? GROUP BY role",
            (sid,),
        ).fetchall()
        print("messages:")
        for x in rows:
            print(f"  {x['role']:<16} {x['c']}")
    return 0


def cmd_tools(con, sid):
    has_v1, has_v2 = detect(con)
    st = session_type(con, sid, has_v1, has_v2)
    if not st:
        return die(f"session not found: {sid}")
    if st == "v2":
        rows = con.execute(
            "SELECT seq, time_created, data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            for c in d.get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool":
                    continue
                stt = c.get("state") or {}
                t = stt.get("input") or {}
                print(
                    f"{ts(r['time_created'])}  {c.get('name', '?'):<28} "
                    f"{stt.get('status', '?'):<10}  {trunc(json.dumps(t, ensure_ascii=False), 160)}"
                )
    else:
        rows = con.execute(
            "SELECT time_created, data FROM part WHERE session_id=? "
            "AND json_extract(data,'$.type')='tool' ORDER BY time_created",
            (sid,),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            stt = d.get("state") or {}
            print(
                f"{ts(r['time_created'])}  {d.get('tool', '?'):<28} "
                f"{stt.get('status', '?'):<10}  "
                f"{trunc(json.dumps(stt.get('input', {}), ensure_ascii=False), 160)}"
            )
    return 0


FILE_TOOLS = {"edit", "write", "multiedit", "patch", "apply_patch"}


def cmd_patches(con, sid):
    has_v1, has_v2 = detect(con)
    st = session_type(con, sid, has_v1, has_v2)
    if not st:
        return die(f"session not found: {sid}")
    # v2 dropped the `patch` part entirely, and the migration did NOT carry it over
    # (verified: sessions with v1 patch rows show no edit/write tools in v2). So v1
    # patch parts stay authoritative whenever they exist — including for sessions
    # that were migrated into v2.
    if has_v1:
        rows = con.execute(
            "SELECT time_created, data FROM part WHERE session_id=? "
            "AND json_extract(data,'$.type')='patch' ORDER BY time_created",
            (sid,),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            files = d.get("files") or []
            print(
                f"{ts(r['time_created'])}  {d.get('hash', '?')[:12]}  files={len(files)}"
            )
            for fp in files:
                print(f"    {fp}")
        if rows:
            return 0

    # v2-native session (or no v1 patch rows): file mutations are edit/write tool calls.
    if has_v2:
        rows = con.execute(
            "SELECT seq, time_created, data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            for c in d.get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool":
                    continue
                if c.get("name") not in FILE_TOOLS:
                    continue
                inp = (c.get("state") or {}).get("input") or {}
                path = (
                    inp.get("path")
                    or inp.get("filePath")
                    or inp.get("file_path")
                    or "?"
                )
                print(f"{ts(r['time_created'])}  {c.get('name'):<8}  {path}")
    return 0


def cmd_transcript(con, sid, out):
    has_v1, has_v2 = detect(con)
    st = session_type(con, sid, has_v1, has_v2)
    if not st:
        return die(f"session not found: {sid}")
    return (
        _transcript_v2(con, sid, out) if st == "v2" else _transcript_v1(con, sid, out)
    )


def _write_out(out, sid, title, lines):
    if not out:
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in title).strip("_")
        slug = slug or "session"
        out_dir = os.environ.get("SS_OUT_DIR") or os.path.join(
            tempfile.gettempdir(), "ocsearch"
        )
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{slug}-{sid[:8]}.md")
    with open(out, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out}: {os.path.getsize(out)} bytes, {len(lines)} lines")
    return 0


def _transcript_v1(con, sid, out):
    sess = con.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
    msgs = con.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? "
        "ORDER BY time_created, id",
        (sid,),
    ).fetchall()

    def parts_for(mid):
        return con.execute(
            "SELECT id, time_created, data FROM part WHERE message_id=? "
            "ORDER BY time_created, id",
            (mid,),
        ).fetchall()

    w = []
    w.append(f"# {sess['title']}")
    w += [
        "",
        f"- **Session**: `{sid}`",
        "- **Schema**: `v1`",
        f"- **Directory**: `{sess['directory']}`",
        f"- **Created**: {ts(sess['time_created'])}",
        f"- **Last updated**: {ts(sess['time_updated'])}",
        "",
    ]

    for m in msgs:
        data = json.loads(m["data"])
        role = data.get("role", "?")
        mode = data.get("mode", "")
        if data.get("summary") and role == "assistant":
            w += [
                f"## COMPACTION SUMMARY — {ts(m['time_created'])}",
                "*(original context replaced by this summary from here on; "
                "full earlier history below)*",
                "",
            ]
        elif role == "user":
            w += [f"## USER — {ts(m['time_created'])}"]
            if mode:
                w.append(f"*(mode: {mode})*")
            w.append("")
        else:
            model = (data.get("model") or {}).get("modelID", "")
            w += [
                f"## ASSISTANT — {ts(m['time_created'])} {('- ' + model) if model else ''}",
                "",
            ]

        for p in parts_for(m["id"]):
            d = json.loads(p["data"])
            t = d.get("type")
            if t == "text":
                if d.get("text", "").strip():
                    w += [d["text"].strip(), ""]
            elif t == "reasoning":
                if d.get("text", "").strip():
                    w += [
                        "<details><summary>reasoning</summary>",
                        "",
                        "```",
                        d["text"].strip(),
                        "```",
                        "</details>",
                        "",
                    ]
            elif t == "tool":
                stt = d.get("state") or {}
                w += [f"**Tool: {d.get('tool', '?')}** `{stt.get('status', '?')}`", ""]
                inp = stt.get("input", {})
                if inp not in (None, {}):
                    w += [
                        "Input:",
                        "",
                        "```",
                        json.dumps(inp, ensure_ascii=False, indent=2),
                        "```",
                        "",
                    ]
                output = stt.get("output", "")
                if output:
                    w += ["Output:", "", "```", output, "```", ""]
                if stt.get("error"):
                    w += [f"Error: {stt['error']}", ""]
            elif t == "compaction":
                w += [
                    "*[compaction checkpoint here - the context up to this point "
                    "was summarized]*",
                    "",
                ]
            elif t in ("file", "image"):
                w += [
                    f"*[attached {t}: {d.get('filename', '')} ({d.get('mime', '')})]*",
                    "",
                ]
        w += ["---", ""]

    return _write_out(out, sid, sess["title"], w)


def _transcript_v2(con, sid, out):
    sess = con.execute("SELECT * FROM session_v2 WHERE id=?", (sid,)).fetchone()
    rows = con.execute(
        "SELECT seq, type, time_created, data FROM session_message "
        "WHERE session_id=? ORDER BY seq",
        (sid,),
    ).fetchall()

    w = []
    w.append(f"# {sess['title']}")
    w += [
        "",
        f"- **Session**: `{sid}`",
        "- **Schema**: `v2`",
        f"- **Directory**: `{sess['directory']}`",
        f"- **Created**: {ts(sess['time_created'])}",
        f"- **Last updated**: {ts(sess['time_updated'])}",
        "",
    ]

    for r in rows:
        try:
            d = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        t = r["type"]

        if t == "user":
            w += [f"## USER — {ts(r['time_created'])}", ""]
            if d.get("text"):
                w += [d["text"].strip(), ""]
            for f in d.get("files") or []:
                name = (
                    f.get("filename") or f.get("name") or f if isinstance(f, str) else f
                )
                w += [f"*[attached file: {name}]*", ""]
        elif t == "compaction":
            w += [
                f"## COMPACTION SUMMARY — {ts(r['time_created'])}",
                "*(original context replaced by this summary from here on; "
                "full earlier history below)*",
                "",
            ]
            if d.get("summary"):
                w += [d["summary"].strip(), ""]
        elif t == "assistant":
            model = (d.get("model") or {}).get("id", "")
            w += [
                f"## ASSISTANT — {ts(r['time_created'])} {('- ' + model) if model else ''}",
                "",
            ]
            for c in d.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    if (c.get("text") or "").strip():
                        w += [c["text"].strip(), ""]
                elif ct == "reasoning":
                    if (c.get("text") or "").strip():
                        w += [
                            "<details><summary>reasoning</summary>",
                            "",
                            "```",
                            c["text"].strip(),
                            "```",
                            "</details>",
                            "",
                        ]
                elif ct == "tool":
                    stt = c.get("state") or {}
                    w += [
                        f"**Tool: {c.get('name', '?')}** `{stt.get('status', '?')}`",
                        "",
                    ]
                    inp = stt.get("input", {})
                    if inp not in (None, {}):
                        w += [
                            "Input:",
                            "",
                            "```",
                            json.dumps(inp, ensure_ascii=False, indent=2),
                            "```",
                            "",
                        ]
                    output = tool_output(stt)
                    if output:
                        w += ["Output:", "", "```", output, "```", ""]
                    if (stt.get("metadata") or {}).get("truncated"):
                        w += ["*(output truncated)*", ""]
        elif t == "system":
            if (d.get("text") or "").strip():
                w += [
                    "<details><summary>system</summary>",
                    "",
                    "```",
                    d["text"].strip(),
                    "```",
                    "</details>",
                    "",
                ]
        elif t == "synthetic":
            if (d.get("text") or "").strip():
                w += [f"*(synthetic: {d['text'].strip()})*", ""]
        else:
            # idle / model-switched / agent-switched / location-switched: skip
            continue

        w += ["---", ""]

    return _write_out(out, sid, sess["title"], w)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not os.path.exists(DB):
        return die(f"db not found: {DB}")

    cmd, rest = argv[0], argv[1:]
    con = connect()
    try:
        if cmd == "schemas":
            return cmd_schemas(con)
        if cmd == "list":
            return cmd_list(con)
        if cmd == "title":
            if not rest:
                return die("usage: title PATTERN")
            return cmd_title(con, " ".join(rest))
        if cmd == "search":
            if not rest:
                return die("usage: search KEYWORD")
            return cmd_search(con, " ".join(rest))
        if cmd in ("info", "tools", "patches"):
            if not rest:
                return die(f"usage: {cmd} SESSION_ID")
            return {"info": cmd_info, "tools": cmd_tools, "patches": cmd_patches}[cmd](
                con, rest[0]
            )
        if cmd == "transcript":
            if not rest:
                return die("usage: transcript SESSION_ID [OUTPUT_PATH]")
            return cmd_transcript(con, rest[0], rest[1] if len(rest) > 1 else None)
        return die(f"unknown command: {cmd}\n\n{__doc__}")
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
