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
    ScreenBack,
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

    def test_import_legacy_config_rejects_bluebuild_repo(self) -> None:
        recipe = textwrap.dedent(
            """\
            name: "legacy-image"
            description: "Legacy BlueBuild repo"
            base-image: "ghcr.io/ublue-os/bazzite"
            image-version: "stable"

            modules:
              - type: default-flatpaks
                configurations:
                  - notify: true
                    scope: system
                    install:
                      - "org.mozilla.firefox"
                  - scope: user
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "recipes").mkdir()
            (repo_dir / "recipes/recipe.yml").write_text(recipe)
            with self.assertRaisesRegex(CommandError, "BlueBuild repos are no longer supported"):
                self.make_app().import_legacy_config(repo_dir)

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

    def test_manual_packages_pauses_after_successful_add(self) -> None:
        app = self.make_app()

        class GumStub:
            def __init__(self) -> None:
                self.prompts: list[str] = []

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

            def hint(self, _message: str) -> None:
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

    def test_update_menu_restores_base_image_when_cancelled(self) -> None:
        app = self.make_app()
        base_choice = app.format_task_choice("Base image", "Bazzite")

        class GumStub:
            def __init__(self) -> None:
                self.choices = [base_choice, "Cancel and go back"]

            def header(self, *_args, **_kwargs) -> None:
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
        self.assertEqual(choices["Packages"], "2 selected")
        self.assertEqual(choices["COPR repositories"], "1 added")
        self.assertEqual(choices["Services"], "1 enabled")
        self.assertEqual(choices["Removed base packages"], "1 selected")

    def test_pager_text_with_hint_puts_exit_instruction_in_pager(self) -> None:
        app = self.make_app()
        text = app.pager_text_with_hint("diff --git a/file b/file\n+new line\n")
        self.assertTrue(text.startswith("Press q to close this diff and return to the previous screen."))
        self.assertIn("diff --git a/file b/file", text)

    def test_import_legacy_containerfile_splits_multiple_services(self) -> None:
        repo = textwrap.dedent(
            """\
            FROM ghcr.io/ublue-os/bazzite:stable
            """
        )
        build_sh = textwrap.dedent(
            """\
            #!/bin/bash
            set -ouex pipefail
            systemctl enable sshd.service cockpit.socket
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "Containerfile").write_text(repo)
            (repo_dir / "build_files").mkdir()
            (repo_dir / "build_files/build.sh").write_text(build_sh)
            cfg = self.make_app().import_legacy_containerfile(repo_dir)
        self.assertEqual(cfg.services, ["sshd.service", "cockpit.socket"])

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
