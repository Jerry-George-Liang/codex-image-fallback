"""Offline tests; fake credentials and mocked network only."""

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location("provider_images", Path(__file__).with_name("provider_images.py"))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")
MODELS = {"data": [{"id": "chat-model"}, {"id": "gpt-image-example"}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config_text = '''model_provider = "custom"
[model_providers.custom]
base_url = "https://provider.example/coding/" # preserve this prefix
env_key = "SKILL_TEST_KEY"
experimental_bearer_token = "fake-config-key"
'''
        (self.home / "config.toml").write_text(self.config_text)
        self.env = patch.dict(os.environ, {"SKILL_TEST_KEY": "fake-env-key"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self, auth=None):
        if auth is not None:
            (self.home / "auth.json").write_text(json.dumps(auth))
        return api.load_config(self.home)

    def args(self):
        prompt = self.home / "prompt.txt"
        prompt.write_text("A cat in sunlight")
        return argparse.Namespace(expected_base="https://provider.example/coding",
                                  out=self.home / "result.png", model="gpt-image-example",
                                  prompt_file=prompt, size=None)

    def run_generate(self, config, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return api.generate(config, args)

    def http_error(self, code):
        return urllib.error.HTTPError("https://provider.example/coding/models", code,
                                      "fake error", {}, io.BytesIO(b"private server text"))

    def test_auth_precedence_real_toml_and_prefix(self):
        config = self.config({"OPENAI_API_KEY": "fake-auth-key"})
        self.assertEqual(config["base_url"], "https://provider.example/coding")
        self.assertEqual([v for _, v in config["credentials"]],
                         ["fake-auth-key", "fake-env-key", "fake-config-key"])

    def test_oauth_is_not_used_as_api_key(self):
        config = self.config({"tokens": {"access_token": "fake-oauth"}})
        self.assertNotIn("fake-oauth", [v for _, v in config["credentials"]])
        self.assertIn("OAuth", " ".join(config["notes"]))

    def test_duplicate_credentials_not_retried(self):
        config = self.config({"OPENAI_API_KEY": "fake-env-key"})
        self.assertEqual(len(config["credentials"]), 2)

    def test_named_profile_selects_provider(self):
        with (self.home / "config.toml").open("a") as f:
            f.write('''
[profiles.other]
model_provider = "second"
[model_providers.second]
base_url = "https://second.example/v1"
experimental_bearer_token = "fake-second-key"
''')
        config = api.load_config(self.home, "other")
        self.assertEqual(config["base_url"], "https://second.example/v1")

    def test_malformed_file_does_not_echo_secret(self):
        (self.home / "config.toml").write_text('experimental_bearer_token = "private-unterminated')
        with self.assertRaises(api.Failure) as ctx:
            self.config()
        self.assertNotIn("private-unterminated", str(ctx.exception))

    def test_no_credentials_fails_without_network(self):
        (self.home / "config.toml").write_text('model_provider="x"\n[model_providers.x]\nbase_url="https://example.com"')
        with patch.object(api, "request_json") as request:
            with self.assertRaises(api.Failure):
                self.config({"tokens": {"access_token": "fake-oauth"}})
            request.assert_not_called()

    def test_discovery_auth_failure_falls_back_no_secret_in_report(self):
        config = self.config({"OPENAI_API_KEY": "fake-auth-key"})
        with patch.object(api, "request_json", side_effect=[self.http_error(401), MODELS]) as request:
            report, key = api.discover(config)
        self.assertEqual(key, "fake-env-key")
        self.assertEqual(report["image_candidates"], ["gpt-image-example"])
        self.assertFalse(report["generation_verified"])
        self.assertEqual(request.call_args_list[0].args[0], "https://provider.example/coding/models")
        self.assertIn("401", str(report["auth_notes"]))
        for _, value in config["credentials"]:
            self.assertNotIn(value, json.dumps(report))

    def test_404_does_not_rotate_credentials_or_guess_urls(self):
        with patch.object(api, "request_json", side_effect=self.http_error(404)) as request:
            with self.assertRaises(urllib.error.HTTPError):
                api.discover(self.config())
            self.assertEqual(request.call_count, 1)

    def test_all_auth_failures_distinguished_from_no_models(self):
        config = self.config()
        with patch.object(api, "request_json", side_effect=[self.http_error(401), self.http_error(403)]):
            with self.assertRaisesRegex(api.Failure, "全部配置认证"):
                api.discover(config)

    def test_empty_directory_and_output_capability(self):
        with patch.object(api, "request_json", return_value={"data": []}):
            report, _ = api.discover(self.config())
            self.assertEqual(report["image_candidates"], [])
        self.assertTrue(api.image_candidate({"id": "unknown", "output_modalities": ["image"]}))
        self.assertFalse(api.image_candidate({"id": "vision-chat", "input_modalities": ["image"]}))
        with self.assertRaises(api.Failure):
            api.model_items({"error": "not a directory"})

    def test_generation_one_post_base64_saved_privately(self):
        config, args = self.config(), self.args()
        response = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        with patch.object(api, "request_json", side_effect=[MODELS, response]) as request:
            report = self.run_generate(config, args)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], "https://provider.example/coding/images/generations")
        self.assertEqual(request.call_args.args[2], {"model": args.model, "prompt": "A cat in sunlight", "n": 1})
        self.assertEqual(Path(report["output"]).read_bytes(), PNG)
        self.assertEqual(args.out.stat().st_mode & 0o777, 0o600)
        self.assertFalse(args.out.with_suffix(".response.json").exists())

    def test_url_failure_retains_response_fetch_does_not_regenerate(self):
        config, args = self.config(), self.args()
        url = "https://cdn.example/result.png?signature=private"
        with patch.object(api, "request_json", side_effect=[MODELS, {"data": [{"url": url}]}]) as request:
            with patch.object(api, "download_image", side_effect=OSError("network unavailable")):
                with self.assertRaises(OSError):
                    self.run_generate(config, args)
        self.assertEqual(request.call_count, 2)
        recovery = args.out.with_suffix(".response.json")
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        with patch.object(api, "download_image", return_value=PNG), patch.object(api, "request_json") as request:
            api.fetch_result(recovery, args.out)
            request.assert_not_called()
        self.assertFalse(recovery.exists())

    def test_timeout_and_post_auth_failure_never_retry(self):
        for error in (TimeoutError(), self.http_error(401)):
            with self.subTest(error=type(error).__name__):
                config, args = self.config(), self.args()
                args.out = self.home / (type(error).__name__ + ".png")
                with patch.object(api, "request_json", side_effect=[MODELS, error]) as request:
                    with self.assertRaises(type(error)):
                        self.run_generate(config, args)
                    self.assertEqual(request.call_count, 2)

    def test_changed_config_or_existing_output_prevents_post(self):
        config, args = self.config(), self.args()
        args.expected_base = "https://old.example"
        with patch.object(api, "request_json") as request:
            with self.assertRaises(api.Failure):
                self.run_generate(config, args)
            request.assert_not_called()
            args.expected_base = config["base_url"]
            args.out.write_bytes(b"existing")
            with self.assertRaises(api.Failure):
                self.run_generate(config, args)
            request.assert_not_called()
        self.assertEqual(args.out.read_bytes(), b"existing")

    def test_unknown_selection_prevents_post(self):
        config, args = self.config(), self.args()
        args.model = "absent-model"
        with patch.object(api, "request_json", return_value=MODELS) as request:
            with self.assertRaises(api.Failure):
                self.run_generate(config, args)
            self.assertEqual(request.call_count, 1)

    def test_chat_only_selection_prevents_post(self):
        config, args = self.config(), self.args()
        args.model = "chat-model"
        with patch.object(api, "request_json", return_value=MODELS) as request:
            with self.assertRaises(api.Failure):
                self.run_generate(config, args)
            self.assertEqual(request.call_count, 1)

    def test_cli_generate_defaults_to_gpt_image_2(self):
        args = self.args()
        argv = ["provider_images.py", "--codex-home", str(self.home), "generate",
                "--prompt-file", str(args.prompt_file), "--out", str(args.out),
                "--expected-base", args.expected_base]
        models = {"data": [{"id": "gpt-image-2"}]}
        response = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        with patch.object(sys, "argv", argv), patch.object(api, "request_json", side_effect=[models, response]) as request:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(api.main(), 0)
        self.assertEqual(request.call_args.args[2]["model"], "gpt-image-2")
        self.assertNotIn("quality", request.call_args.args[2])
        self.assertNotIn("complexity", request.call_args.args[2])
        self.assertEqual(args.out.read_bytes(), PNG)

    def test_single_generate_command_needs_no_separate_discovery(self):
        args = self.args()
        argv = ["provider_images.py", "--codex-home", str(self.home), "generate",
                "--prompt-file", str(args.prompt_file), "--out", str(args.out)]
        models = {"data": [{"id": "gpt-image-2"}]}
        response = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        with patch.object(sys, "argv", argv), patch.object(api, "load_config", wraps=api.load_config) as load:
            with patch.object(api, "request_json", side_effect=[models, response]) as request:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(api.main(), 0)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[0], "https://provider.example/coding/models")
        self.assertEqual(request.call_args_list[1].args[0], "https://provider.example/coding/images/generations")
        self.assertEqual(request.call_args_list[1].args[2]["model"], "gpt-image-2")
        self.assertNotIn("image_candidates", output.getvalue())
        self.assertNotIn("models", output.getvalue())

    def test_interrupted_generation_keeps_readable_pending_record(self):
        for error in (TimeoutError(), api.Failure("invalid JSON"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                config, args = self.config(), self.args()
                args.out = self.home / (type(error).__name__ + ".png")
                with patch.object(api, "request_json", side_effect=[MODELS, error]) as request:
                    with self.assertRaises(type(error)):
                        self.run_generate(config, args)
                self.assertEqual(request.call_count, 2)
                recovery = args.out.with_suffix(".response.json")
                record = json.loads(recovery.read_text())
                self.assertEqual(record["kind"], "generation_pending")
                self.assertEqual(record["model"], args.model)
                self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
                for _, secret in config["credentials"]:
                    self.assertNotIn(secret, recovery.read_text())

    def test_fetch_pending_record_needs_no_config_or_network(self):
        recovery = self.home / "pending.response.json"
        recovery.write_text(json.dumps({"kind": "generation_pending", "model": "gpt-image-2"}))
        argv = ["provider_images.py", "--codex-home", str(self.home / "nonexistent"), "fetch",
                "--result-file", str(recovery), "--out", str(self.home / "result.png")]
        with patch.object(sys, "argv", argv), patch.object(api, "load_config") as load:
            with patch.object(api, "request_json") as request:
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(api.main(), 1)
        self.assertIn("结果尚未确认", error.getvalue())
        load.assert_not_called()
        request.assert_not_called()
        self.assertTrue(recovery.exists())

    def test_removed_quality_cli_rejected_before_network(self):
        args = self.args()
        argv = ["provider_images.py", "--codex-home", str(self.home), "generate",
                "--prompt-file", str(args.prompt_file), "--out", str(args.out),
                "--expected-base", args.expected_base, "--quality", "auto"]
        with patch.object(sys, "argv", argv), patch.object(api, "request_json") as request:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                api.main()
        self.assertEqual(ctx.exception.code, 2)
        request.assert_not_called()

    def test_http_error_is_saved_redacted_with_request_id(self):
        config, args = self.config({"OPENAI_API_KEY": "fake-auth-key"}), self.args()
        body = {"error": {"code": "invalid_value", "type": "image_generation_user_error",
                          "message": "Unsupported medium; fake-auth-key fake-env-key fake-config-key https://cdn.example/x?signature=private",
                          "param": "size"}, "private_extra": "not for output"}
        error = urllib.error.HTTPError("https://provider.example/images/generations", 400, "bad",
                                       {"x-request-id": "request-example-123"},
                                       io.BytesIO(json.dumps(body).encode()))
        with patch.object(api, "request_json", side_effect=[MODELS, error]) as request:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(urllib.error.HTTPError):
                    api.generate(config, args)
        recovery = args.out.with_suffix(".response.json")
        data = json.loads(recovery.read_text())
        self.assertEqual(data["http_status"], 400)
        self.assertEqual(data["request_id"], "request-example-123")
        self.assertEqual(data["error"]["param"], "size")
        self.assertNotIn("quality", data)
        self.assertEqual(data["kind"], "generation_http_error")
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        self.assertEqual(request.call_count, 2)
        combined = recovery.read_text() + output.getvalue()
        for secret in ("fake-auth-key", "fake-env-key", "fake-config-key", "signature=private", "not for output"):
            self.assertNotIn(secret, combined)
        self.assertIn("Unsupported medium", combined)

    def test_non_json_http_error_does_not_echo_body(self):
        error = urllib.error.HTTPError("https://provider.example", 502, "bad", {},
                                       io.BytesIO(b"<html>private-server-data</html>"))
        report = api.http_error_report(error)
        self.assertEqual(report["http_status"], 502)
        self.assertNotIn("private-server-data", json.dumps(report))

    def test_fetch_diagnostic_refuses_to_regenerate(self):
        recovery = self.home / "failed.response.json"
        recovery.write_text(json.dumps({"kind": "generation_http_error", "http_status": 400}))
        with patch.object(api, "request_json") as request:
            with self.assertRaisesRegex(api.Failure, "诊断记录"):
                api.fetch_result(recovery, self.home / "image.png")
            request.assert_not_called()
        self.assertTrue(recovery.exists())

    def test_cli_discover_hides_model_list_by_default(self):
        argv = ["provider_images.py", "--codex-home", str(self.home), "discover"]
        models = {"data": [{"id": "gpt-image-2"}, {"id": "gpt-image-2.6"}]}
        with patch.object(sys, "argv", argv), patch.object(api, "request_json", return_value=models):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(api.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["model"], "gpt-image-2")
        self.assertTrue(report["model_available"])
        self.assertNotIn("models", report)
        self.assertNotIn("image_candidates", report)
        self.assertNotIn("gpt-image-2.6", output.getvalue())

    def test_cli_discover_explicit_list_keeps_candidates(self):
        argv = ["provider_images.py", "--codex-home", str(self.home), "discover", "--list-models"]
        with patch.object(sys, "argv", argv), patch.object(api, "request_json", return_value=MODELS):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(api.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["image_candidates"], ["gpt-image-example"])

    def test_missing_default_does_not_switch_or_post(self):
        args = self.args()
        argv = ["provider_images.py", "--codex-home", str(self.home), "generate",
                "--prompt-file", str(args.prompt_file), "--out", str(args.out),
                "--expected-base", args.expected_base]
        with patch.object(sys, "argv", argv), patch.object(api, "request_json", return_value=MODELS) as request:
            with contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(api.main(), 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn("gpt-image-2", error.getvalue())
        self.assertNotIn("gpt-image-example", error.getvalue() + output.getvalue())
        self.assertFalse(args.out.exists())

    def test_request_does_not_follow_redirects(self):
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))
        with patch.object(api.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = self.http_error(302)
            with self.assertRaises(urllib.error.HTTPError):
                api.request_json("https://provider.example/models", "fake-request-key")
            self.assertEqual(build.return_value.open.call_count, 1)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer fake-request-key")
            self.assertEqual(build.call_args.args, (api.NoRedirect,))

    def test_download_no_auth_and_private_url_rejected(self):
        address = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch.object(api.socket, "getaddrinfo", return_value=address):
            with patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = PNG
                self.assertEqual(api.download_image("https://cdn.example/x?signature=secret"), PNG)
                self.assertEqual(build.return_value.open.call_args.args, ("https://cdn.example/x?signature=secret",))
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(api.socket, "getaddrinfo", return_value=private):
            with patch.object(api.urllib.request, "build_opener") as build:
                with self.assertRaises(api.Failure):
                    api.download_image("https://localhost/private")
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
