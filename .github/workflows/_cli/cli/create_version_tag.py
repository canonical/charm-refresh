# Derived from https://github.com/canonical/data-platform-workflows/blob/v32.0.0/python/cli/data_platform_workflows_cli/create_semantic_version_tag.py
import dataclasses
import logging
import os
import re
import subprocess
import sys

from . import check_version_prefix

logging.basicConfig(level=logging.INFO, stream=sys.stdout)


def main():
    # Get last release tag
    try:
        last_tag = subprocess.run(
            # Include "." in match so that we don't match major version tags (e.g. "v1") commonly
            # used in GitHub Actions
            # Use `HEAD^` to exclude a tag created by a previous workflow run (on `HEAD`) if the
            # workflow was retried
            ["git", "describe", "--abbrev=0", "--match", "v[0-9]*.*", "HEAD^"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"{e.stderr=}")
        raise
    logging.info(f"Last release tag: {last_tag}")

    # Get commit prefixes since last release tag
    commit_subjects = subprocess.run(
        ["git", "log", f"{last_tag}..HEAD", "--pretty=format:%s"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.splitlines()
    assert len(commit_subjects) > 0
    prefixes = set()
    for subject in commit_subjects:
        prefixes.add(check_version_prefix.check(subject))
    logging.info(f"Commit prefixes since last release tag: {prefixes}")

    @dataclasses.dataclass(frozen=True)
    class Version:
        refresh: int
        major: int
        minor: int
        patch: int

        @classmethod
        def from_tag(cls, tag: str, /):
            # Regular expression derived from
            # https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
            match = re.fullmatch(
                r"v(?P<refresh>0|[1-9]\d*)\.(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)",
                tag,
            )
            if not match:
                raise ValueError
            return cls(**{name: int(value) for name, value in match.groupdict().items()})

        def to_tag(self) -> str:
            return f"v{self.refresh}.{self.major}.{self.minor}.{self.patch}"

    try:
        last_version = Version.from_tag(last_tag)
    except ValueError:
        raise ValueError(f"Last release tag is not a valid version: {repr(last_tag)}")

    # Determine new version based on commit prefixes
    assert last_version.refresh > 0
    if "REFRESH BREAKING" in prefixes:
        new_version = Version(last_version.refresh + 1, 0, 0, 0)
    elif "breaking" in prefixes:
        new_version = Version(last_version.refresh, last_version.major + 1, 0, 0)
    elif "compatible" in prefixes:
        new_version = Version(last_version.refresh, last_version.major, last_version.minor + 1, 0)
    elif "patch" in prefixes:
        new_version = Version(
            last_version.refresh, last_version.major, last_version.minor, last_version.patch + 1
        )
    else:
        raise ValueError
    new_tag = new_version.to_tag()
    logging.info(f"Determined new release tag: {new_tag}")

    subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        check=True,
    )

    logging.info("Checking if new release tag already exists")
    try:
        tag_commit_sha = subprocess.run(
            ["git", "rev-list", "-n", "1", new_tag], capture_output=True, check=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        logging.info("Release tag does not already exist. Creating tag")
        subprocess.run(["git", "tag", new_tag, "--annotate", "-m", new_tag], check=True)
        subprocess.run(["git", "push", "origin", new_tag], check=True)
    else:
        logging.info("Release tag already exists. Verifying tag")
        head_commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
        ).stdout.strip()
        if head_commit_sha == tag_commit_sha:
            logging.info("Verified existing tag points to the correct commit")
        else:
            raise ValueError(
                f"Attempted to create tag {new_tag} on commit {head_commit_sha} but tag already "
                f"exists on commit {tag_commit_sha}"
            )

    output = f"tag={new_tag}"
    print(f"\n\n{output}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as file:
        file.write(output)
