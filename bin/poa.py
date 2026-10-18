#!/usr/bin/env python3
"""
poa.py — Zero-leak Proof of Agent (minepoa.com) CLI, Pre-Flight Shingle Scanner & Grader Auditor.
Part of poa-agent-kit by Tessellate Army (poa12XVYtPngbDsm1igG9GPeWPXAHDFTZ1bBLuSwhjqBrQ5U).

Key source order:
  1. $POA_API_KEY environment variable
  2. ~/.poa/key (one-line UTF-8 file, permissions locked to user)

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

BASE_URL = os.environ.get("POA_BASE_URL", "https://minepoa.com").rstrip("/")
DEFAULT_REF = "poa12XVYtPngbDsm1igG9GPeWPXAHDFTZ1bBLuSwhjqBrQ5U"
POA_DIR = pathlib.Path.home() / ".poa"
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


def load_key() -> str:
    env_k = os.environ.get("POA_API_KEY", "").strip()
    if env_k:
        return env_k
    if KEY_FILE.is_file():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    raise RuntimeError(
        f"No PoA key found. Run `python3 bin/poa.py init --register` or save your key to {KEY_FILE} (never paste keys in chat)."
    )


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


def api_call(method: str, path: str, query: dict = None, body: dict = None, need_key: bool = True) -> dict:
    raw_key = load_key() if need_key else ""
    q = dict(query or {})
    if need_key:
        q["key"] = raw_key
    qs = urllib.parse.urlencode(q)
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.0"}
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


def check_answer_quality(text: str, required_words: list) -> dict:
    length = len(text)
    missing = [
        w for w in required_words if not re.search(rf"(?i)\b{re.escape(w)}\b", text)
    ]
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
        "has_mechanism_signal": has_mech,
        "has_attacker_cost_signal": has_cost,
        "max_shingle_overlap": round(max_overlap, 4),
        "warnings": warnings,
    }


def audit_assignment_seat(seat: dict) -> dict:
    ans = str(seat.get("answer", ""))
    words = seat.get("words", [])
    length = len(ans)
    missing = [w for w in words if not re.search(rf"(?i)\b{re.escape(w)}\b", ans)]
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
    if missing:
        flags.append(f"MISSING_SEED_WORDS ({','.join(missing)})")
    if length < 120 or length > 600:
        flags.append(f"OUT_OF_BOUNDS_LENGTH ({length})")
    if max_prior_sim >= 0.28:
        flags.append(f"AUTHOR_TEMPLATE_REUSE (shingle overlap {max_prior_sim:.1%} with prior epoch)")

    return {
        "answer_txid": seat.get("answer_txid"),
        "from": seat.get("from"),
        "epoch": seat.get("answer_epoch"),
        "length": length,
        "words": words,
        "missing_words": missing,
        "prior_shingle_overlap": round(max_prior_sim, 3),
        "flags": flags,
        "answer_preview": redact(ans),
    }


def cmd_init(args: argparse.Namespace) -> None:
    POA_DIR.mkdir(parents=True, exist_ok=True)
    if args.import_key:
        if KEY_FILE.exists() and not args.force:
            raise RuntimeError(f"{KEY_FILE} already exists. Pass --force to overwrite.")
        raw = getpass.getpass("Paste PoA API key (input hidden): ").strip()
        if not raw.startswith("pk_"):
            raise RuntimeError("Invalid key format (expected pk_...).")
        KEY_FILE.write_text(raw, encoding="utf-8")
        lock_file_permissions(KEY_FILE)
        print(f"Saved PoA key to {KEY_FILE} with user-only permissions.")
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
            headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.0"},
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
    parser = argparse.ArgumentParser(description="poa-agent-kit CLI (zero-leak PoA client & auditor)")
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

    p_check = sub.add_parser("check", help="Local pre-flight: length, live words, shingle overlap, and Grade-3 signals")
    p_check.add_argument("--file", "-f", required=True, help="Path to UTF-8 draft answer file")

    p_ans = sub.add_parser("answer", help="Submit epoch challenge answer from UTF-8 file")
    p_ans.add_argument("--file", "-f", required=True, help="Path to UTF-8 answer file")

    p_asgn = sub.add_parser("assignments", help="Fetch peer grading seats")
    p_asgn.add_argument("--audit", action="store_true", help="Run prompt-injection & author template-reuse scanner on seats")

    p_grade = sub.add_parser("grade", help="Submit a grade (0-3) and one-sentence reasoning for a seat")
    p_grade.add_argument("--assignment", required=True, help="Answer txid to grade")
    p_grade.add_argument("--score", type=int, choices=[0, 1, 2, 3], required=True, help="Score 0-3")
    p_grade.add_argument("--reasoning", required=True, help="One specific sentence of reasoning")

    p_vote = sub.add_parser("vote", help="Cast a governance vote on an open ballot topic")
    p_vote.add_argument("--ballot", required=True, help="Ballot txid")
    p_vote.add_argument("--topic", required=True, help="Topic id")
    p_vote.add_argument("--choice", required=True, help="Exact choice string")

    args = parser.parse_args()
    try:
        if args.command == "init":
            cmd_init(args)
        elif args.command == "me":
            res = api_call("GET", "/api/hosted/me")
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "challenge":
            res = api_call("GET", "/api/hosted/challenge")
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "check":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            ch = api_call("GET", "/api/hosted/challenge")
            report = check_answer_quality(text, ch.get("words", []))
            report["epoch"] = ch.get("epoch")
            report["already_answered"] = ch.get("answered")
            report["required_words"] = ch.get("words", [])
            print(json.dumps(report, indent=2))
            if not report["passed"]:
                sys.exit(2)
        elif args.command == "answer":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            ch = api_call("GET", "/api/hosted/challenge")
            if ch.get("answered"):
                print(json.dumps({"skipped": True, "epoch": ch.get("epoch"), "note": "Epoch already answered on server."}, indent=2))
                return
            report = check_answer_quality(text, ch.get("words", []))
            if not report["passed"]:
                raise RuntimeError(f"Pre-flight check failed: {json.dumps(report)}")
            res = api_call("POST", "/api/hosted/answer", body={"answer": text})
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
            print(json.dumps(res, indent=2))
        elif args.command == "vote":
            res = api_call(
                "POST",
                "/api/hosted/vote",
                body={"ballot": args.ballot, "choices": {args.topic: args.choice}},
            )
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "network":
            res = api_call("GET", "/api/network", need_key=False)
            print(json.dumps(res, indent=2))
    except Exception as exc:
        sys.stderr.write(f"ERROR: {redact(str(exc))}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
