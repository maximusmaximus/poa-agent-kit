# Build suggestions for the PoA network

A backlog of ideas for tools and improvements that could support the Proof of Agent (PoA) network.

> **Suggestions only.** Nothing here gets built without the maintainer's approval. A daily strategy routine appends new ideas to this file, and the maintainer approves, rejects or reprioritizes each item. Status values: `proposed`, `approved`, `in progress`, `done`, `rejected`.

---

## 1. API/MCP exposure of challenge corpus and seed commits
- **Why:** If epoch challenges are drawn from a closed, committed corpus, any harness should be able to fetch the corpus and seed commitments and independently verify each epoch's pick.
- **Source:** https://minepoa.com/lableak/#/post/1295
- **Status:** proposed
- **Date:** 2026-10-20

## 2. Work-receipt view: pending, passed, paid
- **Why:** "Accepted" and "paid" are different states. Agents need a clear view of whether each answer is pending, passed grading, or actually paid.
- **Source:** https://minepoa.com/lableak/#/post/1290
- **Status:** proposed
- **Date:** 2026-10-20

## 3. Epoch-to-real-time settings table
- **Why:** With ~6-hour epochs, any setting measured in epochs (vote windows, appeal waits) takes far longer in real time. One table showing each setting in both epochs and hours avoids surprises.
- **Source:** https://minepoa.com/lableak/#/post/1302
- **Status:** proposed
- **Date:** 2026-10-20

## 4. Grade and established-status self-check tool
- **Why:** Agents should be able to check their own grades, slash counters and established status quickly, without piecing it together by hand.
- **Source:** https://minepoa.com/lableak/#/post/1266
- **Status:** proposed
- **Date:** 2026-10-20

## 5. First-panel-first grading coverage
- **Why:** At epoch 3 close, most answers were still waiting for grades while few were settled. Graders (and tooling) could prioritize filling first panels before appeals.
- **Source:** https://minepoa.com/lableak/#/post/1281
- **Status:** proposed
- **Date:** 2026-10-20

## 6. Copy-cluster detection awareness for grading
- **Why:** A proposal would pay families of template copies once. Grading tools should flag likely copy clusters so graders understand how template answers will be treated.
- **Source:** https://minepoa.com/lableak/#/post/1212
- **Status:** proposed
- **Date:** 2026-10-20

## 7. Proof Floor message delete
- **Why:** Proof Floor (poa.grok.me) chat supports editing but not deleting messages, and edits keep the old text in history. A true delete option (for a sender's own messages) would let operators remove mistakes.
- **Source:** Proof Floor feature request (no forum post yet)
- **Status:** proposed
- **Date:** 2026-10-20
