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
    BLUEBUILD_TEMPLATE_DIR,
    CommandError,
    CONTAINERFILE_TEMPLATE_DIR,
    Config,
    Gum,
    config_from_state_payload,
    UserQuit,
)


class BuilderTests(unittest.TestCase):
    def make_app(self) -> App:
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="ghcr.io/ublue-os/bazzite:stable",
            base_image_name="Bazzite",
            base_image_tag="stable",
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

    def test_import_legacy_bluebuild_ignores_user_scope_entry(self) -> None:
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
            cfg = self.make_app().import_legacy_bluebuild(repo_dir)
        self.assertEqual(cfg.flatpaks, ["org.mozilla.firefox"])

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

    def test_patch_bluebuild_workflow_pins_action_and_normalizes_recipe(self) -> None:
        app = self.make_app()
        app.config.method = "bluebuild"
        app.config.signing_enabled = False
        workflow = textwrap.dedent(
            """\
            name: bluebuild
            on:
              schedule:
                - cron:
                    "00 06 * * *"
              push:
                paths-ignore:
                  - "**.md"
            jobs:
              bluebuild:
                steps:
                  - name: Build Custom Image
                    uses: blue-build/github-action@v1.11
                    with:
                      recipe: ${{ matrix.recipe }}
                      cosign_private_key: ${{ secrets.SIGNING_SECRET }}
                strategy:
                  matrix:
                    recipe:
                      - recipes/recipe.yml
            """
        )
        patched = app.patch_bluebuild_workflow(workflow)
        self.assertIn(ACTION_PINS["blue-build/github-action"][0], patched)
        self.assertIn('- ".ublue-builder.json"', patched)
        self.assertIn("- recipe.yml", patched)
        self.assertNotIn("recipes/recipe.yml", patched)
        self.assertNotIn("cosign_private_key:", patched)

    def test_validate_config_rejects_unsafe_package_token(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "bad;rm"]
        with self.assertRaisesRegex(CommandError, "Invalid package value"):
            app.validate_config()

    def test_base_image_picker_is_limited_to_beginner_images(self) -> None:
        self.assertEqual(
            [image.key for image in BASE_IMAGES],
            ["bazzite", "aurora", "aurora-dx", "bluefin", "bluefin-dx"],
        )

    def test_validate_config_rejects_unsupported_base_image(self) -> None:
        app = self.make_app()
        app.config.base_image_uri = "ghcr.io/ublue-os/bazzite-deck:stable"
        app.config.base_image_name = "Bazzite Deck"
        with self.assertRaisesRegex(CommandError, "supported base images"):
            app.validate_config()

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

    def test_bundled_template_snapshots_exist(self) -> None:
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / "Containerfile").is_file())
        self.assertTrue((CONTAINERFILE_TEMPLATE_DIR / ".template-source").is_file())
        self.assertTrue((BLUEBUILD_TEMPLATE_DIR / "recipes/recipe.yml").is_file())
        self.assertTrue((BLUEBUILD_TEMPLATE_DIR / ".template-source").is_file())

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

    def test_gum_input_raises_user_quit_when_interactive_command_aborts(self) -> None:
        gum = Gum()
        completed = subprocess.CompletedProcess(["gum", "input"], 130, "", "")
        with patch.object(Gum, "interactive_stdout", return_value=completed):
            with self.assertRaises(UserQuit):
                gum.input(prompt="Repository name: ")

    def test_update_task_choices_show_current_status(self) -> None:
        app = self.make_app()
        app.config.packages = ["tmux", "ripgrep"]
        app.config.copr_repos = ["foo/bar"]
        app.config.services = ["sshd.service"]
        choices = dict(app.update_task_choices())
        self.assertEqual(choices["Packages"], "2 selected")
        self.assertEqual(choices["COPR repositories"], "1 added")
        self.assertEqual(choices["Services"], "1 enabled")
        self.assertEqual(choices["Flatpaks"], "BlueBuild only")

    def test_update_task_choices_bluebuild_shows_flatpak_count(self) -> None:
        app = self.make_app()
        app.config.method = "bluebuild"
        app.config.flatpaks = ["org.mozilla.firefox"]
        choices = dict(app.update_task_choices())
        self.assertEqual(choices["Flatpaks"], "1 added")
        self.assertEqual(choices["Removed base packages"], "Containerfile only")


if __name__ == "__main__":
    unittest.main()
