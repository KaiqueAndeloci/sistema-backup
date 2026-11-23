# Upload para o Google Drive

Utilitário de terminal em Python para enviar arquivos e pastas do computador ao
Google Drive. O envio de arquivos é retomável e usa blocos para arquivos grandes.

## Configuração

1. No [Google Cloud Console](https://console.cloud.google.com/), crie ou escolha
   um projeto e ative a **Google Drive API**.
2. Configure a tela de consentimento OAuth e crie um ID de cliente do tipo
   **Aplicativo para computador**.
3. Baixe o arquivo JSON do cliente OAuth e salve-o como `credentials.json` na
   pasta deste projeto. Não compartilhe nem versione esse arquivo.
4. Instale as dependências:

   ```powershell
   .\environment\Scripts\python.exe -m pip install -r requirements.txt
   ```

## Uso

O envio lê as entradas `upload-drive` de `config.json`. Em cada objeto,
`files` aponta para um CSV local com as colunas `path` e `is_file`; cada linha
indica um arquivo (`true`) ou pasta (`false`). Os caminhos relativos no CSV são
resolvidos a partir da pasta do próprio CSV. As pastas listadas são criadas no
Drive mantendo a hierarquia, e os arquivos são enviados à pasta correspondente.

Informe `name` para localizar a pasta ou criá-la se não for encontrada; `id` é
opcional e pode ser usado para localizar diretamente uma pasta existente. Se o
ID não for encontrado e houver um nome, o script tenta localizar pelo nome.
Quando a busca não retornar uma pasta visível ao app, cria uma usando `name`,
dentro de `parent_id` se informado ou na raiz do Drive. Se houver mais de uma
pasta com esse nome, informe um ID válido ou um `parent_id` para desambiguar.
Com `drive.file`, pastas existentes que não estejam acessíveis ao app podem não
aparecer na busca e uma pasta duplicada pode ser criada.

```json
{
  "upload-drive": [
    {
      "id": "ID_DA_PASTA_DE_DESTINO",
      "name": "Backups",
      "files": "upload_files.csv"
    }
  ]
}
```

Execute o upload:

```powershell
.\environment\Scripts\python.exe .\upload_drive.py
```

Na primeira execução, o navegador abrirá para você autorizar o acesso. A sessão
é salva localmente em `token.json` para as próximas execuções. Os dois arquivos
de credenciais são ignorados pelo Git.

O escopo OAuth permanece `drive.file`: o app só consegue verificar pastas às
quais tem acesso. A busca por nome só encontra pastas visíveis ao app; se o
Drive negar a consulta, o script informa a falha e não cria a pasta. As chamadas
de leitura e criação incluem suporte a drives compartilhados. Use `--config`
para indicar outro JSON, `--credentials` para outro arquivo OAuth, `--token`
para mudar o arquivo de sessão ou `--chunk-size 16` para enviar em blocos de
16 MB.

```powershell
.\environment\Scripts\python.exe .\upload_drive.py --help
```

## Listar pastas do Drive

O script `list-folders.py` percorre as pastas e subpastas do Meu Drive e grava
um CSV com nome, ID, pasta pai, caminho, data de modificação e link. As pastas a
mapear são definidas no campo `observed-folders` do `config.json`, usando os IDs
das pastas do Drive. Cada pasta configurada inclui suas subpastas; entradas
repetidas geram linhas repetidas no CSV. Se a lista estiver vazia (ou contiver
somente o objeto modelo sem ID), todas as pastas e subpastas serão mapeadas.
Exemplo de configuração para mapear uma pasta específica:

```json
{
  "observed-folders": [
    { "id": "ID_DA_PASTA_NO_GOOGLE_DRIVE" }
  ]
}
```

```powershell
.\environment\Scripts\python.exe .\list-folders.py
```

Por padrão, o arquivo gerado é `drive_folders.csv`. Para alterar os caminhos,
use `--config`, `--output` e `--token`. Na primeira execução, autorize a leitura
de metadados do Drive no navegador; o token de leitura é separado do token
usado pelo upload.

## Testes

```powershell
.\environment\Scripts\python.exe -m unittest discover -s tests -v
```
