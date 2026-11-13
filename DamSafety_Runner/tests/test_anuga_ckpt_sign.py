"""Runner 3.19.3 (security audit pass 3): ANUGA checkpoints are signed, and only a checkpoint
this user's own runs signed is loaded.

A checkpoint is a pickle of the domain, and loading a pickle runs the code in it. The worker
writes <checkpoint>.sig (HMAC-SHA256 under a key in the user's settings folder) when it saves
one, and checks it before loading. No signature, another key, or a changed file: the checkpoint
is not loaded, the run stops and the checkpoints are kept (never a silent fresh start, which
would delete them). NDBREACH_ANUGA_RESUME_TRUST=1 loads the user's own unsigned checkpoint.
The proof that nothing is unpickled: a planted pickle that would write a marker file."""
import json
import os
import pickle
import shutil
import stat
import tempfile

from nd_breach_runner.core import anuga_worker as AW
from nd_breach_runner.core import ckpt_sign as CS
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_anuga_checkpoint import _Env, _fake_run, _grids, _same


def _file(d, name="anuga_breach_600.0.pickle", data=b"domain bytes"):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


@test
def ckpt_sign_a_signed_file_verifies_and_any_change_or_other_key_does_not():
    d = tempfile.mkdtemp()
    try:
        key = os.path.join(d, "keys", "k.key")
        env = {CS.KEY_FILE_ENV: key}
        p = _file(d)
        assert CS.verify(p, env) == (False, CS.verify(p, env)[1])
        assert "no signature" in CS.verify(p, env)[1]
        assert not os.path.exists(key)                 # checking never makes a key
        assert CS.sign(p, env) == ""
        sig = json.load(open(CS.sig_path(p)))
        assert sig["alg"] == "HMAC-SHA256" and len(sig["mac"]) == 64 and sig["size"] == 12
        assert CS.verify(p, env) == (True, "")
        # the key: 32 random bytes as hex, never the same twice, for this user only
        raw = open(key).read().strip()
        assert len(bytes.fromhex(raw)) == 32
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(os.path.dirname(key)).st_mode) == 0o700
        # one byte changed, same size
        with open(p, "r+b") as f:
            f.write(b"D")
        ok, why = CS.verify(p, env)
        assert not ok and "changed since it was signed" in why, why
        # a longer file
        _file(d, data=b"domain bytes and more")
        ok, why = CS.verify(p, env)
        assert not ok and why == "the file has changed since it was signed", why
        # signed with another user's key
        _file(d)
        other = {CS.KEY_FILE_ENV: os.path.join(d, "other.key")}
        assert CS.sign(p, other) == "" and CS.verify(p, other) == (True, "")
        ok, why = CS.verify(p, env)
        assert not ok and "another user or computer" in why, why
        assert open(key).read().strip() != open(other[CS.KEY_FILE_ENV]).read().strip()
        # a user with no key at all, a signature that is not ours, one that is not JSON
        ok, why = CS.verify(p, {CS.KEY_FILE_ENV: os.path.join(d, "none.key")})
        assert not ok and "has no checkpoint key" in why, why
        with open(CS.sig_path(p), "w") as f:
            json.dump({"alg": "none", "mac": "00"}, f)
        assert "not one the Runner writes" in CS.verify(p, env)[1]
        with open(CS.sig_path(p), "w") as f:
            f.write("not json")
        assert "cannot be read" in CS.verify(p, env)[1]
        CS.remove_sig(p)
        CS.remove_sig(p)                               # twice is fine
        assert not os.path.exists(CS.sig_path(p))
    finally:
        shutil.rmtree(d)


@test
def ckpt_sign_a_damaged_key_file_is_never_replaced_and_signs_nothing():
    d = tempfile.mkdtemp()
    try:
        key = os.path.join(d, "k.key")
        with open(key, "w") as f:
            f.write("abcd")                            # too short to be a key
        env = {CS.KEY_FILE_ENV: key}
        p = _file(d)
        err = CS.sign(p, env)
        assert "no signing key" in err and not os.path.exists(CS.sig_path(p)), err
        assert open(key).read() == "abcd"
        assert CS.load_key(True, {CS.KEY_FILE_ENV: os.path.join(d, "no", "such", "k")})
    finally:
        shutil.rmtree(d)


@test
def ckpt_sign_settings_folder_is_the_users_own_on_each_system():
    home = os.path.expanduser("~")
    assert CS.settings_dir({"APPDATA": r"C:\Users\n\AppData\Roaming"}, "win32") == \
        os.path.join(r"C:\Users\n\AppData\Roaming", "DamSafety")
    assert CS.settings_dir({}, "darwin") == os.path.join(
        home, "Library", "Application Support", "DamSafety")
    assert CS.settings_dir({"XDG_CONFIG_HOME": "/x"}, "linux") == os.path.join("/x", "DamSafety")
    assert CS.settings_dir({}, "linux") == os.path.join(home, ".config", "DamSafety")
    assert CS.key_path({CS.KEY_FILE_ENV: "/k"}) == "/k"
    assert not CS.trusted({}) and CS.trusted({CS.TRUST_ENV: "1"}) and \
        not CS.trusted({CS.TRUST_ENV: "0"})


@test
def ckpt_sign_worker_signs_what_it_saves_and_prunes_the_signatures():
    d = tempfile.mkdtemp()
    try:
        _fake_run(d, stop_at=1000.0, ckpt_every_steps=4)
        ckroot = os.path.join(d, "anuga_checkpoints")
        ckdir = os.path.join(ckroot, os.listdir(ckroot)[0])
        meta = AW.read_meta(ckdir)
        assert meta["times"], meta
        for t in meta["times"]:
            assert CS.verify(AW.checkpoint_path(ckdir, t)) == (True, ""), t
        names = sorted(os.listdir(ckdir))
        pickles = [n for n in names if n.endswith(".pickle")]
        # every checkpoint kept has its signature, and no signature is left of a pruned one
        assert sorted(n[:-4] for n in names if n.endswith(".sig")) == pickles, names
        kept = AW.prune_checkpoints(ckdir, meta["times"], 1)
        left = sorted(os.listdir(ckdir))
        assert [n for n in left if n.endswith(".sig")] == \
            [os.path.basename(AW.checkpoint_path(ckdir, kept[-1])) + ".sig"], left
    finally:
        shutil.rmtree(d)


class _Planted:
    """Unpickling this would create the marker file."""

    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (open, (self.marker, "w"))


@test
def ckpt_sign_worker_never_unpickles_an_unsigned_or_foreign_checkpoint():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        g0, _ = _grids(_fake_run(d0))
        _fake_run(d1, stop_at=1000.0, ckpt_every_steps=4)
        ckroot = os.path.join(d1, "anuga_checkpoints")
        ckdir = os.path.join(ckroot, os.listdir(ckroot)[0])
        times = AW.read_meta(ckdir)["times"]
        before = sorted(os.listdir(ckdir))
        # 1. a checkpoint folder from somewhere else: the pickles replaced by a planted one,
        #    with signatures made under another key
        marker = os.path.join(d1, "PWNED")
        other = {CS.KEY_FILE_ENV: os.path.join(d1, "attacker.key")}
        good = {}
        for t in times:
            p = AW.checkpoint_path(ckdir, t)
            good[t] = open(p, "rb").read()
            with open(p, "wb") as f:
                pickle.dump(_Planted(marker), f)
            assert CS.sign(p, other) == ""
        try:
            _fake_run(d1, ckpt_every_steps=4, resume=1)
            raise AssertionError("a foreign checkpoint was resumed")
        except AW.ResumeError as e:
            msg = str(e)
        assert "was not loaded" in msg and CS.TRUST_ENV + "=1" in msg, msg
        assert "Nothing was run" in msg and "were kept" in msg, msg
        assert not os.path.exists(marker), "the planted pickle was loaded"
        assert sorted(os.listdir(ckdir)) == before          # nothing deleted, no fresh start
        assert not os.path.exists(os.path.join(d1, "anuga_result.npz"))
        # 2. the same with no signature at all (a checkpoint from before 3.19.3)
        for t in times:
            CS.remove_sig(AW.checkpoint_path(ckdir, t))
        try:
            _fake_run(d1, ckpt_every_steps=4, resume=1)
            raise AssertionError("an unsigned checkpoint was resumed")
        except AW.ResumeError as e:
            assert "has no signature" in str(e), str(e)
        assert not os.path.exists(marker)
        # 3. the user's own checkpoints back, still unsigned: refused, then loaded on their say
        for t in times:
            with open(AW.checkpoint_path(ckdir, t), "wb") as f:
                f.write(good[t])
        try:
            _fake_run(d1, ckpt_every_steps=4, resume=1)
            raise AssertionError("an unsigned checkpoint was resumed")
        except AW.ResumeError:
            pass
        with _Env(**{CS.TRUST_ENV: "1"}):
            g1, s1 = _grids(_fake_run(d1, ckpt_every_steps=4, resume=1))
        _same(g0, g1)
        assert s1["checkpoint"]["resumed_from_t_s"] == max(times)
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def ckpt_sign_the_signature_is_the_only_gate_before_the_pickle_is_loaded():
    """The control works both ways: the planted pickle runs when the user says to trust it,
    and only then. (So the test above is not passing because the loader ignores the file.)"""
    import types
    from nd_breach_runner.tests import fake_anuga as FA
    d = tempfile.mkdtemp()
    try:
        marker = os.path.join(d, "RAN")
        p = AW.checkpoint_path(d, 600.0)
        with open(p, "wb") as f:
            pickle.dump(_Planted(marker), f)
        anuga = types.SimpleNamespace(load_checkpoint_file=FA.load_checkpoint_file)
        meta = {"times": [600.0]}
        refused = []
        with _Env(**{CS.TRUST_ENV: None}):
            dom, why = AW.load_latest_checkpoint(anuga, d, meta, refused)
        assert dom is None and refused == [p] and "was not loaded" in why, why
        assert not os.path.exists(marker)
        import contextlib
        import io
        out = io.StringIO()
        with _Env(**{CS.TRUST_ENV: "1"}), contextlib.redirect_stdout(out):
            dom, t = AW.load_latest_checkpoint(anuga, d, meta, [])
        if dom is not None:
            dom.close()
        assert os.path.exists(marker) and t == 600.0
        assert "does not verify" in out.getvalue() and CS.TRUST_ENV + "=1" in out.getvalue()
    finally:
        shutil.rmtree(d)


@test
def ckpt_sign_resume_from_a_named_folder_is_checked_too():
    from nd_breach_runner.tests.test_anuga_checkpoint import _stopped
    d1 = tempfile.mkdtemp()
    try:
        old, _ref = _stopped(d1)
        for t in AW.read_meta(old)["times"]:
            CS.remove_sig(AW.checkpoint_path(old, t))
        before = sorted(os.listdir(old))
        try:
            _fake_run(d1, ckpt_every_steps=4, manning_n=0.06, resume_from=old)
            raise AssertionError("an unsigned checkpoint was resumed by name")
        except AW.ResumeError as e:
            assert "has no signature" in str(e) and "nothing was run" in str(e), str(e)
        assert sorted(os.listdir(old)) == before
    finally:
        shutil.rmtree(d1)
