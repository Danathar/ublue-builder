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
SERVICE_NAME = "ublue-local-build"
TIMER_DIR = Path.home() / ".config/systemd/user"
DEFAULT_REPO_NAME = "my-ublue-image"
DEFAULT_GITHUB_BUILD_CRON = "05 10 * * *"


@dataclass(frozen=True)
class BaseImage:
    key: str
    name: str
    description: str
    image_uri: str
    tag: str


BASE_IMAGES: tuple[BaseImage, ...] = (
    BaseImage("bazzite", "Bazzite", "Gaming-focused, SteamOS-like experience", "ghcr.io/ublue-os/bazzite:stable", "stable"),
    BaseImage("bazzite-deck", "Bazzite Deck", "Bazzite for Steam Deck / HTPC", "ghcr.io/ublue-os/bazzite-deck:stable", "stable"),
    BaseImage("aurora", "Aurora (KDE)", "KDE Plasma desktop, polished and productive", "ghcr.io/ublue-os/aurora:stable", "stable"),
    BaseImage("aurora-dx", "Aurora DX", "Aurora with developer tools", "ghcr.io/ublue-os/aurora-dx:stable", "stable"),
    BaseImage("bluefin", "Bluefin (GNOME)", "GNOME desktop, clean and opinionated", "ghcr.io/ublue-os/bluefin:stable", "stable"),
    BaseImage("bluefin-dx", "Bluefin DX", "Bluefin with developer tools", "ghcr.io/ublue-os/bluefin-dx:stable", "stable"),
    BaseImage("base-main", "Universal Blue Base", "Minimal base with codecs + RPMFusion", "ghcr.io/ublue-os/base-main:latest", "latest"),
    BaseImage("silverblue", "Silverblue (GNOME base)", "Universal Blue Silverblue", "ghcr.io/ublue-os/silverblue-main:latest", "latest"),
    BaseImage("kinoite", "Kinoite (KDE base)", "Universal Blue Kinoite", "ghcr.io/ublue-os/kinoite-main:latest", "latest"),
    BaseImage("fedora-bootc", "Fedora Bootc 42", "Vanilla Fedora bootc, no uBlue additions", "quay.io/fedora/fedora-bootc:42", "42"),
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


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


class CommandError(RuntimeError):
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

    def header(self, title: str) -> None:
        print()
        print(self.style(f"━━━  {title}  ━━━", foreground=117, bold=True))
        print()

    def hint(self, message: str) -> None:
        print(self.style(message, faint=True, width=64))

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        args = ["gum", "confirm", prompt]
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
        args = ["gum", "input", "--prompt", prompt]
        if value is not None:
            args.extend(["--value", value])
        if placeholder is not None:
            args.extend(["--placeholder", placeholder])
        if width is not None:
            args.extend(["--width", str(width)])
        return run(args, check=False).stdout.rstrip("\n")

    def write(self, *, placeholder: str, height: int, width: int) -> str:
        return run(
            ["gum", "write", "--placeholder", placeholder, "--height", str(height), "--width", str(width)],
            check=False,
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
        args = ["gum", "choose", "--height", str(height)]
        if no_limit:
            args.append("--no-limit")
        if selected:
            args.extend(["--selected", ",".join(selected)])
        if header:
            args.extend(["--header", header])
        proc = run(args, check=False, stdin="\n".join(options) + "\n")
        output = proc.stdout.strip("\n")
        return [line for line in output.splitlines() if line]

    def filter(self, options: Sequence[str], *, height: int = 20, placeholder: str = "Search...") -> str:
        proc = run(
            ["gum", "filter", "--height", str(height), "--placeholder", placeholder],
            check=False,
            stdin="\n".join(options) + "\n",
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
        run(["gum", "input", "--placeholder", placeholder], check=False, capture=False)


class App:
    def __init__(self) -> None:
        self.gum = Gum()
        self.config = Config()
        self.github_available = False
        self.github_user = ""
        self.used_legacy_import = False

    def banner(self) -> None:
        print(
            self.gum.style(
                f"Universal Blue Custom Image Builder  v{VERSION}",
                "",
                "Build custom OCI images from Universal Blue base images.",
                "Python rewrite with canonical JSON state and gum UI.",
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
        if sys.stdout.isatty() and os.environ.get("TERM"):
            run(["clear"], capture=False, check=False)

    def gh_json(self, args: Sequence[str]) -> object:
        proc = run(["gh", *args])
        return json.loads(proc.stdout or "null")

    def gh_json_with_spinner(self, title: str, args: Sequence[str]) -> object:
        output = self.gum.spinner_capture(title, ["gh", *args])
        return json.loads(output or "null")

    def preflight(self) -> None:
        self.gum.ensure_available()
        self.gum.header("Preflight Checks")
        self.gum.hint("Checking required tools and the runtime environment...")
        print()

        if not command_exists("git"):
            raise SystemExit("git is required. Install it with: brew install git")
        self.gum.success("git found")

        if command_exists("podman"):
            self.gum.success("podman found (local builds available)")
        else:
            self.gum.warn("podman not found (local builds unavailable)")

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
            action = self.gum.choose(
                [
                    "Create New Image",
                    "Scan OS & Migrate Layered Packages",
                    "Update Existing Image",
                    "Build & Install Locally",
                    "Set Up Nightly Local Build",
                    "Quit",
                ],
                height=10,
            )
            selected = action[0] if action else "Quit"
            if selected == "Quit":
                raise SystemExit(0)
            if selected == "Create New Image":
                self.create_new_image()
                return
            if selected == "Scan OS & Migrate Layered Packages":
                if self.scan_os():
                    self.create_new_image(scanned=True)
                return
            if selected == "Update Existing Image":
                self.update_existing_image()
                return
            if selected == "Build & Install Locally":
                self.local_build_image()
                return
            if selected == "Set Up Nightly Local Build":
                self.setup_nightly_build()
                return

    def create_new_image(self, *, scanned: bool = False) -> None:
        if scanned and self.config.scanned_removed:
            self.gum.warn("Removed base packages were detected and will only be applied in Containerfile mode.")
        self.choose_method()
        self.choose_base_image()
        self.configure_repo()
        self.select_packages()
        self.show_summary()
        print()
        if self.gum.confirm("Push to GitHub and trigger a build?", default=True):
            self.do_build()

    def choose_method(self) -> None:
        self.gum.header("Build Method")
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

    def choose_base_image(self) -> None:
        self.gum.header("Base Image")
        if self.config.base_image_uri:
            print(f"  Detected base image: {self.gum.style(self.config.base_image_name or self.config.base_image_uri, bold=True)}")
            print(f"  Image: {self.gum.style(self.config.base_image_uri, foreground=117)}")
            print()
            if self.gum.confirm("Use this base image?", default=True):
                return

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

    def configure_repo(self) -> None:
        self.gum.header("Repository Configuration")
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

    def select_packages(self) -> None:
        self.gum.header("Software Selection")
        while True:
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
        options = list(CATALOGS) + ["Back"]
        choice = self.gum.choose(options, height=10)
        selected = choice[0] if choice else "Back"
        if selected == "Back":
            return
        current = set(self.config.packages)
        picked = self.gum.choose(
            CATALOGS[selected],
            height=20,
            no_limit=True,
            selected=[pkg for pkg in CATALOGS[selected] if pkg in current],
            header=selected,
        )
        self.config.packages.extend(picked)
        self.config.normalize()
        if picked:
            self.gum.success(f"Added {len(picked)} packages from {selected}")

    def manual_packages(self) -> None:
        print()
        self.gum.hint("Enter RPM package names separated by spaces or newlines.")
        print()
        raw = self.gum.write(placeholder="Enter package names...", height=6, width=70)
        self.config.packages.extend(token.strip(",") for token in raw.split())
        self.config.normalize()
        self.gum.success(f"Total packages configured: {len(self.config.packages)}")

    def add_copr(self) -> None:
        print()
        repo = self.gum.input(prompt="COPR repo: ", placeholder="owner/project", width=50)
        if not repo or "/" not in repo:
            return
        self.config.copr_repos.append(repo.strip())
        pkgs = self.gum.input(prompt="Packages: ", placeholder="package1 package2", width=60)
        self.config.packages.extend(pkg.strip(",") for pkg in pkgs.split())
        self.config.normalize()
        self.gum.success(f"Added COPR: {repo}")

    def add_services(self) -> None:
        print()
        raw = self.gum.write(placeholder="Enter service names, one per line...", height=5, width=50)
        self.config.services.extend(line.strip() for line in raw.splitlines())
        self.config.normalize()
        self.gum.success(f"Total services configured: {len(self.config.services)}")

    def add_flatpaks(self) -> None:
        if self.config.method != "bluebuild":
            self.gum.warn("Flatpaks in generated config are only supported in BlueBuild mode.")
            return
        raw = self.gum.write(placeholder="Enter flatpak IDs, one per line...", height=5, width=60)
        self.config.flatpaks.extend(line.strip() for line in raw.splitlines())
        self.config.normalize()
        self.gum.success(f"Total flatpaks configured: {len(self.config.flatpaks)}")

    def view_selections(self) -> None:
        self.gum.header("Current Selections")
        rows = [
            ("Packages", ", ".join(self.config.packages) or "(none)"),
            ("COPR Repos", ", ".join(self.config.copr_repos) or "(none)"),
            ("Services", ", ".join(self.config.services) or "(none)"),
            ("Flatpaks", ", ".join(self.config.flatpaks) or "(none)"),
            ("Removed Base Packages", ", ".join(self.config.removed_packages) or "(none)"),
        ]
        self.gum.table(rows, columns="Setting,Value", widths="20,60")
        print()
        self.gum.enter_to_continue()

    def show_summary(self) -> None:
        self.gum.header("Review Build Configuration")
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
            if value == image.image_uri or value.startswith(image.image_uri.split(":")[0]):
                return image
        return None

    def repo_secret_exists(self, owner: str, repo: str, secret_name: str) -> bool:
        if not command_exists("gh"):
            return False
        proc = run(["gh", "secret", "list", "-R", f"{owner}/{repo}"], check=False)
        if proc.returncode != 0:
            return False
        return any(line.split()[0] == secret_name for line in proc.stdout.splitlines() if line.strip())

    def maybe_enable_signing(self, owner: str, repo: str) -> bool:
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
            if proc.returncode != 0 or not key_path.exists():
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
        self.gum.success("Configured SIGNING_SECRET for image signing.")
        return True

    def clone_repo(self, owner: str, repo: str, target: Path) -> None:
        self.gum.spinner(f"Cloning {owner}/{repo}...", ["gh", "repo", "clone", f"{owner}/{repo}", str(target)])

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
        return branch or "main"

    def do_build(self) -> None:
        if not self.require_github():
            return
        owner = self.github_user
        repo = self.config.repo_name
        self.config.github_user = owner
        self.gum.header("Building Image")
        exists = run(["gh", "repo", "view", f"{owner}/{repo}", "--json", "name"], check=False).returncode == 0
        if not exists:
            self.gum.spinner(
                f"Creating {owner}/{repo}...",
                ["gh", "repo", "create", repo, "--description", self.config.image_desc, "--public"],
            )
        self.config.signing_enabled = self.maybe_enable_signing(owner, repo)

        with tempfile.TemporaryDirectory(prefix="ublue-builder.") as tmp:
            tmpdir = Path(tmp)
            self.clone_repo(owner, repo, tmpdir)
            self.write_project_files(tmpdir, include_workflow=True)
            run(["git", "add", "-A"], cwd=tmpdir)
            run(["git", "commit", "-m", "Initial image configuration via ublue-builder"], cwd=tmpdir, check=False)
            run(["git", "push", "origin", "HEAD"], cwd=tmpdir, capture=False)

        run(
            ["gh", "api", "-X", "PUT", f"/repos/{owner}/{repo}/actions/permissions", "-f", "enabled=true", "-f", "allowed_actions=all"],
            check=False,
        )
        image_uri = f"ghcr.io/{owner}/{repo}:latest"
        summary_lines = [
            "Build Complete",
            "",
            f"Repository: https://github.com/{owner}/{repo}",
            f"Image:      {image_uri}",
            "",
            f"Switch to your image: sudo bootc switch {image_uri}",
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

    def select_repo(self) -> tuple[str, str]:
        if not self.require_github():
            raise SystemExit(1)
        repos = self.gh_json_with_spinner(
            "Fetching repositories from GitHub...",
            ["repo", "list", self.github_user, "--json", "name,description", "--limit", "100"],
        )
        if not repos:
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
        choice = self.gum.filter(labels, height=20, placeholder="Search repos...")
        if choice == manual_label:
            repo = sanitize_slug(self.gum.input(prompt="Repository name: ", placeholder=DEFAULT_REPO_NAME, width=50))
            self.gh_json(["repo", "view", f"{self.github_user}/{repo}", "--json", "name"])
            return self.github_user, repo
        if choice in mapping:
            return mapping[choice]
        raise SystemExit("No repository selected.")

    def update_existing_image(self) -> None:
        if not self.require_github():
            return
        owner, repo = self.select_repo()
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
            self.show_summary()
            self.gum.enter_to_continue("Press Enter to continue to the update menu...")
            self.update_menu()
            self.show_summary()
            print()
            self.push_update(owner, repo, tmpdir)

    def load_repo_config(self, repo_dir: Path) -> None:
        self.used_legacy_import = False
        state_path = repo_dir / STATE_FILE
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CommandError(f"Unable to read {STATE_FILE}: {exc}") from exc
            cfg.normalize()
            self.config = cfg
            self.github_user = cfg.github_user or self.github_user
            return
        self.import_legacy_config(repo_dir)

    def import_legacy_config(self, repo_dir: Path) -> None:
        self.used_legacy_import = True
        if (repo_dir / "recipes/recipe.yml").exists():
            self.config = self.import_legacy_bluebuild(repo_dir)
        else:
            self.config = self.import_legacy_containerfile(repo_dir)
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
            elif stripped.startswith("packages:") and section == "dnf":
                section = "dnf-packages"
            elif stripped.startswith("copr:") and section == "dnf":
                section = "dnf-copr"
            elif stripped.startswith("install:") and section == "flatpak":
                in_flatpaks = True
            elif stripped.startswith("enabled:") and section == "systemd":
                in_services = True
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

    def update_menu(self) -> None:
        while True:
            self.gum.header("Update Image")
            choice = self.gum.choose(
                [
                    "Add packages from catalog",
                    "Add packages manually",
                    "Remove packages",
                    "Change base image",
                    "Change description",
                    "Add a COPR repository",
                    "Remove a COPR repository",
                    "Manage systemd services",
                    "Manage Flatpaks",
                    "Manage removed base packages",
                    "View current configuration",
                    "Done",
                ],
                height=14,
            )
            selected = choice[0] if choice else "Done"
            if selected == "Done":
                self.config.normalize()
                return
            if selected == "Add packages from catalog":
                self.select_from_catalog()
            elif selected == "Add packages manually":
                self.manual_packages()
            elif selected == "Remove packages":
                self.config.packages = self.choose_to_remove(self.config.packages, "Remove Packages")
            elif selected == "Change base image":
                self.config.base_image_uri = ""
                self.choose_base_image()
            elif selected == "Change description":
                value = self.gum.input(prompt="New description: ", value=self.config.image_desc, width=80)
                if value:
                    self.config.image_desc = value
            elif selected == "Add a COPR repository":
                self.add_copr()
            elif selected == "Remove a COPR repository":
                self.config.copr_repos = self.choose_to_remove(self.config.copr_repos, "Remove COPR Repos")
            elif selected == "Manage systemd services":
                self.manage_services()
            elif selected == "Manage Flatpaks":
                self.manage_flatpaks()
            elif selected == "Manage removed base packages":
                self.manage_removed_packages()
            elif selected == "View current configuration":
                self.show_summary()
                self.gum.enter_to_continue()

    def choose_to_remove(self, values: list[str], header: str) -> list[str]:
        if not values:
            self.gum.warn("Nothing to remove.")
            return values
        selected = set(self.gum.choose(values, no_limit=True, height=20, header=header))
        return [value for value in values if value not in selected]

    def manage_services(self) -> None:
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
        choice = self.gum.choose(["Add removed base packages", "Remove removed base packages", "Back"], height=5)
        selected = choice[0] if choice else "Back"
        if selected == "Add removed base packages":
            raw = self.gum.write(placeholder="Enter package names, one per line...", height=6, width=60)
            self.config.removed_packages.extend(line.strip() for line in raw.splitlines())
            self.config.normalize()
        elif selected == "Remove removed base packages":
            self.config.removed_packages = self.choose_to_remove(self.config.removed_packages, "Remove Base Package Removals")

    def push_update(self, owner: str, repo: str, repo_dir: Path) -> None:
        self.config.signing_enabled = self.repo_secret_exists(owner, repo, "SIGNING_SECRET") or self.maybe_enable_signing(owner, repo)
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
    def local_build_image(self) -> None:
        self.gum.header("Build Image Locally")
        if not command_exists("podman"):
            self.gum.error("Podman is required for local builds. Install it with: brew install podman")
            return
        bootc_available = command_exists("bootc")

        choice = self.gum.choose(
            [
                "Use an existing local project directory",
                "Clone a GitHub repository to build locally",
                "Create a new image config to build locally",
                "Back",
            ],
            height=5,
        )
        selected = choice[0] if choice else "Back"
        local_dir: Path | None = None
        if selected == "Use an existing local project directory":
            path = self.gum.input(prompt="Path to project: ", placeholder=str(Path.home() / "my-ublue-image"), width=60)
            local_dir = Path(path).expanduser()
            if not local_dir.is_dir():
                self.gum.error(f"Directory not found: {local_dir}")
                return
        elif selected == "Clone a GitHub repository to build locally":
            owner, repo = self.select_repo()
            local_dir = Path.home() / repo
            if local_dir.exists():
                if not (local_dir / ".git").is_dir():
                    raise CommandError(f"{local_dir} already exists but is not a git repository.")
                branch = self.current_branch(local_dir)
                run(["git", "pull", "origin", branch], cwd=local_dir, check=False, capture=False)
            else:
                self.clone_repo(owner, repo, local_dir)
        elif selected == "Create a new image config to build locally":
            self.config = Config()
            self.choose_method()
            self.choose_base_image()
            self.config.repo_name = sanitize_slug(
                self.gum.input(prompt="Project name: ", value=DEFAULT_REPO_NAME, placeholder=DEFAULT_REPO_NAME, width=60)
            )
            self.config.image_desc = self.gum.input(
                prompt="Description: ",
                value=self.config.image_desc,
                placeholder="Description",
                width=80,
            ) or self.config.image_desc
            self.select_packages()
            self.show_summary()
            local_dir = Path.home() / self.config.repo_name
            local_dir.mkdir(parents=True, exist_ok=True)
            self.write_project_files(local_dir, include_workflow=False)
        else:
            return

        assert local_dir is not None
        containerfile = local_dir / "Containerfile"
        if not containerfile.exists():
            containerfile = local_dir / "Dockerfile"
        if not containerfile.exists():
            recipe = local_dir / "recipes/recipe.yml"
            if recipe.exists():
                self.gum.error("BlueBuild local builds need the bluebuild CLI. This tool currently only automates local Containerfile builds.")
            else:
                self.gum.error(f"No Containerfile found in {local_dir}")
            return

        image_name = sanitize_slug(local_dir.name, local_dir.name)
        print()
        print(self.gum.style("Building... (this may take a while)", foreground=117, bold=True))
        print()
        proc = run(
            ["podman", "build", "--pull=newer", "--tag", f"localhost/{image_name}:latest", "-f", str(containerfile), str(local_dir)],
            check=False,
            capture=False,
        )
        if proc.returncode != 0:
            self.gum.error("Build failed.")
            return
        self.gum.success(f"Built localhost/{image_name}:latest")
        if not bootc_available:
            print()
            self.gum.warn("bootc is not installed on this host. Install/stage actions are unavailable.")
            return

        action = self.gum.choose(
            [
                "Install now (bootc switch + reboot)",
                "Stage for next boot (bootc switch, reboot later)",
                "Skip",
            ],
            height=5,
        )
        selected = action[0] if action else "Skip"
        if selected.startswith("Install now"):
            run(["sudo", "bootc", "switch", "--transport", "containers-storage", f"localhost/{image_name}:latest"], capture=False)
            run(["sudo", "systemctl", "reboot"], check=False, capture=False)
        elif selected.startswith("Stage for next boot"):
            run(["sudo", "bootc", "switch", "--transport", "containers-storage", f"localhost/{image_name}:latest"], capture=False)

    def systemd_shell_command(self, script: str) -> str:
        return f"/bin/bash -lc {shlex.quote(script)}"

    def setup_nightly_build(self) -> None:
        self.gum.header("Nightly Local Build")
        if not command_exists("podman"):
            self.gum.error("Podman is required for nightly builds.")
            return
        bootc_available = command_exists("bootc")
        project_dir = Path(
            self.gum.input(prompt="Project directory: ", placeholder=str(Path.home() / "my-ublue-image"), width=60)
        ).expanduser()
        if not project_dir.is_dir():
            self.gum.error(f"Directory not found: {project_dir}")
            return
        containerfile = project_dir / "Containerfile"
        if not containerfile.exists():
            containerfile = project_dir / "Dockerfile"
        if not containerfile.exists():
            self.gum.error("Nightly builds currently require a Containerfile or Dockerfile project.")
            return

        hour = self.gum.input(prompt="Build hour (0-23): ", value="3", width=30) or "3"
        if not hour.isdigit() or int(hour) > 23:
            hour = "3"
        auto_stage = False
        if bootc_available:
            auto_stage = self.gum.confirm("Automatically stage the image for next boot after building?", default=True)
        else:
            self.gum.warn("bootc is not installed on this host. Nightly builds can rebuild locally, but they cannot stage the image.")
        auto_pull = (project_dir / ".git").is_dir() and self.gum.confirm("Pull latest git changes before each build?", default=True)
        image_name = sanitize_slug(project_dir.name, project_dir.name)

        TIMER_DIR.mkdir(parents=True, exist_ok=True)
        service_path = TIMER_DIR / f"{SERVICE_NAME}.service"
        timer_path = TIMER_DIR / f"{SERVICE_NAME}.timer"

        build_script = []
        if auto_pull:
            build_script.append(f"git -C {shlex.quote(str(project_dir))} pull origin $(git -C {shlex.quote(str(project_dir))} branch --show-current)")
        build_script.append(
            f"podman build --pull=newer --tag localhost/{image_name}:latest -f {shlex.quote(str(containerfile))} {shlex.quote(str(project_dir))}"
        )
        if auto_stage:
            build_script.append(
                f"sudo -n /usr/bin/bootc switch --transport containers-storage localhost/{image_name}:latest"
            )
        service_body = textwrap.dedent(
            f"""\
            [Unit]
            Description=Universal Blue Local Image Builder
            Wants=network-online.target
            After=network-online.target

            [Service]
            Type=oneshot
            ExecStart={self.systemd_shell_command(' && '.join(build_script))}

            [Install]
            WantedBy=default.target
            """
        )
        timer_body = textwrap.dedent(
            f"""\
            [Unit]
            Description=Nightly Universal Blue image build

            [Timer]
            OnCalendar=*-*-* {hour}:00:00
            Persistent=true
            RandomizedDelaySec=900

            [Install]
            WantedBy=timers.target
            """
        )
        service_path.write_text(service_body)
        timer_path.write_text(timer_body)
        run(["systemctl", "--user", "daemon-reload"], capture=False)
        run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"], capture=False)
        if auto_stage:
            self.gum.warn("Auto-stage uses 'sudo -n'; configure passwordless sudo for /usr/bin/bootc or the stage step will fail.")
        self.gum.success("Nightly build timer created.")

    def state_payload(self) -> dict[str, object]:
        self.config.normalize()
        payload = asdict(self.config)
        payload["tool_version"] = VERSION
        payload["state_version"] = 1
        return payload

    def write_project_files(self, base_dir: Path, *, include_workflow: bool) -> None:
        if self.config.method == "bluebuild" and self.config.removed_packages:
            raise CommandError("Removed base packages are only supported in Containerfile mode.")
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / STATE_FILE).write_text(json.dumps(self.state_payload(), indent=2) + "\n")
        (base_dir / "README.md").write_text(self.generate_readme())
        (base_dir / ".gitignore").write_text("cosign.key\n_build*/\noutput/\n")

        if self.config.method == "containerfile":
            (base_dir / "build_files").mkdir(parents=True, exist_ok=True)
            (base_dir / "Containerfile").write_text(self.generate_containerfile())
            build_sh = base_dir / "build_files/build.sh"
            build_sh.write_text(self.generate_build_sh())
            build_sh.chmod(0o755)
            (base_dir / "Justfile").write_text(self.generate_justfile())
            if include_workflow:
                workflow_dir = base_dir / ".github/workflows"
                workflow_dir.mkdir(parents=True, exist_ok=True)
                (workflow_dir / "build.yml").write_text(self.generate_container_workflow())
        else:
            (base_dir / "recipes").mkdir(parents=True, exist_ok=True)
            (base_dir / "files/system").mkdir(parents=True, exist_ok=True)
            (base_dir / "modules").mkdir(parents=True, exist_ok=True)
            (base_dir / "files/system/.gitkeep").touch()
            (base_dir / "modules/.gitkeep").touch()
            (base_dir / "recipes/recipe.yml").write_text(self.generate_bluebuild_recipe())
            if include_workflow:
                workflow_dir = base_dir / ".github/workflows"
                workflow_dir.mkdir(parents=True, exist_ok=True)
                (workflow_dir / "build.yml").write_text(self.generate_bluebuild_workflow())

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
                lines.append(f"dnf5 -y copr enable {repo}")
            lines.append("")
        if self.config.removed_packages:
            lines.append("# Remove packages from the base image")
            lines.append("dnf5 remove -y \\")
            for index, pkg in enumerate(self.config.removed_packages):
                suffix = " \\" if index < len(self.config.removed_packages) - 1 else ""
                lines.append(f"    {pkg}{suffix}")
            lines.append("")
        if self.config.packages:
            lines.append("# Install packages")
            lines.append("dnf5 install -y \\")
            for index, pkg in enumerate(self.config.packages):
                suffix = " \\" if index < len(self.config.packages) - 1 else ""
                lines.append(f"    {pkg}{suffix}")
            lines.append("")
        else:
            lines.extend(["# dnf5 install -y <your-packages-here>", ""])
        if self.config.copr_repos:
            lines.append("# Disable COPRs so they do not persist in the final image")
            for repo in self.config.copr_repos:
                lines.append(f"dnf5 -y copr disable {repo}")
            lines.append("")
        if self.config.services:
            lines.append("# Enable systemd services")
            for service in self.config.services:
                lines.append(f"systemctl enable {service}")
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
            "        uses: actions/checkout@v4",
            "",
            "      - name: Maximize build space",
            "        uses: ublue-os/remove-unwanted-software@v8",
            "",
            "      - name: Get current date",
            "        id: date",
            '        run: echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> $GITHUB_OUTPUT',
            "",
            "      - name: Image Metadata",
            "        uses: docker/metadata-action@v5",
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
            "        uses: redhat-actions/buildah-build@v2",
            "        with:",
            "          containerfiles: ./Containerfile",
            "          image: ${{ env.IMAGE_NAME }}",
            "          tags: ${{ steps.metadata.outputs.tags }}",
            "          labels: ${{ steps.metadata.outputs.labels }}",
            "          oci: false",
            "",
            "      - name: Login to GHCR",
            "        uses: docker/login-action@v3",
            "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "        with:",
            "          registry: ghcr.io",
            "          username: ${{ github.actor }}",
            "          password: ${{ secrets.GITHUB_TOKEN }}",
            "",
            "      - name: Push to GHCR",
            "        uses: redhat-actions/push-to-registry@v2",
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
                    "        uses: sigstore/cosign-installer@v3",
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
            "          - recipes/recipe.yml",
            "    steps:",
            "      - name: Build Custom Image",
            "        uses: blue-build/github-action@v1.11",
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
    except CommandError as exc:
        app.gum.error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
