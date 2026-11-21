"""scripts/setup_tts.py: her voice, her Live2D model and the paper doll from Hugging Face, against a local stand-in for
the Hub. Offline.

The stand-in serves a tiny synthetic copy of the repository layout (never her real files): public, or private (401
without the right bearer token, as the Hub answers anonymous requests for a private repository). Large files are
answered with a redirect to a second server on another port, like the Hub's CDN, which must never see the token;
the clips are answered with a same-host redirect, which must keep it. Like the real repository, the pinned revision
(VOICE_REV) has no live2d/Host/: the paper doll is served only at its own revision (PAPER_DOLL_REV).
"""
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("setup_tts", ROOT / "scripts" / "setup_tts.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

TOKEN = "fake-hub-pass"                     # a dummy string the fake Hub accepts; not a Hugging Face token
REPO_FILES = {
    "character.json": json.dumps({"character_id": "MekaHime_Amika"}).encode(),
    "MekaHime_Amika.ckpt": b"gpt-weights" * 900,
    "MekaHime_Amika.pth": b"sovits-weights" * 700,
    "references/emotions.json": json.dumps({"happy1": {"audio": "开心.wav", "text": "x"},
                                            "sad1": {"audio": "难过.wav", "text": "y"}}, ensure_ascii=False).encode(),
    "references/开心.wav": b"RIFF-happy" * 50,
    "references/难过.wav": b"RIFF-sad" * 60,
    "README.md": b"# model card\n",
    "LICENSE": b"personal use only\n",
    ".gitattributes": b"*.ckpt filter=lfs\n",
    "live2d/Amika/AmikaV4.model3.json": json.dumps({"FileReferences": {
        "Moc": "AmikaV4.moc3", "Textures": ["AmikaV4.8192/texture_00.png"],
        "Motions": {"": [{"File": "idle1.motion3.json"}]}}}).encode(),
    "live2d/Amika/AmikaV4.moc3": b"moc" * 3000,
    "live2d/Amika/AmikaV4.8192/texture_00.png": b"png" * 2500,
    "live2d/Amika/idle1.motion3.json": b'{"Version": 3}',
    "live2d/Amika/banks.json": b'{"banks": {}}',
    # the paper doll: its own revision, its banks.json says "role": "agent", its model3 names no motions
    "live2d/Host/host.model3.json": json.dumps({"FileReferences": {
        "Moc": "host.moc3", "Textures": ["host.2048/texture_00.png"]}}).encode(),
    "live2d/Host/host.moc3": b"doll-moc" * 400,
    "live2d/Host/host.2048/texture_00.png": b"doll-png" * 300,
    "live2d/Host/idle1.motion3.json": b'{"Version": 0}',
    "live2d/Host/think1.motion3.json": b'{"Version": 0, "think": 1}',
    "live2d/Host/banks.json": json.dumps({"config": {"display_name": "Host (agent avatar)", "role": "agent"},
                                          "banks": {}}).encode(),
}
CDN_SUFFIXES = (".ckpt", ".pth", ".moc3", ".png")           # served from the "CDN" (another host)
DOLL = "live2d/Host/"
DOLL_REV = "d011" * 10                                      # stands in for the revision that adds live2d/Host/


class Hub:
    def __init__(self):
        self.private = False
        self.gone = set()                     # paths answered with 404 EntryNotFound
        self.tampered = {}                    # path -> other bytes of the same length (not the pinned file)
        self.requests = 0
        self.saw_auth = 0                     # requests to the Hub itself that carried a token
        self.cdn_saw_auth = []
        self.same_host_redirect_auth = []
        self.ranges = []
        self.revisions = {}                   # path -> the revision it was last asked for at

    def denied(self, handler):
        return self.private and handler.headers.get("Authorization") != f"Bearer {TOKEN}"


def make_servers(hub):
    class CDN(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization"):
                hub.cdn_saw_auth.append(self.path)
            rel = urllib.parse.unquote(self.path.split("/blob/", 1)[1])
            data = REPO_FILES[rel]
            start = 0
            if self.headers.get("Range"):
                start = int(self.headers["Range"].split("=")[1].split("-")[0])
                hub.ranges.append((rel, start))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

        def log_message(self, *args):
            pass

    cdn = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CDN)
    cdn_base = f"http://127.0.0.1:{cdn.server_address[1]}"

    class HF(http.server.BaseHTTPRequestHandler):
        def empty(self, code, **headers):
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            hub.requests += 1
            hub.saw_auth += bool(self.headers.get("Authorization"))
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if path.startswith("/api/resolve-cache/"):              # the same-host second leg of a redirect
                hub.same_host_redirect_auth.append(bool(self.headers.get("Authorization")))
                if hub.denied(self):
                    return self.empty(401, **{"X-Error-Code": "RepoNotFound"})
                rel = path.split("/", 4)[4]                       # /api/resolve-cache/models/<path>

                return self.send_file(REPO_FILES[rel])
            prefix = f"/{st.VOICE_REPO}/resolve/"
            rev, _, rel = path[len(prefix):].partition("/") if path.startswith(prefix) else ("", "", "")
            if not rev or rev not in (st.VOICE_REV, DOLL_REV):
                return self.empty(404, **{"X-Error-Code": "RevisionNotFound"})
            if hub.denied(self):
                return self.empty(401, **{"X-Error-Code": "RepoNotFound"})
            hub.revisions[rel] = rev
            if rel not in REPO_FILES or rel in hub.gone or (rel.startswith(DOLL) and rev != DOLL_REV):
                return self.empty(404, **{"X-Error-Code": "EntryNotFound"})
            if rel.endswith(CDN_SUFFIXES):
                return self.empty(302, Location=f"{cdn_base}/blob/{urllib.parse.quote(rel)}")
            if rel.startswith("references/"):
                return self.empty(307, Location=f"/api/resolve-cache/models/{urllib.parse.quote(rel)}")
            self.send_file(hub.tampered.get(rel, REPO_FILES[rel]))

        def send_file(self, data):
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    hf = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HF)
    for server in (cdn, hf):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return hf, cdn, f"http://127.0.0.1:{hf.server_address[1]}"


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    hub = Hub()
    hf, cdn, base = make_servers(hub)
    monkeypatch.setattr(st, "HF", base)
    monkeypatch.setattr(st, "TOKEN_HOSTS", ("huggingface.co", "127.0.0.1"))   # the stand-in plays huggingface.co
    monkeypatch.setattr(st, "VOICE_ADAPTER", tmp_path / "no-adapter.py")      # the real adapter wants all 67 tags
    monkeypatch.setattr(st, "PAPER_DOLL_REV", DOLL_REV)                       # as after the doll's upload
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")                   # never read this machine's saved login
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))                         # no console: never asks
    repo = tmp_path / "repo"
    for rel, data in REPO_FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    manifest = tmp_path / "manifest.json"
    pins = {rel: [len(data), hashlib.sha256(data).hexdigest()] for rel, data in REPO_FILES.items()}
    manifest.write_text(json.dumps({"repo": st.VOICE_REPO, "revision": st.VOICE_REV,
                                    "files": {r: e for r, e in pins.items() if not r.startswith(DOLL)},
                                    "paper_doll": {"files": {r: e for r, e in pins.items() if r.startswith(DOLL)}}}),
                        "utf-8")
    hub.repo, hub.manifest = repo, manifest
    hub.engine, hub.avatar, hub.doll = tmp_path / "engine", tmp_path / "avatar" / "Amika", tmp_path / "avatar" / "Host"
    yield hub
    for server in (hf, cdn):
        server.shutdown()
        server.server_close()


def run(hub, *extra):
    argv = ["--assets-only", "--engine-dir", str(hub.engine), "--avatar-dir", str(hub.avatar),
            "--paper-doll-dir", str(hub.doll), "--manifest", str(hub.manifest), *extra]
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = st.main(argv)
    rows = {name: (mark.strip(), detail) for mark, name, detail in st.ROWS}
    return rc, rows, out.getvalue()


def installed(hub):
    voice = {rel: (hub.engine / "Speakers" / "MekaHime_Amika" / rel) for rel in REPO_FILES
             if not rel.startswith("live2d/") and rel != ".gitattributes"}
    model = {rel: hub.avatar / rel[len("live2d/Amika/"):] for rel in REPO_FILES if rel.startswith("live2d/Amika/")}
    doll = {rel: hub.doll / rel[len(DOLL):] for rel in REPO_FILES if rel.startswith(DOLL)}
    return voice, model, doll


VOICE, MODEL, PAPER_DOLL = "voice MekaHime_Amika", "Live2D model Amika", "paper doll Host"


def test_public_repo_installs_her_voice_and_model_and_a_rerun_downloads_nothing(hub):
    rc, rows, out = run(hub, "--accept-voice-license")
    assert rc == 0, out
    assert rows[VOICE][0] == "OK" and "downloaded 8 file(s)" in rows[VOICE][1] and "8 files verified" in rows[VOICE][1]
    assert "2/2 emotion tags with a clip" in rows[VOICE][1]
    assert rows[MODEL][0] == "OK" and "AmikaV4.model3.json, 1 motions" in rows[MODEL][1]
    assert rows[PAPER_DOLL][0] == "OK" and f"downloaded 6 file(s) from {st.VOICE_REPO}@{DOLL_REV[:8]}" in rows[PAPER_DOLL][1]
    assert "host.model3.json, 2 motions" in rows[PAPER_DOLL][1] and "her helpers" in rows[PAPER_DOLL][1]
    voice, model, doll = installed(hub)
    for rel, path in {**voice, **model, **doll}.items():
        assert path.read_bytes() == REPO_FILES[rel], rel
    assert all(hub.revisions[rel] == DOLL_REV for rel in doll)           # the doll from its own revision,
    assert all(hub.revisions[rel] == st.VOICE_REV for rel in {**voice, **model})   # the rest from the pinned one
    assert not (hub.engine / "Speakers" / "MekaHime_Amika" / ".gitattributes").exists()
    assert not list(hub.engine.rglob("*.part")) and not list(hub.avatar.rglob("*.part")) and not list(hub.doll.rglob("*.part"))
    assert hub.cdn_saw_auth == []
    assert "Done: her voice, her Live2D model and the paper doll are in place." in out
    served = hub.requests
    rc, rows, out = run(hub)                         # installed: no question, no download, every file re-checked
    assert rc == 0, out
    assert hub.requests == served
    assert rows[VOICE][1].count("existing; 8 files verified") == 1
    assert f"existing; 6 files verified against {st.VOICE_REPO}@{DOLL_REV[:8]}" in rows[PAPER_DOLL][1]


def test_private_repo_without_a_token_says_what_to_do(hub):
    hub.private = True
    rc, rows, out = run(hub, "--accept-voice-license")
    assert rc == 1
    for name in (VOICE, MODEL, PAPER_DOLL):
        mark, detail = rows[name]
        assert mark == "!!" and "HTTP 401" in detail and "not public yet" in detail and "HF_TOKEN" in detail, detail
    assert "Traceback" not in out
    assert "Fix the !! lines above and run this script again" in out
    assert not (hub.engine / "Speakers" / "MekaHime_Amika" / "character.json").exists()
    # said the moment it happens, not only in the summary after the voice server's long install
    assert "[setup] !! her voice: Hugging Face refused the download (HTTP 401)" in out.split("OpenAmika voice setup summary")[0]


def test_a_mirror_never_gets_the_token(hub, monkeypatch):
    monkeypatch.setattr(st, "TOKEN_HOSTS", ("huggingface.co",))        # the stand-in is a mirror (HF_ENDPOINT) now
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    rc, rows, out = run(hub, "--accept-voice-license")
    assert rc == 0, out
    assert hub.saw_auth == 0 and hub.cdn_saw_auth == []
    assert "is not sent to a mirror" in out and TOKEN not in out
    assert st.token_goes_to("https://huggingface.co/x") and st.token_goes_to("https://cdn-lfs.huggingface.co/x")
    assert not st.token_goes_to("https://hf-mirror.com/x") and not st.token_goes_to("https://huggingface.co.example.org/x")


def test_private_repo_with_a_token_works_and_the_cdn_never_sees_it(hub, monkeypatch):
    hub.private = True
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    rc, rows, out = run(hub, "--accept-voice-license")
    assert rc == 0, out
    assert hub.cdn_saw_auth == []                                   # cross-host redirect: the token stays behind
    assert hub.same_host_redirect_auth and all(hub.same_host_redirect_auth)   # same host: it goes along
    assert "using your Hugging Face token (from HF_TOKEN)" in out and TOKEN not in out


def test_a_wrong_token_is_named_as_the_problem(hub, monkeypatch):
    hub.private = True
    monkeypatch.setenv("HF_TOKEN", "not-the-right-one")
    rc, rows, _ = run(hub, "--accept-voice-license")
    assert rc == 1 and "did not accept your Hugging Face token (from HF_TOKEN)" in rows[VOICE][1]


def test_without_the_license_nothing_is_downloaded(hub):
    rc, rows, out = run(hub)
    assert rc == 1 and hub.requests == 0
    for name in (VOICE, MODEL, PAPER_DOLL):
        assert rows[name][0] == "!!" and "license was not accepted" in rows[name][1] and "--accept-voice-license" in rows[name][1]
    assert "her voice, her Live2D model and the paper doll" in rows[PAPER_DOLL][1]


def test_verify_finds_missing_and_damaged_files_and_a_rerun_repairs_them(hub):
    assert run(hub, "--accept-voice-license")[0] == 0
    voice, model, doll = installed(hub)
    voice["references/开心.wav"].write_bytes(b"truncated")
    model["live2d/Amika/idle1.motion3.json"].unlink()
    doll["live2d/Host/think1.motion3.json"].unlink()
    served = hub.requests
    rc, rows, out = run(hub, "--verify")
    assert rc == 1 and hub.requests == served                       # --verify never downloads
    assert "references/开心.wav" in rows[VOICE][1] and "1 of 8 files" in rows[VOICE][1]
    assert "idle1.motion3.json" in rows[MODEL][1]
    assert rows[PAPER_DOLL][0] == "!!" and "1 of 6 files" in rows[PAPER_DOLL][1] and "think1.motion3.json" in rows[PAPER_DOLL][1]
    assert "run this script without --verify" in out
    rc, rows, out = run(hub)                                        # installed before: repaired without asking again
    assert rc == 0, out
    assert voice["references/开心.wav"].read_bytes() == REPO_FILES["references/开心.wav"]
    assert model["live2d/Amika/idle1.motion3.json"].is_file() and doll["live2d/Host/think1.motion3.json"].is_file()
    rc, rows, out = run(hub, "--verify")
    assert rc == 0 and "Verified: her voice, her Live2D model and the paper doll are complete." in out


def test_her_edited_banks_json_is_kept(hub):
    assert run(hub, "--accept-voice-license")[0] == 0
    _, model, doll = installed(hub)
    model["live2d/Amika/banks.json"].write_text('{"banks": {"casual_happy": {}}}', "utf-8")
    doll["live2d/Host/banks.json"].write_text('{"config": {"role": "agent", "nx": 0.1}, "banks": {}}', "utf-8")
    rc, rows, _ = run(hub, "--update-avatar")
    assert rc == 0 and "your banks.json kept" in rows[MODEL][1] and "your banks.json kept" in rows[PAPER_DOLL][1]
    assert "casual_happy" in model["live2d/Amika/banks.json"].read_text("utf-8")
    assert '"nx": 0.1' in doll["live2d/Host/banks.json"].read_text("utf-8")


def test_an_unedited_banks_json_follows_a_newer_pin_and_an_edited_one_stays(hub, monkeypatch):
    assert run(hub, "--accept-voice-license")[0] == 0
    banks = installed(hub)[1]["live2d/Amika/banks.json"]

    def pin(data):                     # a newer revision of her repository brings another banks.json
        monkeypatch.setitem(REPO_FILES, "live2d/Amika/banks.json", data)
        manifest = json.loads(hub.manifest.read_text("utf-8"))
        manifest["files"]["live2d/Amika/banks.json"] = [len(data), hashlib.sha256(data).hexdigest()]
        hub.manifest.write_text(json.dumps(manifest), "utf-8")

    pin(b'{"banks": {"v": 2}}')
    rc, rows, out = run(hub)
    assert rc == 0, out
    assert banks.read_bytes() == b'{"banks": {"v": 2}}' and "kept" not in rows[MODEL][1]
    banks.write_text('{"banks": {"mine": 1}}', "utf-8")                 # then edited on the Setup page
    pin(b'{"banks": {"v": 3}}')
    rc, rows, _ = run(hub)
    assert rc == 0 and "your banks.json kept" in rows[MODEL][1]
    assert banks.read_text("utf-8") == '{"banks": {"mine": 1}}'


def test_a_file_that_is_not_the_pinned_one_is_named_once_and_kept_out(hub):
    hub.tampered["README.md"] = b"# other card\n"                       # same length, other bytes
    rc, rows, _ = run(hub, "--accept-voice-license")
    detail = rows[VOICE][1]
    assert rc == 1 and rows[VOICE][0] == "!!" and "SHA-256 mismatch" in detail
    assert detail.count("not the pinned one") == 1 and "OpenAmika pins" not in detail and "git pull" in detail
    assert not (hub.engine / "Speakers" / "MekaHime_Amika" / "README.md").exists()


def test_linux_on_arm_takes_the_torch_pair_that_exists_for_it():
    """A DGX Spark (Linux, aarch64) has no torchaudio 2.9.1+cu128 wheel and its GB10 needs CUDA 13. Every other
    machine keeps the pair it had."""
    assert st.torch_build(False, "AMD64") == ("2.9.1", "cu128")         # Windows
    assert st.torch_build(True, "x86_64") == ("2.9.1", "cu128")
    assert st.torch_build(False, "arm64") == ("2.9.1", "cu128")         # a Mac: the CUDA tag is never used there
    assert st.torch_build(True, "aarch64") == ("2.10.0", "cu130")
    assert st.TORCH_INDEX["cuda"].endswith("/" + st.TORCH_CUDA) and st.TORCH_LOCAL["cuda"] == "+" + st.TORCH_CUDA
    assert st.TORCH_CUDA_NAME in ("CUDA 12.8", "CUDA 13.0")


def test_a_damaged_torch_is_installed_again_but_a_fresh_one_once(monkeypatch, tmp_path):
    """pip calls an installed torch satisfied even when its files are damaged: the same build goes in place again
    (--force-reinstall); a fresh venv gets one plain install, and a too-long folder is left to its own hint."""
    calls = []
    monkeypatch.setattr(st, "TORCH_VERSION", "2.9.1")                   # the pair below, whatever machine runs this
    monkeypatch.setattr(st, "TORCH_LOCAL", {"cuda": "+cu128", "cpu": "+cpu"})
    monkeypatch.setattr(st, "check_platform", lambda vpy: None)
    monkeypatch.setattr(st, "pip", lambda vpy, *args: calls.append(args))
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    vpy = tmp_path / ".venv" / "Scripts" / "python.exe"
    cases = {"damaged": {"installed": "2.9.1+cu128", "error": "OSError: [WinError 126] Error loading c10.dll"},
             "fresh": {"error": "ModuleNotFoundError: No module named 'torch'"},
             "too long": {"installed": "2.9.1+cu128",
                          "error": "ImportError: DLL load failed while importing _C: The filename or extension is too long."}}
    for name, probe in cases.items():
        calls.clear()
        monkeypatch.setattr(st.subprocess, "run", lambda *a, probe=probe, **k: type("P", (), {
            "stdout": json.dumps(probe), "returncode": 0})())
        st.ensure_torch(vpy, "cuda", False)
        forced = [c for c in calls if "--force-reinstall" in c]
        assert len(calls) == (2 if name == "damaged" else 1), (name, calls)
        assert bool(forced) == (name == "damaged"), name
        assert all("torch==2.9.1+cu128" in c for c in calls)


def test_a_too_long_folder_is_named_with_its_only_fix():
    err = "resampy: ImportError: DLL load failed while importing _box: The filename or extension is too long."
    hint = st.too_long_hint(err)
    assert "C:\\OpenAmika" in hint and "does not help" in hint
    assert st.too_long_hint("resampy: ImportError: No module named 'resampy'") == ""
    if st.IS_WIN:
        assert "DLL" in st.long_folder_warning(Path("C:/" + "x" * 120))
        assert st.long_folder_warning(Path("C:/OpenAmika")) == ""


def test_an_interrupted_download_resumes_from_its_part_file(hub):
    target = hub.engine / "Speakers" / "MekaHime_Amika" / "MekaHime_Amika.ckpt"
    target.parent.mkdir(parents=True)
    half = len(REPO_FILES["MekaHime_Amika.ckpt"]) // 2
    target.with_name(target.name + ".part").write_bytes(REPO_FILES["MekaHime_Amika.ckpt"][:half])
    rc, _, out = run(hub, "--accept-voice-license")
    assert rc == 0, out
    assert ("MekaHime_Amika.ckpt", half) in hub.ranges
    assert target.read_bytes() == REPO_FILES["MekaHime_Amika.ckpt"]


def test_a_file_gone_from_the_revision_says_to_update(hub):
    hub.gone.add("references/难过.wav")
    hub.gone.add("live2d/Host/idle1.motion3.json")
    rc, rows, _ = run(hub, "--accept-voice-license")
    assert rc == 1 and "HTTP 404" in rows[VOICE][1] and "git pull" in rows[VOICE][1]
    assert rows[MODEL][0] == "OK"                                    # the model does not need the voice
    assert rows[PAPER_DOLL][0] == "!!" and f"at revision {DOLL_REV[:8]}" in rows[PAPER_DOLL][1]   # its own revision


def test_from_dir_installs_offline_and_checks_every_file(hub):
    hub.private = True                                               # the Hub would refuse: it must not be asked
    rc, rows, out = run(hub, "--accept-voice-license", "--from-dir", str(hub.repo))
    assert rc == 0, out
    assert hub.requests == 0 and "copied 8 file(s)" in rows[VOICE][1] and "copied 6 file(s)" in rows[PAPER_DOLL][1]
    voice, model, doll = installed(hub)
    assert all(p.read_bytes() == REPO_FILES[r] for r, p in {**voice, **model, **doll}.items())
    (hub.repo / "references" / "开心.wav").write_bytes(b"RIFF-other" * 50)   # same size, other bytes
    voice["references/开心.wav"].unlink()
    rc, rows, _ = run(hub, "--from-dir", str(hub.repo))
    assert rc == 1 and "SHA-256" in rows[VOICE][1]
    assert not voice["references/开心.wav"].exists()


def test_write_manifest_reproduces_the_pinned_list(hub, tmp_path):
    out = tmp_path / "written.json"
    with contextlib.redirect_stdout(io.StringIO()):
        assert st.main(["--write-manifest", str(hub.repo), "--voice-revision", st.VOICE_REV + "x", "--manifest", str(out)]) == 0
    written = json.loads(out.read_text("utf-8"))
    pinned = json.loads(hub.manifest.read_text("utf-8"))
    assert written["files"] == pinned["files"] and written["revision"] == st.VOICE_REV + "x"
    assert written["paper_doll"]["files"] == pinned["paper_doll"]["files"]          # live2d/Host/ in its own block
    assert not any(rel.startswith(DOLL) for rel in written["files"])
    # a copy without the doll keeps the pins that are there (the doll stays at its own revision)
    for rel in [r for r in REPO_FILES if r.startswith(DOLL)]:
        (hub.repo / rel).unlink()
    with contextlib.redirect_stdout(io.StringIO()) as said:
        assert st.main(["--write-manifest", str(hub.repo), "--voice-revision", st.VOICE_REV + "y", "--manifest", str(out)]) == 0
    again = json.loads(out.read_text("utf-8"))
    assert again["paper_doll"]["files"] == pinned["paper_doll"]["files"] and "pins were kept" in said.getvalue()


def test_hf_token_sources_and_their_order(monkeypatch, tmp_path):
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    saved = tmp_path / "token"
    saved.write_text("saved-login\n", "utf-8")
    monkeypatch.setenv("HF_TOKEN_PATH", str(saved))
    assert st.hf_token(None) == ("saved-login", "`hf auth login`")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "older-variable")
    assert st.hf_token(None) == ("older-variable", "HUGGING_FACE_HUB_TOKEN")
    monkeypatch.setenv("HF_TOKEN", "from-env")
    assert st.hf_token(None) == ("from-env", "HF_TOKEN")
    assert st.hf_token("flag") == ("flag", "--hf-token")
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(var)
    monkeypatch.setenv("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    assert st.hf_token(None) == (None, "")


def test_the_shipped_manifest_pins_her_whole_repository():
    m = st.load_manifest(st.MANIFEST)
    assert (m["repo"], m["revision"]) == (st.VOICE_REPO, st.VOICE_REV)
    voice, model = st.split_assets(m["files"])
    assert len(m["files"]) == 250 and len(voice) == 75 and len(model) == 174
    assert {"character.json", "MekaHime_Amika.ckpt", "MekaHime_Amika.pth", "references/emotions.json"} <= set(voice)
    assert sum(r.endswith(".wav") for r in voice) == 69
    assert list(voice)[-1] == "character.json" and list(model)[-1] == "AmikaV4.model3.json"   # written last
    assert sum(r.endswith(".motion3.json") for r in model) == 142 and sum(r.endswith(".exp3.json") for r in model) == 25
    assert all(len(sha) == 64 and size > 0 for size, sha in m["files"].values())


def test_the_shipped_manifest_pins_the_paper_doll():
    """live2d/Host/: 70 files, 2,349,288 bytes, 64 motions in seven families, its banks.json before its model3 (the
    folder never counts as a model of hers), at a revision that is either the placeholder or a full commit."""
    m = st.load_manifest(st.MANIFEST)
    doll = st.model_files(m["paper_doll"], st.PAPER_DOLL_PATH)
    assert len(doll) == 70 and sum(size for size, _ in doll.values()) == 2_349_288
    assert {"host.model3.json", "host.moc3", "host.physics3.json", "host.cdi3.json", "host.2048/texture_00.png",
            "banks.json"} <= set(doll)
    families = {"idle": 15, "talk": 14, "listen": 7, "think": 6, "happy": 9, "worried": 6, "surprise": 7}
    for family, count in families.items():
        assert sum(r.startswith(family) and r.endswith(".motion3.json") for r in doll) == count, family
    assert sum(r.endswith(".motion3.json") for r in doll) == 64
    assert list(doll)[-1] == "host.model3.json"
    assert all(len(sha) == 64 and size > 0 for size, sha in doll.values())
    assert not any(r.startswith(st.PAPER_DOLL_PATH + "/") for r in m["files"])       # not at the voice's revision
    assert st.PAPER_DOLL_REV == "" or (len(st.PAPER_DOLL_REV) == 40 and int(st.PAPER_DOLL_REV, 16) >= 0)


def test_the_paper_doll_waits_for_its_revision_and_is_never_fetched_before(hub, monkeypatch):
    """PAPER_DOLL_REV empty (before the doll's upload): her voice and model install as always, the doll is not
    requested at all, and its row says why, without failing the run."""
    monkeypatch.setattr(st, "PAPER_DOLL_REV", "")
    rc, rows, out = run(hub, "--accept-voice-license")
    assert rc == 0, out
    assert rows[VOICE][0] == "OK" and rows[MODEL][0] == "OK"
    assert rows[PAPER_DOLL][0] == "--" and "not downloaded" in rows[PAPER_DOLL][1] and "PAPER_DOLL_REV" in rows[PAPER_DOLL][1]
    assert not any(rel.startswith(DOLL) for rel in hub.revisions) and not hub.doll.exists()
    assert "Done: her voice and her Live2D model are in place." in out
    assert run(hub, "--verify")[0] == 0                              # nothing to verify yet: not a failure either


def test_a_doll_in_place_is_checked_against_its_pins_before_its_revision_is_pinned(hub, monkeypatch):
    assert run(hub, "--accept-voice-license")[0] == 0
    monkeypatch.setattr(st, "PAPER_DOLL_REV", "")
    served = hub.requests
    rc, rows, out = run(hub)
    assert rc == 0 and rows[PAPER_DOLL][0] == "OK" and "existing; 6 files verified against the pinned list" in rows[PAPER_DOLL][1]
    doll = installed(hub)[2]
    doll["live2d/Host/host.moc3"].write_bytes(b"x" * len(REPO_FILES["live2d/Host/host.moc3"]))
    rc, rows, _ = run(hub)
    assert rc == 0 and rows[PAPER_DOLL][0] == "--" and "host.moc3" in rows[PAPER_DOLL][1]
    assert "cannot be repaired from Hugging Face" in rows[PAPER_DOLL][1]
    assert hub.requests == served                                    # checked, never fetched


def test_from_dir_installs_the_paper_doll_before_its_revision_is_pinned(hub, monkeypatch):
    monkeypatch.setattr(st, "PAPER_DOLL_REV", "")
    rc, rows, out = run(hub, "--accept-voice-license", "--from-dir", str(hub.repo))
    assert rc == 0, out
    assert rows[PAPER_DOLL][0] == "OK" and "copied 6 file(s)" in rows[PAPER_DOLL][1] and hub.requests == 0
    assert all(p.read_bytes() == REPO_FILES[r] for r, p in installed(hub)[2].items())


@pytest.mark.parametrize("doll_rev", ["", DOLL_REV])
def test_from_dir_of_a_copy_without_the_doll_still_installs_her_voice_and_model(hub, monkeypatch, tmp_path, doll_rev):
    """A local copy of her repository at the pinned revision has no live2d/Host/ (the doll came later, at its own
    revision): an offline install from it is not a failure. The doll's row says why; a doll already in place is only
    checked; the license question never counts a doll that cannot come."""
    monkeypatch.setattr(st, "PAPER_DOLL_REV", doll_rev)
    old = tmp_path / "copy-without-doll"
    for rel, data in REPO_FILES.items():
        if not rel.startswith(DOLL):
            (old / rel).parent.mkdir(parents=True, exist_ok=True)
            (old / rel).write_bytes(data)
    rc, rows, out = run(hub, "--accept-voice-license", "--from-dir", str(old))
    assert rc == 0, out
    assert rows[VOICE][0] == "OK" and rows[MODEL][0] == "OK" and hub.requests == 0
    assert rows[PAPER_DOLL][0] == "--" and "not copied" in rows[PAPER_DOLL][1] and "has no live2d/Host/" in rows[PAPER_DOLL][1]
    assert ("without --from-dir" if doll_rev else "PAPER_DOLL_REV") in rows[PAPER_DOLL][1]
    assert not hub.doll.exists() and "Done: her voice and her Live2D model are in place." in out
    asked = []                                                       # nothing left that could come: never asked
    monkeypatch.setattr(st, "ask_voice_license", lambda: asked.append(1) or False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert run(hub, "--from-dir", str(old))[0] == 0 and asked == []
    # a doll in place (from a copy that has it) is checked against its pins, never touched
    assert run(hub, "--accept-voice-license", "--from-dir", str(hub.repo))[0] == 0
    rc, rows, _ = run(hub, "--from-dir", str(old))
    assert rc == 0 and rows[PAPER_DOLL][0] == "OK" and "existing; 6 files verified" in rows[PAPER_DOLL][1]
    installed(hub)[2]["live2d/Host/host.moc3"].write_bytes(b"x" * len(REPO_FILES["live2d/Host/host.moc3"]))
    rc, rows, _ = run(hub, "--from-dir", str(old))
    assert rc == 0 and rows[PAPER_DOLL][0] == "--" and "host.moc3" in rows[PAPER_DOLL][1]
    assert "cannot be repaired from that folder" in rows[PAPER_DOLL][1] and hub.requests == 0


def test_update_avatar_before_the_doll_revision_is_pinned_keeps_an_intact_doll(hub, monkeypatch):
    """--update-avatar while PAPER_DOLL_REV is empty: her model is fetched again, the doll cannot be, and its intact
    files are not reported as missing (they were checked as they are)."""
    monkeypatch.setattr(st, "PAPER_DOLL_REV", "")
    assert run(hub, "--accept-voice-license", "--from-dir", str(hub.repo))[0] == 0
    rc, rows, out = run(hub, "--update-avatar")
    assert rc == 0, out
    assert rows[MODEL][0] == "OK" and "downloaded" in rows[MODEL][1]
    assert rows[PAPER_DOLL][0] == "OK" and "existing; 6 files verified against the pinned list" in rows[PAPER_DOLL][1]
    assert "not downloaded again (--update-avatar)" in rows[PAPER_DOLL][1]
    assert not any(rel.startswith(DOLL) for rel in hub.revisions)


def test_skip_paper_doll_leaves_it_out(hub):
    rc, rows, out = run(hub, "--accept-voice-license", "--skip-paper-doll")
    assert rc == 0, out
    assert rows[PAPER_DOLL][0] == "--" and "--skip-paper-doll" in rows[PAPER_DOLL][1]
    assert not any(rel.startswith(DOLL) for rel in hub.revisions) and not hub.doll.exists()


def test_a_doll_without_its_agent_role_is_named(hub):
    assert run(hub, "--accept-voice-license")[0] == 0
    banks = installed(hub)[2]["live2d/Host/banks.json"]
    banks.write_text('{"config": {"display_name": "mine"}, "banks": {}}', "utf-8")     # edited: kept, but no role
    rc, rows, _ = run(hub)
    assert rc == 1 and rows[PAPER_DOLL][0] == "!!" and '"role": "agent"' in rows[PAPER_DOLL][1]
    banks.unlink()                                                   # its fix: delete it, run again
    rc, rows, _ = run(hub)
    assert rc == 0 and rows[PAPER_DOLL][0] == "OK" and banks.read_bytes() == REPO_FILES["live2d/Host/banks.json"]


def test_the_license_question_counts_the_doll_only_when_it_can_come(hub, monkeypatch):
    """An install that already has her voice and model is asked again for the doll (the license covers it too), but
    only once the doll can be downloaded; never while PAPER_DOLL_REV is empty."""
    assert run(hub, "--accept-voice-license", "--skip-paper-doll")[0] == 0
    asked = []
    monkeypatch.setattr(st, "ask_voice_license", lambda: asked.append(1) or False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(st, "PAPER_DOLL_REV", "")
    rc, rows, _ = run(hub)
    assert asked == [] and rc == 0 and rows[PAPER_DOLL][0] == "--"
    monkeypatch.setattr(st, "PAPER_DOLL_REV", DOLL_REV)
    rc, rows, _ = run(hub)
    assert asked == [1] and rc == 1 and "license was not accepted" in rows[PAPER_DOLL][1]   # declined: not downloaded
    assert rows[VOICE][0] == "OK" and not hub.doll.exists()
