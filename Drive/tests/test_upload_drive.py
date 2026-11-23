import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from googleapiclient.errors import HttpError
from upload_drive import (
    FOLDER_MIME_TYPE,
    PROJECT_DIR,
    build_parser,
    get_target_folder,
    load_upload_jobs,
    parse_chunk_size,
    read_upload_manifest,
    upload_manifest,
)


class ParseChunkSizeTests(unittest.TestCase):
    def test_converts_megabytes_to_bytes(self):
        self.assertEqual(parse_chunk_size("8"), 8 * 1024 * 1024)

    def test_rejects_non_positive_values(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                parse_chunk_size(value)

    def test_rejects_non_integer_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_chunk_size("1.5")


class UploadConfigTests(unittest.TestCase):
    def test_default_config_is_next_to_the_script(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.config, PROJECT_DIR / "config.json")

    def test_reads_jobs_and_resolves_relative_csv_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "upload-drive": [
                            {"id": "destination-id", "files": "manifest.csv"},
                            {"name": "Backups", "files": "other.csv"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            jobs = load_upload_jobs(config_path)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["folder_id"], "destination-id")
        self.assertEqual(jobs[0]["csv_path"], config_path.parent / "manifest.csv")
        self.assertEqual(jobs[1]["folder_name"], "Backups")

    def test_requires_a_destination_id_or_name(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"upload-drive": [{"files": "manifest.csv"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "id' ou 'name"):
                load_upload_jobs(config_path)


class UploadManifestTests(unittest.TestCase):
    def test_reads_file_and_folder_rows_from_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "backup"
            root.mkdir()
            source_file = root / "report.txt"
            source_file.write_text("test", encoding="utf-8")
            csv_path = Path(directory) / "manifest.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=["path", "is_file"])
                writer.writeheader()
                writer.writerow({"path": str(root), "is_file": "false"})
                writer.writerow({"path": str(source_file), "is_file": "true"})

            manifest = read_upload_manifest(csv_path)

        self.assertEqual(
            [(entry["path"], entry["is_file"]) for entry in manifest],
            [(root.resolve(), False), (source_file.resolve(), True)],
        )

    def test_rejects_csv_rows_with_missing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "manifest.csv"
            csv_path.write_text(
                "path,is_file\nmissing.txt,true\n",
                encoding="utf-8",
            )

            with self.assertRaises(FileNotFoundError):
                read_upload_manifest(csv_path)

    def test_creates_folder_tree_and_uploads_file_into_matching_folder(self):
        class FakeRequest:
            def __init__(self, response):
                self.response = response

            def execute(self):
                return self.response

        class FakeFiles:
            def __init__(self):
                self.created_folders = []

            def create(self, body, fields, supportsAllDrives):
                self.created_folders.append(body)
                self.supports_all_drives = supportsAllDrives
                folder_id = f"drive-folder-{len(self.created_folders)}"
                return FakeRequest({"id": folder_id, "name": body["name"]})

        class FakeService:
            def __init__(self):
                self.files_api = FakeFiles()

            def files(self):
                return self.files_api

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "backup"
            nested = root / "documents"
            nested.mkdir(parents=True)
            source_file = nested / "report.txt"
            source_file.write_text("test", encoding="utf-8")
            manifest = [
                {"path": root, "is_file": False},
                {"path": nested, "is_file": False},
                {"path": source_file, "is_file": True},
            ]
            service = FakeService()
            upload_calls = []

            def record_upload(_service, path, _chunk_size, parent_id):
                upload_calls.append((path, parent_id))
                return {"id": "uploaded-file"}

            with patch("upload_drive.upload_file", side_effect=record_upload):
                created_folder_count, uploaded_count = upload_manifest(
                    service,
                    manifest,
                    destination_id="destination",
                    chunk_size=1024 * 1024,
                )

        self.assertEqual(created_folder_count, 2)
        self.assertEqual(uploaded_count, 1)
        self.assertTrue(service.files_api.supports_all_drives)
        self.assertEqual(
            service.files_api.created_folders,
            [
                {
                    "name": "backup",
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": ["destination"],
                },
                {
                    "name": "documents",
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": ["drive-folder-1"],
                },
            ],
        )
        self.assertEqual(upload_calls, [(source_file, "drive-folder-2")])


class TargetFolderTests(unittest.TestCase):
    def test_rejects_empty_id_and_name_before_calling_drive_api(self):
        class FakeService:
            def files(self):
                raise AssertionError("A API não deveria ser chamada.")

        with self.assertRaisesRegex(ValueError, "sem ID e sem nome"):
            get_target_folder(
                FakeService(),
                {"folder_id": "", "folder_name": ""},
            )

    def test_uses_folder_id_and_validates_optional_name(self):
        class FakeRequest:
            def execute(self):
                return {
                    "id": "destination-id",
                    "name": "Backups",
                    "mimeType": FOLDER_MIME_TYPE,
                }

        class FakeFiles:
            def get(self, fileId, fields, supportsAllDrives):
                self.file_id = fileId
                self.supports_all_drives = supportsAllDrives
                return FakeRequest()

        class FakeService:
            def __init__(self):
                self.files_api = FakeFiles()

            def files(self):
                return self.files_api

        service = FakeService()
        folder = get_target_folder(
            service,
            {"folder_id": "destination-id", "folder_name": "Backups"},
        )

        self.assertEqual(folder["id"], "destination-id")
        self.assertEqual(service.files_api.file_id, "destination-id")
        self.assertTrue(service.files_api.supports_all_drives)

    def test_resolves_folder_by_name(self):
        class FakeRequest:
            def execute(self):
                return {
                    "files": [
                        {
                            "id": "destination-id",
                            "name": "Backups",
                            "mimeType": FOLDER_MIME_TYPE,
                        }
                    ]
                }

        class FakeFiles:
            def list(self, q, **kwargs):
                self.query = q
                return FakeRequest()

        class FakeService:
            def __init__(self):
                self.files_api = FakeFiles()

            def files(self):
                return self.files_api

        service = FakeService()
        folder = get_target_folder(
            service,
            {"folder_id": "", "folder_name": "Backups", "parent_id": ""},
        )

        self.assertEqual(folder["id"], "destination-id")
        self.assertIn("name = 'Backups'", service.files_api.query)

    def test_creates_destination_when_name_is_not_found(self):
        class FakeRequest:
            def __init__(self, response):
                self.response = response

            def execute(self):
                return self.response

        class FakeFiles:
            def __init__(self):
                self.created_body = None

            def list(self, q, **kwargs):
                return FakeRequest({"files": []})

            def create(self, body, **kwargs):
                self.created_body = body
                return FakeRequest({"id": "new-folder-id", "name": body["name"]})

        class FakeService:
            def __init__(self):
                self.files_api = FakeFiles()

            def files(self):
                return self.files_api

        service = FakeService()
        folder = get_target_folder(
            service,
            {
                "folder_id": "",
                "folder_name": "Backups",
                "parent_id": "shared-drive-root",
            },
        )

        self.assertEqual(folder["id"], "new-folder-id")
        self.assertEqual(
            service.files_api.created_body,
            {
                "name": "Backups",
                "mimeType": FOLDER_MIME_TYPE,
                "parents": ["shared-drive-root"],
            },
        )

    def test_falls_back_to_name_when_id_is_not_found(self):
        class FakeRequest:
            def __init__(self, response=None, error=None):
                self.response = response
                self.error = error

            def execute(self):
                if self.error:
                    raise self.error
                return self.response

        class FakeFiles:
            def get(self, **kwargs):
                return FakeRequest(
                    error=HttpError(
                        SimpleNamespace(status=404, reason="Not Found"),
                        b'{"error":{"message":"File not found"}}',
                    )
                )

            def list(self, q, **kwargs):
                return FakeRequest(
                    response={
                        "files": [
                            {
                                "id": "resolved-folder-id",
                                "name": "Backups",
                                "mimeType": FOLDER_MIME_TYPE,
                            }
                        ]
                    }
                )

        class FakeService:
            def files(self):
                return FakeFiles()

        folder = get_target_folder(
            FakeService(),
            {
                "folder_id": "stale-folder-id",
                "folder_name": "Backups",
                "parent_id": "",
            },
        )

        self.assertEqual(folder["id"], "resolved-folder-id")


if __name__ == "__main__":
    unittest.main()
