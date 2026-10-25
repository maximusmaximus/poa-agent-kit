# Changelog

## 2026-10-25
- **Hourly mine lock in `safe_mine()`** (`bin/poa.py`): at most one `/api/hosted/mine` per local clock hour per machine. A second call in the same hour returns `kind: already-mined-this-hour` with zero network calls. Lock files live in `$POA_HOME/mine-locks/` (default `~/.poa`) and are named by hour only. Opt out with `POA_MINE_HOURLY_LOCK=0`. Tests: `tests/test_mine_lock.py` (mocked responses only).
- `close-pull`: close-synced keyless assignments pull with seconds-since-close log (#52).
