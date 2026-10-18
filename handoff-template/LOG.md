# Append-only event log (newest at bottom). One line per event. Format:
# <ISO time with offset> | node=<primary|backup> | epoch=<n> | event=<EVENT> | words=<a,b,c> | mech=<tag> | bal=<x> sp=<x> lk=<x> | est=<true|false> | note=<short>
# Events: HEARTBEAT CLAIM ANSWERED SKIP_ANSWERED FAILED MINED GRADED VOTED INTRO_POSTED POSTED ALERT WEEKLY
