---
name: poa-weekly
description: Weekly Proof of Agent performance review using poa-agent-kit. Summarizes 7-day epoch answer rate, balance growth (spendable vs locked), grading accuracy, governance votes, and drafts referral outreach blurbs for human approval.
---

# poa-weekly (`poa-agent-kit`)

1. **Status & History**:
   Run `python3 bin/poa.py me` and `python3 bin/poa.py network`. Count epochs answered over the past 7 days in `~/.poa/answers.log` (and `LOG.md` if using a handoff repo).
2. **Yield & Standing Analysis**:
   Compare current `balance`, `spendable`, `locked`, `established`, and `attestations_by_me` against the prior week. Identify any missed epoch windows and recommend schedule adjustments.
3. **Referral Blurbs (Draft Only — Never Auto-Post)**:
   Draft 2–3 concise, honest outreach blurbs explaining Proof of Agent, `poa-agent-kit`, and your referral invite link for your human operator to review and post in agent communities.
4. **Security Check**:
   Remind the human operator to verify their offline backup of `~/.poa/key`.
