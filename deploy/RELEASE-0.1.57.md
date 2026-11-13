# Lem 0.1.57 — Revisão de perguntas, recorte e limpeza local

## Perguntas (checklist versão 6)

- EST.04: «Indique o(s) número(s) de série do carregador (e dispensers, caso aplicável)». Fotografia opcional. Os números podem ser escritos no mesmo campo e continuam a atualizar o cadastro da estação ao fechar o report.
- SITE.02: «Verifique o estado da pintura / sinalização horizontal».
- SITE.03: «Verifique o estado da sinalética vertical (carregamento VE e números de lugares)».
- SITE.04: «Verifique o estado da proteção mecânica (apertos, retrorrefletores, estado geral)».
- SITE.02–04 exigem fotografia quando aplicáveis, conservando a regra geral de dispensa em N/A.
- MAN.12 apresenta OK, NOK e N/A, conservando o campo de valor/resultado.
- MAN.14–17 mantêm OK/NOK como avaliação separada do valor introduzido pelo técnico. Não há seleção nem preenchimento automático. Premir OK/NOK conserva uma leitura existente e não preenche uma leitura vazia. NOK exige observação. N/A dispensa a leitura e limpa a avaliação anterior; passar de N/A para OK/NOK exige introduzir novamente a leitura.
- OK/NOK isolados no campo de leitura não satisfazem MAN.14–17. A app e a API validam a conclusão independentemente. Mantém-se texto livre para valores apresentados pelo instrumento, como «>500 MΩ» ou «OL».
- A avaliação separada é guardada em `payload.answers[checkpoint].assessment`; `value` conserva a leitura e `notes` as observações. A leitura e a avaliação constam do PDF local e do servidor.

As regras de MAN.18, testes de carga, cópia QVE/medições e seleção múltipla da 0.1.56 mantêm-se. A nova checklist aplica-se a novos reports; reports existentes conservam a checklist guardada com eles, incluindo os fechados.

## Recortar fotografia no Android

A janela usa as margens da janela principal para respeitar as barras de estado e navegação. O título e o rodapé ficam dentro da área disponível, com uma folga de 8 unidades. Repor, Cancelar e Recortar têm altura mínima de 48 unidades. A geometria adapta-se à rotação e a recortes laterais sem consultar as margens do próprio popup.

## Limpar cópias locais

A causa do desaparecimento era a remoção do report completo do cache: sem um catálogo do servidor atualizado, o report podia desaparecer da lista até à atualização seguinte. A ação passa a conservar os reports, respostas, estado fechado, histórico e metadados das evidências no dispositivo. Remove apenas os ficheiros privados de evidências de reports integralmente sincronizados, sem apagar nada no servidor. Retira também a marca de manter as evidências offline; os ficheiros podem voltar a ser descarregados.

Reports ainda não confirmados pelo servidor, com alterações por enviar, fotografias por enviar, remoções pendentes ou conflitos conservam integralmente os ficheiros. A ação continua bloqueada durante transferências ou operações locais. O estado é guardado atomicamente antes de remover ficheiros. Os originais na galeria não são alterados.

Não é possível recuperar apenas a partir deste dispositivo uma cópia já removida numa versão anterior: atualize o catálogo e abra o report para o obter novamente do servidor.

## Atualizar

1. Atualizar o servidor com o pacote 0.1.57.
2. Recompilar e instalar a app 0.1.57 (Android versionCode 170).

A política de compatibilidade passa a 0.1.57. Não é necessário apagar a base de dados nem os dados da app. Conservar os dados da app é especialmente importante se existirem reports por sincronizar.

## Validação

- API: validação de leitura independente de OK/NOK, observações NOK, N/A, fotografias SITE, números de série múltiplos sem fotografia e exportação PDF com avaliação.
- QML real: digitação e mudança de avaliação sem substituir a leitura; escolha N/A; MAN.12; recorte em retrato/paisagem e margens simuladas das quatro bordas; arrastar/aplicar/cancelar; navegação dos reports.
- C++: conclusão local, limpeza de report fechado confirmado versus pendente e conservação das respostas após reiniciar.
- Integração com API descartável: sincronização e reabertura após limpeza, incluindo evidências.

Os testes de áreas reservadas simulam as barras do sistema em Qt; a confirmação num telefone Android real continua necessária.
