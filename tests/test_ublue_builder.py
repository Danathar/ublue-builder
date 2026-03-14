import tempfile
import textwrap
import unittest
from pathlib import Path

from ublue_builder import (
    ACTION_PINS,
    App,
    BLUEBUILD_TEMPLATE_DIR,
    CommandError,
    CONTAINERFILE_TEMPLATE_DIR,
    Config,
    config_from_state_payload,
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

    def make_query_stub_app(self, *, available: set[str] | None = None) -> App:
        class QueryStubApp(App):
            def __init__(self, available_packages: set[str]) -> None:
                super().__init__()
                self.available_packages = available_packages
                self.query_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

            def query_available_packages_in_image(self, packages, *, copr_repos=None):
                repos = tuple(copr_repos or ())
                self.query_calls.append((tuple(packages), repos))
                return set(self.available_packages).intersection(packages)

        app = QueryStubApp(available or set())
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

    def test_find_unavailable_packages_uses_cache(self) -> None:
        app = self.make_query_stub_app(available={"tmux"})
        self.assertEqual(app.find_unavailable_packages(["tmux", "ripgrep"]), ["ripgrep"])
        self.assertEqual(app.find_unavailable_packages(["tmux", "ripgrep"]), ["ripgrep"])
        self.assertEqual(len(app.query_calls), 1)

    def test_add_packages_to_config_keeps_available_and_warns_on_missing(self) -> None:
        app = self.make_query_stub_app(available={"tmux"})

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
        self.assertEqual(app.config.packages, ["tmux"])
        self.assertTrue(any(level == "warn" and "ripgrep" in message for level, message in app.gum.messages))

    def test_validate_package_availability_rejects_missing_packages(self) -> None:
        app = self.make_query_stub_app(available={"tmux"})
        app.config.packages = ["tmux", "ripgrep"]
        with self.assertRaisesRegex(CommandError, "ripgrep"):
            app.validate_package_availability()

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


if __name__ == "__main__":
    unittest.main()
