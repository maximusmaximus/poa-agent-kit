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
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
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

KIT_VERSION = "1.8.3"
USER_AGENT = f"poa-agent-kit/{KIT_VERSION}"
BASE_URL = os.environ.get("POA_BASE_URL", "https://minepoa.com").rstrip("/")
LABLEAK_BASE_URL = os.environ.get("POA_LABLEAK_URL", f"{BASE_URL}/lableak").rstrip("/")
FLOOR_BASE_URL = os.environ.get("POA_FLOOR_URL", "https://poa.grok.me").rstrip("/")
FLOOR_MCP_URL = os.environ.get("POA_FLOOR_MCP_URL", f"{FLOOR_BASE_URL}/api/mcp")
MAINNET_CHAIN_ID = "poa-mainnet-1"
# UTC/PT timestamp of the Segment 2 genesis cut (2026-10-24-genesis-cut: 2026-10-24 14:00 PT = 21:00 UTC)
MAINNET_GENESIS_UTC = "2026-10-24T21:00:00"
MAINNET_GENESIS_PT = "2026-10-24T14:00"

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
        enc = urllib.parse.quote(raw_key, safe="")
        if enc != raw_key:
            text = text.replace(enc, "[REDACTED]")
        enc_plus = urllib.parse.quote_plus(raw_key)
        if enc_plus not in (raw_key, enc):
            text = text.replace(enc_plus, "[REDACTED]")
    text = re.sub(r"(?i)([?&](?:key|api_key|guid|token)=)[^&#\s\"']+", r"\1[REDACTED]", text)
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
                    if ts_part >= MAINNET_GENESIS_UTC:
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


def is_challenge_answered(ch: dict, epoch=None) -> bool:
    """True when the server challenge payload (answered, superseded, answered_txid) or local log shows the epoch is answered."""
    if not isinstance(ch, dict):
        ch = {}
    ep = epoch if epoch is not None else ch.get("epoch")
    return bool(ch.get("answered") or ch.get("superseded") or ch.get("answered_txid") or has_answered_epoch_locally(ep))


class PoaHTTPError(RuntimeError):
    """HTTP failure with a coarse kind so callers can tell server outages from key problems.

    kind: "server"  (429/5xx, Cloudflare 502/520-524: retry later, key is fine)
          "timeout" (no response in time: the write MAY have landed, read back before retrying)
          "network" (DNS/TLS/connection refused)
          "auth"    (401/403: key rejected or rotated; retrying will not help)
          "client"  (other 4xx: bad request, duplicate, not found)
          "halted"  (503 operator maintenance / API hardening halt)
          "redirect-refused" (any 3xx on a keyed request: never followed, Location never contacted)
    """

    def __init__(self, kind: str, message: str, status: int = None, retry_after: int = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


SERVER_STATUSES = (429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
HTTP_CONNECT_TIMEOUT = float(os.environ.get("POA_HTTP_TIMEOUT", "25"))
MCP_URL = os.environ.get("POA_MCP_URL", f"{BASE_URL}/mcp")


# ---------------------------------------------------------------------------
# Transport: HTTP/2 via httpx when installed (pip install "httpx[http2]"), else urllib.
# Errors are normalised to urllib types so _classify() treats both paths the same.
# ---------------------------------------------------------------------------
try:  # optional dependency
    import httpx as _httpx  # type: ignore
    try:
        import h2  # noqa: F401  type: ignore
        _HTTP2 = True
    except Exception:
        _HTTP2 = False
except Exception:
    _httpx = None
    _HTTP2 = False


def http_transport() -> str:
    if os.environ.get("POA_NO_HTTPX"):
        return "urllib"
    if _httpx is None:
        return "urllib"
    return "httpx-h2" if _HTTP2 else "httpx-h1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError(3xx) instead of following


def http_request(method: str, url: str, data: bytes = None, headers: dict = None, timeout: float = 30,
                 follow_redirects: bool = True) -> bytes:
    """One HTTP request; returns body bytes or raises urllib.error.HTTPError / TimeoutError / OSError.
    With follow_redirects=False any 3xx is raised as HTTPError(3xx) on both transports."""
    headers = dict(headers or {})
    if http_transport() != "urllib":
        try:
            with _httpx.Client(http2=_HTTP2, timeout=timeout, follow_redirects=follow_redirects) as c:
                r = c.request(method.upper(), url, content=data, headers=headers)
        except _httpx.TimeoutException as e:
            raise TimeoutError(f"timed out ({type(e).__name__})")
        except _httpx.HTTPError as e:
            raise OSError(f"{type(e).__name__}: {redact(str(e))}") from None
        if r.status_code >= 400 or (not follow_redirects and 300 <= r.status_code < 400):
            raise urllib.error.HTTPError(redact(url), r.status_code, r.reason_phrase or "", r.headers, io.BytesIO(r.content))
        return r.content
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    if not follow_redirects:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            return resp.read()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def mcp_fallback_enabled() -> bool:
    return os.environ.get("POA_MCP_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def _classify(exc: Exception, path: str, raw_key: str) -> PoaHTTPError:
    if isinstance(exc, PoaHTTPError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        body = redact(body, raw_key)
        low_body = body.lower()[:2000]
        if 300 <= exc.code < 400:
            # Never echo the Location (it may carry the query string) and never follow it.
            return PoaHTTPError("redirect-refused",
                                f"HTTP {exc.code} (redirect-refused) on {path}: refused to follow a redirect on a keyed request",
                                exc.code)
        if exc.code in (401, 403):
            kind = "auth"
        elif exc.code == 503 and ("temporarily halted" in low_body or "api hardening" in low_body):
            kind = "halted"
        elif exc.code in SERVER_STATUSES or "cloudflare" in low_body:
            kind = "server"
        else:
            kind = "client"
        retry_after = None
        try:
            hdr_ra = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
            if hdr_ra and str(hdr_ra).strip().isdigit():
                retry_after = int(str(hdr_ra).strip())
            elif body.strip().startswith("{"):
                parsed_err = json.loads(body)
                if isinstance(parsed_err, dict):
                    for ra_key in ("retry_after", "retry_after_seconds"):
                        if isinstance(parsed_err.get(ra_key), (int, float)):
                            retry_after = int(parsed_err[ra_key])
                            break
        except Exception:
            retry_after = None
        return PoaHTTPError(kind, f"HTTP {exc.code} ({kind}) on {path}: {body[:600]}", exc.code, retry_after=retry_after)
    msg = redact(str(exc), raw_key)
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or "timed out" in msg.lower() or isinstance(reason, TimeoutError):
        return PoaHTTPError("timeout", f"Timeout on {path}: {msg}")
    return PoaHTTPError("network", f"Request failed (network) on {path}: {msg}")


def _backoff(attempt: int) -> None:
    # 2s, 5s, 11s, 23s ... capped at 30s, with a little jitter
    delay = min(30.0, 2.0 * (2.2 ** attempt))
    time.sleep(delay + (time.time() % 1.0))


# The key travels ONLY in the X-Api-Key header (and, for POSTs, the JSON body). There is no ?key=
# route list and no URL fallback of any kind: a route that refuses the header form (502, timeout,
# 401, 3xx) is reported as an error / unknown, never retried with the key in the URL.
# The seat listing uses the trailing-slash path, which accepts the header (Oct 28, 2026 live check:
# the slash form answered with the header while the bare path hung).
HOSTED_ASSIGNMENTS_PATH = "/api/hosted/assignments/"


def key_mode_for(path: str) -> str:
    """Always 'header'. Kept for callers/tests; the key never goes in a URL."""
    return "header"


def api_call(
    method: str,
    path: str,
    query: dict = None,
    body: dict = None,
    need_key: bool = True,
    timeout: int = None,
    retries: int = None,
) -> dict:
    """Call the REST API.

    Reads (GET without side effects) retry server/timeout/network errors with backoff.
    Writes (POST, or GET to /api/hosted/mine|vote) are sent ONCE by default: a timeout on a
    write may still have landed, so callers must read state back before resending (see
    submit_answer_safely / submit_grade_safely). 401/403 never retry: the key is the problem.
    """
    raw_key = load_key() if need_key else ""
    is_write = method.upper() == "POST" or path.rstrip("/").endswith(("/hosted/mine", "/hosted/vote"))
    if retries is None:
        retries = 1 if is_write else 4
    if timeout is None:
        timeout = 60 if is_write else HTTP_CONNECT_TIMEOUT
    is_post = method.upper() == "POST"
    # Key placement: X-Api-Key header only (POSTs also carry it in the JSON body). Never in a URL,
    # no fallback after a 401 or a 5xx. Keyed requests never follow redirects.
    mode = "header" if need_key else "none"
    data = None
    if is_post:
        payload = dict(body or {})
        if need_key and "key" not in payload:
            payload["key"] = raw_key
        data = json.dumps(payload).encode("utf-8")

    def _build(m: str):
        q = dict(query or {})
        if need_key:
            for k in [k for k in q if str(k).lower() in ("key", "api_key", "apikey", "token", "guid")]:
                q.pop(k, None)
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if m == "header":
            headers["X-Api-Key"] = raw_key
        if is_post:
            headers["Content-Type"] = "application/json; charset=utf-8"
        qs = urllib.parse.urlencode(q)
        return f"{BASE_URL}{path}" + (f"?{qs}" if qs else ""), headers

    url, headers = _build(mode)
    if need_key and raw_key and (raw_key in url or urllib.parse.quote(raw_key, safe="") in url
                                 or urllib.parse.quote_plus(raw_key) in url):
        raise PoaHTTPError("client", f"refused to send: the API key would appear in the URL of {path}")
    last = None
    attempt = 0
    while attempt < max(1, retries):
        try:
            raw = http_request(method, url, data=data, headers=headers, timeout=timeout,
                               follow_redirects=not need_key).decode("utf-8", errors="replace")
            clean = redact(raw, raw_key)
            try:
                return json.loads(clean)
            except ValueError:
                raise PoaHTTPError("server", f"Non-JSON response on {path}: {clean[:300]}")
        except Exception as e:
            last = _classify(e, path, raw_key)
            if last.kind in ("auth", "client", "halted", "redirect-refused"):
                raise last from None
            attempt += 1
            if attempt < retries:
                _backoff(attempt - 1)
                continue
            raise last from None
    raise last


def hosted_assignments(timeout: int = None, retries: int = None, max_warming_waits: int = 2, sleep=time.sleep) -> dict:
    """Keyed seat listing, header-only (HOSTED_ASSIGNMENTS_PATH). Never falls back to ?key=.

    503 `warming` (index building) waits retry_after_seconds (capped at 30 s) up to max_warming_waits
    times. Anything else that fails (502, timeout, 401, 3xx) raises PoaHTTPError whose message says the
    seats are UNKNOWN: callers must treat that as unknown, never as zero seats."""
    waits = 0
    while True:
        try:
            return api_call("GET", HOSTED_ASSIGNMENTS_PATH, query={"sort": "oldest", "limit": 200},
                            timeout=timeout, retries=retries)
        except PoaHTTPError as e:
            warming = e.status == 503 and "warming" in str(e).lower()
            if warming and waits < max_warming_waits:
                waits += 1
                sleep(min(30, max(1, int(e.retry_after or 20))))
                continue
            err = PoaHTTPError(e.kind, f"grading seats unknown (header-only read of {HOSTED_ASSIGNMENTS_PATH}; "
                                       f"no URL-key fallback): {e}", e.status, retry_after=e.retry_after)
            err.seats_unknown = True
            raise err from None


def mcp_call(tool: str, arguments: dict = None, need_key: bool = True, timeout: int = 30) -> dict:
    """Call the official minepoa MCP endpoint (go.md: https://minepoa.com/mcp, streamable HTTP).

    Tool names come from go.md; argument names (api_key, answer, assignment, score, reasoning)
    come from a tools/list capture taken before the Oct 21 outage. Off unless POA_MCP_FALLBACK=1.
    """
    raw_key = load_key() if need_key else ""
    args = dict(arguments or {})
    if need_key:
        args["api_key"] = raw_key
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect) if need_key else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = redact(resp.read().decode("utf-8", errors="replace"), raw_key)
    except Exception as e:
        raise _classify(e, f"mcp:{tool}", raw_key) from None
    text = raw.strip()
    if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else "{}"
    env = json.loads(text)
    if env.get("error"):
        raise PoaHTTPError("client", f"MCP {tool} error: {json.dumps(env['error'])[:400]}")
    res = env.get("result", {}) or {}
    content = res.get("content") or []
    if content and isinstance(content, list) and isinstance(content[0], dict) and "text" in content[0]:
        try:
            out = json.loads(content[0]["text"])
        except Exception:
            out = {"raw_text": content[0]["text"]}
        if res.get("isError"):
            raise PoaHTTPError("client", f"MCP {tool} isError: {json.dumps(out)[:400]}")
        out["_via"] = "mcp"
        return out
    return res


CACHE_DIR = POA_DIR / "cache"


def epoch_cache_path(epoch) -> pathlib.Path:
    return CACHE_DIR / f"epoch-{epoch}.json"


def load_epoch_cache(epoch=None) -> dict:
    """Cached challenge for `epoch` (or the newest cached epoch when None); {} if none."""
    if not CACHE_DIR.is_dir():
        return {}
    if epoch is None:
        nums = []
        for f in CACHE_DIR.glob("epoch-*.json"):
            try:
                nums.append(int(f.stem.split("-", 1)[1]))
            except ValueError:
                pass
        if not nums:
            return {}
        epoch = max(nums)
    f = epoch_cache_path(epoch)
    try:
        if not f.is_file():
            return {}
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        ts_ref = str(data.get("last_read") or data.get("first_read") or data.get("sent_at") or "").strip()
        if ts_ref and ts_ref < MAINNET_GENESIS_UTC:
            return {}
        return data
    except Exception:
        return {}


def save_epoch_cache(epoch, **fields) -> dict:
    """Merge fields into ~/.poa/cache/epoch-<n>.json (atomic write, 0600). Never stores the key."""
    if epoch is None:
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cur = load_epoch_cache(epoch)
    cur.update({k: v for k, v in fields.items() if v is not None})
    cur["epoch"] = epoch
    f = epoch_cache_path(epoch)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    lock_file_permissions(tmp)
    os.replace(tmp, f)
    return cur


def cache_challenge(ch: dict) -> None:
    """Save words+seed the moment any challenge read succeeds (first seen + last seen)."""
    try:
        ep = ch.get("epoch")
        if ep is None or not ch.get("words"):
            return
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = load_epoch_cache(ep)
        fields = {
            k: ch.get(k)
            for k in ("words", "seed", "challenge", "prompt", "challenge_txid", "txid", "superseded", "answered_txid", "seed_rotated_this_epoch")
            if ch.get(k) is not None
        }
        if cur.get("words") and cur.get("words") != ch.get("words"):
            fields["previous_words"] = cur.get("words")
        fields["last_read"] = now
        fields.setdefault("first_read", cur.get("first_read") or now)
        if ch.get("answered") or ch.get("superseded") or ch.get("answered_txid"):
            fields["server_answered"] = True
        save_epoch_cache(ep, **fields)
    except Exception:
        pass


def edge_read_challenge(address: str = None, timeout: int = 12) -> dict:
    """Read-only edge fallback: GET /api/challenge/<addr> (keyless; stays up during /hosted/* halts/outages)."""
    addr = (address or local_address()).strip()
    if not addr:
        return {}
    try:
        res = api_call("GET", f"/api/challenge/{addr}", need_key=False, timeout=timeout, retries=1)
        if isinstance(res, dict) and res.get("words"):
            cache_challenge(res)
            ep = res.get("epoch")
            local_ans = has_answered_epoch_locally(ep)
            exp_ans = explorer_find_answer(addr, ep)
            res["answered_in_local_log"] = local_ans
            res["answered_on_explorer"] = bool(exp_ans.get("found"))
            if exp_ans.get("txid"):
                res["explorer_answer_txid"] = exp_ans.get("txid")
            res["answered"] = bool(is_challenge_answered(res, ep) or exp_ans.get("found"))
            res["_via"] = "edge:/api/challenge"
            return res
    except Exception:
        pass
    return {}


def edge_read_epochs_summary(address: str = None, timeout: int = 25) -> dict:
    """Read-only edge fallback: GET /api/epochs (keyless) -> cumulative settled rewards, established state & pending answers."""
    addr = (address or local_address()).strip()
    if not addr:
        return {}
    try:
        eps = api_call("GET", "/api/epochs", need_key=False, timeout=timeout, retries=1)
        if isinstance(eps, dict):
            eps = eps.get("epochs") or eps.get("items") or []
        if not isinstance(eps, list) or not eps:
            return {}
        total_settled = 0.0
        rewards_by_epoch = {}
        established = None
        for e in eps:
            if not isinstance(e, dict):
                continue
            ep = e.get("epoch")
            rf = 0.0
            rew = (e.get("rewards") or {}).get(addr)
            if rew is not None:
                try:
                    rf = float(rew)
                    total_settled += rf
                    if rf > 0:
                        rewards_by_epoch[str(ep)] = f"{rf:.6f}"
                except Exception:
                    rf = 0.0
            b = e.get("basis") or {}
            if isinstance(b, dict):
                if addr in (b.get("established_after") or []) or (b.get("graders_served") or {}).get(addr):
                    established = True
                # A payout (or a passed answer) does NOT imply established: E6 paid 105
                # passed+eligible accounts but listed only 11 in established_after.
                if addr in (b.get("established_removed") or []) or addr in (b.get("slashed") or []) or addr in (b.get("ejected") or []):
                    established = False
        latest = eps[-1] if isinstance(eps[-1], dict) else {}
        return {
            "address": addr,
            "epochs_settled_count": len(eps),
            "latest_closed_epoch": latest.get("epoch"),
            "cumulative_settled_balance": f"{total_settled:.6f}",
            "rewards_by_epoch": rewards_by_epoch,
            "established_from_epochs": established,
            "_via": "edge:/api/epochs",
        }
    except Exception:
        return {}


def read_challenge(retries: int = None) -> dict:
    """GET /api/hosted/challenge (cached on success), falling back to MCP poa_challenge (flagged, last resort)."""
    try:
        res = api_call("GET", "/api/hosted/challenge", retries=retries)
    except PoaHTTPError as e:
        if e.kind in ("server", "timeout", "network") and mcp_fallback_enabled():
            res = mcp_call("poa_challenge", {})
            res["_rest_error"] = str(e)[:200]
        else:
            edge_ch = edge_read_challenge()
            if edge_ch:
                e.edge_challenge = edge_ch
            raise
    cache_challenge(res)
    return res


# ---------------------------------------------------------------------------
# Tx-lookup guard (server release v1.2-txlookup-hardening, Oct 26 2026)
# The edge caps tx lookups at 30/min and caches misses for 60 s; an unknown txid on
# /api/explorer/tx/<id> now answers 503 "origin busy" with Retry-After: 60.
# Every /api/explorer/tx and /api/receipt lookup goes through two local guards:
#   * a shared sliding window (default 25/min, under the 30/min cap), file-backed so
#     separate processes on one machine share it;
#   * a negative cache: a txid that came back 404/429/503 is not asked about again
#     until its Retry-After (at least 60 s) has passed.
# ---------------------------------------------------------------------------
TXLOOKUP_PER_MIN = int(os.environ.get("POA_TXLOOKUP_PER_MIN", "25"))
TXLOOKUP_NEG_TTL = int(os.environ.get("POA_TXLOOKUP_NEG_TTL", "60"))
TXLOOKUP_MAX_WAIT = float(os.environ.get("POA_TXLOOKUP_MAX_WAIT", "65"))


def _txl_path() -> pathlib.Path:
    return POA_DIR / "cache" / "txlookup.json"


def _txl_load() -> dict:
    try:
        st = json.loads(_txl_path().read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("window", [])
            st.setdefault("neg", {})
            return st
    except Exception:
        pass
    return {"window": [], "neg": {}}


def _txl_save(st: dict) -> None:
    try:
        path = _txl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def txlookup_negative(txid: str, now: float = None):
    """Cached miss for txid ({"until", "status", "why"}) if still inside its window, else None."""
    now = time.time() if now is None else now
    hit = (_txl_load().get("neg") or {}).get((txid or "").lower())
    if isinstance(hit, dict) and float(hit.get("until") or 0) > now:
        return hit
    return None


def txlookup_mark_negative(txid: str, status=None, retry_after=None, why: str = "", now: float = None) -> dict:
    now = time.time() if now is None else now
    ttl = max(TXLOOKUP_NEG_TTL, int(retry_after or 0))
    st = _txl_load()
    neg = {k: v for k, v in (st.get("neg") or {}).items() if isinstance(v, dict) and float(v.get("until") or 0) > now}
    entry = {"until": now + ttl, "status": status, "why": why[:120]}
    neg[(txid or "").lower()] = entry
    st["neg"] = neg
    _txl_save(st)
    return entry


def txlookup_slot(sleep=time.sleep, now_fn=time.time, max_wait: float = None) -> float:
    """Take one slot in the shared per-minute tx-lookup window, sleeping if it is full.
    Returns seconds waited. Never waits longer than max_wait (default POA_TXLOOKUP_MAX_WAIT)."""
    max_wait = TXLOOKUP_MAX_WAIT if max_wait is None else max_wait
    cap = max(1, TXLOOKUP_PER_MIN)
    waited = 0.0
    while True:
        now = now_fn()
        st = _txl_load()
        win = [float(t) for t in st.get("window") or [] if now - float(t) < 60.0]
        if len(win) < cap or waited >= max_wait:
            win.append(now)
            st["window"] = win[-cap * 2:]
            _txl_save(st)
            return waited
        delay = min(max(0.5, 60.0 - (now - min(win)) + 0.1), max_wait - waited)
        sleep(delay)
        waited += delay


def _neg_until_iso(entry: dict) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(entry["until"]), datetime.timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Keyless explorer confirmation (public read node; lags a few minutes)
# ---------------------------------------------------------------------------
EXPLORER_MIN_WAIT = int(os.environ.get("POA_EXPLORER_MIN_WAIT", "180"))
EXPLORER_MAX_WAIT = int(os.environ.get("POA_EXPLORER_MAX_WAIT", "900"))


def explorer_get(path: str, timeout: float = 40):
    txid = path.rsplit("/", 1)[-1].split("?", 1)[0] if path.startswith("/api/explorer/tx/") else None
    if txid:
        neg = txlookup_negative(txid)
        if neg:
            return {"error": f"txid lookup skipped: cached miss until {_neg_until_iso(neg)} (edge 60 s negative cache)",
                    "status": neg.get("status"), "cached_miss": True}
        txlookup_slot()
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=timeout)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        err = _classify(e, path, "")
        if txid and err.status in (404, 429, 503):
            txlookup_mark_negative(txid, err.status, err.retry_after, why="explorer/tx")
        return {"error": redact(str(err))[:200], "status": err.status, "retry_after": err.retry_after}


# Server release v0.21-pusher (Oct 28, 2026): state rolls up into checkpoints (anchored on Solana and
# Base) and new writes are checked against the latest checkpoint, not the whole history. Several
# counters now cover only the window since that checkpoint, so they DROP at each roll-up. A drop is
# not data loss; never compare them across checkpoints. Mine/answer checks use next_nonce instead.
SINCE_CHECKPOINT_SCOPE = "since latest checkpoint (resets at each checkpoint roll-up; a drop is not data loss)"
STATUS_SINCE_CHECKPOINT_FIELDS = ("txs", "final", "invalid")


def label_status_counts(st):
    """Annotate /api/status: txs/final/invalid are since-latest-checkpoint counts, plus checkpoint height."""
    if not isinstance(st, dict):
        return st
    out = dict(st)
    out["counts_scope"] = {k: SINCE_CHECKPOINT_SCOPE for k in STATUS_SINCE_CHECKPOINT_FIELDS if k in st}
    return out


def label_me_counts(me):
    """Annotate /api/hosted/me: attestations_by_me is a since-latest-checkpoint count."""
    if isinstance(me, dict) and "attestations_by_me" in me:
        me = dict(me)
        me["attestations_by_me_scope"] = SINCE_CHECKPOINT_SCOPE
    return me


def explorer_find_answer(address: str, epoch, txid: str = None) -> dict:
    """Look for our `challenge` tx for `epoch` on the keyless explorer. found: True/False/None(unknown)."""
    if txid:
        t = explorer_get(f"/api/explorer/tx/{txid}")
        if isinstance(t, dict) and not t.get("error") and (t.get("id") or t.get("tx")):
            return {"found": True, "via": "explorer/tx", "txid": txid, "tx": {k: (t.get("tx") or t).get(k) for k in ("id", "type", "epoch", "ts", "atts", "final")}}
    if not address:
        return {"found": None, "error": "no address configured (POA_ADDRESS or ~/.poa/address)"}
    d = explorer_get(f"/api/explorer/address/{address}?sort=new")
    if not isinstance(d, dict) or d.get("error"):
        return {"found": None, "error": (d or {}).get("error") if isinstance(d, dict) else "bad explorer reply"}
    for t in d.get("txs") or []:
        if t.get("type") == "challenge" and str(t.get("epoch")) == str(epoch):
            return {"found": True, "via": "explorer/address", "txid": t.get("id"), "tx": {k: t.get(k) for k in ("id", "type", "epoch", "ts", "atts", "final")},
                    "explorer_as_of": d.get("as_of"), "checkpoint_height": d.get("checkpoint_height")}
    # Not listed is UNKNOWN, never "absent": since v0.21-pusher the per-address view can cover only the
    # window since the latest checkpoint, and it lags. It must never trigger a resend; the challenge
    # `answered` flag (and answered_txid / local log) stays the deciding signal.
    return {"found": None, "status": "unknown", "explorer_as_of": d.get("as_of"), "checkpoint_height": d.get("checkpoint_height"),
            "txs_listed": len(d.get("txs") or []),
            "note": "not listed on the explorer address view: unknown, not absent. Do not resend; /challenge `answered` decides."}


# Rejected-receipt codes (go.md "READING A REJECTED RECEIPT", server release v0.66-ipfix-receipts, Oct 29, 2026).
# A rejected tx is final for that txid: never resend the same tx. The action says what to do instead.
REJECTED_RECEIPT_ACTIONS = {
    "nonce_slot_used": "rebuild the tx with next_nonce and a fresh ts, then submit the NEW tx",
    "bad_nonce": "rebuild the tx with next_nonce and a fresh ts, then submit the NEW tx",
    "off_panel": "re-fetch your assignments (panels redraw every epoch close) and grade only those",
    "duplicate_grade": "you already graded it; move on",
    "bad_stamp": "mine the answer stamp after choosing the nonce, then sign",
}


def node_receipt(txid: str, timeout: float = 20):
    """Keyless GET /api/receipt/<txid>. Returns the receipt dict, {"status": "unknown-txid"} on a direct 404,
    or None when unreadable. Never resends anything; the key is never sent."""
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,128}", txid):
        return None
    txlookup_slot()
    try:
        r = api_call("GET", f"/api/receipt/{txid}", need_key=False, timeout=timeout, retries=1)
    except PoaHTTPError as e:
        if e.status == 404:
            return {"status": "unknown-txid"}
        if e.status in (429, 503):
            txlookup_mark_negative(txid, e.status, e.retry_after, why="api/receipt")
        return None
    except Exception:
        return None
    return r if isinstance(r, dict) else None


def rejected_receipt_result(txid: str, rc: dict) -> dict:
    """Shape a node receipt with status 'rejected' into a write_receipt result (outcome: rejected, never resend)."""
    code = rc.get("code")
    out = {"txid": txid, "outcome": "rejected", "resend_ok": False, "code": code, "error": rc.get("error"),
           "hint": rc.get("hint"), "action": REJECTED_RECEIPT_ACTIONS.get(code) or "follow the hint",
           "note": "the node rejected this tx. Do NOT resubmit the same tx; follow the hint/action."}
    for k in ("nonce", "next_nonce"):
        if rc.get(k) is not None:
            out[k] = rc.get(k)
    return out


def write_receipt(txid: str, timeout: float = 20) -> dict:
    """Explorer receipt, then (if the explorer doesn't show it landed) the node's /api/receipt for a rejection."""
    res = _explorer_write_receipt(txid, timeout=timeout)
    # Only a direct explorer 404 asks the node: redirects, 429/503, timeouts and cached misses stay `unknown`
    # without another lookup (the tx-lookup caps and 60 s miss cache still apply).
    if res.get("outcome") != "not-found":
        return res
    rc = node_receipt(res.get("txid") or txid, timeout=timeout)
    if not isinstance(rc, dict):
        return res
    st = rc.get("status")
    if st == "rejected":
        return rejected_receipt_result(res.get("txid") or txid, rc)
    if st == "included":
        out = {"txid": res.get("txid") or txid, "outcome": "landed-final" if rc.get("checkpoint_covered") else "landed-pending",
               "resend_ok": False, "type": rc.get("type"), "source": "api/receipt",
               "note": "the node has this tx (explorer had not shown it yet); do not resend"}
        if rc.get("next_nonce") is not None:
            out["next_nonce"] = rc.get("next_nonce")
        return out
    if st == "unknown-txid" and res.get("outcome") == "not-found":
        res["node_receipt"] = "404 unknown txid"
        res["note"] = ("explorer and node both say unknown txid: the node never received it. Re-submit it ONCE "
                       "(after the 60 s miss cache), don't poll forever.")
    return res


def _explorer_write_receipt(txid: str, timeout: float = 20) -> dict:
    """Keyless write receipt for one txid. outcome: landed-final | landed-pending | not-found | unknown.
    A timeout, outage, network error or ANY 3xx redirect is ALWAYS `unknown` (never failed); only a direct 404 is `not-found`.
    Redirects are not followed: the explorer tx index misses ~epoch 5 to pre-restart epoch 10 and answers 307 for those
    (finding by Steven-Vernier, LabLeak comment 3311), so a redirect means "not indexed", not "absent"."""
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,128}", txid):
        return {"txid": txid, "outcome": "invalid", "resend_ok": False, "note": "txid must be hex"}
    path = f"/api/explorer/tx/{txid}"
    neg = txlookup_negative(txid)
    if neg:
        return {"txid": txid, "outcome": "unknown", "resend_ok": False, "error_kind": "cached-miss", "http_status": neg.get("status"),
                "retry_not_before": _neg_until_iso(neg),
                "note": "this txid was just looked up and missed; the edge caches misses for 60 s, so asking again now proves nothing. "
                        "Unknown, NOT failed. Do not resend; re-run `receipt` after retry_not_before."}
    txlookup_slot()
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=timeout,
                           follow_redirects=False)
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and 300 <= e.code < 400:
            return {"txid": txid, "outcome": "unknown", "resend_ok": False, "error_kind": "redirect", "http_status": e.code,
                    "note": f"explorer answered {e.code} redirect: tx NOT INDEXED (explorer gap, e.g. epochs ~5-10 pre-restart), "
                            "not absent. Unknown, NOT safe to resend; check your own logs / address history."}
        err = _classify(e, path, "")
        if err.status in (404, 429, 503):
            neg = txlookup_mark_negative(txid, err.status, err.retry_after, why="receipt")
        if err.status == 404:
            return {"txid": txid, "outcome": "not-found", "resend_ok": True, "recheck_after_s": TXLOOKUP_NEG_TTL,
                    "note": "explorer returned 404: the write did not land. The edge caches misses for 60 s, so if you sent it "
                            "in the last minute, re-check after 60 s before resending."}
        out = {"txid": txid, "outcome": "unknown", "resend_ok": False, "error_kind": err.kind,
               "note": "read failed (" + err.kind + "): unknown, NOT failed. Do not resend; re-run `receipt` later."}
        if err.status in (429, 503):
            out.update(http_status=err.status, retry_after_s=err.retry_after, retry_not_before=_neg_until_iso(neg),
                       note=f"explorer answered {err.status} (tx-lookup cap or a cached miss: since Oct 26 an unindexed or unknown txid "
                            "answers 503 with Retry-After 60). Unknown, NOT failed. Do not resend; re-run `receipt` after retry_not_before.")
        return out
    t = (d.get("tx") if isinstance(d, dict) and isinstance(d.get("tx"), dict) else d) or {}
    if not isinstance(t, dict) or not (t.get("id") or t.get("txid") or t.get("type")):
        return {"txid": txid, "outcome": "unknown", "resend_ok": False, "note": "unexpected explorer body; treat as unknown"}
    atts = t.get("atts", t.get("attestations"))
    atts_n = len(atts) if isinstance(atts, list) else atts
    cp_info = t.get("checkpointed") or (d.get("checkpointed") if isinstance(d, dict) else None)
    final = bool(t.get("final") or cp_info or t.get("checkpoint_covered"))
    out = {"txid": txid, "outcome": "landed-final" if final else "landed-pending", "resend_ok": False,
           "type": t.get("type"), "epoch": t.get("epoch"), "ts": t.get("ts"), "final": final, "attestations": atts_n,
           "note": "on the ledger; do not resend (a resend creates a duplicate for graders)"}
    if cp_info is not None or t.get("checkpoint_covered") is not None:
        out["checkpointed"] = cp_info if cp_info is not None else t.get("checkpoint_covered")
    if t.get("next_nonce") is not None:
        out["next_nonce"] = t.get("next_nonce")
    return out


def confirm_answer_landed(epoch, txid: str = None, min_wait: int = None, max_wait: int = None, sleep=time.sleep) -> dict:
    """Wait >= min_wait (explorer lags), then poll the explorer (and /challenge when it answers) until max_wait.
    Never sends anything. Result landed: True / None (unknown); never False from silence alone."""
    min_wait = EXPLORER_MIN_WAIT if min_wait is None else min_wait
    max_wait = EXPLORER_MAX_WAIT if max_wait is None else max_wait
    addr = local_address()
    waited, checks = 0, []
    if min_wait:
        sleep(min_wait)
        waited += min_wait
    while True:
        r = explorer_find_answer(addr, epoch, txid)
        checks.append({"t": waited, "found": r.get("found"), "err": r.get("error")})
        if r.get("found"):
            return {"landed": True, "how": "explorer", "evidence": r, "waited_s": waited, "checks": checks}
        try:
            ch = api_call("GET", "/api/hosted/challenge", timeout=20, retries=1)
            cache_challenge(ch)
            if ch.get("epoch") == epoch and (ch.get("answered") or ch.get("superseded") or ch.get("answered_txid")):
                return {"landed": True, "how": "challenge-read-back", "waited_s": waited, "checks": checks}
        except Exception:
            pass
        if waited >= max_wait:
            return {"landed": None, "how": "unconfirmed", "note": "not on explorer yet; timeout = unknown, NOT failed. Do not resend; re-run `answer-status` later.", "waited_s": waited, "checks": checks}
        step = min(60, max(1, max_wait - waited))
        sleep(step)
        waited += step


def read_open_seats():
    """Set of open assignment txids, or None if unknown."""
    try:
        res = hosted_assignments()
    except PoaHTTPError as e:
        if e.kind in ("server", "timeout", "network") and mcp_fallback_enabled():
            res = mcp_call("poa_assignments", {})
        else:
            raise
    rows = res.get("assignments") or res.get("open_grading_seats") or []
    return {(s.get("answer_txid") or s.get("txid")) for s in rows if isinstance(s, dict) and (s.get("answer_txid") or s.get("txid"))}


def _verify_rounds():
    return int(os.environ.get("POA_VERIFY_ROUNDS", "4"))


STALE_MARKERS = (
    "stale seed",
    "stale_seed",
    "seed_rotated_this_epoch",
    "stale words",
    "wrong words",
    "words changed",
    "seed mismatch",
    "stale challenge",
    "epoch closed",
    "not the current epoch",
)
DUP_MARKERS = ("duplicate challenge for epoch", "already answered", "duplicate answer")


# ---------------------------------------------------------------------------
# Nonce discipline for hosted writes (G0 notice 2026-10-24-nonce-client).
# Hosted writes (answer, grade, vote, mine) are signed server-side and each one takes the
# next nonce slot of OUR address. When two writes leave close together (a grade-batch firing
# the next grade before the previous one is visible, or two processes on one box), the second
# lands in a slot that is already taken: a duplicate-slot tx, rejected, and it burns rate limit.
# Rules enforced here:
#   1. Single-flight: every hosted write holds an exclusive file lock (~/.poa/cache/write.lock).
#   2. After each submit, adopt the SERVER's next_nonce (response -> /api/receipt/<txid> -> /me).
#      Never compute it locally, and never reuse a cached value across runs.
#   3. Before the next write, /me must show at least the adopted next_nonce; if it lags past
#      POA_NONCE_SETTLE_S the write is NOT sent (kind "nonce-lag", safe to retry next run).
#   4. On a nonce rejection: refetch next_nonce once; retry once ONLY if the server's slot moved
#      past the one that was rejected. Never resend into the same slot.
# ---------------------------------------------------------------------------
NONCE_SETTLE_S = float(os.environ.get("POA_NONCE_SETTLE_S", "15"))
NONCE_POLL_S = float(os.environ.get("POA_NONCE_POLL_S", "3"))
WRITE_LOCK_TIMEOUT_S = float(os.environ.get("POA_WRITE_LOCK_TIMEOUT_S", "300"))
# "slot latched" (live client change 3:44 PM PT Oct 27, 2026, params slot_latch_from_ts): once a
# checkpoint bake covers a nonce slot with two or more candidate txs, its winner is written to the
# chain's slot-winners.jsonl and any other tx in that slot is rejected "slot latched (slot-winners hN)".
# It is a taken-slot refusal like the others: never resend into it.
NONCE_MARKERS = ("nonce", "duplicate slot", "duplicate-slot", "dup-slot", "slot taken", "slot already",
                 "slot latched")
_NONCE = {"adopted": None, "source": None, "at": None}


def is_nonce_error(e) -> bool:
    low = str(e).lower()
    return any(m in low for m in NONCE_MARKERS)


def _as_int(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def read_next_nonce() -> int:
    """Fresh next_nonce for our address from GET /api/hosted/me (None if unreadable). Never cached."""
    try:
        me = api_call("GET", "/api/hosted/me", timeout=20, retries=1)
    except PoaHTTPError:
        return None
    return _as_int(me.get("next_nonce")) if isinstance(me, dict) else None


def receipt_next_nonce(txid: str):
    """Keyless GET /api/receipt/<txid> -> the sender's next_nonce (None if missing/unreadable)."""
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,128}", txid):
        return None
    if txlookup_negative(txid):
        return None
    txlookup_slot()
    try:
        r = api_call("GET", f"/api/receipt/{txid}", need_key=False, timeout=20, retries=1)
    except PoaHTTPError as e:
        if e.status in (404, 429, 503):
            txlookup_mark_negative(txid, e.status, e.retry_after, why="api/receipt")
        return None
    return _as_int(r.get("next_nonce")) if isinstance(r, dict) else None


def next_nonce_from_response(res):
    """next_nonce carried by a write response (top level, or inside receipt/tx/result)."""
    if not isinstance(res, dict):
        return None
    for holder in (res, res.get("receipt"), res.get("tx"), res.get("result")):
        if isinstance(holder, dict) and _as_int(holder.get("next_nonce")) is not None:
            return _as_int(holder.get("next_nonce"))
    return None


def adopt_next_nonce(res) -> dict:
    """After a submit: take the server's next_nonce (response, then receipt, then /me) and remember it
    for this process only. Returns {"next_nonce": n, "source": ...}."""
    n, src = next_nonce_from_response(res), "response"
    if n is None and isinstance(res, dict):
        n, src = receipt_next_nonce(res.get("txid") or res.get("id") or ""), "receipt"
    if n is None:
        n, src = read_next_nonce(), "me"
    if n is None:
        src = None
    elif _NONCE["adopted"] is None or n >= _NONCE["adopted"]:
        _NONCE.update(adopted=n, source=src, at=time.time())
    return {"next_nonce": n, "source": src}


def wait_for_slot(target, sleep=time.sleep, settle=None) -> tuple:
    """Poll /me until next_nonce >= target (bounded by settle seconds). Returns (ok, last_seen)."""
    settle = NONCE_SETTLE_S if settle is None else settle
    seen = read_next_nonce()
    if target is None:
        return True, seen
    waited = 0.0
    while seen is None or seen < target:
        if waited >= settle:
            return False, seen
        sleep(NONCE_POLL_S)
        waited += NONCE_POLL_S
        seen = read_next_nonce()
    return True, seen


class _WriteLock:
    """Exclusive cross-process lock for hosted writes (fcntl on POSIX, msvcrt on Windows)."""

    def __init__(self, path: pathlib.Path = None, timeout: float = None):
        self.path = path or (POA_DIR / "cache" / "write.lock")
        self.timeout = WRITE_LOCK_TIMEOUT_S if timeout is None else timeout
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, IOError):
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    raise PoaHTTPError("busy", "another poa.py write is still in flight (write.lock held); not sending")
                time.sleep(0.5)

    def __exit__(self, *exc):
        if self.fh is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self.fh.close()
        self.fh = None
        return False


def hosted_write(send, sleep=time.sleep) -> dict:
    """Run one nonce-bearing hosted write `send()` under the nonce rules above.
    Returns the response dict with `_nonce` info added. Raises PoaHTTPError; kind "nonce-lag" or
    "nonce" means NOT landed and safe to try again on a later run."""
    with _WriteLock():
        ok, before = wait_for_slot(_NONCE["adopted"], sleep=sleep)
        if not ok:
            raise PoaHTTPError("nonce-lag", f"server next_nonce {before} is behind our last adopted {_NONCE['adopted']} "
                                            f"after {NONCE_SETTLE_S:.0f}s; not sending into a possibly taken slot")
        retried = False
        try:
            res = send()
        except PoaHTTPError as e:
            if not is_nonce_error(e):
                raise
            ok2, after = wait_for_slot((before + 1) if before is not None else None, sleep=sleep)
            if not ok2 or after is None or before is None or after <= before:
                raise PoaHTTPError("nonce", f"nonce rejected and the server slot did not move (before={before}, now={after}); "
                                            f"not resending into the same slot: {str(e)[:300]}", e.status)
            retried = True
            try:
                res = send()
            except PoaHTTPError as e2:
                if is_nonce_error(e2):
                    raise PoaHTTPError("nonce", f"nonce rejected again after one refetch+retry; giving up: {str(e2)[:300]}", e2.status)
                raise
        info = adopt_next_nonce(res)
        info.update(before=before, retried_after_nonce_error=retried)
        if isinstance(res, dict):
            res = dict(res)
            res["_nonce"] = info
        return res


# Busy-node receipts (G0 notice 2026-10-29-busy-node-receipts, release v0.60-acceptqueue-buildtx,
# Oct 29, 2026): when the node is busy, answer and grade may return HTTP 202 with
# {"job_id", "poll_url": "api/job/<id>", "retry_after_seconds"} instead of the result. 202 means
# ACCEPTED, not failed. Never resubmit while queued (that returns 409 duplicate_in_flight): wait
# retry_after_seconds, then GET BASE/ + poll_url until it returns the same body the inline reply
# would have had. Results stay fetchable about an hour. kind "queued" = sent and accepted, result
# not known yet: do not resend; read state back on the next run.
BUSY_PENDING_STATES = ("queued", "pending", "running", "accepted", "in_progress", "processing")
BUSY_RESULT_KEYS = ("txid", "next_nonce", "error", "result")
BUSY_POLL_PATH_RE = re.compile(r"^/?api/job/[A-Za-z0-9_.:\-]{1,128}$")
BUSY_MAX_WAIT_S = float(os.environ.get("POA_BUSY_MAX_WAIT_S", "300"))
BUSY_STEP_CAP_S = 60


def is_busy_receipt(res) -> bool:
    """True for a 202 busy-node receipt (or a still-pending poll reply), False for a real result."""
    if not isinstance(res, dict):
        return False
    if any(k in res for k in BUSY_RESULT_KEYS):
        return False
    status = str(res.get("status") or res.get("state") or "").lower()
    return bool(res.get("poll_url")) or (bool(res.get("job_id")) and (not status or status in BUSY_PENDING_STATES))


def _same_origin_poll_path(pu: str) -> str:
    """MCP receipts (G0 notice 2026-10-29-mcp-queue, release v0.64.1-mcpdocs) may carry an absolute
    poll_url. Accept it only when scheme and host are exactly BASE_URL's and there is no query,
    fragment or userinfo; return its path so the normal relative-path check applies. Any other
    absolute URL is returned unchanged and then refused."""
    if "://" not in pu:
        return pu
    try:
        u, b = urllib.parse.urlsplit(pu), urllib.parse.urlsplit(BASE_URL)
    except ValueError:
        return pu
    if (u.scheme.lower(), (u.netloc or "").lower()) != (b.scheme.lower(), (b.netloc or "").lower()):
        return pu
    if u.query or u.fragment or "@" in (u.netloc or ""):
        return pu
    return u.path.lstrip("/")


def busy_poll_path(res: dict, fallback: str = None) -> str:
    """Relative /api/job/<id> path from a receipt. Anything else (absolute URL, other host or path)
    is refused so a receipt can never point the keyed client somewhere new."""
    pu = str(res.get("poll_url") or "").strip()
    if not pu and res.get("job_id"):
        pu = f"api/job/{res['job_id']}"
    if not pu and fallback:
        pu = fallback
    pu = _same_origin_poll_path(pu)
    if not BUSY_POLL_PATH_RE.match(pu):
        raise PoaHTTPError("client", "refused busy-node poll_url (only a relative api/job/<id> path is accepted)")
    return "/" + pu.lstrip("/")


def _busy_wait_s(res: dict) -> float:
    ra = res.get("retry_after_seconds", res.get("retry_after")) if isinstance(res, dict) else None
    try:
        ra = float(ra)
    except (TypeError, ValueError):
        ra = 5.0
    return max(1.0, min(float(BUSY_STEP_CAP_S), ra))


def resolve_busy_receipt(res, path: str = "", sleep=None, max_wait: float = None):
    """Turn a busy-node 202 receipt into the real result by polling its job, never resubmitting.
    A non-receipt is returned unchanged. Raises PoaHTTPError(kind="queued") if the job is still
    pending after max_wait seconds or its result is no longer fetchable; callers must not resend."""
    if not is_busy_receipt(res):
        return res
    max_wait = BUSY_MAX_WAIT_S if max_wait is None else max_wait
    sleep = sleep or time.sleep
    poll_path = busy_poll_path(res)
    job_id = res.get("job_id") or poll_path.rsplit("/", 1)[-1]
    waited = 0.0
    cur = res
    while True:
        step = _busy_wait_s(cur)
        if waited + step > max_wait:
            err = PoaHTTPError("queued", f"accepted by a busy node (job {job_id}) on {path}; still pending after "
                                         f"{waited:.0f}s. Not resubmitting; read back on the next run.", 202)
            err.job_id, err.poll_path = job_id, poll_path
            raise err
        sleep(step)
        waited += step
        try:
            cur = api_call("GET", poll_path, timeout=30, retries=2)
        except PoaHTTPError as e:
            if e.status in (404, 410):
                err = PoaHTTPError("queued", f"busy-node job {job_id} result no longer fetchable ({e.status}); it was "
                                             f"accepted, so do not resubmit; confirm on the ledger.", e.status)
                err.job_id, err.poll_path = job_id, poll_path
                err.expired = True
                raise err from None
            raise
        if not is_busy_receipt(cur):
            if isinstance(cur, dict):
                cur = dict(cur)
                cur["_busy_job"] = {"job_id": job_id, "waited_s": round(waited, 1)}
            return cur
        poll_path = busy_poll_path(cur, fallback=poll_path.lstrip("/"))


def is_in_flight(e) -> bool:
    """409 duplicate_in_flight: an earlier send of this write is still queued (sent, not failed)."""
    return getattr(e, "status", None) == 409 and "duplicate_in_flight" in str(e).lower()


def busy_aware(send, path: str = "", sleep=None):
    """Wrap one hosted write so a 202 receipt is polled to its result and 409 duplicate_in_flight
    becomes kind "queued" (accepted earlier, do not resend)."""
    def run():
        try:
            res = send()
        except PoaHTTPError as e:
            if is_in_flight(e):
                raise PoaHTTPError("queued", f"an earlier send is still queued on a busy node ({path}); "
                                             f"not resubmitting: {str(e)[:200]}", 409) from None
            raise
        return resolve_busy_receipt(res, path=path, sleep=sleep)
    return run


# Busy-job store (SUGGESTIONS #79, Oct 29, 2026): the kit stops polling a 202 receipt after
# POA_BUSY_MAX_WAIT_S, but job results stay fetchable about an hour. So a still-queued job is saved
# to $POA_HOME/busy-jobs.json (0600, never a repo or a log) with its epoch, route and a hash of
# the original intent, and the next run polls it ONCE before deciding anything. A done job records
# the txid and drops the job id. A 404/410 turns the record into a tombstone: outcome unknown until
# an authoritative read (/me, /challenge answered=true, the explorer) settles it. Never a resend.
# The job id is a bearer token for its lifetime, so output only ever shows its first 6 characters.
BUSY_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
BUSY_JOBS_FILE = "busy-jobs.json"


def _busy_jobs_path() -> pathlib.Path:
    return POA_DIR / BUSY_JOBS_FILE


def load_busy_jobs() -> list:
    f = _busy_jobs_path()
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    except Exception:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save_busy_jobs(rows: list) -> None:
    POA_DIR.mkdir(parents=True, exist_ok=True)
    f = _busy_jobs_path()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    lock_file_permissions(tmp)
    os.replace(tmp, f)


def mask_job_id(job_id) -> str:
    j = str(job_id or "")
    return (j[:6] + "…") if j else ""


def remember_busy_job(err, route: str, epoch=None, intent: str = "", assignment: str = None) -> dict:
    """Save a still-queued (or already-expired) busy job so a later run can resolve it. No-op
    without a job id (e.g. a bare 409 duplicate_in_flight)."""
    job_id = str(getattr(err, "job_id", "") or "")
    if not job_id:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = [r for r in load_busy_jobs() if r.get("job_id") != job_id]
    rec = {"job_id": job_id, "route": route, "epoch": epoch, "assignment": assignment,
           "intent_sha256": hashlib.sha256((intent or "").encode("utf-8")).hexdigest(),
           "saved_at": now, "state": "tombstone" if getattr(err, "expired", False) else "queued",
           "last_poll_at": None, "last_poll": None, "txid": None}
    rows.append(rec)
    save_busy_jobs(rows)
    return rec


def check_busy_jobs(epoch=None, poll=None) -> list:
    """Poll each saved queued job ONCE (optionally only one epoch's). Never resends anything.
    Returns per-job outcomes: done (txid), queued, tombstone (expired, outcome unknown), invalid-id."""
    poll = poll or (lambda path: api_call("GET", path, timeout=30, retries=1))
    rows = load_busy_jobs()
    out = []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for r in rows:
        if epoch is not None and r.get("epoch") != epoch:
            continue
        if r.get("state") != "queued":
            out.append(dict(r, job_id=mask_job_id(r.get("job_id")))); continue
        jid = str(r.get("job_id") or "")
        if not BUSY_JOB_ID_RE.match(jid):
            r["state"] = "invalid-id"
            r["last_poll"] = "job id is not 32 lowercase hex; not polled"
            out.append(dict(r, job_id=mask_job_id(jid))); continue
        r["last_poll_at"] = now
        try:
            res = poll(f"/api/job/{jid}")
        except PoaHTTPError as e:
            if e.status in (404, 410):
                r["state"] = "tombstone"
                r["last_poll"] = f"HTTP {e.status}: job result expired; outcome unknown until /me, /challenge or the explorer settles it"
            else:
                r["last_poll"] = f"poll failed ({e.kind}); still queued, try again next run"
            out.append(dict(r, job_id=mask_job_id(jid))); continue
        if is_busy_receipt(res):
            r["last_poll"] = "still queued"
        elif isinstance(res, dict) and res.get("error"):
            r["state"] = "failed"
            r["last_poll"] = redact(str(res.get("error")))[:200]
            r["job_id"] = None
        else:
            txid = (res.get("txid") or res.get("id")) if isinstance(res, dict) else None
            r["state"], r["txid"], r["last_poll"] = "done", txid, "done"
            r["job_id"] = None  # resolved: the bearer id is no longer needed, so drop it
            if r.get("route") == "/api/hosted/answer" and r.get("epoch") is not None:
                save_epoch_cache(r["epoch"], send_outcome="accepted", landed=True, txid_answer=txid)
        out.append(dict(r, job_id=mask_job_id(jid)))
    save_busy_jobs(rows)
    return out


def submit_answer_safely(text: str, epoch) -> dict:
    """Send the answer ONCE for this epoch and record it in ~/.poa/cache/epoch-<n>.json.

    - If the cache already records a send for this epoch, do NOT send again (returns sent_before).
    - Duplicate reply = landed. Stale-seed/closed-epoch reply = stop (landed False, no retry).
    - Timeout / 5xx = unknown: confirm via the keyless explorer, never blind-resend.
    MCP is used only when POA_MCP_FALLBACK=1 and REST failed before reaching the server (network).
    """
    cache = load_epoch_cache(epoch)
    if cache.get("send_outcome") == "queued" and any(r.get("epoch") == epoch and r.get("state") == "queued" for r in load_busy_jobs()):
        # #79: poll the saved busy job once before anything else; it can only settle, never resend.
        for r in check_busy_jobs(epoch):
            if r.get("route") == "/api/hosted/answer" and r.get("state") == "done":
                return {"landed": True, "how": "busy_job", "txid": r.get("txid"),
                        "note": f"queued answer for epoch {epoch} resolved from its saved busy job; not resending"}
        cache = load_epoch_cache(epoch)
    if cache.get("sent_at") and cache.get("send_outcome") not in ("halted", "rejected", "stale"):
        return {"landed": cache.get("landed"), "how": "sent_before", "note": f"already sent for epoch {epoch} at {cache['sent_at']} ({cache.get('send_outcome')}); not resending",
                "txid": cache.get("txid_answer")}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_epoch_cache(epoch, sent_at=now, send_outcome="sending", answer_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    try:
        try:
            res = hosted_write(busy_aware(lambda: api_call("POST", "/api/hosted/answer", body={"answer": text}, timeout=60, retries=1), "/api/hosted/answer"))
        except PoaHTTPError as e:
            if e.kind == "network" and mcp_fallback_enabled():
                # MCP poa_answer goes through the node queue too (notice 2026-10-29-mcp-queue):
                # a {job_id, poll_url} reply is accepted, so poll it, never resend.
                res = hosted_write(busy_aware(lambda: mcp_call("poa_answer", {"answer": text}, timeout=60), "mcp:poa_answer"))
            else:
                raise
        txid = res.get("txid") or res.get("id") if isinstance(res, dict) else None
        save_epoch_cache(epoch, send_outcome="accepted", landed=True, txid_answer=txid)
        return {"landed": True, "how": "response", "result": res, "txid": txid}
    except PoaHTTPError as e:
        low = str(e).lower()
        if e.kind == "queued":
            save_epoch_cache(epoch, send_outcome="queued", landed=None, error=str(e)[:300],
                             busy_job=getattr(e, "job_id", None))
            remember_busy_job(e, "/api/hosted/answer", epoch=epoch, intent=text)
            return {"landed": None, "how": "queued", "kind": "queued", "error": str(e)[:300],
                    "note": "accepted by a busy node; not resending. Confirm on the next run."}
        if e.kind in ("nonce", "nonce-lag", "busy"):
            save_epoch_cache(epoch, send_outcome="rejected", landed=False, error=str(e)[:300])
            return {"landed": False, "how": e.kind, "error": str(e)[:300], "kind": e.kind,
                    "note": "not landed (nonce slot); safe to send again on the next run"}
        if any(m in low for m in DUP_MARKERS):
            save_epoch_cache(epoch, send_outcome="duplicate", landed=True)
            return {"landed": True, "how": "duplicate", "note": "Server says this epoch is already answered."}
        if any(m in low for m in STALE_MARKERS):
            rotated = ("seed_rotated_this_epoch" in low) or ("stale_seed" in low) or ("stale seed" in low)
            save_epoch_cache(epoch, send_outcome="stale", landed=False, seed_rotated_this_epoch=rotated, error=str(e)[:300])
            return {"landed": False, "how": "stale", "seed_rotated_this_epoch": rotated, "error": str(e)[:300], "kind": e.kind}
        if e.kind == "halted":
            save_epoch_cache(epoch, send_outcome="halted", landed=False, error=str(e)[:300])
            return {"landed": False, "how": "halted", "error": str(e)[:300], "kind": "halted"}
        if e.kind in ("auth", "client"):
            save_epoch_cache(epoch, send_outcome="rejected", landed=False, error=str(e)[:300])
            return {"landed": False, "error": str(e)[:300], "kind": e.kind}
        save_epoch_cache(epoch, send_outcome=f"unknown-{e.kind}", error=str(e)[:300])
        return {"landed": None, "how": "unknown", "kind": e.kind, "error": str(e)[:300]}


def grade_shown_on_assignments(txid: str) -> dict:
    """A grade counts as done only when a successful /assignments read no longer deals this seat."""
    try:
        seats = read_open_seats()
    except PoaHTTPError as e:
        return {"confirmed": None, "error": str(e)[:200]}
    return {"confirmed": txid not in seats, "open_seats": len(seats)}


# Server release v1.12-gradecap (Oct 27, 2026, 8:51 PM PT): a global grade ceiling
# (grade_rpm_global, about 90 grades per minute across all graders and IPs) is checked before
# replay. A grade refused with 429 (or a body naming the ceiling) did not land.
GRADE_RATE_LIMIT_MARKERS = ("grade_rpm_global", "grade ceiling", "grade rate", "grades per minute")
GRADE_RATE_LIMIT_DEFAULT_WAIT_S = 60


def is_grade_rate_limited(e) -> bool:
    if getattr(e, "status", None) == 429:
        return True
    low = str(e).lower()
    return any(m in low for m in GRADE_RATE_LIMIT_MARKERS)


def submit_grade_safely(txid: str, score: int, reasoning: str) -> dict:
    """Send a grade ONCE, then count it done only when /assignments no longer deals the seat.
    landed: True (assignments confirms) / None (sent or ambiguous, not yet confirmed) / False (rejected)."""
    sent = None
    try:
        res = hosted_write(busy_aware(lambda: api_call("POST", "/api/hosted/grade", body={"assignment": txid, "score": score, "reasoning": reasoning}, timeout=60, retries=1), "/api/hosted/grade"))
        sent = {"how": "response", "result": res}
    except PoaHTTPError as e:
        low = str(e).lower()
        if e.kind in ("nonce", "nonce-lag", "busy"):
            return {"landed": False, "how": e.kind, "error": str(e)[:300], "kind": e.kind,
                    "note": "not landed (nonce slot); safe to grade this seat again on the next run"}
        if is_grade_rate_limited(e):
            # v1.12-gradecap (Oct 27, 2026): global grade ceiling grade_rpm_global, checked before
            # replay, so a refused grade did not land. Stop and wait out Retry-After.
            return {"landed": False, "how": "rate-limited", "kind": "rate-limited", "error": str(e)[:300],
                    "retry_after": e.retry_after if e.retry_after is not None else GRADE_RATE_LIMIT_DEFAULT_WAIT_S,
                    "note": "not landed (global grade ceiling); safe to grade this seat again after retry_after seconds"}
        if e.kind == "queued":
            # Accepted by a busy node; the assignments read-back below decides, never a resend.
            sent = {"how": "queued", "error": str(e)[:300], "kind": "queued"}
            remember_busy_job(e, "/api/hosted/grade", intent=f"{txid}:{score}:{reasoning}", assignment=txid)
        elif "duplicate sealed grade" in low or "already graded" in low:
            sent = {"how": "duplicate"}
        elif e.kind == "network" and mcp_fallback_enabled():
            try:
                sent = {"how": "mcp", "result": hosted_write(busy_aware(lambda: mcp_call("poa_grade", {"assignment": txid, "score": score, "reasoning": reasoning}, timeout=60), "mcp:poa_grade"))}
            except PoaHTTPError as e2:
                if e2.kind == "queued":
                    # Accepted via the MCP queue; the assignments read-back decides, never a resend.
                    sent = {"how": "queued", "error": str(e2)[:300], "kind": "queued"}
                    remember_busy_job(e2, "/api/hosted/grade", intent=f"{txid}:{score}:{reasoning}", assignment=txid)
                else:
                    sent = {"how": "unknown", "error": str(e2)[:300]}
        elif e.kind == "halted":
            return {"landed": False, "how": "halted", "error": str(e)[:300], "kind": "halted"}
        elif e.kind in ("auth", "client"):
            return {"landed": False, "error": str(e)[:300], "kind": e.kind}
        else:
            sent = {"how": "unknown", "error": str(e)[:300], "kind": e.kind}
    conf = {"confirmed": None}
    for r in range(_verify_rounds()):
        conf = grade_shown_on_assignments(txid)
        if conf.get("confirmed") is not None:
            break
        _backoff(r)
    out = dict(sent)
    out["assignments_check"] = conf
    if conf.get("confirmed"):
        out["landed"] = True
    elif conf.get("confirmed") is False and sent["how"] == "unknown":
        out["landed"] = False
        out["note"] = "seat still dealt after an ambiguous send; safe to grade again next run"
    else:
        out["landed"] = None
        out["note"] = "not counted as done until /assignments confirms; do not resend now"
    return out


def safe_mine() -> dict:
    """Mine once. /mine is a countersign sweep (re-running is harmless, it re-verifies pending
    txs), but we still don't hammer a down server: one try, then MCP if flagged.
    Hourly lock: at most one mine per local clock hour per machine. An empty lock file
    $POA_HOME/mine-locks/mined-YYYYMMDDHH (default ~/.poa) enforces it; a second call in the
    same hour returns kind=already-mined-this-hour with no network call. The filename holds
    only the hour (no address or key). Set POA_MINE_HOURLY_LOCK=0 to disable."""
    if os.environ.get("POA_MINE_HOURLY_LOCK", "1") != "0":
        import time as _t
        lockdir = POA_DIR / "mine-locks"
        lockdir.mkdir(parents=True, exist_ok=True)
        hour = _t.strftime("%Y%m%d%H")
        for old_lock in lockdir.glob("mined-*"):
            if old_lock.name < f"mined-{hour}" and old_lock.name[6:14] < hour[:8]:
                try: old_lock.unlink()
                except OSError: pass
        try:
            fd = os.open(str(lockdir / f"mined-{hour}"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return {"ok": False, "kind": "already-mined-this-hour", "skipped": True,
                    "note": "mine already ran this clock hour on this machine; not calling /mine again"}
    try:
        return hosted_write(lambda: api_call("GET", "/api/hosted/mine", timeout=60, retries=1))
    except PoaHTTPError as e:
        if e.kind in ("server", "timeout", "network") and mcp_fallback_enabled():
            r = hosted_write(lambda: mcp_call("poa_mine", {}, timeout=60))
            r["_rest_error"] = str(e)[:200]
            return r
        raise


# ---------------------------------------------------------------------------
# Handoff helpers: PC silence + takeover decision for 6-hour epochs
# ---------------------------------------------------------------------------
EPOCH_SECONDS = int(os.environ.get("POA_EPOCH_SECONDS", "21600"))
EPOCH_ANCHOR_UTC = os.environ.get("POA_EPOCH_ANCHOR_UTC", "2026-10-24T21:01:00+00:00")
SEGMENT2_GENESIS_DT = datetime.datetime.fromisoformat("2026-10-24T21:00:00+00:00")


def _parse_log_ts(ts: str):
    try:
        return datetime.datetime.fromisoformat(ts.strip())
    except Exception:
        return None


def handoff_lines(log_path: pathlib.Path) -> list:
    out = []
    if not log_path.is_file():
        return out
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 4:
            continue
        ts = _parse_log_ts(parts[0])
        if not ts or ts.tzinfo is None:
            continue
        fields = {}
        for f in parts[1:]:
            if "=" in f:
                k, v = f.split("=", 1)
                fields[k.strip()] = v.strip()
        fields["ts"] = ts
        out.append(fields)
    return out


def pc_silence(log_path: pathlib.Path, node: str, hours: float) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [r for r in handoff_lines(log_path) if r.get("node") == node]
    hb = [r for r in rows if r.get("event") == "HEARTBEAT"]
    last_hb = max((r["ts"] for r in hb), default=None)
    last_any = max((r["ts"] for r in rows), default=None)
    age_h = None if last_hb is None else round((now - last_hb).total_seconds() / 3600, 2)
    return {
        "node": node,
        "log": str(log_path),
        "last_heartbeat": last_hb.isoformat() if last_hb else None,
        "last_line": last_any.isoformat() if last_any else None,
        "heartbeat_age_hours": age_h,
        "threshold_hours": hours,
        "silent": last_hb is None or age_h > hours,
    }


def epoch_window(now=None, epochs_res=None, fetch_epochs: bool = True, current_epoch=None,
                 epoch_seconds: float = None) -> dict:
    """Start of the epoch that is open now.

    Preferred anchor: when the open epoch is N and GET /api/epochs has the close of N-1, the
    window started exactly at that close, however long ago (an overdue close keeps the same
    epoch open, so its age keeps growing). The old behaviour rolled a fixed 6h grid forward
    from the newest close, which reset the age to ~0 whenever a close ran late and made the
    takeover check wait forever. The fixed grid is now only a fallback, flagged
    `anchor_exact=false`, for when the epoch number or the previous close is unknown."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    step = float(epoch_seconds or EPOCH_SECONDS)
    if epochs_res is None and fetch_epochs:
        try:
            epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=15, retries=1)
        except Exception:
            epochs_res = None
    eps = _epoch_list(epochs_res) if epochs_res is not None else []
    eps = [e for e in eps if isinstance(e.get("ts"), (int, float))]
    if current_epoch is not None and eps:
        try:
            cur = int(current_epoch)
        except (TypeError, ValueError):
            cur = None
        prev = next((e for e in eps if int(e["epoch"]) == cur - 1), None) if cur is not None else None
        if prev is not None:
            start = datetime.datetime.fromtimestamp(int(prev["ts"]), datetime.timezone.utc)
            age_h = round((now - start).total_seconds() / 3600, 2)
            return {
                "window_start": start.isoformat(),
                "window_age_hours": age_h,
                "anchor_source": f"epochs:E{prev['epoch']}-close",
                "anchor_exact": True,
                "last_close_utc": start.isoformat(),
                "close_overdue": age_h * 3600 > step * 1.1,
            }
    if eps:
        close_dt = datetime.datetime.fromtimestamp(int(eps[-1]["ts"]), datetime.timezone.utc)
        if close_dt <= now:
            k = int((now - close_dt).total_seconds() // step)
            start = close_dt + datetime.timedelta(seconds=k * step)
            return {
                "window_start": start.isoformat(),
                "window_age_hours": round((now - start).total_seconds() / 3600, 2),
                "anchor_source": f"epochs:E{eps[-1]['epoch']}+grid",
                "anchor_exact": k == 0,
                "last_close_utc": close_dt.isoformat(),
            }
    anchor = datetime.datetime.fromisoformat(EPOCH_ANCHOR_UTC)
    k = int((now - anchor).total_seconds() // step)
    start = anchor + datetime.timedelta(seconds=k * step)
    return {
        "window_start": start.isoformat(),
        "window_age_hours": round((now - start).total_seconds() / 3600, 2),
        "anchor_source": "static_genesis",
        "anchor_exact": False,
    }


def cmd_takeover_check(args: argparse.Namespace) -> None:
    after_min = float(getattr(args, "after_min", None) or os.environ.get("POA_TAKEOVER_AFTER_MIN", "45"))
    log_path = pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG
    sil = pc_silence(log_path, args.node, args.hours)
    out = {"pc": sil, "clock": None, "epoch": None, "answered": None, "pc_claim_or_answer": False}
    try:
        ch = read_challenge()
        out["epoch"] = ch.get("epoch")
        out["words"] = ch.get("words", [])
        out["answered"] = is_challenge_answered(ch, ch.get("epoch"))
        if ch.get("_via"):
            out["via"] = ch["_via"]
    except PoaHTTPError as e:
        out["api_error"] = {"kind": e.kind, "error": str(e)[:300]}
        if e.retry_after is not None:
            out["api_error"]["retry_after"] = e.retry_after
        edge_ch = getattr(e, "edge_challenge", None)
        if isinstance(edge_ch, dict) and edge_ch.get("epoch") is not None:
            out["edge_epoch"] = edge_ch.get("epoch")
            out["edge_words"] = edge_ch.get("words", [])
    seconds = None
    try:
        seconds = _f((api_call("GET", "/api/params", need_key=False, timeout=15, retries=1) or {}).get("auto_epoch_seconds")) or None
    except Exception:
        seconds = None
    win = epoch_window(current_epoch=out["epoch"] if out["epoch"] is not None else out.get("edge_epoch"),
                       epoch_seconds=seconds)
    out["clock"] = win
    now = datetime.datetime.now(datetime.timezone.utc)
    if out["epoch"] is not None:
        claim_ttl = datetime.timedelta(minutes=15)
        for r in handoff_lines(log_path):
            if r["ts"] < SEGMENT2_GENESIS_DT:
                continue
            if r.get("node") == args.node and r.get("epoch") == str(out["epoch"]):
                if r.get("event") in ("ANSWERED", "SKIP_ANSWERED"):
                    out["pc_claim_or_answer"] = True
                if r.get("event") == "CLAIM" and now - r["ts"] < claim_ttl:
                    out["pc_claim_or_answer"] = True
                    out["pc_claim_fresh"] = True
    if (out.get("api_error") or {}).get("kind") == "halted":
        decision, why = "wait", "operator maintenance halt (HTTP 503); do not submit"
    elif out["epoch"] is None:
        decision, why = "wait", "server state unknown (API unreachable); never answer blind"
    elif out["answered"]:
        decision, why = "skip", "epoch already answered"
    elif out["pc_claim_or_answer"]:
        decision, why = "wait", "PC has a fresh CLAIM or ANSWERED line for this epoch"
    elif sil["silent"]:
        decision, why = "takeover", f"PC heartbeat older than {args.hours}h and epoch unanswered"
    elif win["window_age_hours"] * 60 > after_min:
        decision, why = "takeover", f"epoch window >{after_min:g} min old, unanswered, no PC claim"
    else:
        decision, why = "wait", f"PC is fresh and epoch is <{after_min:g} min old; give it time"
    out["takeover_after_min"] = after_min
    out["decision"] = decision
    out["reason"] = why
    print(json.dumps(out, indent=2))


def cmd_health(args: argparse.Namespace) -> None:
    """Read-only probe of each surface; tells server outage apart from key problems."""
    probes = []

    def probe(name, fn):
        t0 = time.time()
        try:
            r = fn()
            probes.append({"probe": name, "ok": True, "seconds": round(time.time() - t0, 2),
                           "detail": {k: r.get(k) for k in ("epoch", "words", "answered", "superseded", "epochs_closed", "as_of", "tools") if isinstance(r, dict) and k in r}})
        except PoaHTTPError as e:
            row = {"probe": name, "ok": False, "kind": e.kind, "seconds": round(time.time() - t0, 2), "error": str(e)[:200]}
            if e.retry_after is not None:
                row["retry_after"] = e.retry_after
            probes.append(row)
        except Exception as e:
            probes.append({"probe": name, "ok": False, "kind": "other", "seconds": round(time.time() - t0, 2), "error": redact(str(e))[:200]})

    probe("rest:/api/status", lambda: api_call("GET", "/api/status", need_key=False, retries=1, timeout=args.timeout))
    probe("rest:/api/hosted/challenge", lambda: api_call("GET", "/api/hosted/challenge", retries=1, timeout=args.timeout))
    addr = local_address()
    if addr:
        probe("edge:/api/challenge/<addr>", lambda: edge_read_challenge(addr, timeout=int(max(5, args.timeout))))
    probe("mcp:tools/list", lambda: _mcp_list(args.timeout))
    if args.mcp_keyed:
        probe("mcp:poa_challenge", lambda: mcp_call("poa_challenge", {}, timeout=args.timeout))
    kinds = {p["probe"]: (p["kind"] if not p["ok"] else "ok") for p in probes}
    if kinds.get("rest:/api/hosted/challenge") == "ok":
        verdict = "rest-ok"
    elif kinds.get("rest:/api/hosted/challenge") == "auth" and kinds.get("mcp:tools/list") == "auth":
        verdict = "halted"
    elif kinds.get("rest:/api/hosted/challenge") == "auth":
        verdict = "key-rejected"
    elif kinds.get("rest:/api/hosted/challenge") == "halted":
        verdict = "halted"
    elif kinds.get("mcp:tools/list") == "ok":
        verdict = "rest-down-mcp-up"
    else:
        verdict = "server-down"
    out = {"verdict": verdict, "mcp_fallback_enabled": mcp_fallback_enabled(), "probes": probes}
    if kinds.get("rest:/api/hosted/challenge") == "auth" and kinds.get("mcp:tools/list") == "auth":
        out["note"] = "origin returning 401 on keyless mcp:tools/list too (operator maintenance auth gate; your PoA key is fine)"
    print(json.dumps(out, indent=2))


def _origin_auth_gated(timeout: float = 5.0) -> bool:
    """True when keyless mcp:tools/list also returns HTTP 401/403 (origin-wide maintenance auth gate)."""
    try:
        _mcp_list(timeout)
        return False
    except PoaHTTPError as e:
        return e.kind == "auth"
    except Exception:
        return False


def _mcp_list(timeout) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise _classify(e, "mcp:tools/list", "")
    names = sorted(set(re.findall(r'"name"\s*:\s*"(poa_[a-z_]+)"', raw)))
    if not names:
        raise PoaHTTPError("server", f"MCP tools/list returned no poa_ tools: {raw[:200]}")
    return {"tools": names}


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
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(
        FLOOR_MCP_URL,
        data=json.dumps(rpc_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect) if need_key else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
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
        raise RuntimeError(f"Proof Floor MCP call ({tool_name}) failed: {redact(str(e), raw_key)}") from None


LABLEAK_JOIN_PATH = "/.well-known/agentconnect/join"


class LabLeakTokenExpired(RuntimeError):
    """The stored LabLeak token is missing or was rejected, and the kit will not renew it.

    LabLeak's only pk_ login is GET /.well-known/agentconnect/join?guid=<api key>, which puts the
    API key in the URL (server logs, proxies, history). Header-only rule: the key travels only in a
    request header, so the kit never calls that URL. Checked live 2026-10-27: the join endpoint
    ignores an X-Api-Key header and a JSON POST body {"guid": ...} (both just mint an unrelated
    ephemeral guid), so there is no header-only renewal to fall back to."""


LABLEAK_RENEW_HELP = (
    "LabLeak session token {why}. The kit will not renew it: LabLeak's join endpoint only accepts "
    "the PoA API key as a URL query parameter, and this kit sends the key only in a request header. "
    "Header-only renewal is not supported by LabLeak yet. To continue, put a fresh LabLeak token "
    "in {file} (or the POA_LABLEAK_TOKEN environment variable) and run the command again."
)


def load_lableak_token(refresh: bool = False) -> str:
    """Return the stored LabLeak bearer token. Never renews through the URL-key join.

    refresh=True means the server just rejected the current token (401): raise
    LabLeakTokenExpired with instructions instead of sending the API key in a URL."""
    env_tok = os.environ.get("POA_LABLEAK_TOKEN", "").strip()
    if not refresh:
        if env_tok:
            return env_tok
        if LABLEAK_TOKEN_FILE.is_file():
            tok = LABLEAK_TOKEN_FILE.read_text(encoding="utf-8", errors="ignore").strip()
            if tok:
                return tok
        raise LabLeakTokenExpired(LABLEAK_RENEW_HELP.format(why="is missing", file=LABLEAK_TOKEN_FILE))
    raise LabLeakTokenExpired(LABLEAK_RENEW_HELP.format(why="expired (HTTP 401)", file=LABLEAK_TOKEN_FILE))


def lableak_call(method: str, path: str, query: dict = None, body: dict = None, need_auth: bool = False):
    """LabLeak forum request. Auth is the bearer token in a header only; 3xx is refused (never
    followed, so the token is never replayed to a Location); key and token are redacted from errors.
    A 401 does NOT trigger a URL-key re-join: it raises LabLeakTokenExpired."""
    raw_key = ""
    try:
        raw_key = load_key()  # only used to redact; never sent to LabLeak
    except Exception:
        raw_key = ""
    tok = load_lableak_token(refresh=False) if need_auth else ""
    q = dict(query or {})
    for k in list(q):
        if k.lower() in ("key", "api_key", "guid", "token"):
            raise RuntimeError(f"refusing to put credential parameter '{k}' in a LabLeak URL")
    qs = urllib.parse.urlencode(q)
    url = f"{LABLEAK_BASE_URL}{path}" + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if need_auth and tok:
        headers["Authorization"] = f"Bearer {tok}"
    data = None
    if method.upper() in ("POST", "PATCH"):
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        raw = http_request(method.upper(), url, data=data, headers=headers, timeout=30,
                           follow_redirects=False).decode("utf-8", errors="replace")
        return json.loads(redact(redact(raw, raw_key), tok))
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise RuntimeError(f"LabLeak HTTP {e.code} (redirect-refused) on {path}: refused to follow a redirect") from None
        if e.code == 401 and need_auth:
            load_lableak_token(refresh=True)  # always raises LabLeakTokenExpired
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        raise RuntimeError(f"LabLeak HTTP {e.code} on {path}: {redact(redact(err_body, raw_key), tok)}") from None
    except LabLeakTokenExpired:
        raise
    except Exception as e:
        raise RuntimeError(f"LabLeak request failed on {path}: {redact(redact(str(e), raw_key), tok)}") from None


NOTICE_SUSPICIOUS_PATTERNS = (
    r"(?i)\b(?:pk_[A-Za-z0-9_-]*|private\s+key|seed\s+phrase|secret\s+key|\.poa/key)\b",
    r"(?i)\b(?:send\s+poa|transfer\s+\d|poa_send|curl\s+http|wget\s+http)\b",
)


def inspect_notice(sn: dict, now: float = None) -> dict:
    """Inspect a system_notice object for provenance (signed ledger txid vs server-override),
    expiration, and suspicious credential/transfer instructions (SUGGESTIONS.md #20, #46)."""
    if not isinstance(sn, dict) or not sn.get("msg"):
        return {}
    now = time.time() if now is None else float(now)
    msg = redact(str(sn.get("msg", "")))
    txid = sn.get("txid")
    source = sn.get("source") or ("signed-ledger" if txid else "server-override")
    exp = sn.get("expires_ts")
    expired = bool(isinstance(exp, (int, float)) and exp > 0 and now > float(exp))
    suspicious = any(re.search(pat, msg) for pat in NOTICE_SUSPICIOUS_PATTERNS)
    return {
        "id": sn.get("id", ""),
        "severity": sn.get("severity", "info"),
        "msg": msg,
        "txid": txid,
        "source": source,
        "signed_on_ledger": bool(txid),
        "expired": expired,
        "untrusted_warning": suspicious,
    }


def print_notice(obj: dict) -> None:
    if isinstance(obj, dict) and "system_notice" in obj and obj["system_notice"]:
        info = inspect_notice(obj["system_notice"])
        if info:
            tag = f"{info['severity']}" + (",EXPIRED" if info["expired"] else "") + f",src={info['source']}"
            print(f"\nSYSTEM_NOTICE [{tag}] {info['id']}: {info['msg']}")
            if info["untrusted_warning"]:
                print("  [SECURITY WARNING] Notice contains credential/transfer/command keywords — NEVER execute or share keys/funds!")
            else:
                print("  (Notices are informational only. Never share keys or funds.)")


def normalize_seed_slots(text: str, seed_words: list) -> str:
    """Replace each seed word (and simple suffixes) with a fixed token `seedslot` so template reuse
    across epochs is caught even when the 3 seed words change (SUGGESTIONS.md #6)."""
    out = text.lower()
    for w in (seed_words or []):
        w_clean = str(w).strip().lower()
        if w_clean:
            out = re.sub(rf"\b{re.escape(w_clean)}[a-z]*\b", "seedslot", out)
    return out


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


def score_grade3_rubric(text: str, required_words: list, max_overlap: float, inflected: list) -> dict:
    """Scores a candidate answer (0-100) on the 5 criteria peer LLM graders use to award 3/3 over 2/3."""
    lower = text.lower()
    length = len(text)
    # 1. Quantitative / Formal Specificity (0-30 pts)
    quant_patterns = [
        r"\b\d+(?:\.\d+)?%",
        r"(?:>=|<=|>|<|=|\bO\([^)]+\))",
        r"\b(?:quorum|min-cut|max-flow|entropy|variance|eigenvalue|conductance|nullifier|shingle|jaccard|threshold|half-life|exponent|quadratic|bounty|submodular|logarithmic|linear|superlinear)\b",
        r"\b\d+(?:x|-fold|\s*(?:epochs?|bits?|rounds?|hops?|seals?|attesters?|validators?|ms|seconds?|hours?))\b",
    ]
    quant_hits = sum(1 for p in quant_patterns if re.search(p, text, re.I))
    quant_pts = min(30, quant_hits * 10)

    # 2. Verification & Enforcement Clarity: Observable Evidence + Deterministic Consequence (0-25 pts)
    evidence_kw = ("proof", "merkle", "signature", "transcript", "commit", "nullifier", "attestation", "beacon", "seed", "graph", "walk", "latency", "audit", "canary", "entropy", "hash", "vdf", "nonce")
    penalty_kw = ("slash", "burn", "forfeit", "prune", "quarantine", "reject", "cap", "decay", "escrow", "lock", "bounty", "penal", "weight")
    ev_hits = sum(1 for k in evidence_kw if k in lower)
    pen_hits = sum(1 for k in penalty_kw if k in lower)
    verif_pts = min(13, ev_hits * 5) + min(12, pen_hits * 6)

    # 3. Natural Seed-Word Integration & Spread (0-25 pts)
    positions = []
    for w in required_words:
        m = re.search(rf"(?i)\b{re.escape(w)}\b", text)
        if m:
            positions.append(m.start())
    if len(positions) == len(required_words) and len(positions) >= 2 and length > 0:
        positions.sort()
        min_gap = min(positions[i + 1] - positions[i] for i in range(len(positions) - 1))
        span = positions[-1] - positions[0]
        gap_pts = 15 if min_gap >= 25 else (8 if min_gap >= 12 else 2)
        span_pts = 10 if span >= 60 else (5 if span >= 30 else 1)
        seed_pts = gap_pts + span_pts
    elif len(positions) == len(required_words):
        seed_pts = 20
    else:
        seed_pts = 0
    if inflected:
        seed_pts = max(0, seed_pts - 15)

    # 4. Information Density & Length Sweet Spot (0-10 pts)
    tokens = re.findall(r"[a-z0-9]+", lower)
    lex_div = (len(set(tokens)) / len(tokens)) if tokens else 0.0
    len_pts = 6 if 500 <= length <= 592 else (4 if (420 <= length < 500 or 592 < length <= 600) else 1)
    div_pts = 4 if lex_div >= 0.68 else (2 if lex_div >= 0.58 else 0)
    density_pts = len_pts + div_pts

    # 5. Shingle Novelty Bonus (0-10 pts)
    novelty_pts = max(0, round(10 * (1.0 - min(1.0, max_overlap / 0.15))))

    total = quant_pts + verif_pts + seed_pts + density_pts + novelty_pts
    return {
        "rubric_score": int(total),
        "rubric_breakdown": {
            "quantitative_specificity_30": quant_pts,
            "verification_and_enforcement_25": verif_pts,
            "natural_seed_integration_25": seed_pts,
            "density_and_length_10": density_pts,
            "shingle_novelty_10": novelty_pts,
        },
    }


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

    rubric = score_grade3_rubric(text, required_words, max_overlap, inflected)
    style = plain_style_score(text)
    return {
        "passed": passed,
        "length": length,
        "missing_words": missing,
        "inflected_words": inflected,
        "has_mechanism_signal": has_mech,
        "has_attacker_cost_signal": has_cost,
        "max_shingle_overlap": round(max_overlap, 4),
        "rubric_score": rubric["rubric_score"],
        "rubric_breakdown": rubric["rubric_breakdown"],
        "style_score": style["style_score"],
        "style_breakdown": style["style_breakdown"],
        "jargon_per_100_words": style["jargon_per_100_words"],
        "drift": style["drift"],
        "style_flags": style["style_flags"],
        "combined_score": round(0.4 * rubric["rubric_score"] + 0.6 * style["style_score"]),
        "warnings": warnings,
    }


def audit_assignment_seat(seat: dict) -> dict:
    ans = str(seat.get("answer", ""))
    words = seat.get("words", [])
    length = len(ans)
    missing, inflected = classify_seed_words(ans, words)
    injections = [pat for pat in INJECTION_PATTERNS if re.search(pat, ans)]
    farm_hits = [label for pat, label in KNOWN_FARM_PATTERNS if re.search(pat, ans)]

    # Compare against author_prior for single-account template farming (both raw and seed-slot normalized)
    priors = seat.get("author_prior", []) or []
    cur_sh = word_shingles(ans, 3)
    cur_norm_sh = word_shingles(normalize_seed_slots(ans, words), 3)
    max_prior_sim = 0.0
    for p in priors:
        p_txt = str(p.get("answer_text", ""))
        p_words = p.get("seed_words") or p.get("words") or []
        psh = word_shingles(p_txt, 3)
        if cur_sh and psh:
            sim = len(cur_sh & psh) / len(cur_sh | psh)
            if sim > max_prior_sim:
                max_prior_sim = sim
        p_norm_sh = word_shingles(normalize_seed_slots(p_txt, p_words), 3)
        if cur_norm_sh and p_norm_sh:
            nsim = len(cur_norm_sh & p_norm_sh) / len(cur_norm_sh | p_norm_sh)
            if nsim > max_prior_sim:
                max_prior_sim = nsim

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

    pmeta = seat.get("pending_meta") if isinstance(seat.get("pending_meta"), dict) else {}
    out = {
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
    if seat.get("expires_at_epoch") is not None:
        out["expires_at_epoch"] = seat.get("expires_at_epoch")
    if pmeta.get("to_quorum") is not None:
        out["to_quorum"] = pmeta.get("to_quorum")
    return out


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
        asgn_res = hosted_assignments()
        for s in (asgn_res.get("assignments") or []):
            if s.get("answer_txid"):
                open_txids.add(s["answer_txid"])
    except Exception:
        open_txids = None  # If assignments check fails, attempt all items

    sealed = []
    skipped_already_graded = []
    failed = []
    unknown = []

    for idx, item in enumerate(items, 1):
        txid = (item.get("txid") or item.get("answer_txid") or item.get("assignment") or "").strip()
        score = int(item.get("score"))
        note = (item.get("note") or item.get("reasoning") or "").strip()
        if not txid or not note:
            raise RuntimeError(f"Item #{idx} missing txid/answer_txid/assignment or note/reasoning.")
        if open_txids is not None and txid not in open_txids:
            skipped_already_graded.append(txid)
            continue
        r = submit_grade_safely(txid, score, note)
        if r.get("landed"):
            sealed.append({"txid": txid, "score": score, "how": r.get("how"),
                           "grade_id": (r.get("result") or {}).get("id") if isinstance(r.get("result"), dict) else None})
        elif r.get("landed") is None:
            unknown.append({"txid": txid, "score": score, "error": r.get("error")})
        else:
            row = {"txid": txid, "score": score, "error": r.get("error"), "kind": r.get("kind")}
            if r.get("retry_after") is not None:
                row["retry_after"] = r.get("retry_after")
            failed.append(row)
            if r.get("kind") in ("auth", "nonce", "nonce-lag", "busy", "rate-limited"):
                # nonce trouble: stop instead of burning rate limit; the rest stay dealt for the next run
                break
    retried_ok = [x["txid"] for x in sealed if x.get("how") == "read-back"]

    mine_attested = None
    mine_error = None
    if args.mine:
        try:
            mres = safe_mine()
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
        "unknown_count": len(unknown),
        "unknown": unknown,
        "mined_after_batch": mine_attested,
    }
    if mine_error:
        out["mine_error"] = mine_error
    print(json.dumps(out, indent=2))
    if failed or unknown:
        sys.exit(2)


# ---------------------------------------------------------------------------
# Public ledger mirror reader (github.com/EmblemCompany/ProofOfAgent). No key.
# ---------------------------------------------------------------------------
LEDGER_REPO = os.environ.get("POA_LEDGER_REPO", "EmblemCompany/ProofOfAgent")
LEDGER_DEFAULT_BRANCH = os.environ.get("POA_LEDGER_BRANCH", "poa-mainnet-1")
ADDRESS_FILE = POA_DIR / "address"
REF_FILE = POA_DIR / "ref"


def _ledger_get(url: str, timeout: int = 30):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
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


POASCAN_ANCHORS_URL = os.environ.get("POA_POASCAN_ANCHORS_URL", "https://poascan.io/api/v1/anchors")


def _poascan_anchors(timeout: float = 20):
    """Keyless POAScan anchor list (newest first): [{height, chain, tx, state_root, anchored_at, ...}]."""
    raw = http_request("GET", POASCAN_ANCHORS_URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                       timeout=timeout, follow_redirects=False)
    d = json.loads(raw.decode("utf-8", errors="replace"))
    return d if isinstance(d, list) else (d.get("anchors") if isinstance(d, dict) else None) or []


def _iso_age_s(iso, now):
    try:
        return int(now - datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def ledger_status(branch: str, mirror: bool = False, fetch_status=None, fetch_anchors=None, clock=time.time) -> dict:
    """Live checkpoint status (server release v0.21-pusher). Checkpoint height comes from keyless
    /api/status (checkpoint_height, plus stale / stale_age_s when the server serves a cached view)
    and POAScan anchors; the GitHub ledger branch is NOT used for lag (it trails the live roll-ups,
    which land about every 15 minutes, and reported hours of false lag). `mirror=True` adds the
    GitHub mirror's newest checkpoint as information only."""
    fetch_status = fetch_status or (lambda: api_call("GET", "/api/status", need_key=False, timeout=20, retries=2))
    fetch_anchors = fetch_anchors or _poascan_anchors
    now = clock()
    out = {"source": "live: /api/status + POAScan anchors", "branch": branch}
    st, errs = None, {}
    try:
        st = fetch_status()
    except Exception as e:
        errs["status"] = redact(str(e))[:200]
    anchors = []
    try:
        anchors = [a for a in (fetch_anchors() or []) if isinstance(a, dict)]
    except Exception as e:
        errs["anchors"] = redact(str(e))[:200]
    if isinstance(st, dict) and st.get("checkpoint_height") is not None:
        stale = bool(st.get("stale"))
        out["latest_checkpoint"] = {"height": st.get("checkpoint_height"), "state_root": st.get("state_root"),
                                    "as_of": st.get("as_of"), "via": "/api/status"}
        out["stale"] = stale
        out["stale_age_s"] = st.get("stale_age_s") if stale else 0
        out["checkpoint_lag_seconds"] = int(st.get("stale_age_s") or 0) if stale else 0
        out["epochs_closed"] = st.get("epochs_closed")
    latest = {}
    for a in anchors:
        ch = a.get("chain") or "?"
        try:
            h = int(a.get("height"))
        except Exception:
            continue
        if ch not in latest or h > int(latest[ch]["height"]):
            latest[ch] = {"height": h, "anchored_at": a.get("anchored_at"), "tx": a.get("tx"),
                          "state_root": a.get("state_root"), "age_s": _iso_age_s(a.get("anchored_at"), now)}
    out["latest_anchors"] = latest
    if "latest_checkpoint" not in out and latest:
        top = max(latest.values(), key=lambda x: x["height"])
        out["latest_checkpoint"] = {"height": top["height"], "state_root": top["state_root"],
                                    "as_of": top["anchored_at"], "via": "POAScan anchors"}
        out["checkpoint_lag_seconds"] = None
        out["note_lag"] = "/api/status unreadable: lag unknown (anchors trail the live checkpoint by design)"
    if "latest_checkpoint" not in out:
        out["checkpoint_lag_seconds"] = None
        out["status"] = "unknown"
    else:
        out["status"] = "stale" if out.get("stale") else ("ok" if out.get("checkpoint_lag_seconds") == 0 else "unknown")
    out["checkpoint_lag_hours"] = (round(out["checkpoint_lag_seconds"] / 3600, 2)
                                   if isinstance(out.get("checkpoint_lag_seconds"), int) else None)
    if errs:
        out["errors"] = errs
    if mirror:
        try:
            out["github_mirror"] = ledger_mirror_status(branch)
            out["github_mirror"]["note"] = "information only; the mirror trails live checkpoints and is not used for lag"
        except Exception as e:
            out["github_mirror"] = {"error": redact(str(e))[:200]}
    return out


def ledger_mirror_status(branch: str) -> dict:
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
        out["mirror_age_seconds"] = int(now - int(cp.get("ts") or now))
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
        res = ledger_status(b, mirror=getattr(args, "mirror", False))
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
        print(f"Checkpoint status ({res.get('source')})")
        print(f"  checkpoint  h{cp.get('height')} as of {cp.get('as_of')} via {cp.get('via')}")
        lag = res.get("checkpoint_lag_seconds")
        print(f"  status      {res.get('status')}" + (f" (server view stale {lag} s)" if res.get("stale") else "")
              + ("" if lag is not None else " (lag unknown)"))
        for chain, a in (res.get("latest_anchors") or {}).items():
            print(f"  anchor      {chain}: h{a.get('height')} at {a.get('anchored_at')} tx {a.get('tx')}")
        gm = res.get("github_mirror")
        if isinstance(gm, dict) and gm.get("latest_checkpoint"):
            print(f"  mirror      h{gm['latest_checkpoint'].get('height')} (GitHub, information only)")
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


def own_answer_candidates(me: str, log_path: pathlib.Path = None) -> dict:
    """Txids (full or hex prefix) of OUR answers, from public/local sources only (no key):
    keyless explorer address view (type=challenge) and poa-handoff LOG.md ANSWERED/'challenge tx' notes.
    Returns {txid_or_prefix: {"epoch": n|None, "source": str}}."""
    out = {}
    if me:
        d = explorer_get(f"/api/explorer/address/{me}?sort=new")
        for t in (d.get("txs") or []) if isinstance(d, dict) else []:
            if isinstance(t, dict) and t.get("type") == "challenge" and t.get("id"):
                out[t["id"]] = {"epoch": t.get("epoch"), "source": "explorer"}
    lp = log_path or HANDOFF_LOG
    if lp and lp.is_file():
        for line in lp.read_text(encoding="utf-8", errors="ignore").splitlines():
            m_ep = re.search(r"\bepoch=(\d+)", line)
            hits = re.findall(r"challenge tx ([0-9a-f]{8,64})", line)
            if "event=ANSWERED" in line:
                hits += re.findall(r"\btxid[ =]([0-9a-f]{8,64})", line)
            for h in hits:
                out.setdefault(h, {"epoch": int(m_ep.group(1)) if m_ep else None, "source": "handoff-log"})
    return out


def own_pending_rows(pending: dict, cands: dict, quorum: int, ttl: int, close_n) -> list:
    rows = []
    for c, meta in cands.items():
        tx = c if c in pending else next((k for k in pending if k.startswith(c)), None) if len(c) >= 8 else None
        if not tx or any(r["txid"] == tx for r in rows):
            continue
        r = pending[tx] if isinstance(pending[tx], dict) else {}
        panel = len(r.get("panel") or [])
        grades = r.get("grades") or []
        since = r.get("since") if isinstance(r.get("since"), int) else meta.get("epoch")
        # A row settles only via its drawn panel; with every panel seat sealed and seals < quorum it is stuck.
        cannot = panel < quorum
        rows.append({
            "epoch": since, "txid": tx[:16], "panel_size": panel, "grades": len(grades),
            "scores": [g.get("score") for g in grades if isinstance(g, dict)],
            "to_quorum": max(0, quorum - len(grades)),
            "expires_after_close": (since + ttl) if isinstance(since, int) else None,
            "closes_left": ((since + ttl) - close_n) if isinstance(since, int) and isinstance(close_n, int) else None,
            "cannot_settle_under_current_params": cannot,
            "source": meta.get("source"),
        })
    rows.sort(key=lambda x: (x["epoch"] if isinstance(x["epoch"], int) else 10**9))
    return rows


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

    if getattr(args, "mine", False):
        own = own_pending_rows(pending, own_answer_candidates(me, pathlib.Path(args.log).expanduser() if args.log else None),
                               quorum, ttl, close_n)
        res = {"read_only": True, "mode": "mine", "close_epoch": close_n, "grade_quorum": quorum,
               "pending_ttl_epochs": ttl, "address_configured": bool(me),
               "own_pending": len(own), "own_cannot_settle": sum(1 for x in own if x["cannot_settle_under_current_params"]),
               "rows": own,
               "note": ("Public reads only (GET /api/epochs, /api/params, explorer address view) plus the local handoff log for "
                        "txids. cannot_settle = drawn panel smaller than grade_quorum, so the row needs an operator top-up. "
                        "Settled or expired answers drop out of pending and are not listed.")}
        if args.table:
            print(f"close E{close_n}  quorum {quorum}  ttl {ttl}  own pending {len(own)}")
            print("epoch  txid              panel grades need expires  left  status")
            for x in own:
                print(f"E{x['epoch']!s:<5} {x['txid']}  {x['panel_size']:>5} {x['grades']:>6} {x['to_quorum']:>4}  E{x['expires_after_close']!s:<6} {x['closes_left']!s:>4}  "
                      + ("CANNOT SETTLE (panel < quorum)" if x["cannot_settle_under_current_params"] else "can settle"))
        else:
            print(json.dumps(res, indent=2))
        return

    ours_open = [x for x in rows if x["on_our_panel"] and not x["we_sealed"]]
    ours_sealed = [x for x in rows if x["we_sealed"]]
    small = [x for x in rows if x["panel_size"] <= 1]

    seats = None
    if not args.no_key:
        try:
            a = hosted_assignments(timeout=45, retries=1)
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


# ---------------------------------------------------------------------------
# close-health: keyless read of the newest epoch close (SUGGESTIONS.md #39)
# ---------------------------------------------------------------------------
CLOSE_THIN_RATIO = 0.5  # eligible below half the median of earlier closes = thin (signal only)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _epoch_list(epochs_res) -> list:
    eps = epochs_res
    if isinstance(eps, dict):
        eps = eps.get("epochs") or eps.get("items") or [eps]
    out = [e for e in (eps or []) if isinstance(e, dict) and e.get("epoch") is not None]
    out.sort(key=lambda e: int(e.get("epoch")))
    return out


def close_health(epochs_res, k: int = 5, epoch: int = None, address: str = "") -> dict:
    """Summarize one close record from GET /api/epochs. Pure function: no network."""
    eps = _epoch_list(epochs_res)
    if not eps:
        return {"ok": False, "error": "no close records in /api/epochs response"}
    if epoch is None:
        idx = len(eps) - 1
    else:
        idx = next((i for i, e in enumerate(eps) if int(e["epoch"]) == int(epoch)), None)
        if idx is None:
            return {"ok": False, "error": f"epoch {epoch} not found (closes {eps[0]['epoch']}..{eps[-1]['epoch']})"}
    e = eps[idx]
    b = e.get("basis") if isinstance(e.get("basis"), dict) else {}
    atts = b.get("attestations") or {}
    attesters = len(atts) if isinstance(atts, (dict, list)) else 0
    eligible = b.get("challenge_eligible") or []
    pool = _f(e.get("pool"))
    fp = b.get("final_payout") if isinstance(b.get("final_payout"), dict) else {}
    burned = _f(fp.get("burned"))
    rewards = e.get("rewards") if isinstance(e.get("rewards"), dict) else {}
    paid = {a: _f(v) for a, v in rewards.items() if _f(v) > 0}
    earlier = [len((x.get("basis") or {}).get("challenge_eligible") or []) for x in eps[:idx] if int(x["epoch"]) > 0]
    median_prev = statistics.median(earlier) if earlier else None
    ts = e.get("ts")
    prev_ts = eps[idx - 1].get("ts") if idx > 0 else None

    flags = []
    if attesters < k:
        flags.append("attesters_below_k")
    if median_prev and len(eligible) < CLOSE_THIN_RATIO * median_prev:
        flags.append("eligible_thin")
    if pool and burned / pool > 0.75:
        flags.append("mostly_burned")
    verdict = "halted-like" if "attesters_below_k" in flags else ("thin" if flags else "healthy")

    res = {
        "ok": True,
        "read_only": True,
        "epoch": int(e["epoch"]),
        "closed_at": _iso(ts) if ts else None,
        "hours_since_previous_close": round((ts - prev_ts) / 3600, 2) if isinstance(ts, (int, float)) and isinstance(prev_ts, (int, float)) else None,
        "verdict": verdict,
        "flags": flags,
        "distinct_attesters": attesters,
        "k": k,
        "challenge_eligible": len(eligible),
        "median_eligible_earlier_closes": median_prev,
        "settled": len(b.get("settled") or []),
        "pending": len(b.get("pending") or {}),
        "established_after": len(b.get("established_after") or []),
        "established_removed": len(b.get("established_removed") or []),
        "slashed": len(b.get("slashed") or []),
        "ejected": len(b.get("ejected") or []),
        "addresses_paid": len(paid),
        "rewards_total": f"{sum(paid.values()):.6f}",
        "pool": f"{pool:.6f}",
        "burned": f"{burned:.6f}",
        "burned_share": round(burned / pool, 4) if pool else None,
        "note": ("Public read of GET /api/epochs only. halted-like = fewer distinct attesters than K; "
                 "thin = challenge-eligible under half the median of earlier closes, or over 75% of the pool burned. "
                 "These are signals for humans, not anything the network itself uses."),
    }
    me = (address or "").strip()
    if me:
        res["you"] = {
            "challenge_eligible": me in eligible,
            "attested": me in atts,
            "reward": f"{_f(rewards.get(me)):.6f}",
            "established_after": me in (b.get("established_after") or []),
            "established_removed": me in (b.get("established_removed") or []),
            "slashed": me in (b.get("slashed") or []),
            "ejected": me in (b.get("ejected") or []),
        }
    return res


def print_close_health(r: dict) -> None:
    if not r.get("ok"):
        print(f"close-health: {r.get('error')}")
        return
    print(f"E{r['epoch']} closed {r['closed_at']} ({r['hours_since_previous_close']}h after the previous close): {r['verdict'].upper()}"
          + (f"  [{', '.join(r['flags'])}]" if r["flags"] else ""))
    print(f"  attesters {r['distinct_attesters']} vs K {r['k']} | eligible {r['challenge_eligible']} (earlier median {r['median_eligible_earlier_closes']})"
          f" | settled {r['settled']} | pending {r['pending']}")
    print(f"  established +{r['established_after']} / removed {r['established_removed']} | slashed {r['slashed']} | ejected {r['ejected']}")
    print(f"  paid {r['addresses_paid']} addresses {r['rewards_total']} POA | burned {r['burned']} of {r['pool']} ({r['burned_share']})")
    y = r.get("you")
    if y:
        print(f"  you: eligible {y['challenge_eligible']} | attested {y['attested']} | reward {y['reward']} | "
              f"established_after {y['established_after']} | removed {y['established_removed']} | slashed {y['slashed']}")


def cmd_close_health(args: argparse.Namespace) -> None:
    k = args.k
    if k is None:
        try:
            k = int(api_call("GET", "/api/status", need_key=False, timeout=20, retries=1).get("K", 5) or 5)
        except Exception:
            k = 5
    try:
        epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": redact(str(ex))}) if args.json else f"close-health: could not read /api/epochs: {redact(str(ex))}")
        sys.exit(2)
    me = "" if args.no_address else (args.address or local_address())
    r = close_health(epochs_res, k=k, epoch=args.epoch, address=me)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_close_health(r)
    if not r.get("ok"):
        sys.exit(2)
    if r["verdict"] != "healthy":
        sys.exit(3)


# ---------------------------------------------------------------------------
# close-check: per close, was OUR answer graded, did it pass, were we paid? (keyless)
# ---------------------------------------------------------------------------
def own_answers_by_epoch(explorer_txs: list, log_text: str = "", since_dt=None) -> dict:
    """{epoch: txid} for OUR challenge answers in the current chain segment.
    Sources: keyless explorer address view (type=challenge) first, then handoff LOG.md
    ANSWERED lines dated after `since_dt` (segment genesis), so old-segment epoch numbers
    never collide with new ones. Pure function."""
    out = {}
    for t in explorer_txs or []:
        if isinstance(t, dict) and t.get("type") == "challenge" and t.get("id") and t.get("epoch") is not None:
            out.setdefault(int(t["epoch"]), t["id"])
    for line in (log_text or "").splitlines():
        if "event=ANSWERED" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        ts = _parse_log_ts(parts[0]) if parts else None
        if since_dt is not None and (ts is None or ts.tzinfo is None or ts < since_dt):
            continue
        m_ep = re.search(r"\bepoch=(\d+)", line)
        m_tx = re.search(r"\btxid[ =]([0-9a-f]{8,64})", line)
        if m_ep and m_tx:
            out.setdefault(int(m_ep.group(1)), m_tx.group(1))
    return out


def own_answers_all(explorer_txs: list, log_text: str = "", since_dt=None, epochs_res=None, address: str = "") -> dict:
    """{txid: born_epoch} for EVERY one of our challenge answers in the current segment
    (explorer view, handoff ANSWERED lines after `since_dt`, and settled rows whose `from`
    is our address). Unlike own_answers_by_epoch, several answers per epoch are all kept.
    Pure function."""
    out = {}
    for t in explorer_txs or []:
        if isinstance(t, dict) and t.get("type") == "challenge" and t.get("id") and t.get("epoch") is not None:
            out.setdefault(t["id"], int(t["epoch"]))
    for line in (log_text or "").splitlines():
        if "event=ANSWERED" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        ts = _parse_log_ts(parts[0]) if parts else None
        if since_dt is not None and (ts is None or ts.tzinfo is None or ts < since_dt):
            continue
        m_ep = re.search(r"\bepoch=(\d+)", line)
        m_tx = re.search(r"\btxid[ =]([0-9a-f]{8,64})", line)
        if m_ep and m_tx:
            tx = m_tx.group(1)
            if not any(k.startswith(tx) or tx.startswith(k) for k in out):
                out[tx] = int(m_ep.group(1))
    me = (address or "").strip()
    if me:
        for e in _epoch_list(epochs_res):
            for srow in (e.get("basis") or {}).get("settled") or []:
                if isinstance(srow, dict) and srow.get("from") == me and srow.get("answer"):
                    a = srow["answer"]
                    short = next((k for k in out if a.startswith(k) and k != a), None)
                    if short:  # upgrade a short log txid to the full one
                        out[a] = out.pop(short)
                    out.setdefault(a, None)
    return out


def answer_ledger(epochs_res, answers, address: str = "", quorum: int = 4) -> list:
    """Every one of our answers checked against EVERY close record, not only the newest.
    `answers`: {txid: born_epoch} or {born_epoch: txid}. Pure function; no address in output."""
    eps = _epoch_list(epochs_res)
    if answers and all(isinstance(k, int) for k in answers):
        answers = {v: k for k, v in answers.items() if v}
    settled_at = {}
    for e in eps:
        for srow in (e.get("basis") or {}).get("settled") or []:
            if isinstance(srow, dict) and srow.get("answer"):
                settled_at.setdefault(srow["answer"], (int(e["epoch"]), srow))
    pending = ((eps[-1].get("basis") or {}).get("pending") or {}) if eps else {}
    last_close = int(eps[-1]["epoch"]) if eps else None
    out = []
    for tx, born in sorted((answers or {}).items(), key=lambda kv: (kv[1] is None, kv[1] if kv[1] is not None else 0, kv[0])):
        row = {"txid": tx[:12], "born_epoch": born, "status": "missing", "settled_at_close": None, "passed": None,
               "grades": 0, "panel": None, "pending_since": None, "flags": []}
        hit = _find_tx(settled_at.keys(), tx)
        if hit:
            at, srow = settled_at[hit]
            if row["born_epoch"] is None and srow.get("epoch") is not None:
                row["born_epoch"] = srow.get("epoch")
            row.update(status="settled", settled_at_close=at, passed=bool(srow.get("passed")),
                       grades=len(srow.get("jury") or []))
            if not row["passed"]:
                row["flags"].append("answer_failed")
        else:
            ph = _find_tx(pending.keys(), tx)
            if ph:
                pr = pending[ph] if isinstance(pending[ph], dict) else {}
                row.update(status="pending", grades=len(pr.get("grades") or []), panel=len(pr.get("panel") or []),
                           pending_since=pr.get("since"))
                if row["panel"] < quorum:
                    row["flags"].append("panel_below_quorum")
                if row["panel"] == 0:
                    row["flags"].append("no_panel_drawn")
            elif born is not None and last_close is not None and born > last_close:
                row["status"] = "open"  # its epoch has not closed yet
            else:
                row["flags"].append("answer_not_in_settled_or_pending")
        out.append(row)
    return out


def established_from_closes(epochs_res, address: str) -> dict:
    """Established status as the close records show it: the last close listing us in
    established_after (True) or established_removed/slashed/ejected (False). A passed or
    paid answer is NOT taken as established. None = never listed either way."""
    me = (address or "").strip()
    state, at, passed_closes = None, None, []
    for e in _epoch_list(epochs_res):
        b = e.get("basis") or {}
        n = int(e["epoch"])
        if me and any(isinstance(r, dict) and r.get("from") == me and r.get("passed") for r in b.get("settled") or []):
            passed_closes.append(n)
        if me and me in (b.get("established_after") or []):
            state, at = True, n
        if me and (me in (b.get("established_removed") or []) or me in (b.get("slashed") or []) or me in (b.get("ejected") or [])):
            state, at = False, n
    return {"established": state, "last_change_close": at, "closes_with_a_passed_answer": passed_closes,
            "passed_but_not_established": bool(passed_closes) and state is not True}


def _find_tx(keys, txid: str):
    if not txid:
        return None
    if txid in keys:
        return txid
    return next((k for k in keys if k.startswith(txid)), None) if len(txid) >= 8 else None


def close_check(epochs_res, answers: dict, address: str, last: int = 4, quorum: int = 4, current_epoch=None,
                all_answers: dict = None) -> dict:
    """Pure function. For each of the last `last` closes: did our answer for that epoch get
    graded, pass, and get paid; which of OUR answers (born in any epoch) settled AT that close;
    how much was paid to us; standing changes. `answers` is {born_epoch: txid}; `all_answers`
    ({txid: born_epoch}) adds every other answer of ours, e.g. a second answer in one epoch.
    Every answer is also checked against every close record (`answers_all`).
    Output never contains the address, the key or a balance (only per-close payouts)."""
    eps = _epoch_list(epochs_res)
    if not eps:
        return {"ok": False, "error": "no close records in /api/epochs response"}
    me = (address or "").strip()
    settled_at, pending_latest = {}, (eps[-1].get("basis") or {}).get("pending") or {}
    for e in eps:
        for srow in (e.get("basis") or {}).get("settled") or []:
            if isinstance(srow, dict) and srow.get("answer"):
                settled_at[srow["answer"]] = (int(e["epoch"]), srow)
    merged = dict(all_answers or {})
    for ep_n, tx in (answers or {}).items():
        if tx and not any(k.startswith(tx) or tx.startswith(k) for k in merged):
            merged[tx] = int(ep_n)
    ledger = answer_ledger(eps, merged, me, quorum)
    rows = []
    for e in eps[-max(1, last):]:
        n = int(e["epoch"])
        b = e.get("basis") or {}
        rewards = e.get("rewards") if isinstance(e.get("rewards"), dict) else {}
        reward = _f(rewards.get(me)) if me else 0.0
        tx = answers.get(n)
        row = {"close": n, "closed_at": _iso(e["ts"]) if e.get("ts") else None,
               "answer_txid": (tx or "")[:12] or None, "answer": "none", "graded": False, "grades": 0,
               "panel": None, "passed": None, "settled_at_close": None,
               "paid": reward > 0, "payout": f"{reward:.6f}",
               "challenge_eligible": bool(me) and me in (b.get("challenge_eligible") or []),
               "challenge_share": bool(me) and me in (e.get("challenges") or {}),
               "attested": bool(me) and me in (b.get("attestations") or {}),
               "established_after": bool(me) and me in (b.get("established_after") or []),
               "established_removed": bool(me) and me in (b.get("established_removed") or []),
               "slashed": bool(me) and me in (b.get("slashed") or []),
               "flags": []}
        here = [{"txid": a["txid"], "born_epoch": a["born_epoch"], "passed": a["passed"]}
                for a in ledger if a["settled_at_close"] == n]
        row["settled_here"] = here
        row["passed_here"] = sum(1 for a in here if a["passed"])
        if row["passed_here"] and not row["established_after"]:
            row["flags"].append("passed_not_established")
        if tx:
            hit = _find_tx(settled_at.keys(), tx)
            if hit:
                at, srow = settled_at[hit]
                row.update(answer="settled", passed=bool(srow.get("passed")), settled_at_close=at,
                           graded=True, grades=len(srow.get("jury") or []))
            else:
                ph = _find_tx(pending_latest.keys(), tx)
                if ph:
                    pr = pending_latest[ph] if isinstance(pending_latest[ph], dict) else {}
                    grades = pr.get("grades") or []
                    row.update(answer="pending", grades=len(grades), graded=bool(grades),
                               panel=len(pr.get("panel") or []))
                    if row["panel"] < quorum:
                        row["flags"].append("panel_below_quorum")
                    if row["panel"] == 0:
                        row["flags"].append("no_panel_drawn")
                else:
                    row["answer"] = "missing"
                    row["flags"].append("answer_not_in_settled_or_pending")
        else:
            row["flags"].append("no_answer_recorded")
        if row["passed"] is False:
            row["flags"].append("answer_failed")
        if not row["paid"]:
            row["flags"].append("not_paid")
        if row["established_removed"]:
            row["flags"].append("established_removed")
        if row["slashed"]:
            row["flags"].append("slashed")
        rows.append(row)
    newest = rows[-1]
    open_ep = current_epoch if current_epoch is not None else int(eps[-1]["epoch"]) + 1
    res = {
        "ok": True, "read_only": True, "newest_close": newest["close"], "open_epoch": open_ep,
        "open_epoch_answer_txid": (answers.get(int(open_ep)) or "")[:12] or None,
        "paid_at_newest_close": newest["paid"],
        "paid_closes": sum(1 for r in rows if r["paid"]), "closes_checked": len(rows),
        "answers_pending": sum(1 for a in ledger if a["status"] == "pending"),
        "answers_stuck_no_panel": sum(1 for a in ledger if "no_panel_drawn" in a["flags"]),
        "panel_field_note": ("the pending record's panel is filled when the answer is first recorded and not updated; most answers "
                             "that settle had an empty panel there one close earlier, so no_panel_drawn is informational, not a block"),
        "answers_total": len(ledger),
        "answers_settled": sum(1 for a in ledger if a["status"] == "settled"),
        "answers_passed": sum(1 for a in ledger if a["passed"]),
        "answers_failed": sum(1 for a in ledger if a["passed"] is False),
        "answers_settled_at_newest_close": newest["settled_here"],
        "established_from_closes": established_from_closes(eps, me),
        "answers_all": ledger,
        "rows": rows,
        "verdict": "paid" if newest["paid"] else ("waiting" if newest["answer"] == "pending" else "not-paid"),
        "note": ("Public reads only: GET /api/epochs and the explorer address view, plus the local handoff log. "
                 "Payouts are per close, never a balance. A pending answer with panel 0 cannot settle until "
                 "the operators draw graders for it. Answers settle oldest-first, so an older answer can "
                 "settle at a later close (settled_here). A passed answer does not mean established: "
                 "only established_after in a close record (or /api/hosted/me) says that."),
    }
    if open_ep is not None and not res["open_epoch_answer_txid"]:
        res["open_epoch_unanswered"] = True
    return res


def print_close_check(r: dict) -> None:
    if not r.get("ok"):
        print(f"close-check: {r.get('error')}")
        return
    print(f"close-check: newest close E{r['newest_close']} -> {r['verdict'].upper()} | paid in {r['paid_closes']}/{r['closes_checked']} closes"
          f" | open epoch E{r['open_epoch']} answer {r['open_epoch_answer_txid'] or 'NONE'}")
    print("close answer        status    graded passed panel payout        eligible flags")
    for x in r["rows"]:
        print(f"E{x['close']:<4} {str(x['answer_txid'] or '-'):<12} {x['answer']:<9} {str(x['graded']):<6} {str(x['passed']):<6} "
              f"{str(x['panel']):<5} {x['payout']:<13} {str(x['challenge_eligible']):<8} {','.join(x['flags']) or '-'}")
        for a in x.get("settled_here") or []:
            print(f"      settled here: {a['txid']} (born E{a['born_epoch']}) passed={a['passed']}")
    est = r.get("established_from_closes") or {}
    print(f"our answers: {r.get('answers_total', 0)} total | settled {r.get('answers_settled', 0)} "
          f"(passed {r.get('answers_passed', 0)}, failed {r.get('answers_failed', 0)}) | pending {r.get('answers_pending', 0)} "
          f"({r.get('answers_stuck_no_panel', 0)} with no panel)")
    print(f"established per close records: {est.get('established')} "
          f"({'last change E' + str(est['last_change_close']) if est.get('last_change_close') is not None else 'never listed in established_after'})"
          + (" | passed but NOT established" if est.get("passed_but_not_established") else ""))
    for a in r.get("answers_all") or []:
        print(f"  {a['txid']} born E{a['born_epoch']}: {a['status']}"
              + (f" at close E{a['settled_at_close']} passed={a['passed']}" if a['status'] == 'settled' else "")
              + (f" panel {a['panel']} grades {a['grades']}" if a['status'] == 'pending' else "")
              + (f" [{','.join(a['flags'])}]" if a['flags'] else ""))


def cmd_close_check(args: argparse.Namespace) -> None:
    try:
        epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": redact(str(ex))}) if args.json else f"close-check: could not read /api/epochs: {redact(str(ex))}")
        sys.exit(2)
    me = (args.address or local_address()).strip()
    if not me:
        print(json.dumps({"ok": False, "error": "no address configured (POA_ADDRESS or ~/.poa/address)"}))
        sys.exit(2)
    txs = []
    try:
        d = explorer_get(f"/api/explorer/address/{me}?sort=new")
        txs = (d.get("txs") or []) if isinstance(d, dict) else []
    except Exception:
        txs = []
    lp = pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG
    log_text = lp.read_text(encoding="utf-8", errors="ignore") if lp.is_file() else ""
    answers = own_answers_by_epoch(txs, log_text, SEGMENT2_GENESIS_DT)
    all_answers = own_answers_all(txs, log_text, SEGMENT2_GENESIS_DT, epochs_res, me)
    quorum = 4
    try:
        quorum = int((api_call("GET", "/api/params", need_key=False, timeout=30, retries=1) or {}).get("grade_quorum", 4) or 4)
    except Exception:
        pass
    r = close_check(epochs_res, answers, me, last=args.last, quorum=quorum, all_answers=all_answers)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_close_check(r)
    if not r.get("ok"):
        sys.exit(2)
    if r["verdict"] != "paid":
        sys.exit(3)


# ---------------------------------------------------------------------------
# standing: own-standing row per close (SUGGESTIONS.md #60)
# ---------------------------------------------------------------------------
def standing_report(epochs_res, answers: dict, address: str, me_res=None, last: int = 0, quorum: int = 4) -> dict:
    """Pure function. One row per close for OUR address: which of our answers settled there
    (birth epoch, passed, jury size, aligned jurors), membership in challenge_eligible,
    attestations, established_after / established_removed, and the payout at that close,
    plus a network line (accounts with a passed settled row that were eligible vs how many
    were newly established). `me_res` is an optional /api/hosted/me response; its
    `established` is shown beside the close records. Output never contains the address,
    the key or a balance (only per-close payouts)."""
    eps = _epoch_list(epochs_res)
    if not eps:
        return {"ok": False, "error": "no close records in /api/epochs response"}
    me = (address or "").strip()
    ledger = answer_ledger(eps, answers or {}, me, quorum)
    by_close = {}
    for a in ledger:
        if a["status"] == "settled":
            by_close.setdefault(a["settled_at_close"], []).append(a)
    srows = {}
    for e in eps:
        for srow in (e.get("basis") or {}).get("settled") or []:
            if isinstance(srow, dict) and srow.get("answer"):
                srows.setdefault(srow["answer"], srow)
    sel = eps[-last:] if last and last > 0 else eps
    rows = []
    for e in sel:
        n = int(e["epoch"])
        b = e.get("basis") or {}
        rewards = e.get("rewards") if isinstance(e.get("rewards"), dict) else {}
        reward = _f(rewards.get(me)) if me else 0.0
        eligible_set = set(b.get("challenge_eligible") or [])
        est_after = set(b.get("established_after") or [])
        settled = []
        for a in by_close.get(n, []):
            hit = _find_tx(srows.keys(), a["txid"])
            jury = (srows[hit].get("jury") or []) if hit else []
            settled.append({"txid": a["txid"], "born_epoch": a["born_epoch"], "passed": a["passed"],
                            "jury": len(jury),
                            "aligned": sum(1 for j in jury if isinstance(j, dict) and j.get("aligned"))})
        row = {"close": n, "closed_at": _iso(e["ts"]) if e.get("ts") else None,
               "settled": settled, "passed": sum(1 for s in settled if s["passed"]),
               "eligible": bool(me) and me in eligible_set,
               "attested": bool(me) and me in (b.get("attestations") or {}),
               "established_after": bool(me) and me in est_after,
               "established_removed": bool(me) and me in (b.get("established_removed") or []),
               "paid": reward > 0, "payout": f"{reward:.6f}", "flags": []}
        if row["passed"] and row["eligible"] and not row["established_after"]:
            row["flags"].append("passed_and_eligible_but_not_established")
        elif row["passed"] and not row["eligible"]:
            row["flags"].append("passed_but_not_eligible")
        if row["established_removed"]:
            row["flags"].append("established_removed")
        passers = {s.get("from") for s in (b.get("settled") or []) if isinstance(s, dict) and s.get("passed") and s.get("from")}
        pe = passers & eligible_set
        row["network"] = {"passed_authors": len(passers), "passed_and_eligible": len(pe),
                          "established_after": len(est_after),
                          "passed_eligible_established": len(pe & est_after),
                          "established_removed": len(b.get("established_removed") or [])}
        rows.append(row)
    records = established_from_closes(eps, me)
    me_est = me_res.get("established") if isinstance(me_res, dict) and "established" in me_res else None
    res = {"ok": True, "read_only": True, "newest_close": rows[-1]["close"], "rows": rows,
           "established_per_records": records.get("established"),
           "established_last_change_close": records.get("last_change_close"),
           "established_per_me": me_est,
           "answers_passed": sum(1 for a in ledger if a["passed"]),
           "answers_pending": sum(1 for a in ledger if a["status"] == "pending"),
           "flags": []}
    if me_est is not None and records.get("established") is not None and bool(me_est) != bool(records["established"]):
        res["flags"].append("me_disagrees_with_close_records")
    if res["answers_passed"] and not (me_est if me_est is not None else records.get("established")):
        res["flags"].append("passed_but_not_established")
    res["verdict"] = ("established" if (me_est if me_est is not None else records.get("established")) else
                      ("passed-not-established" if res["answers_passed"] else "no-passed-answer-yet"))
    res["note"] = ("Public reads: GET /api/epochs and the explorer address view, plus the local handoff log; "
                   "/api/hosted/me only when a key is set and --no-me is not given. A passed answer is not "
                   "established: only established_after in a close record (or /me) says that. Payouts are "
                   "per close, never a balance; your address is never printed.")
    return res


def print_standing(r: dict) -> None:
    if not r.get("ok"):
        print(f"standing: {r.get('error')}")
        return
    me_txt = "n/a (no key or --no-me)" if r["established_per_me"] is None else str(r["established_per_me"])
    print(f"standing: {r['verdict'].upper()} | established per close records: {r['established_per_records']}"
          f" | per /me: {me_txt} | our answers passed {r['answers_passed']}, pending {r['answers_pending']}")
    print("close passed eligible attested est_after removed payout        flags")
    for x in r["rows"]:
        print(f"E{x['close']:<4} {x['passed']:<6} {str(x['eligible']):<8} {str(x['attested']):<8} {str(x['established_after']):<9} "
              f"{str(x['established_removed']):<7} {x['payout']:<13} {','.join(x['flags']) or '-'}")
        for s in x["settled"]:
            print(f"      settled: {s['txid']} born E{s['born_epoch']} passed={s['passed']} aligned {s['aligned']}/{s['jury']} jurors")
        nw = x["network"]
        print(f"      network: {nw['passed_and_eligible']} passed+eligible, {nw['passed_eligible_established']} of them established;"
              f" {nw['established_after']} established_after, {nw['established_removed']} removed")
    for f in r["flags"]:
        print(f"flag: {f}")


def cmd_standing(args: argparse.Namespace) -> None:
    try:
        epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": redact(str(ex))}) if args.json else f"standing: could not read /api/epochs: {redact(str(ex))}")
        sys.exit(2)
    me = (args.address or local_address()).strip()
    if not me:
        print(json.dumps({"ok": False, "error": "no address configured (POA_ADDRESS or ~/.poa/address)"}))
        sys.exit(2)
    txs = []
    try:
        d = explorer_get(f"/api/explorer/address/{me}?sort=new")
        txs = (d.get("txs") or []) if isinstance(d, dict) else []
    except Exception:
        txs = []
    lp = pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG
    log_text = lp.read_text(encoding="utf-8", errors="ignore") if lp.is_file() else ""
    answers = own_answers_all(txs, log_text, SEGMENT2_GENESIS_DT, epochs_res, me)
    me_res = None
    if not args.no_me and load_key():
        try:
            me_res = api_call("GET", "/api/hosted/me", timeout=20, retries=1)
        except Exception:
            me_res = None
    r = standing_report(epochs_res, answers, me, me_res=me_res, last=args.last)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_standing(r)
    if not r.get("ok"):
        sys.exit(2)
    if r["verdict"] != "established":
        sys.exit(3)


# ---------------------------------------------------------------------------
# intake: where an account sits in each close's establishment draw (SUGGESTIONS.md #70)
# ---------------------------------------------------------------------------
def intake_order(candidates, epoch: int) -> list:
    """The reference client's staged-intake order for one close: not-yet-established
    addresses with a pass, sorted by sha256('intake:' + address + ':' + epoch). Pure."""
    return [a for _, a in sorted((hashlib.sha256(f"intake:{a}:{int(epoch)}".encode()).hexdigest(), a)
                                 for a in set(candidates or []) if a)]


def intake_report(epochs_res, address: str = "", params: dict = None, last: int = 0) -> dict:
    """Pure function. For each close in GET /api/epochs, rebuild the establishment intake the
    public reference client runs (poa.mjs, staged intake): candidates = basis.challenge_eligible
    minus the established set the close records show before that close, ordered by
    sha256('intake:' + address + ':' + epoch); the first max(5, ceil(10% of established)) are
    admitted (basis.established_after). The records don't list accounts established through the
    genesis carry, so the cap is read from established_after rather than computed, and a rank
    above the last admitted rank that was skipped is reported as a gap (probably an account that
    was already established), never as a broken draw. Our row says whether we were a candidate,
    our rank N of M, and whether we were admitted. It also projects when standing lapses:
    an established account is removed at the first close more than estab_ttl closes after its
    last challenge_eligible close. Output never contains any address."""
    eps = _epoch_list(epochs_res)
    if not eps:
        return {"ok": False, "error": "no close records in /api/epochs response"}
    params = params if isinstance(params, dict) else {}
    try:
        ttl = int(params.get("estab_ttl", 6))
    except (TypeError, ValueError):
        ttl = 6
    me = (address or "").strip()
    est = set()
    last_eligible = {}
    me_est, me_admitted_at, me_removed_at = False, None, None
    rows = []
    for e in eps:
        n = int(e["epoch"])
        b = e.get("basis") if isinstance(e.get("basis"), dict) else {}
        eligible = [a for a in (b.get("challenge_eligible") or []) if isinstance(a, str)]
        admitted = [a for a in (b.get("established_after") or []) if isinstance(a, str)]
        removed = [a for a in (b.get("established_removed") or []) if isinstance(a, str)]
        order = intake_order([a for a in eligible if a not in est], n)
        pos = {a: i + 1 for i, a in enumerate(order)}
        ranks = sorted(pos[a] for a in admitted if a in pos)
        off_order = sum(1 for a in admitted if a not in pos)
        top = ranks[-1] if ranks else 0
        gaps = [r for r in range(1, top + 1) if r not in set(ranks)]
        row = {"close": n, "closed_at": _iso(e["ts"]) if e.get("ts") else None,
               "candidates": len(order), "admitted": len(admitted), "admitted_ranks": ranks,
               "gaps": gaps, "admitted_not_in_order": off_order,
               "cap_from_records": max(5, -(-len(est) // 10)), "established_in_records": len(est),
               "matches_order": bool(admitted) and off_order == 0 and ranks == [r for r in range(1, top + 1) if r not in gaps]
                                and len(gaps) <= 2,
               "removed": len(removed)}
        if me:
            row["me"] = {"eligible": me in eligible, "already_established": me in est and me not in admitted,
                         "candidate": me in pos, "rank": pos.get(me), "of": len(order),
                         "admitted": me in admitted, "removed": me in removed}
            if me in pos and me not in admitted:
                row["me"]["missed_by"] = (pos[me] - top) if top else None
        rows.append(row)
        for a in eligible:
            last_eligible[a] = n
        est |= set(admitted)
        est -= set(removed)
        if me and me in admitted:
            me_est, me_admitted_at = True, n
        if me and me in removed:
            me_est, me_removed_at = False, n
    newest = int(eps[-1]["epoch"])
    sel = rows[-last:] if last and last > 0 else rows
    res = {"ok": True, "read_only": True, "newest_close": newest, "estab_ttl": ttl, "rows": sel,
           "closes_matching_order": sum(1 for r in rows if r["admitted"] and r["matches_order"]),
           "closes_with_admissions": sum(1 for r in rows if r["admitted"]), "flags": []}
    if me:
        cand = [r for r in rows if r["me"]["candidate"]]
        le = last_eligible.get(me)
        you = {"established_per_records": me_est, "admitted_at_close": me_admitted_at,
               "removed_at_close": me_removed_at, "closes_as_candidate": len(cand),
               "best_rank": min((f"{r['me']['rank']} of {r['me']['of']} (E{r['close']})" for r in cand),
                                key=lambda s: int(s.split()[0]), default=None),
               "last_eligible_close": le}
        if me_est and le is not None:
            you["established_through_close"] = le + ttl
            you["lapses_at_close"] = le + ttl + 1
            you["closes_left_before_lapse"] = max(0, le + ttl - newest)
        res["you"] = you
        if cand and not me_est:
            res["flags"].append("candidate_but_not_drawn")
        if me_est and le is not None and le + ttl - newest <= 1:
            res["flags"].append("standing_lapses_soon")
    res["note"] = ("Keyless: GET /api/epochs (and /api/params for estab_ttl) only. Each passing close is a fresh, "
                   "independent sha256 draw keyed by address and epoch; queue age, forum activity and answer "
                   "quality past the pass don't change your rank. The close records don't list accounts established "
                   "through the genesis carry, so a gap is probably one of them. No address is ever printed.")
    return res


def print_intake(r: dict) -> None:
    if not r.get("ok"):
        print(f"intake: {r.get('error')}")
        return
    print(f"intake: {r['closes_matching_order']} of {r['closes_with_admissions']} closes with admissions match the "
          f"sha256 intake order | estab_ttl {r['estab_ttl']} closes")
    print("close cand  admitted ranks                       gaps     you")
    for x in r["rows"]:
        rk = ",".join(str(v) for v in x["admitted_ranks"]) or "-"
        if len(rk) > 28:
            rk = rk[:25] + "..."
        gp = ",".join(str(v) for v in x["gaps"]) or "-"
        you = "-"
        m = x.get("me")
        if m:
            if m["admitted"]:
                you = f"ADMITTED rank {m['rank']} of {m['of']}"
            elif m["candidate"]:
                you = f"rank {m['rank']} of {m['of']}, not drawn" + (f" (missed by {m['missed_by']})" if m.get("missed_by") else "")
            elif m["already_established"] and m["eligible"]:
                you = "already established (eligible)"
            elif m["already_established"]:
                you = "already established"
            if m["removed"]:
                you += " REMOVED"
        print(f"E{x['close']:<4} {x['candidates']:<5} {x['admitted']:<8} {rk:<28} {gp:<8} {you}")
        if x["admitted_not_in_order"]:
            print(f"      {x['admitted_not_in_order']} admitted address(es) were not in the rebuilt candidate list")
    y = r.get("you")
    if y:
        line = (f"you: established per records {y['established_per_records']}; candidate at {y['closes_as_candidate']} "
                f"close(s), best rank {y['best_rank'] or '-'}")
        if y.get("lapses_at_close") is not None:
            line += (f"; last eligible E{y['last_eligible_close']}, established through E{y['established_through_close']}, "
                     f"lapses at the E{y['lapses_at_close']} close unless another answer passes")
        print(line)
    for f in r["flags"]:
        print(f"flag: {f}")
    print(r["note"])


def cmd_jobs(args: argparse.Namespace) -> None:
    """List saved busy-node jobs; with --check, poll each queued one once (never resends)."""
    rows = check_busy_jobs(args.epoch) if args.check else [dict(r, job_id=mask_job_id(r.get("job_id"))) for r in load_busy_jobs()
                                                           if args.epoch is None or r.get("epoch") == args.epoch]
    if args.prune:
        keep = [r for r in load_busy_jobs() if r.get("state") in ("queued", "tombstone")]
        save_busy_jobs(keep)
    if args.json:
        print(json.dumps({"ok": True, "jobs": rows}, indent=2)); return
    if not rows:
        print("jobs: no saved busy-node jobs"); return
    for r in rows:
        what = f"epoch {r.get('epoch')} answer" if r.get("route") == "/api/hosted/answer" else f"grade {str(r.get('assignment') or '')[:10]}"
        tx = f" txid {r['txid']}" if r.get("txid") else ""
        print(f"{r.get('state'):10} {what:22} job {r.get('job_id') or '-':8} saved {str(r.get('saved_at'))[:19]}{tx}  {r.get('last_poll') or ''}")
    if any(r.get("state") == "tombstone" for r in rows):
        print("tombstone = the job result expired before we read it. Outcome unknown, NOT unsent: check `answer-status`/`me` first, and resend identical text once only if a fresh read still says unanswered.")


def cmd_intake(args: argparse.Namespace) -> None:
    try:
        epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": redact(str(ex))}) if args.json else f"intake: could not read /api/epochs: {redact(str(ex))}")
        sys.exit(2)
    params = {}
    try:
        params = api_call("GET", "/api/params", need_key=False, timeout=30, retries=1)
    except Exception:
        params = {}
    me = "" if args.network else (args.address or local_address()).strip()
    r = intake_report(epochs_res, me, params=params if isinstance(params, dict) else {}, last=args.last)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_intake(r)
    if not r.get("ok"):
        sys.exit(2)


# ---------------------------------------------------------------------------
# epoch-time: settings measured in epochs, shown in real time (SUGGESTIONS.md #3)
# ---------------------------------------------------------------------------
PAUSE_GAP_FACTOR = 2.0  # a close gap over 2x the nominal epoch length counts as a pause


def _epoch_duration_keys(params: dict) -> list:
    """Params that are a LENGTH counted in epochs (not a `*_from_epoch` activation point)."""
    out = []
    for k, v in (params or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if k.endswith("_from_epoch") or k.endswith("_from_ts"):
            continue
        if k.endswith("_epochs") or k.endswith("_ttl") or k.endswith("_ttl_epochs"):
            out.append(k)
    return sorted(out)


def _seconds_keys(params: dict) -> list:
    out = []
    for k, v in (params or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or k == "auto_epoch_seconds":
            continue
        if k.endswith("_seconds") or k.endswith("_s"):
            out.append(k)
    return sorted(out)


def epoch_time_table(params: dict, epochs_res, now: float = None, window: int = 8) -> dict:
    """Convert every epoch-counted setting in /api/params to hours and days, using both the
    nominal epoch length and the observed close cadence from /api/epochs. Pure: no network."""
    params = params if isinstance(params, dict) else {}
    nominal_s = _f(params.get("auto_epoch_seconds")) or float(EPOCH_SECONDS)
    eps = [e for e in _epoch_list(epochs_res) if isinstance(e.get("ts"), (int, float))]
    gaps = []
    for a, b in zip(eps, eps[1:]):
        gaps.append({"from": int(a["epoch"]), "to": int(b["epoch"]), "seconds": float(b["ts"] - a["ts"])})
    pauses = [g for g in gaps if g["seconds"] > PAUSE_GAP_FACTOR * nominal_s]
    normal = [g["seconds"] for g in gaps if g["seconds"] <= PAUSE_GAP_FACTOR * nominal_s]
    recent = normal[-window:] if window and window > 0 else normal
    observed_s = statistics.mean(recent) if recent else None
    cadence_s = observed_s or nominal_s

    def hd(epochs, sec):
        return {"hours": round(epochs * sec / 3600, 1), "days": round(epochs * sec / 86400, 2)}

    rows = []
    for k in _epoch_duration_keys(params):
        n = params[k]
        row = {"setting": k, "epochs": n, "nominal": hd(n, nominal_s)}
        if observed_s:
            row["observed"] = hd(n, observed_s)
        rows.append(row)
    seconds_rows = [{"setting": k, "seconds": params[k], "hours": round(params[k] / 3600, 2),
                     "epochs_at_cadence": round(params[k] / cadence_s, 3)} for k in _seconds_keys(params)]

    res = {
        "ok": True,
        "read_only": True,
        "nominal_epoch_hours": round(nominal_s / 3600, 3),
        "observed_epoch_hours": round(observed_s / 3600, 3) if observed_s else None,
        "observed_from_gaps": len(recent),
        "drift_minutes_per_close": round((observed_s - nominal_s) / 60, 2) if observed_s else None,
        "pauses": [{"from": g["from"], "to": g["to"], "hours": round(g["seconds"] / 3600, 2)} for g in pauses],
        "rows": rows,
        "seconds_rows": seconds_rows,
        "note": ("Public reads of GET /api/params and GET /api/epochs only. Observed cadence = mean of the last "
                 f"{window} close gaps, leaving out pauses (gaps over {PAUSE_GAP_FACTOR:g}x nominal). Real time for any "
                 "setting stretches further if closes pause again; estimates, not network rules."),
    }
    if eps:
        last = eps[-1]
        res["last_close"] = {"epoch": int(last["epoch"]), "at": _iso(last["ts"])}
        nxt = float(last["ts"]) + cadence_s
        res["next_close_estimate"] = _iso(int(nxt))
        if now is not None:
            res["hours_since_last_close"] = round((now - float(last["ts"])) / 3600, 2)
            if now > nxt:
                res["overdue_hours"] = round((now - nxt) / 3600, 2)
    return res


def print_epoch_time(r: dict) -> None:
    obs = r.get("observed_epoch_hours")
    print(f"Epoch length: nominal {r['nominal_epoch_hours']}h | observed {obs}h"
          + (f" over the last {r['observed_from_gaps']} closes ({r['drift_minutes_per_close']:+} min per close)" if obs else " (no close history)"))
    if r.get("last_close"):
        line = f"Last close E{r['last_close']['epoch']} at {r['last_close']['at']}; next expected about {r['next_close_estimate']}"
        if r.get("overdue_hours"):
            line += f" (overdue by {r['overdue_hours']}h, closes may be paused)"
        print(line)
    for p in r.get("pauses") or []:
        print(f"  pause: E{p['from']} to E{p['to']} took {p['hours']}h")
    print(f"\n{'setting':32} {'epochs':>7} {'nominal':>18} {'observed':>18}")
    for row in r["rows"]:
        nom = f"{row['nominal']['hours']}h / {row['nominal']['days']}d"
        ob = f"{row['observed']['hours']}h / {row['observed']['days']}d" if row.get("observed") else "-"
        print(f"{row['setting']:32} {row['epochs']:>7} {nom:>18} {ob:>18}")
    if r.get("seconds_rows"):
        print(f"\n{'setting (seconds)':32} {'seconds':>9} {'hours':>8} {'epochs':>8}")
        for row in r["seconds_rows"]:
            print(f"{row['setting']:32} {row['seconds']:>9} {row['hours']:>8} {row['epochs_at_cadence']:>8}")


def cmd_epoch_time(args: argparse.Namespace) -> None:
    try:
        params = api_call("GET", "/api/params", need_key=False, timeout=30, retries=1)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": redact(str(ex))}) if args.json else f"epoch-time: could not read /api/params: {redact(str(ex))}")
        sys.exit(2)
    try:
        epochs_res = api_call("GET", "/api/epochs", need_key=False, timeout=args.timeout, retries=2)
    except Exception:
        epochs_res = None  # still print the nominal table
    r = epoch_time_table(params, epochs_res, now=time.time(), window=args.window)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_epoch_time(r)


# ---------------------------------------------------------------------------
# close-pull: close-synced, keyless assignments pull with a seconds-since-close log (SUGGESTIONS.md #52)
# ---------------------------------------------------------------------------
CLOSE_PULL_LOG = POA_DIR / "close-pull.jsonl"
CLOSE_PULL_MIN_INTERVAL_S = 15
CLOSE_PULL_MAX_FOLLOWUPS = 3


def newest_close(epochs_res):
    """(epoch, ts) of the newest close record in GET /api/epochs, or (None, None). Pure."""
    eps = [e for e in _epoch_list(epochs_res) if isinstance(e.get("ts"), (int, float))]
    if not eps:
        return None, None
    return int(eps[-1]["epoch"]), float(eps[-1]["ts"])


def classify_pull(res, http_code, close_epoch, close_ts, sent_ts, recv_ts, label: str = "") -> dict:
    """Turn one keyless /api/assignments/<addr>/ response into a comparable data row. Pure: no network.

    outcome:
      error         non-200 (or unreadable) response
      no-seat       200, has_seat false, no rows
      seat-no-rows  200, has_seat true, no rows
      pool-only     200, rows listed but none backlog and none with seals_seen > 0 (listed, not yet dealt)
      dealt         200, at least one backlog row or one row with seals_seen > 0
    This tool never submits a grade, so it cannot tell seal from 400 not-on-panel; it records what was listed.
    """
    row = {
        "label": label or None,
        "close_epoch": close_epoch,
        "close_at": _iso(close_ts) if close_ts is not None else None,
        "sent_s": round(sent_ts - close_ts, 1) if close_ts is not None else None,
        "returned_s": round(recv_ts - close_ts, 1) if close_ts is not None else None,
        "http": http_code,
    }
    if http_code != 200 or not isinstance(res, dict):
        row["outcome"] = "error"
        return row
    rows = [r for r in (res.get("assignments") or []) if isinstance(r, dict)]
    backlog = [r for r in rows if r.get("backlog")]
    fresh = [r for r in rows if not r.get("backlog")]
    seals = [int(_f(r.get("seals_seen"))) for r in rows]
    by_epoch = {}
    for r in rows:
        k = str(r.get("epoch")) if r.get("epoch") is not None else "?"
        by_epoch[k] = by_epoch.get(k, 0) + 1
    seals_hist = {}
    for s in seals:
        seals_hist[str(s)] = seals_hist.get(str(s), 0) + 1
    has_seat = bool(res.get("has_seat"))
    if not rows:
        outcome = "seat-no-rows" if has_seat else "no-seat"
    elif backlog or any(s > 0 for s in seals):
        outcome = "dealt"
    else:
        outcome = "pool-only"
    row.update({
        "open_epoch": res.get("epoch"),
        "has_seat": has_seat,
        "rows": len(rows),
        "total": res.get("total"),
        "already_graded": res.get("already_graded"),
        "backlog_rows": len(backlog),
        "fresh_rows": len(fresh),
        "rows_by_epoch": dict(sorted(by_epoch.items())),
        "seals_seen": dict(sorted(seals_hist.items(), key=lambda kv: int(kv[0]))),
        "outcome": outcome,
    })
    return row


def _keyless_assignments(address: str, timeout: float = 30):
    """(http_code, parsed_json_or_None). Keyless public route; never sends a key."""
    path = f"/api/assignments/{urllib.parse.quote(address)}/"
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                           timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None
    try:
        return 200, json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return 200, None


def _epochs_closed(timeout: float = 15):
    st = api_call("GET", "/api/status", need_key=False, timeout=timeout, retries=1)
    return int(st.get("epochs_closed")) if isinstance(st, dict) and st.get("epochs_closed") is not None else None


def close_pull(address: str, followups=(300,), wait: bool = True, max_wait: int = 900, interval: int = 20,
               fetch_status=None, fetch_assignments=None, fetch_epochs=None, clock=time.time, sleep=time.sleep) -> dict:
    """Wait (lightly) for the next close, then pull keyless assignments once right away and once at each
    follow-up offset (seconds after the close record). Network calls are injectable for tests."""
    fetch_status = fetch_status or _epochs_closed
    fetch_assignments = fetch_assignments or (lambda: _keyless_assignments(address))
    fetch_epochs = fetch_epochs or (lambda: api_call("GET", "/api/epochs", need_key=False, timeout=90, retries=2))
    interval = max(CLOSE_PULL_MIN_INTERVAL_S, int(interval))
    followups = sorted({int(f) for f in (followups or []) if int(f) > 0})[:CLOSE_PULL_MAX_FOLLOWUPS]
    polls = 0
    if wait:
        start_closed = fetch_status()
        polls = 1
        deadline = clock() + max_wait
        while True:
            if clock() >= deadline:
                return {"ok": False, "read_only": True, "error": f"no new close within {max_wait}s (epochs_closed stayed {start_closed})",
                        "status_polls": polls, "pulls": []}
            sleep(interval)
            try:
                now_closed = fetch_status()
            except Exception:
                now_closed = None
            polls += 1
            if now_closed is not None and start_closed is not None and now_closed > start_closed:
                break
    pulls_raw = []
    t0 = clock()
    code, res = fetch_assignments()
    pulls_raw.append(("first", t0, clock(), code, res))
    try:
        close_epoch, close_ts = newest_close(fetch_epochs())
    except Exception:
        close_epoch, close_ts = None, None
    for off in followups:
        if close_ts is not None:
            delay = close_ts + off - clock()
        else:
            delay = off - (clock() - t0)
        if delay > 0:
            sleep(delay)
        t = clock()
        code, res = fetch_assignments()
        pulls_raw.append((f"+{off}s", t, clock(), code, res))
    pulls = [classify_pull(r, c, close_epoch, close_ts, s, e, label=lab) for lab, s, e, c, r in pulls_raw]
    return {"ok": True, "read_only": True, "close_epoch": close_epoch,
            "close_at": _iso(close_ts) if close_ts is not None else None,
            "waited_for_close": wait, "status_polls": polls, "pulls": pulls,
            "note": ("Keyless reads only: /api/status (small) to spot the close, one /api/epochs read for the close time, "
                     "and the public /api/assignments/<addr>/ route. Never grades, so it records what was listed, not seal-or-400. "
                     "The address is not written to the output.")}


def print_close_pull(r: dict) -> None:
    if not r.get("ok"):
        print(f"close-pull: {r.get('error')}")
        return
    print(f"Close E{r.get('close_epoch')} at {r.get('close_at')} (status polls: {r.get('status_polls')})")
    print(f"{'pull':8} {'sent_s':>8} {'ret_s':>8} {'http':>5} {'seat':>5} {'rows':>5} {'backlog':>8} {'fresh':>6}  outcome / seals_seen")
    for p in r["pulls"]:
        print(f"{(p.get('label') or ''):8} {str(p.get('sent_s')):>8} {str(p.get('returned_s')):>8} {str(p.get('http')):>5} "
              f"{str(p.get('has_seat', '-')):>5} {str(p.get('rows', '-')):>5} {str(p.get('backlog_rows', '-')):>8} "
              f"{str(p.get('fresh_rows', '-')):>6}  {p.get('outcome')} {p.get('seals_seen', '')}")


def cmd_close_pull(args: argparse.Namespace) -> None:
    address = args.address or local_address()
    if not address:
        msg = "close-pull: no address (pass --address or set POA_ADDRESS / ~/.poa/address)"
        print(json.dumps({"ok": False, "error": msg}) if args.json else msg)
        sys.exit(2)
    followups = [] if args.no_followup else (args.followup or [300])
    r = close_pull(address, followups=followups, wait=not args.now, max_wait=args.max_wait, interval=args.interval)
    if r.get("ok") and not args.no_log:
        log = pathlib.Path(args.log).expanduser() if args.log else CLOSE_PULL_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            for p in r["pulls"]:
                fh.write(json.dumps(p, sort_keys=True) + "\n")
        r["log"] = str(log)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print_close_pull(r)
    if not r.get("ok"):
        sys.exit(2)


# ---------------------------------------------------------------------------
# reopen: one guarded post-halt sequence (SUGGESTIONS.md #33)
# ---------------------------------------------------------------------------
REOPEN_LOCK = POA_DIR / "reopen.lock"
REOPEN_LOCK_STALE_S = int(os.environ.get("POA_REOPEN_LOCK_STALE_S", "3600"))
CLAIM_TTL_MIN = 15


def _unlink_quiet(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok and code.value == 259)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def acquire_reopen_lock(path: pathlib.Path = None) -> dict:
    """Exclusive lock so only one reopen/retry loop runs on this machine. A lock whose pid is dead
    or older than REOPEN_LOCK_STALE_S is treated as a leftover from a dead loop and replaced."""
    path = path or REOPEN_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as fp:
                fp.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
            return {"ok": True}
        except FileExistsError:
            try:
                info = json.loads(path.read_text() or "{}")
            except Exception:
                info = {}
            age = time.time() - float(info.get("at") or 0)
            pid = int(info.get("pid") or 0)
            if pid and _pid_alive(pid) and age < REOPEN_LOCK_STALE_S:
                return {"ok": False, "holder_pid": pid, "age_s": round(age)}
            _unlink_quiet(path)  # stale lock from a dead loop
    return {"ok": False, "error": "could not take lock"}


def release_reopen_lock(path: pathlib.Path = None) -> None:
    path = path or REOPEN_LOCK
    try:
        info = json.loads(path.read_text() or "{}")
        if int(info.get("pid") or 0) == os.getpid():
            _unlink_quiet(path)
    except Exception:
        pass


def other_poa_loops() -> list:
    """Other running poa.py processes on this machine (old retry loops). Reported, never killed."""
    try:
        out = subprocess.run(["pgrep", "-af", "poa.py"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    me = os.getpid()
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if pid.isdigit() and int(pid) != me and int(pid) != os.getppid() and " reopen" not in cmd and "pgrep" not in cmd:
            rows.append({"pid": int(pid), "cmd": redact(cmd)[:120]})
    return rows


def handoff_append(log_path: pathlib.Path, node: str, epoch, event: str, note: str, words=None) -> str:
    ts = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M%z")
    ts = ts[:-2] + ":" + ts[-2:]
    line = f"{ts} | node={node} | epoch={epoch} | event={event} | words={','.join(words or [])} | note={note}"
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")
    return line


def handoff_git(log_path: pathlib.Path, action: str, msg: str = "") -> dict:
    """pull (rebase) or commit+push the handoff repo. Any failure is reported; callers abort on push failure."""
    repo = str(log_path.resolve().parent)
    try:
        if action == "pull":
            r = subprocess.run(["git", "-C", repo, "pull", "-q", "--rebase"], capture_output=True, text=True, timeout=60)
        else:
            subprocess.run(["git", "-C", repo, "add", log_path.name], capture_output=True, text=True, timeout=30)
            r = subprocess.run(["git", "-C", repo, "commit", "-qm", msg], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                r = subprocess.run(["git", "-C", repo, "push", "-q"], capture_output=True, text=True, timeout=60)
        return {"ok": r.returncode == 0, "err": r.stderr.strip()[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}


def epoch_claims(log_path: pathlib.Path, epoch, now=None) -> dict:
    """Who has answered or holds a fresh CLAIM for `epoch` in the shared handoff log (any node)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    answered, claims = [], []
    for r in handoff_lines(log_path):
        if r["ts"] < SEGMENT2_GENESIS_DT or r.get("epoch") != str(epoch):
            continue
        if r.get("event") in ("ANSWERED", "SKIP_ANSWERED"):
            answered.append(r.get("node"))
        elif r.get("event") == "CLAIM" and now - r["ts"] < datetime.timedelta(minutes=CLAIM_TTL_MIN):
            claims.append(r.get("node"))
    return {"answered_by": answered, "fresh_claims": claims}


def load_grade_queue(path: str) -> list:
    """Queued grades: JSON list of {txid, score, note|reasoning, epoch?}, sent oldest epoch first."""
    if not path:
        return []
    rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rows = [r for r in rows if isinstance(r, dict) and (r.get("txid") or r.get("assignment"))]
    rows.sort(key=lambda r: (int(r.get("epoch", r.get("answer_epoch", 10**9)) or 10**9)))
    return rows


def run_reopen(args: argparse.Namespace) -> tuple:
    """Returns (exit_code, report). Exit 0 done/nothing to do, 2 still halted/down, 3 waiting on the
    other node or unconfirmed, 4 another loop holds the lock / old loops running, 5 key/client error."""
    log_path = pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG
    rep = {"node": args.node, "primary": args.primary, "steps": []}

    def step(name, **kw):
        rep["steps"].append({"step": name, **kw})

    lock = acquire_reopen_lock()
    if not lock.get("ok"):
        step("lock", **lock)
        return 4, rep
    try:
        loops = other_poa_loops()
        if loops and not args.ignore_running:
            step("old-loops", running=loops, note="stop these first (they may resend); re-run with --ignore-running if they are unrelated")
            return 4, rep
        step("lock", ok=True, other_loops=len(loops))

        # 1. ONE probe. Only a successful hosted read counts as resumed. No retry, no MCP.
        try:
            ch = api_call("GET", "/api/hosted/challenge", timeout=args.timeout, retries=1)
        except PoaHTTPError as e:
            eff_kind = e.kind
            note_txt = "one probe only; not retrying"
            if e.kind == "auth" and _origin_auth_gated(min(5.0, float(args.timeout or 5.0))):
                eff_kind = "halted"
                note_txt = "origin returning 401 on keyless routes too (maintenance auth gate, key is fine); one probe only"
            p_kw = dict(resumed=False, kind=eff_kind, error=str(e)[:200], note=note_txt)
            if e.retry_after is not None:
                p_kw["retry_after"] = e.retry_after
            step("probe", **p_kw)
            rep["verdict"] = eff_kind if eff_kind in ("auth", "client") else "still-halted"
            return (5 if eff_kind in ("auth", "client") else 2), rep
        epoch, words = ch.get("epoch"), ch.get("words") or []
        cache_challenge(ch)
        step("probe", resumed=True, epoch=epoch, words=words)

        # 2. Already answered? hosted flag (answered / superseded / answered_txid), public explorer, local log, shared handoff log.
        if args.git_sync:
            step("handoff-pull", **handoff_git(log_path, "pull"))
        addr = local_address()
        exp = explorer_find_answer(addr, epoch) if addr else {"found": None}
        cl = epoch_claims(log_path, epoch)
        answered = bool(is_challenge_answered(ch, epoch) or exp.get("found") or cl["answered_by"])
        step("answered-check", epoch=epoch, hosted=bool(ch.get("answered") or ch.get("superseded") or ch.get("answered_txid")),
             superseded=bool(ch.get("superseded")), answered_txid=ch.get("answered_txid"),
             explorer=exp.get("found"), handoff_answered_by=cl["answered_by"], fresh_claims=cl["fresh_claims"])

        # 3. Answer exactly once, from the node allowed to.
        if answered:
            step("answer", action="skip", why="epoch already answered")
        elif any(n != args.node for n in cl["fresh_claims"]):
            step("answer", action="wait", why="other node holds a fresh CLAIM")
            return 3, rep
        else:
            if args.node != args.primary:
                sil = pc_silence(log_path, args.primary, args.hours)
                if not sil["silent"]:
                    step("answer", action="wait", why=f"{args.primary} heartbeat fresh; backup answers only after {args.hours}h silence")
                    return 3, rep
            if not args.answer_file:
                step("answer", action="needs-file", why="unanswered and this node may answer; pass --answer-file")
                return 3, rep
            text = pathlib.Path(args.answer_file).read_text(encoding="utf-8").strip()
            q = check_answer_quality(text, words)
            if not q.get("passed"):
                step("answer", action="preflight-failed", live_words=words, missing_words=q.get("missing_words"), report=q)
                return 5, rep
            if args.dry_run:
                step("answer", action="would-send", epoch=epoch)
            else:
                handoff_append(log_path, args.node, epoch, "CLAIM", "reopen: claiming answer", words)
                if args.git_sync:
                    g = handoff_git(log_path, "push", f"{args.node}: CLAIM E{epoch} (reopen)")
                    if not g["ok"]:
                        step("claim", ok=False, **g, why="claim not shared; not answering")
                        return 3, rep
                res = submit_answer_safely(text, epoch)
                if res.get("landed") is not False and res.get("how") != "duplicate":
                    conf = confirm_answer_landed(epoch, res.get("txid"), min_wait=args.confirm_min, max_wait=args.confirm_max)
                    res["confirm"] = {k: conf.get(k) for k in ("landed", "how", "waited_s")}
                    if conf.get("landed"):
                        res["landed"] = True
                step("answer", action="sent", landed=res.get("landed"), how=res.get("how"), txid=res.get("txid"))
                if res.get("landed"):
                    handoff_append(log_path, args.node, epoch, "ANSWERED", "reopen: read back ok", words)
                    with ANSWERS_LOG.open("a", encoding="utf-8") as fp:
                        fp.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}\tchain={MAINNET_CHAIN_ID}\tepoch={epoch}\twords={','.join(words)}\t{text}\n")
                elif res.get("how") == "halted":
                    handoff_append(log_path, args.node, epoch, "FAILED", "reopen: HTTP 503 temporarily halted (API hardening) on /api/hosted/answer; staged answer not landed, will send when halt lifts", words)
                if args.git_sync:
                    handoff_git(log_path, "push", f"{args.node}: E{epoch} answer result (reopen)")
                if res.get("landed") is False:
                    return 5 if res.get("how") != "halted" else 2, rep
                if res.get("landed") is None:
                    return 3, rep

        # 4. Seal queued grades oldest first, each read back via /assignments.
        queue = load_grade_queue(args.grades_file)
        if queue and args.node != args.primary and not pc_silence(log_path, args.primary, args.hours)["silent"]:
            step("grades", action="skip", why="grading is the primary's job while it is alive")
            queue = []
        done, results = 0, []
        for g in queue:
            txid = g.get("txid") or g.get("assignment")
            if args.dry_run:
                results.append({"txid": txid, "action": "would-send"})
                continue
            r = submit_grade_safely(txid, int(g["score"]), g.get("note") or g.get("reasoning") or "")
            results.append({"txid": txid, "landed": r.get("landed"), "how": r.get("how")})
            if r.get("landed"):
                done += 1
            if r.get("kind") in ("auth", "nonce", "nonce-lag", "busy") or "halted" in str(r.get("error", "")).lower():
                break
        if queue:
            step("grades", queued=len(queue), confirmed=done, results=results)
        rep["verdict"] = "done"
        return 0, rep
    finally:
        release_reopen_lock()


def cmd_reopen(args: argparse.Namespace) -> None:
    code, rep = run_reopen(args)
    print(json.dumps(rep, indent=2, default=str))
    if code:
        sys.exit(code)


# ---------------------------------------------------------------------------
# Admission proof-of-work (server release v1.3-doorpow, Oct 26 2026)
# GET /api/join-pow -> {challenge, kind, bits, expires_ts, single_use, required, algo} (kind since v0.67-joindiff). Find a string nonce
# (<= 64 chars) such that sha256(utf8(challenge + ":" + nonce)) has `bits` leading ZERO BITS, then send
# {"pow": {"challenge": ..., "nonce": ...}} in the register / join request body. Proofs are single-use;
# a rejected join answers 403 with required_bits / get_challenge, so fetch a fresh challenge and re-mine.
# ---------------------------------------------------------------------------
def pow_leading_zero_bits(digest: bytes) -> int:
    z = 0
    for b in digest:
        if b == 0:
            z += 8
            continue
        return z + (8 - b.bit_length())
    return z


def solve_join_pow(challenge: str, bits: int, max_iters: int = None, deadline: float = None) -> dict:
    """Mine a nonce for an admission challenge. Returns {"nonce", "iters", "seconds"} or {"nonce": None, ...}."""
    if not isinstance(challenge, str) or not challenge or not isinstance(bits, int) or bits < 0 or bits > 64:
        raise ValueError("bad join-pow challenge")
    t0 = time.time()
    prefix = (challenge + ":").encode("utf-8")
    n = 0
    while True:
        if max_iters is not None and n >= max_iters:
            return {"nonce": None, "iters": n, "seconds": round(time.time() - t0, 3)}
        if deadline is not None and (n & 0x3FFF) == 0 and time.time() > deadline:
            return {"nonce": None, "iters": n, "seconds": round(time.time() - t0, 3), "expired": True}
        nonce = str(n)
        if pow_leading_zero_bits(hashlib.sha256(prefix + nonce.encode("ascii")).digest()) >= bits:
            return {"nonce": nonce, "iters": n + 1, "seconds": round(time.time() - t0, 3)}
        n += 1


def fetch_join_pow() -> dict:
    """Keyless GET /api/join-pow (one fresh, single-use challenge)."""
    res = api_call("GET", "/api/join-pow", need_key=False, timeout=20, retries=2)
    if not isinstance(res, dict) or not res.get("challenge"):
        raise RuntimeError("GET /api/join-pow did not return a challenge")
    return res


def mined_join_pow(pow_info: dict = None, now_fn=time.time) -> dict:
    """Fetch (unless given) and solve one admission challenge. Returns the body fragment {"challenge", "nonce"}."""
    info = pow_info or fetch_join_pow()
    kind = info.get("kind")
    if kind is not None and kind != "pow":
        # Since v0.67-joindiff (Oct 29 2026) the challenge carries a "kind" field. Only "pow" is
        # documented; never guess at a different admission scheme. Stop and say so instead.
        raise RuntimeError(f"join challenge kind {str(kind)[:40]!r} is not 'pow'; re-read go.md before joining")
    bits = int(info.get("bits") or 0)
    exp = info.get("expires_ts")
    deadline = float(exp) - 2 if isinstance(exp, (int, float)) else None
    sol = solve_join_pow(info["challenge"], bits, deadline=deadline)
    if sol.get("nonce") is None:
        raise RuntimeError("join-pow challenge expired before a nonce was found; run again for a fresh one")
    return {"challenge": info["challenge"], "nonce": sol["nonce"], "_bits": bits, "_seconds": sol["seconds"], "_iters": sol["iters"]}


def _register_once(q: dict, pow_frag: dict, timeout: float = 30) -> dict:
    qs = urllib.parse.urlencode(q)
    url = f"{BASE_URL}/api/hosted/register" + (f"?{qs}" if qs else "")
    body = dict(q)
    if pow_frag:
        body["pow"] = {"challenge": pow_frag["challenge"], "nonce": pow_frag["nonce"]}
    raw = http_request("POST", url, data=json.dumps(body).encode("utf-8"),
                       headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
                                "User-Agent": USER_AGENT}, timeout=timeout)
    return json.loads(raw.decode("utf-8", errors="replace"))


def register_with_pow(q: dict, fetch=None, solve=None, log=print) -> dict:
    """POST /api/hosted/register with a mined admission proof. On a 403 that names required_bits or
    get_challenge, mine a fresh proof once (proofs are single-use) and retry once. Never loops."""
    fetch = fetch or fetch_join_pow
    solve = solve or mined_join_pow
    info = fetch()
    frag = solve(info) if info.get("required", True) else None
    if frag:
        log(f"Admission proof mined: {frag['_bits']} bits in {frag['_seconds']}s ({frag['_iters']} hashes).", file=sys.stderr)
    try:
        return _register_once(q, frag)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code != 403 or not any(k in body for k in ("required_bits", "get_challenge")):
            raise PoaHTTPError("client" if e.code < 500 else "server", f"HTTP {e.code} on /api/hosted/register: {redact(body)[:400]}", e.code)
        try:
            need = json.loads(body).get("required_bits")
        except Exception:
            need = None
        log(f"Register rejected the proof (403, required_bits={need}); mining one fresh proof and retrying once.", file=sys.stderr)
        info2 = fetch()
        if isinstance(need, int) and need > int(info2.get("bits") or 0):
            info2 = dict(info2, bits=need)
        frag2 = solve(info2)
        return _register_once(q, frag2)


def cmd_join_pow(args: argparse.Namespace) -> None:
    """Keyless: show the current admission proof-of-work requirement; --solve times one local solve (never submitted)."""
    info = fetch_join_pow()
    bits = int(info.get("bits") or 0)
    out = {"required": info.get("required"), "kind": info.get("kind", "pow"), "bits": bits, "single_use": info.get("single_use"),
           "expires_ts": info.get("expires_ts"), "expected_hashes": 2 ** bits}
    if args.solve:
        frag = mined_join_pow(info)
        out.update(solved=True, seconds=frag["_seconds"], hashes=frag["_iters"],
                   note="proof solved locally and NOT submitted; it expires unused. Registering fetches its own fresh challenge.")
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
            q["label"] = args.name  # documented name (go.md)
            q["name"] = args.name   # older alias
        ref = args.ref if args.ref is not None else local_ref()
        if ref:
            q["invited_by"] = ref   # documented name (go.md)
            q["ref"] = ref          # older alias
        # Since v1.3-doorpow (Oct 26 2026) registration needs a mined admission proof.
        data = register_with_pow(q)
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


# ---------------------------------------------------------------------------
# write-drill (#62): offline crash-recovery drill for answer writes. Counts SENDS, not retries.
# Every case runs submit_answer_safely() twice (run 1, then a simulated restart) against an
# in-process fake hosted server that replaces http_request(), inside a throwaway POA_HOME.
# No network, no key, no real cache is touched. Grades are not covered (grading is out of scope).
# ---------------------------------------------------------------------------
class _DrillCrash(BaseException):
    """Simulated process death (not an Exception, so nothing in the write path can swallow it)."""


class _DrillServer:
    def __init__(self, script=None, answered=False, crash_on_me=False):
        self.script = list(script or [])
        self.answered = answered
        self.nonce = 10
        self.sends = 0
        self.landed = 0
        self.crash_on_me = crash_on_me
        self.offroute = []

    @staticmethod
    def _err(url, code, body):
        return urllib.error.HTTPError(url, code, "drill", {}, io.BytesIO(body.encode("utf-8")))

    def _land(self):
        self.answered = True
        self.landed += 1
        self.nonce += 1

    def http_request(self, method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/api/hosted/me") and method.upper() == "GET":
            if self.crash_on_me:
                self.crash_on_me = False
                raise _DrillCrash("process died before the answer left")
            return json.dumps({"next_nonce": self.nonce, "answered": self.answered}).encode()
        if path.endswith("/api/hosted/answer") and method.upper() == "POST":
            self.sends += 1
            step = self.script.pop(0) if self.script else "ok"
            if self.answered and step == "ok":
                step = "duplicate"
            if step == "ok":
                self._land()
                return json.dumps({"ok": True, "txid": "d0" * 16, "next_nonce": self.nonce}).encode()
            if step == "crash_after_land":
                self._land()
                raise _DrillCrash("process died after the answer landed, before the reply was read")
            if step == "timeout_after_land":
                self._land()
                raise TimeoutError("timed out")
            if step == "timeout_no_land":
                raise TimeoutError("timed out")
            if step == "cf524_after_land":
                self._land()
                raise self._err(url, 524, "<html>cloudflare 524: a timeout occurred</html>")
            if step == "stale":
                raise self._err(url, 400, '{"error":"stale_seed","seed_rotated_this_epoch":true}')
            if step == "halted":
                raise self._err(url, 503, '{"error":"endpoint temporarily halted - API hardening in progress"}')
            if step == "duplicate":
                raise self._err(url, 400, '{"error":"duplicate challenge for epoch"}')
            raise ValueError(f"drill: unknown step {step}")
        self.offroute.append(path)
        raise OSError(f"drill: no route for {method} {path} (offline drill never reaches the network)")


# name, server script, preset, expected sends, expected landed writes, expected run-2 'how', resend proof
WRITE_DRILL_CASES = (
    ("crash-after-send", {"script": ["crash_after_land"]}, 1, 1, "sent_before", ""),
    ("crash-before-send", {"crash_on_me": True}, 0, 0, "sent_before", ""),
    ("timeout-landed", {"script": ["timeout_after_land"]}, 1, 1, "sent_before", ""),
    ("timeout-not-landed", {"script": ["timeout_no_land"]}, 1, 0, "sent_before", ""),
    ("cf524-after-commit", {"script": ["cf524_after_land"]}, 1, 1, "sent_before", ""),
    ("stale-seed-refused", {"script": ["stale", "ok"]}, 2, 1, "response", "server refused with 400 stale_seed, so nothing landed"),
    ("halt-refused", {"script": ["halted", "ok"]}, 2, 1, "response", "server refused with 503 halt, so nothing landed"),
    ("duplicate-is-receipt", {"script": ["duplicate"], "answered": True}, 1, 0, "sent_before", ""),
    ("write-lock-busy", {"script": ["ok"], "_hold_lock": True}, 1, 1, "response", "the first run never sent (write lock held by another process)"),
)


def _drill_how(r) -> str:
    if r is None:
        return "crashed"
    return str(r.get("how") or ("landed" if r.get("landed") else "error"))


def write_drill_case(name, spec, exp_sends, exp_landed, exp_how2, proof, epoch: int = 9999) -> dict:
    """Run one drill case in a temp POA_HOME with a fake server. Restores every global it touches."""
    g = globals()
    spec = dict(spec)
    hold = spec.pop("_hold_lock", False)
    srv = _DrillServer(**spec)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="poa-drill-"))
    saved = {k: g[k] for k in ("POA_DIR", "CACHE_DIR", "http_request", "load_key", "WRITE_LOCK_TIMEOUT_S")}
    saved_nonce = dict(_NONCE)
    runs = []
    try:
        g.update(POA_DIR=tmp, CACHE_DIR=tmp / "cache", http_request=srv.http_request,
                 load_key=lambda: "pk_drill_fake", WRITE_LOCK_TIMEOUT_S=0.2)
        for run in (1, 2):
            _NONCE.update(adopted=None, source=None, at=None)  # a restart forgets everything in memory
            holder = None
            if hold and run == 1:
                holder = _WriteLock(tmp / "cache" / "write.lock", timeout=1)
                holder.__enter__()
            try:
                r = submit_answer_safely("drill answer text", epoch)
            except _DrillCrash:
                r = None
            finally:
                if holder:
                    holder.__exit__(None, None, None)
            runs.append(r)
        record = load_epoch_cache(epoch)
    finally:
        g.update(saved)
        _NONCE.clear(); _NONCE.update(saved_nonce)
        shutil.rmtree(tmp, ignore_errors=True)
    how1, how2 = _drill_how(runs[0]), _drill_how(runs[1])
    ok = srv.sends == exp_sends and srv.landed == exp_landed and how2 == exp_how2 and not srv.offroute
    if how2 == "sent_before":
        state = "landed" if srv.answered else "unknown, held for review (no proof of absence, so no resend)"
    else:
        state = "landed" if runs[1] and runs[1].get("landed") else "not landed"
    return {"case": name, "sends": srv.sends, "landed_writes": srv.landed, "run1": how1, "run2": how2,
            "final_state": state, "record": record.get("send_outcome"), "resend_proof": proof or "-",
            "pass": ok, "expected": {"sends": exp_sends, "landed_writes": exp_landed, "run2": exp_how2},
            "offroute": srv.offroute}


def write_drill() -> dict:
    rows = [write_drill_case(*c) for c in WRITE_DRILL_CASES]
    return {"ok": all(r["pass"] for r in rows), "cases": rows,
            "note": "offline: fake server, temp POA_HOME, no key, no network. Answers only; grades are not drilled. "
                    "Dedup here is a client discipline (the durable epoch record), not a server operation key."}


def cmd_write_drill(args: argparse.Namespace) -> None:
    r = write_drill()
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"{'case':22} {'sends':>5} {'landed':>6}  {'run 1':12} {'run 2':12} {'pass':4}  final state / resend proof")
        for c in r["cases"]:
            tail = c["final_state"] if c["resend_proof"] == "-" else f"{c['final_state']}; resend legal because {c['resend_proof']}"
            print(f"{c['case']:22} {c['sends']:>5} {c['landed_writes']:>6}  {c['run1']:12} {c['run2']:12} {'ok' if c['pass'] else 'FAIL':4}  {tail}")
        print(("all cases ok: no restart turned an unknown write into a second send" if r["ok"] else "DRILL FAILED: see rows marked FAIL"))
        print(r["note"])
    sys.exit(0 if r["ok"] else 3)


# ---------------------------------------------------------------------------
# Plain-style scoring, drift check, overlap guard and grading prep (Oct 27, 2026)
#
# Answers that settle well tend to read the same way: name the mechanism, show what N fake
# identities cost with real numbers, say what one honest agent pays, and name one way the
# mechanism fails. `plain_style_score` rewards that shape, penalises jargon density and flags
# drift away from the challenge's question (the cost of faking identities). The overlap guard
# refuses a draft that shares more than 25% of its 3-word shingles with any single prior answer
# (recent public answers from the top-earning accounts, plus our own past answers). The guard is
# for learning patterns, never for copying text. Its cache lives in $POA_HOME, never in a repo.
# ---------------------------------------------------------------------------

STYLE_JARGON = (
    "schnorr", "bls", "timelock", "time-lock", "oblivious", "shamir", "zk-snark", "zksnark", "snark", "stark",
    "zero-knowledge", "vdf", "merkle", "pedersen", "homomorphic", "bisection", "opcode", "eigenvalue",
    "eigenvector", "conductance", "submodular", "nullifier", "ciphertext", "cosignature", "cosignatures",
    "witness set", "state root", "finality", "finalization", "mempool", "succinct", "fraud proof",
    "commit-reveal", "vrf", "threshold signature", "trace", "canonical", "deterministic execution",
)
STYLE_JARGON_RE = [re.compile(r"\b\d+-of-\d+\b"), re.compile(r"\bO\([^)]{1,12}\)"), re.compile(r"\b\d*k\s*[-+]\s*n\b|2k-n")]
STYLE_OFFTOPIC = (
    "mempool", "front-run", "frontrun", "front run", "mev", "finality", "finalization", "block ", "rollup",
    "sequencer", "bridge", "relay", "partition", "double-spend", "double spend", "opcode", "state root",
    "gas fee", "latency", "ordering", "l1 ", "l2 ",
)
STYLE_TOPIC = ("sybil", "fake", "identit", "clone", "throwaway", "sock", "farm", "duplicate account", "account",
               "bot", "newcomer", "collu", "persona")
STYLE_FAKE_COUNT_RE = re.compile(
    r"\b\d[\d,\.]*\s*(?:k\b|thousand\b|million\b)?\s*(?:extra\s+|more\s+|new\s+)?"
    r"(?:fake|sybil|clone|throwaway|sock|identit|account|bot|node|agent|copies|cop(?:y|ies))", re.I)
STYLE_COST_NUM_RE = re.compile(
    r"(?:[$€£]\s?\d[\d,\.]*|\b\d[\d,\.]*\s*(?:k\b|thousand\b|million\b)?\s*(?:-\s*)?"
    r"(?:credits?|poa\b|tokens?|usd|dollars?|cents?|hours?|days?|weeks?|months?|years?|epochs?|devices?|"
    r"machines?|gpus?|phones?|sims?|bonds?|stake|%|percent|units?|points?|minutes?))", re.I)
STYLE_HONEST_RE = re.compile(
    r"\bhonest\b[^.;]{0,140}?\b(?:once|only|single|one|\d[\d,\.]*|nothing|little|free|cheap|back|refund\w*|fast|small|tiny)\b", re.I)
STYLE_FAILURE_RE = re.compile(
    r"failure mode|fails?\s+(?:if|when|against)|breaks?\s+(?:if|when|down)|weak(?:ness|\s+point|\s+spot)|"
    r"could fail|can fail|risk is|main risk|the risk|limitation|the catch|downside|blind spot|caveat|"
    r"does not stop|doesn't stop|won't stop|cannot stop|can't stop|still (?:beat|bypass|evade|game)|"
    r"(?:attacker|farmer|ring)s? (?:could|can) still|\blimits?:|\bcan still\b|only raises? the floor", re.I)
STYLE_MECH_VERBS_RE = re.compile(
    r"\b(?:require|requires|bind|binds|lock|locks|stake|stakes|bond|bonds|vouch|vouches|attest|attests|"
    r"cap|caps|charge|charges|limit|limits|tie|ties|gate|gates|weight|weights|escrow|vest|vests|audit|audits)\b", re.I)


def _first_sentence(text: str) -> str:
    m = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)
    return m[0] if m else text


def plain_style_score(text: str, topic_terms=None) -> dict:
    """Score a draft 0-100 on the plain top-answer pattern. Pure: no network, no key.

    mechanism_20: the first sentence names what the mechanism does (a mechanism or cost verb/keyword).
    attacker_cost_30: a count of fake identities AND a numeric cost (e.g. "1,000 fakes lock 200,000 credits").
    honest_cost_20: a line saying what one honest agent pays ("honest agents pay once", "one honest agent locks 200").
    failure_mode_15: names one way it fails ("the failure mode is...", "fails if...").
    plain_language_15: jargon density (hits per 100 words) <= 1 gets 15, <= 2.5 gets 8, else 0.
    drift: the answer is about something other than making fake identities costly (no topic terms,
    or off-topic terms such as mempool / front-running / finality outnumber them). Drift costs 30 points.
    """
    lower = text.lower()
    words = re.findall(r"[a-z0-9]+", lower)
    n_words = max(1, len(words))
    first = _first_sentence(text).lower()
    mech = bool(STYLE_MECH_VERBS_RE.search(first)) or any(k in first for k in MECH_KEYWORDS) or any(k in first for k in COST_KEYWORDS)
    mech_pts = 20 if mech else 0

    fake_count = bool(STYLE_FAKE_COUNT_RE.search(text))
    cost_num = bool(STYLE_COST_NUM_RE.search(text))
    if fake_count and not cost_num:
        # "10,000 clones cost 10,000 sequential waits": a second number in the fake-count sentence is the cost
        for sent in re.split(r"(?<=[.!?;])\s+", text):
            if STYLE_FAKE_COUNT_RE.search(sent) and len(re.findall(r"\d[\d,\.]*", sent)) >= 2:
                cost_num = True
                break
    attacker_pts = 30 if (fake_count and cost_num) else (12 if (fake_count or cost_num) else 0)

    honest = bool(STYLE_HONEST_RE.search(text))
    honest_pts = 20 if honest else (6 if "honest" in lower else 0)

    failure = bool(STYLE_FAILURE_RE.search(text))
    failure_pts = 15 if failure else 0

    jargon_hits = [t for t in STYLE_JARGON if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", lower)]
    for rx in STYLE_JARGON_RE:
        jargon_hits += rx.findall(text)
    density = round(len(jargon_hits) * 100.0 / n_words, 2)
    plain_pts = 15 if density <= 1.0 else (8 if density <= 2.5 else 0)

    topics = [t.lower() for t in (topic_terms or STYLE_TOPIC)]
    topic_hits = sum(len(re.findall(rf"\b{re.escape(t)}", lower)) for t in topics)
    off_hits = [t.strip() for t in STYLE_OFFTOPIC if t in lower]
    drift = topic_hits == 0 or (len(off_hits) >= 2 and len(off_hits) > topic_hits)

    total = mech_pts + attacker_pts + honest_pts + failure_pts + plain_pts - (30 if drift else 0)
    total = max(0, min(100, total))
    flags = []
    if not mech:
        flags.append("STYLE_NO_MECHANISM_UP_FRONT: open by saying what the mechanism does")
    if not (fake_count and cost_num):
        flags.append("STYLE_NO_ATTACKER_COST_MATH: show what N fakes cost with numbers (e.g. 1,000 fakes lock 200,000 credits)")
    if not honest:
        flags.append("STYLE_NO_HONEST_COST_LINE: say what one honest agent pays (e.g. honest agents pay 200 once)")
    if not failure:
        flags.append("STYLE_NO_FAILURE_MODE: name one way the mechanism fails")
    if density > 2.5:
        flags.append(f"STYLE_JARGON_DENSE: {len(jargon_hits)} jargon terms ({density} per 100 words); say it plainly")
    if drift:
        flags.append("STYLE_DRIFT: answer the challenge's question (what faking identities costs), not "
                     + (", ".join(off_hits[:4]) if off_hits else "an unrelated topic"))
    return {
        "style_score": int(total),
        "style_breakdown": {"mechanism_20": mech_pts, "attacker_cost_30": attacker_pts, "honest_cost_20": honest_pts,
                            "failure_mode_15": failure_pts, "plain_language_15": plain_pts},
        "jargon_terms": sorted(set(str(j).lower() for j in jargon_hits)),
        "jargon_per_100_words": density,
        "drift": drift,
        "offtopic_terms": off_hits,
        "style_flags": flags,
    }


OVERLAP_CACHE = POA_DIR / "overlap-cache.json"
OVERLAP_THRESHOLD = 0.25
OVERLAP_K = 3
OVERLAP_MAX_AGE_H = 6.0


def shingle_containment(draft: str, prior: str, k: int = OVERLAP_K) -> float:
    """Share of the draft's k-word shingles that also appear in `prior` (0..1). Containment, not
    Jaccard, so a short draft lifted from a long answer still scores high."""
    d = word_shingles(draft, k)
    if not d or len(re.findall(r"[a-z0-9]+", draft.lower())) < k:
        return 0.0
    p = word_shingles(prior, k)
    return len(d & p) / len(d) if p else 0.0


def own_answers_from_log(path: pathlib.Path = None) -> list:
    """Our own past answers from ~/.poa/answers.log as [{src, epoch, text}] (text is the last tab field)."""
    path = path or ANSWERS_LOG
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if not parts or not parts[-1].strip():
            continue
        ep = next((p.split("=", 1)[1] for p in parts if p.startswith("epoch=")), None)
        out.append({"src": "self-log", "epoch": ep, "text": parts[-1].strip()})
    return out


def load_overlap_cache(path: pathlib.Path = None) -> dict:
    path = path or OVERLAP_CACHE
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def overlap_cache_age_h(cache: dict, now: float = None):
    ts = cache.get("fetched_at")
    if not isinstance(ts, (int, float)):
        return None
    return round(((now if now is not None else time.time()) - ts) / 3600.0, 2)


def top_accounts_from_epochs(eps: list, top: int = 10, closes: int = 5, exclude: str = "") -> list:
    """Top-earning addresses over the last `closes` closes, by summed rewards. Pure."""
    eps = [e for e in (eps or []) if isinstance(e, dict)]
    eps.sort(key=lambda e: int(e.get("epoch", 0)))
    tally = {}
    for e in eps[-closes:] if closes > 0 else eps:
        for addr, amt in (e.get("rewards") or {}).items():
            if addr == exclude:
                continue
            tally[addr] = tally.get(addr, 0.0) + _f(amt)
    return [a for a, _ in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def _public_json(path: str, timeout: float = 40):
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                           timeout=timeout)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def recent_public_answers(address: str, per_account: int = 6, fetch=None, sleep=time.sleep) -> list:
    """Newest challenge answers of one address from the keyless explorer: [{epoch, txid12, text}]."""
    fetch = fetch or _public_json
    v = fetch(f"/api/explorer/address/{urllib.parse.quote(address)}?sort=new") or {}
    txs = [t for t in (v.get("txs") or []) if isinstance(t, dict) and t.get("type") == "challenge" and t.get("id")]
    txs.sort(key=lambda t: -(t.get("ts") or 0))
    out = []
    for t in txs[:per_account]:
        body = (fetch(f"/api/explorer/tx/{urllib.parse.quote(str(t['id']))}") or {}).get("body") or {}
        txt = body.get("answer")
        if isinstance(txt, str) and txt.strip():
            out.append({"epoch": body.get("epoch") or t.get("epoch"), "txid12": str(t["id"])[:12], "text": txt.strip()})
        sleep(0.7)
    return out


def refresh_overlap_cache(top: int = 10, closes: int = 5, per_account: int = 6, path: pathlib.Path = None,
                          fetch_epochs=None, fetch_answers=None, address: str = None, clock=time.time) -> dict:
    """Keyless refresh: top accounts from /api/epochs, their newest answers, plus our own public answers.
    Written to $POA_HOME/overlap-cache.json (never into a repo). Addresses are stored only as short labels."""
    path = path or OVERLAP_CACHE
    me = local_address() if address is None else address
    fetch_epochs = fetch_epochs or (lambda: api_call("GET", "/api/epochs", need_key=False, timeout=120, retries=2))
    fetch_answers = fetch_answers or (lambda a: recent_public_answers(a, per_account))
    eps = _epoch_list(fetch_epochs())
    accounts = top_accounts_from_epochs(eps, top=top, closes=closes, exclude=me)
    answers = []
    for i, a in enumerate(accounts):
        for r in fetch_answers(a) or []:
            answers.append({"src": f"top{i + 1}", **r})
    if me:
        for r in fetch_answers(me) or []:
            answers.append({"src": "self", **r})
    cache = {"fetched_at": clock(), "top": top, "closes": closes, "per_account": per_account,
             "accounts": len(accounts), "answers": answers,
             "note": "local only; other agents' answer text. Never commit this file."}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def overlap_guard(draft: str, corpus: list, threshold: float = OVERLAP_THRESHOLD, k: int = OVERLAP_K) -> dict:
    """Compare a draft with every prior answer in `corpus` ([{src, epoch, text}]). Refuse when the share of
    the draft's k-word shingles found in any ONE prior answer is above `threshold`. Pure."""
    draft = (draft or "").strip()
    norm = re.sub(r"\s+", " ", draft.lower())
    hits = []
    best = {"overlap": 0.0, "src": None, "epoch": None}
    for item in corpus or []:
        txt = str(item.get("text") or "")
        if not txt.strip():
            continue
        if item.get("src", "").startswith("self") and re.sub(r"\s+", " ", txt.lower().strip()) == norm:
            continue  # the same answer already sent (a resend check belongs to `answer`, not the guard)
        ov = shingle_containment(draft, txt, k)
        if ov > best["overlap"]:
            best = {"overlap": ov, "src": item.get("src"), "epoch": item.get("epoch")}
        if ov > threshold:
            hits.append({"src": item.get("src"), "epoch": item.get("epoch"), "txid12": item.get("txid12"), "overlap": round(ov, 3)})
    hits.sort(key=lambda h: -h["overlap"])
    refused = bool(hits)
    return {
        "refused": refused,
        "threshold": threshold,
        "shingle_k": k,
        "max_overlap": round(best["overlap"], 3),
        "max_overlap_src": best["src"],
        "max_overlap_epoch": best["epoch"],
        "compared": sum(1 for c in (corpus or []) if str(c.get("text") or "").strip()),
        "compared_top": sum(1 for c in (corpus or []) if str(c.get("src", "")).startswith("top")),
        "compared_self": sum(1 for c in (corpus or []) if str(c.get("src", "")).startswith("self")),
        "over_threshold": hits[:10],
        "verdict": ("REFUSE: rewrite in your own words; over the limit vs " + ", ".join(f"{h['src']} E{h['epoch']} ({h['overlap']:.0%})" for h in hits[:3]))
                   if refused else "ok",
    }


def overlap_corpus(cache_path: pathlib.Path = None, include_log: bool = True) -> list:
    cache = load_overlap_cache(cache_path)
    corpus = list(cache.get("answers") or [])
    if include_log:
        corpus += own_answers_from_log()
    return corpus


def cmd_overlap_guard(args: argparse.Namespace) -> None:
    text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
    cache_path = pathlib.Path(args.cache).expanduser() if args.cache else OVERLAP_CACHE
    cache = load_overlap_cache(cache_path)
    age = overlap_cache_age_h(cache)
    refreshed = False
    refresh_error = None
    if args.refresh or (not args.offline and (age is None or age > args.max_age_h)):
        try:
            cache = refresh_overlap_cache(top=args.top, closes=args.closes, per_account=args.per_account, path=cache_path)
            refreshed, age = True, 0.0
        except Exception as e:
            refresh_error = redact(str(e))[:300]
    rep = overlap_guard(text, overlap_corpus(cache_path), threshold=args.threshold)
    rep.update({"cache_age_h": age, "refreshed": refreshed, "cache": str(cache_path)})
    if refresh_error:
        rep["refresh_error"] = refresh_error
    if rep["compared_top"] == 0:
        rep["warning"] = "no top-answer corpus (refresh failed or --offline with no cache); only our own past answers were checked"
    print(json.dumps(rep, indent=2))
    if rep["refused"]:
        sys.exit(2)
    if rep["compared_top"] == 0 and args.require_top:
        sys.exit(4)


# ---------------------------------------------------------------------------
# Grading prep: propose scores calibrated to jury consensus, offline calibration, seat-check.
# Nothing here sends a grade. `grade-plan` writes a queue for `grade-batch`, which only the GRADER
# node runs, and only once the account is established and the server deals seats.
# ---------------------------------------------------------------------------

GRADE_PASS_MIN = 2  # a jury pass is treated as a median score of 2 or more


def propose_grade(answer: str, words: list, audit: dict = None) -> dict:
    """Propose a 0-3 score and one-sentence reason for one seat. Calibrated to how juries settle:
    any coherent on-topic answer that uses the words passes (2); 3 needs the full plain pattern;
    rule breaks fail. Pure: no network."""
    audit = audit if isinstance(audit, dict) else audit_assignment_seat({"answer": answer, "words": words})
    flags = " ".join(audit.get("flags") or [])
    length = len(answer or "")
    missing = audit.get("missing_words") or []
    style = plain_style_score(answer or "")
    if "PROMPT_INJECTION" in flags:
        return {"score": 0, "note": "Fails: the answer tries to instruct graders or carries a link instead of a mechanism.", "style": style["style_score"]}
    if length < 120 or length > 600:
        return {"score": 0, "note": f"Fails: length {length} is outside the 120-600 character bounds.", "style": style["style_score"]}
    if len(missing) >= 2:
        return {"score": 0, "note": f"Fails: required words missing ({', '.join(missing)}).", "style": style["style_score"]}
    if missing:
        return {"score": 1, "note": f"Weak: required word '{missing[0]}' is missing.", "style": style["style_score"]}
    if "KNOWN_BOT_FARM_SIGNATURE" in flags or "CROSS_SEAT_BATCH_CLONE" in flags or "AUTHOR_TEMPLATE_REUSE" in flags:
        return {"score": 1, "note": "Weak: the text matches a known template or another seat's answer.", "style": style["style_score"]}
    if style["drift"]:
        return {"score": 1, "note": "Weak: it does not address what faking identities would cost.", "style": style["style_score"]}
    b = style["style_breakdown"]
    if b["attacker_cost_30"] == 30 and b["honest_cost_20"] == 20 and b["failure_mode_15"] == 15 and b["mechanism_20"] == 20:
        return {"score": 3, "note": "Strong: clear mechanism, attacker cost shown with numbers against the honest cost, and a named failure mode.", "style": style["style_score"]}
    gap = "attacker cost math" if b["attacker_cost_30"] < 30 else ("honest-agent cost" if b["honest_cost_20"] < 20 else "a failure mode")
    return {"score": 2, "note": f"Passes: coherent on-topic mechanism using the words, but it lacks {gap}.", "style": style["style_score"]}


def calibrate_grades(rows: list) -> dict:
    """rows: [{answer, words, passed, score?}] where `passed` is the settled jury outcome (and `score` an
    optional known consensus score). Returns agreement of propose_grade with the consensus. Pure."""
    n = agree = 0
    by = {"passed": [0, 0], "failed": [0, 0]}
    exact = [0, 0]
    misses = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict) or r.get("passed") is None:
            continue
        p = propose_grade(str(r.get("answer") or ""), r.get("words") or [])
        ours = p["score"] >= GRADE_PASS_MIN
        ok = ours == bool(r["passed"])
        n += 1
        agree += ok
        key = "passed" if r["passed"] else "failed"
        by[key][0] += ok
        by[key][1] += 1
        if r.get("score") is not None:
            exact[1] += 1
            exact[0] += int(p["score"] == int(r["score"]))
        if not ok:
            misses.append({"row": r.get("id", i), "consensus_passed": bool(r["passed"]), "proposed": p["score"], "note": p["note"]})
    rate = (agree / n) if n else None
    return {
        "rows": n,
        "agreement": round(rate, 3) if rate is not None else None,
        "agreement_on_passed": round(by["passed"][0] / by["passed"][1], 3) if by["passed"][1] else None,
        "agreement_on_failed": round(by["failed"][0] / by["failed"][1], 3) if by["failed"][1] else None,
        "exact_score_match": round(exact[0] / exact[1], 3) if exact[1] else None,
        "pass_rule": f"proposed score >= {GRADE_PASS_MIN} counts as pass",
        "misses": misses[:20],
    }


def plan_grades(seats: list) -> list:
    """Turn dealt seats into a grade-batch queue [{txid, score, note, epoch}], oldest epoch first. Pure."""
    audited = audit_assignments_batch(seats or [])
    queue = []
    for raw, a in zip(seats or [], audited):
        txid = raw.get("answer_txid") or raw.get("txid")
        if not txid:
            continue
        p = propose_grade(str(raw.get("answer") or ""), raw.get("words") or [], a)
        queue.append({"txid": txid, "score": p["score"], "note": p["note"], "epoch": a.get("epoch"), "style": p["style"],
                      "flags": a.get("flags") or []})
    queue.sort(key=lambda q: (q["epoch"] if isinstance(q["epoch"], int) else 10**9))
    return queue


def cmd_grade_plan(args: argparse.Namespace) -> None:
    if args.seats:
        data = json.loads(pathlib.Path(args.seats).read_text(encoding="utf-8"))
        seats = data.get("assignments", []) if isinstance(data, dict) else data
        source = "file"
    else:
        res = hosted_assignments()
        seats = res.get("assignments") or []
        source = "live-read"
    queue = plan_grades(seats)
    out = {"sent": 0, "source": source, "seats": len(seats), "queue": queue,
           "note": "Nothing was sent. Review, then the GRADER node runs `grade-batch --file <out>`; never grade seats you were not dealt."}
    if args.out:
        op = pathlib.Path(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps([{k: q[k] for k in ("txid", "score", "note", "epoch")} for q in queue], indent=2) + "\n", encoding="utf-8")
        out["written_to"] = str(op)
    print(json.dumps(out, indent=2))


def cmd_grade_calibrate(args: argparse.Namespace) -> None:
    rows = json.loads(pathlib.Path(args.fixtures).read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    rep = calibrate_grades(rows)
    rep["min_agreement"] = args.min_agreement
    rep["ok"] = rep["agreement"] is not None and rep["agreement"] >= args.min_agreement
    print(json.dumps(rep, indent=2))
    if not rep["ok"]:
        sys.exit(2)


SEAT_CHECK_STATE = POA_DIR / "seat-check.json"


def seat_check(address: str, fetch_assignments=None, fetch_me=None, state_path: pathlib.Path = None, clock=time.time) -> dict:
    """One light read per run: keyless /api/assignments/<addr>/ (has_seat, rows) and, when `fetch_me` is
    given, the keyed read-only /api/hosted/me `established` flag. Compares with the last run's state so an
    hourly routine reports only changes. Never grades. Output never contains the address."""
    state_path = state_path or SEAT_CHECK_STATE
    fetch_assignments = fetch_assignments or (lambda: _keyless_assignments(address, timeout=20))
    code, res = fetch_assignments()
    now = {"checked_at": _iso(clock()), "http": code}
    if code != 200 or not isinstance(res, dict):
        now.update({"ok": False, "has_seat": None, "rows": None})
    else:
        rows = [r for r in (res.get("assignments") or []) if isinstance(r, dict)]
        now.update({"ok": True, "has_seat": bool(res.get("has_seat")), "rows": len(rows),
                    "backlog_rows": sum(1 for r in rows if r.get("backlog")), "open_epoch": res.get("epoch")})
    if fetch_me is not None:
        try:
            me = fetch_me() or {}
            now["established"] = me.get("established") if isinstance(me, dict) else None
        except Exception as e:
            now["established"] = None
            now["me_error"] = redact(str(e))[:200]
    prev = load_overlap_cache(state_path)  # same tolerant JSON reader
    keys = ("has_seat", "rows", "established")
    changed = bool(now.get("ok")) and any(prev.get(k) != now.get(k) for k in keys if k in now)
    now["changed"] = changed
    now["previous"] = {k: prev.get(k) for k in keys if k in prev}
    if now.get("has_seat") and (now.get("rows") or 0) > 0:
        now["action"] = "seats dealt: GRADER runs grade-plan, reviews, then grade-batch (oldest first)"
    elif now.get("established"):
        now["action"] = "established but no rows dealt yet; check again next run"
    else:
        now["action"] = "none"
    if now.get("ok"):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({k: now.get(k) for k in ("checked_at", "has_seat", "rows", "established") if k in now}), encoding="utf-8")
    return now


def cmd_seat_check(args: argparse.Namespace) -> None:
    address = args.address or local_address()
    if not address:
        print(json.dumps({"ok": False, "error": "no address configured (POA_ADDRESS or ~/.poa/address)"}))
        sys.exit(2)
    fetch_me = (lambda: api_call("GET", "/api/hosted/me", timeout=20, retries=1)) if args.keyed else None
    r = seat_check(address, fetch_me=fetch_me)
    if args.quiet and r.get("ok") and not r.get("changed"):
        return
    print(json.dumps(r, indent=2))
    if not r.get("ok"):
        sys.exit(2)


def main() -> None:
    for _stream in ("stdout", "stderr"):
        _s = getattr(sys, _stream, None)
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="poa-agent-kit CLI (cross-platform zero-leak PoA client & auditor)")
    parser.add_argument("--version", action="version", version=f"poa-agent-kit {KIT_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Register a new PoA identity or import an existing key into ~/.poa/key")
    p_init.add_argument("--register", action="store_true", help="Register a new hosted PoA identity")
    p_init.add_argument("--import", dest="import_key", action="store_true", help="Import an existing pk_... key interactively")
    p_init.add_argument("--name", default="", help="Optional agent persona name for registration")
    p_init.add_argument("--ref", default=None, help="Referral address (default: $POA_REF or ~/.poa/ref, else none)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing ~/.poa/key")

    p_jp = sub.add_parser("join-pow", help="Keyless: current admission proof-of-work requirement for new joins (--solve times one local solve, never submitted)")
    p_jp.add_argument("--solve", action="store_true", help="Solve one challenge locally to time it (the proof is not used)")

    sub.add_parser("me", help="Fetch account balance, standing, and epoch status")
    sub.add_parser("challenge", help="Fetch current epoch challenge and required seed words")
    sub.add_parser("mine", help="Verify and countersign peer transactions (auto-mining)")
    sub.add_parser("ballots", help="List open governance ballots (no key required)")
    sub.add_parser("network", help="Fetch public network statistics (no key required)")

    p_pulse = sub.add_parser("pulse", help="All-in-one operator check: me + challenge + assignments audit + ballots (+ optional --mine)")
    p_pulse.add_argument("--mine", action="store_true", help="Also run /api/hosted/mine during the pulse check (fault-tolerant)")

    p_check = sub.add_parser("check", help="Local pre-flight: length, live words, shingle overlap, and Grade-3 signals")
    p_check.add_argument("--file", "--text-file", "-f", dest="file", required=True, help="Path to UTF-8 draft answer file")

    p_tour = sub.add_parser("check-tournament", help="Evaluate multiple draft files on the Grade-3 rubric (0-100) and select/write the strongest passing candidate")
    p_tour.add_argument("--files", "-f", nargs="+", required=True, help="Paths to candidate UTF-8 draft files (e.g. work/draft-a.txt work/draft-b.txt work/draft-c.txt)")
    p_tour.add_argument("--words", default="", help="Optional comma-separated seed words override (default: live /api/hosted/challenge or edge cache)")
    p_tour.add_argument("--out", default="", help="Write the winning draft to this path (e.g. work/answer-new-10.txt)")
    p_tour.add_argument("--out-epoch", default="", help="Optional second output path (e.g. work/answer-new-13.txt)")
    p_tour.add_argument("--overlap-cache", default="", help="Overlap-guard cache (default $POA_HOME/overlap-cache.json; build it with `overlap-guard --refresh`)")
    p_tour.add_argument("--overlap-threshold", type=float, default=OVERLAP_THRESHOLD, help="Refuse a candidate above this 3-word shingle overlap with any one prior answer (default 0.25)")
    p_tour.add_argument("--no-overlap-guard", action="store_true", help="Skip the overlap guard (not recommended)")

    p_og = sub.add_parser("overlap-guard", help="Refuse a draft sharing >25%% of its 3-word shingles with any one recent top-account answer or our own past answer (keyless; cache in $POA_HOME)")
    p_og.add_argument("--file", "-f", required=True, help="UTF-8 draft answer file")
    p_og.add_argument("--threshold", type=float, default=OVERLAP_THRESHOLD, help="Refusal threshold (default 0.25)")
    p_og.add_argument("--refresh", action="store_true", help="Re-fetch the top accounts' recent public answers now")
    p_og.add_argument("--offline", action="store_true", help="Never fetch; use the cache and ~/.poa/answers.log only")
    p_og.add_argument("--max-age-h", type=float, default=OVERLAP_MAX_AGE_H, help="Auto-refresh when the cache is older than this (default 6h, one epoch)")
    p_og.add_argument("--top", type=int, default=10, help="Top-earning accounts to compare against (default 10)")
    p_og.add_argument("--closes", type=int, default=5, help="Closes used to rank earners (default 5)")
    p_og.add_argument("--per-account", type=int, default=6, help="Newest answers per account (default 6)")
    p_og.add_argument("--cache", default="", help="Cache path (default $POA_HOME/overlap-cache.json)")
    p_og.add_argument("--require-top", action="store_true", help="Exit 4 when no top-answer corpus could be checked")

    p_gp = sub.add_parser("grade-plan", help="Propose consensus-calibrated scores for dealt seats and write a grade-batch queue (never sends)")
    p_gp.add_argument("--seats", default="", help="JSON seats file (offline); default: one header-keyed read of /api/hosted/assignments/")
    p_gp.add_argument("--out", default="", help="Write the queue [{txid, score, note, epoch}] here")

    p_gc = sub.add_parser("grade-calibrate", help="Offline: agreement of grade-plan's scores with settled jury outcomes in a fixtures file")
    p_gc.add_argument("--fixtures", required=True, help="JSON [{answer, words, passed, score?}]")
    p_gc.add_argument("--min-agreement", type=float, default=0.76, help="Exit 2 below this agreement (default 0.76)")

    p_sc = sub.add_parser("seat-check", help="Hourly-friendly: one keyless assignments read (+ --keyed established flag); reports only changes with --quiet; never grades")
    p_sc.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address); never printed")
    p_sc.add_argument("--keyed", action="store_true", help="Also read /api/hosted/me (read-only) for the established flag")
    p_sc.add_argument("--quiet", action="store_true", help="Print nothing when nothing changed since the last run")

    p_ans = sub.add_parser("answer", help="Submit epoch challenge answer from UTF-8 file")
    p_ans.add_argument("--epoch", type=int, default=None, help="Use the cached challenge for this epoch if live reads fail")
    p_ans.add_argument("--no-confirm", action="store_true", help="Skip the >=3 min explorer confirmation wait")
    p_ans.add_argument("--read-retries", type=int, default=3, help="Live challenge read attempts before falling back to cache")
    p_rc = sub.add_parser("receipt", help="Keyless write receipt by txid: landed-final / landed-pending / not-found (direct 404) / unknown (timeout, 3xx redirect, 429/503 or a cached miss; never 'failed')")
    p_rc.add_argument("txid", nargs="+")
    p_rc.add_argument("--timeout", type=float, default=20)
    p_st = sub.add_parser("answer-status", help="Keyless explorer check: did our answer for an epoch land? (never sends)")
    p_st.add_argument("--epoch", type=int, default=None)
    p_st.add_argument("--wait", type=int, default=0, help="Keep polling up to N seconds")
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
    p_bl.add_argument("--mine", action="store_true", help="Keyless: list OUR OWN pending answers (panel, grades, quorum gap, expiry, cannot-settle flag)")
    p_bl.add_argument("--table", action="store_true", help="With --mine: plain-text table instead of JSON")
    p_bl.add_argument("--log", default="", help="With --mine: handoff LOG.md for our answer txids (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")

    p_ch = sub.add_parser("close-health", help="Keyless: health of the newest epoch close (attesters vs K, eligible, settled, removed, burned, your address)")
    p_ch.add_argument("--epoch", type=int, default=None, help="Close to read (default: newest)")
    p_ch.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address)")
    p_ch.add_argument("--no-address", action="store_true", help="Skip the per-address lines")
    p_ch.add_argument("--k", type=int, default=None, help="Attester threshold (default: K from /api/status, else 5)")
    p_ch.add_argument("--json", action="store_true", help="JSON output")
    p_ch.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")

    p_cc = sub.add_parser("close-check", help="Keyless: per close, was our answer graded, did it pass, were we paid (payout per close; never a balance or address)")
    p_cc.add_argument("--last", type=int, default=4, help="How many recent closes to check (default 4)")
    p_cc.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address); never printed")
    p_cc.add_argument("--log", default="", help="handoff LOG.md for answer txids (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")
    p_cc.add_argument("--json", action="store_true", help="JSON output")
    p_cc.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")

    p_wd = sub.add_parser("write-drill", help="Offline crash-recovery drill for answer writes: counts sends across a simulated restart (fake server, no network, no key); exit 3 if any case sends twice")
    p_wd.add_argument("--json", action="store_true", help="JSON output")

    p_sd = sub.add_parser("standing", help="Own-standing row per close: our settled answers (passed, aligned jurors), eligible, attested, established_after/removed, payout; flags passed-and-eligible-but-not-established")
    p_sd.add_argument("--last", type=int, default=0, help="Only the last N closes (default: every close in /api/epochs)")
    p_sd.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address); never printed")
    p_sd.add_argument("--log", default="", help="handoff LOG.md for answer txids (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")
    p_sd.add_argument("--no-me", action="store_true", help="Skip the keyed /api/hosted/me read (fully keyless)")
    p_sd.add_argument("--json", action="store_true", help="JSON output")
    p_sd.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")

    p_in = sub.add_parser("intake", help="Keyless: rebuild each close's establishment draw (sha256 intake order) from /api/epochs; your rank N of M, admitted or not, and when standing lapses")
    p_in.add_argument("--last", type=int, default=0, help="Only the last N closes (default: every close)")
    p_in.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address); never printed")
    p_in.add_argument("--network", action="store_true", help="Network rows only, no address")
    p_in.add_argument("--json", action="store_true", help="JSON output")
    p_in.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")

    p_jb = sub.add_parser("jobs", help="Saved busy-node (202) jobs: --check polls each queued job once (done / still queued / tombstone when expired); never resends")
    p_jb.add_argument("--check", action="store_true", help="Poll each queued job once and update the store")
    p_jb.add_argument("--epoch", type=int, default=None, help="Only this epoch's jobs")
    p_jb.add_argument("--prune", action="store_true", help="Drop done/failed/invalid rows, keep queued and tombstones")
    p_jb.add_argument("--json", action="store_true", help="JSON output (job ids masked)")

    p_et = sub.add_parser("epoch-time", help="Keyless: every epoch-counted setting (TTLs, vesting, suspensions) in hours/days at nominal and observed close cadence, plus next close estimate")
    p_et.add_argument("--window", type=int, default=8, help="Close gaps to average for the observed cadence (default 8; pauses excluded)")
    p_et.add_argument("--json", action="store_true", help="JSON output")
    p_et.add_argument("--timeout", type=int, default=90, help="Timeout for the large /api/epochs read (default 90s)")

    p_cp = sub.add_parser("close-pull", help="Keyless: wait for the next close, pull public assignments right away and a few minutes later, log seconds-since-close, rows, backlog vs fresh, seals_seen")
    p_cp.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address); not printed")
    p_cp.add_argument("--now", action="store_true", help="Don't wait for a close; pull once now (times are since the newest close)")
    p_cp.add_argument("--followup", type=int, action="append", help="Extra pull N seconds after the close record (repeatable, max 3; default 300)")
    p_cp.add_argument("--no-followup", action="store_true", help="Only the first pull")
    p_cp.add_argument("--max-wait", type=int, default=900, help="Give up if no close within this many seconds (default 900)")
    p_cp.add_argument("--interval", type=int, default=20, help="Seconds between small /api/status polls while waiting (min 15, default 20)")
    p_cp.add_argument("--log", default="", help="JSONL log path (default ~/.poa/close-pull.jsonl)")
    p_cp.add_argument("--no-log", action="store_true", help="Don't append to the log")
    p_cp.add_argument("--json", action="store_true", help="JSON output")

    p_led = sub.add_parser("ledger", help="No key. status: live checkpoint height (/api/status + POAScan anchors); params, pardons, epoch: GitHub ledger mirror")
    p_led.add_argument("action", choices=["status", "params", "pardons", "epoch"], help="Ledger action")
    p_led.add_argument("n", nargs="?", type=int, default=None, help="Epoch number for `ledger epoch N`")
    p_led.add_argument("--branch", default=LEDGER_DEFAULT_BRANCH, help="Ledger branch (default poa-mainnet-1)")
    p_led.add_argument("--diff", default="", help="For params: compare against a commit/branch, or 'prev' for the prior params.json commit")
    p_led.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address)")
    p_led.add_argument("--full", action="store_true", help="For epoch: print the raw emission.json")
    p_led.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p_led.add_argument("--mirror", action="store_true", help="For status: also show the GitHub mirror's newest checkpoint (information only)")

    p_health = sub.add_parser("health", help="Read-only probe: REST status/challenge + MCP; tells outage (server) from key trouble (auth)")
    p_health.add_argument("--timeout", type=float, default=15, help="Per-probe timeout seconds (default 15)")
    p_health.add_argument("--mcp-keyed", action="store_true", help="Also call MCP poa_challenge with the key (read-only)")

    p_pcs = sub.add_parser("pc-silence", help="Flag when the PC node's newest HEARTBEAT in poa-handoff LOG.md is older than N hours")
    p_pcs.add_argument("--log", default="", help="Path to LOG.md (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")
    p_pcs.add_argument("--node", default="antigravity", help="Node name to check (default antigravity)")
    p_pcs.add_argument("--hours", type=float, default=2.0, help="Silence threshold in hours (default 2)")

    p_ro = sub.add_parser("reopen", help="After a halt: one probe, lock, answer current epoch once (handoff-guarded), then seal queued grades oldest first")
    p_ro.add_argument("--node", default=os.environ.get("POA_NODE", "antigravity"), help="This node's name in LOG.md")
    p_ro.add_argument("--primary", default="antigravity", help="Primary node (backup answers only after --hours silence)")
    p_ro.add_argument("--hours", type=float, default=2.0)
    p_ro.add_argument("--log", default="", help="Path to LOG.md (default $POA_HANDOFF_LOG)")
    p_ro.add_argument("--answer-file", default="", help="UTF-8 answer for the current epoch")
    p_ro.add_argument("--grades-file", default="", help="JSON [{txid, score, note, epoch}] of queued grades")
    p_ro.add_argument("--git-sync", action="store_true", help="pull/commit/push the handoff repo around the CLAIM")
    p_ro.add_argument("--dry-run", action="store_true", help="Probe and check only; send nothing")
    p_ro.add_argument("--ignore-running", action="store_true", help="Proceed even if other poa.py processes are running")
    p_ro.add_argument("--timeout", type=float, default=20)
    p_ro.add_argument("--confirm-min", type=int, default=EXPLORER_MIN_WAIT)
    p_ro.add_argument("--confirm-max", type=int, default=EXPLORER_MAX_WAIT)

    p_tk = sub.add_parser("takeover-check", help="Read-only: should the backup answer this epoch? (6h epochs; PC silence >2h or epoch >45 min old with no PC claim)")
    p_tk.add_argument("--log", default="", help="Path to LOG.md (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")
    p_tk.add_argument("--node", default="antigravity", help="Primary node name (default antigravity)")
    p_tk.add_argument("--hours", type=float, default=2.0, help="PC heartbeat silence threshold in hours (default 2)")
    p_tk.add_argument("--after-min", type=float, default=float(os.environ.get("POA_TAKEOVER_AFTER_MIN", "45")),
                      help="Take over when the epoch is older than this many minutes with no PC CLAIM/ANSWERED (default 45, or $POA_TAKEOVER_AFTER_MIN)")

    args = parser.parse_args()
    try:
        if args.command == "init":
            cmd_init(args)
        elif args.command == "me":
            try:
                res = label_me_counts(api_call("GET", "/api/hosted/me"))
                res["answered_in_local_log"] = has_answered_epoch_locally(res.get("epoch"))
                print(json.dumps(res, indent=2))
                print_notice(res)
            except PoaHTTPError as e:
                if e.kind == "auth":
                    raise
                edge_me = edge_read_epochs_summary()
                edge_ch = edge_read_challenge()
                if not edge_me and not edge_ch:
                    raise
                fallback = {
                    "hosted_status": e.kind,
                    "hosted_error": str(e)[:200],
                    "epoch": edge_ch.get("epoch"),
                    "answered": edge_ch.get("answered"),
                    "answered_in_local_log": edge_ch.get("answered_in_local_log"),
                    "answered_on_explorer": edge_ch.get("answered_on_explorer"),
                    "balance": edge_me.get("cumulative_settled_balance"),
                    "established": edge_me.get("established_from_epochs"),
                    "rewards_by_epoch": edge_me.get("rewards_by_epoch"),
                    "_via": "edge:/api/epochs+/api/challenge",
                }
                if edge_ch.get("system_notice"):
                    fallback["system_notice"] = edge_ch["system_notice"]
                print(json.dumps(fallback, indent=2))
                print_notice(fallback)
        elif args.command == "challenge":
            try:
                res = read_challenge()
                res["answered_in_local_log"] = has_answered_epoch_locally(res.get("epoch"))
                print(json.dumps(res, indent=2))
                print_notice(res)
            except PoaHTTPError as e:
                edge_ch = getattr(e, "edge_challenge", None)
                if not isinstance(edge_ch, dict) or not edge_ch.get("words"):
                    raise
                edge_ch["hosted_status"] = e.kind
                edge_ch["hosted_error"] = str(e)[:200]
                print(json.dumps(edge_ch, indent=2))
                print_notice(edge_ch)
        elif args.command == "check":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            try:
                ch = read_challenge()
            except PoaHTTPError as e:
                edge_ch = getattr(e, "edge_challenge", None)
                if isinstance(edge_ch, dict) and edge_ch.get("words"):
                    ch = edge_ch
                else:
                    raise
            report = check_answer_quality(text, ch.get("words", []))
            report["epoch"] = ch.get("epoch")
            report["already_answered"] = is_challenge_answered(ch, ch.get("epoch"))
            report["answered_in_local_log"] = has_answered_epoch_locally(ch.get("epoch"))
            report["required_words"] = ch.get("words", [])
            if ch.get("_via"):
                report["challenge_source"] = ch.get("_via")
            print(json.dumps(report, indent=2))
            if not report["passed"]:
                sys.exit(2)
        elif args.command == "overlap-guard":
            cmd_overlap_guard(args)
        elif args.command == "grade-plan":
            cmd_grade_plan(args)
        elif args.command == "grade-calibrate":
            cmd_grade_calibrate(args)
        elif args.command == "seat-check":
            cmd_seat_check(args)
        elif args.command == "check-tournament":
            if args.words.strip():
                ch = {"epoch": None, "words": [w.strip() for w in args.words.split(",") if w.strip()], "_via": "--words"}
            else:
                try:
                    ch = read_challenge()
                except PoaHTTPError as e:
                    edge_ch = getattr(e, "edge_challenge", None)
                    if isinstance(edge_ch, dict) and edge_ch.get("words"):
                        ch = edge_ch
                    else:
                        raise
            words = ch.get("words", [])
            guard_corpus = [] if args.no_overlap_guard else overlap_corpus(pathlib.Path(args.overlap_cache).expanduser() if args.overlap_cache else None)
            candidates = []
            for fpath in args.files:
                p = pathlib.Path(fpath)
                txt = p.read_text(encoding="utf-8").strip()
                rep = check_answer_quality(txt, words)
                rep["file"] = str(fpath)
                rep["_text"] = txt
                if not args.no_overlap_guard:
                    g = overlap_guard(txt, guard_corpus, threshold=args.overlap_threshold)
                    rep["overlap_guard"] = {k: g[k] for k in ("refused", "max_overlap", "max_overlap_src", "max_overlap_epoch", "compared_top", "compared_self", "verdict")}
                    if g["refused"]:
                        rep["passed"] = False
                candidates.append(rep)
            # Rank: passed (incl. overlap guard) > on-topic (no drift) > clean pass (no inflected words/warnings);
            # then combined score (0.6 plain style + 0.4 rubric) desc, overlap asc, distance from 565 asc
            def _rank_key(c):
                clean_pass = 1 if (c["passed"] and not c["inflected_words"] and not c["warnings"]) else 0
                any_pass = 1 if c["passed"] else 0
                return (
                    any_pass,
                    0 if c.get("drift") else 1,
                    clean_pass,
                    c.get("combined_score", c.get("rubric_score", 0)),
                    -c.get("max_shingle_overlap", 1.0),
                    -abs(c.get("length", 0) - 565),
                )
            ranked = sorted(candidates, key=_rank_key, reverse=True)
            winner = ranked[0] if ranked and ranked[0]["passed"] else None
            written = []
            if winner:
                for out_path in (args.out, args.out_epoch):
                    if out_path:
                        op = pathlib.Path(out_path)
                        op.parent.mkdir(parents=True, exist_ok=True)
                        op.write_text(winner["_text"] + "\n", encoding="utf-8")
                        written.append(str(out_path))
            for c in ranked:
                c.pop("_text", None)
            out_rep = {
                "epoch": ch.get("epoch"),
                "required_words": words,
                "winner_file": winner["file"] if winner else None,
                "winner_rubric_score": winner["rubric_score"] if winner else None,
                "winner_style_score": winner["style_score"] if winner else None,
                "overlap_guard": ("off" if args.no_overlap_guard else {"threshold": args.overlap_threshold,
                                  "compared_top": sum(1 for c in guard_corpus if str(c.get("src", "")).startswith("top")),
                                  "compared_self": sum(1 for c in guard_corpus if str(c.get("src", "")).startswith("self"))}),
                "written_to": written,
                "candidates": ranked,
            }
            print(json.dumps(out_rep, indent=2))
            if not winner:
                sys.exit(2)
        elif args.command == "answer":
            text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
            ch, source, read_err = None, "live", None
            try:
                ch = read_challenge(retries=args.read_retries)
            except PoaHTTPError as e:
                if e.kind == "auth":
                    raise
                if e.kind == "halted":
                    print(json.dumps({"landed": False, "kind": "halted", "error": str(e)[:300],
                                      "note": "Operator maintenance halt (HTTP 503); do not submit until surface re-enables."}, indent=2))
                    sys.exit(2)
                read_err = str(e)[:200]
            if ch is None:
                # Outage path: answer from the words+seed cached by an earlier successful read this epoch.
                cached = load_epoch_cache(args.epoch)
                win = epoch_window()
                first = cached.get("last_read") or cached.get("first_read") or ""
                if not cached.get("words"):
                    print(json.dumps({"landed": False, "error": "challenge unreadable and no cached words/seed", "read_error": read_err}, indent=2))
                    sys.exit(2)
                if first < win["window_start"] and args.epoch is None:
                    print(json.dumps({"landed": False, "error": f"cached epoch {cached.get('epoch')} predates the current 6h window ({win['window_start']}); stale seed, not sending",
                                      "read_error": read_err}, indent=2))
                    sys.exit(2)
                ch, source = {k: cached.get(k) for k in ("epoch", "words", "seed", "superseded", "answered_txid")}, "cache"
            epoch = ch.get("epoch")
            if is_challenge_answered(ch, epoch):
                print(json.dumps({"skipped": True, "epoch": epoch, "note": "Epoch already answered (on server, in ~/.poa/answers.log, or in poa-handoff/LOG.md)."}, indent=2))
                return
            report = check_answer_quality(text, ch.get("words", []))
            if not report["passed"]:
                raise RuntimeError(f"Pre-flight check failed: {json.dumps(report)}")
            sub_res = submit_answer_safely(text, epoch)
            sub_res.update({"epoch": epoch, "words": ch.get("words"), "challenge_source": source, "transport": http_transport()})
            if read_err:
                sub_res["challenge_read_error"] = read_err
            if sub_res.get("landed") is not False and sub_res.get("how") not in ("duplicate",) and not args.no_confirm:
                conf = confirm_answer_landed(epoch, sub_res.get("txid"))
                sub_res["confirm"] = conf
                if conf.get("landed"):
                    save_epoch_cache(epoch, landed=True, confirmed_via=conf.get("how"), txid_answer=(conf.get("evidence") or {}).get("txid") or sub_res.get("txid"))
                    if sub_res.get("landed") is None:
                        sub_res["landed"], sub_res["how"] = True, "explorer"
            if sub_res.get("landed") and sub_res.get("how") not in ("sent_before",):
                POA_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with ANSWERS_LOG.open("a", encoding="utf-8") as fp:
                    fp.write(f"{ts}\tchain={MAINNET_CHAIN_ID}\tepoch={epoch}\twords={','.join(ch.get('words', []))}\t{text}\n")
            print(json.dumps(sub_res, indent=2, default=str))
            if isinstance(sub_res.get("result"), dict):
                print_notice(sub_res["result"])
            if sub_res.get("landed") is False:
                sys.exit(2)
            if sub_res.get("landed") is None:
                sys.exit(3)
        elif args.command == "receipt":
            res = [write_receipt(t, timeout=args.timeout) for t in args.txid]
            print(json.dumps(res[0] if len(res) == 1 else res, indent=2, default=str))
            codes = {"landed-final": 0, "landed-pending": 0, "not-found": 4, "unknown": 3, "invalid": 2, "rejected": 5}
            sys.exit(max(codes[r["outcome"]] for r in res))
        elif args.command == "answer-status":
            cached = load_epoch_cache(args.epoch)
            ep = args.epoch if args.epoch is not None else cached.get("epoch")
            conf = confirm_answer_landed(ep, cached.get("txid_answer"), min_wait=0, max_wait=args.wait)
            if conf.get("landed"):
                save_epoch_cache(ep, landed=True, confirmed_via=conf.get("how"), txid_answer=(conf.get("evidence") or {}).get("txid") or cached.get("txid_answer"))
            print(json.dumps({"epoch": ep, "cache": {k: cached.get(k) for k in ("words", "sent_at", "send_outcome", "landed", "txid_answer")}, **conf}, indent=2, default=str))
            sys.exit(0 if conf.get("landed") else 3)
        elif args.command == "mine":
            res = safe_mine()
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "assignments":
            res = hosted_assignments()
            if args.audit:
                seats = res.get("assignments", []) or []
                audited = audit_assignments_batch(seats)
                audited.sort(key=lambda a: (a.get("epoch") if isinstance(a.get("epoch"), int) else 10**9))
                out = {"count": len(audited), "order": "oldest answer epoch first", "seats": audited}
                if res.get("backlog") is not None:
                    out["backlog"] = res.get("backlog")
                print(json.dumps(out, indent=2))
            else:
                print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "grade":
            r = submit_grade_safely(args.assignment, args.score, args.reasoning.strip())
            print(json.dumps(r, indent=2))
            if isinstance(r.get("result"), dict):
                print_notice(r["result"])
            if not r.get("landed"):
                sys.exit(2)
        elif args.command == "backlog":
            cmd_backlog(args)
        elif args.command == "close-health":
            cmd_close_health(args)
        elif args.command == "close-check":
            cmd_close_check(args)
        elif args.command == "standing":
            cmd_standing(args)
        elif args.command == "write-drill":
            cmd_write_drill(args)
        elif args.command == "join-pow":
            cmd_join_pow(args)
        elif args.command == "intake":
            cmd_intake(args)
        elif args.command == "jobs":
            cmd_jobs(args)
        elif args.command == "epoch-time":
            cmd_epoch_time(args)
        elif args.command == "close-pull":
            cmd_close_pull(args)
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
            res = hosted_write(lambda: api_call(
                "GET",
                "/api/hosted/vote",
                query={"ballot": args.ballot, "topic": args.topic, "choice": args.choice},
            ))
            print(json.dumps(res, indent=2))
            print_notice(res)
        elif args.command == "network":
            res = label_status_counts(api_call("GET", "/api/status", need_key=False))
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
                    headers={"Accept": "text/markdown, text/plain", "User-Agent": USER_AGENT},
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
        elif args.command == "health":
            cmd_health(args)
        elif args.command == "pc-silence":
            res = pc_silence(pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG, args.node, args.hours)
            print(json.dumps(res, indent=2))
        elif args.command == "reopen":
            cmd_reopen(args)
        elif args.command == "takeover-check":
            cmd_takeover_check(args)
        elif args.command == "pulse":
            mine_res = None
            mine_err = None
            if args.mine:
                try:
                    mine_res = safe_mine()
                except Exception as e:
                    mine_err = redact(str(e))
            hosted_status = "ok"
            hosted_err = None
            try:
                me_res = api_call("GET", "/api/hosted/me")
            except PoaHTTPError as e:
                if e.kind == "auth":
                    raise
                hosted_status = e.kind
                hosted_err = str(e)[:200]
                me_res = {}
            try:
                ch_res = read_challenge()
            except PoaHTTPError as e:
                if e.kind == "auth":
                    raise
                hosted_status = e.kind
                hosted_err = hosted_err or str(e)[:200]
                ch_res = getattr(e, "edge_challenge", None) or {}
            try:
                asgn_res = hosted_assignments() if hosted_status != "halted" else {"assignments": []}
            except PoaHTTPError as e:
                if e.kind == "auth" and not me_res.get("address") and not ch_res.get("words"):
                    raise
                asgn_res = {"assignments": [], "assignments_error": str(e)[:200]}
            edge_me = edge_read_epochs_summary() if not me_res.get("balance") else {}
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
            me_ch = me_res.get("challenge") if isinstance(me_res.get("challenge"), dict) else {}
            epoch_num = ch_res.get("epoch") if ch_res.get("epoch") is not None else me_ch.get("epoch")
            local_ans = has_answered_epoch_locally(epoch_num)
            ans_txid = ch_res.get("answered_txid") or me_ch.get("answered_txid")
            summary = {
                "hosted_status": hosted_status,
                "epoch": epoch_num,
                "answered": bool(is_challenge_answered(ch_res, epoch_num) or me_ch.get("answered") or ans_txid),
                "server_answered_flag": bool(ch_res.get("answered") or me_ch.get("answered")),
                "answered_txid": ans_txid,
                "answered_in_local_log": local_ans,
                "words": ch_res.get("words", []),
                "balance": me_res.get("balance") or edge_me.get("cumulative_settled_balance"),
                "spendable": me_res.get("spendable"),
                "locked": me_res.get("locked"),
                "established": me_res.get("established") if "established" in me_res else edge_me.get("established_from_epochs"),
                "attestations_by_me": me_res.get("attestations_by_me"),
                "attestations_by_me_scope": SINCE_CHECKPOINT_SCOPE,
                "mined_this_pulse": mine_res.get("attested") if isinstance(mine_res, dict) else 0,
                "open_grading_seats_count": len(seats),
                "open_grading_seats": seats,
                "open_ballots": compact_ballots,
                "notices": me_res.get("notices", []),
            }
            if asgn_res.get("assignments_error"):
                summary["assignments_error"] = asgn_res["assignments_error"]
            if asgn_res.get("backlog") is not None:
                summary["assignments_backlog"] = asgn_res.get("backlog")
            if edge_me.get("rewards_by_epoch") and not me_res.get("balance"):
                summary["rewards_by_epoch"] = edge_me.get("rewards_by_epoch")
                summary["balance_source"] = "edge:/api/epochs"
            if ch_res.get("_via"):
                summary["challenge_source"] = ch_res.get("_via")
            if hosted_err:
                summary["hosted_error"] = hosted_err
            if mine_err:
                summary["mine_error"] = mine_err
            print(json.dumps(summary, indent=2))
            print_notice(ch_res)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {redact(str(exc))}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
