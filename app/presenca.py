"""Quem esta com o portal aberto agora (Rafael, 2026-10-04: "mostre se o vendedor
estiver online apenas com uma bola verde, se nao tiver online, bola cinza").

Online = o portal mandou sinal nos ultimos ONLINE_MIN minutos e a pessoa nao saiu
depois disso. Sinal: qualquer pagina aberta (get_current_user, no maximo 1 gravacao
por minuto) e o "estou aqui" que o base.html manda a cada 2 minutos enquanto a aba
esta visivel (/api/presenca). Sair do portal (logout) desliga na hora."""
import datetime as dt

ONLINE_MIN = 3
GRAVAR_A_CADA_S = 60


def tocar(db, user, forcar=False):
    agora = dt.datetime.utcnow()
    if forcar or user.ultimo_acesso is None or (agora - user.ultimo_acesso).total_seconds() >= GRAVAR_A_CADA_S:
        user.ultimo_acesso = agora
        db.commit()


def online(user, agora=None):
    agora = agora or dt.datetime.utcnow()
    if user.ultimo_acesso is None or agora - user.ultimo_acesso > dt.timedelta(minutes=ONLINE_MIN):
        return False
    return user.saiu_em is None or user.saiu_em < user.ultimo_acesso


def _ha(delta):
    minutos = int(delta.total_seconds() // 60)
    if minutos < 60:
        return f"há {max(minutos, 1)} min"
    horas = minutos // 60
    if horas < 24:
        return f"há {horas} h"
    dias = horas // 24
    return f"há {dias} dia{'s' if dias != 1 else ''}"


def descricao(user, agora=None):
    """Texto do passar o mouse na bolinha."""
    agora = agora or dt.datetime.utcnow()
    if online(user, agora):
        return "Online agora (portal aberto)"
    if user.ultimo_acesso is None:  # a presenca so passou a ser registrada em 04/10/2026: nao dizer "nunca entrou"
        return "Offline · sem acesso registrado desde 04/10/2026"
    return f"Offline · último acesso {_ha(agora - user.ultimo_acesso)}"
