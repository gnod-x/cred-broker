"""Publish versioned notes only after the exact tag's container release passed.

Run by release-notes.yml with GitHub's short-lived repository token. This helper
does not build images, create tags, or edit an existing GitHub Release.
"""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
NOTES = re.compile(r"docs/releases/(v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\.md")


class PublicationError(Exception):
    pass


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise PublicationError("Publication dependency failed; no command output is echoed")
    return result.stdout


def event_context(env, event):
    repository = env.get("GITHUB_REPOSITORY", "")
    before, after = event.get("before", ""), event.get("after", "")
    if (env.get("GITHUB_EVENT_NAME") != "push"
            or env.get("GITHUB_REF") != "refs/heads/main"
            or event.get("ref") != "refs/heads/main"
            or event.get("deleted") is True
            or not REPOSITORY.fullmatch(repository)
            or any(not isinstance(value, str) or not SHA.fullmatch(value)
                   or value == "0" * 40 for value in (before, after))
            or env.get("GITHUB_SHA") != after):
        raise PublicationError("Expected a main-branch push with an exact before/after revision")
    return repository, before, after


def version_from_source(source):
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            return node.value.value
    raise PublicationError("The tagged source has no literal version")


def release_passed(pages, tag, revision):
    return any(
        run.get("head_sha") == revision
        and run.get("head_branch") == tag
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        for page in pages for run in page.get("workflow_runs", [])
    )


def existing_release(pages, tag, body):
    releases = [release for page in pages for release in page
                if release.get("tag_name") == tag]
    if not releases:
        return False
    if len(releases) != 1:
        raise PublicationError("Ambiguous existing release")
    release = releases[0]
    if (release.get("draft") is not False or release.get("prerelease") is not False
            or release.get("body") != body):
        raise PublicationError("An existing release differs; refusing to overwrite it")
    return True


def publish(repository, after, filename):
    match = NOTES.fullmatch(filename)
    if not match:
        raise PublicationError("Notes filename must be docs/releases/vMAJOR.MINOR.PATCH.md")
    tag = match[1]
    path = Path(filename)
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024
            or not path.resolve().is_relative_to(Path.cwd().resolve())):
        raise PublicationError("Notes must be a bounded regular file")
    body = path.read_text(encoding="utf-8")
    if not body.strip() or "\x00" in body or body != command("git", "show", f"{after}:{filename}"):
        raise PublicationError("Notes do not match the committed document")
    revision = command("git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").strip()
    if not SHA.fullmatch(revision):
        raise PublicationError("Expected an existing version tag")
    source = command("git", "show", f"{revision}:cred_broker/__init__.py")
    if version_from_source(source) != tag[1:]:
        raise PublicationError("Tag and source versions disagree")
    runs = json.loads(command(
        "gh", "api", "--paginate", "--slurp",
        f"repos/{repository}/actions/workflows/release.yml/runs?head_sha={revision}&per_page=100",
    ))
    if not release_passed(runs, tag, revision):
        raise PublicationError("The exact tag has no successful container release")
    releases = json.loads(command("gh", "api", "--paginate", "--slurp",
                                  f"repos/{repository}/releases?per_page=100"))
    if existing_release(releases, tag, body):
        print(f"{tag}: identical published notes already exist")
        return
    command("gh", "release", "create", tag, "--repo", repository,
            "--verify-tag", "--title", f"Cred Broker {tag}", "--notes-file", filename)
    print(f"{tag}: published notes for validated revision {revision}")


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repository, before, after = event_context(os.environ, event)
    if command("git", "rev-parse", "HEAD").strip() != after:
        raise PublicationError("Checkout does not match the triggering revision")
    command("git", "merge-base", "--is-ancestor", before, after)
    names = command("git", "diff", "--name-only", "-z", "--diff-filter=AM",
                    before, after, "--", "docs/releases").split("\x00")
    selected = [name for name in names if name and Path(name).name.startswith("v")
                and name.endswith(".md")]
    # Validate every filename before the first external write.
    if any(not NOTES.fullmatch(name) for name in selected):
        raise PublicationError("Unsupported versioned notes filename")
    for name in selected:
        publish(repository, after, name)
    if not selected:
        print("No added or modified versioned release notes")


if __name__ == "__main__":
    try:
        main()
    except PublicationError as error:
        print(f"Release notes not published: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, SyntaxError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"Release notes not published: {error.__class__.__name__}", file=sys.stderr)
        raise SystemExit(1) from None
