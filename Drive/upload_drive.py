from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Sequence

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PROJECT_DIR = Path(__file__).resolve().parent


def parse_chunk_size(value: str) -> int:
    try:
        megabytes = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("informe um número inteiro de MB") from exc
    if megabytes < 1:
        raise argparse.ArgumentTypeError("o tamanho do bloco deve ser pelo menos 1 MB")
    return megabytes * 1024 * 1024


def load_upload_jobs(config_path: Path) -> list[dict]:
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido em {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError(f"O conteúdo de {config_path} deve ser um objeto JSON.")
    upload_entries = config.get("upload-drive")
    if not isinstance(upload_entries, list):
        raise ValueError(f"{config_path} deve conter uma lista chamada 'upload-drive'.")

    jobs = []
    for index, entry in enumerate(upload_entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"O item {index} de 'upload-drive' deve ser um objeto.")

        folder_id = entry.get("id", "")
        folder_name = entry.get("name", "")
        csv_value = entry.get("files", "")
        if not isinstance(folder_id, str) or not isinstance(folder_name, str):
            raise ValueError(f"O ID e o nome do item {index} devem ser texto.")
        if not isinstance(csv_value, str) or not csv_value.strip():
            raise ValueError(
                f"O item {index} de 'upload-drive' precisa informar o CSV em 'files'."
            )
        parent_id = entry.get("parent_id", "")
        if not isinstance(parent_id, str):
            raise ValueError(f"O parent_id do item {index} deve ser texto.")
        folder_id = folder_id.strip()
        folder_name = folder_name.strip()
        if not folder_id and not folder_name:
            raise ValueError(
                f"O item {index} de 'upload-drive' precisa informar 'id' ou 'name'."
            )

        csv_path = Path(csv_value).expanduser()
        if not csv_path.is_absolute():
            csv_path = config_path.parent / csv_path
        jobs.append(
            {
                "folder_id": folder_id,
                "folder_name": folder_name,
                "parent_id": parent_id,
                "csv_path": csv_path,
            }
        )
    return jobs


def read_upload_manifest(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV não encontrado: {csv_path}")

    manifest = []
    with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or not {"path", "is_file"}.issubset(reader.fieldnames):
            raise ValueError(
                f"{csv_path} precisa conter as colunas 'path' e 'is_file'."
            )

        for line_number, row in enumerate(reader, start=2):
            value = (row.get("path") or "").strip()
            if not value:
                raise ValueError(f"Caminho vazio em {csv_path}, linha {line_number}.")

            path = Path(value).expanduser()
            if not path.is_absolute():
                path = csv_path.parent / path
            if not path.exists():
                raise FileNotFoundError(
                    f"Caminho da linha {line_number} não encontrado: {path}"
                )

            file_value = (row.get("is_file") or "").strip().casefold()
            if file_value not in {"true", "false"}:
                raise ValueError(
                    f"is_file deve ser true ou false em {csv_path}, "
                    f"linha {line_number}."
                )
            is_file = file_value == "true"
            if is_file and not path.is_file():
                raise ValueError(
                    f"A linha {line_number} marca uma pasta como arquivo: {path}"
                )
            if not is_file and not path.is_dir():
                raise ValueError(
                    f"A linha {line_number} marca um arquivo como pasta: {path}"
                )
            manifest.append({"path": path.resolve(), "is_file": is_file})

    return manifest


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
                f"Não foi possível ler {token_path}. Remova esse token e autentique novamente."
            ) from exc

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise RuntimeError(
                f"A sessão salva em {token_path} expirou. "
                "Remova o token e autentique novamente."
            ) from exc

    if not credentials or not credentials.valid:
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


def escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_target_folder(service, job: dict) -> dict:
    from googleapiclient.errors import HttpError

    folder_id = job.get("folder_id", "")
    folder_name = job.get("folder_name", "")
    if not isinstance(folder_id, str) or not isinstance(folder_name, str):
        raise ValueError("O ID e o nome da pasta de destino devem ser texto.")
    folder_id = folder_id.strip()
    folder_name = folder_name.strip()
    if not folder_id and not folder_name:
        raise ValueError(
            "A pasta de destino está sem ID e sem nome. Preencha 'id' ou 'name' "
            "no objeto correspondente de 'upload-drive' em config.json."
        )

    if folder_id:
        try:
            folder = (
                service.files()
                .get(
                    fileId=folder_id,
                    fields="id, name, mimeType"
                )
                .execute()
            )
            if folder.get("mimeType") != FOLDER_MIME_TYPE:
                raise ValueError(f"O ID '{folder_id}' não corresponde a uma pasta.")
            if folder_name and folder.get("name") != folder_name:
                raise ValueError(
                    f"O ID '{folder_id}' pertence à pasta '{folder.get('name')}', "
                    f"não '{folder_name}'."
                )
            return folder
        except HttpError as exc:
            if getattr(exc, "status_code", None) != 404 or not folder_name:
                raise

    if not folder_name:
        raise ValueError(
            f"A pasta com ID '{folder_id}' não foi encontrada. Informe também "
            "o nome para permitir localizar ou criar a pasta."
        )

    parent_id = job.get("parent_id")
    if not isinstance(parent_id, str):
        raise ValueError("O parent_id da pasta de destino deve ser texto.")
    parent_id = parent_id.strip()
    query = (
        f"name = '{escape_query_value(folder_name)}' and "
        f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
    )
    if parent_id:
        query = f"'{escape_query_value(parent_id)}' in parents and {query}"

    matches = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                pageSize=1000,
                pageToken=page_token,
                fields="nextPageToken,files(id,name,mimeType)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        matches.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if len(matches) > 1:
        ids = ", ".join(folder["id"] for folder in matches)
        raise ValueError(
            f"Há mais de uma pasta chamada '{folder_name}' ({ids}). "
            "Informe o ID da pasta em config.json."
        )
    if matches:
        return matches[0]

    #created = create_drive_folder(service, folder_name, parent_id)
    #created = create_drive_folder(service, folder_name, parent_id)
    # if not created.get("id"):
    #     raise RuntimeError(
    #         f"A API não retornou o ID da pasta criada '{folder_name}'."
    #     )
    #print(f"Pasta de destino criada: {created.get('name', folder_name)}")
    return {
        "id": folder_id,
        "name": folder_name,
        "mimeType": FOLDER_MIME_TYPE,
    }


def upload_file(service, path: Path, chunk_size: int, parent_id: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(
        str(path),
        mimetype=mime_type,
        resumable=True,
        chunksize=chunk_size,
    )
    request = service.files().create(
        body={"name": path.name, "parents": [parent_id]},
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    )

    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=3)
        if status:
            percent = int(status.progress() * 100)
            print(f"\rEnviando {path.name}: {percent}%", end="", flush=True)

    print(f"\rEnviado: {path.name}")
    return response


def create_drive_folder_old(service, name: str, parent_id: str) -> dict:
    body = {"name": name, "mimeType": FOLDER_MIME_TYPE}
    if parent_id:
        body["parents"] = [parent_id]
    return (
        service.files()
        .create(
            body=body,
            fields="id,name",
            supportsAllDrives=True,
        )
        .execute()
    )

def create_drive_folder(service, nome_pasta, pasta_pai_id=None):
    metadados = {
        "name": nome_pasta,
        "mimeType": FOLDER_MIME_TYPE,
    }
    if pasta_pai_id:
        metadados["parents"] = [pasta_pai_id]

    return (service.files().create(body=metadados, fields="id, name").execute())
    print(f"Pasta criada: {pasta.get('name')} (ID: {pasta.get('id')})")
    return pasta.get("id")


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def find_drive_parent_id(
    local_parent: Path,
    folder_ids: dict[str, str],
    destination_id: str,
) -> str:
    current = local_parent
    while True:
        drive_id = folder_ids.get(path_key(current))
        if drive_id:
            return drive_id
        if current.parent == current:
            return destination_id
        current = current.parent


def upload_manifest(
    service,
    manifest: list[dict],
    destination_id: str,
    chunk_size: int,
) -> tuple[int, int]:
    folder_ids: dict[str, str] = {}
    created_folder_count = 0
    folders = [entry["path"] for entry in manifest if not entry["is_file"]]
    folders.sort(key=lambda path: len(path.parts))

    for local_folder in folders:
        key = path_key(local_folder)
        if key in folder_ids:
            continue
        parent_id = find_drive_parent_id(
            local_folder.parent, folder_ids, destination_id
        )
        created = create_drive_folder(service, local_folder.name, parent_id)
        #created = create_drive_folder(service, local_folder.name, parent_id)
        folder_id = created.get("id")
        if not folder_id:
            raise RuntimeError(
                f"A API não retornou o ID da pasta '{local_folder.name}'."
            )
        folder_ids[key] = folder_id
        created_folder_count += 1
        print(f"Pasta criada: {created.get('name', local_folder.name)}")

    uploaded_files = 0
    for entry in manifest:
        if not entry["is_file"]:
            continue
        path = entry["path"]
        parent_id = find_drive_parent_id(path.parent, folder_ids, destination_id)
        result = upload_file(service, path, chunk_size, parent_id)
        file_id = result.get("id")
        if not file_id:
            raise RuntimeError(f"A API não retornou o ID de '{path.name}'.")
        uploaded_files += 1
        print(f"ID no Drive: {file_id}")
        if result.get("webViewLink"):
            print(f"Link: {result['webViewLink']}")
    return created_folder_count, uploaded_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Envia arquivos e pastas listados em CSV para o Google Drive."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_DIR / "config.json",
        help="arquivo JSON com a lista upload-drive (padrão: config.json)",
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
        default=Path("token.json"),
        help="arquivo local para salvar a sessão OAuth (padrão: token.json)",
    )
    parser.add_argument(
        "--chunk-size",
        type=parse_chunk_size,
        default=8 * 1024 * 1024,
        metavar="MB",
        help="tamanho dos blocos em MB (padrão: 8)",
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
        config_path = args.config.expanduser().resolve()
        jobs = load_upload_jobs(config_path)
        prepared_jobs = [
            (job, read_upload_manifest(job["csv_path"])) for job in jobs
        ]
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if not prepared_jobs:
        print(f"Nenhum objeto configurado em 'upload-drive' em {config_path}.")
        return 0

    print(f"Configuração carregada: {config_path}")

    try:
        service = build_drive_service(args.credentials, args.token)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    failed = False
    for index, (job, manifest) in enumerate(prepared_jobs, start=1):
        try:
            target = get_target_folder(service, job)
            created_folders, uploaded_files = upload_manifest(
                service,
                manifest,
                target["id"],
                args.chunk_size,
            )
            print(
                f"Configuração {index}: {created_folders} pastas e "
                f"{uploaded_files} arquivos enviados para '{target['name']}'."
            )
        except (HttpError, OSError, RuntimeError, ValueError, KeyError) as exc:
            failed = True
            print(f"Falha na configuração {index}: {exc}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
