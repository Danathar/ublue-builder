import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from ublue_builder import (
    ACTION_PINS,
    App,
    BASE_IMAGES,
    COMMON_SERVICES,
    CommandError,
    CONTAINERFILE_TEMPLATE_DIR,
    Config,
    Gum,
    MANAGED_REPO_WARNING,
    PACKAGE_SEARCH_LIMIT,
    ScreenBack,
    STATE_FILE,
    VERSION,
    config_from_state_payload,
)


class BuilderTests(unittest.TestCase):
    def make_app(self) -> App:
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="ghcr.io/ublue-os/bazzite:stable",
            base_image_name="Bazzite",
            repo_name="test-image",
            image_desc="Test image",
            github_user="example",
        )
        return app

    def test_config_from_state_payload_rejects_string_list_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "packages must be a list of strings"):
            config_from_state_payload({"packages": "tmux"})

    def test_config_from_state_payload_rejects_non_string_list_item(self) -> None:
        with self.assertRaisesRegex(ValueError, "packages must contain only strings"):
            config_from_state_payload({"packages": ["tmux", 42]})

    def test_load_repo_config_rejects_repo_without_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "Containerfile").write_text("FROM ghcr.io/ublue-os/bazzite:stable\n")
            (repo_dir / "build_files").mkdir()
            (repo_dir / "build_files/build.sh").write_text("#!/bin/bash\n")
            with self.assertRaisesRegex(CommandError, "Only repos created by this tool are supported"):
                self.make_app().load_repo_config(repo_dir)

    def test_patch_container_workflow_pins_actions_and_ignores_state_file(self) -> None:
        app = self.make_app()
        app.config.signing_enabled = True
        workflow = textwrap.dedent(
            """\
            name: Build container image
            on:
              schedule:
                - cron: '00 00 * * *'
              push:
                paths-ignore:
                  - '**/README.md'
            jobs:
              build_push:
                steps:
                  - name: Checkout
                    uses: actions/checkout@v4
                  - name: Maximize build space
                    uses: ublue-os/remove-unwanted-software@v8
                  - name: Install Cosign
                    if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
                    uses: sigstore/cosign-installer@v3
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn(".ublue-builder.json", patched)
        self.assertIn(ACTION_PINS["actions/checkout"][0], patched)
        self.assertIn(ACTION_PINS["ublue-os/remove-unwanted-software"][0], patched)
        self.assertIn(ACTION_PINS["sigstore/cosign-installer"][0], patched)
        self.assertIn("env.COSIGN_PRIVATE_KEY != ''", patched)

    def test_validate_config_rejects_unsafe_package_token(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "bad;rm"]
        with self.assertRaisesRegex(CommandError, "Invalid package value"):
            app.validate_config()

    def test_base_image_picker_is_limited_to_beginner_images(self) -> None:
        self.assertEqual(
            [image.key for image in BASE_IMAGES],
            ["bazzite", "bazzite-dx", "aurora", "aurora-dx", "bluefin", "bluefin-dx"],
        )

    def test_validate_config_rejects_unsupported_base_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite-deck:stable"
        app.config.base_image_name = "Bazzite Deck"
        with self.assertRaisesRegex(CommandError, "supported base images"):
            app.validate_config()

    def test_validate_config_rejects_invalid_repo_name(self) -> None:
        app = self.make_app()
        app.config.repo_name = ".git"
        with self.assertRaisesRegex(CommandError, "Repository name is invalid"):
            app.validate_config()

    def test_ensure_signing_ready_requires_cosign(self) -> None:
        app = self.make_app()
        with patch.object(app, "repo_secret_exists", return_value=False):
            with patch("ublue_builder.command_exists", side_effect=lambda name: False if name == "cosign" else True):
                with self.assertRaisesRegex(CommandError, "brew install cosign"):
                    app.ensure_signing_ready("example", "test-image")

    def test_preflight_requires_cosign(self) -> None:
        app = self.make_app()

        class GumStub:
            def ensure_available(self) -> None:
                pass

            def header(self, *_args, **_kwargs) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def success(self, *_args, **_kwargs) -> None:
                pass

            def warn(self, *_args, **_kwargs) -> None:
                pass

            def enter_to_continue(self, *_args, **_kwargs) -> None:
                pass

        app.gum = GumStub()

        def fake_exists(name: str) -> bool:
            if name == "cosign":
                return False
            return True

        with patch("ublue_builder.command_exists", side_effect=fake_exists):
            with patch("ublue_builder.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(["gh", "auth", "status"], 0, "", "")
                with patch.object(app, "gh_json", return_value={"login": "example"}):
                    with self.assertRaisesRegex(SystemExit, "brew install cosign"):
                        app.preflight()

    def test_preflight_requires_github_cli(self) -> None:
        app = self.make_app()

        class GumStub:
            def ensure_available(self) -> None:
                pass

            def header(self, *_args, **_kwargs) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def success(self, *_args, **_kwargs) -> None:
                pass

        app.gum = GumStub()

        def fake_exists(name: str) -> bool:
            return name == "git"

        with patch("ublue_builder.command_exists", side_effect=fake_exists):
            with self.assertRaisesRegex(SystemExit, "GitHub CLI is required"):
                app.preflight()

    def test_preflight_requires_github_login(self) -> None:
        app = self.make_app()

        class GumStub:
            def ensure_available(self) -> None:
                pass

            def header(self, *_args, **_kwargs) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def success(self, *_args, **_kwargs) -> None:
                pass

        app.gum = GumStub()

        def fake_exists(name: str) -> bool:
            return name in {"git", "gh"}

        with patch("ublue_builder.command_exists", side_effect=fake_exists):
            with patch("ublue_builder.run", return_value=subprocess.CompletedProcess(["gh", "auth", "status"], 1, "", "")):
                with self.assertRaisesRegex(SystemExit, "gh auth login"):
                    app.preflight()

    def test_add_packages_to_config_accepts_valid_tokens(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", side_effect=[True, True]):
            added = app.add_packages_to_config(["tmux", "ripgrep"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux", "ripgrep"])
        self.assertTrue(any(level == "success" for level, _message in app.gum.messages))

    def test_add_packages_to_config_rejects_unsafe_tokens(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

        app.gum = GumStub()
        added = app.add_packages_to_config(["tmux", "bad;rm"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.packages, [])
        self.assertTrue(any(level == "error" and "Invalid package value" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_rejects_missing_manual_packages(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=False):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertFalse(added)
        self.assertEqual(app.config.packages, [])
        self.assertTrue(any(level == "error" and "not found" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_keeps_checked_manual_packages_only(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", side_effect=[True, False]):
            added = app.add_packages_to_config(["tmux", "nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "error" and "nethock" in message for level, message in app.gum.messages))

    def test_add_packages_to_config_warns_when_manual_check_is_unavailable(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=None):
            added = app.add_packages_to_config(["tmux"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "warn" for level, _message in app.gum.messages))

    def test_add_packages_to_config_keeps_missing_manual_packages_when_copr_is_configured(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def success(self, message: str) -> None:
                self.messages.append(("success", message))

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def error(self, message: str) -> None:
                self.messages.append(("error", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=False):
            added = app.add_packages_to_config(["nethock"], source_label="manual entry")
        self.assertTrue(added)
        self.assertEqual(app.config.packages, ["nethock"])
        self.assertTrue(any(level == "warn" and "host repos" in message for level, message in app.gum.messages))

    def test_search_host_packages_parses_results_and_limits_output(self) -> None:
        app = self.make_app()
        seen_commands: list[list[str]] = []

        class GumStub:
            def spinner_result(self, _title, _command, *, cwd=None):
                seen_commands.append(list(_command))
                output = "\n".join(
                    [f"pkg{i}\tSummary {i}" for i in range(PACKAGE_SEARCH_LIMIT + 2)]
                )
                return subprocess.CompletedProcess(["dnf5", "repoquery"], 0, output, "")

        app.gum = GumStub()
        with patch("ublue_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("pkg")

        self.assertIsNone(message)
        self.assertTrue(truncated)
        self.assertEqual(len(results), PACKAGE_SEARCH_LIMIT)
        self.assertEqual(results[0], ("pkg0", "Summary 0"))
        self.assertTrue(any("%{name}\t%{summary}\n" in command for command in seen_commands))

    def test_search_host_packages_reports_missing_cache(self) -> None:
        app = self.make_app()

        class GumStub:
            def spinner_result(self, _title, _command, *, cwd=None):
                return subprocess.CompletedProcess(
                    ["dnf5", "repoquery"],
                    1,
                    "",
                    'Cache-only enabled but no cache for repository "fedora"',
                )

        app.gum = GumStub()
        with patch("ublue_builder.command_exists", side_effect=lambda name: name == "dnf5"):
            results, truncated, message = app.search_host_packages("tmux")

        self.assertEqual(results, [])
        self.assertFalse(truncated)
        self.assertIn("dnf5 makecache", message or "")

    def test_search_packages_can_remove_previously_selected_match(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]

        class GumStub:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def header(self, _title: str) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def input(self, **_kwargs) -> str:
                return "fish"

            def form_width(self, **_kwargs) -> int:
                return 72

            def choose(self, _options, **_kwargs):
                return []

            def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
                self.prompts.append(placeholder)

        app.gum = GumStub()
        with patch.object(app, "search_host_packages", return_value=([("fish", "Friendly interactive shell")], False, None)):
            app.search_packages()

        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, ["Removed 1 package(s). Press Enter to return to the package menu..."])

    def test_select_packages_allows_remove_path_in_create_flow(self) -> None:
        app = self.make_app()
        app.config.packages = ["fish"]

        class GumStub:
            def __init__(self) -> None:
                self.choices = ["Remove selected packages", "Continue to review"]

            def header(self, *_args, **_kwargs) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def choose(self, _options, **_kwargs):
                return [self.choices.pop(0)]

        app.gum = GumStub()
        with patch.object(app, "choose_to_remove", return_value=[]) as remove_mock:
            app.select_packages()

        remove_mock.assert_called_once_with(["fish"], "Remove Packages")
        self.assertEqual(app.config.packages, [])

    def test_select_packages_allows_remove_copr_and_service_paths_in_create_flow(self) -> None:
        app = self.make_app()
        app.config.copr_repos = ["foo/bar"]
        app.config.services = ["sshd.service"]

        class GumStub:
            def __init__(self) -> None:
                self.choices = ["Remove COPR repositories", "Remove enabled services", "Continue to review"]

            def header(self, *_args, **_kwargs) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def choose(self, _options, **_kwargs):
                return [self.choices.pop(0)]

        app.gum = GumStub()
        with patch.object(app, "choose_to_remove", side_effect=[[], []]) as remove_mock:
            app.select_packages()

        self.assertEqual(remove_mock.call_args_list[0].args, (["foo/bar"], "Remove COPR Repositories"))
        self.assertEqual(remove_mock.call_args_list[1].args, (["sshd.service"], "Remove Services"))
        self.assertEqual(app.config.copr_repos, [])
        self.assertEqual(app.config.services, [])

    def test_select_packages_shows_requested_package_note(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.hints: list[str] = []

            def header(self, *_args, **_kwargs) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, message: str) -> None:
                self.hints.append(message)

            def controls(self, *_parts: str) -> None:
                pass

            def choose(self, _options, **_kwargs):
                return ["Continue to review"]

        app.gum = GumStub()
        app.select_packages()

        self.assertIn(app.requested_packages_note(), app.gum.hints)

    def test_manual_packages_pauses_after_successful_add(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def header(self, _title: str) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def write(self, **_kwargs) -> str:
                return "tmux"

            def form_width(self, **_kwargs) -> int:
                return 80

            def success(self, _message: str) -> None:
                pass

            def warn(self, _message: str) -> None:
                pass

            def error(self, _message: str) -> None:
                pass

            def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
                self.prompts.append(placeholder)

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=True):
            app.manual_packages()
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertEqual(app.gum.prompts, ["Added 1 package(s). Press Enter to return to the package menu..."])

    def test_manual_packages_pauses_after_failed_add(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def header(self, _title: str) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def write(self, **_kwargs) -> str:
                return "nethock"

            def form_width(self, **_kwargs) -> int:
                return 80

            def success(self, _message: str) -> None:
                pass

            def warn(self, _message: str) -> None:
                pass

            def error(self, _message: str) -> None:
                pass

            def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
                self.prompts.append(placeholder)

        app.gum = GumStub()
        with patch.object(app, "lookup_host_package", return_value=False):
            app.manual_packages()
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.gum.prompts, ["No packages were added. Press Enter to return to the package menu..."])

    def test_select_common_services_replaces_curated_selection_only(self) -> None:
        app = self.make_app()
        app.config.services = ["custom.service", COMMON_SERVICES[0][1]]

        class GumStub:
            def header(self, _title: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def choose(self, _options, **_kwargs):
                return [f"{COMMON_SERVICES[1][0]} ({COMMON_SERVICES[1][1]})"]

            def success(self, _message: str) -> None:
                pass

        app.gum = GumStub()
        app.select_common_services()
        self.assertEqual(app.config.services, ["custom.service", COMMON_SERVICES[1][1]])

    def test_do_build_validates_before_creating_repo(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"
        app.config.packages = ["bad;rm"]
        with patch("ublue_builder.run") as run_mock:
            with self.assertRaisesRegex(CommandError, "Invalid package value"):
                app.do_build()
        run_mock.assert_not_called()

    def test_do_build_deletes_repo_if_setup_fails_after_creation(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def header(self, _title: str) -> None:
                pass

            def spinner(self, _title, _command, *, cwd=None) -> None:
                pass

            def warn(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(list(args), 0, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("ublue_builder.run", side_effect=fake_run):
            with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                with self.assertRaisesRegex(CommandError, "signing failed"):
                    app.do_build()
        self.assertIn(["gh", "repo", "delete", "example/test-image", "--yes"], run_calls)

    def test_do_build_sets_local_git_identity_before_initial_commit(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def header(self, _title: str) -> None:
                pass

            def spinner(self, _title, _command, *, cwd=None) -> None:
                pass

            def warn(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def style(self, *lines, **_kwargs) -> str:
                return "\n".join(lines)

            def content_width(self, reserve: int = 0) -> int:
                return 100 - reserve

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("ublue_builder.run", side_effect=fake_run):
            with patch.object(app, "ensure_signing_ready", return_value=True):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "seed_project_template", return_value=None):
                        with patch.object(app, "write_project_files", return_value=None):
                            self.assertTrue(app.do_build())

        self.assertIn(["git", "config", "user.name", "example"], run_calls)
        self.assertIn(["git", "config", "user.email", "example@users.noreply.github.com"], run_calls)
        self.assertIn(["git", "commit", "-m", "Initial image configuration via ublue-builder"], run_calls)

    def test_do_build_warns_about_hand_edited_managed_repos(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def header(self, _title: str) -> None:
                pass

            def spinner(self, _title, _command, *, cwd=None) -> None:
                pass

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

            def style(self, *lines, **_kwargs) -> str:
                return "\n".join(lines)

            def content_width(self, reserve: int = 0) -> int:
                return 100 - reserve

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("ublue_builder.run", side_effect=fake_run):
            with patch.object(app, "ensure_signing_ready", return_value=True):
                with patch.object(app, "repo_default_branch", return_value="main"):
                    with patch.object(app, "seed_project_template", return_value=None):
                        with patch.object(app, "write_project_files", return_value=None):
                            self.assertTrue(app.do_build())

        self.assertIn(("warn", MANAGED_REPO_WARNING), app.gum.messages)

    def test_do_build_explains_manual_cleanup_when_delete_scope_is_missing(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def __init__(self) -> None:
                self.messages: list[tuple[str, str]] = []

            def header(self, _title: str) -> None:
                pass

            def spinner(self, _title, _command, *, cwd=None) -> None:
                pass

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(list(args), 1, "", "")
            if args[:3] == ["gh", "repo", "delete"]:
                return subprocess.CompletedProcess(
                    list(args),
                    1,
                    "",
                    'HTTP 403: Must have admin rights to Repository.\nThis API operation needs the "delete_repo" scope.',
                )
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with patch("ublue_builder.run", side_effect=fake_run):
            with patch.object(app, "ensure_signing_ready", side_effect=CommandError("signing failed")):
                with self.assertRaisesRegex(CommandError, "signing failed"):
                    app.do_build()

        self.assertTrue(any("delete_repo scope" in message for level, message in app.gum.messages if level == "hint"))

    def test_push_update_sets_local_git_identity_before_commit(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def __init__(self) -> None:
                self.confirm_results = iter([False, True])

            def confirm(self, _prompt: str, default: bool = False) -> bool:
                return next(self.confirm_results)

            def success(self, _message: str) -> None:
                pass

            def warn(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

            def pager(self, _text: str) -> None:
                pass

        app.gum = GumStub()

        run_calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            run_calls.append(list(args))
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("ublue_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", return_value=True):
                    with patch.object(app, "write_project_files", return_value=None):
                        app.push_update("example", "test-image", repo_dir)

        self.assertIn(["git", "config", "user.name", "example"], run_calls)
        self.assertIn(["git", "config", "user.email", "example@users.noreply.github.com"], run_calls)
        self.assertIn(["git", "commit", "-m", f"Update image configuration via ublue-builder v{VERSION}"], run_calls)

    def test_push_update_warns_about_hand_edited_managed_repos(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.github_user = "example"

        class GumStub:
            def __init__(self) -> None:
                self.confirm_results = iter([False, True])
                self.messages: list[tuple[str, str]] = []

            def confirm(self, _prompt: str, default: bool = False) -> bool:
                return next(self.confirm_results)

            def success(self, _message: str) -> None:
                pass

            def warn(self, message: str) -> None:
                self.messages.append(("warn", message))

            def hint(self, message: str) -> None:
                self.messages.append(("hint", message))

            def enter_to_continue(self, _placeholder: str = "Press Enter to continue...") -> None:
                pass

            def pager(self, _text: str) -> None:
                pass

        app.gum = GumStub()

        def fake_run(args, **_kwargs):
            if list(args) == ["git", "diff", "--stat"]:
                return subprocess.CompletedProcess(list(args), 0, " build_files/build.sh | 1 +\n", "")
            if list(args) == ["git", "diff"]:
                return subprocess.CompletedProcess(list(args), 0, "diff --git a/x b/x\n", "")
            return subprocess.CompletedProcess(list(args), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("ublue_builder.run", side_effect=fake_run):
                with patch.object(app, "ensure_signing_ready", return_value=True):
                    with patch.object(app, "write_project_files", return_value=None):
                        app.push_update("example", "test-image", repo_dir)

        self.assertIn(("warn", MANAGED_REPO_WARNING), app.gum.messages)

    def test_create_new_image_starts_from_fresh_config(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["tmux"]
        app.config.services = ["sshd.service"]
        app.config.repo_name = "old-repo"
        seen: dict[str, object] = {}

        def fake_choose_base_image(**_kwargs):
            seen["packages"] = list(app.config.packages)
            seen["services"] = list(app.config.services)
            seen["repo_name"] = app.config.repo_name
            seen["github_user"] = app.config.github_user
            raise ScreenBack()

        with patch.object(app, "choose_base_image", side_effect=fake_choose_base_image):
            app.create_new_image()

        self.assertEqual(seen["packages"], [])
        self.assertEqual(seen["services"], [])
        self.assertEqual(seen["repo_name"], "")
        self.assertEqual(seen["github_user"], "example")

    def test_scan_os_resets_stale_config_before_loading_host_state(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["old-package"]
        app.config.removed_packages = ["old-removal"]

        class GumStub:
            def header(self, *_args, **_kwargs) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def table(self, *_args, **_kwargs) -> None:
                pass

            def error(self, *_args, **_kwargs) -> None:
                pass

            def warn(self, *_args, **_kwargs) -> None:
                pass

            def confirm(self, *_args, **_kwargs) -> bool:
                return True

            def table_widths(self, *_args, **_kwargs) -> str:
                return "20,40"

        status_payload = json.dumps(
            {
                "deployments": [
                    {
                        "booted": True,
                        "container-image-reference": "docker://ghcr.io/ublue-os/bazzite:stable",
                        "requested-packages": [],
                        "requested-base-removals": [],
                    }
                ]
            }
        )
        app.gum = GumStub()
        with patch("ublue_builder.command_exists", side_effect=lambda name: name == "rpm-ostree"):
            with patch(
                "ublue_builder.run",
                return_value=subprocess.CompletedProcess(["rpm-ostree", "status", "--json", "--booted"], 0, status_payload, ""),
            ):
                result = app.scan_os()

        self.assertTrue(result)
        self.assertEqual(app.config.packages, [])
        self.assertEqual(app.config.removed_packages, [])
        self.assertEqual(app.config.base_image_name, "Bazzite")
        self.assertEqual(app.config.github_user, "example")

    def test_select_repo_manual_entry_recovers_after_missing_repo(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"
        existing_label = f"{'existing-repo':<30} (no description)"

        class GumStub:
            def __init__(self) -> None:
                self.errors: list[str] = []
                self.prompts: list[str] = []
                self.filters = [manual_label, existing_label]

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def form_width(self, **_kwargs) -> int:
                return 72

            def filter(self, _options, **_kwargs) -> str:
                return self.filters.pop(0)

            def input(self, **_kwargs) -> str:
                return "missing repo"

            def error(self, message: str) -> None:
                self.errors.append(message)

            def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
                self.prompts.append(placeholder)

        app.gum = GumStub()
        with patch.object(
            app,
            "gh_json_with_spinner",
            return_value=[{"name": "existing-repo", "description": None}],
        ):
            with patch.object(app, "gh_json", side_effect=[CommandError("not found")]):
                owner, repo = app.select_repo()

        self.assertEqual((owner, repo), ("example", "existing-repo"))
        self.assertTrue(any("missing-repo" in message for message in app.gum.errors))
        self.assertEqual(app.gum.prompts, ["Press Enter to choose a different repository..."])

    def test_select_repo_allows_manual_entry_when_no_managed_repos_are_found(self) -> None:
        app = self.make_app()
        app.github_available = True
        app.github_user = "example"
        manual_label = "Type a repository name manually"

        class GumStub:
            def warn(self, _message: str) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, _message: str) -> None:
                pass

            def controls(self, *_parts: str) -> None:
                pass

            def form_width(self, **_kwargs) -> int:
                return 72

            def filter(self, _options, **_kwargs) -> str:
                return manual_label

            def input(self, **_kwargs) -> str:
                return "managed-repo"

        app.gum = GumStub()
        with patch.object(app, "gh_json_with_spinner", return_value=[]):
            with patch.object(app, "gh_json", return_value={"name": "managed-repo"}):
                with patch.object(app, "repo_has_state_file", return_value=True):
                    owner, repo = app.select_repo(require_state_file=True)

        self.assertEqual((owner, repo), ("example", "managed-repo"))

    def test_update_menu_restores_base_image_when_cancelled(self) -> None:
        app = self.make_app()
        base_choice = app.format_task_choice("Base image", "Bazzite")

        class GumStub:
            def __init__(self) -> None:
                self.choices = [base_choice, "Cancel and go back"]

            def header(self, *_args, **_kwargs) -> None:
                pass

            def instruction(self, _message: str) -> None:
                pass

            def hint(self, *_args, **_kwargs) -> None:
                pass

            def choose(self, _options, **_kwargs):
                return [self.choices.pop(0)]

        app.gum = GumStub()
        with patch.object(app, "choose_base_image", side_effect=ScreenBack()):
            result = app.update_menu()

        self.assertFalse(result)
        self.assertEqual(app.config.base_image_uri, "ghcr.io/ublue-os/bazzite:stable")
        self.assertEqual(app.config.base_image_name, "Bazzite")

    def test_bundled_template_snapshots_exist(self) -> None:
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / "Containerfile").is_file())
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / ".template-source").is_file())

    def test_clone_container_template_uses_bundled_snapshot(self) -> None:
        app = self.make_app()

        class GumStub:
            def spinner(self, title, command, *, cwd=None):
                from ublue_builder import run

                run(command, cwd=cwd)

        app.gum = GumStub()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "seeded"
            app.clone_container_template(target)
            self.assertTrue((target / "Containerfile").is_file())
            self.assertFalse((target / ".template-source").exists())

    def test_gum_input_raises_screen_back_when_interactive_command_aborts(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 1, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(ScreenBack):
                gum.input(prompt="Repository name: ")

    def test_gum_input_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 130, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(KeyboardInterrupt):
                gum.input(prompt="Repository name: ")

    def test_gum_confirm_raises_keyboard_interrupt_on_ctrl_c(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "confirm"], 130, "", "")
        with patch("ublue_builder.run", return_value=completed):
            with self.assertRaises(KeyboardInterrupt):
                gum.confirm("Continue?")

    def test_update_task_choices_show_current_status(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "ripgrep"]
        app.config.copr_repos = ["foo/bar"]
        app.config.services = ["sshd.service"]
        app.config.removed_packages = ["vim-enhanced"]
        choices = dict(app.update_task_choices())
        self.assertEqual(choices["Packages"], "tmux, ripgrep")
        self.assertEqual(choices["COPR repositories"], "foo/bar")
        self.assertEqual(choices["Services"], "sshd.service")
        self.assertEqual(choices["Removed base packages"], "vim-enhanced")

    def test_pager_text_with_hint_puts_exit_instruction_in_pager(self) -> None:
        app = self.make_app()
        text = app.pager_text_with_hint("diff --git a/file b/file\n+new line\n")
        self.assertTrue(text.startswith("Press q to close this diff and return to the previous screen."))
        self.assertIn("diff --git a/file b/file", text)

    def test_show_summary_uses_pager_for_read_only_view(self) -> None:
        app = self.make_app()
        app.github_user = "example"
        app.config.packages = ["tmux", "ripgrep"]

        class GumStub:
            def __init__(self) -> None:
                self.paged: list[str] = []

            def pager(self, text: str) -> None:
                self.paged.append(text)

        app.gum = GumStub()
        app.show_summary(step=4, total_steps=4, next_hint="This is the full build summary.")

        self.assertEqual(len(app.gum.paged), 1)
        self.assertIn("Review Build Configuration", app.gum.paged[0])
        self.assertIn("Press q to close this screen", app.gum.paged[0])
        self.assertIn("Repository", app.gum.paged[0])
        self.assertIn("Step 4 of 4.", app.gum.paged[0])

    def test_view_selections_uses_pager_for_read_only_view(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux"]
        app.config.services = ["sshd.service"]

        class GumStub:
            def __init__(self) -> None:
                self.paged: list[str] = []

            def pager(self, text: str) -> None:
                self.paged.append(text)

        app.gum = GumStub()
        app.view_selections()

        self.assertEqual(len(app.gum.paged), 1)
        self.assertIn("Current Selections", app.gum.paged[0])
        self.assertIn("- tmux", app.gum.paged[0])
        self.assertIn("- sshd.service", app.gum.paged[0])

    def test_patch_container_workflow_injects_cosign_key_into_existing_job_env(self) -> None:
        app = self.make_app()
        workflow = textwrap.dedent(
            """\
            name: Build container image
            jobs:
              build_push:
                env:
                  FOO: bar
                steps:
                  - name: Install Cosign
                    if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
                    uses: sigstore/cosign-installer@v3
            """
        )
        patched = app.patch_container_workflow(workflow)
        self.assertIn("      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}", patched)
        self.assertIn("      FOO: bar", patched)

    def test_generate_readme_uses_custom_base_title_and_lists_packages(self) -> None:
        app = self.make_app()
        app.config.base_image_name = "Bazzite"
        app.config.packages = ["tmux", "ripgrep"]
        readme = app.generate_readme()
        self.assertIn("# Custom Bazzite Image", readme)
        self.assertIn("| Base Image | `Bazzite` |", readme)
        self.assertIn("- `tmux`", readme)
        self.assertIn("- `ripgrep`", readme)
        self.assertIn("## Requested Packages", readme)
        self.assertIn("requested by this repo's generated build script", readme)
        self.assertIn(app.requested_packages_note(), readme)
        self.assertNotIn("## Installed Packages", readme)
        self.assertIn("## Managed By ublue-builder", readme)
        self.assertIn(f"`{STATE_FILE}`", readme)
        self.assertIn("stop using `ublue-builder` for this repo", readme)
        self.assertNotIn("## Local Build", readme)
        self.assertNotIn("just build", readme)

    def test_write_project_files_updates_readme_when_config_changes(self) -> None:
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)

            app.config.packages = ["tmux"]
            app.write_project_files(repo_dir, include_workflow=False)
            first_readme = (repo_dir / "README.md").read_text()

            app.config.packages = ["ripgrep"]
            app.write_project_files(repo_dir, include_workflow=False)
            second_readme = (repo_dir / "README.md").read_text()

        self.assertIn("- `tmux`", first_readme)
        self.assertNotIn("- `tmux`", second_readme)
        self.assertIn("- `ripgrep`", second_readme)


if __name__ == "__main__":
    unittest.main()
