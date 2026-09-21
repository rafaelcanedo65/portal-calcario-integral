# Portal Calcário Integral

Portal web interno para a Calcário Integral LTDA — logística, vendas/CRM e (futuramente) integração
com o NetSuite. FastAPI + Jinja2 + SQLite, sem build step de frontend (HTML/CSS/JS puro).

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
uvicorn app.main:app --reload --port 8420
```

O banco (`portal.db`, SQLite) é criado automaticamente na primeira execução (schema vazio, sem
clientes/pedidos). Veja "Dados" abaixo para popular.

## ⚠️ Antes de usar com usuários de verdade

**`app/auth.py::get_current_user` tem um bypass de login temporário**, pedido pelo Rafael pra não
precisar logar a cada teste durante o desenvolvimento: sem sessão, o sistema auto-loga como o primeiro
usuário `role="admin"` que encontrar, sem senha. Está marcado com um comentário `TEMPORARIO` no código.
**Remova esse bloco antes de qualquer deploy que usuários reais vão acessar** — do jeito que está, não
tem autenticação nenhuma.

## Estrutura

- `app/main.py` — rotas principais (login, logística, admin/usuários, pedidos).
- `app/crm_routes.py` — todas as rotas do CRM (funil de vendas, propostas, pedidos, mapa, agenda).
- `app/models.py` — modelos SQLAlchemy (`Pedido` = logística/NetSuite; `ClienteCRM`/`PropostaCRM`/
  `PedidoCRM`/`ContatoCRM` = CRM comercial — são dois mundos ainda **não ligados**, ver nota abaixo).
- `app/auth.py` — sessão, hash de senha, `require_role()`.
- `app/templates/`, `app/static/` — Jinja2 + CSS (paleta terrosa, fontes Fraunces/Work Sans).
- `app/import_data.py` — importa Pedidos de uma planilha exportada do NetSuite (aba "Pedidos" do
  formato CALCARIO 2026) e cria os usuários iniciais.
- `app/crm_import_real.py` — importa os clientes do CRM a partir de `crm_dados_reais.json` (não
  incluído no repositório — dado real de PII, veja `.gitignore`). Idempotente: apaga e reimporta todo
  `ClienteCRM`/`ContatoCRM` — **não rode isso se já tiver uso real acumulado no CRM** (propostas,
  fases avançadas) sem confirmar com o Rafael antes.
- `app/crm_seed.py` — **legado**, populava dado de exemplo antes do `crm_import_real.py` existir.
  Não é mais usado.
- `app/netsuite_client.py` — cliente OAuth 1.0 (TBA) pra consultar o NetSuite via SuiteQL. Integração
  ainda não ligada nas rotas principais — é a base pra quando isso avançar.

## Dois modelos de "Pedido" que ainda não se falam

Isso é importante pra quem for mexer no código: existe `Pedido` (logística, alimentado pelo
`import_data.py` a partir de planilha/NetSuite real) e existe `PedidoCRM` (nasce das propostas feitas
dentro do próprio CRM, `crm_routes.py`). São tabelas separadas de propósito — misturar as duas
corromperia a reconciliação de dados reais da Logística. Quando a integração NetSuite estiver pronta de
verdade, é aí que os dois mundos devem se encontrar.

## Papéis (roles)

Três setores usam o portal: `logistica`, `vendedor` e `admin` (o "balcão de vendas", que faz a gestão
de todos os dados). `require_role()` em `app/auth.py` controla o acesso por rota.
