# Validação da importação com o Excel original

Em 10-11-2026 foi usado o ficheiro original `Import_Sheet_V0-2(1).xlsm`,
fornecido pelo utilizador, para executar os testes de importação da Lem 0.1.33.
O Excel não é incluído no pacote de código. O original foi apenas lido e o seu
SHA-256/tamanho foram confirmados antes e depois dos testes. Não foram executadas
macros, calculadas fórmulas ou guardadas alterações nesse ficheiro.

## Resultado

**14 testes de importação passaram.** Após executar também o PowerShell, a suite
conjunta de servidor e deploy ficou com **109 testes aprovados e zero ignorados**.
Não foi necessário alterar o comportamento da app ou do servidor.

| Verificação | Resultado |
| --- | --- |
| Reconhecimento automático de Stations e cabeçalho | Aprovado |
| Importação dos 51 IDs e preservação dos atributos verificados | Aprovado |
| Cada uma das dez colunas obrigatórias em falta numa cópia temporária | Rejeição correta, indicando a célula |
| Colunas opcionais vazias | Aceites |
| Limitação de potência igual a zero | Preservada como valor presente |
| Tentativa de omitir validações por remapeamento | Rejeitada |
| Importação inválida para uma BD temporária | Rejeitada sem alterações parciais |
| Primeira importação válida | 51 criados |
| Reimportação do mesmo ficheiro | 0 criados, 51 atualizados, sem duplicação |

Os avisos de openpyxl sobre extensões de validação de dados referem-se à sua
representação em memória. O ficheiro original não foi exportado/regravado, pelo
que não perdeu validações. Os outros avisos são de depreciação das bibliotecas
de testes; não causaram falhas.

## Repetir

A partir da raiz Lem, com as dependências do servidor e pytest instalados:

```sh
STATIONS_TEST_WORKBOOK="/caminho/Import_Sheet_V0-2(1).xlsm" CHECKLIST_FILE="$PWD/shared/checklist-v1.json" PYTHONPATH="$PWD/server" python -m pytest server/tests/test_stations_import.py -q
```

Cada teste usa cópias em memória e, quando necessário, SQLite temporário. Não
contacta o servidor real. As expectativas de alguns testes pertencem a este
conjunto de 51 IDs, não a qualquer Excel arbitrário com o mesmo formato.

## PowerShell validado em simulação

Os 14 testes de `deploy/tests/test_deploy.py` passaram com PowerShell 7.6.6 num
ambiente POSIX. Executam Bash/Python e simulam SSH/SCP/sudo/Docker. Não fazem deploy
real nem apagam dados reais. Não se deve assumir que correm diretamente no Windows
PowerShell 5.1 do laptop: esse ambiente requer validação própria. O acesso ao
laptop não foi estabelecido nesta revisão. Ver os resultados e comandos para a
verificação Windows na [validação PowerShell](powershell-validation-2026-11-10.md).

Esta validação complementa a revisão de segurança. A pendência dos patches do
Qt 6.12.0 continua descrita no [relatório de segurança](../../Common/docs/security-review-0.1.33.md).
