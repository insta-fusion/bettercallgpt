"""The bettercallgpt launcher: config/state locations and the no-session preflight.

Offline and silent: no stream, socket or session is opened; sounddevice is faked."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bettercallgpt import cli
from voice import config as voice_config


class LocationTests(unittest.TestCase):
    def test_installed_copy_gets_user_config_and_state(self):
        env = {"HOME": "/h", "XDG_CONFIG_HOME": "/cfg", "XDG_STATE_HOME": "/st"}
        with mock.patch.object(cli, "bundle_env", return_value=Path("/nope/.env")):
            applied = cli.apply_defaults(env, "darwin")
        self.assertEqual(env[cli.ENV_FILE_NAME], str(Path("/cfg") / "bettercallgpt" / ".env"))
        self.assertEqual(env[cli.STATE_DIR_NAME], str(Path("/st") / "bettercallgpt"))
        self.assertEqual(set(applied), {cli.ENV_FILE_NAME, cli.STATE_DIR_NAME})

    def test_operator_settings_win(self):
        env = {cli.ENV_FILE_NAME: "/mine/.env", cli.STATE_DIR_NAME: "/mine/state"}
        with mock.patch.object(cli, "bundle_env", return_value=Path("/nope/.env")):
            self.assertEqual(cli.apply_defaults(env, "darwin"), {})
        self.assertEqual(env[cli.ENV_FILE_NAME], "/mine/.env")

    def test_source_checkout_keeps_its_own_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_text("X=1\n")
            env = {}
            with mock.patch.object(cli, "bundle_env", return_value=Path(d) / ".env"):
                cli.apply_defaults(env, "darwin")
        self.assertNotIn(cli.ENV_FILE_NAME, env)       # the daemon's own lookup applies
        self.assertIn(cli.STATE_DIR_NAME, env)

    def test_windows_locations(self):
        env = {"APPDATA": r"C:\U\AppData\Roaming", "LOCALAPPDATA": r"C:\U\AppData\Local"}
        with mock.patch.object(cli, "bundle_env", return_value=Path("/nope/.env")):
            cli.apply_defaults(env, "win32")
        self.assertTrue(env[cli.ENV_FILE_NAME].endswith(".env"))
        self.assertIn("Roaming", env[cli.ENV_FILE_NAME])
        self.assertIn("Local", env[cli.STATE_DIR_NAME])


class DoctorTests(unittest.TestCase):
    CREDS = "AZURE_OPENAI_ENDPOINT=https://x.openai.azure.com\nAZURE_OPENAI_API_KEY=sk-never-printed\n"

    def _fake_sd(self, inputs=1, outputs=1):
        devs = ([{"max_input_channels": 1, "max_output_channels": 0}] * inputs
                + [{"max_input_channels": 0, "max_output_channels": 2}] * outputs)
        return types.SimpleNamespace(query_devices=lambda: devs)

    def _doctor(self, env_text, platform="darwin", environ=None, sd=None, require=None):
        from voice.backend import registry as backends
        with tempfile.TemporaryDirectory() as d:
            envf = Path(d) / ".env"
            envf.write_text(env_text)
            clean = {k: v for k, v in os.environ.items()
                     if not k.startswith(("AZURE_OPENAI_", "OPENAI_", "VOICE_"))}
            clean.update({cli.ENV_FILE_NAME: str(envf), **(environ or {})})
            real_find = cli.importlib.util.find_spec
            with mock.patch.dict(os.environ, clean, clear=True), \
                 mock.patch.dict(sys.modules, {"sounddevice": sd or self._fake_sd()}), \
                 mock.patch.object(cli.importlib.util, "find_spec",
                                   side_effect=lambda m, *a: object()
                                   if m in ("websockets", "sounddevice") else real_find(m, *a)), \
                 mock.patch.object(backends, "require", side_effect=require or (lambda *a: None)), \
                 mock.patch("shutil.which", return_value="/usr/bin/x"):
                return cli.doctor(platform)

    def test_paths_in_the_report_are_redacted_too(self):
        r = self._doctor(self.CREDS, environ={cli.STATE_DIR_NAME: "/tmp/sk-never-printed/state"})
        self.assertNotIn("sk-never-printed", json.dumps(r))

    def test_ready_when_everything_is_present(self):
        r = self._doctor(self.CREDS)
        self.assertTrue(r["ready"], r)
        self.assertEqual(r["credentials_missing"], [])
        self.assertNotIn("sk-never-printed", json.dumps(r))

    def test_missing_credentials_are_named_not_valued(self):
        r = self._doctor("")
        self.assertFalse(r["ready"])
        self.assertEqual(r["credentials_missing"],
                         ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"])

    def test_openai_provider_needs_its_own_key(self):
        r = self._doctor("", environ={"VOICE_LIVE_PROVIDER": "openai"})
        self.assertEqual(r["credentials_missing"], ["OPENAI_API_KEY"])

    def test_the_daemon_registry_decides_platform_and_backend(self):
        from voice.backend import registry as backends

        def refuse(*a):
            raise backends.Unsupported("VOICE_BACKEND=claude_code is unsupported on linux")
        r = self._doctor(self.CREDS, platform="linux", require=refuse)
        self.assertIn("unsupported on linux", r["backend_problem"])
        self.assertFalse(r["ready"])

    def test_claude_code_no_longer_needs_orca(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(cli.backend_check("claude_code", lambda n, d="": d, "darwin"))

    def test_no_audio_device_is_not_ready(self):
        self.assertFalse(self._doctor(self.CREDS, sd=self._fake_sd(inputs=0))["ready"])

    def test_process_child_is_resolved_like_the_backend_will(self):
        # The registry's own dialect check is tested in voice/tests; here only the resolution
        # bettercallgpt adds on top of it.
        from voice.backend import registry as backends
        patcher = mock.patch.object(backends, "require", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = lambda env: (lambda n, d="": env.get(n, d))   # noqa: E731
        self.assertIn("VOICE_PROCESS_ARGV", cli.backend_check("process", cfg({}), "darwin"))
        self.assertIn("does not parse",
                      cli.backend_check("process", cfg({"VOICE_PROCESS_ARGV": "codex '"}), "darwin"))
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "agent.sh"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            if os.name != "nt":
                self.assertIsNone(cli.backend_check(
                    "process", cfg({"VOICE_PROCESS_ARGV": "./agent.sh", "VOICE_BACKEND_CWD": d}),
                    "darwin"))
                with mock.patch.dict(os.environ, {"PATH": "."}):
                    self.assertIsNone(cli.backend_check(
                        "process", cfg({"VOICE_PROCESS_ARGV": "agent.sh",
                                        "VOICE_BACKEND_CWD": d}), "darwin"))
                # PATH unset: the backend hands its child PATH="" (cwd only), so a bare
                # system command is NOT runnable there, and doctor must say so.
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIn("not found", cli.backend_check(
                        "process", cfg({"VOICE_PROCESS_ARGV": "sh", "VOICE_BACKEND_CWD": d}),
                        "darwin"))
                for argv, cwd, why in (("~/agent.sh", d, "not found"),
                                       ("./agent.sh/", d, "not found"),
                                       ("agent.sh", str(exe), "not a directory"),
                                       ("./agent.sh", d + " ", "not a directory")):
                    self.assertIn(why, cli.backend_check(
                        "process", cfg({"VOICE_PROCESS_ARGV": argv, "VOICE_BACKEND_CWD": cwd}),
                        "darwin"))

    def tearDown(self):
        voice_config.load_env_file(force=True)      # leave no test .env in the snapshot


class MainTests(unittest.TestCase):
    def test_daemon_commands_pass_through_untouched(self):
        from voice.app import daemon
        with mock.patch.object(daemon, "main", return_value=0) as dm, \
             mock.patch.object(cli, "apply_defaults"):
            self.assertEqual(cli.main(["--session", "s1", "status"]), 0)
        dm.assert_called_once_with(["--session", "s1", "status"])

    def test_doctor_prints_json_and_exits_by_readiness(self):
        out = io.StringIO()
        with mock.patch.object(cli, "doctor", return_value={"ready": False}), \
             mock.patch.object(cli, "apply_defaults"), redirect_stdout(out):
            self.assertEqual(cli.main(["doctor"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"ready": False})


class StatuslineTests(unittest.TestCase):
    def _seg(self, status, session="s1", alive=True):
        with tempfile.TemporaryDirectory() as d:
            if status is not None:
                (Path(d) / "s1").mkdir()
                (Path(d) / "s1" / "status.json").write_text(json.dumps(status))
            stdin = io.StringIO(json.dumps({"session_id": session}))
            return cli.statusline(stdin, {cli.STATE_DIR_NAME: d}, alive=lambda pid: alive)

    LIVE = {"phase": "running", "relay": "qualified", "ended": {}, "pid": 4242,
            "reconnecting": False}

    def test_live_and_renewing(self):
        self.assertEqual(self._seg(self.LIVE), "🎙 voice")
        # A provider relay keeps the phase `running`: the segment stays, marked.
        self.assertEqual(self._seg({**self.LIVE, "reconnecting": True}), "🎙 voice ↻")

    def test_there_is_no_muted_segment(self):
        self.assertEqual(self._seg({**self.LIVE, "muted": True}), "🎙 voice")

    def test_nothing_unless_the_call_is_really_live(self):
        for status in (None, {**self.LIVE, "phase": "built"}, {**self.LIVE, "relay": ""},
                       {**self.LIVE, "ended": {"reason": "stopped"}}):
            self.assertEqual(self._seg(status), "", status)
        self.assertEqual(self._seg(self.LIVE, alive=False), "")          # a crashed daemon
        self.assertEqual(self._seg(self.LIVE, session=""), "")
        self.assertEqual(self._seg(self.LIVE, session="../s1"), "")

    def test_garbage_is_silent(self):
        self.assertEqual(cli.statusline(io.StringIO("not json"), {}), "")
        self.assertEqual(cli.statusline(io.StringIO("[1]"), {}), "")
        for status in ([], None, "x", {**self.LIVE, "pid": 10 ** 30}, {**self.LIVE, "pid": True}):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "s1").mkdir()
                (Path(d) / "s1" / "status.json").write_text(json.dumps(status))
                self.assertEqual(cli.statusline(io.StringIO('{"session_id": "s1"}'),
                                                {cli.STATE_DIR_NAME: d}), "", status)

    def test_run_by_hand_in_a_terminal_it_does_not_wait(self):
        tty = mock.Mock(isatty=lambda: True, read=mock.Mock(side_effect=AssertionError))
        self.assertEqual(cli.statusline(tty, {}), "")

    def test_a_session_id_is_a_name_never_a_path(self):
        for bad in ("../s1", "..", "C:/x", "C:x", "a\\b", "a/b", ".hidden", "x" * 200):
            self.assertEqual(self._seg(self.LIVE, session=bad), "", bad)

    def test_main_prints_the_segment_and_always_succeeds(self):
        out = io.StringIO()
        with mock.patch.object(cli, "apply_defaults"), \
             mock.patch.object(sys, "stdin", io.StringIO("{}")), redirect_stdout(out):
            self.assertEqual(cli.main(["statusline"]), 0)
        self.assertEqual(out.getvalue(), "")


class PluginTests(unittest.TestCase):
    """The Claude Code plugin: three small command files with the safety properties README states."""
    ROOT = Path(__file__).resolve().parents[1]

    def test_manifests_parse_and_versions_agree(self):
        import tomllib
        version = tomllib.loads((self.ROOT / "pyproject.toml").read_text())["project"]["version"]
        market = json.loads((self.ROOT / ".claude-plugin" / "marketplace.json").read_text())
        plugin = json.loads((self.ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(plugin["version"], version)
        self.assertEqual(market["plugins"][0]["version"], version)
        self.assertEqual(market["plugins"][0]["source"], "./plugin")

    # Every command runs the release this plugin belongs to through uvx: nothing to install.
    VV = "uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.2.0 bettercallgpt"
    # On, off, status — as in Codex. No mute: the daemon has none.
    COMMANDS = {"on": None, "off": f"{VV} stop", "status": f"{VV} status"}

    def _command(self, name):
        import re
        text = (self.ROOT / "plugin" / "commands" / f"{name}.md").read_text()
        _, front, body = text.split("---", 2)
        allowed = [line for line in front.splitlines() if line.startswith("allowed-tools:")][0]
        rules = {r.strip() for r in allowed.split(":", 1)[1].split("),")}
        rules = {r if r.endswith(")") else r + ")" for r in rules}
        # Claude Code runs !`cmd` only at a line start or after whitespace; `x!`…`` stays text.
        marks = re.findall(r"(?:^|(?<=\s))!`([^`]+)`", body, re.M)
        self.assertEqual(re.findall(r"\S!`", body), [], name)
        return front, body, rules, marks

    def test_three_commands_only_the_user_runs_them(self):
        plugin = self.ROOT / "plugin"
        self.assertEqual(sorted(p.stem for p in (plugin / "commands").iterdir()),
                         sorted(self.COMMANDS))
        for name in self.COMMANDS:
            self.assertIn("disable-model-invocation: true", self._command(name)[0], name)

    # Claude Code lays editor types beside a plugin it loads from disk (git-ignored).
    GENERATED = ("tsconfig.json", ".claude-plugin/types/")

    def test_the_plugin_is_exactly_these_files_and_manifest_keys(self):
        plugin = self.ROOT / "plugin"
        files = sorted(rel for rel in (p.relative_to(plugin).as_posix() for p in plugin.rglob("*")
                                       if p.is_file() and p.name != ".DS_Store")
                       if not rel.startswith(self.GENERATED))
        self.assertEqual(files, [".claude-plugin/icon.png", ".claude-plugin/plugin.json", "README.md"]
                         + [f"commands/{n}.md" for n in sorted(self.COMMANDS)]
                         + ["hooks/hooks.json", "hooks/register.tsx", "tests/register.test.ts",
                            "types/index.d.ts"])
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
        # No inline hooks, MCP servers, agents or anything else that runs on its own; `types`
        # is the hooks module's state contract (a .d.ts: declarations, no code).
        self.assertLessEqual({"name", "version"}, set(manifest))
        self.assertLessEqual(set(manifest), {"name", "displayName", "version", "description",
                                             "author", "homepage", "repository", "license", "keywords",
                                             "documentationUrl", "supportUrl", "privacyPolicyUrl",
                                             "types"})
        self.assertEqual(manifest["types"], "./types/index.d.ts")

    def test_one_hooks_module_the_call_console(self):
        """The plugin's promise, held on the source: one module. It never decides a permission
        request (it observes one, to announce it). It runs only the voice launcher (start, stop,
        steer) and a PATH lookup, reads status.json, writes permission.json, and the one thing
        it does to Claude's work is end the running turn on the operator's Steer. No network,
        tool, model, prompt or message calls."""
        import re
        hooks = self.ROOT / "plugin" / "hooks"
        self.assertEqual(json.loads((hooks / "hooks.json").read_text()),
                         {"modules": ["./register.tsx"]})
        source = (hooks / "register.tsx").read_text()
        self.assertEqual(set(re.findall(r"\bon\('([^']+)'", source)),
                         {"classic.PermissionRequest", "session.start", "session.end",
                          "command.run", "turn.start", "turn.complete", "prompt.submit",
                          "ui.render"})
        for word in ("decision", "behavior", "updatedInput", "updatedPermissions", "deny",
                     "block", "import("):
            self.assertNotIn(word, source, word)
        calls = set(re.findall(r"\$\.([a-z]+\.[a-zA-Z]+)\(", source))
        self.assertLessEqual(calls, {"env.get", "fs.exists", "fs.read", "fs.write", "clock.now",
                                     "clock.every", "clock.after", "session.id", "ui.resolve",
                                     "command.register", "process.spawn", "turn.abort"})
        # $.process.run is written across lines (`$.process\n.run([`): count it by its argv.
        self.assertEqual(source.count("$.process.spawn("), 1)
        self.assertEqual(source.count(".run(["), 3)
        self.assertEqual(sorted(re.findall(r"'--session', sessionId, (?:'--nonce', nonce, '--mod', )?'(\w+)'\]", source)),
                         ["start", "steer", "stop"])
        self.assertIn(".run(['/bin/sh', '-c', 'command -v bettercallgpt'])", source)
        self.assertEqual(source.count("$.turn.abort("), 1)
        # The permission hook hands back exactly what the chain beneath answered.
        hook = source[source.index("on('classic.PermissionRequest'"):source.index("on('session.start'")]
        self.assertEqual(hook.count("return next(e)"), 1)
        self.assertEqual(re.findall(r"\$\.fs\.write\(`\$\{live\.dir\}/([^`]+)`", source),
                         ["permission.json"])
        self.assertEqual(source.count("$.fs.write("), 1)
        self.assertEqual(re.findall(r"\$\.fs\.read\(([^)]*)\)", source), ["statusPath"])
        self.assertIn("const statusPath = `${dir}/status.json`", source)
        # prompt.submit is only watched: the text and origin go on unchanged.
        submit = source[source.index("on('prompt.submit'"):source.index("on('ui.render'")]
        self.assertIn("return next(e)", submit)
        self.assertNotIn("next({", source)

    def test_controls_report_only_what_status_confirms(self):
        for name in ("off",):
            body = self._command(name)[1]
            self.assertIn(f"Run `{self.VV} status` once", body, name)
            self.assertIn("not confirmed", body, name)

    def test_the_start_is_never_pre_approved_and_only_on_can_start(self):
        # Exact rules only: a wider one (`Bash`, `Bash(bettercallgpt:*)`) would pre-approve the
        # start, which opens the microphone and a paid connection.
        _, body, rules, marks = self._command("on")
        self.assertEqual(rules, {"Bash(openssl rand -hex 6)", f"Bash({self.VV} status)"})
        self.assertEqual(marks, ["openssl rand -hex 6"])
        # NONCE= stays the FIRST token: the Orca pane proof reads it at the start of the line.
        self.assertIn(f"NONCE=<token> {self.VV} --nonce <token> start", body)
        for name, cmd in self.COMMANDS.items():
            if cmd is None:
                continue
            _, body, rules, marks = self._command(name)
            self.assertEqual(rules, {f"Bash({cmd})"} if name == "status"
                             else {f"Bash({cmd})", f"Bash({self.VV} status)"}, name)
            # `status` exits 1 when no call ran; a failing !`cmd` aborts the command's load,
            # so it is run as a tool call instead. The rest always exit 0 and run at load.
            self.assertEqual(marks, [] if name == "status" else [cmd], name)
            self.assertNotIn("start", body, name)
            self.assertNotIn("NONCE", body, name)


class ReleasePinTests(unittest.TestCase):
    """The plugin, the skill and the README pin one release, and it is this package's version."""
    ROOT = Path(__file__).resolve().parents[1]

    def test_every_pin_is_this_version(self):
        import re
        import tomllib
        version = tomllib.loads((self.ROOT / "pyproject.toml").read_text())["project"]["version"]
        files = [*sorted((self.ROOT / "plugin" / "commands").glob("*.md")),
                 self.ROOT / "skills" / "bettercallgpt" / "SKILL.md", self.ROOT / "README.md",
                 self.ROOT / "docs" / "GUIDE.md"]
        for f in files:
            pins = set(re.findall(r"insta-fusion/bettercallgpt@v([0-9][^ \s`\"]*)", f.read_text()))
            self.assertEqual(pins, {version}, f.name)


class SkillTests(unittest.TestCase):
    """The setup skill: installs and checks, never starts a call and never touches a key."""
    SKILL = Path(__file__).resolve().parents[1] / "skills" / "bettercallgpt" / "SKILL.md"

    def test_frontmatter_names_the_skill(self):
        _, front, body = self.SKILL.read_text().split("---", 2)
        self.assertIn("name: bettercallgpt", front)
        self.assertIn("description:", front)
        self.assertIn("Never starts a call", front)

    def test_it_cannot_start_a_call_or_handle_a_key(self):
        text = self.SKILL.read_text()
        self.assertNotIn("--nonce", text)             # a claude_code start needs one
        self.assertNotIn("NONCE=", text)
        self.assertIn("Never ask for, read out, echo or commit an API key", text)
        self.assertIn("Never start a call", text)
        self.assertIn("/bettercallgpt:on", text)          # the only way a call starts
        # No command of any backend: a `process` start needs no nonce and would open the mic.
        self.assertNotRegex(text, r"(?:bettercallgpt|VV)\b[^\n`]*\sstart\b")
        self.assertIn("Bash(uvx:*)", text)             # the broad rule it must never add


class OrcaPaneTests(unittest.TestCase):
    PANE = {cli.ORCA_HANDLE_NAME: "term_abc123"}
    START = ["--nonce", "a1b2c3d4e5f6", "start"]

    @staticmethod
    def _env(extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "VOICE_"))}
        return {**env, **extra}

    def _with(self, args, environ=None, orca="/opt/homebrew/bin/orca", proves=True):
        environ = self.PANE if environ is None else environ
        tried = []

        def prove(parsed, handle):
            tried.append((parsed.nonce, handle))
            return proves

        with mock.patch.dict(os.environ, self._env(environ), clear=True):
            out = cli.with_orca_pane(list(args), os.environ, lambda name: orca, prove)
        return out, tried

    def test_a_start_whose_pane_proof_holds_binds_that_pane(self):
        out, tried = self._with(self.START)
        self.assertEqual(out, ["--terminal", "term_abc123", *self.START])
        self.assertEqual(tried, [("a1b2c3d4e5f6", "term_abc123")])

    def test_a_pane_that_does_not_prove_leaves_the_screenless_start(self):
        # Stale handle, tmux, a headless claude: the start must work exactly as before.
        self.assertEqual(self._with(self.START, proves=False)[0], self.START)

    def test_no_proof_is_tried_when_it_cannot_or_should_not_bind(self):
        cases = {
            "not in Orca": dict(environ={}),
            "opted out": dict(environ={**self.PANE, cli.ORCA_OPT_OUT_NAME: "0"}),
            "opted out, spelled out": dict(environ={**self.PANE, cli.ORCA_OPT_OUT_NAME: "Off"}),
            "no orca CLI": dict(orca=None),
            "another backend": dict(environ={**self.PANE, "VOICE_BACKEND": "process"}),
        }
        for name, kw in cases.items():
            self.assertEqual(self._with(self.START, **kw), (self.START, []), name)
        flagged = ["--backend", "process", *self.START]
        self.assertEqual(self._with(flagged), (flagged, []))

    def test_an_explicit_terminal_and_other_commands_pass_untouched(self):
        explicit = ["--terminal", "term_other", *self.START]
        self.assertEqual(self._with(explicit), (explicit, []))
        for args in (["status"], ["stop"], ["--session", "start", "status"]):
            self.assertEqual(self._with(args), (args, []))

    def test_a_bad_command_line_is_left_for_the_daemon_to_report_once(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            for args in (["--bogus", "start"], ["--help"], []):
                self.assertEqual(self._with(args), (args, []))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_the_pre_proof_is_the_daemons_own_and_never_raises(self):
        from voice.app import daemon
        parsed = daemon.build_parser().parse_args(self.START)
        bound = {"binding": {"bound": True}}
        with mock.patch.object(daemon, "find_claude_ancestor", return_value=4242), \
             mock.patch.object(daemon, "prove_ownership",
                               new=mock.AsyncMock(return_value=bound)) as proof:
            self.assertTrue(cli.pane_proves(parsed, "term_abc123"))
        self.assertEqual(proof.call_args.kwargs["terminal"], "term_abc123")
        self.assertEqual(proof.call_args.kwargs["claude_pid"], 4242)
        with mock.patch.object(daemon, "find_claude_ancestor", return_value=4242), \
             mock.patch.object(daemon, "prove_ownership",
                               new=mock.AsyncMock(side_effect=OSError("orca gone"))):
            self.assertFalse(cli.pane_proves(parsed, "term_abc123"))
        with mock.patch.object(daemon, "find_claude_ancestor", return_value=None):
            self.assertFalse(cli.pane_proves(parsed, "term_abc123"))

    def test_the_pre_proof_runs_the_real_proof_end_to_end(self):
        # Real prove_ownership and Pane.bind over a faked `orca terminal read`: a
        # rename would fail here instead of silently turning pane mode off.
        from voice.app import daemon
        parsed = daemon.build_parser().parse_args(self.START)
        nonce = "a1b2c3d4e5f6"

        def runner(tail):
            async def run(argv, timeout):
                body = {"result": {"terminal": {"tail": tail, "handle": "term_abc123"}}}
                return {"ok": True, "stdout": json.dumps(body), "returncode": 0}
            return run

        owner = {"pid": 4242, "start": "s", "tty": "t"}
        claims = ({"pid": 4242, "sessionId": "s"}, None)
        drawn = [f"  \u23bf  $ NONCE={nonce} bettercallgpt --nonce {nonce} start"]
        for tail, expect in ((drawn, True), (["  \u23bf  $ ls"], False)):
            with mock.patch.object(daemon, "find_claude_ancestor", return_value=4242), \
                 mock.patch.object(daemon, "_pane_runner", return_value=runner(tail)), \
                 mock.patch("voice.backend.claude_code.pane.owner_fingerprint",
                            return_value=owner), \
                 mock.patch("voice.backend.claude_code.pane.registry_claims",
                            return_value=claims), \
                 mock.patch("voice.backend.claude_code.pane.session_registry",
                            return_value={}):
                self.assertIs(cli.pane_proves(parsed, "term_abc123"), expect, tail)
        self.assertEqual(nonce, parsed.nonce)

    def test_a_status_preflight_reads_no_pane(self):
        preflight = ["--status", *self.START]
        self.assertEqual(self._with(preflight), (preflight, []))

    def test_main_hands_the_daemon_the_bound_command_line(self):
        from voice.app import daemon
        with mock.patch.dict(os.environ, self._env(self.PANE), clear=True), \
             mock.patch.object(daemon, "main", return_value=0) as dm, \
             mock.patch.object(cli, "apply_defaults"), \
             mock.patch.object(cli, "pane_proves", return_value=True), \
             mock.patch("shutil.which", return_value="/opt/homebrew/bin/orca"):
            self.assertEqual(cli.main(list(self.START)), 0)
        dm.assert_called_once_with(["--terminal", "term_abc123", *self.START])

    def test_doctor_says_whether_and_why(self):
        state = cli.orca_pane_state
        found, missing = (lambda n: "/x/orca"), (lambda n: None)
        self.assertEqual(state("claude_code", self.PANE, found), "tried at start")
        self.assertEqual(state("claude_code", {}, found), "not in an Orca pane")
        self.assertEqual(state("claude_code", self.PANE, missing), "orca CLI not on PATH")
        self.assertEqual(state("claude_code", {**self.PANE, cli.ORCA_OPT_OUT_NAME: "false"},
                               found), "off (BETTERCALLGPT_ORCA_PANE)")
        self.assertEqual(state("process", self.PANE, found), "n/a")
        report = DoctorTests()._doctor(DoctorTests.CREDS, environ=self.PANE)
        self.assertEqual(report["orca_pane"], "tried at start")


if __name__ == "__main__":
    unittest.main()
