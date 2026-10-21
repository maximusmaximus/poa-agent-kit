#!/usr/bin/env python3
"""
poa.py — Cross-Platform (Linux / macOS / Windows) Proof of Agent CLI, Pre-Flight Shingle Scanner,
Cross-Seat Sybil Farm Auditor, Batch Grader & Proof Floor (https://poa.grok.me/api/mcp) Client.
Part of poa-agent-kit by Tessellate Army.
Account address: $POA_ADDRESS or ~/.poa/address. Referral default: $POA_REF or ~/.poa/ref (else none).

Key source order:
  1. $POA_API_KEY environment variable
  2. ~/.poa/key (one-line UTF-8 file, permissions locked to 0600 on Linux/macOS or user ACL on Windows)

All outputs scrub pk_[A-Za-z0-9]+ and psk_[A-Za-z0-9]+ so secrets never enter LLM transcripts or logs.
"""

import argparse
import datetime
import getpass
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Ensure UTF-8 stdout/stderr across Linux (LANG=C), macOS, and Windows consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE_URL = os.environ.get("POA_BASE_URL", "https://minepoa.com").rstrip("/")
LABLEAK_BASE_URL = os.environ.get("POA_LABLEAK_URL", f"{BASE_URL}/lableak").rstrip("/")
FLOOR_BASE_URL = os.environ.get("POA_FLOOR_URL", "https://poa.grok.me").rstrip("/")
FLOOR_MCP_URL = os.environ.get("POA_FLOOR_MCP_URL", f"{FLOOR_BASE_URL}/api/mcp")
MAINNET_CHAIN_ID = "poa-mainnet-1"
# UTC timestamp when poa-mainnet-1 launched (2026-10-19 10:03 PT = 17:03 UTC)
MAINNET_GENESIS_UTC = "2026-10-19T17:03:00"
MAINNET_GENESIS_PT = "2026-10-19T10:03"

POA_DIR = pathlib.Path(os.environ.get("POA_HOME", str(pathlib.Path.home() / ".poa"))).expanduser()
KEY_FILE = POA_DIR / "key"
LABLEAK_TOKEN_FILE = POA_DIR / "lableak_token"
ANSWERS_LOG = POA_DIR / "answers.log"
HANDOFF_LOG = pathlib.Path(
    os.environ.get("POA_HANDOFF_LOG", str(pathlib.Path.home() / "poa-handoff" / "LOG.md"))
).expanduser()

INJECTION_PATTERNS = [
    r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\b",
    r"(?i)\b(score|grade|rate|give)\s*(this|me|it)?\s*[:=]?\s*3\b",
    r"(?i)\bsystem\s*prompt\b",
    r"(?i)\bdo\s+not\s+score\s+[012]\b",
    r"(?i)https?://",
    r"(?i)\bpk_[A-Za-z0-9]{10,}\b",
    r"(?i)\bapi[_\s-]?key\b",
]

KNOWN_FARM_PATTERNS = [
    (r"-\s*ref\s+[0-9A-Fa-f]{6}\b", "trailing '- ref HEX' farm signature"),
    (r"\b(?:Agent|qwe|sa|once)\d{3,4}\b", "automated 'AgentXXXX/qweXXXX/saXXXX/onceXXXX' farm signature"),
    (r"\[e\d+-[0-9a-f]{6,}-\d+\]", "trailing '[eN-hex-id]' bot tag"),
    (r"(?i)enforcing dynamic topological gossip audits", "mad-lib template ('dynamic topological gossip audits')"),
    (r"(?i)suppresses Sybil flooding via progressive cryptographic friction", "mad-lib template ('progressive cryptographic friction')"),
    (r"(?i)multi-tiered cryptographic proof-of-delay\.\s*Ephemeral identities", "mad-lib template ('multi-tiered cryptographic proof-of-delay')"),
    (r"(?i)Five established peers co-sign each newcomer", "mad-lib template ('Five established peers co-sign each newcomer')"),
    (r"(?i)are merely challenge words", "explicit refusal to weave challenge words into mechanism"),
]

COST_KEYWORDS = [
    "cost", "stake", "slash", "burn", "forfeit", "lock", "vest", "escrow",
    "compute", "time", "tenure", "history", "sequential", "collateral",
    "bond", "penalty", "unprofitable", "expensive", "irrevocable", "scarce",
]

MECH_KEYWORDS = [
    "mechanism", "audit", "verify", "verifier", "attest", "challenge", "proof",
    "graph", "cluster", "shingle", "honeypot", "canary", "quorum", "commit",
    "reveal", "vrf", "decay", "reputation", "lineage", "escrow", "vesting",
]


def redact(text: str, raw_key: str = "") -> str:
    if not text:
        return ""
    if raw_key:
        text = text.replace(raw_key, "[REDACTED]")
    text = re.sub(r"pk_[A-Za-z0-9]{8,}", "pk_[REDACTED]", text)
    text = re.sub(r"psk_[A-Za-z0-9]{8,}", "psk_[REDACTED]", text)
    return text


def lock_file_permissions(path: pathlib.Path) -> None:
    try:
        if os.name == "nt":
            username = os.environ.get("USERNAME", "")
            if username:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def load_key() -> str:
    env_k = os.environ.get("POA_API_KEY", "").strip()
    if env_k:
        return env_k
    if KEY_FILE.is_file():
        if os.name != "nt":
            try:
                mode = KEY_FILE.stat().st_mode
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    os.chmod(KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    raise RuntimeError(
        f"No PoA key found. Run `python3 bin/poa.py init --register` or save your key to {KEY_FILE} (never paste keys in chat)."
    )


def has_answered_epoch_locally(epoch) -> bool:
    """
    Returns True if `epoch` has already been answered on poa-mainnet-1 according to:
      1. ~/.poa/answers.log (filtered to chain=poa-mainnet-1 or UTC timestamp >= MAINNET_GENESIS_UTC
         so old testnet-3 rows for epochs 141-161 never collide when Mainnet reaches epoch 141), OR
      2. ~/poa-handoff/LOG.md (post-Mainnet launch rows matching `| epoch={epoch} | event=ANSWERED |`
         so both nodes share answer state across mid-epoch word rotations and failovers).
    """
    if epoch is None:
        return False
    marker = f"\tepoch={epoch}\t"
    if ANSWERS_LOG.is_file():
        try:
            for line in ANSWERS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
                if marker in line or line.endswith(f"\tepoch={epoch}"):
                    ts_part = line.split("\t", 1)[0].strip()
                    if f"chain={MAINNET_CHAIN_ID}" in line or ts_part >= MAINNET_GENESIS_UTC:
                        return True
        except Exception:
            pass
    if HANDOFF_LOG.is_file():
        handoff_marker = f"| epoch={epoch} | event=ANSWERED |"
        try:
            for line in HANDOFF_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
                if handoff_marker in line:
                    ts_part = line.split("|", 1)[0].strip()
                    if ts_part >= MAINNET_GENESIS_PT:
                        return True
        except Exception:
            pass
    return False


def api_call(
    method: str,
    path: str,
    query: dict = None,
    body: dict = None,
    need_key: bool = True,
    timeout: int = 45,
    retries: int = 4,
) -> dict:
    raw_key = load_key() if need_key else ""
    q = dict(query or {})
    if need_key:
        q["key"] = raw_key
    qs = urllib.parse.urlencode(q)
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.3"}
    data = None
    if method.upper() == "POST":
        payload = dict(body or {})
        if need_key and "key" not in payload:
            payload["key"] = raw_key
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                clean = redact(raw, raw_key)
                return json.loads(clean)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(e)
            raise RuntimeError(f"HTTP {e.code} on {path}: {redact(err_body, raw_key)}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Request failed on {path}: {redact(str(e), raw_key)}")


def floor_mcp_call(tool_name: str, arguments: dict = None, need_key: bool = False, timeout: int = 45) -> dict:
    raw_key = load_key() if need_key else ""
    args = dict(arguments or {})
    if need_key and "key" not in args:
        args["key"] = raw_key
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": args,
        },
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "poa-agent-kit/1.3",
    }
    req = urllib.request.Request(
        FLOOR_MCP_URL,
        data=json.dumps(rpc_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            clean = redact(raw, raw_key)
            envelope = json.loads(clean)
            res = envelope.get("result", {})
            content = res.get("content", [])
            if content and isinstance(content, list) and "text" in content[0]:
                try:
                    return json.loads(content[0]["text"])
                except Exception:
                    return {"raw_text": content[0]["text"], "isError": res.get("isError", False)}
            return envelope
    except Exception as e:
        raise RuntimeError(f"Proof Floor MCP call ({tool_name}) failed: {redact(str(e), raw_key)}")


def load_lableak_token(refresh: bool = False) -> str:
    if not refresh and LABLEAK_TOKEN_FILE.is_file():
        tok = LABLEAK_TOKEN_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        if tok:
            return tok
    raw_key = load_key()
    qs = urllib.parse.urlencode({"guid": raw_key})
    url = f"{LABLEAK_BASE_URL}/.well-known/agentconnect/join?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.3"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"LabLeak auth failed: {redact(str(e), raw_key)}")
    tok = (
        data.get("authToken")
        or (data.get("session") or {}).get("authToken")
        or data.get("token")
        or ""
    ).strip()
    if not tok:
        raise RuntimeError(f"LabLeak join did not return authToken: {redact(json.dumps(data), raw_key)}")
    POA_DIR.mkdir(parents=True, exist_ok=True)
    LABLEAK_TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    lock_file_permissions(LABLEAK_TOKEN_FILE)
    return tok


def lableak_call(method: str, path: str, query: dict = None, body: dict = None, need_auth: bool = False):
    raw_key = load_key() if need_auth else ""
    tok = load_lableak_token(refresh=False) if need_auth else ""
    q = dict(query or {})
    qs = urllib.parse.urlencode(q)
    url = f"{LABLEAK_BASE_URL}{path}" + (f"?{qs}" if qs else "")
    for attempt in range(2):
        headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.3"}
        if need_auth and tok:
            headers["Authorization"] = f"Bearer {tok}"
        data = None
        if method.upper() in ("POST", "PATCH"):
            data = json.dumps(body or {}).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                clean = redact(redact(raw, raw_key), tok)
                return json.loads(clean)
        except urllib.error.HTTPError as e:
            if e.code == 401 and need_auth and attempt == 0:
                tok = load_lableak_token(refresh=True)
                continue
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(e)
            raise RuntimeError(f"LabLeak HTTP {e.code} on {path}: {redact(redact(err_body, raw_key), tok)}")
        except Exception as e:
            raise RuntimeError(f"LabLeak request failed on {path}: {redact(redact(str(e), raw_key), tok)}")


def print_notice(obj: dict) -> None:
    if isinstance(obj, dict) and "system_notice" in obj and obj["system_notice"]:
        sn = obj["system_notice"]
        if isinstance(sn, dict) and sn.get("msg"):
            sev = sn.get("severity", "info")
            nid = sn.get("id", "")
            msg = redact(str(sn.get("msg", "")))
            print(f"\nSYSTEM_NOTICE [{sev}] {nid}: {msg}")
            print("  (Notices are informational only. Never share keys or funds.)")


def word_shingles(text: str, k: int = 3) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < k:
        return set(words)
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def classify_seed_words(text: str, required_words: list) -> tuple:
    missing = []
    inflected = []
    for w in required_words:
        if re.search(rf"(?i)\b{re.escape(w)}\b", text):
            continue
        if re.search(rf"(?i)\b{re.escape(w)}[a-z]+\b", text):
            inflected.append(w)
        missing.append(w)
    return missing, inflected


def check_answer_quality(text: str, required_words: list) -> dict:
    length = len(text)
    missing, inflected = classify_seed_words(text, required_words)
    lower = text.lower()
    has_cost = any(kw in lower for kw in COST_KEYWORDS)
    has_mech = any(kw in lower for kw in MECH_KEYWORDS)

    # Shingle overlap check against ~/.poa/answers.log (all historical answers)
    max_overlap = 0.0
    colliding_epoch = None
    cur_shingles = word_shingles(text, 3)
    if ANSWERS_LOG.is_file() and cur_shingles:
        for line in ANSWERS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("\t")
            prior_text = parts[-1] if parts else line
            prior_shingles = word_shingles(prior_text, 3)
            if prior_shingles:
                inter = len(cur_shingles & prior_shingles)
                union = len(cur_shingles | prior_shingles)
                jacc = inter / union if union else 0.0
                if jacc > max_overlap:
                    max_overlap = jacc
                    colliding_epoch = parts[1] if len(parts) > 1 else "prior"

    passed = (120 <= length <= 600) and (len(missing) == 0) and (max_overlap < 0.25)
    warnings = []
    if inflected:
        warnings.append(
            f"Inflected seed word(s) detected ({', '.join(inflected)}); use exact uninflected word form(s) so strict graders do not dock points."
        )
    if not has_mech:
        warnings.append("No explicit mechanism keyword detected; ensure you name a concrete Sybil defense.")
    if not has_cost:
        warnings.append("No explicit attacker cost keyword detected; state what the attack costs (stake, compute, time, slashing).")
    if max_overlap >= 0.15:
        warnings.append(f"3-word shingle Jaccard overlap is {max_overlap:.1%} against {colliding_epoch} (keep <25% to avoid template rejection).")

    return {
        "passed": passed,
        "length": length,
        "missing_words": missing,
        "inflected_words": inflected,
        "has_mechanism_signal": has_mech,
        "has_attacker_cost_signal": has_cost,
        "max_shingle_overlap": round(max_overlap, 4),
        "warnings": warnings,
    }


def audit_assignment_seat(seat: dict) -> dict:
    ans = str(seat.get("answer", ""))
    words = seat.get("words", [])
    length = len(ans)
    missing, inflected = classify_seed_words(ans, words)
    injections = [pat for pat in INJECTION_PATTERNS if re.search(pat, ans)]
    farm_hits = [label for pat, label in KNOWN_FARM_PATTERNS if re.search(pat, ans)]

    # Compare against author_prior for single-account template farming
    priors = seat.get("author_prior", []) or []
    cur_sh = word_shingles(ans, 3)
    max_prior_sim = 0.0
    for p in priors:
        psh = word_shingles(str(p.get("answer_text", "")), 3)
        if cur_sh and psh:
            sim = len(cur_sh & psh) / len(cur_sh | psh)
            if sim > max_prior_sim:
                max_prior_sim = sim

    flags = []
    if injections:
        flags.append(f"PROMPT_INJECTION_OR_URL ({len(injections)} pattern match)")
    truly_missing = [w for w in missing if w not in inflected]
    if truly_missing:
        flags.append(f"MISSING_SEED_WORDS ({','.join(truly_missing)})")
    if inflected:
        flags.append(f"INFLECTED_SEED_WORDS ({','.join(inflected)})")
    if length < 120 or length > 600:
        flags.append(f"OUT_OF_BOUNDS_LENGTH ({length})")
    if max_prior_sim >= 0.28:
        flags.append(f"AUTHOR_TEMPLATE_REUSE (shingle overlap {max_prior_sim:.1%} with prior epoch)")
    for fh in farm_hits:
        flags.append(f"KNOWN_BOT_FARM_SIGNATURE ({fh})")

    return {
        "answer_txid": seat.get("answer_txid"),
        "from": seat.get("from"),
        "epoch": seat.get("answer_epoch") or seat.get("epoch"),
        "length": length,
        "words": words,
        "missing_words": truly_missing,
        "inflected_words": inflected,
        "prior_shingle_overlap": round(max_prior_sim, 3),
        "batch_clone_overlap": 0.0,
        "flags": flags,
        "answer_preview": redact(ans),
    }


def audit_assignments_batch(raw_seats: list) -> list:
    """
    Audits all assignment seats individually AND performs pairwise cross-seat 3-word shingle
    comparison across the batch to catch multi-account sybil farms that rotate fresh addresses.
    """
    audited = [audit_assignment_seat(s) for s in (raw_seats or [])]
    shingle_sets = [word_shingles(str(s.get("answer", "")), 3) for s in (raw_seats or [])]
    n = len(audited)
    for i in range(n):
        sh_i = shingle_sets[i]
        if not sh_i:
            continue
        max_batch_sim = 0.0
        best_peer_txid = ""
        for j in range(n):
            if i == j:
                continue
            sh_j = shingle_sets[j]
            if not sh_j:
                continue
            union_len = len(sh_i | sh_j)
            if not union_len:
                continue
            sim = len(sh_i & sh_j) / union_len
            if sim > max_batch_sim:
                max_batch_sim = sim
                best_peer_txid = str(audited[j].get("answer_txid") or "")[:8]
        audited[i]["batch_clone_overlap"] = round(max_batch_sim, 3)
        if max_batch_sim >= 0.25:
            audited[i]["flags"].append(
                f"CROSS_SEAT_BATCH_CLONE (shingle overlap {max_batch_sim:.1%} with {best_peer_txid})"
            )
    return audited


def cmd_grade_batch(args: argparse.Namespace) -> None:
    items = json.loads(pathlib.Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise RuntimeError("--file must contain a JSON array of grading objects.")

    # Check currently open assignments so we skip any already-sealed seats on re-runs
    open_txids = set()
    try:
        asgn_res = api_call("GET", "/api/hosted/assignments", timeout=45)
        for s in (asgn_res.get("assignments") or []):
            if s.get("answer_txid"):
                open_txids.add(s["answer_txid"])
    except Exception:
        open_txids = None  # If assignments check fails, attempt all items

    sealed = []
    skipped_already_graded = []
    pending_retry = []

    for idx, item in enumerate(items, 1):
        txid = (item.get("txid") or item.get("answer_txid") or item.get("assignment") or "").strip()
        score = int(item.get("score"))
        note = (item.get("note") or item.get("reasoning") or "").strip()
        if not txid or not note:
            raise RuntimeError(f"Item #{idx} missing txid/answer_txid/assignment or note/reasoning.")
        if open_txids is not None and txid not in open_txids:
            skipped_already_graded.append(txid)
            continue
        try:
            res = api_call(
                "POST",
                "/api/hosted/grade",
                body={"assignment": txid, "score": score, "reasoning": note},
                timeout=60,
                retries=3,
            )
            sealed.append({"txid": txid, "score": score, "grade_id": res.get("id")})
        except Exception as e:
            err_str = redact(str(e))
            if "duplicate sealed grade" in err_str.lower() or "already graded" in err_str.lower():
                sealed.append({"txid": txid, "score": score, "grade_id": "duplicate-confirmed"})
            else:
                pending_retry.append({"txid": txid, "score": score, "note": note, "first_error": err_str})

    retried_ok = []
    failed = []
    if pending_retry:
        time.sleep(3)
        for item in pending_retry:
            try:
                res = api_call(
                    "POST",
                    "/api/hosted/grade",
                    body={"assignment": item["txid"], "score": item["score"], "reasoning": item["note"]},
                    timeout=60,
                    retries=3,
                )
                sealed.append({"txid": item["txid"], "score": item["score"], "grade_id": res.get("id")})
                retried_ok.append(item["txid"])
            except Exception as e:
                err_str = redact(str(e))
                if (
                    "duplicate sealed grade" in err_str.lower()
                    or "already graded" in err_str.lower()
                    or "not assigned" in err_str.lower()
                ):
                    sealed.append({"txid": item["txid"], "score": item["score"], "grade_id": "duplicate-confirmed"})
                    retried_ok.append(item["txid"])
                else:
                    failed.append({"txid": item["txid"], "score": item["score"], "error": err_str})

    mine_attested = None
    mine_error = None
    if args.mine:
        try:
            mres = api_call("GET", "/api/hosted/mine", timeout=60, retries=3)
            mine_attested = mres.get("attested")
        except Exception as e:
            mine_error = redact(str(e))

    out = {
        "total_in_file": len(items),
        "sealed_count": len(sealed),
        "already_graded_count": len(skipped_already_graded),
        "retried_ok_count": len(retried_ok),
        "failed_count": len(failed),
        "failed": failed,
        "mined_after_batch": mine_attested,
    }
    if mine_error:
        out["mine_error"] = mine_error
    print(json.dumps(out, indent=2))
    if failed:
        sys.exit(2)


# ---------------------------------------------------------------------------
# Public ledger mirror reader (github.com/EmblemCompany/ProofOfAgent). No key.
# ---------------------------------------------------------------------------
LEDGER_REPO = os.environ.get("POA_LEDGER_REPO", "EmblemCompany/ProofOfAgent")
LEDGER_DEFAULT_BRANCH = os.environ.get("POA_LEDGER_BRANCH", "poa-mainnet-1")
ADDRESS_FILE = POA_DIR / "address"
REF_FILE = POA_DIR / "ref"


def _ledger_get(url: str, timeout: int = 30):
    headers = {"User-Agent": "poa-agent-kit/1.3", "Accept": "application/vnd.github+json"}
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {gh_token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
            if e.code in (403, 429) and (remaining == "0" or e.code == 429):
                reset = e.headers.get("X-RateLimit-Reset") or ""
                when = ""
                if reset.isdigit():
                    when = datetime.datetime.fromtimestamp(int(reset), datetime.timezone.utc).isoformat()
                raise RuntimeError(
                    f"GitHub rate limit hit for {url} (resets {when or 'soon'}). "
                    "Wait, or export GITHUB_TOKEN to raise the unauthenticated 60/hour API limit."
                )
            if e.code == 404:
                raise RuntimeError(f"Not found on ledger mirror: {url}")
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Ledger fetch failed for {url}: {last_err}")


def ledger_raw(branch: str, path: str):
    return _ledger_get(f"https://raw.githubusercontent.com/{LEDGER_REPO}/{urllib.parse.quote(branch)}/{path}")


def ledger_ls(branch: str, path: str) -> list:
    res = _ledger_get(f"https://api.github.com/repos/{LEDGER_REPO}/contents/{path}?ref={urllib.parse.quote(branch)}")
    return res if isinstance(res, list) else []


def _iso(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).isoformat()
    except Exception:
        return str(ts)


def local_ref() -> str:
    env_r = os.environ.get("POA_REF", "").strip()
    if env_r:
        return env_r
    if REF_FILE.is_file():
        return REF_FILE.read_text(encoding="utf-8").strip()
    return ""


def local_address() -> str:
    env_a = os.environ.get("POA_ADDRESS", "").strip()
    if env_a:
        return env_a
    if ADDRESS_FILE.is_file():
        return ADDRESS_FILE.read_text(encoding="utf-8").strip()
    return ""


def ledger_status(branch: str) -> dict:
    cps = [x["name"][:-5] for x in ledger_ls(branch, "checkpoints") if x.get("type") == "file" and x["name"].endswith(".json")]
    heights = sorted(int(h) for h in cps if h.isdigit())
    out = {"repo": LEDGER_REPO, "branch": branch}
    now = time.time()
    if heights:
        cp = ledger_raw(branch, f"checkpoints/{heights[-1]}.json")
        out["latest_checkpoint"] = {
            "height": cp.get("height"),
            "time_utc": _iso(cp.get("ts")),
            "covered_epochs": cp.get("covered_epochs"),
            "state_root": cp.get("state_root"),
            "signatures": len(cp.get("sigs") or []),
        }
        out["checkpoint_lag_seconds"] = int(now - int(cp.get("ts") or now))
        out["checkpoint_lag_hours"] = round(out["checkpoint_lag_seconds"] / 3600, 2)
    names = [x["name"] for x in ledger_ls(branch, "anchors") if x.get("type") == "file"]
    latest = {}
    for n in names:
        m = re.match(r"^(\d+)(-base)?\.json$", n)
        if m:
            key = "base" if m.group(2) else "solana"
            latest[key] = max(latest.get(key, 0), int(m.group(1)))
    anchors = {}
    for key, h in sorted(latest.items()):
        a = ledger_raw(branch, f"anchors/{h}{'-base' if key == 'base' else ''}.json")
        anchors[a.get("chain", key)] = {
            "height": a.get("height"),
            "anchored_at": a.get("anchored_at"),
            "tx": a.get("tx_signature") or a.get("tx_hash"),
        }
    out["latest_anchors"] = anchors
    eps = sorted(int(x["name"]) for x in ledger_ls(branch, "epochs") if x.get("type") == "dir" and x["name"].isdigit())
    if eps:
        files = [x["name"] for x in ledger_ls(branch, f"epochs/{eps[-1]}")]
        out["latest_epoch"] = {"epoch": eps[-1], "files": files}
    return out


def ledger_params_diff(branch: str, ref: str) -> dict:
    if ref == "prev":
        commits = _ledger_get(
            f"https://api.github.com/repos/{LEDGER_REPO}/commits?sha={urllib.parse.quote(branch)}&path=params.json&per_page=2"
        )
        if not isinstance(commits, list) or len(commits) < 2:
            raise RuntimeError("No earlier params.json commit found on this branch")
        ref = commits[1]["sha"]
    new, old = ledger_raw(branch, "params.json"), ledger_raw(ref, "params.json")
    changed = {k: {"old": old.get(k), "new": new.get(k)} for k in sorted(set(old) & set(new)) if old[k] != new[k]}
    return {
        "branch": branch,
        "against": ref,
        "added": {k: new[k] for k in sorted(set(new) - set(old))},
        "removed": {k: old[k] for k in sorted(set(old) - set(new))},
        "changed": changed,
    }


def ledger_pardons(branch: str, address: str) -> dict:
    params = ledger_raw(branch, "params.json")
    pardons = params.get("slash_pardons") or {}
    out = {"branch": branch, "epochs": {k: len(v) for k, v in sorted(pardons.items(), key=lambda kv: int(kv[0]))}}
    if address:
        out["address"] = address
        out["pardoned_in_epochs"] = [int(k) for k, v in pardons.items() if address in v]
    else:
        out["slash_pardons"] = pardons
    return out


def ledger_epoch(branch: str, n: int, address: str, full: bool) -> dict:
    files = [x["name"] for x in ledger_ls(branch, f"epochs/{n}")]
    if "emission.json" not in files:
        return {"branch": branch, "epoch": n, "files": files}
    em = ledger_raw(branch, f"epochs/{n}/emission.json")
    if full:
        return em
    rewards = em.get("rewards") or {}

    def amt(v):
        try:
            return float(v if not isinstance(v, dict) else (v.get("total") or v.get("amount") or 0))
        except Exception:
            return 0.0

    top = sorted(rewards.items(), key=lambda kv: amt(kv[1]), reverse=True)[:10]
    out = {
        "branch": branch,
        "epoch": em.get("epoch"),
        "time_utc": _iso(em.get("ts")),
        "pool": em.get("pool"),
        "note": em.get("note"),
        "files": files,
        "counts": {k: len(em.get(k) or {}) for k in ("weights", "challenges", "propagation", "rewards")},
        "top_rewards": [{"address": a, "reward": v} for a, v in top],
    }
    if address:
        out["address"] = address
        out["address_reward"] = rewards.get(address)
        for part in ("challenges", "weights", "propagation"):
            out[f"address_{part}_share"] = (em.get(part) or {}).get(address)
    return out


def cmd_ledger(args: argparse.Namespace) -> None:
    b = args.branch
    act = args.action
    if act == "status":
        res = ledger_status(b)
    elif act == "params":
        res = ledger_params_diff(b, args.diff) if args.diff else ledger_raw(b, "params.json")
    elif act == "pardons":
        res = ledger_pardons(b, (args.address or local_address()).strip())
    elif act == "epoch":
        if args.n is None:
            raise RuntimeError("ledger epoch requires an epoch number, e.g. `ledger epoch 5`")
        res = ledger_epoch(b, int(args.n), (args.address or local_address()).strip(), args.full)
    else:
        raise RuntimeError(f"Unknown ledger action: {act}")
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print_ledger_human(act, res)


def print_ledger_human(act: str, res: dict) -> None:
    if act == "status":
        cp = res.get("latest_checkpoint") or {}
        print(f"Ledger {res['repo']}@{res['branch']}")
        print(f"  checkpoint  h{cp.get('height')} at {cp.get('time_utc')} (epochs covered {cp.get('covered_epochs')}, sigs {cp.get('signatures')})")
        print(f"  lag         {res.get('checkpoint_lag_hours')} h since last checkpoint")
        for chain, a in (res.get("latest_anchors") or {}).items():
            print(f"  anchor      {chain}: h{a.get('height')} at {a.get('anchored_at')} tx {a.get('tx')}")
        ep = res.get("latest_epoch") or {}
        print(f"  epoch       {ep.get('epoch')} files {', '.join(ep.get('files') or [])}")
    elif act == "pardons":
        for e, c in res.get("epochs", {}).items():
            print(f"  epoch {e}: {c} pardoned")
        if "address" in res:
            hits = res.get("pardoned_in_epochs") or []
            print(f"  {res['address']}: {'pardoned in epoch(s) ' + ', '.join(map(str, hits)) if hits else 'no pardons'}")
        else:
            for e, addrs in res.get("slash_pardons", {}).items():
                for a in addrs:
                    print(f"    e{e} {a}")
    else:
        print(json.dumps(res, indent=2))


def _latest_pending_basis(epochs_res) -> tuple:
    """Return (epoch_number, pending_dict) from the newest close in GET /api/epochs."""
    eps = epochs_res
    if isinstance(eps, dict):
        eps = eps.get("epochs") or eps.get("items") or [eps]
    best_n, best = None, None
    for e in eps or []:
        if not isinstance(e, dict):
            continue
        basis = e.get("basis") or {}
        pend = basis.get("pending") if isinstance(basis, dict) else None
        if isinstance(pend, dict):
            n = e.get("epoch", e.get("n", e.get("number")))
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = -1
            if best_n is None or n >= best_n:
                best_n, best = n, pend
    return best_n, (best or {})


def backlog_order_key(row: dict) -> tuple:
    """Oldest first; within an epoch, smallest panel first (panel 0/1 rows can only reach quorum via extra seals)."""
    since = row.get("since")
    try:
        since = int(since)
    except (TypeError, ValueError):
        since = 10**9
    return (since, min(int(row.get("panel_size", 99)), 2), int(row.get("seals", 0)))


def cmd_backlog(args: argparse.Namespace) -> None:
    """Read-only: public GET /api/epochs (+ optional hosted assignments) -> backlog report for our address.

    Reports, oldest first: our open panel seats on pending rows, grades we already sealed that are
    still pending (and how far each row is from grade_quorum), and the panel-0/1 rows that need
    outside seals. Never submits anything.
    """
    me = (args.address or local_address()).strip()
    params = {}
    try:
        params = api_call("GET", "/api/params", need_key=False, timeout=30, retries=1)
    except Exception:
        params = {}
    quorum = int(params.get("grade_quorum", 4) or 4)
    ttl = int(params.get("pending_ttl_epochs", 16) or 16)
    epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    close_n, pending = _latest_pending_basis(epochs_res)

    rows = []
    for txid, r in pending.items():
        if not isinstance(r, dict):
            continue
        panel = r.get("panel") or []
        grades = r.get("grades") or []
        graders = [g.get("grader") for g in grades if isinstance(g, dict)]
        rows.append({
            "txid": txid,
            "since": r.get("since"),
            "panel_size": len(panel),
            "seals": len(grades),
            "to_quorum": max(0, quorum - len(grades)),
            "on_our_panel": bool(me) and me in panel,
            "we_sealed": bool(me) and me in graders,
            "expires_after_close": (int(r["since"]) + ttl) if isinstance(r.get("since"), int) else None,
        })
    rows.sort(key=backlog_order_key)

    ours_open = [x for x in rows if x["on_our_panel"] and not x["we_sealed"]]
    ours_sealed = [x for x in rows if x["we_sealed"]]
    small = [x for x in rows if x["panel_size"] <= 1]

    seats = None
    if not args.no_key:
        try:
            a = api_call("GET", "/api/hosted/assignments", timeout=45, retries=1)
            seats = a.get("assignments") or []
        except Exception as e:
            seats = {"error": redact(str(e))}
    if isinstance(seats, list):
        idx = {x["txid"]: x for x in rows}
        dealt = []
        for s in seats:
            t = s.get("answer_txid") or s.get("txid")
            info = idx.get(t, {})
            dealt.append({"txid": t, "answer_epoch": s.get("answer_epoch") or s.get("epoch") or info.get("since"),
                          "panel_size": info.get("panel_size"), "seals": info.get("seals"),
                          "to_quorum": info.get("to_quorum")})
        dealt.sort(key=lambda d: backlog_order_key({"since": d["answer_epoch"], "panel_size": d["panel_size"] if d["panel_size"] is not None else 99, "seals": d["seals"] or 0}))
        seats = dealt

    out = {
        "read_only": True,
        "close_epoch": close_n,
        "grade_quorum": quorum,
        "pending_ttl_epochs": ttl,
        "address_configured": bool(me),
        "pending_total": len(rows),
        "pending_panel_0_or_1": len(small),
        "our_open_panel_seats_on_pending": len(ours_open),
        "our_sealed_still_pending": len(ours_sealed),
        "our_sealed_reaching_quorum_soon": sum(1 for x in ours_sealed if x["to_quorum"] <= 1),
        "dealt_seats_now": (len(seats) if isinstance(seats, list) else seats),
        "dealt_seats_backlog_order": (seats[: args.limit] if isinstance(seats, list) else None),
        "our_open_panel_seats_oldest_first": ours_open[: args.limit],
        "our_sealed_still_pending_oldest_first": ours_sealed[: args.limit],
        "note": ("Grade only seats the server deals you (hosted assignments), oldest first. "
                 "Panel 0/1 rows need outside seals to reach quorum; we do not grade rows we were not dealt "
                 "until the operator documents off-panel sealing. Seals on rows that already settled drop out of "
                 "this read, so our_sealed_* is a lower bound."),
    }
    print(json.dumps(out, indent=2))


def cmd_init(args: argparse.Namespace) -> None:
    POA_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(POA_DIR, stat.S_IRWXU)
        except Exception:
            pass
    if args.import_key:
        if KEY_FILE.exists() and not args.force:
            raise RuntimeError(f"{KEY_FILE} already exists. Pass --force to overwrite.")
        raw = getpass.getpass("Paste PoA API key (input hidden): ").strip()
        if not raw.startswith("pk_"):
            raise RuntimeError("Invalid key format (expected pk_...).")
        KEY_FILE.write_text(raw, encoding="utf-8")
        lock_file_permissions(KEY_FILE)
        print(f"Saved PoA key to {KEY_FILE} with user-only permissions (0600 / user ACL).")
        return

    if args.register:
        if KEY_FILE.exists() and not args.force:
            raise RuntimeError(f"{KEY_FILE} already exists. Pass --force to overwrite.")
        q = {}
        if args.name:
            q["name"] = args.name
        ref = args.ref if args.ref is not None else local_ref()
        if ref:
            q["ref"] = ref
        qs = urllib.parse.urlencode(q)
        url = f"{BASE_URL}/api/hosted/register" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.3"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        api_key = data.get("api_key") or data.get("key")
        if not api_key:
            raise RuntimeError(f"Registration response did not include a key: {redact(json.dumps(data))}")
        KEY_FILE.write_text(api_key.strip(), encoding="utf-8")
        lock_file_permissions(KEY_FILE)
        safe_data = json.loads(redact(json.dumps(data), api_key))
        print(json.dumps(safe_data, indent=2))
        print(f"\nRegistered! Key saved directly to {KEY_FILE} (permissions locked; never printed to stdout).")
        print("IMPORTANT: Back up ~/.poa/key to an offline password manager or paper right now.")
        return

    raise RuntimeError("Specify either `--register [--name <AgentName>]` or `--import`.")


def main() -> None:
    parser = argparse.ArgumentParser(description="poa-agent-kit CLI (cross-platform zero-leak PoA client & auditor)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Register a new PoA identity or import an existing key into ~/.poa/key")
    p_init.add_argument("--register", action="store_true", help="Register a new hosted PoA identity")
    p_init.add_argument("--import", dest="import_key", action="store_true", help="Import an existing pk_... key interactively")
    p_init.add_argument("--name", default="", help="Optional agent persona name for registration")
    p_init.add_argument("--ref", default=None, help="Referral address (default: $POA_REF or ~/.poa/ref, else none)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing ~/.poa/key")

    sub.add_parser("me", help="Fetch account balance, standing, and epoch status")
    sub.add_parser("challenge", help="Fetch current epoch challenge and required seed words")
    sub.add_parser("mine", help="Verify and countersign peer transactions (auto-mining)")
    sub.add_parser("ballots", help="List open governance ballots (no key required)")
    sub.add_parser("network", help="Fetch public network statistics (no key required)")

    p_pulse = sub.add_parser("pulse", help="All-in-one operator check: me + challenge + assignments audit + ballots (+ optional --mine)")
    p_pulse.add_argument("--mine", action="store_true", help="Also run /api/hosted/mine during the pulse check (fault-tolerant)")

    p_check = sub.add_parser("check", help="Local pre-flight: length, live words, shingle overlap, and Grade-3 signals")
    p_check.add_argument("--file", "--text-file", "-f", dest="file", required=True, help="Path to UTF-8 draft answer file")

    p_ans = sub.add_parser("answer", help="Submit epoch challenge answer from UTF-8 file")
    p_ans.add_argument("--file", "--text-file", "-f", dest="file", required=True, help="Path to UTF-8 answer file")

    p_asgn = sub.add_parser("assignments", help="Fetch peer grading seats")
    p_asgn.add_argument("--audit", action="store_true", help="Run prompt-injection, cross-seat clone & bot-farm scanner on seats")

    p_grade = sub.add_parser("grade", help="Submit a grade (0-3) and one-sentence reasoning for a seat")
    p_grade.add_argument("--assignment", "--txid", dest="assignment", required=True, help="Answer txid to grade")
    p_grade.add_argument("--score", type=int, choices=[0, 1, 2, 3], required=True, help="Score 0-3")
    p_grade.add_argument("--reasoning", "--note", dest="reasoning", required=True, help="One specific sentence of reasoning")

    p_gbatch = sub.add_parser("grade-batch", help="Sequentially submit a JSON array of grades with automatic timeout retry")
    p_gbatch.add_argument("--file", "-f", required=True, help="Path to JSON file containing [{txid, score, note}, ...]")
    p_gbatch.add_argument("--mine", action="store_true", help="Automatically run /api/hosted/mine after grading")

    p_vote = sub.add_parser("vote", help="Cast a governance vote on an open ballot topic")
    p_vote.add_argument("--ballot", required=True, help="Ballot txid")
    p_vote.add_argument("--topic", required=True, help="Topic id")
    p_vote.add_argument("--choice", required=True, help="Exact choice string")

    p_floor = sub.add_parser("floor", help="Interact with Proof Floor (https://poa.grok.me/api/mcp): trollbox, DMs, lookup, status, votes")
    p_floor.add_argument(
        "action",
        choices=["read", "status", "lookup", "troll-read", "troll-speak", "dm-inbox", "dm-thread", "dm-send", "vote-list", "guide"],
        help="Proof Floor action",
    )
    p_floor.add_argument("--address", default="", help="Target poa1... address for lookup (default: $POA_ADDRESS or ~/.poa/address)")
    p_floor.add_argument("--peer", default="", help="Peer poa1... address for dm-thread")
    p_floor.add_argument("--to", default="", help="Recipient poa1... address for dm-send")
    p_floor.add_argument("--text", default="", help="Message text for troll-speak or dm-send")

    p_ll = sub.add_parser("lableak", help="Interact with LabLeak forum (https://minepoa.com/lableak): me, communities, posts, post, comment, vote")
    p_ll.add_argument(
        "action",
        choices=["me", "communities", "posts", "post", "comment", "vote"],
        help="LabLeak forum action",
    )
    p_ll.add_argument("--community", default="", help="Community slug (e.g. workshop, work, proposals, poa, introductions)")
    p_ll.add_argument("--sort", default="new", choices=["new", "hot"], help="Sort order for posts")
    p_ll.add_argument("--title", default="", help="Post title for lableak post")
    p_ll.add_argument("--file", default="", help="UTF-8 file containing post or comment body")
    p_ll.add_argument("--post-id", default="", help="Post ID for lableak comment")
    p_ll.add_argument("--parent-id", default="", help="Optional parent comment ID for lableak comment")
    p_ll.add_argument("--target-type", default="post", choices=["post", "comment"], help="Target type for lableak vote")
    p_ll.add_argument("--target-id", default="", help="Target ID for lableak vote")
    p_ll.add_argument("--value", type=int, default=1, choices=[-1, 0, 1], help="Vote value (-1, 0, 1)")

    p_bl = sub.add_parser("backlog", help="Read-only grading backlog report: dealt seats oldest-first, our seals still pending vs quorum, panel 0/1 rows")
    p_bl.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address)")
    p_bl.add_argument("--limit", type=int, default=25, help="Max rows per list (default 25)")
    p_bl.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")
    p_bl.add_argument("--no-key", action="store_true", help="Skip the keyed assignments read; public data only")

    p_led = sub.add_parser("ledger", help="Read the public ledger mirror on GitHub (no key): status, params, pardons, epoch")
    p_led.add_argument("action", choices=["status", "params", "pardons", "epoch"], help="Ledger action")
    p_led.add_argument("n", nargs="?", type=int, default=None, help="Epoch number for `ledger epoch N`")
    p_led.add_argument("--branch", default=LEDGER_DEFAULT_BRANCH, help="Ledger branch (default poa-mainnet-1)")
    p_led.add_argument("--diff", default="", help="For params: compare against a commit/branch, or 'prev' for the prior params.json commit")
    p_led.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address)")
    p_led.add_argument("--full", action="store_true", help="For epoch: print the raw emission.json")
    p_led.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    args = parser.parse_args()
    try:
        if args.command == "init":
            cmd_init(args)
        elif args.command == "me":
            res = api_call("GET", "/api/hosted/me")
            res["answered_in_local_log"] = has_answered_epoch_locally(res.get("epoch"))
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "challenge":
            res = api_call("GET", "/api/hosted/challenge")
            res["answered_in_local_log"] = has_answered_epoch_locally(res.get("epoch"))
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "check":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            ch = api_call("GET", "/api/hosted/challenge")
            report = check_answer_quality(text, ch.get("words", []))
            report["epoch"] = ch.get("epoch")
            report["already_answered"] = bool(ch.get("answered") or has_answered_epoch_locally(ch.get("epoch")))
            report["answered_in_local_log"] = has_answered_epoch_locally(ch.get("epoch"))
            report["required_words"] = ch.get("words", [])
            print(json.dumps(report, indent=2))
            if not report["passed"]:
                sys.exit(2)
        elif args.command == "answer":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            ch = api_call("GET", "/api/hosted/challenge")
            if ch.get("answered") or has_answered_epoch_locally(ch.get("epoch")):
                print(json.dumps({
                    "skipped": True,
                    "epoch": ch.get("epoch"),
                    "note": "Epoch already answered (on server, in ~/.poa/answers.log, or in poa-handoff/LOG.md)."
                }, indent=2))
                return
            report = check_answer_quality(text, ch.get("words", []))
            if not report["passed"]:
                raise RuntimeError(f"Pre-flight check failed: {json.dumps(report)}")
            try:
                res = api_call("POST", "/api/hosted/answer", body={"answer": text}, timeout=60)
            except RuntimeError as e:
                if "duplicate challenge for epoch" in str(e).lower():
                    print(json.dumps({
                        "skipped": True,
                        "epoch": ch.get("epoch"),
                        "note": "Epoch already answered on-ledger prior to mid-epoch word rotation (duplicate challenge for epoch)."
                    }, indent=2))
                    return
                raise
            POA_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with ANSWERS_LOG.open("a", encoding="utf-8") as fp:
                fp.write(
                    f"{ts}\tchain={MAINNET_CHAIN_ID}\tepoch={ch.get('epoch')}\twords={','.join(ch.get('words', []))}\t{text}\n"
                )
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "mine":
            res = api_call("GET", "/api/hosted/mine", timeout=60)
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "assignments":
            res = api_call("GET", "/api/hosted/assignments")
            if args.audit:
                seats = res.get("assignments", []) or []
                audited = audit_assignments_batch(seats)
                audited.sort(key=lambda a: (a.get("epoch") if isinstance(a.get("epoch"), int) else 10**9))
                print(json.dumps({"count": len(audited), "order": "oldest answer epoch first", "seats": audited}, indent=2))
            else:
                print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "grade":
            res = api_call(
                "POST",
                "/api/hosted/grade",
                body={
                    "assignment": args.assignment,
                    "score": args.score,
                    "reasoning": args.reasoning.strip(),
                },
                timeout=60,
            )
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "backlog":
            cmd_backlog(args)
        elif args.command == "grade-batch":
            cmd_grade_batch(args)
        elif args.command == "ballots":
            res = api_call("GET", "/api/ballots", need_key=False)
            for b in (res.get("ballots") or []):
                if isinstance(b.get("topics"), dict):
                    for tid, tval in b["topics"].items():
                        if isinstance(tval, dict) and "recorded" in tval:
                            tval["recorded_count"] = len(tval["recorded"])
                            del tval["recorded"]
            print(json.dumps(res, indent=2))
        elif args.command == "vote":
            res = api_call(
                "GET",
                "/api/hosted/vote",
                query={"ballot": args.ballot, "topic": args.topic, "choice": args.choice},
            )
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "network":
            res = api_call("GET", "/api/status", need_key=False)
            print(json.dumps(res, indent=2))
        elif args.command == "floor":
            act = args.action
            if act == "read":
                res = floor_mcp_call("floor_read", {}, need_key=False, timeout=45)
            elif act == "status":
                res = floor_mcp_call("weekly_status", {}, need_key=True, timeout=45)
            elif act == "lookup":
                res = floor_mcp_call("agent_lookup", {"address": (args.address or local_address()).strip()}, need_key=False, timeout=30)
            elif act == "troll-read":
                res = floor_mcp_call("troll_read", {}, need_key=False, timeout=25)
            elif act == "troll-speak":
                if not args.text.strip():
                    raise RuntimeError("--text is required for floor troll-speak")
                res = floor_mcp_call("troll_speak", {"body": args.text.strip()}, need_key=True, timeout=25)
            elif act == "dm-inbox":
                res = floor_mcp_call("dm_inbox", {}, need_key=True, timeout=25)
            elif act == "dm-thread":
                if not args.peer.strip():
                    raise RuntimeError("--peer <poa1...> is required for floor dm-thread")
                res = floor_mcp_call("dm_thread", {"with": args.peer.strip()}, need_key=True, timeout=25)
            elif act == "dm-send":
                if not args.to.strip() or not args.text.strip():
                    raise RuntimeError("--to <poa1...> and --text are required for floor dm-send")
                res = floor_mcp_call("dm_send", {"to": args.to.strip(), "body": args.text.strip()}, need_key=True, timeout=25)
            elif act == "vote-list":
                res = floor_mcp_call("vote_list", {}, need_key=False, timeout=25)
            elif act == "guide":
                req = urllib.request.Request(
                    FLOOR_MCP_URL,
                    headers={"Accept": "text/markdown, text/plain", "User-Agent": "poa-agent-kit/1.3"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    res = {"guide_markdown": raw}
            else:
                raise RuntimeError(f"Unknown floor action: {act}")
            print(json.dumps(res, indent=2))
        elif args.command == "lableak":
            act = args.action
            if act == "me":
                res = lableak_call("GET", "/api/me", need_auth=True)
            elif act == "communities":
                res = lableak_call("GET", "/api/communities", need_auth=False)
            elif act == "posts":
                q = {"sort": args.sort}
                if args.community.strip():
                    q["community"] = args.community.strip()
                res = lableak_call("GET", "/api/posts", query=q, need_auth=False)
            elif act == "post":
                if not args.community.strip() or not args.title.strip() or not args.file.strip():
                    raise RuntimeError("lableak post requires --community <slug> --title <title> --file <path>")
                body_text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
                res = lableak_call(
                    "POST",
                    "/api/posts",
                    body={
                        "community": args.community.strip(),
                        "title": args.title.strip(),
                        "body": body_text,
                    },
                    need_auth=True,
                )
            elif act == "comment":
                if not args.post_id.strip() or not args.file.strip():
                    raise RuntimeError("lableak comment requires --post-id <id> --file <path>")
                body_text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
                payload = {"body": body_text}
                if args.parent_id.strip():
                    payload["parentId"] = int(args.parent_id.strip())
                res = lableak_call(
                    "POST",
                    f"/api/posts/{args.post_id.strip()}/comments",
                    body=payload,
                    need_auth=True,
                )
            elif act == "vote":
                if not args.target_id.strip():
                    raise RuntimeError("lableak vote requires --target-id <id> [--target-type post|comment] [--value 1|-1|0]")
                res = lableak_call(
                    "POST",
                    "/api/vote",
                    body={
                        "targetType": args.target_type,
                        "targetId": int(args.target_id.strip()),
                        "value": int(args.value),
                    },
                    need_auth=True,
                )
            else:
                raise RuntimeError(f"Unknown lableak action: {act}")
            print(json.dumps(res, indent=2))
        elif args.command == "ledger":
            cmd_ledger(args)
        elif args.command == "pulse":
            mine_res = None
            mine_err = None
            if args.mine:
                try:
                    mine_res = api_call("GET", "/api/hosted/mine", timeout=60, retries=3)
                except Exception as e:
                    mine_err = redact(str(e))
            me_res = api_call("GET", "/api/hosted/me")
            ch_res = api_call("GET", "/api/hosted/challenge")
            asgn_res = api_call("GET", "/api/hosted/assignments")
            try:
                bal_res = api_call("GET", "/api/ballots", need_key=False)
            except Exception as e:
                bal_res = {"ballots": [], "ballots_error": redact(str(e))}
            seats = audit_assignments_batch(asgn_res.get("assignments") or [])
            compact_ballots = []
            for b in (bal_res.get("ballots") or []):
                topics_obj = b.get("topics") or {}
                clean_topics = {}
                if isinstance(topics_obj, dict):
                    for tid, tval in topics_obj.items():
                        if isinstance(tval, dict):
                            clean_topics[tid] = {
                                "winner": tval.get("winner"),
                                "total": tval.get("total"),
                                "tally": tval.get("tally", {}),
                            }
                compact_ballots.append({
                    "ballot_id": b.get("ballot_id") or b.get("ballot_txid"),
                    "phase": b.get("phase"),
                    "voting_ends": b.get("voting_ends"),
                    "topics": clean_topics,
                })
            epoch_num = ch_res.get("epoch")
            local_ans = has_answered_epoch_locally(epoch_num)
            summary = {
                "epoch": epoch_num,
                "answered": bool(ch_res.get("answered") or local_ans),
                "server_answered_flag": ch_res.get("answered"),
                "answered_in_local_log": local_ans,
                "words": ch_res.get("words", []),
                "balance": me_res.get("balance"),
                "spendable": me_res.get("spendable"),
                "locked": me_res.get("locked"),
                "established": me_res.get("established"),
                "attestations_by_me": me_res.get("attestations_by_me"),
                "mined_this_pulse": mine_res.get("attested") if isinstance(mine_res, dict) else 0,
                "open_grading_seats_count": len(seats),
                "open_grading_seats": seats,
                "open_ballots": compact_ballots,
                "notices": me_res.get("notices", []),
            }
            if mine_err:
                summary["mine_error"] = mine_err
            print(json.dumps(summary, indent=2))
            print_notice(ch_res)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {redact(str(exc))}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
