# LEM Server — repositório da API

Código da API em `server/`; instalação, Docker e gestão do serviço em `deploy/`. Documentação específica em `docs/`. Recursos de teste de vídeo próprios em `server/tests/fixtures/`.

Usar `../ongoing_work/` como diretório de trabalho, incluindo ambientes Python, bases de teste e logs. A partir desse diretório, com as dependências de `../Server/server/requirements.txt`, pytest, httpx e pypdf instaladas:

```powershell
$env:PYTHONPATH = (Resolve-Path ../Server/server).Path
python -m pytest ../Server/server/tests -q --basetemp=server-tests-temp
```

Os testes de vídeo necessitam de ffprobe no PATH. Os testes de alinhamento entre versões procuram o repositório irmão `App`; pode indicar outra localização através de `LEM_APP_REPO`. Esse teste é explicitamente ignorado quando o cliente não está disponível; a API e os restantes testes não dependem dele.

`shared/` é uma cópia versionada de `../Common/shared/`, para permitir implantação independente. Alterar a origem em Common e usar `python ../Common/tools/sync_shared.py --write`. Verificar e fazer commit dos três repositórios quando o contrato mudar. A instalação do servidor continua a incluir `server/`, `shared/`, `deploy/` e `.dockerignore`.

Base importada: `Lem_Qt_0.1.57_GeneralNotesAndSignaturePhoto.zip`. Nunca versionar `.env`, dados de produção, backups ou credenciais.
