# Modelo de dados e importação

## Entidades

| Tabela | Função |
|---|---|
| `users` / `sessions` | Perfis admin/técnico, passwords scrypt, tokens aleatórios guardados como SHA-256, validade 30 dias |
| `contracts` | Empresa, ano, semestre, tipo e nome composto único |
| `assignments` | Contratos atribuídos a cada técnico |
| `chargers` | UUID interno, contrato, ID externo e dados técnicos/metadados JSON |
| `reports` | UUID gerado no dispositivo, carregador, autor, revisão, estado, respostas, datas e snapshots |
| `photos` | UUID, report, checkpoint, nome, MIME, SHA-256 e bytes originais |
| `import_profiles` | Mapeamento mais recente por empresa |
| `imports` | Histórico de imports, hash do ficheiro, mapeamento e contagens |
| `audit` | Autor, operação, entidade e data |

A chave única do catálogo é `(contract_id, external_id)`. Um ID de empresa A pode coincidir com um da empresa B. O carregador do contrato do segundo semestre não é automaticamente considerado concluído pelo report do primeiro. Reimportar atualiza o catálogo do mesmo contrato sem criar duplicados; não elimina carregadores que tenham deixado de aparecer no Excel. Qualquer alteração do ID externo cria outro registo: uma renomeação terá de ser resolvida pelo administrador.

Várias intervenções por carregador são permitidas, incluindo correções e novas visitas. A lista apresenta os reports existentes e respetivo estado; não assume que um rascunho seja manutenção concluída. Nesta versão, os itens de site e QVE são preenchidos por report do carregador. Não há ainda um report de site partilhado entre vários carregadores.

Cada report conserva `snapshot` com dados do carregador/contrato e `template` com os checkpoints/versionamento à criação. Atualizações posteriores do catálogo ou do modelo de checklist não alteram os reports anteriores.

## Checklist inicial

Fonte: PDF de manutenção anexado pelo utilizador. Grupos e contagens:

| Grupo | Itens |
|---|---:|
| PC01 — Estado físico | 13 |
| PC02 — Manutenção carregador | 21 |
| PC03 — Testes de carga | 6 |
| 00 — Segurança | 8 |
| PG01 — Site | 5 |
| PG02 — QVE | 12 |
| PG03 — Correção de não conformidades | 1 |
| PG04 — Informações gerais | 3 |
| Total | 69 |

Mantêm-se os checkpoints específicos ABB/Atlante e Longship do exemplo. Devem ser assinalados NA quando não se aplicam. As tolerâncias/valores nos rótulos vêm do exemplo e são apresentados para registo; a app não calcula nem certifica conformidade a partir das medições. Na próxima iteração, validar os textos com a equipa e associar versões/perfis de checklist a cada tipo de contrato/cliente, incluindo diferenças AC/DC.

Fotografia permitida em todos os itens. Obrigatória, exceto NA: fotografias iniciais, sete medições de tensão, delimitação da área, QVE aberto e panorâmica final. Os outros itens que tinham fotografias no exemplo admitem evidências, mas a mera presença de uma foto nesse exemplo não foi interpretada como obrigação universal. Para fechar: todos os itens respondidos, observação em NOK e evidências obrigatórias presentes.

Datas são guardadas em ISO 8601/UTC. A app envia o início/fim registados no dispositivo, mesmo offline; `updated` e audit usam o relógio do servidor. O PDF apresenta hora de Portugal (`Europe/Lisbon`). A exatidão do início/fim depende do relógio do dispositivo.

## Importação Excel

### Modelo Stations fornecido em 19-10-2026

A app/API reconhecem o cabeçalho `Station ID`, selecionam a folha `Stations` e preenchem automaticamente o mapeamento. A tab `Listas` é auxiliar e não é importada. As colunas A:N e os seus cabeçalhos são verificados. Não é necessário executar as macros do XLSM.

| Coluna | Cabeçalho | Obrigatório por carregador |
|---|---|---|
| A | Station ID | Sim |
| B | Morada | Sim |
| C | Código Postal | Não |
| D | Latitude | Não |
| E | Longitude | Não |
| F | CCDR | Sim |
| G | CPE Associado | Não |
| H | Fornecimento de Energia | Sim |
| I | Nº(s) Série | Sim |
| J | Fabricante | Sim |
| K | Modelo | Sim |
| L | Potência (kVA) | Sim |
| M | Limitação de Potência (kVA) | Sim |
| N | Tomadas / Fichas | Sim |

Uma linha com dados e qualquer atributo obrigatório em branco produz um erro com a célula (ex.: `F6`) e bloqueia toda a importação. Linhas A:N completamente vazias são ignoradas. O valor zero em potência/limitação é um valor preenchido; não é tratado como vazio. A validação exige preenchimento, sem inventar restrições elétricas adicionais.

Séries compostas e tomadas repetidas (ex.: `CCS2, CCS2`) são conservadas como texto. Potência e limitação mantêm a unidade kVA. Os atributos opcionais, quando preenchidos, ficam disponíveis nos dados do carregador e no snapshot dos novos reports. O PDF inclui também CCDR, fornecimento, limitação, tomadas, código postal e coordenadas quando disponíveis. Os dados de reports já criados não são alterados.

Não há mudança nas tabelas: os novos atributos usam `chargers.details` e os snapshots JSON existentes. A API aplica os atributos obrigatórios mesmo que um cliente omita o mapeamento desses atributos. Os formatos genéricos anteriores continuam suportados; as dez regras por posição são próprias do perfil Stations.

### Outros formatos e regras comuns

A API analisa até 12 linhas iniciais para apresentar amostras. A app mapeia cabeçalhos até à linha 12; a API suporta até à linha 100. Não avalia macros/fórmulas e nunca executa o Excel. `.xls` antigo e CSV não estão incluídos nesta versão.

1. Criar/selecionar contrato.
2. Abrir ficheiro e escolher folha/linha de cabeçalho.
3. Mapear ID obrigatório e os dados disponíveis (modelo, fabricante, série, localização, morada, CPE, potência, site).
4. Pré-visualizar IDs e dados. Verificar especialmente zeros à esquerda.
5. Corrigir erros no ficheiro/mapeamento e confirmar.

IDs vazios, acima de 200 caracteres ou duplicados bloqueiam toda a importação. Cabeçalhos duplicados/ambíguos também bloqueiam o mapeamento. Não há correspondência automática por semelhança. As colunas originais ficam no JSON `source`, mesmo sem mapeamento.

IDs e séries em texto preservam zeros. Formatos numéricos simples como `000000` também são respeitados nos identificadores mapeados. Outros formatos personalizados, fórmulas sem resultado em cache e ficheiros com títulos/linhas de subtotal complexos precisam de avaliação quando chegarem os exemplos reais. A importação é transacional; previews não gravam carregadores.

Formato de mapeamento/API:

```json
{
  "sheet": "Parque",
  "header_row": 1,
  "columns": {
    "external_id": "Identificador",
    "model": "Modelo",
    "serial": "Série",
    "location": "Local",
    "cpe": "CPE"
  }
}
```

Para colunas especiais, a API também suporta chaves `extra.<nome>`; a UI usa os atributos comuns e preserva automaticamente todos os outros em `source`.

## Sincronização

Uma app envia UUID/revisão base e respostas. O servidor verifica contrato, autor e revisão. A gravação usa UPDATE condicionado pela revisão, impedindo sobreposição silenciosa. Fotografias têm UUID estável e hash: repetir o mesmo upload não cria duplicados. Uma resposta de rede perdida pode ser recuperada quando os dados/revisão/autor do servidor confirmam a mesma operação; caso contrário é conflito.

Ordem: guardar rascunho no servidor → enviar fotografias pendentes → fechar, quando pedido. Se cair a rede, manter dados e UUIDs no dispositivo e reenviar. Um fecho central concluído não pode ser alterado. Um técnico pode consultar reports dos colegas nos contratos atribuídos; a edição é limitada ao autor (ou admin pela API).

Sem ligação: é possível escolher contratos/carregadores já descarregados e continuar reports locais. Os reports de outro dispositivo precisam de ter sido abertos/descarregados anteriormente para os consultar offline. O botão Atualizar renova o catálogo; abrir um report sincronizado com sessão ativa descarrega a versão central. Um rascunho pendente é aberto da cópia local para conservar o trabalho.
