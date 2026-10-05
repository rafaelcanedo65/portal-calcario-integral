import datetime as dt
import os
import re

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import get_current_user, hash_password, require_role, verify_password
from .database import Base, engine, get_db
from .models import (CAMPOS_ANOTACAO, ESTADOS_OPERACAO, LOCAL_FONTE_ROTULO, PRODUTOS, ROTULO_HISTORICO, ROLES,
                     PedidoAnotacao, ler_coordenadas, SITUACOES_ENCERRAM, SITUACOES_LOGISTICA, ImportacaoPlanilha,
                     MensagemPedido, Pedido, User, codigo_pedido)
from . import config, expedicao, geo, presenca

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Portal Calcário Integral")
# Cookie com nome proprio por app: prototipo (8422) e producao (8420) rodam no
# mesmo localhost, e com o mesmo nome um login valia nos dois.
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("PORTAL_SECRET_KEY", "chave-de-desenvolvimento-trocar-em-producao"),
                   session_cookie="prototipo_sessao")

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def formatar_numero_br(valor, casas=2):
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


templates.env.filters["numbr"] = formatar_numero_br
templates.env.filters["codigo_pedido"] = codigo_pedido


def telefone_link(telefone):
    """Pro link tel: (ligar pelo celular): so digitos, com +55 quando tem DDD."""
    digitos = "".join(c for c in (telefone or "") if c.isdigit())
    return "+55" + digitos if len(digitos) in (10, 11) else digitos


templates.env.filters["telefone_link"] = telefone_link

_CONECTIVOS = {"de", "da", "do", "das", "dos", "e"}
_SIGLAS = {"ME", "EPP", "EI", "SA", "S/A", "S.A", "S.A.", "II", "III", "IV"}


def _palavra(p, primeira):
    if p.upper() in _SIGLAS:
        return p.upper()
    if not primeira and p.lower() in _CONECTIVOS:
        return p.lower()
    i = next((k for k, ch in enumerate(p) if ch.isalpha()), None)  # "(FAZ" -> "(Faz"
    return p if i is None else p[:i] + p[i].upper() + p[i + 1:].lower()


def nome_cliente(nome):
    """Nome pra lista: sem o codigo do NetSuite na frente ("13177 LUCAS ..." ->
    "Lucas ...") e, se veio todo em maiusculas, em forma de nome. So exibicao."""
    nome = re.sub(r"^\d+\s+", "", (nome or "").strip())
    if not nome or any(ch.islower() for ch in nome):
        return nome
    return " ".join(_palavra(p, i == 0) for i, p in enumerate(nome.split()))


def produto_legivel(produto):
    """"GESSO AGRICOLA AGRANEL" -> "Gesso agrícola agranel"; vazio -> None."""
    t = (produto or "").strip().lower()
    for sem, com in (("calcario", "calcário"), ("agricola", "agrícola"), ("calcio", "cálcio"), ("organico", "orgânico"),
                     ("moida", "moída")):
        t = t.replace(sem, com)
    return (t[:1].upper() + t[1:]) or None


templates.env.filters["nome_cliente"] = nome_cliente
templates.env.filters["produto_legivel"] = produto_legivel
templates.env.globals["link_ponto"] = geo.link_ponto
templates.env.globals["cfg"] = config.valor  # numero de regra num texto de tela (pagina Regras)
templates.env.globals["ler_coordenadas"] = ler_coordenadas

# Cache-busting pro CSS: o navegador (do Rafael inclusive, ja aconteceu de
# verdade) segura o /static/style.css antigo em cache e ignora edicoes ate um
# hard-refresh manual. Recalcula a cada render (so um stat(), barato) pra
# qualquer alteracao no arquivo forcar o navegador a buscar a versao nova.
_CSS_PATH = os.path.join(BASE_DIR, "static", "style.css")
templates.env.globals["static_version"] = lambda: int(os.path.getmtime(_CSS_PATH))

app.state.templates = templates

# Menu lateral do design system (ver app/menu.py). Qualquer POST pode mudar
# fila/cadastros/avisos, entao limpa o cache dos contadores depois dele.
from . import menu  # noqa: E402
from .feedback import avisar_erro, avisar_sucesso  # noqa: E402
templates.env.globals["menu_lateral"] = menu.menu_lateral
templates.env.globals["iniciais"] = menu.iniciais
templates.env.globals["iniciais_cliente"] = menu.iniciais_cliente


@app.middleware("http")
async def limpar_contadores_apos_gravacao(request: Request, call_next):
    resposta = await call_next(request)
    if request.method == "POST":
        menu.limpar_cache_contadores()
    return resposta

from . import crm_routes  # noqa: E402
app.include_router(crm_routes.router)
from . import financeiro_routes  # noqa: E402
app.include_router(financeiro_routes.router)
from . import relatorios  # noqa: E402
app.include_router(relatorios.router)
from . import admin_inicio  # noqa: E402
app.include_router(admin_inicio.router)
from . import acessos, portaria  # noqa: E402
app.include_router(acessos.router)
app.include_router(portaria.router)
from . import inicio  # noqa: E402
app.include_router(inicio.router)
from . import logistica_regiao  # noqa: E402
app.include_router(logistica_regiao.router)
from . import rotas  # noqa: E402
app.include_router(rotas.router)
from . import regras  # noqa: E402
app.include_router(regras.router)
from .crm_routes import encontrar_cliente_crm  # noqa: E402


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(url=exc.headers.get("Location", "/login"), status_code=303)
    return templates.TemplateResponse(request, "erro.html", {"detail": exc.detail}, status_code=exc.status_code)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    # Login religado em 2026-10-02 (pedido do Rafael): sem auto-login e sem
    # o atalho ?como= -- toda entrada passa por usuario e senha.
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"erro": None, "msg": request.query_params.get("msg")})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=username.strip()).first()
    if not user or not user.ativo or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {"erro": "Usuário ou senha inválidos."}, status_code=401)
    request.session["user_id"] = user.id
    presenca.tocar(db, user, forcar=True)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = db.get(User, request.session.get("user_id")) if request.session.get("user_id") else None
    if user is not None:  # sair desliga a bolinha verde na hora
        user.saiu_em = dt.datetime.utcnow()
        db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/presenca")
def api_presenca(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """"Estou aqui" do base.html a cada 2 min com a aba visivel (presenca.py)."""
    presenca.tocar(db, user, forcar=True)
    return Response(status_code=204)


@app.get("/")
def home(user: User = Depends(get_current_user)):
    # Admin entra no Inicio dele: o que so ele resolve, a equipe e a Logistica (Rafael, 2026-10-03)
    if user.role == "admin":
        return RedirectResponse("/admin/inicio", status_code=303)
    if user.role == "logistica":
        return RedirectResponse("/logistica/inicio", status_code=303)
    if user.role == "financeiro":
        return RedirectResponse("/financeiro/inicio", status_code=303)
    if user.role == "portaria":
        return RedirectResponse("/portaria", status_code=303)
    # Balcao de vendas (2026-10-05): entra no Inicio de vendas, com todas as carteiras
    # Vendedor entra direto no Inicio do CRM (filas de trabalho e cadastro),
    # nao na lista de pedidos do NetSuite -- pedido do Rafael, 2026-10-02.
    return RedirectResponse("/vendedor/crm", status_code=303)


@app.get("/api/busca-clientes")
def busca_clientes(q: str = "", user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    termo = q.strip()
    if len(termo) < 2:
        return JSONResponse([])
    coringa = f"%{termo}%"
    linhas = (db.query(Pedido.cliente, Pedido.numero_pedido)
                .filter(or_(Pedido.cliente.ilike(coringa), Pedido.numero_pedido.ilike(coringa)))
                .limit(50).all())
    vistos = set()
    resultados = []
    for cliente, numero in linhas:
        if cliente in vistos:
            continue
        vistos.add(cliente)
        if termo.lower() in numero.lower():
            label = f"{cliente} — pedido {numero}"
        else:
            label = cliente
        resultados.append({"cliente": cliente, "label": label})
        if len(resultados) >= 10:
            break
    return JSONResponse(resultados)


@app.get("/logistica", response_class=HTMLResponse)
def logistica_dashboard(request: Request, data_de: str = "", data_ate: str = "",
                         uf: str = "", produto: str = "", visao: str = "aberto", cliente: str = "",
                         user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    # Em aberto = abas que a expedicao trabalha, sem situacao que encerra
    # (expedicao.query_em_aberto). "Encerrados" = a equipe encerrou, NetSuite ainda tem saldo.
    encerrados = visao == "encerrados"
    query = expedicao.query_em_aberto(db, encerrados=encerrados)
    if data_de:
        query = query.filter(Pedido.data_pedido >= dt.datetime.strptime(data_de, "%Y-%m-%d").date())
    if data_ate:
        query = query.filter(Pedido.data_pedido <= dt.datetime.strptime(data_ate, "%Y-%m-%d").date())
    if uf:
        query = query.filter(Pedido.uf == uf)
    if cliente:
        query = query.filter(Pedido.cliente.ilike(f"%{cliente}%"))
    pedidos = query.all()
    if produto:
        # O NetSuite traz o nome completo ("GESSO AGRICOLA - BAG"); o filtro e por categoria
        pedidos = [p for p in pedidos if p.categoria == produto]
    pedidos.sort(key=lambda p: p.prioridade_key())
    total_saldo = sum(p.saldo for p in pedidos)
    vencidos = sum(1 for p in pedidos if p.vencido())
    sem_prazo = sum(1 for p in pedidos if not p.data_limite_retirada)

    mensagens_nao_lidas = {
        row[0]: row[1] for row in
        db.query(MensagemPedido.pedido_id, func.count(MensagemPedido.id))
          .filter(MensagemPedido.autor_role == "vendedor", MensagemPedido.lida.is_(False))
          .group_by(MensagemPedido.pedido_id).all()
    }
    return templates.TemplateResponse(request, "logistica.html", {
        "user": user, "pedidos": pedidos,
        "total_saldo": total_saldo, "vencidos": vencidos, "sem_prazo": sem_prazo, "hoje": dt.date.today(),
        "estados": ESTADOS_OPERACAO, "produtos": PRODUTOS, "situacoes": SITUACOES_LOGISTICA,
        "filtros": {"data_de": data_de, "data_ate": data_ate, "uf": uf, "produto": produto, "visao": visao, "cliente": cliente},
        "mensagens_nao_lidas": mensagens_nao_lidas, "encerrados": encerrados,
        "total_abertos": expedicao.query_em_aberto(db).count(),
        "total_encerrados": expedicao.query_em_aberto(db, encerrados=True).count(),
        "importacao": db.query(ImportacaoPlanilha).order_by(ImportacaoPlanilha.importada_em.desc()).first(),
    })


@app.get("/clientes/pedidos", response_class=HTMLResponse)
def cliente_pedidos(request: Request, nome: str, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedidos = db.query(Pedido).filter(Pedido.cliente == nome).order_by(Pedido.data_pedido.desc()).all()
    cliente_crm = encontrar_cliente_crm(db, nome)
    total_saldo = sum(p.saldo for p in pedidos if p.em_aberto())
    return templates.TemplateResponse(request, "cliente_pedidos.html", {
        "user": user, "nome": nome, "pedidos": pedidos, "cliente_crm": cliente_crm, "total_saldo": total_saldo,
    })


@app.get("/logistica/fila", response_class=HTMLResponse)
def logistica_fila(request: Request, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    """Fila da Logistica com prioridade (Rafael, 2026-10-03: carregar "sem
    apertar e sem esquecer nenhum pedido"): niveis na ordem e com os nomes
    da pagina Regras (expedicao.niveis), cada pedido no mais urgente, com o motivo."""
    itens = expedicao.fila(db)
    niveis = expedicao.niveis()
    grupos = {chave: [] for chave in niveis}
    for p, n in itens:
        grupos[n["chave"]].append((p, n))
    return templates.TemplateResponse(request, "logistica_fila.html", {
        "user": user, "grupos": grupos, "niveis": niveis, "ind": expedicao.indicadores(db, itens),
        "regras": {"dias_parou": config.valor("log_dias_parou"), "dias_nunca": config.valor("log_dias_nunca"), "ton_dia": config.valor("retirada_apertada_t_dia"),
                   "dias_prazo": config.valor("log_dias_prazo_apertado"), "dias_cobranca": config.valor("log_dias_cobranca"),
                   "sem_acao": config.valor("log_dias_sem_acao")},
        "hoje": dt.date.today(),
        "importacao": db.query(ImportacaoPlanilha).order_by(ImportacaoPlanilha.importada_em.desc()).first(),
    })


def _voltar_seguro(voltar):
    return voltar if voltar in ("/logistica", "/logistica/fila") else "/logistica"


@app.get("/logistica/pedido/{pedido_id}/anotacao", response_class=HTMLResponse)
def logistica_anotacao(request: Request, pedido_id: int, voltar: str = "/logistica",
                       user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    """Ficha do pedido na Logistica (Rafael, 2026-10-03: mesmo conceito da ficha
    do cliente no CRM): abre embaixo da linha com as etapas do pedido, o
    proximo passo com o botao certo, o contato, a conversa com o vendedor, a
    anotacao e o historico."""
    pedido = db.get(Pedido, pedido_id)
    if not pedido:
        return HTMLResponse("<p class='nota'>Pedido não encontrado.</p>", status_code=404)
    pcrm = expedicao.pedido_do_portal(db, pedido)
    passo = expedicao.proximo_passo(db, pedido, pcrm)   # antes de marcar como lidas
    mensagens = expedicao.mensagens_do_pedido(db, pedido)
    for m in db.query(MensagemPedido).filter(MensagemPedido.pedido_id == pedido.id, MensagemPedido.lida.is_(False)):
        if m.autor_role != user.role and m.autor_role == "vendedor":
            m.lida = True
    db.commit()
    cliente_crm = encontrar_cliente_crm(db, pedido.cliente)
    return templates.TemplateResponse(request, "_anotacao_logistica.html", {
        "user": user, "p": pedido, "situacoes": SITUACOES_LOGISTICA, "encerram": SITUACOES_ENCERRAM, "campos": ROTULO_HISTORICO,
        "local_rotulo": LOCAL_FONTE_ROTULO.get(pedido.local_fonte), "ufs_local": UFS_LOCAL,
        "voltar": _voltar_seguro(voltar), "pcrm": pcrm, "etapas": expedicao.etapas_pedido(pedido, pcrm), "passo": passo,
        "mensagens": mensagens, "cliente_crm": cliente_crm, "hoje": dt.date.today(),
    })


@app.post("/logistica/pedido/{pedido_id}/anotacao")
def logistica_anotar(request: Request, pedido_id: int, situacao: str = Form(None), data_limite: str = Form(None),
                     comentario: str = Form(None), mensagem: str = Form(""), campos: str = Form(""),
                     voltar: str = Form("/logistica"),
                     user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    """So mexe nos campos que vieram no formulario: os botoes do proximo passo
    mandam um campo so (ex.: so a data, ou so "Cobrar retorno do vendedor").
    Campo vazio chega como None; por isso o formulario declara em `campos` o que
    esta mandando -- declarado e vazio = limpar (ex.: "Reabrir", "Sem situacao")."""
    pedido = db.get(Pedido, pedido_id)
    voltar = _voltar_seguro(voltar)
    if not pedido:
        return RedirectResponse(voltar, status_code=303)
    declarados = {c.strip() for c in campos.split(",")}
    so = tuple(c for c, v in (("situacao", situacao), ("data_limite", data_limite), ("comentario", comentario))
               if v is not None or c in declarados)
    if situacao and situacao not in SITUACOES_LOGISTICA:
        situacao = pedido.situacao_logistica or ""
    try:
        data = dt.datetime.strptime(data_limite, "%Y-%m-%d").date() if data_limite else None
    except ValueError:
        data = pedido.data_limite_retirada
    # Formulario sem nenhum dos tres campos nao mexe em nada (so=() seria "todos")
    mudados = expedicao.anotar(db, pedido, user, situacao=situacao, data_limite=data, comentario=comentario, so=so,
                               mensagem=mensagem.strip() or None) if so else []
    db.commit()
    if mudados == ["cobranca"]:
        avisar_sucesso(request, f"{pedido.vendedor or 'O vendedor'} foi cobrado de novo.")
    elif not mudados:
        avisar_sucesso(request, f"Nada mudou no pedido {pedido.numero_pedido}.")
    elif pedido.situacao_logistica in SITUACOES_ENCERRAM and "situacao" in mudados:
        avisar_sucesso(request, f"Pedido {pedido.numero_pedido}: {SITUACOES_LOGISTICA[pedido.situacao_logistica]}. "
                                f"Saiu da lista em aberto (fica em Encerrados).")
    elif pedido.situacao_logistica == "cobrar_vendedor" and "situacao" in mudados:
        avisar_sucesso(request, f"Anotação salva. {pedido.vendedor or 'O vendedor'} foi avisado para dar retorno.")
    else:
        avisar_sucesso(request, f"Anotação salva no pedido {pedido.numero_pedido}.")
    # Volta pra lista com a ficha do pedido aberta de novo (se ele ainda estiver la)
    return RedirectResponse(f"{voltar}?abrir={pedido.id}#row-{pedido.id}", status_code=303)


# Estados de operacao primeiro; o resto do Brasil depois (destino final pode ser longe)
UFS_LOCAL = list(ESTADOS_OPERACAO) + [u for u in geo.ufs() if u not in ESTADOS_OPERACAO]


@app.post("/logistica/pedido/{pedido_id}/local")
def logistica_local(request: Request, pedido_id: int, uf: str = Form(""), cidade: str = Form(""), coordenadas: str = Form(""),
                    voltar: str = Form("/logistica"),
                    user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    """Local de entrega informado pela Logistica (Rafael, 2026-10-03): corrige ou
    completa o que veio do CRM -- e, na venda pra transportadora, e o destino
    final. A importacao da planilha nao troca mais este local."""
    pedido = db.get(Pedido, pedido_id)
    voltar = _voltar_seguro(voltar)
    if not pedido:
        return RedirectResponse(voltar, status_code=303)
    achado = geo.municipio(uf, cidade)
    ponto = ler_coordenadas(coordenadas) if coordenadas.strip() else None
    if not achado:
        avisar_erro(request, f"Não achamos a cidade \"{cidade.strip()}\" em {uf or 'nenhum estado'}. Escolha da lista. Nada foi salvo.")
    elif coordenadas.strip() and not ponto:
        avisar_erro(request, "Não conseguimos ler as coordenadas. Use o formato -3.0021, -47.3527 ou cole o link do Google Maps. Nada foi salvo.")
    else:
        antes = f"{pedido.cidade}/{pedido.uf}" if pedido.cidade else (pedido.uf or "sem local")
        pedido.uf, pedido.cidade, pedido.cidade_fonte = uf.upper(), achado[0], "Logística"
        pedido.latitude, pedido.longitude = ponto or achado[1]
        pedido.local_fonte = "logistica" if ponto else "logistica cidade"
        depois = f"{achado[0]}/{uf.upper()}" + (f" ({ponto[0]:.5f}, {ponto[1]:.5f})" if ponto else " (centro da cidade)")
        db.add(PedidoAnotacao(pedido_id=pedido.id, quando=dt.datetime.now(), quem=user.nome_completo, origem="portal",
                              campo="local", antes=antes, depois=depois))
        pedido.anotado_no_portal_em = dt.datetime.utcnow()
        db.commit()
        avisar_sucesso(request, f"Local de entrega do pedido {pedido.numero_pedido}: {depois}.")
    return RedirectResponse(f"{voltar}?abrir={pedido.id}#row-{pedido.id}", status_code=303)


@app.post("/api/pedidos/{pedido_id}/prazo")
def atualizar_prazo(pedido_id: int, data_limite: str = Form(""), user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido:
        return JSONResponse({"erro": "Pedido não encontrado"}, status_code=404)
    try:
        data = dt.datetime.strptime(data_limite, "%Y-%m-%d").date() if data_limite else None
    except ValueError:
        return JSONResponse({"erro": "Data inválida"}, status_code=400)
    expedicao.anotar(db, pedido, user, data_limite=data, so=("data_limite",))
    db.commit()
    return JSONResponse({
        "dias_restantes": pedido.dias_restantes(),
        "ton_dia": pedido.ton_dia_necessario(),
        "vencido": pedido.vencido(),
        "fila": expedicao.contar_fila(db),
        # Em que nivel o pedido ficou (a fila tira ou mantem o item na hora)
        "nivel": (expedicao.nivel_fila(pedido, expedicao.contexto_fila(db, [pedido])) or {}).get("chave"),
    })


def _pode_ver_pedido(user: User, pedido: Pedido) -> bool:
    if user.role in ("admin", "logistica"):
        return True
    return pedido.vendedor == user.vendedor_nome


@app.get("/pedidos/{pedido_id}/conversa", response_class=HTMLResponse)
def pedido_conversa(request: Request, pedido_id: int, user: User = Depends(require_role("admin", "logistica", "vendedor")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido or not _pode_ver_pedido(user, pedido):
        return templates.TemplateResponse(request, "erro.html", {"detail": "Pedido não encontrado, ou fora da sua carteira."}, status_code=404)

    mensagens = db.query(MensagemPedido).filter_by(pedido_id=pedido_id).order_by(MensagemPedido.criado_em).all()
    for m in mensagens:
        if m.autor_role != user.role and not m.lida:
            m.lida = True
    db.commit()

    cliente_crm = encontrar_cliente_crm(db, pedido.cliente)
    return templates.TemplateResponse(request, "pedido_conversa.html", {
        "user": user, "pedido": pedido, "mensagens": mensagens, "cliente_crm": cliente_crm,
    })


@app.post("/pedidos/{pedido_id}/conversa")
def pedido_conversa_enviar(request: Request, pedido_id: int, texto: str = Form(...), voltar: str = Form(""),
                            user: User = Depends(require_role("admin", "logistica", "vendedor")),
                            db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if pedido and _pode_ver_pedido(user, pedido) and texto.strip():
        db.add(MensagemPedido(pedido_id=pedido_id, autor_id=user.id, autor_nome=user.nome_completo,
                               autor_role=user.role, texto=texto.strip()))
        db.commit()
        if voltar:
            avisar_sucesso(request, "Mensagem enviada ao vendedor.")
    # Respondida de dentro da ficha do pedido na Logistica: volta pra la
    if voltar in ("/logistica", "/logistica/fila") and user.role in ("admin", "logistica"):
        return RedirectResponse(f"{voltar}?abrir={pedido_id}#row-{pedido_id}", status_code=303)
    # Cai no fim da conversa, onde a mensagem nova aparece
    return RedirectResponse(f"/pedidos/{pedido_id}/conversa#fim-conversa", status_code=303)


@app.get("/vendedor", response_class=HTMLResponse)
def vendedor_dashboard(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(Pedido)
    if user.role == "vendedor":
        query = query.filter(Pedido.vendedor == user.vendedor_nome)
    pedidos = query.order_by(Pedido.data_pedido.desc()).all()

    # Saldo so do que esta em aberto: pedido fechado no NetSuite com sobra nao e
    # saldo a retirar. A lista mostra os em aberto; o historico fica no cliente.
    clientes = {}
    for p in pedidos:
        info = clientes.setdefault(p.cliente, {"pedidos": 0, "saldo_aberto": 0.0, "faturado": 0.0})
        info["pedidos"] += 1
        info["saldo_aberto"] += p.saldo if p.em_aberto() else 0
        info["faturado"] += p.faturado
    total_pedidos = len(pedidos)
    pedidos = [p for p in pedidos if p.em_aberto()]

    mensagens_nao_lidas = {
        row[0]: row[1] for row in
        db.query(MensagemPedido.pedido_id, func.count(MensagemPedido.id))
          .filter(MensagemPedido.autor_role.in_(["admin", "logistica"]), MensagemPedido.lida.is_(False))
          .group_by(MensagemPedido.pedido_id).all()
    }

    return templates.TemplateResponse(request, "vendedor.html", {
        "user": user, "pedidos": pedidos, "clientes": clientes, "mensagens_nao_lidas": mensagens_nao_lidas,
        "total_pedidos": total_pedidos,
    })


