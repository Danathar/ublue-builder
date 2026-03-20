# uBlue Builder

Beta terminal tool for creating and updating GitHub-backed Universal Blue image repositories.

This project is a guided terminal app for beginner Bazzite, Aurora, and Bluefin users who want a custom image repo without learning the full upstream template and workflow setup first, though the author still recommends learning how to do all of this by hand anyway ;)

> [!NOTE]
> This project was created with AI assistance and should be treated cautiously.
>
> This is a third-party tool. It is not an official Universal Blue utility and is not sanctioned by the Universal Blue project.
>
> This project is provided as-is, without any promise that it will be safe for your repositories, data, systems, or build pipeline. Use it carefully, review its changes before applying them, and keep backups where appropriate. The maintainer is not responsible for repository damage, data loss, failed builds, system changes, or other consequences that may result from using this software.

## Status

This project is currently **0.8 beta** and is **not fully tested yet**. Use it carefully, review the changes it makes, and do not assume every workflow or edge case has already been exercised.

## What This Is

This tool creates and maintains a GitHub repository that builds a custom Universal Blue image from an official Universal Blue base image.

It currently focuses on the beginner-friendly Containerfile path. Generated repos start from a bundled snapshot of the official `ublue-os/image-template` repository:

https://github.com/ublue-os/image-template

That bundled snapshot may lag behind upstream, though the maintainer aims to keep this utility aligned with the latest version of that template.

## What It Does

- Creates a new public GitHub repo for a custom Universal Blue image
- Writes the repo files needed for a GitHub Actions build
- Lets users add packages, COPR repos, services, and base-package removals
- Updates repos that were previously created by this tool
- Can scan a running rpm-ostree / bootc system and carry layered packages into a new image repo

## Why It Exists

Universal Blue images are powerful, but the normal setup path assumes users are comfortable with image templates, GitHub Actions, signing, and image maintenance.

This project exists to reduce that setup cost for newer users by turning the common path into a guided terminal workflow with stricter defaults and guardrails.

## Who It Is For

This is for:

- beginner and intermediate Universal Blue users
- Bazzite, Aurora, and Bluefin users who want a custom repo on GitHub
- people who want GitHub Actions to build their image automatically

This is not aimed at:

- people who want full BlueBuild support in the same tool
- people who want every advanced image workflow exposed in the beginner UI

## Requirements

You need:

- Python 3.10 or newer
- `gum`
- `git`
- `gh`
- `cosign`
- `dnf5` for manual package-name checks

On an existing Universal Blue system, all or nearly all of these should probably already be present. If something is missing, you can usually solve it by switching to a developer image such as Bazzite DX, Aurora DX, or Bluefin DX, and/or by installing the missing CLI tools with Homebrew.

You also need a GitHub account and should log in first:

```bash
gh auth login
```

On Universal Blue systems, missing CLI tools are typically installed with Homebrew:

```bash
brew install gum gh cosign
```

## Installation

Clone this repo locally and enter the project directory:

```bash
git clone https://github.com/Danathar/ublue-builder.git
cd ublue-builder
```

If the script is not already executable on your system, make it executable once:

```bash
chmod +x ublue_builder.py
```

## Usage

Run the beginner app:

```bash
./ublue_builder.py
```

## Project Scope

This repo intentionally keeps the beginner tool narrow:

- Containerfile-based repo creation and updates are supported
- Existing repos that do not contain `.ublue-builder.json` are not supported for adoption or import
- BlueBuild support was removed from the beginner app to keep the UX and code simpler
- If a BlueBuild-focused workflow is needed later, it should live in a separate tool

## License

GPL-3.0-only. See [LICENSE](LICENSE).
