"""Menu lateral do portal (design system Integral, 2026-09-28).

Cada perfil ve so o que pode acessar (mesmos `require_role` das rotas). Os
contadores (fila de trabalho, cadastros, avisos) custam caro pro admin (~0,5s,
a base inteira passa pela regra da fila), entao ficam num cache curto por
usuario -- e qualquer POST (gravacao) limpa o cache, pra o numero nao ficar
desatualizado depois que o vendedor registra um contato ou fecha um pedido.
"""
import re
import time

from .database import SessionLocal

TTL_CONTADORES = 120  # segundos
_cache_contadores = {}  # user_id -> (instante, dict)

from .models import SETOR_ROTULO as PAPEL_ROTULO  # noqa: E402  (Administrador, Balcao de vendas, ...)

# Ficha do cliente: o item destacado segue de onde o vendedor veio (?via=),
# o mesmo parametro que a migalha usa (ORIGENS_FICHA em crm_routes).
VIA_PARA_ITEM = {
    "fila": "/vendedor/crm/fila", "base": "/vendedor/crm/base", "agenda": "/vendedor/crm/agenda",
    "avisos": "/vendedor/crm/avisos", "pedidos": "/vendedor/crm/pedidos",
}


def limpar_cache_contadores():
    _cache_contadores.clear()


def guardar_contadores(user, fila, cadastros, avisos):
    """A home ja calcula as duas filas -- aproveita pra aquecer o cache e nao
    repetir o calculo so pro menu."""
    _cache_contadores[user.id] = (time.monotonic(), {"fila": fila, "cadastros": cadastros, "avisos": avisos})


def contadores(user):
    if user.role not in ("admin", "vendedor"):
        return {}
    em_cache = _cache_contadores.get(user.id)
    if em_cache and time.monotonic() - em_cache[0] < TTL_CONTADORES:
        return em_cache[1]
    from . import crm_routes  # import tardio: crm_routes importa varias coisas do app
    db = SessionLocal()
    try:
        fila, clientes, _, _, _, ids_leads = crm_routes._montar_fila_trabalho(db, user)
        base = crm_routes._montar_fila_base(clientes, {i["cliente"].id for i in fila} | ids_leads)
        avisos = crm_routes._query_avisos(db, user).filter_by(lido=False).count()
    finally:
        db.close()
    guardar_contadores(user, len(fila), len(base), avisos)
    return _cache_contadores[user.id][1]


def _item(rotulo, href, icone, principal=False, contador=None, contador_neutro=False, prefixos=()):
    return {"rotulo": rotulo, "href": href, "icone": icone, "principal": principal, "contador": contador,
            "contador_neutro": contador_neutro, "prefixos": (href,) + tuple(prefixos), "ativo": False}


def _pagamentos_urgentes():
    """Pagamento a vista esperando conferencia (contador do Financeiro e faixa
    vermelha de prioridade maxima): sem cache, muda na hora em que o vendedor
    anexa ou o financeiro confere. -> {n, ha, link} ou None."""
    from . import inicio
    db = SessionLocal()
    try:
        return inicio.resumo_urgente(db)
    finally:
        db.close()


def _fila_logistica():
    """Contador da Fila da logistica: uma contagem simples, sem cache (muda a
    cada prazo salvo)."""
    from . import expedicao
    db = SessionLocal()
    try:
        return expedicao.contar_fila(db)
    finally:
        db.close()


def _setor(chave, rotulo, icone, itens, contador=None, contador_neutro=False):
    """Grupo que abre e fecha (Rafael, 2026-10-04: menu por setor, "quanto menos
    botao melhor"). Fechado, mostra o contador mais importante do setor."""
    return {"tipo": "setor", "chave": chave, "rotulo": rotulo, "icone": icone, "itens": itens,
            "contador": contador, "contador_neutro": contador_neutro, "aberto": False}


def _grupos(user, cont):
    """-> (topo, rodape): listas de itens soltos e setores. Cada perfil ve so o
    que pode acessar (mesmos require_role das rotas)."""
    pagamentos = _item("Pagamentos a confirmar", "/financeiro/pagamentos", "banknote", principal=True,
                       contador=cont.get("pagamentos"))
    relatorios = _item("Relatórios", "/relatorios", "file-chart-column", prefixos=("/crm/estado",))  # funil do estado: Market share
    recebimentos = _item("Recebimentos", "/financeiro/recebimentos", "hand-coins")
    if user.role == "financeiro":
        return [_item("Início", "/financeiro/inicio", "house", principal=True), pagamentos, recebimentos, relatorios], []
    portaria = _item("Portaria", "/portaria", "shield-check", principal=True)
    if user.role == "portaria":
        return [portaria], []
    fila = _item("Fila de trabalho", "/vendedor/crm/fila", "list-checks", principal=True, contador=cont.get("fila"))
    carteira = _item("Carteira", "/vendedor/crm/carteira", "users", principal=True, prefixos=("/vendedor/crm/fase",))
    agenda = _item("Agenda", "/vendedor/crm/agenda", "calendar-days", principal=True)
    cadastros = _item("Cadastros", "/vendedor/crm/base", "user-pen", contador=cont.get("cadastros"), contador_neutro=True)
    pedidos_portal = _item("Pedidos do portal", "/vendedor/crm/pedidos", "package")
    pedidos_cliente = _item("Pedidos por cliente", "/vendedor", "receipt-text")
    # "Avisos" saiu do menu: e o sino do topo (mesma pagina, com o ponto de nao lidos)
    if user.role == "vendedor":
        pedidos = _setor("pedidos", "Pedidos", "package", [dict(pedidos_portal, rotulo="Do portal"),
                                                            dict(pedidos_cliente, rotulo="Por cliente (NetSuite)")])
        return [_item("Início", "/vendedor/crm", "house", principal=True), fila, agenda, carteira, cadastros, pedidos,
                _item("Meu desempenho", "/vendedor/crm/desempenho", "chart-line"), relatorios], []
    fila_logistica = _item("Fila da logística", "/logistica/fila", "list-todo", principal=True,
                           contador=cont.get("fila_logistica"))
    pedidos_logistica = _item("Pedidos", "/logistica", "truck", principal=True, prefixos=("/clientes", "/pedidos"))
    regiao = _item("Pedidos por região", "/logistica/regiao", "map-pin")
    if user.role == "logistica":
        return [_item("Início", "/logistica/inicio", "house", principal=True), fila_logistica,
                dict(pedidos_logistica, rotulo="Logística"), regiao, relatorios], []
    # admin: por setor, pra seguir o fluxo de cada area (Rafael, 2026-10-04)
    for item in (fila, carteira, agenda, pedidos_logistica):
        item["principal"] = False
    vendas = _setor("vendas", "Vendas", "briefcase-business",
                    [fila, carteira, agenda, cadastros, pedidos_portal, pedidos_cliente,
                     _item("Desempenho", "/vendedor/crm/desempenho", "chart-line")],
                    contador=cont.get("fila"))
    logistica = _setor("logistica", "Logística", "truck", [fila_logistica, pedidos_logistica, regiao, dict(portaria, principal=False)],
                       contador=cont.get("fila_logistica"))
    financeiro = _setor("financeiro", "Financeiro", "banknote",
                        [pagamentos, recebimentos, _item("Parceiros de plano safra", "/crm/parceiros", "handshake")],
                        contador=cont.get("pagamentos"))
    administracao = _setor("admin", "Administração", "settings",
                           ([_item("Pessoas e acessos", "/admin/usuarios", "user-cog")] if user.role == "admin" else [])
                           + [_item("Regras", "/admin/regras", "scale"), _item("Log de atividades", "/crm/log", "history")])
    inicio = _item("Início", "/admin/inicio", "house", principal=True)
    # barra de baixo do celular (4 atalhos): Inicio, Fila de trabalho, Fila da logistica, Relatorios
    fila["principal"] = True
    fila_logistica["principal"] = True
    relatorios["principal"] = True
    pagamentos["principal"] = False
    fila["curto"] = "Vendas"           # na barra as duas filas viravam "Fila" e "Fila"
    fila_logistica["curto"] = "Logística"
    if user.role == "balcao":  # ve tudo; Inicio = o de vendas (todas as carteiras); Administracao so consulta
        inicio = _item("Início", "/vendedor/crm", "house", principal=True)
        logistica["itens"] = [_item("Início da logística", "/logistica/inicio", "layout-dashboard")] + logistica["itens"]
        financeiro["itens"] = [i for i in financeiro["itens"] if i["href"] != "/crm/parceiros"]
        return [inicio, vendas, logistica, financeiro, relatorios], [administracao]
    return [inicio, vendas, logistica, financeiro, relatorios], [administracao]


def menu_lateral(request, user):
    cont = contadores(user)
    urgente = None
    if user.role in ("admin", "financeiro"):
        urgente = _pagamentos_urgentes()
        cont = dict(cont, pagamentos=urgente["n"] if urgente else 0)
    if user.role in ("admin", "logistica"):
        cont = dict(cont, fila_logistica=_fila_logistica())
    topo, rodape = _grupos(user, cont)
    itens = [i for e in topo + rodape for i in (e["itens"] if e.get("tipo") == "setor" else [e])]
    caminho = request.url.path
    alvo = None
    if caminho.startswith("/crm/cliente"):
        via = request.query_params.get("via", "")
        alvo = VIA_PARA_ITEM.get(via) or ("/vendedor/crm/carteira" if user.role in ("vendedor", "admin") else "")
    melhor = None
    for item in itens:
        if alvo is not None:
            if item["href"] == alvo:
                melhor = item
            continue
        for prefixo in item["prefixos"]:
            if caminho == prefixo or caminho.startswith(prefixo + "/"):
                if melhor is None or len(prefixo) > len(melhor["_casou"]):
                    item["_casou"] = prefixo
                    melhor = item
    if melhor:
        melhor["ativo"] = True
        for e in topo + rodape:  # o setor da pagina atual ja vem aberto
            if e.get("tipo") == "setor" and any(i is melhor for i in e["itens"]):
                e["aberto"] = True
    principais = [item for item in itens if item["principal"]][:4]
    return {"topo": topo, "rodape": rodape, "principais": principais, "contadores": cont, "urgente": urgente,
            "papel": PAPEL_ROTULO.get(user.role, user.role)}


_PREFIXOS_CLIENTE = {"fazenda", "faz", "grupo", "sitio", "sítio", "chacara", "chácara", "condominio", "condomínio"}
_LIGACOES = {"de", "da", "do", "das", "dos", "e"}


def iniciais_cliente(nome):
    """Avatar da lista de clientes: 'Fazenda Nome Exemplo' -> 'NE' (o prefixo
    Fazenda/Grupo se repete em quase todo cliente e nao ajuda a distinguir)."""
    palavras = re.findall(r"[^\W_]+", nome or "")
    while len(palavras) > 1 and palavras[0].lower() in _PREFIXOS_CLIENTE:
        palavras.pop(0)
    palavras = [p for p in palavras if p.lower() not in _LIGACOES] or palavras
    if not palavras:
        return "?"
    return "".join(p[0] for p in palavras[:2]).upper()


def iniciais(nome):
    partes = [p for p in (nome or "").split() if p[:1].isalpha()]
    if not partes:
        return "?"
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()
