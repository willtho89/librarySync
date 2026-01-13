#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = (
    ROOT / "backend/pyproject.toml",
)
UV_LOCK = ROOT / "uv.lock"

VERSION_LINE = re.compile(r'^(?P<indent>\s*)version\s*=\s*"(?P<version>[^"]+)"\s*$')
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>[-+].+)?$"
)


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def get_version(path: Path) -> str:
    for line in path.read_text().splitlines():
        match = VERSION_LINE.match(line)
        if match:
            return match.group("version")
    raise SystemExit(f"Version not found in {path}")


def set_version(path: Path, new_version: str) -> None:
    lines = path.read_text().splitlines()
    updated = False
    for idx, line in enumerate(lines):
        match = VERSION_LINE.match(line)
        if match:
            indent = match.group("indent")
            lines[idx] = f'{indent}version = "{new_version}"'
            updated = True
            break
    if not updated:
        raise SystemExit(f"Version not found in {path}")
    path.write_text("\n".join(lines) + "\n")


def bump_version(version: str, part: str) -> str:
    match = SEMVER.match(version)
    if not match:
        raise SystemExit(
            f'Current version "{version}" is not semver; pass an explicit version instead.'
        )
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def ensure_clean_worktree() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise SystemExit("Working tree is dirty; commit/stash or use --allow-dirty.")


def run_checks() -> None:
    run(["uv", "run", "ruff", "check", "./backend/"])
    run(["uv", "run", "python", "-m", "unittest", "discover", "backend/tests"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bump versions, tag, and create a GitHub release.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true", help="Bump major version.")
    group.add_argument("--minor", action="store_true", help="Bump minor version.")
    group.add_argument("--patch", action="store_true", help="Bump patch version.")
    parser.add_argument(
        "version",
        nargs="?",
        help="Explicit version to set (e.g. 0.8.5).",
    )
    parser.add_argument("--no-commit", action="store_true", help="Skip git commit.")
    parser.add_argument("--no-tag", action="store_true", help="Skip git tag.")
    parser.add_argument("--no-release", action="store_true", help="Skip GitHub release.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow dirty working tree.",
    )
    parser.add_argument(
        "--tag-prefix",
        default="v",
        help="Tag prefix (default: v).",
    )
    args = parser.parse_args()

    if args.no_commit and (not args.no_tag or not args.no_release):
        parser.error("--no-commit requires --no-tag and --no-release.")
    if args.no_tag and not args.no_release:
        parser.error("--no-tag requires --no-release.")
    if not args.version and not (args.major or args.minor or args.patch):
        parser.error("Provide a version or one of --major/--minor/--patch.")

    base_version = get_version(VERSION_FILES[0])

    if args.version:
        if not SEMVER.match(args.version):
            raise SystemExit(f'Provided version "{args.version}" is not semver (x.y.z).')
        new_version = args.version
    else:
        part = "major" if args.major else "minor" if args.minor else "patch"
        new_version = bump_version(base_version, part)

    if new_version == base_version:
        raise SystemExit("New version matches current version; nothing to do.")

    if not args.allow_dirty and (not args.no_commit or not args.no_tag or not args.no_release):
        ensure_clean_worktree()

    if not args.no_commit:
        run_checks()

    for path in VERSION_FILES:
        set_version(path, new_version)
        print(f"Updated {path.relative_to(ROOT)} to {new_version}.")

    run(["uv", "lock"])
    print(f"Updated {UV_LOCK.relative_to(ROOT)}.")

    tag_name = f"{args.tag_prefix}{new_version}"

    if not args.no_commit:
        run(["git", "add", str(VERSION_FILES[0]), str(UV_LOCK)])
        run(["git", "commit", "-m", f"Release {tag_name}"])
        run(["git", "push"])

    if not args.no_tag:
        run(["git", "tag", "-a", tag_name, "-m", tag_name])
        run(["git", "push", "--tags"])

    if not args.no_release:
        if not shutil.which("gh"):
            raise SystemExit("gh CLI not found; install it or use --no-release.")
        run(["gh", "release", "create", tag_name, "--title", tag_name, "--generate-notes"])

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
