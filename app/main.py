import datetime as dt
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import get_current_user, hash_password, require_role, verify_password
from .database import Base, engine, get_db
from .models import ESTADOS_OPERACAO, PRODUTOS, RESOLUCAO_LABEL, MensagemPedido, Pedido, User

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Portal Calcario Integral")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("PORTAL_SECRET_KEY", "chave-de-desenvolvimento-trocar-em-producao"))

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def formatar_numero_br(valor, casas=2):
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


templates.env.filters["numbr"] = formatar_numero_br
app.state.templates = templates

from . import crm_routes  # noqa: E402
app.include_router(crm_routes.router)
from .crm_routes import encontrar_cliente_crm  # noqa: E402


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(url=exc.headers.get("Location", "/login"), status_code=303)
    return templates.TemplateResponse(request, "erro.html", {"detail": exc.detail}, status_code=exc.status_code)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=username.strip()).first()
    if not user or not user.ativo or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {"erro": "Usuario ou senha invalidos."}, status_code=401)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def home(user: User = Depends(get_current_user)):
    if user.role in ("admin", "logistica"):
        return RedirectResponse("/logistica", status_code=303)
    return RedirectResponse("/vendedor", status_code=303)


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
    query = db.query(Pedido).filter(Pedido.saldo > 0)
    if visao == "aberto":
        query = query.filter(Pedido.situacao_resolucao.is_(None))
    elif visao != "todos":
        query = query.filter(Pedido.situacao_resolucao == visao)
    if data_de:
        query = query.filter(Pedido.data_pedido >= dt.datetime.strptime(data_de, "%Y-%m-%d").date())
    if data_ate:
        query = query.filter(Pedido.data_pedido <= dt.datetime.strptime(data_ate, "%Y-%m-%d").date())
    if uf:
        query = query.filter(Pedido.uf == uf)
    if produto:
        query = query.filter(Pedido.produto == produto)
    if cliente:
        query = query.filter(Pedido.cliente.ilike(f"%{cliente}%"))
    pedidos = query.all()
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
        "estados": ESTADOS_OPERACAO, "produtos": PRODUTOS, "resolucao_label": RESOLUCAO_LABEL,
        "filtros": {"data_de": data_de, "data_ate": data_ate, "uf": uf, "produto": produto, "visao": visao, "cliente": cliente},
        "mensagens_nao_lidas": mensagens_nao_lidas,
    })


@app.get("/clientes/pedidos", response_class=HTMLResponse)
def cliente_pedidos(request: Request, nome: str, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedidos = db.query(Pedido).filter(Pedido.cliente == nome).order_by(Pedido.data_pedido.desc()).all()
    cliente_crm = encontrar_cliente_crm(db, nome)
    total_saldo = sum(p.saldo for p in pedidos)
    return templates.TemplateResponse(request, "cliente_pedidos.html", {
        "user": user, "nome": nome, "pedidos": pedidos, "cliente_crm": cliente_crm, "total_saldo": total_saldo,
    })


@app.get("/pedidos/{pedido_id}/resolver", response_class=HTMLResponse)
def pedido_resolver_form(request: Request, pedido_id: int, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido:
        return templates.TemplateResponse(request, "erro.html", {"detail": "Pedido nao encontrado."}, status_code=404)
    return templates.TemplateResponse(request, "pedido_resolver.html", {
        "user": user, "pedido": pedido, "resolucao_label": RESOLUCAO_LABEL,
    })


@app.post("/api/pedidos/{pedido_id}/situacao")
def definir_situacao_pedido(pedido_id: int, situacao: str = Form(...), observacao: str = Form(""),
                             user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido:
        return RedirectResponse("/logistica", status_code=303)

    if situacao == "reabrir":
        pedido.situacao_resolucao = None
        pedido.situacao_observacao = None
    elif situacao in RESOLUCAO_LABEL:
        pedido.situacao_resolucao = situacao
        pedido.situacao_observacao = observacao.strip() or None
        pedido.situacao_definida_em = dt.datetime.utcnow()

        if situacao == "renegociar" and pedido.vendedor:
            texto = (f"Pedido marcado para RENEGOCIACAO pela logistica: o cliente parou de retirar "
                     f"(saldo ainda em {pedido.saldo:.2f}t) e pode estar negociando o restante com outro "
                     f"fornecedor. {('Obs: ' + observacao.strip()) if observacao.strip() else ''}")
            db.add(MensagemPedido(pedido_id=pedido.id, autor_id=user.id, autor_nome=user.nome_completo,
                                   autor_role=user.role, texto=texto))
    db.commit()
    return RedirectResponse("/logistica", status_code=303)


@app.post("/api/pedidos/{pedido_id}/prazo")
def atualizar_prazo(pedido_id: int, data_limite: str = Form(""), user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido:
        return JSONResponse({"erro": "Pedido nao encontrado"}, status_code=404)
    pedido.data_limite_retirada = dt.datetime.strptime(data_limite, "%Y-%m-%d").date() if data_limite else None
    db.commit()
    return JSONResponse({
        "dias_restantes": pedido.dias_restantes(),
        "ton_dia": pedido.ton_dia_necessario(),
        "vencido": pedido.vencido(),
    })


def _pode_ver_pedido(user: User, pedido: Pedido) -> bool:
    if user.role in ("admin", "logistica"):
        return True
    return pedido.vendedor == user.vendedor_nome


@app.get("/pedidos/{pedido_id}/conversa", response_class=HTMLResponse)
def pedido_conversa(request: Request, pedido_id: int, user: User = Depends(require_role("admin", "logistica", "vendedor")), db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if not pedido or not _pode_ver_pedido(user, pedido):
        return templates.TemplateResponse(request, "erro.html", {"detail": "Pedido nao encontrado, ou fora da sua carteira."}, status_code=404)

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
def pedido_conversa_enviar(pedido_id: int, texto: str = Form(...),
                            user: User = Depends(require_role("admin", "logistica", "vendedor")),
                            db: Session = Depends(get_db)):
    pedido = db.get(Pedido, pedido_id)
    if pedido and _pode_ver_pedido(user, pedido) and texto.strip():
        db.add(MensagemPedido(pedido_id=pedido_id, autor_id=user.id, autor_nome=user.nome_completo,
                               autor_role=user.role, texto=texto.strip()))
        db.commit()
    return RedirectResponse(f"/pedidos/{pedido_id}/conversa", status_code=303)


@app.get("/vendedor", response_class=HTMLResponse)
def vendedor_dashboard(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(Pedido)
    if user.role == "vendedor":
        query = query.filter(Pedido.vendedor == user.vendedor_nome)
    pedidos = query.order_by(Pedido.data_pedido.desc()).all()

    clientes = {}
    for p in pedidos:
        info = clientes.setdefault(p.cliente, {"pedidos": 0, "saldo_aberto": 0.0, "faturado": 0.0})
        info["pedidos"] += 1
        info["saldo_aberto"] += p.saldo
        info["faturado"] += p.faturado

    mensagens_nao_lidas = {
        row[0]: row[1] for row in
        db.query(MensagemPedido.pedido_id, func.count(MensagemPedido.id))
          .filter(MensagemPedido.autor_role.in_(["admin", "logistica"]), MensagemPedido.lida.is_(False))
          .group_by(MensagemPedido.pedido_id).all()
    }

    return templates.TemplateResponse(request, "vendedor.html", {
        "user": user, "pedidos": pedidos, "clientes": clientes, "mensagens_nao_lidas": mensagens_nao_lidas,
    })


@app.get("/admin/usuarios", response_class=HTMLResponse)
def admin_usuarios(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    usuarios = db.query(User).order_by(User.role, User.username).all()
    return templates.TemplateResponse(request, "admin_usuarios.html", {"user": user, "usuarios": usuarios, "erro": None})


@app.post("/admin/usuarios")
def admin_criar_usuario(request: Request, username: str = Form(...), senha: str = Form(...),
                         nome_completo: str = Form(...), role: str = Form(...),
                         vendedor_nome: str = Form(""), user: User = Depends(require_role("admin")),
                         db: Session = Depends(get_db)):
    if db.query(User).filter_by(username=username.strip()).first():
        usuarios = db.query(User).order_by(User.role, User.username).all()
        return templates.TemplateResponse(request, "admin_usuarios.html", {
            "user": user, "usuarios": usuarios, "erro": "Ja existe um usuario com esse username.",
        }, status_code=400)
    novo = User(username=username.strip(), password_hash=hash_password(senha), nome_completo=nome_completo.strip(),
                role=role, vendedor_nome=vendedor_nome.strip() or None)
    db.add(novo)
    db.commit()
    return RedirectResponse("/admin/usuarios", status_code=303)
