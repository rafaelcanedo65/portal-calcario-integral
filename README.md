# Portal Calcário Integral

Portal web interno para a Calcário Integral LTDA — logística, vendas/CRM e (futuramente) integração
com o NetSuite. FastAPI + Jinja2 + SQLite, sem build step de frontend (HTML/CSS/JS puro).

**Desenvolvedor novo: comece pelo [GUIA_DESENVOLVEDOR.md](GUIA_DESENVOLVEDOR.md)** — como rodar com
dados fictícios, regras de negócio, pendências e cuidados antes de produção.

## Setup

Requer Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Copie `.env.example` para `.env` se for mexer na integração NetSuite (`app/netsuite_client.py`) ou
quiser fixar `PORTAL_SECRET_KEY` (sem isso, cai num valor de desenvolvimento — troque antes de qualquer
ambiente real).

## Rodando localmente

```bash
python -m app.crm_seed                       # só na primeira vez: cria o banco com dados FICTÍCIOS
uvicorn app.main:app --reload --port 8422
```

Abra http://localhost:8422 e entre com um dos usuários de exemplo (senha de todos:
`troque-esta-senha`): `ana.exemplo` (vendedora), `financeiro`, `logistica` ou `admin`.

O banco (`portal.db`, SQLite) fica na pasta de onde o servidor roda. **Este pacote não traz dados
reais de clientes** (LGPD): só o código e o gerador de dados fictícios.

## Login

Toda entrada passa pela página de login (usuário e senha). O login automático e o atalho
`/login?como=<usuario>` usados no desenvolvimento foram removidos em 02/10/2026. Cada app tem cookie
de sessão próprio (`portal_sessao` na produção, `prototipo_sessao` no protótipo), então entrar num não
loga no outro. Antes de um ambiente real: definir `PORTAL_SECRET_KEY` e trocar senhas fracas.

## Estrutura

- `app/main.py` — rotas principais (login, logística, admin/usuários, pedidos).
- `app/crm_routes.py` — todas as rotas do CRM (funil de vendas, propostas, pedidos, mapa, agenda).
- `app/models.py` — modelos SQLAlchemy (`Pedido` = logística/NetSuite; `ClienteCRM`/`PropostaCRM`/
  `PedidoCRM`/`ContatoCRM` = CRM comercial — são dois mundos ainda **não ligados**, ver nota abaixo).
- `app/auth.py` — sessão, hash de senha, `require_role()`.
- `app/templates/`, `app/static/` — Jinja2 + CSS no padrão do Integral Design System (fonte Inter, cores da marca: marinho #33247A e amarelo #FCEC0A, ícones Lucide). A casca (menu lateral, topo, barra do celular) fica em `base.html`; o menu de cada perfil e os contadores em `app/menu.py`.
- `app/import_data.py` — importa Pedidos de uma planilha exportada do NetSuite (aba "Pedidos" do
  formato CALCARIO 2026) e cria os usuários iniciais.
- `app/crm_import_real.py` — importa os clientes do CRM a partir de `crm_dados_reais.json` (não
  incluído no repositório — dado real de PII, veja `.gitignore`). Idempotente: apaga e reimporta todo
  `ClienteCRM`/`ContatoCRM` — **não rode isso se já tiver uso real acumulado no CRM** (propostas,
  fases avançadas) sem confirmar com o Rafael antes.
- `app/crm_seed.py` — cria um banco novo com dados **fictícios** (usuários, clientes, parceiros), pra
  rodar sem os dados reais. Ver o guia.
- `app/netsuite_client.py` — cliente OAuth 1.0 (TBA) pra consultar o NetSuite via SuiteQL. Integração
  ainda não ligada nas rotas principais — é a base pra quando isso avançar.

## Dois modelos de "Pedido" que ainda não se falam

Isso é importante pra quem for mexer no código: existe `Pedido` (logística, alimentado pelo
`import_data.py` a partir de planilha/NetSuite real) e existe `PedidoCRM` (nasce das propostas feitas
dentro do próprio CRM, `crm_routes.py`). São tabelas separadas de propósito — misturar as duas
corromperia a reconciliação de dados reais da Logística. Quando a integração NetSuite estiver pronta de
verdade, é aí que os dois mundos devem se encontrar.

## Papéis (roles)

Quatro setores usam o portal: `logistica`, `vendedor`, `financeiro` (confere o comprovante dos pedidos
à vista e libera o carregamento, em `/financeiro/pagamentos`) e `admin` (o "balcão de vendas", que faz
a gestão de todos os dados e também pode conferir pagamentos). `require_role()` em `app/auth.py` controla o acesso por rota.
