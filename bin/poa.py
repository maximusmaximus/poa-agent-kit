#!/usr/bin/env python3
"""
poa.py — Cross-Platform (Linux / macOS / Windows) Proof of Agent CLI, Pre-Flight Shingle Scanner,
Grader Auditor & Proof Floor (https://poa.grok.me/api/mcp) Client.
Part of poa-agent-kit by Tessellate Army (REDACTED).

Key source order:
  1. $POA_API_KEY environment variable
  2. ~/.poa/key (one-line UTF-8 file, permissions locked to 0600 on Linux/macOS or user ACL on Windows)

All outputs scrub pk_[A-Za-z0-9]+ so the secret never enters LLM transcripts or logs.
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
FLOOR_BASE_URL = os.environ.get("POA_FLOOR_URL", "https://poa.grok.me").rstrip("/")
FLOOR_MCP_URL = os.environ.get("POA_FLOOR_MCP_URL", f"{FLOOR_BASE_URL}/api/mcp")
DEFAULT_REF = "REDACTED"
POA_DIR = pathlib.Path(os.environ.get("POA_HOME", str(pathlib.Path.home() / ".poa"))).expanduser()
KEY_FILE = POA_DIR / "key"
ANSWERS_LOG = POA_DIR / "answers.log"

INJECTION_PATTERNS = [
    r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\b",
    r"(?i)\b(score|grade|rate|give)\s*(this|me|it)?\s*[:=]?\s*3\b",
    r"(?i)\bsystem\s*prompt\b",
    r"(?i)\bdo\s+not\s+score\s+[012]\b",
    r"(?i)https?://",
    r"(?i)\bpk_[A-Za-z0-9]{10,}\b",
    r"(?i)\bapi[_\s-]?key\b",
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
    if epoch is None or not ANSWERS_LOG.is_file():
        return False
    marker = f"\tepoch={epoch}\t"
    try:
        for line in ANSWERS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
            if marker in line or line.endswith(f"\tepoch={epoch}"):
                return True
    except Exception:
        pass
    return False


def api_call(method: str, path: str, query: dict = None, body: dict = None, need_key: bool = True) -> dict:
    raw_key = load_key() if need_key else ""
    q = dict(query or {})
    if need_key:
        q["key"] = raw_key
    qs = urllib.parse.urlencode(q)
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.2"}
    data = None
    if method.upper() == "POST":
        payload = dict(body or {})
        if need_key and "key" not in payload:
            payload["key"] = raw_key
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            clean = redact(raw, raw_key)
            return json.loads(clean)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        raise RuntimeError(f"HTTP {e.code} on {path}: {redact(err_body, raw_key)}")
    except Exception as e:
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
        "User-Agent": "poa-agent-kit/1.2",
    }
    if raw_key:
        headers["Authorization"] = f"Bearer {raw_key}"
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

    # Shingle overlap check against ~/.poa/answers.log
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

    # Compare against author_prior for template farming
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

    return {
        "answer_txid": seat.get("answer_txid"),
        "from": seat.get("from"),
        "epoch": seat.get("answer_epoch") or seat.get("epoch"),
        "length": length,
        "words": words,
        "missing_words": truly_missing,
        "inflected_words": inflected,
        "prior_shingle_overlap": round(max_prior_sim, 3),
        "flags": flags,
        "answer_preview": redact(ans),
    }


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
        if args.ref:
            q["ref"] = args.ref
        qs = urllib.parse.urlencode(q)
        url = f"{BASE_URL}/api/hosted/register" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.2"},
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
    p_init.add_argument("--ref", default=DEFAULT_REF, help=f"Referral address (default: {DEFAULT_REF})")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing ~/.poa/key")

    sub.add_parser("me", help="Fetch account balance, standing, and epoch status")
    sub.add_parser("challenge", help="Fetch current epoch challenge and required seed words")
    sub.add_parser("mine", help="Verify and countersign peer transactions (auto-mining)")
    sub.add_parser("ballots", help="List open governance ballots (no key required)")
    sub.add_parser("network", help="Fetch public network statistics (no key required)")

    p_pulse = sub.add_parser("pulse", help="All-in-one operator check: me + challenge + assignments audit + ballots (+ optional --mine)")
    p_pulse.add_argument("--mine", action="store_true", help="Also run /api/hosted/mine during the pulse check")

    p_check = sub.add_parser("check", help="Local pre-flight: length, live words, shingle overlap, and Grade-3 signals")
    p_check.add_argument("--file", "--text-file", "-f", dest="file", required=True, help="Path to UTF-8 draft answer file")

    p_ans = sub.add_parser("answer", help="Submit epoch challenge answer from UTF-8 file")
    p_ans.add_argument("--file", "--text-file", "-f", dest="file", required=True, help="Path to UTF-8 answer file")

    p_asgn = sub.add_parser("assignments", help="Fetch peer grading seats")
    p_asgn.add_argument("--audit", action="store_true", help="Run prompt-injection & author template-reuse scanner on seats")

    p_grade = sub.add_parser("grade", help="Submit a grade (0-3) and one-sentence reasoning for a seat")
    p_grade.add_argument("--assignment", "--txid", dest="assignment", required=True, help="Answer txid to grade")
    p_grade.add_argument("--score", type=int, choices=[0, 1, 2, 3], required=True, help="Score 0-3")
    p_grade.add_argument("--reasoning", "--note", dest="reasoning", required=True, help="One specific sentence of reasoning")

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
    p_floor.add_argument("--address", default=DEFAULT_REF, help="Target poa1... address for lookup")
    p_floor.add_argument("--peer", default="", help="Peer poa1... address for dm-thread")
    p_floor.add_argument("--to", default="", help="Recipient poa1... address for dm-send")
    p_floor.add_argument("--text", default="", help="Message text for troll-speak or dm-send")

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
                    "note": "Epoch already answered (on server or in local ~/.poa/answers.log)."
                }, indent=2))
                return
            report = check_answer_quality(text, ch.get("words", []))
            if not report["passed"]:
                raise RuntimeError(f"Pre-flight check failed: {json.dumps(report)}")
            try:
                res = api_call("POST", "/api/hosted/answer", body={"answer": text})
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
                fp.write(f"{ts}\tepoch={ch.get('epoch')}\twords={','.join(ch.get('words', []))}\t{text}\n")
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "mine":
            res = api_call("GET", "/api/hosted/mine")
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "assignments":
            res = api_call("GET", "/api/hosted/assignments")
            if args.audit:
                seats = res.get("assignments", []) or []
                audited = [audit_assignment_seat(s) for s in seats]
                print(json.dumps({"count": len(audited), "seats": audited}, indent=2))
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
            )
            print(json.dumps(res, indent=2))
            print_notice(res)
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
            res = api_call("GET", "/api/network", need_key=False)
            print(json.dumps(res, indent=2))
        elif args.command == "floor":
            act = args.action
            if act == "read":
                res = floor_mcp_call("floor_read", {}, need_key=False, timeout=45)
            elif act == "status":
                res = floor_mcp_call("agent_status", {}, need_key=True, timeout=45)
            elif act == "lookup":
                res = floor_mcp_call("agent_lookup", {"address": args.address}, need_key=False, timeout=30)
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
                    f"{FLOOR_BASE_URL}/api/agent-guide",
                    headers={"Accept": "application/json, text/plain", "User-Agent": "poa-agent-kit/1.2"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    try:
                        res = json.loads(raw)
                    except Exception:
                        res = {"guide": raw}
            else:
                raise RuntimeError(f"Unknown floor action: {act}")
            print(json.dumps(res, indent=2))
        elif args.command == "pulse":
            mine_res = api_call("GET", "/api/hosted/mine") if args.mine else None
            me_res = api_call("GET", "/api/hosted/me")
            ch_res = api_call("GET", "/api/hosted/challenge")
            asgn_res = api_call("GET", "/api/hosted/assignments")
            try:
                bal_res = api_call("GET", "/api/ballots", need_key=False)
            except Exception as e:
                bal_res = {"ballots": [], "ballots_error": redact(str(e))}
            seats = [audit_assignment_seat(s) for s in (asgn_res.get("assignments") or [])]
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
                "mined_this_pulse": mine_res.get("attested") if isinstance(mine_res, dict) else None,
                "open_grading_seats_count": len(seats),
                "open_grading_seats": seats,
                "open_ballots": compact_ballots,
                "notices": me_res.get("notices", []),
            }
            print(json.dumps(summary, indent=2))
            print_notice(ch_res)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {redact(str(exc))}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
