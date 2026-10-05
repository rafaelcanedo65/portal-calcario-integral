import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# Senha temporaria: ate criar a propria, so estas paginas abrem
LIVRES_COM_SENHA_TEMPORARIA = ("/trocar-senha", "/logout", "/api/presenca")
# Balcao de vendas ve o que o admin ve, menos a gestao de logins
SO_ADMIN_DE_VERDADE = ("/admin/usuarios",)


def ve_tudo(user):
    """Admin e Balcao de vendas veem todas as carteiras e areas."""
    return user is not None and user.role in ("admin", "balcao")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = db.get(User, user_id)
    if not user or not user.ativo:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    from . import presenca
    presenca.tocar(db, user)  # bolinha verde na equipe (no maximo 1 gravacao por minuto)
    if user.senha_temporaria and request.url.path not in LIVRES_COM_SENHA_TEMPORARIA:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/trocar-senha"})
    return user


def require_role(*roles):
    """Perfis que podem abrir a rota. Balcao de vendas (Rafael, 2026-10-05): "ve tudo, mas nao pode atribuir login nem
    alterar regra" e opera vendas -> abre (so leitura) o que o admin abre, menos a gestao de logins, e faz o que o
    vendedor faz (as rotas de venda aceitam "vendedor"). Acao de Logistica, Financeiro, Regras e pessoas: nao."""
    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        if user.role in roles:
            return user
        if user.role == "balcao":
            leitura = request.method in ("GET", "HEAD")
            if leitura and "admin" in roles and not request.url.path.startswith(SO_ADMIN_DE_VERDADE):
                return user
            if "vendedor" in roles:
                return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem permissão para acessar esta página.")
    return dependency
