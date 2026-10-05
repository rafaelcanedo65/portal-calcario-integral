"""Pessoas e acessos (Rafael, 2026-10-05: "formular essa parte de login, recuperar senha na entrada, criar login ...
clean, intuitivo e leve o usuario sempre para o proximo passo").

- So o admin cria login (escolha do Rafael): nome, setor (vendedor: a carteira, escolhida numa lista pra nao errar o
  nome como aconteceu com Sidney/Wagner), usuario e e-mail. O portal gera uma senha temporaria, mostrada uma vez pro
  admin passar a pessoa; no 1o acesso o portal obriga a criar a propria (auth.get_current_user).
- O admin muda setor, desativa/reativa e redefine senha (nova temporaria). Nao da pra tirar o proprio acesso de admin
  nem deixar o portal sem nenhum admin ativo.
- "Esqueci minha senha" na entrada (escolha do Rafael: link por e-mail): link de uso unico, 30 minutos, guardado so
  como hash. A resposta e sempre a mesma (nao revela se o usuario existe). Sem e-mail cadastrado (ou com o envio
  falhando): vira tarefa no Inicio do admin (PedidoSenha), que redefine na mao.
- "Minha senha" (qualquer pessoa logada) em /trocar-senha."""
import datetime as dt
import hashlib
import re
import secrets
import unicodedata

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import email_envio, presenca
from .auth import get_current_user, hash_password, require_role, verify_password
from .database import get_db
from .feedback import avisar_sucesso
from .models import ROLES, SETOR_DESCRICAO, SETOR_ROTULO, ClienteCRM, PedidoSenha, SenhaToken, User

router = APIRouter()
VALIDADE_LINK_MIN = 30
MAX_LINKS_POR_HORA = 3
ORDEM_SETORES = ("admin", "balcao", "vendedor", "logistica", "financeiro", "portaria")
_ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789"  # sem 0/o, 1/l/i: pra ditar sem confusao


def _templates(request):
    from .crm_routes import _templates as t
    return t(request)


def gerar_senha_temporaria():
    s = "".join(secrets.choice(_ALFABETO) for _ in range(8))
    return f"{s[:4]}-{s[4:]}"


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def sugerir_usuario(nome):
    base = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode().lower()
    partes = re.findall(r"[a-z0-9]+", base)
    return ".".join([partes[0], partes[-1]]) if len(partes) > 1 else (partes[0] if partes else "")


def erro_senha(nova, confirma, usuario):
    if len(nova or "") < 8:
        return "A senha precisa ter pelo menos 8 caracteres."
    if not re.search(r"[A-Za-z]", nova) or not re.search(r"\d", nova):
        return "Use letras e números na senha."
    if nova.strip().lower() == (usuario or "").lower():
        return "A senha não pode ser igual ao usuário."
    if nova != confirma:
        return "As duas senhas não estão iguais."
    return None


def _carteiras(db):
    """Carteiras do CRM (nome do vendedor nos clientes) e se ja tem alguem ativo ligado."""
    com_usuario = {u.vendedor_nome for u in db.query(User).filter(User.ativo.is_(True), User.vendedor_nome.isnot(None))}
    linhas = (db.query(ClienteCRM.vendedor_nome, func.count(ClienteCRM.id)).filter(ClienteCRM.vendedor_nome.isnot(None))
                .group_by(ClienteCRM.vendedor_nome).order_by(ClienteCRM.vendedor_nome).all())
    return [{"nome": n, "clientes": c, "com_usuario": n in com_usuario} for n, c in linhas if n.strip()]


def _admins_ativos(db, exceto_id=None):
    q = db.query(User).filter(User.role == "admin", User.ativo.is_(True))
    if exceto_id:
        q = q.filter(User.id != exceto_id)
    return q.count()


def _pagina(request, db, user, resultado=None, erro=None, abrir=None, form=None, status_code=200):
    pessoas = db.query(User).all()
    pessoas.sort(key=lambda u: (not u.ativo, ORDEM_SETORES.index(u.role) if u.role in ORDEM_SETORES else 99, u.nome_completo.lower()))
    agora = dt.datetime.utcnow()
    pedidos = (db.query(PedidoSenha).filter(PedidoSenha.atendido_em.is_(None)).order_by(PedidoSenha.criado_em).all())
    contagem = {s: sum(1 for u in pessoas if u.role == s and u.ativo) for s in ORDEM_SETORES}
    return _templates(request).TemplateResponse(request, "admin_usuarios.html", {
        "user": user, "pessoas": pessoas, "online": {u.id: presenca.online(u, agora) for u in pessoas},
        "presenca": {u.id: presenca.descricao(u, agora) for u in pessoas}, "setores": ORDEM_SETORES,
        "rotulo": SETOR_ROTULO, "descricao": SETOR_DESCRICAO, "contagem": contagem, "carteiras": _carteiras(db),
        "pedidos_senha": pedidos, "ha": lambda q: presenca._ha(agora - q), "email_configurado": email_envio.configurado(),
        "resultado": resultado, "erro": erro, "abrir": abrir, "form": form or {},
        "migalhas": [("Pessoas e acessos", None)],
    }, status_code=status_code)


def _validar_cadastro(db, nome, role, carteira, carteira_nova, email, usuario=None, editando=None):
    """-> (erro, vendedor_nome)."""
    if len((nome or "").strip()) < 3:
        return "Informe o nome completo.", None
    if role not in ROLES:
        return "Escolha o setor da pessoa.", None
    vendedor_nome = None
    if role == "vendedor":
        vendedor_nome = (carteira_nova or "").strip() or (carteira or "").strip()
        if not vendedor_nome:
            return "Vendedor precisa de uma carteira: escolha na lista ou informe a carteira nova.", None
        outro = (db.query(User).filter(User.vendedor_nome == vendedor_nome, User.ativo.is_(True),
                                       User.id != (editando.id if editando else -1)).first())
        if outro:
            return f"A carteira {vendedor_nome} já é de {outro.nome_completo}.", None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        return "E-mail inválido.", None
    if usuario is not None:
        if not re.fullmatch(r"[a-z0-9._-]{3,40}", usuario):
            return "Usuário: de 3 a 40 letras minúsculas, números, ponto, hífen ou sublinhado (sem espaço e sem acento).", None
        if db.query(User).filter(func.lower(User.username) == usuario).first():
            return f"Já existe o usuário {usuario}. Escolha outro.", None
    return None, vendedor_nome


# ---------------------------------------------------------------- Pessoas e acessos (admin)

@router.get("/admin/usuarios", response_class=HTMLResponse)
def pessoas(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    return _pagina(request, db, user)


@router.post("/admin/usuarios", response_class=HTMLResponse)
def criar_pessoa(request: Request, nome_completo: str = Form(""), role: str = Form(""), carteira: str = Form(""),
                 carteira_nova: str = Form(""), username: str = Form(""), email: str = Form(""),
                 user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    usuario = (username or "").strip().lower() or sugerir_usuario(nome_completo)
    form = {"nome_completo": nome_completo, "role": role, "carteira": carteira, "carteira_nova": carteira_nova,
            "username": usuario, "email": email}
    erro, vendedor_nome = _validar_cadastro(db, nome_completo, role, carteira, carteira_nova, email, usuario=usuario)
    if erro:
        return _pagina(request, db, user, erro=erro, abrir="novo", form=form, status_code=400)
    senha = gerar_senha_temporaria()
    nova = User(username=usuario, password_hash=hash_password(senha), nome_completo=nome_completo.strip(), role=role,
                vendedor_nome=vendedor_nome, email=(email or "").strip() or None, senha_temporaria=True)
    db.add(nova)
    db.commit()
    return _pagina(request, db, user, resultado={"tipo": "criado", "pessoa": nova, "senha": senha})


@router.post("/admin/usuarios/{pessoa_id}", response_class=HTMLResponse)
def editar_pessoa(request: Request, pessoa_id: int, nome_completo: str = Form(""), role: str = Form(""),
                  carteira: str = Form(""), carteira_nova: str = Form(""), email: str = Form(""),
                  user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    p = db.get(User, pessoa_id)
    if p is None:
        return _pagina(request, db, user, erro="Pessoa não encontrada.", status_code=404)
    erro, vendedor_nome = _validar_cadastro(db, nome_completo, role, carteira, carteira_nova, email, editando=p)
    if not erro and p.id == user.id and role != "admin":
        erro = "Você não pode tirar o seu próprio acesso de administrador."
    if not erro and p.role == "admin" and role != "admin" and p.ativo and _admins_ativos(db, exceto_id=p.id) == 0:
        erro = "O portal precisa de pelo menos um administrador ativo."
    if erro:
        return _pagina(request, db, user, erro=erro, abrir=f"p{p.id}", status_code=400)
    p.nome_completo, p.role, p.vendedor_nome, p.email = nome_completo.strip(), role, vendedor_nome, (email or "").strip() or None
    db.commit()
    avisar_sucesso(request, f"{p.nome_completo}: dados salvos ({SETOR_ROTULO.get(p.role, p.role)}).")
    return RedirectResponse(f"/admin/usuarios#p{p.id}", status_code=303)


@router.post("/admin/usuarios/{pessoa_id}/senha", response_class=HTMLResponse)
def redefinir_senha_pessoa(request: Request, pessoa_id: int, user: User = Depends(require_role("admin")),
                           db: Session = Depends(get_db)):
    p = db.get(User, pessoa_id)
    if p is None:
        return _pagina(request, db, user, erro="Pessoa não encontrada.", status_code=404)
    senha = gerar_senha_temporaria()
    p.password_hash, p.senha_temporaria = hash_password(senha), True
    agora = dt.datetime.utcnow()
    for t in db.query(SenhaToken).filter(SenhaToken.user_id == p.id, SenhaToken.usado_em.is_(None)):
        t.usado_em = agora  # links antigos deixam de valer
    for ped in db.query(PedidoSenha).filter(PedidoSenha.user_id == p.id, PedidoSenha.atendido_em.is_(None)):
        ped.atendido_em, ped.atendido_por = agora, user.nome_completo
    db.commit()
    return _pagina(request, db, user, resultado={"tipo": "senha", "pessoa": p, "senha": senha})


@router.post("/admin/usuarios/{pessoa_id}/ativo")
def ativar_pessoa(request: Request, pessoa_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    p = db.get(User, pessoa_id)
    if p is None:
        return _pagina(request, db, user, erro="Pessoa não encontrada.", status_code=404)
    if p.id == user.id:
        return _pagina(request, db, user, erro="Você não pode desativar o seu próprio acesso.", abrir=f"p{p.id}", status_code=400)
    if p.ativo and p.role == "admin" and _admins_ativos(db, exceto_id=p.id) == 0:
        return _pagina(request, db, user, erro="O portal precisa de pelo menos um administrador ativo.", status_code=400)
    p.ativo = not p.ativo
    if not p.ativo:
        p.saiu_em = dt.datetime.utcnow()
    db.commit()
    avisar_sucesso(request, f"{p.nome_completo}: acesso {'reativado' if p.ativo else 'desativado (não entra mais no portal)'}.")
    return RedirectResponse(f"/admin/usuarios#p{p.id}", status_code=303)


# ---------------------------------------------------------------- Esqueci minha senha (sem login)

@router.get("/esqueci-senha", response_class=HTMLResponse)
def esqueci_form(request: Request):
    return _templates(request).TemplateResponse(request, "esqueci_senha.html", {"enviado": False})


@router.post("/esqueci-senha", response_class=HTMLResponse)
def esqueci_enviar(request: Request, identificacao: str = Form(""), db: Session = Depends(get_db)):
    ident = (identificacao or "").strip().lower()
    p = (db.query(User).filter((func.lower(User.username) == ident) | (func.lower(User.email) == ident)).first()
         if ident else None)
    if p is not None and p.ativo:
        agora = dt.datetime.utcnow()
        recentes = db.query(SenhaToken).filter(SenhaToken.user_id == p.id, SenhaToken.criado_em >= agora - dt.timedelta(hours=1)).count()
        motivo = None
        if not p.email:
            motivo = "sem e-mail cadastrado"
        elif recentes < MAX_LINKS_POR_HORA:
            token = secrets.token_urlsafe(32)
            db.add(SenhaToken(user_id=p.id, token_hash=_hash_token(token), expira_em=agora + dt.timedelta(minutes=VALIDADE_LINK_MIN)))
            db.commit()
            link = f"{email_envio.url_portal()}/redefinir-senha?t={token}"
            try:
                email_envio.enviar(p.email, "Portal Integral: criar uma senha nova",
                                   f"Olá, {p.nome_completo.split(' ')[0]}.\n\nPara criar uma senha nova no portal, abra este link "
                                   f"(vale por {VALIDADE_LINK_MIN} minutos e uma vez só):\n\n{link}\n\n"
                                   "Se não foi você que pediu, pode ignorar este e-mail: sua senha continua a mesma.")
            except Exception:
                motivo = "o envio do e-mail falhou"
        if motivo and not db.query(PedidoSenha).filter(PedidoSenha.user_id == p.id, PedidoSenha.atendido_em.is_(None)).first():
            db.add(PedidoSenha(user_id=p.id, motivo=motivo))
            db.commit()
    # Mesma resposta sempre: nao revela se o usuario existe nem se tem e-mail
    return _templates(request).TemplateResponse(request, "esqueci_senha.html", {"enviado": True})


def _token_valido(db, token):
    if not token:
        return None
    t = db.query(SenhaToken).filter(SenhaToken.token_hash == _hash_token(token)).first()
    if t is None or t.usado_em is not None or t.expira_em < dt.datetime.utcnow():
        return None
    p = db.get(User, t.user_id)
    return t if p is not None and p.ativo else None


@router.get("/redefinir-senha", response_class=HTMLResponse)
def redefinir_form(request: Request, t: str = "", db: Session = Depends(get_db)):
    tok = _token_valido(db, t)
    return _templates(request).TemplateResponse(request, "redefinir_senha.html", {
        "valido": tok is not None, "token": t, "usuario": db.get(User, tok.user_id).username if tok else None, "erro": None})


@router.post("/redefinir-senha", response_class=HTMLResponse)
def redefinir_salvar(request: Request, t: str = Form(""), nova: str = Form(""), confirma: str = Form(""),
                     db: Session = Depends(get_db)):
    tok = _token_valido(db, t)
    if tok is None:
        return _templates(request).TemplateResponse(request, "redefinir_senha.html", {"valido": False, "token": "", "erro": None})
    p = db.get(User, tok.user_id)
    erro = erro_senha(nova, confirma, p.username)
    if erro:
        return _templates(request).TemplateResponse(request, "redefinir_senha.html", {
            "valido": True, "token": t, "usuario": p.username, "erro": erro}, status_code=400)
    agora = dt.datetime.utcnow()
    p.password_hash, p.senha_temporaria = hash_password(nova), False
    for outro in db.query(SenhaToken).filter(SenhaToken.user_id == p.id, SenhaToken.usado_em.is_(None)):
        outro.usado_em = agora
    for ped in db.query(PedidoSenha).filter(PedidoSenha.user_id == p.id, PedidoSenha.atendido_em.is_(None)):
        ped.atendido_em, ped.atendido_por = agora, "a própria pessoa (link por e-mail)"
    db.commit()
    return RedirectResponse("/login?msg=senha", status_code=303)


# ---------------------------------------------------------------- Minha senha / 1o acesso (logado)

@router.get("/trocar-senha", response_class=HTMLResponse)
def trocar_form(request: Request, user: User = Depends(get_current_user)):
    return _templates(request).TemplateResponse(request, "trocar_senha.html", {"user": user, "erro": None,
                                                                              "migalhas": [("Minha senha", None)]})


@router.post("/trocar-senha", response_class=HTMLResponse)
def trocar_salvar(request: Request, atual: str = Form(""), nova: str = Form(""), confirma: str = Form(""),
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    erro = None
    if not user.senha_temporaria and not verify_password(atual or "", user.password_hash):
        erro = "A senha atual não confere."
    erro = erro or erro_senha(nova, confirma, user.username)
    if not erro and verify_password(nova, user.password_hash):
        erro = "A senha nova precisa ser diferente da atual."
    if erro:
        return _templates(request).TemplateResponse(request, "trocar_senha.html", {"user": user, "erro": erro,
                                                                                  "migalhas": [("Minha senha", None)]}, status_code=400)
    primeiro = user.senha_temporaria
    user.password_hash, user.senha_temporaria = hash_password(nova), False
    db.commit()
    avisar_sucesso(request, "Pronto! Sua senha foi criada. Bem-vindo ao portal." if primeiro else "Senha nova salva.")
    return RedirectResponse("/", status_code=303)
