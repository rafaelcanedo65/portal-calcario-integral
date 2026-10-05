# Guia do desenvolvedor — Portal Calcário Integral (protótipo)

Portal web interno da Calcário Integral LTDA (calcário agrícola, gesso, sulfato): logística de
retirada de pedidos, CRM comercial dos vendedores e, no futuro, integração com o NetSuite (ERP).

Este pacote é o **protótipo** (`portal_redesign`), a versão mais avançada. A empresa usa hoje uma
versão mais antiga (`portal`, não incluída aqui). Levar o que está aqui para produção é uma das
pendências (seção 7).

---

## 1. Rodar localmente (Windows)

Requer Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.crm_seed                       # cria o banco com dados FICTÍCIOS
uvicorn app.main:app --reload --port 8422
```

Abra http://localhost:8422.

- **Usuários criados pelo seed** (senha de todos: `troque-esta-senha`): `admin`, `logistica`,
  `financeiro`, `ana.exemplo`, `bruno.exemplo`, `carla.exemplo`.
- **Login:** toda entrada pede usuário e senha. O login automático e o atalho `?como=` do
  desenvolvimento foram removidos em 02/10/2026.
- O banco é SQLite (`portal.db`), criado na pasta de onde o servidor roda.

## 2. Dados

- **Dados reais de clientes não vão neste pacote** (LGPD: nomes, telefones, CPF/CNPJ). Use o seed.
- Os scripts de importação real continuam no código, mas dependem de arquivos que não vão no
  pacote: `app/import_data.py` (planilha de pedidos do NetSuite) e `app/crm_import_real.py`
  (`crm_dados_reais.json`). **Nunca rode `crm_import_real` num banco em uso**: ele apaga e reimporta
  todo o CRM.
- **Logística vem da planilha "Expedição — o que falta retirar"** (Google Drive, gerada do NetSuite pela equipe
  da expedição, desde 01/01/2026), até existir a integração direta. Importador: `python -m app.import_expedicao
  <arquivo> [--gravar]`, com o texto da planilha exportado pelo conector do Drive. O texto vem "achatado" (célula
  vazia some, número com vírgula sem aspas), então cada linha é lida por âncoras e os totais de cada aba são
  conferidos com o cabeçalho antes de gravar. A planilha traz, por pedido, total, faturado (= carregado), saldo e
  **data da última retirada**, não cada carregamento. A lista da Logística mostra só as abas que a expedição
  trabalha (Expedição, Parado e Outros produtos); "Saldo abaixo de 5%" e "Finalizados" ficam no histórico do
  cliente. Pedido que some da planilha vira "Fora da planilha" (nada é apagado).
- **A equipe da Logística anota no portal** (decisão de 03/10/2026). Pedido anotado no portal
  (`anotado_no_portal_em`) nunca tem situação, comentário ou data limite mexidos pela importação; nos demais
  (transição) vale a anotação mais nova da aba Anotações. O histórico da aba "Histórico anotações" entra uma vez em
  `pedido_anotacoes` (origem "planilha"; quando só o antes ou só o depois estava preenchido, o texto exportado não
  diz qual dos dois, então fica em `registro`, sem inventar).
- Contratos de cessão de crédito e comprovantes de pagamento enviados pelo sistema ficam em `uploads/`
  (fora de `static/`, servidos só por rota autenticada). Essa pasta não entra no git nem no pacote.

## 3. Stack e estrutura

FastAPI + Jinja2 + SQLAlchemy + SQLite. HTML/CSS/JS puro, sem build de frontend.

| Arquivo | O que tem |
|---|---|
| `app/main.py` | Login, logística, pedidos, admin de usuários |
| `app/crm_routes.py` | Todo o CRM: funil, ficha do cliente, propostas, pedidos, filas, avisos, relatórios |
| `app/models.py` | Modelos e constantes de negócio (fases, cores, opções) |
| `app/relatorios.py` | Página `/relatorios`: catálogo dos 16 relatórios, filtros, escopo por perfil e Excel |
| `app/auth.py` | Sessão, senha, `require_role()` |
| `app/templates/`, `app/static/style.css` | Telas no padrão do Integral Design System (Inter, marinho #33247A + amarelo #FCEC0A, ícones Lucide 1.48.0 via unpkg). Casca em `base.html`, menu por perfil em `app/menu.py` |
| `app/netsuite_client.py` | Cliente OAuth 1.0 (TBA) + SuiteQL, ainda não ligado nas telas |

Papéis: `admin` (inclui o balcão de vendas), `logistica`, `vendedor` e `financeiro` (só a tela
`/financeiro/pagamentos`, em `app/financeiro_routes.py`, e os relatórios do setor; não busca nem abre ficha de cliente). O vendedor só vê a própria
carteira (`_cliente_do_usuario` em `crm_routes.py`).

**Dois modelos de pedido, de propósito:** `Pedido` (logística, vem da planilha/NetSuite) e `PedidoCRM`
(nasce de uma proposta no CRM). Não misture: quando a integração com o NetSuite existir, é ali que os
dois se encontram.

## 4. Regras de negócio (combinadas com o Rafael)

**Funil do cliente:** `a_contactar → contactado → proposta → realizado`, mais os desfechos `perdido`
e `nao_usara`. **Ninguém move a etapa na mão:** ela avança sozinha pelo que é registrado (contato,
proposta real, pedido). Propostas e vendas só entram pela aba Proposta, com produto, volume, preço e
pagamento. Registrar "enviei proposta" só por comentário é bloqueado, porque criava "propostas
fantasma".

**Pagamento:** à vista, a prazo ou plano safra. Plano safra pode ser direto ou por cessão de crédito;
na cessão, o parceiro precisa estar cadastrado (tela Parceiros de Plano Safra) e o contrato assinado
pelas 3 partes é obrigatório para gerar o pedido.

**Venda via transportadora/consultor (destino final):**
- O cadastro tem duas caixas, Consultor e Transportadora. Nenhuma marcada = produtor.
- O pedido é faturado para o intermediário e o vendedor registra para quais clientes finais o
  produto vai (`PedidoDestinoFinal`). Um pedido pode ser rateado entre vários clientes, sem precisar
  fechar 100% do volume.
- O cliente final vai para Realizado, ganha a compra na época de compra e no volume, e o pedido
  aparece na aba de pedidos dele.
- **Pedido cancelado:** o cliente final só continua em Realizado se sobrar outra compra real com a
  empresa (`_tem_compra_ativa`). Senão, volta para a etapa em que estava antes.
- Se o cliente final for de outra carteira, o vendedor que fez a venda consegue abrir a ficha dele
  **só para o pós-venda**: registrar contato, sem proposta, pedido, cadastro nem agenda. O dono da
  carteira recebe um aviso.

**Avisos:** caixa de notificações com lidos e não lidos. Gera aviso quando alguém cadastra cliente
na carteira de outro vendedor, e quando um cliente da carteira recebe produto (ou tem o pedido
cancelado) por venda de outro vendedor.

**Fila de trabalho**, em ordem de prioridade (`_motivo_prioritario` e `_montar_fila_trabalho`):

| # | Motivo |
|---|---|
| 0 | Pedido com prazo de retirada vencido |
| 1 | Retirada exigindo 250 t/dia ou mais para cumprir o prazo |
| 2 | Pedido em aberto com cliente de volta à prospecção |
| 3 | Proposta aberta com cliente "quente": fica até ter resultado |
| 4 | Época de compra no mês atual ou no próximo. Depois do contato volta em 3, 7 ou 14 dias (quente, morno, frio) |
| 5 | Proposta sem retorno (conta a partir do último contato) |
| 6 | 60 dias sem contato, 10 por vez |
| 7 | Primeiro contato (lead ou cliente novo), 15 por vez |

**Fila de atualização de cadastro:** cadastros incompletos, sem telefone primeiro. **Área plantada é
obrigatória:** é a base do cálculo de market share.

**Entressafra (dezembro a março):** poucos compram. A tela inicial destaca contato a cada 60 dias,
cadastro e busca de clientes novos, e a fila de cadastro sobe para cima.

**"Registrar contato" orientado:** o formulário muda com a etapa do cliente (opções, campos e
sugestão de conversa). Exemplos: cliente em Realizado recebe roteiro de pós-venda; proposta aberta
recebe roteiro de retorno da proposta. Ver `_orientacao_contato` e `_formulario_contato`.

**Ciclo de vendas:** todo 1º de novembro a carteira volta para `a_contactar` (`_executar_reset_ciclo`),
menos quem está em Proposta.

**"Voltar para ..." no topo (02/10/2026):** no lugar do caminho inteiro (Início › Carteira › ...), a
tela mostra só o último nível com link das `migalhas`, como "Voltar para Fila de trabalho". As rotas
continuam montando `migalhas` normalmente; `base.html` escolhe o link. Quem abre um cliente pela fila
ou pelos avisos volta para a lista (parâmetro `?via=`).

**Caminho da venda na ficha (02/10/2026, estilo rastreio dos Correios):** um trilho por negócio,
como cada encomenda tem o seu rastreio. O contato fica no topo (é do cliente); cada proposta aberta e
cada pedido em andamento ganham uma linha com o próprio trilho e o botão do próximo passo; os pedidos
concluídos no ciclo (desde o último 1º de novembro) ficam recolhidos. À vista: Proposta › Pedido ›
Pagamento › Carga › Fim. A prazo e plano safra: Proposta › Pedido › Carga › Pagamento › Fim. Cálculo
em `_caminho_venda`, desenho em `templates/_caminho_venda.html`; substituiu a barra "Próxima ação"
(o motivo da fila aparece no topo do caminho). **Gerar pedido não é mais página separada:** o
formulário abre embaixo da proposta, no caminho; a rota GET antiga só redireciona para a ficha com
`?abrir=gp-{id}`, e um erro de preenchimento volta com o formulário aberto e preenchido.

**Pedido a prazo (02/10/2026):** carrega direto, sem aprovação do financeiro. No Gerar pedido o
vendedor escolhe como o cliente paga (`PedidoCRM.forma_prazo`): boleto após a 1ª carga (30 ou
30/60/90 dias; se a 1ª parcela vencer sem pagamento, o carregamento é bloqueado na portaria), sobre
rodas (paga cada carga antes da próxima) ou por período (puxa X dias e paga depois). A prazo paga o
que retira (`condicao_pagamento = por_retirada`). O bloqueio automático depende das cargas e dos
pagamentos, que virão do NetSuite; por enquanto a regra só é mostrada (`regra_prazo()`). Decisão do Rafael:
não haverá registro manual de cargas e pagamentos — o bloqueio entra junto com a integração.

**Plano safra (02/10/2026):** direto com o cliente ou via empresa parceira (cessão). Nos dois casos o
cliente carrega agora e paga numa data combinada, normalmente longa (`vencimento_pagamento`,
obrigatória e futura no Gerar pedido); via parceiro exige também o contrato assinado pelas 3 partes.
Paga o que retira (`por_retirada`). Texto da regra em `regra_pagamento()`.

**Caminho, detalhes (02/10/2026):** a etapa "Carregamento" mostra dentro da bolinha o % já carregado
(`volume_retirado` ÷ volume a entregar; o parcial virá do NetSuite — sem dado, não mostra número); ao
finalizar vira o check. Cliente perdido ou "não usará" tem a linha "Negociação encerrada" com o trilho
Contato › Proposta › Perdido/Não usará (mostra se a venda parou antes ou depois da proposta), motivo,
data, reavaliação e o botão "Cliente voltou a negociar".

**Pedido do mesmo produto em andamento (02/10/2026):** ao gerar um pedido de um produto que o cliente
já tem em andamento, o vendedor precisa responder se é "compra a mais" (gera e registra no histórico
que é adicional) ou "nova negociação do mesmo pedido" (não gera; leva ao Renegociar do pedido
existente). O servidor também recusa sem a confirmação (`confirma_adicional`). Motivo: a mesma venda
não pode virar duas.

**Contato que não funciona (02/10/2026):** no Registrar contato há "Número errado / sem WhatsApp", com o
motivo (os mesmos da planilha: número errado, desativado, não recebe ligações, sem WhatsApp, atende outra
pessoa). Não muda a etapa nem agenda retorno; marca `ClienteCRM.precisa_ajuda`/`motivo_ajuda`. "Não
atendeu" também marca quando o vendedor pede ajuda ou na 3ª tentativa seguida (`TENTATIVAS_SEM_RETORNO_AJUDA`).
Esses clientes saem da fila do vendedor e entram no topo da fila do admin ("Contato a resolver"); na
ficha o admin informa novo telefone e recado (`POST /crm/cliente/{id}/contato-resolvido`), o que encerra o
caso, põe o cliente na agenda do vendedor e o avisa. Uma conversa registrada também encerra o caso.

**Formulário guiado (02/10/2026):** formulários com `data-guiado` apontam o próximo campo obrigatório
vazio depois de cada preenchimento e, com tudo preenchido, o botão de confirmar (`guiarProximo` em
`base.html`). Hoje: Gerar pedido, nova proposta, editar proposta e renegociar pedido. Os campos que
aparecem conforme o pagamento (prazo, modalidade, parceiro) só ficam obrigatórios quando visíveis
(`atualizarCamposPagamento`), e é isso que leva o guia até eles.

**Cadastro rápido de parceiro (02/10/2026):** na lista "Parceiro da cessão" há "+ Cadastrar parceiro",
que abre nome e CNPJ ali mesmo e salva por trás (`POST /crm/parceiros/rapido`, JSON), sem perder a
proposta em preenchimento. **CNPJ obrigatório** (com conferência dos dígitos verificadores e sem repetir
entre parceiros; os antigos sem CNPJ aparecem como "CNPJ pendente" em Parceiros para o admin completar).
Vendedor também pode cadastrar; mesmo nome (sem diferenciar maiúsculas) ou mesmo CNPJ reaproveita o
cadastro existente; desativado pede reativação ao admin, que continua cuidando da página
Parceiros.

**Histórico de pedidos compacto (02/10/2026):** cada pedido é um `<details>` com o resumo no mesmo
formato da proposta no histórico (produto + Nº, grade Volume/Preço/Pagamento/Retirado e Total); clicando, abre tudo, inclusive Renegociar, Finalizar e
Cancelar. Depois de salvar, a âncora `#pedido-{id}` abre o cartão sozinha (`base.html`), e `irPara`
abre o cartão antes de mostrar um formulário de dentro dele.

Regra geral das telas: todo clique que abre algo leva o usuário até lá (`levarAte` em `base.html`).

**Ficha do pedido na Logística e Início do admin (03/10/2026)** — mesmo conceito da ficha do cliente no CRM.
- Clicar no cliente (lista ou fila) abre, embaixo da linha, a ficha do pedido (`_anotacao_logistica.html`):
  telefone do NetSuite (link `tel:`), etapas (`expedicao.etapas_pedido`: emitido → pagamento, só quando se sabe →
  prazo combinado → carregamento X% → finalizado), **próximo passo** com os botões certos (`expedicao.proximo_passo`,
  em ordem: encerrado, finalizado, vendedor respondeu, prazo vencido, aguardando pagamento, sem data, aguardando
  vendedor, parou de retirar, retirada apertada, em dia), conversa com o vendedor (responde ali) e anotação.
  Os botões mandam só o campo deles; o formulário completo declara `campos=` para poder limpar (campo vazio de
  formulário chega como None). Depois de salvar, volta com `?abrir=<id>` e a ficha abre de novo.
- Telefone: `Pedido.telefone`, lido da planilha e formatado por `formatar_telefone` (sem DDD vira "99162-4858 (sem
  DDD)"; estrangeiro fica como veio — nunca inventa DDD).
- **Início do admin** (`/admin/inicio`, `app/admin_inicio.py`; o admin entra por aqui): o que só ele resolve (contatos
  a resolver, pagamentos, parceiros sem CNPJ, clientes sem vendedor no portal), como está a equipe (carteira,
  trabalhados em 30 dias com a conta do Desempenho, retornos atrasados, propostas paradas, último registro no portal
  — números levam à lista de clientes nos relatórios), Logística e números de vendas da empresa.
  **Em uma tela, sem rolagem (04/10/2026, "muito extensa")**: saudação + "N assuntos para resolver agora" numa linha;
  faixa de 4 blocos de uma linha (número + assunto, explicação no `title`; zerado vira "✓"); embaixo, 2 cartões lado a
  lado (Logística e Vendas; o quadro "Clientes por estado" saiu em 04/10) e, por último (pedido do Rafael), a Equipe na largura toda, em 2 tabelas
  lado a lado (metade dos vendedores em cada, na ordem de atenção; colunas de largura fixa, nome com "…" e nome completo
  no `title`). Medido sem rolagem em 1366×657 (área útil de notebook com o navegador) e 1920×950; abaixo de 1180 px
  empilha (Equipe vira uma tabela); no celular a tabela rola de lado dentro do cartão. Classes `ai-*` no CSS.
  **Tarefas = prioridade (04/10/2026, "não estão como prioridade, apenas mais uma informação")**: o topo é o único bloco
  colorido (cor da marca) e leva a saudação; cada tarefa é um cartão clicável com número, verbo da ação ("Conferir
  pagamento", "Resolver contatos", "Completar CNPJ", "Ver carteiras sem dono") e o que ela trava; borda vermelha =
  trava a operação, âmbar = alguém parado; ordem fixa por urgência (`peso` em `admin_inicio.py`; tarefa nova é só
  acrescentar ali). Zerada sai dos cartões e vai para "em dia". Idade só onde há data real (pagamento: comprovante
  mais antigo esperando).
  **Fila de trabalho e Equipe compacta (04/10/2026)**: o painel do topo de todos os Inícios tem o título "Fila de
  trabalho · N tarefas" (`painel_tarefas(..., titulo=)`) e, a pedido do Rafael, **sem o fundo azul**: o destaque fica
  nos cartões de tarefa (os únicos cartões brancos da página, borda vermelha/âmbar) e no cartão vermelho de prioridade
  máxima. A Equipe virou lista em colunas (`.ai-vendedores`, CSS
  `column-count` 3 em ≥1600 px, 2 até 900 px, 1 no celular), uma linha por vendedor: bolinha, nome, % trabalhada,
  "N atras.", "N prop.", último registro; zero não aparece e o significado fica no `title`. Desde 04/10 a Equipe fica
  **fechada por padrão** (`<details id="equipe-det">`): uma linha de resumo sem nomes (`_resumo_equipe`: vendedores,
  online, % trabalhada, retornos atrasados, propostas paradas, quantos nunca usaram) e "Ver vendedores" abre a lista;
  o navegador lembra a escolha (localStorage "inicio-equipe-aberta") e o número de online atualiza a cada minuto.
  **Botão em cada tarefa (04/10/2026)**: como o "Conferir agora", todo cartão tem o verbo da ação + "agora" (campo
  `botao`: admin em `admin_inicio.py`, Logística em `VERBO_LOG`, vendedor em `VERBO_FILA`, Financeiro em `inicio.py`);
  vermelho quando trava a operação, roxo nas demais. No máximo 3 cartões por Início (o resto vai para o rodapé "e
  mais…"). Notebook ou maior: botão à direita, título até 2 linhas; abaixo de 1280 px: botão sempre embaixo do texto.
  **Blocos de apoio como ficha de dados (05/10/2026, "no estilo da proposta no histórico, clean e organizado")**:
  Logística, Vendas, Equipe e os equivalentes dos outros Inícios são fichas brancas com cabeçalho (nome + link,
  linha tracejada embaixo) e números em colunas separadas por linha fina: rótulo pequeno, valor em destaque,
  complemento curto. Sem sombra e sem fundo ao passar o mouse. A Equipe fechada mostra a mesma ficha (Vendedores,
  Online agora, Carteira trabalhada, Retornos atrasados, Propostas paradas, Nunca usaram) com "Ver vendedores" no
  cabeçalho. Em 900–1179 px os Inícios de 2 cartões ficam lado a lado com números 2×2 e "Comece por aqui" mostra 3.
  **Só as tarefas têm peso visual (04/10/2026, "tire essa cara de botão ... tudo isso é secundário")**: Logística,
  Vendas, Equipe (e os blocos equivalentes dos outros Inícios) ficam sem caixa — só uma linha separando, título pequeno
  em cinza maiúsculo, números de 16 px, sem fundo ao passar o mouse; o painel roxo de tarefas é o único bloco com
  destaque. As larguras das colunas da Equipe valem também nas células (empilhada, a 2ª metade esconde o cabeçalho e
  desalinhava). Medido sem rolagem em 1100×760, 1366×657 e 1920×950.
  **Vendido, expedido e falta entregar (04/10/2026)** — `expedicao.volumes(db)`, mesma base nos dois cartões (planilha do
  NetSuite, safra = pedidos desde o início do ciclo): Vendas mostra "Vendido na safra" (soma de `quant_total`);
  Logística troca "Sem data limite" por **Expedido** (soma de `faturado`, tudo que já foi entregue) com barra de % e
  **Falta entregar** (saldo dos pedidos em aberto, `query_em_aberto`). Saldo de pedido finalizado não entra no "falta"
  (virou crédito ou foi encerrado), por isso vendido ≠ expedido + falta. % = expedido ÷ (expedido + falta). O cartão
  da Logística (5 números) é mais largo que o de Vendas (`.ai-topo-54`). O Início da Logística mostra os mesmos dois.
  **Mesma lógica em todos os Inícios (04/10/2026)** — `app/inicio.py` + `templates/_inicio.html` (macros
  `painel_tarefas`, `saudacao_topo`): saudação na barra do topo (`{% block topo_titulo %}` do base.html), tarefas como
  único bloco colorido, números neutros (`.ai-topo3.ai-topo2`), lista curta "Comece por aqui"/"Últimos movimentos"
  (`.ai-linhas`, colunas por `--cols`). Logística entra em `/logistica/inicio` (tarefas = níveis da fila, na ordem
  da página Regras, com verbo; "Comece por aqui" = primeiros da fila; "Não carregar" = à vista sem pagamento);
  Financeiro em `/financeiro/inicio` (planos safra vencidos, registrar recebimentos; tempo médio de conferência dos
  comprovantes em 30 dias; recebido no mês, a receber, comissão liberada; últimos movimentos); vendedor em
  `/vendedor/crm` (motivos da fila com verbo, `VERBO_FILA` em crm_routes; cadastros; seus números; comissão).
  **Pagamento à vista = PRIORIDADE MÁXIMA** ("os caminhões dependem da liberação"): é uma corrente e cada Início mostra
  o seu elo como cartão vermelho "Prioridade máxima" — vendedor: enviar comprovante (recusado primeiro, depois
  diferença, depois sem comprovante; `pedidos_sem_pagamento_do_vendedor`); admin e financeiro: conferir (o que espera
  há mais tempo, com cliente, valor e há quanto tempo); Logística: pagamento confirmado nos últimos 3 dias e ainda sem
  carregamento (`liberados_para_carregar`, confere a última retirada na planilha). Admin e financeiro veem também a
  faixa vermelha em **todas** as páginas enquanto houver comprovante esperando (`menu.urgente`), menos no Início e em
  Pagamentos. Teste: `testar_inicios.py`.
  **Presença (04/10/2026)**: bolinha verde/cinza antes do nome na Equipe (`app/presenca.py`). Online = sinal nos
  últimos 3 min e não saiu depois: qualquer página (`get_current_user`, no máximo 1 gravação/min), o "estou aqui" do
  `base.html` a cada 2 min com a aba visível (`POST /api/presenca`) e o login; `logout` desliga na hora. O Início
  atualiza as bolinhas a cada minuto (`GET /api/equipe-online`). Sem registro ainda: "sem acesso registrado desde
  04/10/2026" (não dizer "nunca entrou": a presença começou nesse dia).
- Nome do vendedor: a carteira do CRM e o usuário precisam ter **exatamente** o mesmo `vendedor_nome` (a comparação é
  exata). "Sidney Passos de Sousa" e "Wagner Souza dos Santos" estavam diferentes só nas maiúsculas e os dois não
  viam a própria carteira; no protótipo o usuário (e os pedidos/avisos) foram alinhados à grafia do CRM.

**Prioridade da Logística e pedidos por região (03/10/2026)** — "carregar sem apertar e sem esquecer nenhum pedido".
- `expedicao.nivel_fila` põe cada pedido em aberto no primeiro nível que se aplica (ordem = urgência): vendedor
  respondeu → prazo vencido → retirada apertada (> `TON_DIA_APERTADO` = 250 t/dia ou até `DIAS_PRAZO_APERTADO` = 7 dias)
  → cobrança sem resposta (`DIAS_COBRANCA_SEM_RESPOSTA` = 3) → parou de puxar / nunca puxou (`DIAS_SEM_PUXAR` = 15,
  escolha do Rafael) → sem data limite. Fora de todos = em dia. A fila (`/logistica/fila`) e o próximo passo da ficha
  usam a mesma regra. Pedido à vista esperando pagamento não entra em "parou/nunca" (não pode carregar).
- Indicadores (`expedicao.indicadores`): carga necessária por dia (soma do t/dia dos pedidos com prazo em dia), sem
  prazo, sem nenhuma ação há 30 dias (sem retirada, anotação nem conversa) e quantos por nível.
- Região (`/logistica/regiao`, `app/logistica_regiao.py`, `app/geo.py`): "caminhão indo para [cidade] num raio de
  [km]" (linha reta até o local de cada pedido), mapa (com as duas plantas) e tabela UF → cidade. Coordenadas dos
  municípios: `static/municipios_coordenadas.json` (IBGE via github.com/kelvins/municipios-brasileiros).
- **Mapa = OpenStreetMap via Leaflet 1.9.4** (unpkg, com SRI). Fazenda com ponto exato vira alfinete; pedido só com
  cidade entra numa bolha por cidade. Os tiles do OSM têm política de uso justo: serve para uso interno; se o uso
  crescer muito, trocar a URL dos tiles por um provedor pago.

**Menu lateral por setores (04/10/2026)** — `menu._grupos` devolve `(topo, rodape)`: itens soltos e setores
(`_setor`, abrem e fecham). Rafael ("quanto menos botão melhor", referência com submenu em árvore e versão só ícones):
- Admin: Início · Vendas ▸ · Logística ▸ · Financeiro ▸ · Relatórios · (separador) Administração ▸.
  Vendedor: 8 itens (Pedidos ▸ junta "Do portal" e "Por cliente (NetSuite)"). Logística 4, Financeiro 3
  (Pagamentos a confirmar, Recebimentos, Relatórios).
- **"Mapa do CRM" saiu do menu (04/10/2026, "ficou avulsa")**: o mapa por estado foi para o Início do admin e, no
  mesmo dia, saiu de lá também (pedido do Rafael). Os números por estado ficam no relatório **Market share** (cada
  estado abre `/crm/estado/{uf}` para admin/Logística; esse funil deixa "Relatórios" ativo no menu). O "%" = área plantada dos clientes ÷ área plantada do
  estado no **IBGE, PAM** (tabela 5457 do SIDRA, total das lavouras; escolha do Rafael em 04/10/2026), gravada em
  `crm_area_estados` por `app/ibge_area.py` (`python -m app.ibge_area --gravar`, ou o botão "Buscar ano novo no IBGE"
  do relatório Market share, só admin; o IBGE publica o ano anterior em set/out). Só vão ao IBGE códigos de estado. Ressalva mostrada na tela:
  soja + milho safrinha na mesma terra contam 2 vezes na PAM (pesa em MT/GO). Na produção: rodar o script uma vez. `/crm` só redireciona (admin → relatório Market share; Logística → Início dela). O quadro "Desempenho por
  vendedor" virou o seletor de vendedor da página Desempenho (`?vendedor=`). Migalha raiz por papel: `_mig_raiz`.
  Barra do celular do admin: Início, Vendas, Logística, Relatórios.
- Saíram do menu: "Avisos" (é o sino do topo) e o "Início de vendas" do admin (repetia o Início do admin).
- O setor da página atual vem aberto; os outros como a pessoa deixou (localStorage "menu-setores"). Setor fechado
  mostra o contador mais importante. Sub-itens só com texto e linha de árvore.
- Botão de recolher (só ícones, `body.menu-recolhido`, `--sidebar-w: 72px`, localStorage "menu-recolhido"); só em
  tela ≥ 900 px. Clicar num setor com o menu recolhido abre o menu e o setor.

**Tela de Pedidos por região (04/10/2026, "página muito extensa")** — busca numa linha ("Caminhão indo para"),
mapa logo abaixo (legenda dentro do mapa), painel ao lado (≥1100 px) ou embaixo, com listas compactas de 2 linhas
que rolam dentro do painel: abas **Carregar** (pedidos no raio) e **Oferecer** (clientes sem pedido, com barra de
ação fixa no rodapé). Lista e mapa andam juntos (`MARCADORES`: "ver no mapa" abre o balão; cliente marcado fica
laranja no mapa). Sem destino: guia de 3 passos + cidades com mais pedido. Resumo por estado/cidade num `<details>`
recolhido. Depois de avisar, volta em `#oferecer`.

**Oportunidade logística (04/10/2026)** — `logistica_regiao.clientes_sem_pedido` e `POST /logistica/oportunidades`,
tabela `oportunidades_logisticas` (criada sozinha).
- Rafael: com o caminhão indo para uma região (frete mais barato), a Logística vê os clientes do cadastro ali que
  ainda não têm pedido em aberto e avisa o vendedor. Ficam fora "Não usará" e "Realizado" (já comprou na safra: o que
  falta retirar está em Carregar; Rafael, 04/10). Perdidos entram. Pedido em aberto exclui TODOS os cadastros que casam
  com o nome (`crm_routes.encontrar_clientes_crm`), porque o CRM tem duplicados (ex.: 3 cadastros "Anversa").
  Pedido em aberto = do portal ou da planilha do NetSuite casado pelo NOME (aproximado; a tela avisa para conferir).
  Local do cliente: coordenada da fazenda ou centro da cidade do cadastro.
- Enviar cria a oportunidade (destino, data do caminhão, validade = data ou `oportunidade_dias`), um aviso
  (`tipo="oportunidade_logistica"`, link para a ficha) e um registro no histórico (`tipo="oportunidade"`, fora de
  `TIPOS_CONTATO_REAL`: não conta como contato nem mexe em `ultima_interacao_em`). Cliente sem vendedor não é avisado;
  cliente com oportunidade aberta não duplica.
- Fila do vendedor: motivo novo "Oportunidade logística" (tier 8, chave `oportunidade`, 3º na ordem padrão,
  configurável na página Regras). `crm_routes._oportunidades_abertas`: aberta = dentro da validade e sem contato real
  registrado depois que foi criada. `config._completar_ordem` põe nível novo na posição padrão em ordem já salva.

**Rota das plantas, dentro do portal (03/10/2026)** — `app/rotas.py`, `templates/_rota.html` (incluído no
`base.html`), `GET /api/rota?pedido=|cliente=|uf=&cidade=`.
- Rafael: "não consegui voltar para dentro do portal" e "não pode usar a localização do usuário". A rota abre numa
  janela (`<dialog>`) na própria página; fechar devolve a pessoa ao mesmo lugar. **Origem sempre as plantas**
  (`rotas.PLANTAS`: São Geraldo do Araguaia/PA e Grajaú/MA), as duas lado a lado com km e tempo, a mais curta
  primeiro. `geo.link_rota(destino, origem)` exige a origem; o Google Maps é só o botão "Navegar" (motorista).
- **Ponto das plantas ainda aproximado** (centro da cidade, `coord: None`): a tela avisa. Quando o Rafael mandar o
  local exato, preencher `coord` em `rotas.PLANTAS` (na etapa 2 da página Regras vira editável).
- Cálculo: OSRM público (`router.project-osrm.org`, sem chave; só vão coordenadas, nunca nome de cliente), no máximo
  ~1 pedido/s (trava em `calcular_rota`). Cada rota fica em `rotas_cache` (tabela criada sozinha) e não é pedida de
  novo. Se o serviço falhar, a tela mostra a linha reta e diz que a estrada não foi calculada. Tempo = de carro
  (a tela avisa que caminhão leva mais). Para produção com uso maior: chave gratuita do OpenRouteService ou OSRM
  próprio — trocar só `rotas.OSRM`/`calcular_rota`.
- Permissões: pedido e cidade = admin/logística; cliente = mesma regra da ficha do CRM (vendedor só a carteira dele,
  mais clientes finais dos pedidos dele).

**Página Regras (03–04/10/2026)** — `app/regras.py` (catálogo, prévia, rotas), `app/config.py` (valores),
`templates/admin_regras.html`, `/admin/regras` (só admin).
- Catálogo de todas as regras do portal (45), em 9 grupos, cada uma com descrição, condições, parâmetros, onde
  aparece e tipo: **Ajustável** (número/prazo), **Liga/desliga**, **Travada** (protege os dados: cadastro,
  duplicados, pagamento, acesso — decisão do Rafael: não mudam) e **Funcionamento** (lógica; mudar exige
  desenvolvimento).
- Visual (Rafael, 2026-10-04, pediu "menos texto", com print de tela de configurações): menu lateral com os grupos
  (faixa de abas no celular), uma linha por regra com o controle à direita (caixinha de número, chave liga/desliga,
  cadeado para Travada), parâmetros recuados embaixo e a explicação só ao clicar. Aba "Histórico" no fim do menu.
- **Etapa 2 (04/10): o admin edita.** Cada número/liga-desliga tem padrão em `config.PARAMETROS` (o que valia no
  código). A tabela `config_regras` guarda só o que o admin mudou (sem linha = padrão; salvar o padrão apaga a linha)
  e `config_historico` guarda toda mudança (quem, quando, de → para). Tabelas criadas sozinhas.
- **No código, sempre `config.valor("chave")` / `config.ligada("liga_x")`** — nunca constante solta. As constantes
  antigas (`expedicao.DIAS_SEM_PUXAR`, `crm_routes.TON_DIA_ALERTA`, `LOTE_*`, `RETORNO_SAZONAL_DIAS`,
  `TENTATIVAS_SEM_RETORNO_AJUDA`, `CICLO_VIRADA_*`, `RAIOS`, `LIMITE_TELA`, `DIAS_PROPOSTA_PARADA`) e os números
  escritos direto (60, 10/3/30, 14, 31, 30, 3000/800) saíram. Textos de tela que citam o número usam o mesmo valor
  (global de template `cfg("chave")`). Cache de 10 s por processo (`config.TTL`); salvar limpa o do processo que
  salvou e o cache de contadores do menu.
- **250 t/dia agora é um número só** (`retirada_apertada_t_dia`), usado pela fila do vendedor (>=) e pela da
  Logística (>). Aparece nas duas regras; mudar numa muda na outra.
- **Prévia antes de salvar** (`regras.previa`, com `config.simular`, que troca valores só na thread e só no bloco):
  fila da Logística por nível, soma das filas de trabalho dos vendedores por motivo (cada vendedor com seus lotes),
  clientes que passaram do limite sem contato, categorias A/B/C, propostas paradas, distância do ponto da planta ao
  centro da cidade. **Virada do ciclo**: se a nova data muda o ciclo de hoje, a carteira de todos é reiniciada —
  a prévia avisa quantos clientes voltam para "A contactar" e o salvar exige marcar a confirmação.
- **Listas de prioridade por arrastar** (Rafael, 2026-10-04): "Prioridade da fila da Logística", "Ordem da fila de
  trabalho" (vendedor) e "Ordem da fila de atualização de cadastro". Cada nível é uma caixa com alça (arrastar nativo
  no mouse, eventos de ponteiro no toque) e setas ▲▼, nome editável (`nome_log_*`, `nome_fila_*`), liga/desliga e os
  números do nível. Ordem em `ordem_log` / `ordem_fila` / `ordem_cad` (tipo "ordem": cada chave uma vez).
  O nome aparece como texto e só vira campo pelo lápis (Rafael: campo livre muda sem querer); Enter/fora confirma,
  Esc desfaz, vazio volta ao anterior — e ainda precisa de Salvar.
  Sempre ligados (sem chave de liga): Logística — vendedor respondeu, prazo vencido, sem data limite; vendedor —
  pedido vencido, pedido em aberto.
- **A ordem decide o nível, não só a exibição:** `expedicao.nivel_fila` e `crm_routes._motivo_prioritario` têm uma
  conferência por nível e devolvem a PRIMEIRA que se aplica na ordem configurada. Na Logística a espera pela
  resposta do vendedor (`_ESPERANDO`) vale na posição de "Cobrança sem resposta". No vendedor, o "primeiro contato"
  virou conferência como as outras (os leads são calculados antes do laço); os lotes (sem contato, primeiro contato)
  são cortados depois da ordenação; leads sem outro motivo continuam fora da fila de cadastro mesmo com o primeiro
  contato desligado. Telas usam `expedicao.niveis()` e `crm_routes.rotulo_tier()` / `_posicao_tier()`.
- "Parou de puxar" e "Nunca puxou" ganharam prazo e liga próprios (`log_dias_parou`/`log_dias_nunca`,
  `liga_log_parou`/`liga_log_nunca`; antes era um `log_dias_sem_puxar` só).
- No JS da página, os valores originais (Cancelar) são guardados pelo NOME do campo — as caixas mudam de posição ao
  arrastar; guardar por posição trocava valores entre níveis.
- Ponto exato das plantas: `planta_sao_geraldo` / `planta_grajau` (coordenada ou link do Google Maps); vazio = centro
  da cidade ("ponto aproximado").
- Para criar um parâmetro novo: entrada em `config.PARAMETROS` (tipo, padrão, min/max), trocar o uso no código por
  `config.valor`, `_c("rótulo", "chave", "unidade")` na regra do catálogo (ou `"liga": "liga_x"`) e, se der, uma
  frase em `regras.previa`.
- Descrição: o admin marca "Descrição confere" ou "Corrigir descrição" (tabela `regras_revisao`).
- **Parceiro de cessão sem CNPJ bloqueado** (Rafael, 2026-10-04: "bloqueia"): `crm_routes._erro_parceiro_sem_cnpj`.
  Vale na proposta nova/editada (`_validar_proposta_form`) e no Gerar pedido (é onde o contrato de cessão é
  assinado; pega também proposta feita antes do bloqueio — o erro do CNPJ vem antes dos outros). Renegociar pedido
  que já existe mantendo o mesmo parceiro continua permitido (`parceiro_atual`). Na lista o parceiro aparece
  "— sem CNPJ" e o Gerar pedido avisa antes. Em 04/10 os 3 parceiros (Juparana, Gees, Agrex) estavam sem CNPJ:
  nenhuma venda nova com cessão passa até o admin completar em Parceiros.

**Local de entrega do pedido (03/10/2026)** — `Pedido.latitude/longitude/local_fonte` + `cidade/cidade_fonte`.
- A planilha só traz a UF. `import_expedicao.preencher_local` completa a partir do CRM: coordenada da fazenda
  (`ler_coordenadas`, exata) ou centro da cidade do cadastro (só se a UF do cadastro bate com a de entrega).
- **Venda para transportadora/consultor nunca usa o endereço dela** (Rafael): "TRANSPORT" no nome do NetSuite ou
  cadastro marcado como consultor/transportadora → usa o destino final registrado no pedido do portal; sem destino
  fica `"transportadora sem destino"` — **aviso, sem obrigar** (chip "Falta destino final" na lista e na fila, caixa
  na ficha do pedido; no CRM, chip no pedido e nota no Gerar pedido via `ClienteCRM.entrega_em_outro_lugar()`).
- O casamento de nome com o CRM (`encontrar_cliente_crm`) é aproximado e casa também pelo campo `empresa` (o dono):
  "SERGIO GUIMARAES - FAZ SAO CARLOS" casava com "COCAL TRANSPORTES". Pedido em nome de pessoa/fazenda que casou com
  cadastro de transportadora fica **sem local** (nem o endereço dela, nem o aviso).
- A Logística informa/corrige na ficha do pedido (`POST /logistica/pedido/{id}/local`: UF + cidade da lista do IBGE +
  coordenada opcional). Fica no histórico (campo "local") e **a importação não troca mais** esse local nem a UF.
- `local_fonte` (`LOCAL_FONTE_ROTULO`): `fazenda`, `cidade`, `destino final`, `destino final cidade`, `logistica`,
  `logistica cidade`, `transportadora sem destino`. Exato = `LOCAL_EXATO` (fazenda, destino final, logistica).
- Cadastro do cliente (novo/editar, `_campo_coordenadas.html`): botão "Usar minha localização" (GPS do celular — **só
  funciona com o portal em HTTPS**; em http a tela explica e pede para colar do Google Maps), conferência ao vivo
  (`POST /api/crm-ler-coordenadas`, mesma leitura do servidor) e "Ver no Google Maps". Ao salvar, texto que não é
  coordenada é recusado e o válido é gravado padronizado (`-3.102340, -47.468710`). Aceita decimal, link do Google
  Maps (`!3d..!4d..` do alfinete, `@`, `/place/`, `/search/`, `?q=`), graus/minutos/segundos e os compactos da
  planilha antiga. Hoje 142 de 152 coordenadas do CRM são lidas; 10 têm o nome da fazenda no campo e 8 ficam a mais
  de 150 km da cidade do cadastro (conferir com os vendedores).

**Logística: anotação, fila e SO (03/10/2026)** — regras em `app/expedicao.py`, usadas pela página, pela fila,
pelo contador do menu e pelos relatórios.
- **Situação: uma lista só** (`SITUACOES_LOGISTICA`): a da equipe (Em andamento, Sem retorno, Cobrar retorno do
  vendedor, Possível desistência, Desistência do pedido, Finalizado) + Cliente com crédito. O antigo botão
  "Resolver" (`situacao_resolucao`) saiu; dado antigo migra por `RESOLUCAO_PARA_SITUACAO`. Desistência, Finalizado
  e Cliente com crédito tiram o pedido da lista em aberto (aba "Encerrados"). "Cobrar retorno do vendedor" manda
  mensagem na conversa do pedido e aviso no sino do vendedor (`AvisoCRM.link` leva direto à conversa).
- **Anotar:** o lápis (ou o chip da situação) abre o painel embaixo da linha: situação, data limite, comentário e
  histórico (quem, quando, antes → depois). Toda mudança, inclusive a data pelo campo da tabela, vai para
  `pedido_anotacoes`. Hora gravada em horário de Brasília, a mesma do histórico da planilha.
- **Fila da logística** (`/logistica/fila`): pedido em aberto sem data limite (o cliente não informou ao emitir)
  ou com prazo vencido. Salvar a data tira o pedido da fila na hora.
- **Pedido do portal → SO do NetSuite:** o SO é do NetSuite; o portal mostra o provisório PV-0001 com "aguardando
  NetSuite". A importação liga sozinha quando um só pedido do NetSuite bate (mesmo cliente, categoria de produto,
  volume ±1% e data entre 2 dias antes e 60 depois); senão, admin ou vendedor informa o número na ficha (aceita
  "106605/SO3451", "SO3451" ou "106605"). Ligado, `PedidoCRM.codigo` passa a ser o SO, o carregamento vem do
  faturado do NetSuite e a data limite segue a da Logística (se a Logística não tem, recebe a do vendedor). Só o
  admin desliga.

**Relatórios (02/10/2026):** uma página só (`/relatorios`, `app/relatorios.py` + `relatorios.html`).
Busca (sem acento) e setor filtram o catálogo; escolhido um relatório, o catálogo recolhe em
"Trocar relatório" e o relatório abre logo abaixo, com a largura toda. Todo relatório tem a mesma
forma: filtros com padrão sensato (período padrão = ciclo, desde 1º/nov), faixa de totais, tabela
(até 500 linhas na tela), **Excel** (`/relatorios/{chave}/excel`, todas as linhas, via openpyxl) e
**Imprimir / PDF** (`@media print`: só o relatório, com logo, filtros, quem gerou e quando, A4 paisagem).
Para criar um relatório: escreva `r_<nome>(db, user, f)` devolvendo `totais`, `colunas` (rótulo, tipo),
`linhas`, `links`, `col_link`, `nota`, e registre em `RELATORIOS` com setor, perfis e filtros.
- Escopo: admin vê os 17; vendedor, 16 (sem o Log), só da própria carteira, mesmo forçando
  `?vendedor=` na URL; logística, os 3 de Logística; financeiro, os 4 de Financeiro, sem link para ficha.
- Compras e retiradas antigas vêm da planilha, onde o volume só existe no texto do histórico
  ("Compra registrada: 50t (À vista).") e não há produto nem valor; aparecem com origem "Planilha".
- A data-sentinela `DATA_DESCONHECIDA` (01/01/2020, planilha sem data) vira "sem data" e só entra em
  "Todo o período". Nunca mostrar 01/01/2020 como data real.
- **Número que vira lista de clientes** (03/10/2026): em "Atividade dos vendedores" e no "Funil", cada número de
  clientes é um link (`?ver=linha.coluna`) que abre, logo abaixo da tabela, quem são os clientes (`_lista_clientes`:
  cliente, UF, cidade, etapa, telefone, último contato e, na atividade, o que aconteceu no período). O gerador devolve
  `celulas` (quais células têm lista) e `ver` (a lista pedida); o Excel ganha a aba "Clientes" quando há uma lista
  aberta. Para pôr em outro relatório: monte `{(i, j): ids}` e chame `_detalhe`.
- **Funil com "Mostrar: Movimentos no período"** (03/10/2026): quantos clientes entraram em cada etapa no período
  (`r_funil_movimentos`; um cliente conta uma vez por etapa; "Clientes a contactar" fica de fora porque a virada do
  ciclo não grava movimento). A lista mostra "origem → destino" com a data; a origem só aparece quando foi gravada
  (quase nunca na planilha). "Vendas fechadas" bate com o relatório de Atividade no mesmo período.
- "Atividade dos vendedores" conta pela **carteira** (vendedor do cliente), não por quem registrou: o histórico da
  planilha não tem autor. Clientes trabalhados, vendas e perdas usam a mesma regra da tela Desempenho
  (`_atividade_no_periodo`; o teste confere que os números batem). "Ver por: semana a semana" lista todas as semanas
  do período, inclusive as vazias. Datas antes de 2000 também viram "sem data".
- "Pedidos a retirar" junta `PedidoCRM` em aberto e `Pedido` (NetSuite/planilha da Logística) com saldo e
  sem situação definida. "A prazo e Plano safra" mostra o combinado; pago ou não só com o NetSuite.

**Comissão do vendedor (04/10/2026):** o vendedor só fica elegível quando a empresa **recebe**.
- Percentual por produto com um preço de corte (R$/t): abaixo do corte paga um %, **a partir** dele (preço igual
  inclusive) outro. Padrão: Calcário corte R$ 70 (2% / 3%), Gesso agrícola corte R$ 60 (2% / 3%); Sulfato e Pedra
  britada desligados (sem comissão até o Rafael definir). Tudo editável em Regras → "Comissão do vendedor"
  (chaves `comissao_<produto>_{liga,corte,abaixo,acima}`, tipo `decimal` em `config.py`).
- O percentual fica **gravado no pedido** (`PedidoCRM.comissao_pct` / `comissao_regra`) quando ele é gerado ou
  renegociado (`comissao.gravar`): mudar a regra só vale para vendas novas. Os 38 pedidos que já existiam
  receberam a regra de 04/10/2026.
- Dinheiro recebido = `RecebimentoPedido` (`crm_recebimentos`), única fonte até a integração com o NetSuite.
  À vista: criado sozinho quando o financeiro confirma o comprovante (origem `comprovante`, só a diferença se o
  pedido ficou mais caro). Carga a carga, a prazo e plano safra: o financeiro registra cada pagamento em
  **Recebimentos** (`/financeiro/recebimentos`, origem `financeiro`, nunca acima do que falta, data não futura;
  pode desfazer lançamento errado; o do comprovante não se desfaz ali). À vista em aberto fica fora da lista
  (segue o comprovante); à vista finalizado sem confirmação no portal (pedidos antigos) aparece num grupo próprio.
- Comissão liberada = recebido × percentual do pedido; a cada recebimento o vendedor ganha um aviso
  (`comissao_liberada`) e o histórico do cliente um registro `pagamento`. Pedido cancelado não gera comissão.
- Vendedor vê em **Meu desempenho** (liberada no período, a liberar, pedidos); relatório **Comissões** (setor
  Financeiro, admin/financeiro/vendedor) traz uma linha por recebimento.

**Setores e acessos (05/10/2026)** — seis perfis (`models.ROLES`, nomes em `SETOR_ROTULO`):
| Perfil | Vê | Faz |
|---|---|---|
| admin | tudo | tudo, inclusive Pessoas e acessos e Regras |
| balcao (Balcão de vendas) | tudo, inclusive o Log | vendas em qualquer carteira; Logística, Financeiro, Regras e Log só consulta; não cria login |
| vendedor | Vendas (a carteira dele) | vendas |
| logistica | Logística | Logística |
| financeiro | Financeiro | pagamentos e recebimentos |
| portaria | `/portaria` | só consulta "pode carregar?" |
- Regra do Balcão num lugar só, `auth.require_role`: abre por GET o que o admin abre (menos `/admin/usuarios`) e faz o
  que as rotas de venda permitem ("vendedor" na lista). Em `/logistica`, `/financeiro`, `/admin/regras`, `/crm/log` e
  `/crm/parceiros` o `base.html` mostra "Você está consultando esta área" e desliga os formulários de ação (o servidor
  recusa de qualquer jeito). Relatórios: vê os do admin. Entra no Início de vendas (todas as carteiras).
- **Pessoas e acessos** (`/admin/usuarios`, `app/acessos.py`, só admin): cadastro em passos (nome → setor → carteira,
  só para vendedor, escolhida na lista de carteiras do CRM → usuário sugerido → e-mail). O portal gera a senha
  temporária (`xxxx-xxxx`, sem letras ambíguas), mostrada uma vez; `senha_temporaria=True` obriga a pessoa a criar a
  dela no 1º acesso (`auth.get_current_user` manda tudo para `/trocar-senha`). Editar setor/carteira/e-mail, redefinir
  senha e desativar/reativar; ninguém tira o próprio acesso de admin e sempre sobra um admin ativo. "Minha senha" no
  rodapé do menu (`/trocar-senha`). Senha nova: 8+ caracteres, letras e números, diferente do usuário.
- **Esqueci minha senha** (`/esqueci-senha`, escolha do Rafael: link por e-mail): token aleatório guardado só como
  hash (`senha_tokens`), 30 minutos, uso único, no máximo 3 por hora; resposta sempre igual (não revela se o usuário
  existe). Sem e-mail cadastrado ou com o envio falhando: `pedidos_senha` → tarefa "Redefinir senha" no Início do admin.
- **E-mail** (`app/email_envio.py`, variáveis no `.env` do servidor): `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USUARIO`,
  `SMTP_SENHA`, `SMTP_REMETENTE`, `SMTP_SSL=1` (porta 465) e `PORTAL_URL` (endereço usado no link). Sem `SMTP_HOST`,
  modo de teste: grava em `emails_teste/emails.log` e Pessoas e acessos avisa o admin. Não versionar `emails_teste/`.
- **Portaria** (`app/portaria.py`): pedido do portal segue o pagamento que o portal acompanha (à vista só com a
  conferência; boleto e sobre rodas = "conferir"; plano safra pode); pedido só do NetSuite segue a aprovação de lá
  ("Aprovação ... pendente" = não carrega) e a situação da Logística. Teste: `testar_acessos.py`.
- Produção: `ALTER TABLE users ADD COLUMN email VARCHAR; ALTER TABLE users ADD COLUMN senha_temporaria BOOLEAN NOT NULL
  DEFAULT 0;` (as tabelas `senha_tokens` e `pedidos_senha` são criadas sozinhas) e configurar o SMTP.

## 5. Cuidados técnicos

- **`autoflush=False`** na sessão (`database.py`): se você muda um campo usado no filtro de uma
  consulta e consulta de novo na mesma requisição, chame `db.flush()` antes. Já causou bug no
  cancelamento de pedido.
- **Histórico (`ContatoCRM`) é foto do momento:** número, produto, volume e preço ficam gravados como
  estavam. Editar a proposta depois não reescreve o passado.
- **Nunca inventar dado ausente:** se algo ainda não existe (ex.: retirada vinda do NetSuite), a tela
  diz isso, em vez de mostrar 0 ou a data de hoje.
- **Sem Alembic:** tabelas novas são criadas no início do app (`create_all`), mas colunas novas em
  tabelas existentes exigem `ALTER TABLE` manual. Vale adotar Alembic.
- O CSS é versionado pela data do arquivo (`static_version()`), sem cache velho.

## 6. Pendências que dependem do NetSuite (decidido esperar a integração)

- **Carregamentos individuais:** hoje só existe o volume retirado informado ao finalizar o pedido.
  Com o NetSuite, entram os lembretes de pós-venda (10 dias após o primeiro carregamento e aos 80%
  retirados) e a retirada real na fila.
- **Pagamentos vencidos na fila de trabalho:** vão vir das contas a receber do NetSuite. Não criar
  controle manual.
- Credenciais do sandbox: `.env` a partir do `.env.example`.

## 7. Levar o protótipo para produção

O banco de produção precisa destas colunas antes de rodar este código (as 2 tabelas novas,
`crm_avisos` e `crm_pedido_destino_final`, são criadas sozinhas):

```sql
ALTER TABLE crm_clientes ADD COLUMN motivo_perdido VARCHAR;
ALTER TABLE crm_clientes ADD COLUMN e_consultor BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE crm_clientes ADD COLUMN e_transportadora BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE crm_contatos ADD COLUMN resultado VARCHAR;
ALTER TABLE crm_contatos ADD COLUMN cliente_relacionado_id INTEGER;
```

Faça backup do `portal.db` antes e valide com o Rafael, porque há uso real acumulado no banco de
produção.

**Situação em 28/09/2026:** esses ALTERs já foram aplicados no banco do portal principal
(`C:\Claude\portal`), junto com todo o funcionamento do protótipo. O visual novo (design system
Integral: `app/menu.py`, `base.html` com menu lateral, `style.css` novo) ficou só no protótipo, por
decisão do Rafael.

**Comprovante de pagamento do pedido à vista (02/10/2026, só no protótipo por enquanto):** o
vendedor anexa o comprovante (ao gerar o pedido ou depois, no cartão do pedido) e o **financeiro ou o
admin** confere em `/financeiro/pagamentos`, confirmando ou recusando com motivo. Só a confirmação
libera o carregamento (e o botão Finalizar). Pedido renegociado para um valor maior volta a esperar o
comprovante da diferença. O vendedor recebe aviso nos dois casos. Perfil novo `financeiro` (o
`import_data` cria o usuário `financeiro` com a senha padrão). A tabela nova `crm_pedido_comprovantes`
é criada sozinha; os arquivos ficam em `uploads/comprovantes_pagamento` (dado bancário do cliente:
fora do git e do pacote). Para levar à produção:

```sql
ALTER TABLE crm_pedidos ADD COLUMN pagamento_liberado_em DATETIME;
ALTER TABLE crm_pedidos ADD COLUMN pagamento_liberado_por VARCHAR;
ALTER TABLE crm_pedidos ADD COLUMN forma_prazo VARCHAR;
ALTER TABLE crm_pedidos ADD COLUMN prazo_parcelas VARCHAR;
ALTER TABLE crm_pedidos ADD COLUMN prazo_periodo_dias INTEGER;
ALTER TABLE crm_pedidos ADD COLUMN vencimento_pagamento DATE;
```

**Planilha de Expedição na Logística (03/10/2026):** a tabela `importacoes_planilha` é criada sozinha. Os bancos
antigos têm duas colunas que o modelo voltou a declarar (`carregado`, NOT NULL, e `observacao_logistica`, sem
uso); sem elas no modelo nenhum pedido novo entra. Para levar à produção:

```sql
ALTER TABLE pedidos ADD COLUMN aba_planilha VARCHAR;
ALTER TABLE pedidos ADD COLUMN ultima_retirada DATE;
ALTER TABLE pedidos ADD COLUMN situacao_logistica VARCHAR;
ALTER TABLE pedidos ADD COLUMN comentario_logistica VARCHAR;
ALTER TABLE pedidos ADD COLUMN anotado_no_portal_em DATETIME;
ALTER TABLE crm_pedidos ADD COLUMN pedido_netsuite VARCHAR;
ALTER TABLE crm_pedidos ADD COLUMN vinculado_em DATETIME;
ALTER TABLE crm_pedidos ADD COLUMN vinculado_por VARCHAR;
ALTER TABLE crm_avisos ADD COLUMN link VARCHAR;
ALTER TABLE pedidos ADD COLUMN telefone VARCHAR;
ALTER TABLE pedidos ADD COLUMN cidade VARCHAR;
ALTER TABLE pedidos ADD COLUMN cidade_fonte VARCHAR;
ALTER TABLE pedidos ADD COLUMN latitude FLOAT;
ALTER TABLE pedidos ADD COLUMN longitude FLOAT;
ALTER TABLE pedidos ADD COLUMN local_fonte VARCHAR;
-- comissao (04/10/2026); a tabela crm_recebimentos e criada sozinha. Depois do ALTER, gravar o percentual
-- dos pedidos que ja existem com a regra do dia: para cada PedidoCRM, comissao.gravar(pedido); commit.
ALTER TABLE crm_pedidos ADD COLUMN comissao_pct FLOAT;
ALTER TABLE crm_pedidos ADD COLUMN comissao_regra VARCHAR;
-- presenca (bolinha verde/cinza da equipe, 04/10/2026)
ALTER TABLE users ADD COLUMN ultimo_acesso DATETIME;
ALTER TABLE users ADD COLUMN saiu_em DATETIME;
-- vendedor que nao ve a propria carteira (so maiusculas diferentes): alinhar ao CRM
UPDATE users SET vendedor_nome = 'Sidney Passos de Sousa' WHERE vendedor_nome = 'SIDNEY PASSOS DE SOUSA';
UPDATE pedidos SET vendedor = 'Sidney Passos de Sousa' WHERE vendedor = 'SIDNEY PASSOS DE SOUSA';
UPDATE users SET vendedor_nome = 'Wagner Souza dos Santos' WHERE vendedor_nome = 'WAGNER SOUZA DOS SANTOS';
UPDATE pedidos SET vendedor = 'Wagner Souza dos Santos' WHERE vendedor = 'WAGNER SOUZA DOS SANTOS';
-- antigo "Resolver" -> lista unica de situacoes
UPDATE pedidos SET situacao_logistica = 'finalizado' WHERE situacao_resolucao = 'finalizado_parcial';
UPDATE pedidos SET situacao_logistica = 'cobrar_vendedor' WHERE situacao_resolucao = 'renegociar';
UPDATE pedidos SET situacao_logistica = 'cliente_com_credito' WHERE situacao_resolucao = 'aguardando_proximo_periodo';
```

A tabela `pedido_anotacoes` é criada sozinha. A página `pedido_resolver.html` e as rotas `/pedidos/{id}/resolver` e
`/api/pedidos/{id}/situacao` foram removidas.

**Pedido do portal = "PV-0001" (03/10/2026):** os números começavam em 105601, os mesmos de pedidos reais do
NetSuite de outros clientes. O código vem de `models.codigo_pedido()` / `PedidoCRM.codigo` (nunca mostrar
`numero` solto) e a numeração começa em 1. Na produção, renumerar os existentes (novo = antigo − 105600),
inclusive `crm_contatos.numero` dos registros de pedido, os textos do histórico e dos avisos e o
`credito_origem_json` (script usado no protótipo: `renumerar_banco.py`, com simulação antes de gravar).

## 8. Antes de qualquer ambiente real

1. ~~Remover o login automático e o `?como=`~~ — feito em 02/10/2026.
2. Definir `PORTAL_SECRET_KEY` no `.env` (sem ela, a sessão usa uma chave de desenvolvimento).
3. Trocar as senhas padrão (`troque-esta-senha`) e as senhas fracas definidas para teste.
4. Deploy planejado na AWS (conta da empresa).
