#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

VERSION = "6.0"
STATE_FILE = ".ublue-builder.json"
DEFAULT_REPO_NAME = "my-ublue-image"
DEFAULT_GITHUB_BUILD_CRON = "05 10 * * *"
CONTAINERFILE_TEMPLATE_REPO = "ublue-os/image-template"
CONTAINERFILE_TEMPLATE_REV = "ec2ccf3b7683d8435a2611eb99d0b702102557b5"
BLUEBUILD_TEMPLATE_REPO = "blue-build/template"
BLUEBUILD_TEMPLATE_REV = "d3f382af4c40c80bbd207507f4ead99b6144a281"
TEMPLATE_SNAPSHOT_DIR = Path(__file__).resolve().parent / "template_snapshots"
CONTAINERFILE_TEMPLATE_DIR = TEMPLATE_SNAPSHOT_DIR / "containerfile"
BLUEBUILD_TEMPLATE_DIR = TEMPLATE_SNAPSHOT_DIR / "bluebuild"
ALLOWED_METHODS = {"containerfile", "bluebuild"}
PACKAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+:-]+$")
COPR_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SERVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9@._:+-]+$")
FLATPAK_ID_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ACTION_PINS: dict[str, tuple[str, str]] = {
    "actions/checkout": ("de0fac2e4500dabe0009e67214ff5f5447ce83dd", "v6"),
    "ublue-os/remove-unwanted-software": ("695eb75bc387dbcd9685a8e72d23439d8686cba6", "v8"),
    "docker/metadata-action": ("c299e40c65443455700f0fdfc63efafe5b349051", "v5"),
    "redhat-actions/buildah-build": ("7a95fa7ee0f02d552a32753e7414641a04307056", "v2"),
    "docker/login-action": ("c94ce9fb468520275223c153574b00df6fe4bcc9", "v3"),
    "redhat-actions/push-to-registry": ("5ed88d269cf581ea9ef6dd6806d01562096bee9c", "v2"),
    "sigstore/cosign-installer": ("faadad0cce49287aee09b3a48701e75088a2c6ad", "v4.0.0"),
    "blue-build/github-action": ("24d146df25adc2cf579e918efe2d9bff6adea408", "v1.11"),
}


@dataclass(frozen=True)
class BaseImage:
    key: str
    name: str
    description: str
    image_uri: str
    tag: str


BASE_IMAGES: tuple[BaseImage, ...] = (
    BaseImage("bazzite", "Bazzite", "Best for gaming systems and handheld-style setups", "ghcr.io/ublue-os/bazzite:stable", "stable"),
    BaseImage("aurora", "Aurora (KDE)", "KDE desktop for everyday use", "ghcr.io/ublue-os/aurora:stable", "stable"),
    BaseImage("aurora-dx", "Aurora DX", "Aurora plus extra developer tools", "ghcr.io/ublue-os/aurora-dx:stable", "stable"),
    BaseImage("bluefin", "Bluefin (GNOME)", "GNOME desktop for everyday use", "ghcr.io/ublue-os/bluefin:stable", "stable"),
    BaseImage("bluefin-dx", "Bluefin DX", "Bluefin plus extra developer tools", "ghcr.io/ublue-os/bluefin-dx:stable", "stable"),
)

CATALOGS: dict[str, list[str]] = {
    "Development": "gcc gcc-c++ make cmake git rust cargo golang nodejs python3-pip java-latest-openjdk meson ninja-build kernel-devel strace valgrind".split(),
    "CLI Tools": "tmux zsh fish htop btop neovim vim-enhanced fzf ripgrep fd-find bat eza jq yq starship zoxide tldr just stow".split(),
    "Networking": "tailscale wireguard-tools nmap tcpdump wireshark-cli curl wget net-tools bind-utils traceroute mtr iperf3 socat ncat".split(),
    "Containers & VMs": "podman buildah skopeo distrobox toolbox cockpit cockpit-podman libvirt virt-manager qemu-kvm".split(),
    "Multimedia": "ffmpeg vlc mpv ImageMagick yt-dlp".split(),
    "Fonts": "google-noto-fonts-common google-noto-sans-fonts fira-code-fonts jetbrains-mono-fonts fontawesome-fonts".split(),
    "Security": "keepassxc age gnupg2 openssh-clients openssh-server".split(),
}


@dataclass
class Config:
    method: str = ""
    base_image_uri: str = ""
    base_image_name: str = ""
    base_image_tag: str = ""
    repo_name: str = ""
    image_desc: str = "My custom Universal Blue image"
    packages: list[str] = field(default_factory=list)
    copr_repos: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    flatpaks: list[str] = field(default_factory=list)
    removed_packages: list[str] = field(default_factory=list)
    signing_enabled: bool = False
    github_user: str = ""
    scanned_base: str = ""
    scanned_packages: list[str] = field(default_factory=list)
    scanned_removed: list[str] = field(default_factory=list)

    def normalize(self) -> None:
        self.packages = unique(self.packages)
        self.copr_repos = unique(self.copr_repos)
        self.services = unique(self.services)
        self.flatpaks = unique(self.flatpaks)
        self.removed_packages = unique(self.removed_packages)


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped and stripped not in seen:
            output.append(stripped)
            seen.add(stripped)
    return output


def sanitize_slug(value: str, default: str = DEFAULT_REPO_NAME) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]", "-", value.lower()).strip("-")
    return cleaned or default


def yaml_scalar(value: str) -> str:
    return json.dumps(value)


def ensure_trailing_newline(text: str) -> str:
    return text.rstrip("\n") + "\n"


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def validate_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    invalid = [item for item in value if not isinstance(item, str)]
    if invalid:
        raise ValueError(f"{field_name} must contain only strings")
    return list(value)


def config_from_state_payload(data: object) -> Config:
    if not isinstance(data, dict):
        raise ValueError("state file must contain a JSON object")
    state_version = data.get("state_version")
    if state_version is not None:
        if not isinstance(state_version, int):
            raise ValueError("state_version must be an integer")
        if state_version > 1:
            raise ValueError(f"unsupported state_version: {state_version}")

    cfg = Config()
    list_fields = {
        "packages",
        "copr_repos",
        "services",
        "flatpaks",
        "removed_packages",
        "scanned_packages",
        "scanned_removed",
    }
    string_fields = {
        "method",
        "base_image_uri",
        "base_image_name",
        "base_image_tag",
        "repo_name",
        "image_desc",
        "github_user",
        "scanned_base",
    }
    for name in list_fields:
        if name in data:
            setattr(cfg, name, validate_string_list(data[name], name))
    for name in string_fields:
        if name in data:
            value = data[name]
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            setattr(cfg, name, value)
    if "signing_enabled" in data:
        value = data["signing_enabled"]
        if not isinstance(value, bool):
            raise ValueError("signing_enabled must be a boolean")
        cfg.signing_enabled = value
    if cfg.method and cfg.method not in ALLOWED_METHODS:
        raise ValueError(f"unsupported build method: {cfg.method}")
    cfg.normalize()
    return cfg


def pin_action_uses_line(line: str) -> str:
    match = re.fullmatch(r"(\s*uses:\s+)([^@\s]+)@([^\s#]+)(.*)", line)
    if not match:
        return line
    prefix, action, _ref, suffix = match.groups()
    pin = ACTION_PINS.get(action)
    if not pin:
        return line
    sha, label = pin
    suffix = re.sub(r"\s+#.*$", "", suffix)
    comment = f" # {label}"
    return f"{prefix}{action}@{sha}{comment}"


def pinned_action(action: str) -> str:
    sha, label = ACTION_PINS[action]
    return f"{action}@{sha} # {label}"


class CommandError(RuntimeError):
    pass


class UserQuit(RuntimeError):
    pass


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        input=stdin,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() if proc.stderr else ""
        stdout = proc.stdout.strip() if proc.stdout else ""
        detail = stderr or stdout or f"command failed: {' '.join(args)}"
        raise CommandError(detail)
    return proc


class Gum:
    def require_interactive_success(self, proc: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        if proc.returncode != 0:
            raise UserQuit()
        return proc

    def clear(self) -> None:
        if sys.stdout.isatty() and os.environ.get("TERM"):
            run(["clear"], capture=False, check=False)

    def interactive_stdout(self, args: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=None,
            check=False,
        )

    def ensure_available(self) -> None:
        if not command_exists("gum"):
            raise SystemExit("gum is required. Install it with: brew install gum")

    def style(self, *lines: str, **opts: str | int | bool) -> str:
        args = ["gum", "style"]
        for key, value in opts.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    args.append(flag)
            else:
                args.extend([flag, str(value)])
        args.extend(lines)
        return run(args).stdout.rstrip("\n")

    def log(self, level: str, message: str) -> None:
        run(["gum", "log", "--level", level, message], capture=False)

    def success(self, message: str) -> None:
        self.log("info", message)

    def warn(self, message: str) -> None:
        self.log("warn", message)

    def error(self, message: str) -> None:
        self.log("error", message)

    def header(self, title: str, *, clear_screen: bool = True) -> None:
        if clear_screen:
            self.clear()
        print()
        print(self.style(f"━━━  {title}  ━━━", foreground=117, bold=True))
        print(self.style("Press q to quit before making changes.", faint=True, width=64))
        print()

    def hint(self, message: str) -> None:
        print(self.style(message, faint=True, width=64))

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        args = ["gum", "confirm", "--show-help", prompt]
        args.append("--default=true" if default else "--default=false")
        return run(args, check=False, capture=False).returncode == 0

    def input(
        self,
        *,
        prompt: str,
        value: str | None = None,
        placeholder: str | None = None,
        width: int | None = None,
    ) -> str:
        args = ["gum", "input", "--show-help", "--prompt", prompt]
        if value is not None:
            args.extend(["--value", value])
        if placeholder is not None:
            args.extend(["--placeholder", placeholder])
        if width is not None:
            args.extend(["--width", str(width)])
        return self.require_interactive_success(self.interactive_stdout(args)).stdout.rstrip("\n")

    def write(self, *, placeholder: str, height: int, width: int) -> str:
        return self.require_interactive_success(
            self.interactive_stdout(
                ["gum", "write", "--show-help", "--placeholder", placeholder, "--height", str(height), "--width", str(width)]
            )
        ).stdout.rstrip("\n")

    def choose(
        self,
        options: Sequence[str],
        *,
        height: int = 10,
        no_limit: bool = False,
        selected: Sequence[str] | None = None,
        header: str | None = None,
    ) -> list[str]:
        args = ["gum", "choose", "--show-help", "--height", str(height)]
        if no_limit:
            args.append("--no-limit")
        if selected:
            args.extend(["--selected", ",".join(selected)])
        if header:
            args.extend(["--header", header])
        proc = self.require_interactive_success(self.interactive_stdout(args, stdin="\n".join(options) + "\n"))
        output = proc.stdout.strip("\n")
        return [line for line in output.splitlines() if line]

    def filter(self, options: Sequence[str], *, height: int = 20, placeholder: str = "Search...") -> str:
        proc = self.require_interactive_success(
            self.interactive_stdout(
                ["gum", "filter", "--show-help", "--height", str(height), "--placeholder", placeholder],
                stdin="\n".join(options) + "\n",
            )
        )
        return proc.stdout.strip()

    def pager(self, text: str) -> None:
        run(["gum", "pager"], capture=False, stdin=text)

    def table(self, rows: Sequence[Sequence[str]], *, columns: str, widths: str) -> None:
        text = "\n".join("\t".join(row) for row in rows) + "\n"
        run(["gum", "table", "--separator", "\t", "--columns", columns, "--widths", widths], capture=False, stdin=text)

    def spinner(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> None:
        run(["gum", "spin", "--spinner", "dot", "--title", title, "--", *command], cwd=cwd, capture=False)

    def spinner_capture(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> str:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name
        try:
            shell_command = f"{shlex.join(command)} > {shlex.quote(output_path)}"
            run(
                ["gum", "spin", "--spinner", "dot", "--title", title, "--", "bash", "-lc", shell_command],
                cwd=cwd,
                capture=False,
            )
            return Path(output_path).read_text()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
        self.require_interactive_success(self.interactive_stdout(["gum", "input", "--show-help", "--placeholder", placeholder]))


class App:
    def __init__(self) -> None:
        self.gum = Gum()
        self.config = Config()
        self.github_available = False
        self.github_user = ""
        self.used_legacy_import = False
        self.generated_cosign_pub: str | None = None

    def banner(self) -> None:
        print(
            self.gum.style(
                f"Universal Blue Custom Image Builder  v{VERSION}",
                "",
                "Build custom OCI images from Universal Blue base images.",
                "Guided setup for beginner Bazzite, Aurora, and Bluefin users.",
                align="center",
                width=68,
                margin="1 2",
                padding="1 2",
                foreground=117,
                border_foreground=117,
                border="double",
            )
        )

    def clear(self) -> None:
        self.gum.clear()

    def gh_json(self, args: Sequence[str]) -> object:
        proc = run(["gh", *args])
        return json.loads(proc.stdout or "null")

    def gh_json_with_spinner(self, title: str, args: Sequence[str]) -> object:
        output = self.gum.spinner_capture(title, ["gh", *args])
        return json.loads(output or "null")

    def show_step_header(self, title: str, *, step: int, total_steps: int) -> None:
        self.gum.header(title)
        self.gum.hint(f"Step {step} of {total_steps}.")
        print()

    def format_task_choice(self, title: str, status: str) -> str:
        return f"{title:<24} {status}"

    def truncate_label(self, value: str, limit: int = 36) -> str:
        clean = " ".join(value.split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3] + "..."

    def update_task_choices(self) -> list[tuple[str, str]]:
        return [
            ("Packages", f"{len(self.config.packages)} selected"),
            ("Base image", self.config.base_image_name or "(not set)"),
            ("Description", self.truncate_label(self.config.image_desc or "(empty)")),
            ("COPR repositories", f"{len(self.config.copr_repos)} added"),
            ("Services", f"{len(self.config.services)} enabled"),
            ("Flatpaks", f"{len(self.config.flatpaks)} added" if self.config.method == "bluebuild" else "BlueBuild only"),
            (
                "Removed base packages",
                f"{len(self.config.removed_packages)} selected" if self.config.method == "containerfile" else "Containerfile only",
            ),
        ]

    def preflight(self) -> None:
        self.gum.ensure_available()
        self.gum.header("Preflight Checks", clear_screen=False)
        self.gum.hint("Checking required tools and the runtime environment...")
        print()

        if not command_exists("git"):
            raise SystemExit("git is required. Install it with: brew install git")
        self.gum.success("git found")

        if command_exists("gh"):
            if run(["gh", "auth", "status"], check=False).returncode == 0:
                try:
                    self.github_user = str(self.gh_json(["api", "user"])["login"])
                    self.github_available = True
                    self.config.github_user = self.github_user
                    self.gum.success(f"GitHub CLI authenticated as: {self.github_user}")
                except Exception:
                    self.github_available = False
                    self.gum.warn("GitHub CLI authenticated, but username lookup failed.")
            else:
                self.gum.warn("GitHub CLI found but not logged in.")
        else:
            self.gum.warn("GitHub CLI not found. Install it with: brew install gh")

        if command_exists("cosign"):
            self.gum.success("cosign found (image signing available)")
        else:
            self.gum.warn("cosign not found (signed builds will be skipped)")

        if command_exists("rpm-ostree"):
            self.gum.success("rpm-ostree found (OS scan available)")
        else:
            self.gum.warn("rpm-ostree not found (OS scan unavailable)")

        print()
        self.gum.enter_to_continue("Press Enter to continue...")

    def require_github(self) -> bool:
        if self.github_available and self.github_user:
            return True
        if not command_exists("gh"):
            self.gum.error("GitHub CLI is required for this action.")
            print()
            self.gum.hint("Install it with: brew install gh")
            return False
        if run(["gh", "auth", "status"], check=False).returncode != 0:
            self.github_setup_guide()
        try:
            self.github_user = str(self.gh_json(["api", "user"])["login"])
        except Exception:
            self.gum.error("Unable to determine GitHub username after login.")
            return False
        self.config.github_user = self.github_user
        self.github_available = True
        self.gum.success(f"GitHub ready: {self.github_user}")
        return True

    def github_setup_guide(self) -> None:
        print(
            self.gum.style(
                "GitHub Account Required",
                "",
                "This tool stores your image configuration on GitHub",
                "and uses GitHub Actions to build it automatically.",
                align="left",
                width=64,
                margin="0 2",
                padding="1 2",
                foreground=117,
                border_foreground=117,
                border="rounded",
            )
        )
        print()
        self.gum.hint("Choose one option below, then press Enter.")
        print()
        choice = self.gum.choose(
            [
                "I already have a GitHub account - log me in",
                "I need to create a GitHub account first",
                "Quit",
            ],
            height=5,
        )
        selected = choice[0] if choice else "Quit"
        if selected.startswith("Quit"):
            raise SystemExit(0)
        if selected.startswith("I need to create"):
            print(
                self.gum.style(
                    "Create a GitHub Account",
                    "",
                    "Go to https://github.com/signup in a browser,",
                    "create an account, then return here for login.",
                    align="left",
                    width=64,
                    margin="0 2",
                    padding="1 2",
                    foreground=11,
                    border_foreground=11,
                    border="rounded",
                )
            )
            print()
            if self.gum.confirm("Open github.com/signup now?", default=True):
                if command_exists("xdg-open"):
                    run(["xdg-open", "https://github.com/signup"], check=False, capture=False)
                elif command_exists("open"):
                    run(["open", "https://github.com/signup"], check=False, capture=False)
            self.gum.enter_to_continue("Press Enter after you've created the account...")

        print(
            self.gum.style(
                "Log In to GitHub",
                "",
                "The GitHub CLI will now guide you through login.",
                "Use GitHub.com, HTTPS, and browser login.",
                align="left",
                width=64,
                margin="0 2",
                padding="1 2",
                foreground=117,
                border_foreground=117,
                border="rounded",
            )
        )
        print()
        if not self.gum.confirm("Ready to log in?", default=True):
            raise SystemExit(0)
        if run(["gh", "auth", "login"], check=False, capture=False).returncode != 0:
            raise SystemExit("GitHub login failed. Try: gh auth login")

    def main_menu(self) -> None:
        while True:
            self.gum.header("Main Menu")
            self.gum.hint("Use the arrow keys to move and Enter to choose.")
            print()
            action = self.gum.choose(
                [
                    "Create New Image",
                    "Scan OS & Migrate Layered Packages",
                    "Update Existing Image",
                    "Quit",
                ],
                height=8,
            )
            selected = action[0] if action else "Quit"
            if selected == "Quit":
                raise SystemExit(0)
            if selected == "Create New Image":
                self.create_new_image()
                continue
            if selected == "Scan OS & Migrate Layered Packages":
                if self.scan_os():
                    self.create_new_image(scanned=True)
                continue
            if selected == "Update Existing Image":
                self.update_existing_image()
                continue

    def create_new_image(self, *, scanned: bool = False) -> None:
        total_steps = 5
        step = 1
        while True:
            if step == 1:
                self.choose_method(step=step, total_steps=total_steps)
                step = 2
                continue
            if step == 2:
                self.choose_base_image(step=step, total_steps=total_steps)
                step = 3
                continue
            if step == 3:
                self.configure_repo(step=step, total_steps=total_steps)
                step = 4
                continue
            if step == 4:
                self.select_packages(step=step, total_steps=total_steps)
                step = 5
                continue
            action = self.review_new_image(step=step, total_steps=total_steps)
            if action == "build":
                if self.do_build():
                    return
                continue
            if action == "method":
                step = 1
            elif action == "base":
                step = 2
            elif action == "repo":
                step = 3
            elif action == "software":
                step = 4
            else:
                return

    def choose_method(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Build Method", step=step, total_steps=total_steps)
        else:
            self.gum.header("Build Method")
        if self.config.scanned_removed:
            self.gum.warn("Removed base packages from your scan only work with Containerfile.")
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        print()
        options = [
            "Containerfile  - Shell script based (recommended)",
            "BlueBuild      - YAML recipe based",
        ]
        selection = self.gum.choose(options, height=4)
        selected = selection[0] if selection else options[0]
        self.config.method = "containerfile" if selected.startswith("Containerfile") else "bluebuild"
        if self.config.method == "bluebuild" and self.config.removed_packages:
            self.gum.warn("Removed base packages are only supported in Containerfile mode. Using Containerfile instead.")
            self.config.method = "containerfile"
        self.gum.success(f"Method: {self.config.method}")

    def choose_base_image(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Base Image", step=step, total_steps=total_steps)
        else:
            self.gum.header("Base Image")
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        self.gum.hint("DX means the image starts with extra developer tools already included.")
        print()
        if self.config.base_image_uri:
            matched = self.match_base_image(self.config.base_image_uri)
            if matched:
                print(f"  Detected base image: {self.gum.style(self.config.base_image_name or self.config.base_image_uri, bold=True)}")
                print(f"  Image: {self.gum.style(self.config.base_image_uri, foreground=117)}")
                print()
                if self.gum.confirm("Use this base image?", default=True):
                    return
            else:
                self.gum.warn("This tool now supports only Aurora, Aurora DX, Bluefin, Bluefin DX, and Bazzite.")
                self.gum.hint("Choose one of those supported starting images below.")
                print()
                self.config.base_image_uri = ""
                self.config.base_image_name = ""
                self.config.base_image_tag = ""

        options = [
            f"{image.name:<25} {image.description}  [{image.image_uri}]"
            for image in BASE_IMAGES
        ]
        choice = self.gum.choose(options, height=14)
        selected = choice[0] if choice else options[0]
        for image in BASE_IMAGES:
            if selected.startswith(image.name):
                self.config.base_image_uri = image.image_uri
                self.config.base_image_name = image.name
                self.config.base_image_tag = image.tag
                break
        self.gum.success(f"Base image: {self.config.base_image_name} ({self.config.base_image_uri})")

    def configure_repo(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Repository Configuration", step=step, total_steps=total_steps)
        else:
            self.gum.header("Repository Configuration")
        self.gum.hint("Repository names use letters, numbers, dashes, and dots. Spaces are turned into dashes.")
        print()
        default_name = self.config.repo_name or DEFAULT_REPO_NAME
        raw_name = self.gum.input(prompt="Repository name: ", value=default_name, placeholder=default_name, width=60)
        self.config.repo_name = sanitize_slug(raw_name, default_name)
        print()
        self.config.image_desc = self.gum.input(
            prompt="Description: ",
            value=self.config.image_desc,
            placeholder="Description",
            width=80,
        ) or self.config.image_desc
        print()
        self.gum.hint("Repositories created by this tool are public.")
        print()
        if self.github_user:
            self.gum.success(f"Repo: {self.github_user}/{self.config.repo_name}")
        else:
            self.gum.success(f"Repo name: {self.config.repo_name}")

    def select_packages(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Software Selection", step=step, total_steps=total_steps)
        else:
            self.gum.header("Software Selection")
        while True:
            self.gum.hint("Use the arrow keys to move and Enter to choose.")
            self.gum.hint("Choose Done when you are finished and want to keep going.")
            self.gum.hint(
                f"Current picks: {len(self.config.packages)} packages, {len(self.config.services)} services, {len(self.config.flatpaks)} Flatpaks."
            )
            print()
            selection = self.gum.choose(
                [
                    "Browse package catalog",
                    "Type package names manually",
                    "Add a COPR repository",
                    "Add systemd services to enable",
                    "Add Flatpaks (BlueBuild only)",
                    "View current selections",
                    "Done",
                ],
                height=10,
            )
            selected = selection[0] if selection else "Done"
            if selected == "Done":
                self.config.normalize()
                return
            if selected == "Browse package catalog":
                self.select_from_catalog()
            elif selected == "Type package names manually":
                self.manual_packages()
            elif selected == "Add a COPR repository":
                self.add_copr()
            elif selected == "Add systemd services to enable":
                self.add_services()
            elif selected == "Add Flatpaks (BlueBuild only)":
                self.add_flatpaks()
            elif selected == "View current selections":
                self.view_selections()

    def select_from_catalog(self) -> None:
        self.gum.header("Package Catalog")
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        self.gum.hint("Choose Back to return to the previous menu.")
        print()
        options = list(CATALOGS) + ["Back"]
        choice = self.gum.choose(options, height=10)
        selected = choice[0] if choice else "Back"
        if selected == "Back":
            return
        current = set(self.config.packages)
        self.gum.hint("Move with the arrow keys. Use the help shown at the bottom to mark packages.")
        self.gum.hint("Press Enter when you are finished, or leave everything unselected to make no changes.")
        print()
        picked = self.gum.choose(
            CATALOGS[selected],
            height=20,
            no_limit=True,
            selected=[pkg for pkg in CATALOGS[selected] if pkg in current],
            header=selected,
        )
        self.add_packages_to_config(picked, source_label=selected)

    def manual_packages(self) -> None:
        print()
        self.gum.hint("Enter RPM package names separated by spaces or newlines.")
        self.gum.hint("The GitHub build will do the final check that each package name exists.")
        self.gum.hint("Leave this empty if you want to go back without adding anything.")
        print()
        raw = self.gum.write(placeholder="Enter package names...", height=6, width=70)
        self.add_packages_to_config((token.strip(",") for token in raw.split()), source_label="manual entry")

    def add_copr(self) -> None:
        print()
        self.gum.hint("Leave the COPR repo field empty if you want to go back.")
        print()
        repo = self.gum.input(prompt="COPR repo: ", placeholder="owner/project", width=50)
        repo = repo.strip()
        if not repo:
            return
        if not COPR_REPO_RE.fullmatch(repo):
            self.gum.error("Enter the COPR repo as owner/project.")
            return
        proposed_copr_repos = unique([*self.config.copr_repos, repo])
        pkgs = self.gum.input(prompt="Packages: ", placeholder="package1 package2", width=60)
        packages = [pkg.strip(",") for pkg in pkgs.split()]
        if packages and not self.add_packages_to_config(packages, source_label=f"COPR {repo}"):
            return
        self.config.copr_repos = proposed_copr_repos
        self.config.normalize()
        self.gum.success(f"Added COPR: {repo}")
        self.gum.hint("The GitHub build will confirm that the COPR repo and package names are valid.")

    def add_services(self) -> None:
        print()
        self.gum.hint("Enter one service per line. Leave this empty if you want to go back.")
        raw = self.gum.write(placeholder="Enter service names, one per line...", height=5, width=50)
        self.config.services.extend(line.strip() for line in raw.splitlines())
        self.config.normalize()
        self.gum.success(f"Total services configured: {len(self.config.services)}")

    def add_flatpaks(self) -> None:
        if self.config.method != "bluebuild":
            self.gum.warn("Flatpaks in generated config are only supported in BlueBuild mode.")
            return
        self.gum.hint("Enter one Flatpak ID per line. Leave this empty if you want to go back.")
        raw = self.gum.write(placeholder="Enter flatpak IDs, one per line...", height=5, width=60)
        self.config.flatpaks.extend(line.strip() for line in raw.splitlines())
        self.config.normalize()
        self.gum.success(f"Total flatpaks configured: {len(self.config.flatpaks)}")

    def view_selections(self) -> None:
        self.gum.header("Current Selections")
        self.gum.hint("This is a read-only summary.")
        self.gum.hint("Press Enter to go back to the software menu.")
        print()
        rows = [
            ("Packages", ", ".join(self.config.packages) or "(none)"),
            ("COPR Repos", ", ".join(self.config.copr_repos) or "(none)"),
            ("Services", ", ".join(self.config.services) or "(none)"),
            ("Flatpaks", ", ".join(self.config.flatpaks) or "(none)"),
            ("Removed Base Packages", ", ".join(self.config.removed_packages) or "(none)"),
        ]
        self.gum.table(rows, columns="Setting,Value", widths="20,60")
        print()
        self.gum.enter_to_continue("Press Enter to go back to the software menu...")

    def show_summary(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Review Build Configuration", step=step, total_steps=total_steps)
        else:
            self.gum.header("Review Build Configuration")
        self.gum.hint("This is a read-only summary of the current settings.")
        print()
        rows = [
            ("Repository", f"{self.github_user}/{self.config.repo_name}" if self.github_user else self.config.repo_name),
            ("Method", self.config.method),
            ("Description", self.config.image_desc),
            ("Base Image", self.config.base_image_name or self.config.base_image_uri),
            ("Image URI", self.config.base_image_uri),
            ("Packages", str(len(self.config.packages))),
            ("COPR Repos", str(len(self.config.copr_repos))),
            ("Services", str(len(self.config.services))),
            ("Flatpaks", str(len(self.config.flatpaks))),
            ("Removed Base Packages", str(len(self.config.removed_packages))),
        ]
        self.gum.table(rows, columns="Setting,Value", widths="20,55")
        if self.config.removed_packages and self.config.method != "containerfile":
            print()
            self.gum.warn("Removed base packages are only applied in Containerfile mode.")

    def review_new_image(self, *, step: int, total_steps: int) -> str:
        self.show_summary(step=step, total_steps=total_steps)
        print()
        self.gum.hint("Choose Continue to create the GitHub repo and start the build.")
        self.gum.hint("Choose one of the edit options if you want to change something first.")
        print()
        options = [
            "Continue and start GitHub build",
            "Edit software",
            "Edit repository name and description",
            "Edit base image",
            "Edit build method",
            "Cancel and go back to the main menu",
        ]
        choice = self.gum.choose(options, height=10)
        selected = choice[0] if choice else options[-1]
        if selected.startswith("Continue"):
            return "build"
        if selected.startswith("Edit software"):
            return "software"
        if selected.startswith("Edit repository"):
            return "repo"
        if selected.startswith("Edit base image"):
            return "base"
        if selected.startswith("Edit build method"):
            return "method"
        return "cancel"

    def scan_os(self) -> bool:
        self.gum.header("Scanning Running OS")
        if not command_exists("rpm-ostree"):
            self.gum.error("rpm-ostree not found. OS scanning is unavailable.")
            return False

        proc = run(["rpm-ostree", "status", "--json", "--booted"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            proc = run(["rpm-ostree", "status", "--json"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            self.gum.error("Failed to read rpm-ostree status.")
            return False

        status = json.loads(proc.stdout)
        deployments = status.get("deployments", [])
        booted = next((item for item in deployments if item.get("booted")), deployments[0] if deployments else {})
        if not booted:
            self.gum.error("No deployment information found.")
            return False

        container_ref = (
            booted.get("container-image-reference")
            or booted.get("origin")
            or ""
        )
        base = container_ref
        for prefix in (
            "ostree-image-signed:docker://",
            "ostree-unverified-registry:",
            "ostree-remote-image:fedora:docker://",
            "docker://",
        ):
            if base.startswith(prefix):
                base = base[len(prefix):]
        self.config.scanned_base = base
        self.config.scanned_packages = unique(booted.get("requested-packages", []))
        self.config.scanned_removed = unique(booted.get("requested-base-removals", []))
        self.config.removed_packages = list(self.config.scanned_removed)

        self.config.base_image_uri = base
        self.config.base_image_name = base
        self.config.base_image_tag = "latest"
        matched = self.match_base_image(base)
        if matched:
            self.config.base_image_uri = matched.image_uri
            self.config.base_image_name = matched.name
            self.config.base_image_tag = matched.tag

        self.gum.header("Scan Results")
        rows = [
            ("Base Image", self.config.base_image_name),
            ("Image URI", self.config.base_image_uri),
            ("Layered Packages", str(len(self.config.scanned_packages))),
            ("Removed Base Packages", str(len(self.config.scanned_removed))),
        ]
        self.gum.table(rows, columns="Setting,Value", widths="22,52")
        print()

        if self.config.scanned_packages:
            self.gum.hint("Move with the arrow keys. Use the help shown at the bottom to mark packages to carry over.")
            self.gum.hint("Press Enter when you are finished, or leave everything unselected to skip them.")
            print()
            selected = self.gum.choose(
                self.config.scanned_packages,
                height=20,
                no_limit=True,
                selected=self.config.scanned_packages,
                header="Layered Packages",
            )
            self.config.packages = selected
        else:
            self.gum.warn("No layered packages found.")
            if not self.gum.confirm("Continue to create a custom image anyway?", default=True):
                return False

        if self.config.scanned_removed:
            self.gum.hint("Move with the arrow keys. Use the help shown at the bottom to mark packages to remove.")
            self.gum.hint("Press Enter when you are finished, or leave everything unselected to skip them.")
            print()
            selected_removed = self.gum.choose(
                self.config.scanned_removed,
                height=20,
                no_limit=True,
                selected=self.config.scanned_removed,
                header="Base Packages To Remove",
            )
            self.config.removed_packages = selected_removed

        self.config.normalize()
        return True

    def match_base_image(self, value: str) -> BaseImage | None:
        for image in BASE_IMAGES:
            image_repo = image.image_uri.rsplit(":", 1)[0]
            if value == image.image_uri or value == image_repo or value.startswith(f"{image_repo}:") or value.startswith(f"{image_repo}@"):
                return image
        return None

    def repo_secret_exists(self, owner: str, repo: str, secret_name: str) -> bool:
        if not command_exists("gh"):
            return False
        proc = run(["gh", "secret", "list", "-R", f"{owner}/{repo}"], check=False)
        if proc.returncode != 0:
            return False
        return any(line.split()[0] == secret_name for line in proc.stdout.splitlines() if line.strip())

    def repo_file_exists(self, owner: str, repo: str, path: str) -> bool:
        proc = run(["gh", "api", f"/repos/{owner}/{repo}/contents/{path}"], check=False)
        return proc.returncode == 0

    def repo_has_state_file(self, owner: str, repo: str) -> bool:
        return self.repo_file_exists(owner, repo, STATE_FILE)

    def repo_is_builder_managed_dir(self, repo_dir: Path) -> bool:
        return (repo_dir / STATE_FILE).is_file()

    def repo_looks_like_legacy_dir(self, repo_dir: Path) -> bool:
        if self.repo_is_builder_managed_dir(repo_dir):
            return False
        if (repo_dir / "recipes/recipe.yml").exists():
            return True
        build_script_exists = (repo_dir / "build_files/build.sh").exists() or (repo_dir / "build.sh").exists()
        return (repo_dir / "Containerfile").exists() and build_script_exists

    def maybe_enable_signing(self, owner: str, repo: str) -> bool:
        self.generated_cosign_pub = None
        if self.repo_secret_exists(owner, repo, "SIGNING_SECRET"):
            return True
        if not command_exists("cosign"):
            return False
        with tempfile.TemporaryDirectory(prefix="ublue-signing.") as tmp:
            tmpdir = Path(tmp)
            env = os.environ.copy()
            env["COSIGN_PASSWORD"] = ""
            proc = run(["cosign", "generate-key-pair"], cwd=tmpdir, env=env, check=False)
            key_path = tmpdir / "cosign.key"
            pub_path = tmpdir / "cosign.pub"
            if proc.returncode != 0 or not key_path.exists() or not pub_path.exists():
                self.gum.warn("Unable to generate cosign keypair. Builds will stay unsigned.")
                return False
            with key_path.open("rb") as key_handle:
                secret_proc = subprocess.run(
                    ["gh", "secret", "set", "SIGNING_SECRET", "-R", f"{owner}/{repo}"],
                    cwd=str(tmpdir),
                    stdin=key_handle,
                    text=False,
                    capture_output=True,
                    check=False,
                )
            if secret_proc.returncode != 0:
                self.gum.warn("Unable to upload SIGNING_SECRET. Builds will stay unsigned.")
                return False
            self.generated_cosign_pub = pub_path.read_text()
        self.gum.success("Configured SIGNING_SECRET for image signing.")
        return True

    def clone_repo(self, owner: str, repo: str, target: Path) -> None:
        self.gum.spinner(f"Cloning {owner}/{repo}...", ["gh", "repo", "clone", f"{owner}/{repo}", str(target)])

    def copy_template_snapshot(self, target: Path, *, repo: str, source_dir: Path) -> None:
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source_dir.is_dir():
            raise CommandError(f"Bundled template snapshot not found for {repo}.")
        if target.exists():
            if any(target.iterdir()):
                raise CommandError(f"{target} already exists and is not empty.")
            target.rmdir()
        self.gum.spinner(
            f"Copying bundled {repo} template...",
            ["python3", "-c", "import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2], ignore=shutil.ignore_patterns('.template-source'))", str(source_dir), str(target)],
        )

    def clone_container_template(self, target: Path) -> None:
        self.copy_template_snapshot(target, repo=CONTAINERFILE_TEMPLATE_REPO, source_dir=CONTAINERFILE_TEMPLATE_DIR)

    def clone_bluebuild_template(self, target: Path) -> None:
        self.copy_template_snapshot(target, repo=BLUEBUILD_TEMPLATE_REPO, source_dir=BLUEBUILD_TEMPLATE_DIR)

    def current_branch(self, repo_dir: Path) -> str:
        proc = run(["git", "branch", "--show-current"], cwd=repo_dir)
        branch = proc.stdout.strip()
        if branch:
            return branch
        proc = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
        return proc.stdout.strip() or "HEAD"

    def repo_default_branch(self, owner: str, repo: str) -> str:
        data = self.gh_json(["repo", "view", f"{owner}/{repo}", "--json", "defaultBranchRef"])
        branch = data.get("defaultBranchRef", {}).get("name")
        if branch:
            return branch
        proc = run(["gh", "api", f"/repos/{owner}/{repo}"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return "main"
            branch = data.get("default_branch")
            if isinstance(branch, str) and branch:
                return branch
        return "main"

    def seed_project_template(self, target: Path) -> None:
        if self.config.method == "containerfile":
            self.clone_container_template(target)
            return
        if self.config.method == "bluebuild":
            self.clone_bluebuild_template(target)
            return
        raise CommandError(f"Unsupported build method: {self.config.method}")

    def add_packages_to_config(
        self,
        candidates: Iterable[str],
        *,
        source_label: str,
    ) -> bool:
        packages = unique(candidates)
        if not packages:
            return False
        try:
            self.validate_token_list(packages, PACKAGE_TOKEN_RE, "package")
        except CommandError as exc:
            self.gum.error(str(exc))
            return False
        self.config.packages.extend(packages)
        self.config.normalize()
        self.gum.success(f"Added {len(packages)} package(s) from {source_label}")
        return True

    def do_build(self) -> bool:
        if not self.require_github():
            return False
        owner = self.github_user
        repo = self.config.repo_name
        self.config.github_user = owner
        self.validate_config()
        self.gum.header("Building Image")
        exists = run(["gh", "repo", "view", f"{owner}/{repo}", "--json", "name"], check=False).returncode == 0
        if exists:
            self.gum.error(f"{owner}/{repo} already exists on GitHub.")
            if self.repo_has_state_file(owner, repo):
                self.gum.hint("That repo was already created by this tool. Use 'Update Existing Image' to change it, or pick a new repo name.")
            else:
                self.gum.hint("That repo was not created by this tool. Use 'Import Legacy Repo' if you want this tool to take it over, or pick a new repo name.")
            self.gum.enter_to_continue("Press Enter to go back to the review screen...")
            return False
        self.gum.spinner(
            f"Creating {owner}/{repo}...",
            ["gh", "repo", "create", repo, "--description", self.config.image_desc, "--public"],
        )
        self.config.signing_enabled = self.maybe_enable_signing(owner, repo)

        with tempfile.TemporaryDirectory(prefix="ublue-builder.") as tmp:
            tmpdir = Path(tmp)
            self.seed_project_template(tmpdir)
            branch = self.repo_default_branch(owner, repo)
            run(["git", "init", "-b", branch], cwd=tmpdir)
            run(["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"], cwd=tmpdir)
            self.write_project_files(tmpdir, include_workflow=True)
            run(["git", "add", "-A"], cwd=tmpdir)
            run(["git", "commit", "-m", "Initial image configuration via ublue-builder"], cwd=tmpdir, check=False)
            run(["git", "push", "origin", "HEAD"], cwd=tmpdir, capture=False)

        image_uri = f"ghcr.io/{owner}/{repo}:latest"
        summary_lines = [
            "Repository Created",
            "",
            f"Repository: https://github.com/{owner}/{repo}",
            f"Image:      {image_uri}",
            "",
            "GitHub Actions is building your image now.",
            "After the first build finishes, switch with:",
            f"sudo bootc switch {image_uri}",
            f"Track the build: https://github.com/{owner}/{repo}/actions",
        ]
        print(
            self.gum.style(
                *summary_lines,
                align="center",
                width=68,
                margin="1",
                padding="1 2",
                foreground=10,
                border_foreground=10,
                border="double",
            )
        )
        print()
        self.gum.enter_to_continue("Press Enter to return to the main menu...")
        return True

    def select_repo(self, *, require_state_file: bool = False) -> tuple[str, str]:
        if not self.require_github():
            raise SystemExit(1)
        repos = self.gh_json_with_spinner(
            "Fetching repositories from GitHub...",
            ["repo", "list", self.github_user, "--json", "name,description", "--limit", "100"],
        )
        if require_state_file:
            self.gum.hint("Checking which repos were created by this tool...")
            repos = [item for item in repos if self.repo_has_state_file(self.github_user, item["name"])]
        if not repos:
            if require_state_file:
                raise SystemExit("I couldn't find any GitHub repos on your account that were created by this tool yet.")
            raise SystemExit("No repositories found on your GitHub account.")
        labels: list[str] = []
        mapping: dict[str, tuple[str, str]] = {}
        for item in repos:
            description = item.get("description") or "(no description)"
            if len(description) > 40:
                description = description[:37] + "..."
            label = f"{item['name']:<30} {description}"
            labels.append(label)
            mapping[label] = (self.github_user, item["name"])
        manual_label = "Type a repository name manually"
        labels.append(manual_label)
        self.gum.hint("Type to search, then use the arrow keys to move and Enter to choose.")
        self.gum.hint("Choose the last option if you want to type a repository name yourself.")
        print()
        choice = self.gum.filter(labels, height=20, placeholder="Search repos...")
        if choice == manual_label:
            repo = sanitize_slug(self.gum.input(prompt="Repository name: ", placeholder=DEFAULT_REPO_NAME, width=50))
            self.gh_json(["repo", "view", f"{self.github_user}/{repo}", "--json", "name"])
            if require_state_file and not self.repo_has_state_file(self.github_user, repo):
                raise CommandError(
                    f"{self.github_user}/{repo} was not created by this tool. Use 'Import Legacy Repo' first if you want to manage it here."
                )
            return self.github_user, repo
        if choice in mapping:
            return mapping[choice]
        raise SystemExit("No repository selected.")

    def update_existing_image(self) -> None:
        if not self.require_github():
            return
        owner, repo = self.select_repo(require_state_file=True)
        self.config.repo_name = repo
        self.config.github_user = owner
        with tempfile.TemporaryDirectory(prefix="ublue-update.") as tmp:
            tmpdir = Path(tmp)
            self.clone_repo(owner, repo, tmpdir)
            self.load_repo_config(tmpdir)
            self.config.repo_name = repo
            self.config.github_user = owner
            self.config.signing_enabled = self.repo_secret_exists(owner, repo, "SIGNING_SECRET")
            if self.used_legacy_import:
                print()
                self.gum.warn("Imported this repo from legacy generated files instead of a canonical state file.")
                self.gum.hint("Review packages, COPR repos, services, Flatpaks, and removed base packages carefully before pushing changes.")
            if self.update_menu():
                self.show_summary()
                print()
                self.push_update(owner, repo, tmpdir)

    def import_legacy_repo(self) -> None:
        if not self.require_github():
            return
        owner, repo = self.select_repo()
        with tempfile.TemporaryDirectory(prefix="ublue-import.") as tmp:
            tmpdir = Path(tmp)
            self.clone_repo(owner, repo, tmpdir)
            if self.repo_is_builder_managed_dir(tmpdir):
                self.gum.error(f"{owner}/{repo} is already managed by this tool.")
                self.gum.hint("Use 'Update Existing Image' instead.")
                return
            if not self.repo_looks_like_legacy_dir(tmpdir):
                self.gum.error(f"{owner}/{repo} does not look like a repo this tool knows how to import.")
                self.gum.hint("I expected to find either a BlueBuild recipe or a Containerfile with a build script.")
                return
            self.import_legacy_config(tmpdir)
            self.config.repo_name = repo
            self.config.github_user = owner
            self.config.signing_enabled = self.repo_secret_exists(owner, repo, "SIGNING_SECRET")
            print()
            self.gum.warn("This repo was made another way, and this tool is about to take over managing it.")
            self.gum.hint("If you continue, the tool will save its own settings file and rewrite the files it manages.")
            if not self.update_menu():
                return
            self.show_summary()
            print()
            if not self.gum.confirm(f"Let this tool take over {owner}/{repo}?", default=False):
                return
            self.push_update(owner, repo, tmpdir)

    def load_repo_config(self, repo_dir: Path) -> None:
        self.used_legacy_import = False
        state_path = repo_dir / STATE_FILE
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                cfg = config_from_state_payload(data)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CommandError(
                    "This repo's saved builder settings file is missing or broken. "
                    "If you edited it by hand, restore it or import the repo again."
                ) from exc
            self.config = cfg
            self.github_user = cfg.github_user or self.github_user
            return
        self.import_legacy_config(repo_dir)

    def import_legacy_config(self, repo_dir: Path) -> None:
        self.used_legacy_import = True
        if (repo_dir / "recipes/recipe.yml").exists():
            self.config = self.import_legacy_bluebuild(repo_dir)
        elif self.repo_looks_like_legacy_dir(repo_dir):
            self.config = self.import_legacy_containerfile(repo_dir)
        else:
            raise CommandError("Repository does not contain a supported legacy builder layout.")
        if not self.config.repo_name:
            self.config.repo_name = DEFAULT_REPO_NAME
        self.config.github_user = self.github_user
        self.config.normalize()

    def import_legacy_containerfile(self, repo_dir: Path) -> Config:
        cfg = Config(method="containerfile")
        containerfile = repo_dir / "Containerfile"
        if containerfile.exists():
            for line in containerfile.read_text().splitlines():
                if line.startswith("FROM ") and "scratch" not in line:
                    cfg.base_image_uri = line.split()[1]
            matched = self.match_base_image(cfg.base_image_uri)
            if matched:
                cfg.base_image_name = matched.name
                cfg.base_image_tag = matched.tag
            else:
                cfg.base_image_name = cfg.base_image_uri
                cfg.base_image_tag = cfg.base_image_uri.rsplit(":", 1)[-1] if ":" in cfg.base_image_uri else "latest"

        build_sh = repo_dir / "build_files/build.sh"
        if not build_sh.exists():
            build_sh = repo_dir / "build.sh"
        if build_sh.exists():
            lines = build_sh.read_text().splitlines()
            block: list[str] = []
            mode = None
            for raw in lines:
                line = re.sub(r"#.*$", "", raw).strip()
                if not line:
                    continue
                if re.search(r"\b(copr enable)\b", line):
                    match = re.search(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", line)
                    if match:
                        cfg.copr_repos.append(match.group(1))
                if "systemctl enable" in line:
                    cfg.services.append(line.split("systemctl enable", 1)[1].strip())
                if re.search(r"\bdnf5?\s+remove\b", line):
                    mode = "remove"
                    block = [line]
                elif re.search(r"\b(dnf5?|rpm-ostree)\s+install\b", line):
                    mode = "install"
                    block = [line]
                elif mode and (line.endswith("\\") or block and block[-1].endswith("\\")):
                    block.append(line)
                else:
                    mode = None
                if mode and not block[-1].endswith("\\"):
                    tokens = " ".join(item.replace("\\", " ") for item in block).split()
                    if mode == "install":
                        for token in tokens:
                            if token not in {"dnf", "dnf5", "install", "-y", "rpm-ostree"}:
                                cfg.packages.append(token)
                    else:
                        for token in tokens:
                            if token not in {"dnf", "dnf5", "remove", "-y"}:
                                cfg.removed_packages.append(token)
                    mode = None
                    block = []

        workflow = repo_dir / ".github/workflows/build.yml"
        if workflow.exists():
            for line in workflow.read_text().splitlines():
                if line.strip().startswith("IMAGE_DESC:"):
                    cfg.image_desc = line.split(":", 1)[1].strip().strip('"')
                    break
        return cfg

    def import_legacy_bluebuild(self, repo_dir: Path) -> Config:
        cfg = Config(method="bluebuild")
        recipe = repo_dir / "recipes/recipe.yml"
        lines = recipe.read_text().splitlines()
        section = None
        in_flatpaks = False
        in_services = False
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("name:"):
                cfg.repo_name = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("description:"):
                cfg.image_desc = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("base-image:"):
                cfg.base_image_uri = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("image-version:"):
                cfg.base_image_tag = stripped.split(":", 1)[1].strip().strip('"')
            elif stripped.startswith("- type: dnf"):
                section = "dnf"
            elif stripped.startswith("- type: default-flatpaks"):
                section = "flatpak"
                in_flatpaks = False
            elif stripped.startswith("- type: systemd"):
                section = "systemd"
                in_services = False
                in_flatpaks = False
            elif stripped.startswith("packages:") and section == "dnf":
                section = "dnf-packages"
            elif stripped.startswith("copr:") and section == "dnf":
                section = "dnf-copr"
            elif stripped.startswith("install:") and section == "flatpak":
                in_flatpaks = True
            elif stripped.startswith("enabled:") and section == "systemd":
                in_services = True
            elif stripped.startswith("- scope:") and section == "flatpak":
                in_flatpaks = False
            elif stripped.startswith("- "):
                value = stripped[2:].strip().strip('"')
                if section == "dnf-packages":
                    cfg.packages.append(value)
                elif section == "dnf-copr":
                    cfg.copr_repos.append(value)
                elif section == "flatpak" and in_flatpaks:
                    cfg.flatpaks.append(value)
                elif section == "systemd" and in_services:
                    cfg.services.append(value)
        matched = self.match_base_image(cfg.base_image_uri)
        if matched:
            cfg.base_image_uri = matched.image_uri
            cfg.base_image_name = matched.name
            cfg.base_image_tag = matched.tag
        else:
            cfg.base_image_name = cfg.base_image_uri
        return cfg

    def update_menu(self) -> bool:
        while True:
            self.gum.header("Update Image")
            self.gum.hint("Choose a section to review or change.")
            self.gum.hint("Save and push changes when you are finished, or cancel to go back.")
            print()
            mapping: dict[str, str] = {}
            options: list[str] = []
            for title, status in self.update_task_choices():
                label = self.format_task_choice(title, status)
                mapping[label] = title
                options.append(label)
            review_label = "Review current configuration"
            save_label = "Save and push changes"
            cancel_label = "Cancel and go back"
            options.extend([review_label, save_label, cancel_label])
            choice = self.gum.choose(options, height=14)
            selected = choice[0] if choice else cancel_label
            if selected == save_label:
                self.config.normalize()
                return True
            if selected == cancel_label:
                return False
            if selected == review_label:
                self.show_summary()
                self.gum.enter_to_continue("Press Enter to go back to the update menu...")
                continue
            task = mapping[selected]
            if task == "Packages":
                self.manage_packages()
            elif task == "Base image":
                self.config.base_image_uri = ""
                self.choose_base_image()
            elif task == "Description":
                self.edit_description()
            elif task == "COPR repositories":
                self.manage_copr_repos()
            elif task == "Services":
                self.manage_services()
            elif task == "Flatpaks":
                self.manage_flatpaks()
            elif task == "Removed base packages":
                self.manage_removed_packages()

    def manage_packages(self) -> None:
        while True:
            self.gum.header("Edit Packages")
            self.gum.hint("Choose how you want to change packages.")
            self.gum.hint("Choose Back to return to the update menu.")
            print()
            choice = self.gum.choose(
                ["Add packages from catalog", "Add packages manually", "Remove packages", "Back"],
                height=8,
            )
            selected = choice[0] if choice else "Back"
            if selected == "Back":
                return
            if selected == "Add packages from catalog":
                self.select_from_catalog()
            elif selected == "Add packages manually":
                self.manual_packages()
            elif selected == "Remove packages":
                self.config.packages = self.choose_to_remove(self.config.packages, "Remove Packages")

    def manage_copr_repos(self) -> None:
        while True:
            self.gum.header("Edit COPR Repositories")
            self.gum.hint("Choose how you want to change COPR repositories.")
            self.gum.hint("Choose Back to return to the update menu.")
            print()
            choice = self.gum.choose(
                ["Add a COPR repository", "Remove a COPR repository", "Back"],
                height=6,
            )
            selected = choice[0] if choice else "Back"
            if selected == "Back":
                return
            if selected == "Add a COPR repository":
                self.add_copr()
            elif selected == "Remove a COPR repository":
                self.config.copr_repos = self.choose_to_remove(self.config.copr_repos, "Remove COPR Repos")

    def edit_description(self) -> None:
        self.gum.header("Edit Description")
        self.gum.hint("Enter a short description for this image.")
        self.gum.hint("Leave it empty if you want to keep the current description.")
        print()
        value = self.gum.input(prompt="New description: ", value=self.config.image_desc, width=80)
        if value:
            self.config.image_desc = value

    def choose_to_remove(self, values: list[str], header: str) -> list[str]:
        if not values:
            self.gum.warn("Nothing to remove.")
            return values
        self.gum.hint("Move with the arrow keys. Use the help shown at the bottom to mark items to remove.")
        self.gum.hint("Press Enter when you are finished, or leave everything unselected to keep everything.")
        print()
        selected = set(self.gum.choose(values, no_limit=True, height=20, header=header))
        return [value for value in values if value not in selected]

    def manage_services(self) -> None:
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        self.gum.hint("Choose Back to return to the previous menu.")
        print()
        choice = self.gum.choose(["Add services", "Remove services", "Back"], height=5)
        selected = choice[0] if choice else "Back"
        if selected == "Add services":
            self.add_services()
        elif selected == "Remove services":
            self.config.services = self.choose_to_remove(self.config.services, "Remove Services")

    def manage_flatpaks(self) -> None:
        if self.config.method != "bluebuild":
            self.gum.warn("Flatpak management in generated config is only available for BlueBuild.")
            return
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        self.gum.hint("Choose Back to return to the previous menu.")
        print()
        choice = self.gum.choose(["Add Flatpaks", "Remove Flatpaks", "Back"], height=5)
        selected = choice[0] if choice else "Back"
        if selected == "Add Flatpaks":
            self.add_flatpaks()
        elif selected == "Remove Flatpaks":
            self.config.flatpaks = self.choose_to_remove(self.config.flatpaks, "Remove Flatpaks")

    def manage_removed_packages(self) -> None:
        if self.config.method != "containerfile":
            self.gum.warn("Removed base packages are only supported in Containerfile mode.")
            return
        self.gum.hint("Use the arrow keys to move and Enter to choose.")
        self.gum.hint("Choose Back to return to the previous menu.")
        print()
        choice = self.gum.choose(["Add removed base packages", "Remove removed base packages", "Back"], height=5)
        selected = choice[0] if choice else "Back"
        if selected == "Add removed base packages":
            self.gum.hint("Enter one package name per line. Leave this empty if you want to go back.")
            raw = self.gum.write(placeholder="Enter package names, one per line...", height=6, width=60)
            self.config.removed_packages.extend(line.strip() for line in raw.splitlines())
            self.config.normalize()
        elif selected == "Remove removed base packages":
            self.config.removed_packages = self.choose_to_remove(self.config.removed_packages, "Remove Base Package Removals")

    def push_update(self, owner: str, repo: str, repo_dir: Path) -> None:
        self.config.signing_enabled = self.repo_secret_exists(owner, repo, "SIGNING_SECRET")
        self.write_project_files(repo_dir, include_workflow=True)
        diff = run(["git", "diff", "--stat"], cwd=repo_dir, check=False).stdout.strip()
        if not diff:
            diff = run(["git", "status", "--porcelain"], cwd=repo_dir, check=False).stdout.strip()
        if not diff:
            self.gum.warn("No changes detected.")
            return
        print(diff)
        print()
        if self.gum.confirm("View full diff?", default=False):
            full_diff = run(["git", "diff"], cwd=repo_dir, check=False).stdout
            self.gum.pager(full_diff)
        if not self.gum.confirm(f"Push changes to {owner}/{repo}?", default=True):
            return
        run(["git", "add", "-A"], cwd=repo_dir)
        run(["git", "commit", "-m", f"Update image configuration via ublue-builder v{VERSION}"], cwd=repo_dir, check=False)
        run(["git", "push", "origin", "HEAD"], cwd=repo_dir, capture=False)
        self.gum.success(f"Pushed changes to {owner}/{repo}.")
        self.gum.enter_to_continue("Press Enter to return to the main menu...")

    def validate_token_list(self, values: list[str], pattern: re.Pattern[str], label: str) -> None:
        invalid = [value for value in values if not pattern.fullmatch(value)]
        if invalid:
            sample = ", ".join(invalid[:3])
            raise CommandError(f"Invalid {label} value(s): {sample}")

    def validate_config(self) -> None:
        self.config.normalize()
        if self.config.method not in ALLOWED_METHODS:
            raise CommandError("Choose a supported build method before writing project files.")
        if not self.config.base_image_uri or re.search(r"\s", self.config.base_image_uri):
            raise CommandError("Base image URI is missing or invalid.")
        if not self.match_base_image(self.config.base_image_uri):
            raise CommandError("Choose one of the supported base images: Aurora, Aurora DX, Bluefin, Bluefin DX, or Bazzite.")
        self.validate_token_list(self.config.packages, PACKAGE_TOKEN_RE, "package")
        self.validate_token_list(self.config.removed_packages, PACKAGE_TOKEN_RE, "removed package")
        self.validate_token_list(self.config.copr_repos, COPR_REPO_RE, "COPR repository")
        self.validate_token_list(self.config.services, SERVICE_TOKEN_RE, "systemd service")
        self.validate_token_list(self.config.flatpaks, FLATPAK_ID_RE, "Flatpak ID")

    def state_payload(self) -> dict[str, object]:
        self.validate_config()
        payload = asdict(self.config)
        payload["tool_version"] = VERSION
        payload["state_version"] = 1
        return payload

    def render_containerfile(self, existing_text: str | None = None) -> str:
        if existing_text:
            lines = existing_text.splitlines()
            for index, line in enumerate(lines):
                if line.startswith("FROM ") and "scratch" not in line:
                    lines[index] = f"FROM {self.config.base_image_uri}"
                    return ensure_trailing_newline("\n".join(lines))
        return self.generate_containerfile()

    def patch_container_justfile(self, existing_text: str) -> str:
        updated = re.sub(
            r'^export image_name := env\("IMAGE_NAME",\s*"[^"]*"\)(.*)$',
            f'export image_name := env("IMAGE_NAME", "{self.config.repo_name}")\\1',
            existing_text,
            count=1,
            flags=re.MULTILINE,
        )
        return ensure_trailing_newline(updated)

    def patch_container_readme(self, existing_text: str) -> str:
        lines = existing_text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("# "):
                lines[index] = f"# {self.config.repo_name}"
                break
        owner = self.config.github_user or "<username>"
        image_ref = f"ghcr.io/{owner}/{self.config.repo_name}"
        updated = "\n".join(lines).replace("ghcr.io/<username>/<image_name>", image_ref)
        return ensure_trailing_newline(updated)

    def patch_container_workflow(self, existing_text: str) -> str:
        branch_if = "github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        sign_if = f"{branch_if} && env.COSIGN_PRIVATE_KEY != ''"
        lines = existing_text.splitlines()
        output: list[str] = []
        current_step = ""
        inserted_job_env = False
        has_job_env = any(re.fullmatch(r" {4}env:", line) for line in lines)
        has_job_cosign = any(re.fullmatch(r" {6}COSIGN_PRIVATE_KEY: \$\{\{ secrets\.SIGNING_SECRET \}\}", line) for line in lines)
        state_ignore_present = any(STATE_FILE in line for line in lines)
        for line in lines:
            line = pin_action_uses_line(line)
            stripped = line.strip()
            if stripped.startswith("- cron:"):
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}- cron: '{DEFAULT_GITHUB_BUILD_CRON}'")
                continue
            if stripped in {"- '**/README.md'", '- "**/README.md"'} and not state_ignore_present:
                output.append(line)
                output.append(f"{line[: len(line) - len(line.lstrip())]}- '{STATE_FILE}'")
                continue
            if stripped.startswith("IMAGE_DESC:"):
                output.append(f"  IMAGE_DESC: {yaml_scalar(self.config.image_desc)}")
                continue
            if stripped.startswith("- name: "):
                current_step = stripped[len("- name: ") :]
            if stripped == "steps:" and not inserted_job_env and not has_job_env and not has_job_cosign:
                output.append("    env:")
                output.append("      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}")
                inserted_job_env = True
            if current_step in {"Install Cosign", "Sign container image"} and stripped.startswith("if: ") and branch_if in stripped:
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}if: {sign_if}")
                continue
            output.append(line)
        return ensure_trailing_newline("\n".join(output))

    def patch_bluebuild_readme(self, existing_text: str) -> str:
        lines = existing_text.splitlines()
        if lines and lines[0].startswith("# "):
            badge = ""
            if "&nbsp;" in lines[0]:
                badge = lines[0][lines[0].find("&nbsp;") :]
            lines[0] = f"# {self.config.repo_name}{badge}"
        owner = self.config.github_user or "<username>"
        repo_ref = f"{owner}/{self.config.repo_name}"
        image_ref = f"ghcr.io/{repo_ref}"
        updated = "\n".join(lines)
        updated = updated.replace("blue-build/template", repo_ref)
        updated = updated.replace("ghcr.io/blue-build/template", image_ref)
        if not self.config.signing_enabled:
            updated = updated.replace(
                "- First rebase to the unsigned image, to get the proper signing keys and policies installed:",
                "- Rebase to the latest image:",
            )
            updated = re.sub(
                r"- Then rebase to the signed image, like so:\n"
                r"  ```\n"
                r".*?\n"
                r"  ```\n"
                r"- Reboot again to complete the installation\n"
                r"  ```\n"
                r"  systemctl reboot\n"
                r"  ```",
                "- Image signing is not configured for this repository yet, so stay on the unsigned image reference above.",
                updated,
                count=1,
                flags=re.DOTALL,
            )
            marker = "\n## Verification\n"
            if marker in updated:
                updated = updated.split(marker, 1)[0].rstrip()
                updated += (
                    "\n\n## Verification\n\n"
                    "Image signing is not configured for this repository yet.\n"
                    "Configure `SIGNING_SECRET` and rerun the tool if you want signed BlueBuild images.\n"
                )
        return ensure_trailing_newline(updated)

    def patch_bluebuild_workflow(self, existing_text: str) -> str:
        lines = existing_text.splitlines()
        output: list[str] = []
        state_ignore_present = any(STATE_FILE in line for line in lines)
        cosign_line_present = any("cosign_private_key:" in line for line in lines)
        pending_schedule_multiline = False
        for line in lines:
            line = pin_action_uses_line(line)
            stripped = line.strip()
            if stripped.startswith('- cron: "') or stripped.startswith("- cron: '"):
                indent = line[: len(line) - len(line.lstrip())]
                quote = '"' if '"' in stripped else "'"
                output.append(f"{indent}- cron: {quote}{DEFAULT_GITHUB_BUILD_CRON}{quote}")
                continue
            if stripped == "- cron:":
                output.append(line)
                pending_schedule_multiline = True
                continue
            if pending_schedule_multiline and (stripped.startswith('"') or stripped.startswith("'")):
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f'{indent}"{DEFAULT_GITHUB_BUILD_CRON}"')
                pending_schedule_multiline = False
                continue
            if stripped.startswith("- ") and "minutes after last ublue images start building" in stripped:
                continue
            if stripped.startswith('- "**.md"') and not state_ignore_present:
                output.append(line)
                output.append(f'{line[: len(line) - len(line.lstrip())]}- "{STATE_FILE}"')
                continue
            if stripped.startswith("cosign_private_key:"):
                if self.config.signing_enabled:
                    output.append(line)
                continue
            if stripped in {"- recipe.yml", "- recipes/recipe.yml"}:
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}- recipe.yml")
                continue
            output.append(line)
        if self.config.signing_enabled and not cosign_line_present:
            updated_output: list[str] = []
            inserted = False
            for line in output:
                updated_output.append(line)
                if line.strip() == "recipe: ${{ matrix.recipe }}" and not inserted:
                    indent = line[: len(line) - len(line.lstrip())]
                    updated_output.append(f"{indent}cosign_private_key: ${{{{ secrets.SIGNING_SECRET }}}}")
                    inserted = True
            output = updated_output
        return ensure_trailing_newline("\n".join(output))

    def write_bluebuild_project_files(self, base_dir: Path, *, include_workflow: bool) -> None:
        readme_path = base_dir / "README.md"
        gitignore_path = base_dir / ".gitignore"
        recipe_path = base_dir / "recipes/recipe.yml"
        workflow_path = base_dir / ".github/workflows/build.yml"
        files_system_dir = base_dir / "files/system"
        modules_dir = base_dir / "modules"
        cosign_pub_path = base_dir / "cosign.pub"

        if readme_path.exists():
            readme_path.write_text(self.patch_bluebuild_readme(readme_path.read_text()))
        else:
            readme_path.write_text(self.generate_readme())

        existing_gitignore = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
        for entry in ["cosign.key", "cosign.private"]:
            if entry not in existing_gitignore:
                existing_gitignore.append(entry)
        gitignore_path.write_text(ensure_trailing_newline("\n".join(existing_gitignore)))

        recipe_path.parent.mkdir(parents=True, exist_ok=True)
        files_system_dir.mkdir(parents=True, exist_ok=True)
        modules_dir.mkdir(parents=True, exist_ok=True)
        (modules_dir / ".gitkeep").touch(exist_ok=True)
        recipe_path.write_text(self.generate_bluebuild_recipe())

        if include_workflow:
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            if workflow_path.exists():
                workflow_path.write_text(self.patch_bluebuild_workflow(workflow_path.read_text()))
            else:
                workflow_path.write_text(self.generate_bluebuild_workflow())

        if self.generated_cosign_pub:
            cosign_pub_path.write_text(ensure_trailing_newline(self.generated_cosign_pub))
        elif not self.config.signing_enabled:
            cosign_pub_path.unlink(missing_ok=True)

    def write_container_project_files(self, base_dir: Path, *, include_workflow: bool) -> None:
        readme_path = base_dir / "README.md"
        gitignore_path = base_dir / ".gitignore"
        justfile_path = base_dir / "Justfile"
        containerfile_path = base_dir / "Containerfile"
        workflow_path = base_dir / ".github/workflows/build.yml"

        if readme_path.exists():
            readme_path.write_text(self.patch_container_readme(readme_path.read_text()))
        else:
            readme_path.write_text(self.generate_readme())

        existing_gitignore = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
        for entry in ["cosign.key", "_build*/", "output/"]:
            if entry not in existing_gitignore:
                existing_gitignore.append(entry)
        gitignore_path.write_text(ensure_trailing_newline("\n".join(existing_gitignore)))

        (base_dir / "build_files").mkdir(parents=True, exist_ok=True)
        existing_containerfile = containerfile_path.read_text() if containerfile_path.exists() else None
        containerfile_path.write_text(self.render_containerfile(existing_containerfile))
        build_sh = base_dir / "build_files/build.sh"
        build_sh.write_text(self.generate_build_sh())
        build_sh.chmod(0o755)

        if justfile_path.exists():
            justfile_path.write_text(self.patch_container_justfile(justfile_path.read_text()))
        else:
            justfile_path.write_text(self.generate_justfile())

        if include_workflow:
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            if workflow_path.exists():
                workflow_path.write_text(self.patch_container_workflow(workflow_path.read_text()))
            else:
                workflow_path.write_text(self.generate_container_workflow())

    def write_project_files(self, base_dir: Path, *, include_workflow: bool) -> None:
        self.validate_config()
        if self.config.method == "bluebuild" and self.config.removed_packages:
            raise CommandError("Removed base packages are only supported in Containerfile mode.")
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / STATE_FILE).write_text(json.dumps(self.state_payload(), indent=2) + "\n")

        if self.config.method == "containerfile":
            self.write_container_project_files(base_dir, include_workflow=include_workflow)
        else:
            self.write_bluebuild_project_files(base_dir, include_workflow=include_workflow)

    def generate_containerfile(self) -> str:
        return textwrap.dedent(
            f"""\
            FROM scratch AS ctx
            COPY build_files /

            FROM {self.config.base_image_uri}

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                --mount=type=cache,dst=/var/cache \\
                --mount=type=cache,dst=/var/log \\
                --mount=type=tmpfs,dst=/tmp \\
                /ctx/build.sh

            RUN bootc container lint
            """
        )

    def generate_build_sh(self) -> str:
        lines = ["#!/bin/bash", "", "set -ouex pipefail", ""]
        if self.config.copr_repos:
            lines.append("# Enable COPR repositories")
            for repo in self.config.copr_repos:
                lines.append(f"dnf5 -y copr enable {shell_quote(repo)}")
            lines.append("")
        if self.config.removed_packages:
            lines.append("# Remove packages from the base image")
            lines.append("dnf5 remove -y \\")
            for index, pkg in enumerate(self.config.removed_packages):
                suffix = " \\" if index < len(self.config.removed_packages) - 1 else ""
                lines.append(f"    {shell_quote(pkg)}{suffix}")
            lines.append("")
        if self.config.packages:
            lines.append("# Install packages")
            lines.append("dnf5 install -y \\")
            for index, pkg in enumerate(self.config.packages):
                suffix = " \\" if index < len(self.config.packages) - 1 else ""
                lines.append(f"    {shell_quote(pkg)}{suffix}")
            lines.append("")
        else:
            lines.extend(["# dnf5 install -y <your-packages-here>", ""])
        if self.config.copr_repos:
            lines.append("# Disable COPRs so they do not persist in the final image")
            for repo in self.config.copr_repos:
                lines.append(f"dnf5 -y copr disable {shell_quote(repo)}")
            lines.append("")
        if self.config.services:
            lines.append("# Enable systemd services")
            for service in self.config.services:
                lines.append(f"systemctl enable {shell_quote(service)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def generate_container_workflow(self) -> str:
        sign_if = "github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && env.COSIGN_PRIVATE_KEY != ''"
        lines = [
            "---",
            "name: Build container image",
            "on:",
            "  pull_request:",
            "  schedule:",
            f"    - cron: '{DEFAULT_GITHUB_BUILD_CRON}'",
            "  push:",
            f"    paths-ignore: ['**/README.md', '{STATE_FILE}']",
            "  workflow_dispatch:",
            "",
            "env:",
            f"  IMAGE_DESC: {yaml_scalar(self.config.image_desc)}",
            '  IMAGE_NAME: "${{ github.event.repository.name }}"',
            '  IMAGE_REGISTRY: "ghcr.io/${{ github.repository_owner }}"',
            '  DEFAULT_TAG: "latest"',
            "",
            "concurrency:",
            "  group: ${{ github.workflow }}-${{ github.ref || github.run_id }}",
            "  cancel-in-progress: true",
            "",
            "jobs:",
            "  build_push:",
            "    runs-on: ubuntu-24.04",
            "    permissions:",
            "      contents: read",
            "      packages: write",
            "      id-token: write",
            "    env:",
            "      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}",
            "    steps:",
            "      - name: Prepare environment",
            "        run: |",
            '          echo "IMAGE_REGISTRY=${IMAGE_REGISTRY,,}" >> $GITHUB_ENV',
            '          echo "IMAGE_NAME=${IMAGE_NAME,,}" >> $GITHUB_ENV',
            "",
            "      - name: Checkout",
            f"        uses: {pinned_action('actions/checkout')}",
            "",
            "      - name: Maximize build space",
            f"        uses: {pinned_action('ublue-os/remove-unwanted-software')}",
            "",
            "      - name: Get current date",
            "        id: date",
            '        run: echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> $GITHUB_OUTPUT',
            "",
            "      - name: Image Metadata",
            f"        uses: {pinned_action('docker/metadata-action')}",
            "        id: metadata",
            "        with:",
            "          tags: |",
            "            type=raw,value=${{ env.DEFAULT_TAG }}",
            "            type=raw,value=${{ env.DEFAULT_TAG }}.{{date 'YYYYMMDD'}}",
            "            type=sha,enable=${{ github.event_name == 'pull_request' }}",
            "          labels: |",
            "            org.opencontainers.image.created=${{ steps.date.outputs.date }}",
            "            org.opencontainers.image.description=${{ env.IMAGE_DESC }}",
            "            org.opencontainers.image.title=${{ env.IMAGE_NAME }}",
            "            containers.bootc=1",
            '          sep-tags: " "',
            "",
            "      - name: Build Image",
            f"        uses: {pinned_action('redhat-actions/buildah-build')}",
            "        with:",
            "          containerfiles: ./Containerfile",
            "          image: ${{ env.IMAGE_NAME }}",
            "          tags: ${{ steps.metadata.outputs.tags }}",
            "          labels: ${{ steps.metadata.outputs.labels }}",
            "          oci: false",
            "",
            "      - name: Login to GHCR",
            f"        uses: {pinned_action('docker/login-action')}",
            "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "        with:",
            "          registry: ghcr.io",
            "          username: ${{ github.actor }}",
            "          password: ${{ secrets.GITHUB_TOKEN }}",
            "",
            "      - name: Push to GHCR",
            f"        uses: {pinned_action('redhat-actions/push-to-registry')}",
            "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "        with:",
            "          registry: ${{ env.IMAGE_REGISTRY }}",
            "          image: ${{ env.IMAGE_NAME }}",
            "          tags: ${{ steps.metadata.outputs.tags }}",
            "          username: ${{ github.actor }}",
            "          password: ${{ github.token }}",
        ]
        if self.config.signing_enabled:
            lines.extend(
                [
                    "",
                    "      - name: Install Cosign",
                    f"        uses: {pinned_action('sigstore/cosign-installer')}",
                    f"        if: {sign_if}",
                    "",
                    "      - name: Sign container image",
                    f"        if: {sign_if}",
                    "        run: |",
                    '          IMAGE_FULL="${{ env.IMAGE_REGISTRY }}/${{ env.IMAGE_NAME }}"',
                    "          for tag in ${{ steps.metadata.outputs.tags }}; do",
                    "            cosign sign -y --key env://COSIGN_PRIVATE_KEY $IMAGE_FULL:$tag",
                    "          done",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def generate_bluebuild_recipe(self) -> str:
        lines = [
            '# yaml-language-server: $schema=https://schema.blue-build.org/recipe-v1.json',
            f"name: {yaml_scalar(self.config.repo_name)}",
            f"description: {yaml_scalar(self.config.image_desc)}",
            "",
            f"base-image: {yaml_scalar(self.config.base_image_uri.rsplit(':', 1)[0])}",
            f"image-version: {yaml_scalar(self.config.base_image_tag)}",
            "",
            "modules:",
            "  - type: files",
            "    files:",
            "      - source: system",
            "        destination: /",
        ]
        if self.config.copr_repos or self.config.packages:
            lines.extend(["", "  - type: dnf"])
            if self.config.copr_repos:
                lines.extend(["    repos:", "      copr:"])
                lines.extend([f"        - {yaml_scalar(repo)}" for repo in self.config.copr_repos])
            if self.config.packages:
                lines.extend(["    install:", "      packages:"])
                lines.extend([f"        - {yaml_scalar(pkg)}" for pkg in self.config.packages])
        if self.config.flatpaks:
            lines.extend(["", "  - type: default-flatpaks", "    configurations:", "      - notify: true", "        scope: system", "        install:"])
            lines.extend([f"          - {yaml_scalar(fp)}" for fp in self.config.flatpaks])
            lines.extend(["      - scope: user"])
        if self.config.services:
            lines.extend(["", "  - type: systemd", "    system:", "      enabled:"])
            lines.extend([f"        - {yaml_scalar(service)}" for service in self.config.services])
        if self.config.removed_packages:
            lines.extend(["", "  # Removed base packages are tracked in the state file and", "  # are only applied by the Containerfile workflow."])
        if self.config.signing_enabled:
            lines.extend(["", "  - type: signing"])
        return "\n".join(lines).rstrip() + "\n"

    def generate_bluebuild_workflow(self) -> str:
        lines = [
            "name: bluebuild",
            "on:",
            '  schedule:',
            f'    - cron: "{DEFAULT_GITHUB_BUILD_CRON}"',
            "  push:",
            f'    paths-ignore: ["**.md", "{STATE_FILE}"]',
            "  pull_request:",
            "  workflow_dispatch:",
            "",
            "concurrency:",
            "  group: ${{ github.workflow }}-${{ github.ref || github.run_id }}",
            "  cancel-in-progress: true",
            "",
            "jobs:",
            "  bluebuild:",
            "    runs-on: ubuntu-latest",
            "    permissions:",
            "      contents: read",
            "      packages: write",
            "      id-token: write",
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
            "        recipe:",
            "          - recipe.yml",
            "    steps:",
            "      - name: Build Custom Image",
            f"        uses: {pinned_action('blue-build/github-action')}",
            "        with:",
            "          recipe: ${{ matrix.recipe }}",
        ]
        if self.config.signing_enabled:
            lines.append("          cosign_private_key: ${{ secrets.SIGNING_SECRET }}")
        lines.extend(
            [
                "          registry_token: ${{ github.token }}",
                "          pr_event_number: ${{ github.event.number }}",
                "          maximize_build_space: true",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def generate_readme(self) -> str:
        method_label = "BlueBuild (recipe.yml)" if self.config.method == "bluebuild" else "Containerfile"
        owner = self.config.github_user or "your-user"
        image_ref = f"ghcr.io/{owner}/{self.config.repo_name}:latest"
        packages = "\n".join(f"- `{pkg}`" for pkg in self.config.packages) or "- _(customize later)_"
        if self.config.github_user:
            usage_block = textwrap.dedent(
                f"""\
                ## Usage

                ```bash
                sudo bootc switch {image_ref}
                systemctl reboot
                ```
                """
            ).strip()
            if self.config.method == "containerfile":
                local_block = textwrap.dedent(
                    f"""\
                    ## Local Build

                    ```bash
                    git clone https://github.com/{owner}/{self.config.repo_name}
                    cd {self.config.repo_name}
                    just build
                    ```
                    """
                ).strip()
            else:
                local_block = textwrap.dedent(
                    """\
                    ## Local Build

                    Local BlueBuild builds are not automated by this tool.
                    Push this repo to GitHub and use the generated workflow, or use the BlueBuild CLI directly.
                    """
                ).strip()
        else:
            usage_block = ""
            if self.config.method == "containerfile":
                local_block = textwrap.dedent(
                    f"""\
                    ## Local Build

                    ```bash
                    cd {self.config.repo_name}
                    just build
                    ```
                    """
                ).strip()
            else:
                local_block = textwrap.dedent(
                    """\
                    ## Local Build

                    This project was created locally in BlueBuild mode.
                    Use the BlueBuild CLI directly or push it to GitHub and use the generated workflow there.
                    """
                ).strip()
        sections = [
            f"# {self.config.repo_name}",
            "",
            f"Custom Universal Blue image built with **{method_label}**.",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Base | `{self.config.base_image_uri}` |",
            f"| Method | {method_label} |",
            "",
            "## Installed Packages",
            "",
            packages,
        ]
        if usage_block:
            sections.extend(["", usage_block])
        sections.extend(["", local_block])
        return "\n".join(sections).rstrip() + "\n"

    def generate_justfile(self) -> str:
        return textwrap.dedent(
            f"""\
            export image_name := env("IMAGE_NAME", "{self.config.repo_name}")
            export default_tag := env("DEFAULT_TAG", "latest")

            [private]
            default:
                @just --list

            build $target_image=image_name $tag=default_tag:
                #!/usr/bin/env bash
                podman build \\
                    --pull=newer \\
                    --tag "${{target_image}}:${{tag}}" \\
                    .
            """
        )

    def run_main(self) -> None:
        self.clear()
        self.banner()
        self.preflight()
        self.main_menu()


def main() -> None:
    app = App()
    try:
        app.run_main()
    except UserQuit:
        print()
        raise SystemExit(0)
    except CommandError as exc:
        app.gum.error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
