"""Offline publication boundary checks; no GitHub token or network is used."""
import importlib.util
from contextlib import chdir
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools/publish_release_notes.py")
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)
REVISION = "a" * 40


class ReleaseNotesTests(unittest.TestCase):
    def test_only_main_push_with_exact_revision_is_accepted(self):
        env = {"GITHUB_REPOSITORY": "owner/broker", "GITHUB_EVENT_NAME": "push",
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": REVISION}
        event = {"before": "b" * 40, "after": REVISION, "ref": "refs/heads/main"}
        self.assertEqual(notes.event_context(env, event), ("owner/broker", "b" * 40, REVISION))
        for changed in ({"GITHUB_EVENT_NAME": "pull_request"},
                        {"GITHUB_REF": "refs/heads/feature"},
                        {"GITHUB_REPOSITORY": "../broker"}, {"GITHUB_SHA": "c" * 40}):
            with self.subTest(changed=changed), self.assertRaises(notes.PublicationError):
                notes.event_context(env | changed, event)
        for changed in ({"before": "0" * 40}, {"after": "not-a-sha"}, {"deleted": True}):
            with self.subTest(changed=changed), self.assertRaises(notes.PublicationError):
                notes.event_context(env, event | changed)

    def test_tag_and_successful_run_must_match_all_boundaries(self):
        good = {"head_sha": REVISION, "head_branch": "v0.3.0", "event": "push",
                "status": "completed", "conclusion": "success"}
        self.assertTrue(notes.release_passed([{"workflow_runs": [good]}], "v0.3.0", REVISION))
        for field, value in (("head_sha", "b" * 40), ("head_branch", "main"),
                             ("event", "pull_request"), ("status", "in_progress"),
                             ("conclusion", "failure")):
            with self.subTest(field=field):
                self.assertFalse(notes.release_passed(
                    [{"workflow_runs": [good | {field: value}]}], "v0.3.0", REVISION))

    def test_existing_release_is_idempotent_and_never_overwritten(self):
        good = {"tag_name": "v0.3.0", "body": "Approved notes\n",
                "draft": False, "prerelease": False}
        self.assertTrue(notes.existing_release([[good]], "v0.3.0", "Approved notes\n"))
        self.assertFalse(notes.existing_release([[]], "v0.3.0", "Approved notes\n"))
        for changed in ({"draft": True}, {"prerelease": True}, {"body": "other"}):
            with self.subTest(changed=changed), self.assertRaises(notes.PublicationError):
                notes.existing_release([[good | changed]], "v0.3.0", "Approved notes\n")

    def test_source_version_is_parsed_without_executing_source(self):
        self.assertEqual(notes.version_from_source(
            'raise RuntimeError("must not run")\n__version__ = "0.3.0"'), "0.3.0")
        with self.assertRaises(notes.PublicationError):
            notes.version_from_source('__version__ = str(3)')

    def test_invalid_notes_names_fail_before_any_external_command(self):
        for name in ("docs/releases/v01.2.3.md", "docs/releases/v1.2.md",
                     "docs/releases/../../v1.2.3.md", "docs/releases/v1.2.3;bad.md"):
            with self.subTest(name=name), patch.object(notes, "command") as execute:
                with self.assertRaises(notes.PublicationError):
                    notes.publish("owner/broker", REVISION, name)
                execute.assert_not_called()

    def test_create_requires_success_and_uses_an_existing_tag(self):
        good_run = {"head_sha": REVISION, "head_branch": "v0.3.0", "event": "push",
                    "status": "completed", "conclusion": "success"}
        for successful in (False, True):
            with self.subTest(successful=successful), tempfile.TemporaryDirectory() as temp, chdir(temp):
                path = Path("docs/releases/v0.3.0.md")
                path.parent.mkdir(parents=True)
                path.write_text("Approved notes\n")
                run = good_run | {"conclusion": "success" if successful else "failure"}
                replies = ["Approved notes\n", REVISION + "\n", '__version__ = "0.3.0"',
                           json.dumps([{"workflow_runs": [run]}]), "[[]]", ""]
                with patch.object(notes, "command", side_effect=replies) as execute:
                    if successful:
                        notes.publish("owner/broker", REVISION, str(path))
                        self.assertEqual(execute.call_args.args,
                            ("gh", "release", "create", "v0.3.0", "--repo", "owner/broker",
                             "--verify-tag", "--title", "Cred Broker v0.3.0", "--notes-file", str(path)))
                    else:
                        with self.assertRaises(notes.PublicationError):
                            notes.publish("owner/broker", REVISION, str(path))
                        self.assertEqual(execute.call_count, 4)

    def test_version_mismatch_stops_before_github_and_identical_release_has_no_write(self):
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            path = Path("docs/releases/v0.3.0.md")
            path.parent.mkdir(parents=True)
            path.write_text("Approved notes\n")
            with patch.object(notes, "command", side_effect=[
                    "Approved notes\n", REVISION, '__version__ = "0.2.1"']) as execute:
                with self.assertRaises(notes.PublicationError):
                    notes.publish("owner/broker", REVISION, str(path))
                self.assertEqual(execute.call_count, 3)
            good_run = {"head_sha": REVISION, "head_branch": "v0.3.0", "event": "push",
                        "status": "completed", "conclusion": "success"}
            existing = {"tag_name": "v0.3.0", "body": "Approved notes\n",
                        "draft": False, "prerelease": False}
            with patch.object(notes, "command", side_effect=[
                    "Approved notes\n", REVISION, '__version__ = "0.3.0"',
                    json.dumps([{"workflow_runs": [good_run]}]), json.dumps([[existing]])]) as execute:
                notes.publish("owner/broker", REVISION, str(path))
                self.assertEqual(execute.call_count, 5)


if __name__ == "__main__":
    unittest.main()
