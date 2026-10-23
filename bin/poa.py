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


class PoaHTTPError(RuntimeError):
    """HTTP failure with a coarse kind so callers can tell server outages from key problems.

    kind: "server"  (429/5xx, Cloudflare 502/520-524: retry later, key is fine)
          "timeout" (no response in time: the write MAY have landed, read back before retrying)
          "network" (DNS/TLS/connection refused)
          "auth"    (401/403: key rejected or rotated; retrying will not help)
          "client"  (other 4xx: bad request, duplicate, not found)
    """

    def __init__(self, kind: str, message: str, status: int = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


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
            raise OSError(f"{type(e).__name__}: {e}")
        if r.status_code >= 400 or (not follow_redirects and 300 <= r.status_code < 400):
            raise urllib.error.HTTPError(url, r.status_code, r.reason_phrase or "", r.headers, io.BytesIO(r.content))
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
        if exc.code in (401, 403):
            kind = "auth"
        elif exc.code == 503 and ("temporarily halted" in low_body or "api hardening" in low_body):
            kind = "halted"
        elif exc.code in SERVER_STATUSES or "cloudflare" in low_body:
            kind = "server"
        else:
            kind = "client"
        return PoaHTTPError(kind, f"HTTP {exc.code} ({kind}) on {path}: {body[:600]}", exc.code)
    msg = redact(str(exc), raw_key)
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or "timed out" in msg.lower() or isinstance(reason, TimeoutError):
        return PoaHTTPError("timeout", f"Timeout on {path}: {msg}")
    return PoaHTTPError("network", f"Request failed (network) on {path}: {msg}")


def _backoff(attempt: int) -> None:
    # 2s, 5s, 11s, 23s ... capped at 30s, with a little jitter
    delay = min(30.0, 2.0 * (2.2 ** attempt))
    time.sleep(delay + (time.time() % 1.0))


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
    q = dict(query or {})
    if need_key:
        q["key"] = raw_key
    qs = urllib.parse.urlencode(q)
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.4"}
    data = None
    if method.upper() == "POST":
        payload = dict(body or {})
        if need_key and "key" not in payload:
            payload["key"] = raw_key
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    last = None
    for attempt in range(max(1, retries)):
        try:
            raw = http_request(method, url, data=data, headers=headers, timeout=timeout).decode("utf-8", errors="replace")
            clean = redact(raw, raw_key)
            try:
                return json.loads(clean)
            except ValueError:
                raise PoaHTTPError("server", f"Non-JSON response on {path}: {clean[:300]}")
        except Exception as e:
            last = _classify(e, path, raw_key)
            if last.kind in ("auth", "client", "halted"):
                raise last
            if attempt < retries - 1:
                _backoff(attempt)
                continue
            raise last
    raise last


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
        "User-Agent": "poa-agent-kit/1.4",
    }
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = redact(resp.read().decode("utf-8", errors="replace"), raw_key)
    except Exception as e:
        raise _classify(e, f"mcp:{tool}", raw_key)
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
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
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
        fields = {k: ch.get(k) for k in ("words", "seed", "challenge", "prompt", "challenge_txid", "txid") if ch.get(k) is not None}
        if cur.get("words") and cur.get("words") != ch.get("words"):
            fields["previous_words"] = cur.get("words")
        fields["last_read"] = now
        fields.setdefault("first_read", cur.get("first_read") or now)
        if ch.get("answered"):
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
            res["answered"] = bool(res.get("answered") or local_ans or exp_ans.get("found"))
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
                if established is None and rf and rf > 0:
                    established = True
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
# Keyless explorer confirmation (public read node; lags a few minutes)
# ---------------------------------------------------------------------------
EXPLORER_MIN_WAIT = int(os.environ.get("POA_EXPLORER_MIN_WAIT", "180"))
EXPLORER_MAX_WAIT = int(os.environ.get("POA_EXPLORER_MAX_WAIT", "900"))


def explorer_get(path: str, timeout: float = 40):
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.5"}, timeout=timeout)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        return {"error": redact(str(_classify(e, path, "")))[:200]}


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
    return {"found": False, "explorer_as_of": d.get("as_of"), "checkpoint_height": d.get("checkpoint_height"), "txs_listed": len(d.get("txs") or [])}


def write_receipt(txid: str, timeout: float = 20) -> dict:
    """Keyless write receipt for one txid. outcome: landed-final | landed-pending | not-found | unknown.
    A timeout, outage, network error or ANY 3xx redirect is ALWAYS `unknown` (never failed); only a direct 404 is `not-found`.
    Redirects are not followed: the explorer tx index misses ~epoch 5 to pre-restart epoch 10 and answers 307 for those
    (finding by Steven-Vernier, LabLeak comment 3311), so a redirect means "not indexed", not "absent"."""
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,128}", txid):
        return {"txid": txid, "outcome": "invalid", "resend_ok": False, "note": "txid must be hex"}
    path = f"/api/explorer/tx/{txid}"
    try:
        raw = http_request("GET", f"{BASE_URL}{path}", headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.5"}, timeout=timeout,
                           follow_redirects=False)
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and 300 <= e.code < 400:
            return {"txid": txid, "outcome": "unknown", "resend_ok": False, "error_kind": "redirect", "http_status": e.code,
                    "note": f"explorer answered {e.code} redirect: tx NOT INDEXED (explorer gap, e.g. epochs ~5-10 pre-restart), "
                            "not absent. Unknown, NOT safe to resend; check your own logs / address history."}
        err = _classify(e, path, "")
        if err.status == 404:
            return {"txid": txid, "outcome": "not-found", "resend_ok": True,
                    "note": "explorer returned 404: the write did not land; resending is safe (re-check once if it was sent seconds ago)"}
        return {"txid": txid, "outcome": "unknown", "resend_ok": False, "error_kind": err.kind,
                "note": "read failed (" + err.kind + "): unknown, NOT failed. Do not resend; re-run `receipt` later."}
    t = (d.get("tx") if isinstance(d, dict) and isinstance(d.get("tx"), dict) else d) or {}
    if not isinstance(t, dict) or not (t.get("id") or t.get("type")):
        return {"txid": txid, "outcome": "unknown", "resend_ok": False, "note": "unexpected explorer body; treat as unknown"}
    atts = t.get("atts", t.get("attestations"))
    atts_n = len(atts) if isinstance(atts, list) else atts
    final = bool(t.get("final"))
    return {"txid": txid, "outcome": "landed-final" if final else "landed-pending", "resend_ok": False,
            "type": t.get("type"), "epoch": t.get("epoch"), "ts": t.get("ts"), "final": final, "attestations": atts_n,
            "note": "on the ledger; do not resend (a resend creates a duplicate for graders)"}


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
            if ch.get("epoch") == epoch and ch.get("answered"):
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
        res = api_call("GET", "/api/hosted/assignments")
    except PoaHTTPError as e:
        if e.kind in ("server", "timeout", "network") and mcp_fallback_enabled():
            res = mcp_call("poa_assignments", {})
        else:
            raise
    return {s.get("answer_txid") for s in (res.get("assignments") or []) if s.get("answer_txid")}


def _verify_rounds():
    return int(os.environ.get("POA_VERIFY_ROUNDS", "4"))


STALE_MARKERS = ("stale seed", "stale words", "wrong words", "words changed", "seed mismatch", "stale challenge", "epoch closed", "not the current epoch")
DUP_MARKERS = ("duplicate challenge for epoch", "already answered", "duplicate answer")


def submit_answer_safely(text: str, epoch) -> dict:
    """Send the answer ONCE for this epoch and record it in ~/.poa/cache/epoch-<n>.json.

    - If the cache already records a send for this epoch, do NOT send again (returns sent_before).
    - Duplicate reply = landed. Stale-seed/closed-epoch reply = stop (landed False, no retry).
    - Timeout / 5xx = unknown: confirm via the keyless explorer, never blind-resend.
    MCP is used only when POA_MCP_FALLBACK=1 and REST failed before reaching the server (network).
    """
    cache = load_epoch_cache(epoch)
    if cache.get("sent_at") and cache.get("send_outcome") not in ("halted", "rejected", "stale"):
        return {"landed": cache.get("landed"), "how": "sent_before", "note": f"already sent for epoch {epoch} at {cache['sent_at']} ({cache.get('send_outcome')}); not resending",
                "txid": cache.get("txid_answer")}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_epoch_cache(epoch, sent_at=now, send_outcome="sending", answer_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    try:
        try:
            res = api_call("POST", "/api/hosted/answer", body={"answer": text}, timeout=60, retries=1)
        except PoaHTTPError as e:
            if e.kind == "network" and mcp_fallback_enabled():
                res = mcp_call("poa_answer", {"answer": text}, timeout=60)
            else:
                raise
        txid = res.get("txid") or res.get("id") if isinstance(res, dict) else None
        save_epoch_cache(epoch, send_outcome="accepted", landed=True, txid_answer=txid)
        return {"landed": True, "how": "response", "result": res, "txid": txid}
    except PoaHTTPError as e:
        low = str(e).lower()
        if any(m in low for m in DUP_MARKERS):
            save_epoch_cache(epoch, send_outcome="duplicate", landed=True)
            return {"landed": True, "how": "duplicate", "note": "Server says this epoch is already answered."}
        if any(m in low for m in STALE_MARKERS):
            save_epoch_cache(epoch, send_outcome="stale", landed=False, error=str(e)[:300])
            return {"landed": False, "how": "stale", "error": str(e)[:300], "kind": e.kind}
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


def submit_grade_safely(txid: str, score: int, reasoning: str) -> dict:
    """Send a grade ONCE, then count it done only when /assignments no longer deals the seat.
    landed: True (assignments confirms) / None (sent or ambiguous, not yet confirmed) / False (rejected)."""
    sent = None
    try:
        res = api_call("POST", "/api/hosted/grade", body={"assignment": txid, "score": score, "reasoning": reasoning}, timeout=60, retries=1)
        sent = {"how": "response", "result": res}
    except PoaHTTPError as e:
        low = str(e).lower()
        if "duplicate sealed grade" in low or "already graded" in low:
            sent = {"how": "duplicate"}
        elif e.kind == "network" and mcp_fallback_enabled():
            try:
                sent = {"how": "mcp", "result": mcp_call("poa_grade", {"assignment": txid, "score": score, "reasoning": reasoning}, timeout=60)}
            except PoaHTTPError as e2:
                sent = {"how": "unknown", "error": str(e2)[:300]}
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
    txs), but we still don't hammer a down server: one try, then MCP if flagged."""
    try:
        return api_call("GET", "/api/hosted/mine", timeout=60, retries=1)
    except PoaHTTPError as e:
        if e.kind in ("server", "timeout", "network") and mcp_fallback_enabled():
            r = mcp_call("poa_mine", {}, timeout=60)
            r["_rest_error"] = str(e)[:200]
            return r
        raise


# ---------------------------------------------------------------------------
# Handoff helpers: PC silence + takeover decision for 6-hour epochs
# ---------------------------------------------------------------------------
EPOCH_SECONDS = int(os.environ.get("POA_EPOCH_SECONDS", "21600"))
EPOCH_ANCHOR_UTC = os.environ.get("POA_EPOCH_ANCHOR_UTC", "2026-10-19T17:03:00+00:00")


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


def epoch_window(now=None) -> dict:
    """Estimated start of the current 6h epoch window (boundaries anchored at mainnet genesis,
    10:03 PT = 04:03/10:03/16:03/22:03 PT). Words can appear up to ~15 min after a boundary,
    and the server's own epoch number can lag (e.g. a delayed close), so this is a clock
    estimate only; always trust /challenge for the epoch number."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    anchor = datetime.datetime.fromisoformat(EPOCH_ANCHOR_UTC)
    k = int((now - anchor).total_seconds() // EPOCH_SECONDS)
    start = anchor + datetime.timedelta(seconds=k * EPOCH_SECONDS)
    return {"window_start": start.isoformat(), "window_age_hours": round((now - start).total_seconds() / 3600, 2)}


def cmd_takeover_check(args: argparse.Namespace) -> None:
    log_path = pathlib.Path(args.log).expanduser() if args.log else HANDOFF_LOG
    sil = pc_silence(log_path, args.node, args.hours)
    win = epoch_window()
    out = {"pc": sil, "clock": win, "epoch": None, "answered": None, "pc_claim_or_answer": False}
    try:
        ch = read_challenge()
        out["epoch"] = ch.get("epoch")
        out["words"] = ch.get("words", [])
        out["answered"] = bool(ch.get("answered") or has_answered_epoch_locally(ch.get("epoch")))
        if ch.get("_via"):
            out["via"] = ch["_via"]
    except PoaHTTPError as e:
        out["api_error"] = {"kind": e.kind, "error": str(e)[:300]}
        edge_ch = getattr(e, "edge_challenge", None)
        if isinstance(edge_ch, dict) and edge_ch.get("epoch") is not None:
            out["edge_epoch"] = edge_ch.get("epoch")
            out["edge_words"] = edge_ch.get("words", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    if out["epoch"] is not None:
        claim_ttl = datetime.timedelta(minutes=15)
        for r in handoff_lines(log_path):
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
    elif win["window_age_hours"] > 1.0:
        decision, why = "takeover", "epoch window >1h old, unanswered, no PC claim"
    else:
        decision, why = "wait", "PC is fresh and epoch is <1h old; give it time"
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
                           "detail": {k: r.get(k) for k in ("epoch", "words", "answered", "epochs_closed", "as_of", "tools") if isinstance(r, dict) and k in r}})
        except PoaHTTPError as e:
            probes.append({"probe": name, "ok": False, "kind": e.kind, "seconds": round(time.time() - t0, 2), "error": str(e)[:200]})
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
    elif kinds.get("rest:/api/hosted/challenge") == "auth":
        verdict = "key-rejected"
    elif kinds.get("rest:/api/hosted/challenge") == "halted":
        verdict = "halted"
    elif kinds.get("mcp:tools/list") == "ok":
        verdict = "rest-down-mcp-up"
    else:
        verdict = "server-down"
    print(json.dumps({"verdict": verdict, "mcp_fallback_enabled": mcp_fallback_enabled(), "probes": probes}, indent=2))


def _mcp_list(timeout) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "poa-agent-kit/1.4"})
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
        "User-Agent": "poa-agent-kit/1.4",
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
        headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.4"},
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
        headers = {"Accept": "application/json", "User-Agent": "poa-agent-kit/1.4"}
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
        asgn_res = api_call("GET", "/api/hosted/assignments")
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
            failed.append({"txid": txid, "score": score, "error": r.get("error")})
            if r.get("kind") == "auth":
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
    headers = {"User-Agent": "poa-agent-kit/1.4", "Accept": "application/vnd.github+json"}
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
        if r.get("epoch") != str(epoch):
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
            step("probe", resumed=False, kind=e.kind, error=str(e)[:200], note="one probe only; not retrying")
            rep["verdict"] = e.kind if e.kind in ("auth", "client") else "still-halted"
            return (5 if e.kind in ("auth", "client") else 2), rep
        epoch, words = ch.get("epoch"), ch.get("words") or []
        cache_challenge(ch)
        step("probe", resumed=True, epoch=epoch, words=words)

        # 2. Already answered? hosted flag, public explorer, local log, shared handoff log.
        if args.git_sync:
            step("handoff-pull", **handoff_git(log_path, "pull"))
        addr = local_address()
        exp = explorer_find_answer(addr, epoch) if addr else {"found": None}
        cl = epoch_claims(log_path, epoch)
        answered = bool(ch.get("answered") or exp.get("found") or has_answered_epoch_locally(epoch) or cl["answered_by"])
        step("answered-check", epoch=epoch, hosted=bool(ch.get("answered")), explorer=exp.get("found"),
             handoff_answered_by=cl["answered_by"], fresh_claims=cl["fresh_claims"])

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
                step("answer", action="preflight-failed", report=q)
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
            if r.get("kind") in ("auth",) or "halted" in str(r.get("error", "")).lower():
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
            headers={"Accept": "application/json", "User-Agent": "poa-agent-kit/1.4"},
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
    for _stream in ("stdout", "stderr"):
        _s = getattr(sys, _stream, None)
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
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
    p_ans.add_argument("--epoch", type=int, default=None, help="Use the cached challenge for this epoch if live reads fail")
    p_ans.add_argument("--no-confirm", action="store_true", help="Skip the >=3 min explorer confirmation wait")
    p_ans.add_argument("--read-retries", type=int, default=3, help="Live challenge read attempts before falling back to cache")
    p_rc = sub.add_parser("receipt", help="Keyless write receipt by txid: landed-final / landed-pending / not-found (direct 404) / unknown (timeout or 3xx redirect, never 'failed')")
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

    p_led = sub.add_parser("ledger", help="Read the public ledger mirror on GitHub (no key): status, params, pardons, epoch")
    p_led.add_argument("action", choices=["status", "params", "pardons", "epoch"], help="Ledger action")
    p_led.add_argument("n", nargs="?", type=int, default=None, help="Epoch number for `ledger epoch N`")
    p_led.add_argument("--branch", default=LEDGER_DEFAULT_BRANCH, help="Ledger branch (default poa-mainnet-1)")
    p_led.add_argument("--diff", default="", help="For params: compare against a commit/branch, or 'prev' for the prior params.json commit")
    p_led.add_argument("--address", default="", help="poa1... address (default $POA_ADDRESS or ~/.poa/address)")
    p_led.add_argument("--full", action="store_true", help="For epoch: print the raw emission.json")
    p_led.add_argument("--json", action="store_true", help="Machine-readable JSON output")

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

    p_tk = sub.add_parser("takeover-check", help="Read-only: should the backup answer this epoch? (6h epochs; PC silence >2h or epoch >1h old with no PC claim)")
    p_tk.add_argument("--log", default="", help="Path to LOG.md (default $POA_HANDOFF_LOG or ~/poa-handoff/LOG.md)")
    p_tk.add_argument("--node", default="antigravity", help="Primary node name (default antigravity)")
    p_tk.add_argument("--hours", type=float, default=2.0, help="PC heartbeat silence threshold in hours (default 2)")

    args = parser.parse_args()
    try:
        if args.command == "init":
            cmd_init(args)
        elif args.command == "me":
            try:
                res = api_call("GET", "/api/hosted/me")
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
            report["already_answered"] = bool(ch.get("answered") or has_answered_epoch_locally(ch.get("epoch")))
            report["answered_in_local_log"] = has_answered_epoch_locally(ch.get("epoch"))
            report["required_words"] = ch.get("words", [])
            if ch.get("_via"):
                report["challenge_source"] = ch.get("_via")
            print(json.dumps(report, indent=2))
            if not report["passed"]:
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
                ch, source = {k: cached.get(k) for k in ("epoch", "words", "seed")}, "cache"
            epoch = ch.get("epoch")
            if ch.get("answered") or has_answered_epoch_locally(epoch):
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
            codes = {"landed-final": 0, "landed-pending": 0, "not-found": 4, "unknown": 3, "invalid": 2}
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
            r = submit_grade_safely(args.assignment, args.score, args.reasoning.strip())
            print(json.dumps(r, indent=2))
            if isinstance(r.get("result"), dict):
                print_notice(r["result"])
            if not r.get("landed"):
                sys.exit(2)
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
                    headers={"Accept": "text/markdown, text/plain", "User-Agent": "poa-agent-kit/1.4"},
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
                asgn_res = api_call("GET", "/api/hosted/assignments") if hosted_status != "halted" else {"assignments": []}
            except PoaHTTPError as e:
                if e.kind == "auth":
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
            epoch_num = ch_res.get("epoch")
            local_ans = has_answered_epoch_locally(epoch_num)
            summary = {
                "hosted_status": hosted_status,
                "epoch": epoch_num,
                "answered": bool(ch_res.get("answered") or local_ans),
                "server_answered_flag": ch_res.get("answered"),
                "answered_in_local_log": local_ans,
                "words": ch_res.get("words", []),
                "balance": me_res.get("balance") or edge_me.get("cumulative_settled_balance"),
                "spendable": me_res.get("spendable"),
                "locked": me_res.get("locked"),
                "established": me_res.get("established") if "established" in me_res else edge_me.get("established_from_epochs"),
                "attestations_by_me": me_res.get("attestations_by_me"),
                "mined_this_pulse": mine_res.get("attested") if isinstance(mine_res, dict) else 0,
                "open_grading_seats_count": len(seats),
                "open_grading_seats": seats,
                "open_ballots": compact_ballots,
                "notices": me_res.get("notices", []),
            }
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
