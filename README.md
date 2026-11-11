# ocsearch

Read-only CLI for searching your local OpenCode session history from a terminal.

It is a **thin wrapper**, not a re-implementation:

- `ocsearch` — a small bash launcher that parses flags
- `search_session.py` — the actual queries against `~/.local/share/opencode/opencode.db`

There is no index, no daemon and no database copy: every query reads the live SQLite
file through a read-only connection (`mode=ro`), so the store cannot be modified or
locked by this tool.

> Verified against the OpenCode v2 schema (`session_v2` / `session_message`) in
> November 2026. That database is an internal implementation detail, not a public
> API, so some release will break this. When it does, the patch is small: the queries
> are plain SQL and schema detection is a single function (`detect()`).

## Install

```bash
git clone https://github.com/demirciAhmet/ocsearch && cd ocsearch
install -m755 ocsearch search_session.py ~/.local/bin/
```

Both files must end up in the same directory: the launcher resolves its real path
through the symlink and runs `search_session.py` next to itself. Requirements are
`python3` and its bundled `sqlite3` module.

If your database lives somewhere else (macOS, WSL, a second install), point at it with
`OPENCODE_DB=/path/to/opencode.db`.

## Usage

Bare input is always a search phrase; commands live behind long flags, so a keyword
can never be mistaken for a subcommand.

```bash
ocsearch "keyword"                # search (grouped by session)
ocsearch ses_<id> [FILE]          # full Markdown transcript (path printed)
ocsearch --list                   # recent sessions
ocsearch --titles "pattern"       # search session titles
ocsearch --search "keyword"       # explicit search
ocsearch --info|--tools|--patches ses_<id>
ocsearch --schemas                # which schemas exist
ocsearch --help
```

Everything after `--` is literal, so pass phrases as data:

```bash
ocsearch -- "--list"              # search for the text "--list"
```

| Option | Effect |
| --- | --- |
| `--max-chars N`, `-c N` | output length budget in characters (default 12000, `0` = unlimited) |
| `--per-session N` | snippets per session in grouped search (default 3) |
| `--snippet N`, `-s N` | snippet width (default 180 grouped / 300 flat, `0` = full) |
| `--no-group` | flat result list instead of one block per session |
| `--color auto\|always\|never` | ANSI output (auto = TTY only) |
| `--out FILE`, `-o FILE` | transcript output path (default `$TMPDIR/ocsearch/`) |
| `--include-current` | include `$OPENCODE_SESSION_ID` in results |

Environment overrides: `OPENCODE_DB` (database path), `OPENCODE_SESSION_ID` (hidden
from discovery), `SS_MAX_CHARS`, `SS_PER_SESSION`, `SS_GROUP`, `SS_SNIPPET`,
`SS_COLOR`, `SS_OUT_DIR`, `SS_EXCLUDE`.

## Example output

### Searching old sessions

Bare input is a keyword, and it lands in every stored message, including what tools
printed:

```text
$ ocsearch "connection timed out"
▌ 2026-11-03 14:12:08  Rate limiter retry backoff  ses_e9f2a41c0ffeQw1mXbT7LpRnD  [5 matches]
    2026-11-03 14:09:51  [assistant]
        …pkg/api/client.go:88: dial tcp 10.0.0.4:5432: connect: connection timed out ⏎ retry 1/3 in 250ms…
    2026-11-03 14:10:44  [assistant]
        …the pool is capped at 2 and the scraper opens 40 goroutines, so most of them queue…
    2026-11-03 14:11:30  [user]
        …that timeout is the pool, not the network. bump MaxOpenConns to 25…
    +2 more matches
▌ 2026-11-05 09:31:12  Nightly scrape dies at 3am  ses_e7c1b90daffe8kLm2QpR4sT6v  [2 matches]
    2026-11-05 09:30:47  [assistant]
        …third run in a row, log ends at pgx pool exhausted, no stack trace…
    +1 more match

────────────────────────────────────────────────────────────────
Summary — "connection timed out": 2 sessions, 7 matches
Top 10 sessions by match count:
  2026-11-03 14:12:08  Rate limiter retry backoff  ses_e9f2a41c0ffeQw1mXbT7LpRnD  (5)
  2026-11-05 09:31:12  Nightly scrape dies at 3am  ses_e7c1b90daffe8kLm2QpR4sT6v  (2)
```

The first snippet came out of a bash result the assistant was reading. It is findable
because the database stores the message as it was and this tool does not pick what to
keep. Sessions print oldest first; the summary ranks them by match count. Long result
sets stop at `--max-chars` and say so.

### Pulling the transcript

The hit tells you which session. The id gives you the whole thing:

```text
$ ocsearch ses_e9f2a41c0ffeQw1mXbT7LpRnD
wrote /tmp/ocsearch/ses_e9f2a41c0ffeQw1mXbT7LpRnD.md: 48120 bytes, 1337 lines
```

```markdown
# Rate limiter retry backoff

- **Session**: `ses_e9f2a41c0ffeQw1mXbT7LpRnD`
- **Schema**: `v2`
- **Directory**: `/srv/scraper`
- **Created**: 2026-11-03 14:12:08
- **Last updated**: 2026-11-03 14:41:55

## USER — 2026-11-03 14:12:08

tests keep dying on the db connection, can you look

---

## ASSISTANT — 2026-11-03 14:12:31 - claude-sonnet-4.5

<details><summary>reasoning</summary>
…
```

Tool calls, reasoning and file patches come along with the text, so `rg` over the
exported file works on the whole session. `--patches ses_<id>` is the short version
when you only want the files it touched.

## Use it from an agent

The output is size-capped so an agent can read it without eating its context. Drop
`skills/ocsearch/SKILL.md` into your skills directory and OpenCode will reach for it
when you mention earlier sessions:

```bash
mkdir -p ~/.config/opencode/skills/ocsearch
cp skills/ocsearch/SKILL.md ~/.config/opencode/skills/ocsearch/
```

Then "what did we decide about the retry backoff last week" hits the database instead
of you re-explaining the project. The skill file only documents the CLI, so it stays
valid as long as the commands do.

## Why no index

- **Nothing is filtered before it is searchable.** Indexing tools pick what to store —
  usually dropping tool output, shell logs and reasoning. That is exactly where error
  traces live, so the thing you remember is missing. This searches the raw message
  blobs, so matches inside tool output are found.
- **No second copy to keep in sync.** The OpenCode schema already changed once
  (v1 → v2); a sidecar index would have to be rebuilt each time it does.
- **No account, no API key, no embedder.**

The cost is a full `LIKE` scan per query: roughly 1.4–1.9 s on a 1.5 GB database.
That is the trade this tool deliberately makes.

## Schema notes

- **v1** (legacy): `session` / `message` / `part`. **v2** (current): `session_v2` /
  `session_message`. v2 is preferred when both exist because migration copies every
  v1 session into it; v1-only sessions still fall back to the v1 tables.
- **v2 dropped the `patch` part, and the migration did not carry it over.** Patch
  records therefore stay authoritative in v1 whenever they exist — including for
  migrated sessions — so `--patches` reads v1 first and falls back to `edit`/`write`
  tool calls found in v2 messages.
- **Compaction does not delete stored history.** `--transcript` marks the compaction
  summary and keeps the full pre-compaction transcript below it.

## License

MIT
