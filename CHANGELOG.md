# Changelog

## 2026-10-26
- **`close-check` checks every answer against every close** (`bin/poa.py`): new `own_answers_all`, `answer_ledger` and `established_from_closes`. Each close row now has `settled_here` / `passed_here` (answers settle oldest-first, so an epoch-1 answer can settle at epoch 6 while that epoch's own answer is still pending), and the result has `answers_all` plus settled/passed/failed/pending totals. A passed or paid answer no longer counts as established, in `close-check` or in the `/api/epochs` edge fallback for `me`/`pulse`: only `established_after` (or `/api/hosted/me`) does, and `passed_not_established` flags the gap. Tests: `tests/test_close_check.py` (mocked responses only).

## 2026-10-25
- **Hourly mine lock in `safe_mine()`** (`bin/poa.py`): at most one `/api/hosted/mine` per local clock hour per machine. A second call in the same hour returns `kind: already-mined-this-hour` with zero network calls. Lock files live in `$POA_HOME/mine-locks/` (default `~/.poa`) and are named by hour only. Opt out with `POA_MINE_HOURLY_LOCK=0`. Tests: `tests/test_mine_lock.py` (mocked responses only).
- `close-pull`: close-synced keyless assignments pull with seconds-since-close log (#52).
