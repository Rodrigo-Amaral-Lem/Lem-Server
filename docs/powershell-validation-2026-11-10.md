# Validação do deploy PowerShell

Data: 10-11-2026. Lem 0.1.33. PowerShell **7.6.6**, Python 3.12.14 e
pytest 9.2.0, num ambiente Debian 13 x86_64.

## Resultado

**14 testes passaram**, sem testes ignorados no ficheiro
`deploy/tests/test_deploy.py`. O script PowerShell, o empacotamento ZIP, a
extração Python e o fluxo Bash remoto foram executados realmente. SSH, SCP,
sudo, Docker, curl e sleep foram substituídos por simulações locais; não foi
estabelecida qualquer ligação ao servidor do utilizador.

A suite conjunta de servidor e deploy, com o Excel original, terminou com
**109 testes aprovados e zero ignorados** após a correção descrita abaixo.

| Cenário | Resultado |
| --- | --- |
| Pacote exclui configuração privada, bases de dados, backups e ambientes virtuais | Aprovado |
| Atualização conserva configuração, cria backup e retira código obsoleto | Aprovado |
| Reset elimina apenas o volume da BD deste projeto | Aprovado |
| Falha de build impede paragem/remoção dos serviços e substituição de código | Aprovado |
| Volume de outro projeto impede reset | Aprovado |
| Domínio incorreto, com instalação indicada explicitamente | Rejeitado antes do build, conservando configuração/código |
| Domínio incorreto, com descoberta automática da instalação | Rejeitado antes de qualquer comando Docker |
| ZIP com separadores Windows | Normalizado e extraído corretamente |
| Cinco casos de caminhos absolutos, travessia, ficheiros privados e duplicados | Rejeitados antes de Docker |

Na primeira execução passaram 11 dos 12 testes antigos. O teste de domínio
esperava uma inspeção Compose, mas a descoberta automática já rejeitava o
destino antes disso. Não era uma falha no deploy. O teste passou a indicar
explicitamente a pasta remota, para alcançar a validação de domínio do fluxo
Bash. Foi acrescentado um teste separado para a rejeição na descoberta
automática, conservando a cobertura dos dois percursos.

### Correção após o ensaio no Windows

A invocação no laptop revelou que `$PSScriptRoot` estava vazio durante a
avaliação do valor predefinido de `ProjectPath`. Os testes anteriores forneciam
sempre `-ProjectPath` e não cobriam essa invocação. A deteção foi movida para o
corpo do script, usando `$MyInvocation.MyCommand.Path`, sem depender da pasta
atual do terminal. O parâmetro explícito continua disponível.

Um novo teste executa `-File` sem `-ProjectPath`, a partir de outra pasta e com
espaços no caminho. O pacote é criado corretamente sem executar SSH. O ensaio
PowerShell 7 passou; Windows PowerShell 5.1 continua a exigir confirmação no
laptop. Não foi alterado o comportamento da app ou do servidor.

## Repetir as simulações

A partir da raiz Lem num ambiente POSIX com PowerShell 7 e pytest:

```sh
PWSH=/caminho/pwsh python -m pytest deploy/tests/test_deploy.py -v
```

O aviso pytest sobre `CallSpec2` vem de um plugin do ambiente de testes e não
impediu a execução. Estes testes não comprovam execução em Windows PowerShell
5.1, transporte SSH real, criação real de containers, backup/restauro PostgreSQL
real ou disponibilidade HTTPS do servidor.

## Próxima verificação no Windows

Abrir PowerShell na pasta `Lem` extraída do pacote atualizado e executar:

```powershell
$PSVersionTable.PSVersion
Get-Command ssh, scp | Select-Object Name, Source
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\deploy.ps1 -PackageOnly "$env:TEMP\lem-deploy-inspecao.zip"
```

O último comando apenas cria um ZIP local, sem SSH ou alterações ao servidor.
Permite verificar o empacotamento na versão Windows PowerShell 5.1 habitual do
Windows. A política indicada aplica-se apenas ao processo iniciado.

Se terminar com sucesso, uma atualização real, conservando os dados, usa:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\deploy.ps1
```

Esse comando já modifica o servidor e reinicia os serviços API/proxy. Executá-lo
num momento adequado, depois de verificar o pacote local. Consultar também
[deploy por PowerShell](deploy-powershell.md).
