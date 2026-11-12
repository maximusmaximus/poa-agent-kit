"""Single-writer lease tests (SUGGESTIONS.md #138).

Offline only: the "shared handoff repo" is a local bare git repo in a temp dir and each node is a
local clone, so lease pushes are real git compare-and-swap pushes with no network. http_request is
patched to fail, so nothing here can reach the live server; no real key is used.
"""
import datetime, importlib.util, json, os, pathlib, shutil, subprocess, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_ADDRESS", "poa1testaddressnotreal")
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}.items():
    os.environ.setdefault(k, v)
spec = importlib.util.spec_from_file_location("poa_single_writer", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

HAVE_GIT = shutil.which("git") is not None


def _no_network(*a, **k):
    raise AssertionError("test tried to reach the network")


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r.stdout


@unittest.skipUnless(HAVE_GIT, "git not installed")
class SingleWriterLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        _git(self.tmp, "clone", "-q", str(self.origin), str(seed))
        (seed / "LOG.md").write_text("# log\n")
        _git(seed, "add", "LOG.md")
        _git(seed, "commit", "-qm", "init")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.pc = self._clone("pc")
        self.box = self._clone("box")
        self.homes = {"pc": self.tmp / "home-pc", "box": self.tmp / "home-box"}
        self._orig_dir = poa.POA_DIR
        self._net = mock.patch.object(poa, "http_request", side_effect=_no_network)
        self._net.start()
        self._env = mock.patch.dict(os.environ, {"POA_SINGLE_WRITER": "1"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._net.stop()
        poa.POA_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone(self, name):
        d = self.tmp / name
        _git(self.tmp, "clone", "-q", str(self.origin), str(d))
        return d

    def as_node(self, name):
        """Switch the process to act as node `name` (its own POA_HOME and handoff clone)."""
        poa.POA_DIR = self.homes[name]
        os.environ["POA_NODE"] = "antigravity" if name == "pc" else "grokbot"
        os.environ["POA_HANDOFF_DIR"] = str(self.pc if name == "pc" else self.box)

    def remote_lease(self):
        return json.loads(_git(self.origin, "show", "main:LEASE.json"))

    def write_remote_lease(self, lease):
        """Plant a lease on the remote as some other actor would."""
        w = self._clone("writer-" + str(len(list(self.tmp.iterdir()))))
        (w / "LEASE.json").write_text(lease if isinstance(lease, str) else json.dumps(lease))
        _git(w, "add", "LEASE.json")
        _git(w, "commit", "-qm", "plant lease")
        _git(w, "push", "-q", "origin", "HEAD:main")

    @staticmethod
    def iso(delta_s):
        return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- holding the lock -------------------------------------------------------------------
    def test_acquire_then_guard_allows_writes_and_working_tree_untouched(self):
        self.as_node("pc")
        r = poa.lease_acquire(purpose="test")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["token"], 1)
        lease = self.remote_lease()
        self.assertEqual(lease["holder"], "antigravity")
        self.assertEqual(lease["token"], 1)
        self.assertFalse((self.pc / "LEASE.json").exists(), "lease must be written via the remote, not the working tree")
        g = poa.lease_guard("mine")
        self.assertEqual(g["token"], 1)
        self.assertEqual(poa.lease_check("answer")["decision"], "write")
        # renew keeps the same fencing token
        r2 = poa.lease_acquire(purpose="renew")
        self.assertEqual((r2["op"], r2["token"]), ("renew", 1))

    def test_hosted_write_runs_send_only_with_lease(self):
        self.as_node("pc")
        sent = []
        with mock.patch.object(poa, "wait_for_slot", return_value=(True, 5)), \
             mock.patch.object(poa, "adopt_next_nonce", return_value={"next_nonce": 6}):
            res = poa.hosted_write(lambda: sent.append(1) or {"ok": True}, action="mine")
        self.assertEqual(sent, [1])
        self.assertTrue(res["ok"])
        self.assertEqual(self.remote_lease()["holder"], "antigravity")

    # --- contention -------------------------------------------------------------------------
    def test_contention_second_node_refused_while_lease_live(self):
        self.as_node("pc")
        self.assertTrue(poa.lease_acquire()["ok"])
        self.as_node("box")
        r = poa.lease_acquire()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "held")
        self.assertIn("antigravity", r["reason"])
        sent = []
        with self.assertRaises(poa.PoaHTTPError) as cm:
            poa.hosted_write(lambda: sent.append(1) or {"ok": True}, action="mine")
        self.assertEqual(cm.exception.kind, "busy")
        self.assertIn("single-writer lease", str(cm.exception))
        self.assertEqual(sent, [], "a refused write must never call send()")
        self.assertEqual(poa.lease_check("mine")["decision"], "refuse")

    def test_race_lost_push_rereads_and_refuses(self):
        """Both nodes read a free lease; the PC's push lands first, so the box's CAS push is
        rejected, it re-reads, sees the live PC lease and refuses."""
        self.as_node("box")
        real_push = poa._lease_cas_push
        state = {"done": False}

        def pc_wins_first(repo, tip, new, msg):
            if not state["done"]:
                state["done"] = True
                saved = (poa.POA_DIR, os.environ["POA_NODE"], os.environ["POA_HANDOFF_DIR"])
                self.as_node("pc")
                self.assertTrue(poa.lease_acquire()["ok"])
                poa.POA_DIR, os.environ["POA_NODE"], os.environ["POA_HANDOFF_DIR"] = saved
            return real_push(repo, tip, new, msg)

        with mock.patch.object(poa, "_lease_cas_push", side_effect=pc_wins_first):
            r = poa.lease_acquire()
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["state"], "held")
        self.assertEqual(self.remote_lease()["holder"], "antigravity")

    def test_unrelated_log_push_does_not_break_acquire(self):
        self.as_node("box")
        (self.pc / "LOG.md").write_text("# log\nline\n")
        _git(self.pc, "commit", "-qam", "log line")
        _git(self.pc, "push", "-q", "origin", "HEAD:main")
        r = poa.lease_acquire()
        self.assertTrue(r["ok"], r)
        self.assertIn("line", _git(self.origin, "show", "main:LOG.md"), "lease commit must sit on top of other pushes")

    # --- expiry and takeover ----------------------------------------------------------------
    def test_clean_expiry_allows_takeover_with_higher_token(self):
        self.write_remote_lease({"version": 1, "holder": "antigravity", "token": 7, "acquired_at": self.iso(-1200),
                                 "expires_at": self.iso(-300), "ttl_s": 900, "pending_jobs": []})
        self.as_node("box")
        self.assertEqual(poa.lease_check("mine")["decision"], "acquire-then-write")
        g = poa.lease_guard("mine")
        self.assertEqual(g["token"], 8)
        lease = self.remote_lease()
        self.assertEqual((lease["holder"], lease["token"], lease.get("took_over_from")), ("grokbot", 8, "antigravity"))

    def test_expiry_within_skew_still_counts_as_held(self):
        self.write_remote_lease({"version": 1, "holder": "antigravity", "token": 2, "acquired_at": self.iso(-900),
                                 "expires_at": self.iso(-10), "ttl_s": 900})
        self.as_node("box")
        self.assertEqual(poa.lease_acquire()["state"], "held")

    def test_release_frees_lease_and_keeps_token_for_next_holder(self):
        self.as_node("pc")
        poa.lease_acquire()
        rel = poa.lease_release()
        self.assertTrue(rel["ok"], rel)
        self.assertIsNone(self.remote_lease()["holder"])
        self.as_node("box")
        self.assertEqual(poa.lease_acquire()["token"], 2)

    def test_release_refused_while_202_job_pending(self):
        self.as_node("pc")
        poa.lease_acquire()
        poa.save_busy_jobs([{"job_id": "abcdef123456", "state": "queued", "route": "/api/hosted/answer"}])
        r = poa.lease_release()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "pending-jobs")

    # --- stale / ambiguous lease ------------------------------------------------------------
    def test_expired_lease_with_pending_job_is_stale_and_refused(self):
        self.write_remote_lease({"version": 1, "holder": "antigravity", "token": 3, "acquired_at": self.iso(-3000),
                                 "expires_at": self.iso(-2000), "ttl_s": 900, "pending_jobs": ["abc123…"]})
        self.as_node("box")
        r = poa.lease_acquire()
        self.assertEqual(r["state"], "stale")
        self.assertIn("202", r["reason"])
        with self.assertRaises(poa.PoaHTTPError) as cm:
            poa.lease_guard("answer")
        self.assertIn("stale", str(cm.exception))
        # an explicit, reasoned force is the only way past it
        self.assertFalse(poa.lease_acquire(force=True)["ok"])
        f = poa.lease_acquire(force=True, reason="PC confirmed down, job 404")
        self.assertTrue(f["ok"], f)
        self.assertEqual(f["token"], 4)

    def test_ambiguous_leases_are_refused(self):
        self.as_node("box")
        cases = [
            "{not json",
            {"holder": "antigravity", "acquired_at": self.iso(-10), "expires_at": self.iso(600)},  # no token
            {"holder": "antigravity", "token": 1, "acquired_at": self.iso(3600), "expires_at": self.iso(4000)},  # future
            {"holder": "antigravity", "token": 1, "acquired_at": self.iso(-10), "expires_at": self.iso(86400)},  # absurd TTL
            {"holder": "antigravity", "token": 1, "expires_at": self.iso(600)},  # missing acquired_at
        ]
        for c in cases:
            self.write_remote_lease(c)
            r = poa.lease_acquire()
            self.assertFalse(r["ok"], c)
            self.assertEqual(r["state"], "ambiguous", (c, r))
            self.assertEqual(poa.lease_check("mine")["decision"], "refuse")

    def test_unreachable_remote_refuses(self):
        self.as_node("box")
        _git(self.box, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        r = poa.lease_acquire()
        self.assertEqual(r["state"], "unsynced")
        with self.assertRaises(poa.PoaHTTPError) as cm:
            poa.lease_guard("mine")
        self.assertEqual(cm.exception.kind, "busy")

    # --- fencing token mismatch -------------------------------------------------------------
    def test_fencing_superseded_token_refuses_write(self):
        self.as_node("pc")
        self.assertEqual(poa.lease_acquire()["token"], 1)
        # the PC's lease lapsed mid-batch and the box took over: remote now says grokbot, token 2
        self.write_remote_lease({"version": 1, "holder": "grokbot", "token": 2, "acquired_at": self.iso(-5),
                                 "expires_at": self.iso(895), "ttl_s": 900, "took_over_from": "antigravity"})
        self.as_node("pc")
        self.assertEqual(poa.lease_acquire(force=True, reason="cannot force a live lease")["state"], "held")
        chk = poa.lease_check("grade")
        self.assertEqual((chk["decision"], chk["state"]), ("refuse", "fenced"))
        sent = []
        with self.assertRaises(poa.PoaHTTPError) as cm:
            poa.hosted_write(lambda: sent.append(1) or {"ok": True}, action="grade")
        self.assertIn("fencing", str(cm.exception))
        self.assertEqual(sent, [])

    def test_fencing_same_name_other_machine_mismatch(self):
        self.as_node("pc")
        poa.lease_acquire()
        poa._lease_local_save({"holder": "antigravity", "token": 99})  # this machine thinks it holds 99
        chk = poa.lease_check("mine")
        self.assertEqual(chk["state"], "fenced")
        r = poa.lease_acquire()
        self.assertEqual(r["state"], "fenced")
        with self.assertRaises(poa.PoaHTTPError):
            poa.lease_guard("mine")
        self.assertFalse(poa.lease_release()["ok"])

    def test_queued_write_ties_lease_to_job(self):
        self.as_node("pc")
        err = poa.PoaHTTPError("queued", "accepted by busy node")
        err.job_id = "job987654321"

        def send():
            raise err

        with mock.patch.object(poa, "wait_for_slot", return_value=(True, 5)):
            with self.assertRaises(poa.PoaHTTPError):
                poa.hosted_write(send, action="answer")
        self.assertEqual(self.remote_lease()["pending_jobs"], ["job987…"])


class DisabledByDefaultTests(unittest.TestCase):
    def test_guard_is_noop_when_not_enabled(self):
        with mock.patch.dict(os.environ, {"POA_SINGLE_WRITER": "0"}), \
             mock.patch.object(poa, "lease_read_remote", side_effect=AssertionError("must not touch git")):
            self.assertEqual(poa.lease_guard("mine"), {})
            with mock.patch.object(poa, "wait_for_slot", return_value=(True, 1)), \
                 mock.patch.object(poa, "adopt_next_nonce", return_value={"next_nonce": 2}):
                self.assertTrue(poa.hosted_write(lambda: {"ok": True})["ok"])


if __name__ == "__main__":
    unittest.main()
