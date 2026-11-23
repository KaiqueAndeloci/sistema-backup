import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "list-folders.py"
SPEC = importlib.util.spec_from_file_location("list_folders", MODULE_PATH)
list_folders = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(list_folders)


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeFiles:
    def __init__(self):
        self.children = {
            "root-id": [
                {"id": "folder-a", "name": "Projetos"},
                {"id": "folder-c", "name": "Fotos"},
            ],
            "folder-a": [{"id": "folder-b", "name": "2026"}],
            "folder-b": [],
            "folder-c": [],
        }

    def get(self, fileId, fields):
        return FakeRequest({"id": "root-id", "name": "My Drive"})

    def list(self, q, **kwargs):
        parent_id = q.split("'")[1]
        return FakeRequest({"files": self.children[parent_id]})


class FakeService:
    def __init__(self):
        self.files_api = FakeFiles()

    def files(self):
        return self.files_api


class ScanFoldersTests(unittest.TestCase):
    def test_scans_nested_folders_with_full_paths(self):
        folders = list_folders.scan_folders(FakeService())

        self.assertEqual(
            [folder["path"] for folder in folders],
            [
                "Meu Drive",
                "Meu Drive/Fotos",
                "Meu Drive/Projetos",
                "Meu Drive/Projetos/2026",
            ],
        )
        nested_folder = folders[-1]
        self.assertEqual(nested_folder["parent_id"], "folder-a")
        self.assertEqual(nested_folder["id"], "folder-b")

    def test_writes_csv_with_headers_and_folder_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "folders.csv"
            folders = list_folders.scan_folders(FakeService())

            list_folders.write_csv(folders, output_path)

            with output_path.open(encoding="utf-8-sig", newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[-1]["path"], "Meu Drive/Projetos/2026")

    def test_empty_config_maps_all_folders(self):
        folders = list_folders.scan_folders(FakeService())

        self.assertEqual(list_folders.map_observed_folders(folders, []), folders)

    def test_blank_placeholder_in_config_maps_all_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "observed-folders": [
                            {
                                "id": "",
                                "name": "",
                                "parent_id": "",
                                "path": "",
                                "modified_time": "",
                                "web_view_link": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            observed_ids = list_folders.load_observed_folder_ids(config_path)
            folders = list_folders.scan_folders(FakeService())

        self.assertEqual(observed_ids, [])
        self.assertEqual(
            list_folders.map_observed_folders(folders, observed_ids),
            folders,
        )

    def test_reads_folder_ids_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "observed-folders": [
                            {"id": "folder-a"},
                            {"id": "folder-a"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                list_folders.load_observed_folder_ids(config_path),
                ["folder-a", "folder-a"],
            )

    def test_maps_configured_folder_and_its_subfolders(self):
        folders = list_folders.scan_folders(FakeService())

        mapped = list_folders.map_observed_folders(folders, ["folder-a"])

        self.assertEqual(
            [folder["id"] for folder in mapped],
            ["folder-a", "folder-b"],
        )

    def test_repeated_configured_folder_is_repeated_in_output(self):
        folders = list_folders.scan_folders(FakeService())

        mapped = list_folders.map_observed_folders(
            folders, ["folder-a", "folder-a"]
        )

        self.assertEqual(
            [folder["id"] for folder in mapped],
            ["folder-a", "folder-b", "folder-a", "folder-b"],
        )

    def test_rejects_configured_folder_that_was_not_found(self):
        folders = list_folders.scan_folders(FakeService())

        with self.assertRaisesRegex(ValueError, "não foi encontrada"):
            list_folders.map_observed_folders(folders, ["missing-folder"])


if __name__ == "__main__":
    unittest.main()
