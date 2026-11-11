---
name: ocsearch
description: Query the local OpenCode session database (~/.local/share/opencode/opencode.db) to find context, decisions, transcripts, and actions from past conversations. Supports both the v1 and v2 database schemas (auto-detected). Use when the user references previous work ("like we did before", "last time", "in that other session"), you're stuck and a past session may have solved a similar problem, or the user asks to recall a specific decision, configuration, solution, or recover a session transcript (including pre-compaction context).
---

# ocsearch

Search and export past OpenCode sessions with `ocsearch` (on `PATH`; read-only).

## When to Use

- User references previous work ("like we did before", "last time", "in that other session")
- You're stuck and a past session may have solved a similar problem
- User asks to recall a specific decision, configuration, or solution
- User asks to recover a session transcript (including pre-compaction context)

## Usage

```bash
ocsearch "keyword"                  # grouped search, oldest -> newest
ocsearch ses_<id> [FILE]            # full Markdown transcript (path printed)
ocsearch --list                     # recent sessions
ocsearch --titles "pattern"         # title search
ocsearch --info|--tools|--patches ses_<id>
ocsearch --help
```

Options: `--max-chars N` (default 12000, 0 = unlimited), `--per-session N`,
`--snippet N` (0 = full), `--no-group`, `--color auto|always|never`,
`--include-current`.

## Notes

- Results are grouped by session with a final `Summary` (top 10 by match count); the
  matched term is red on a TTY. The current session is hidden unless `--include-current`.
- Compaction keeps full history; v1/v2 schemas are auto-detected. Empty results on a
  populated database mean the schema moved: `ocsearch --schemas`.
- `-c 0` is for transcripts; on a search it floods context.
- Everything after `--` is literal, so pass phrases as data (`ocsearch -- "<phrase>"`).
