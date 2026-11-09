# ocsearch

Read-only CLI for searching your local OpenCode session history from a terminal.

It is a **thin wrapper**, not a re-implementation:

- `ocsearch` — a small bash launcher that parses flags
- `search_session.py` — the actual queries against `~/.local/share/opencode/opencode.db`

There is no index, no daemon and no database copy: every query reads the live SQLite
file through a read-only connection (`mode=ro`), so the store cannot be modified or
locked by this tool.

> **Unmaintained.** Published as-is. Verified against the OpenCode v2 schema
> (`session_v2` / `session_message`) in November 2026. OpenCode's database is an
> internal implementation detail, not a public API — it can change in any release.
> That is the intended shelf life of this repo.

## Install

```bash
git clone https://github.com/demirciAhmet/ocsearch && cd ocsearch
install -m755 ocsearch search_session.py ~/.local/bin/
```

Both files must end up in the same directory: the launcher resolves its real path
through the symlink and runs `search_session.py` next to itself. Requirements are
`python3` and its bundled `sqlite3` module.

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
