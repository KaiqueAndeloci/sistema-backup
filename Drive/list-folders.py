from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Sequence

SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
CSV_FIELDS = ["id", "name", "parent_id", "path", "modified_time", "web_view_link"]


def build_drive_service(credentials_path: Path, token_path: Path):
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Dependências ausentes. Instale-as com: "
            "pip install -r requirements.txt"
        ) from exc

    credentials = None
    if token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path), SCOPES
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Não foi possível ler {token_path}. "
                "Remova esse token e autentique novamente."
            ) from exc

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise RuntimeError(
                f"A sessão salva em {token_path} expirou. "
                "Remova o token e autentique novamente."
            ) from exc

    if not credentials or not credentials.valid or not credentials.has_scopes(SCOPES):
        if not credentials_path.is_file():
            raise FileNotFoundError(
                f"Credenciais OAuth não encontradas: {credentials_path}. "
                "Consulte o README para configurá-las."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), SCOPES
        )
        credentials = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_child_folders(service, parent_id: str) -> list[dict]:
    folders: list[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{parent_id}' in parents and "
                f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false",
                pageSize=1000,
                pageToken=page_token,
                orderBy="name",
                fields="nextPageToken,files(id,name,modifiedTime,webViewLink)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        folders.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return folders


def scan_folders(service) -> list[dict[str, str]]:
    root = service.files().get(fileId="root", fields="id,name").execute()
    root_id = root["id"]
    root_name = "Meu Drive"
    folders: list[dict[str, str]] = [
        {
            "id": root_id,
            "name": root_name,
            "parent_id": "",
            "path": root_name,
            "modified_time": "",
            "web_view_link": "",
        }
    ]

    pending = deque([(root_id, root_name)])
    visited = {root_id}
    while pending:
        parent_id, parent_path = pending.popleft()
        children = list_child_folders(service, parent_id)
        children.sort(key=lambda item: item.get("name", "").casefold())
        for child in children:
            folder_id = child["id"]
            if folder_id in visited:
                continue
            visited.add(folder_id)
            name = child.get("name", "")
            path = f"{parent_path}/{name}"
            folders.append(
                {
                    "id": folder_id,
                    "name": name,
                    "parent_id": parent_id,
                    "path": path,
                    "modified_time": child.get("modifiedTime", ""),
                    "web_view_link": child.get("webViewLink", ""),
                }
            )
            pending.append((folder_id, path))

    return folders


def write_csv(folders: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(folders)


def load_observed_folder_ids(config_path: Path) -> list[str]:
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido em {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError(f"O conteúdo de {config_path} deve ser um objeto JSON.")
    observed_folders = config.get("observed-folders")
    if not isinstance(observed_folders, list):
        raise ValueError(
            f"{config_path} deve conter uma lista chamada 'observed-folders'."
        )

    folder_ids = []
    for index, folder in enumerate(observed_folders, start=1):
        if not isinstance(folder, dict):
            raise ValueError(
                f"O item {index} de 'observed-folders' deve ser um objeto."
            )
        folder_id = folder.get("id", "")
        if not isinstance(folder_id, str):
            raise ValueError(f"O ID da pasta no item {index} deve ser texto.")
        folder_id = folder_id.strip()
        if folder_id:
            folder_ids.append(folder_id)
        elif any(value not in ("", None) for value in folder.values()):
            raise ValueError(
                f"O item {index} de 'observed-folders' tem dados, mas não tem ID."
            )

    return folder_ids


def map_observed_folders(
    folders: list[dict[str, str]],
    observed_folder_ids: list[str],
) -> list[dict[str, str]]:
    if not observed_folder_ids:
        return list(folders)

    folder_by_id = {folder["id"]: folder for folder in folders}
    children_by_parent: dict[str, list[str]] = {}
    for folder in folders:
        parent_id = folder["parent_id"]
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(folder["id"])

    mapped_folders = []
    for observed_id in observed_folder_ids:
        if observed_id not in folder_by_id:
            raise ValueError(
                f"A pasta configurada com ID '{observed_id}' não foi encontrada "
                "no Meu Drive."
            )
        subtree_ids = set()
        pending = [observed_id]
        while pending:
            folder_id = pending.pop()
            if folder_id in subtree_ids:
                continue
            subtree_ids.add(folder_id)
            pending.extend(children_by_parent.get(folder_id, []))
        mapped_folders.extend(
            folder for folder in folders if folder["id"] in subtree_ids
        )

    return mapped_folders


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lista as pastas do Meu Drive e exporta a estrutura para CSV."
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(os.environ.get("GOOGLE_CLIENT_SECRETS", "credentials.json")),
        help="arquivo OAuth do Google (padrão: credentials.json)",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=Path("list_folders_token.json"),
        help="arquivo local da sessão de leitura (padrão: list_folders_token.json)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="arquivo JSON com a lista observed-folders (padrão: config.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("drive_folders.csv"),
        help="caminho do CSV de saída (padrão: drive_folders.csv)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        print(
            "Erro: dependências ausentes. Instale-as com: "
            "pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    try:
        observed_folder_ids = load_observed_folder_ids(args.config)
        service = build_drive_service(args.credentials, args.token)
        folders = scan_folders(service)
        mapped_folders = map_observed_folders(folders, observed_folder_ids)
        write_csv(mapped_folders, args.output)
    except HttpError as exc:
        print(f"Erro da API do Google Drive: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(f"{len(mapped_folders)} pastas exportadas para {args.output}")
    if observed_folder_ids:
        print(f"Pastas observadas configuradas: {len(observed_folder_ids)}")
    else:
        print("Nenhuma pasta configurada; todas as pastas e subpastas foram mapeadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
