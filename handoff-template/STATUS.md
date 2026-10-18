# PoA Multi-Node Handoff Status (one account, primary + backup nodes)

## Roles (only human operator edits this block)
MINER=primary
GRADER=primary
VOTER=primary
BACKUP=backup
BACKUP_TAKEOVER_AFTER=1h   # backup acts on a duty only if primary's last HEARTBEAT is older than this AND work is undone
CLAIM_TTL=15m

## Node: primary
last_run=
epoch=
answered=
balance= spendable= locked=
established=
note=

## Node: backup
last_run=
epoch=
answered=
balance= spendable= locked=
established=
note=

<!-- Never put the API key, tokens, or full answer text in this repo. -->
