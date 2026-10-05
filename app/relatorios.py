"""Pagina de Relatorios (Rafael, 2026-10-02): uma pagina so, com busca e filtro
por setor; todo relatorio tem a mesma cara (filtros com padrao sensato,
totais numa faixa, tabela, Excel e Imprimir/PDF). Cada perfil ve so o que e
dele: vendedor = propria carteira; logistica e financeiro = relatorios do
setor; admin = todos.

Cada relatorio e uma funcao gerar(db, user, f) que devolve:
    {"totais": [(rotulo, valor_texto)], "colunas": [(rotulo, tipo)],
     "linhas": [[valores...]], "links": [url ou None], "nota": texto ou None}
tipos de coluna: texto, data, int, t (toneladas), moeda, pct, dias.
Dado que o portal nao tem aparece como falta, nunca inventado.
"""
import datetime as dt
import io
import re
from collections import Counter, defaultdict
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import config
from .auth import require_role
from . import expedicao
from .crm_routes import TIPO_LOG_LABEL, TIPOS_CONTATO_REAL, _e_artefato_import, _templates
from .database import get_db
from .models import (ABAS_EM_ABERTO, DATA_DESCONHECIDA, ESTADOS_OPERACAO, FASE_LABEL, FORMA_PRAZO, MOTIVO_PERDIDO, PRODUTOS, RESULTADO_CONTATO,
                     STATUS_PEDIDO_ABERTO, STATUS_PEDIDO_CANCELADO, STATUS_PEDIDO_FINALIZADO, STATUS_PROPOSTA_ABERTA,
                     TEMPERATURA_LABEL, AreaEstado, ClienteCRM, ContatoCRM, Pedido, PedidoCRM, PropostaCRM, User,
                     inicio_ciclo)

router = APIRouter()

PERFIS = ("admin", "vendedor", "logistica", "financeiro")
SETORES = ["Vendas", "Cadastro", "Logística", "Financeiro", "Administração"]
PERIODOS = [("ciclo", "Este ciclo (desde 1º/nov)"), ("30d", "Últimos 30 dias"), ("mes", "Este mês"),
            ("mes_passado", "Mês passado"), ("ano", "Este ano"), ("tudo", "Todo o período"), ("personalizado", "Escolher datas")]


def _br(valor, casas=0):
    return f"{valor:,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _hoje():
    return dt.date.today()


def _intervalo(f):
    """Datas do filtro de periodo -> (inicio, fim, rotulo); None = sem limite."""
    hoje = _hoje()
    p = f["periodo"]
    if p == "30d":
        ini, fim = hoje - dt.timedelta(days=30), hoje
    elif p == "mes":
        ini, fim = hoje.replace(day=1), hoje
    elif p == "mes_passado":
        fim = hoje.replace(day=1) - dt.timedelta(days=1)
        ini = fim.replace(day=1)
    elif p == "ano":
        ini, fim = hoje.replace(month=1, day=1), hoje
    elif p == "tudo":
        return None, None, "todo o período"
    elif p == "personalizado":
        ini, fim = f["de"], f["ate"]
        if not ini and not fim:
            return None, None, "todo o período"
    else:
        ini, fim = inicio_ciclo(hoje), hoje
    rotulo = (f"{ini.strftime('%d/%m/%Y') if ini else 'início'} a {fim.strftime('%d/%m/%Y') if fim else 'hoje'}")
    return ini, fim, rotulo


def _data(valor):
    """Data do historico pronta pro relatorio: texto do SQLite vira datetime e
    a sentinela DATA_DESCONHECIDA (planilha sem data) vira None -- nunca
    mostrar 01/01/2020 como se fosse data de verdade."""
    if isinstance(valor, str):
        valor = dt.datetime.fromisoformat(valor)
    if valor is None or valor == DATA_DESCONHECIDA or valor.year < 2000:
        return None
    return valor


def _no_periodo(quando, ini, fim):
    if quando is None:
        return ini is None and fim is None
    d = quando.date() if isinstance(quando, dt.datetime) else quando
    return (ini is None or d >= ini) and (fim is None or d <= fim)


def _clientes(db, user, f):
    """Clientes no escopo de quem pede (vendedor: a propria carteira) + filtros."""
    q = db.query(ClienteCRM)
    if user.role == "vendedor":
        q = q.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    elif f["vendedor"]:
        q = q.filter(ClienteCRM.vendedor_nome == f["vendedor"])
    if f["uf"]:
        q = q.filter(ClienteCRM.uf == f["uf"])
    return q


def _pedidos_crm(db, user, f):
    q = db.query(PedidoCRM).join(ClienteCRM, PedidoCRM.cliente_id == ClienteCRM.id)
    if user.role == "vendedor":
        q = q.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    elif f["vendedor"]:
        q = q.filter(ClienteCRM.vendedor_nome == f["vendedor"])
    if f["uf"]:
        q = q.filter(ClienteCRM.uf == f["uf"])
    if f["produto"]:
        q = q.filter(PedidoCRM.produto == f["produto"])
    return q


def _link(user, cliente_id, aba=None):
    if user.role == "financeiro" or not cliente_id:
        return None
    return f"/crm/cliente/{cliente_id}" + (f"?aba={aba}" if aba else "")


def _fechamentos(db, numeros):
    """Registro de finalizacao de cada pedido (data e texto, no historico)."""
    if not numeros:
        return {}
    return {h.numero: h for h in db.query(ContatoCRM).filter(ContatoCRM.tipo == "pedido_finalizado",
                                                              ContatoCRM.numero.in_(numeros)).order_by(ContatoCRM.data)}


def _volume_texto(texto):
    m = re.search(r"([\d.,]+)\s*t\b", texto or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", ".") if "," in m.group(1) else m.group(1))
    except ValueError:
        return None


FORMA_CURTA = {"A vista": "À vista", "A prazo": "A prazo", "Plano safra": "Plano safra"}


# ------------------------------------------------------------------ Vendas

def r_vendas(db, user, f):
    ini, fim, _ = _intervalo(f)
    linhas, links = [], []
    pedidos = [p for p in _pedidos_crm(db, user, f).filter(PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all()
               if _no_periodo(p.criado_em, ini, fim)]
    for p in sorted(pedidos, key=lambda p: p.criado_em, reverse=True):
        c = p.cliente
        linhas.append([p.criado_em, "Portal", p.codigo, c.fazenda, c.vendedor_nome, c.uf, p.produto, p.volume,
                       p.valor_total(), FORMA_CURTA.get(p.pagamento, p.pagamento),
                       "Em aberto" if p.status == STATUS_PEDIDO_ABERTO else "Finalizado"])
        links.append(_link(user, c.id, "pedidos"))
    # Compras antigas registradas na planilha (o volume so existe no texto)
    if not f["produto"]:
        ids = {c.id: c for c in _clientes(db, user, f).all()}
        for h in db.query(ContatoCRM).filter(ContatoCRM.tipo == "compra", ContatoCRM.cliente_id.in_(list(ids))).all():
            if not _no_periodo(_data(h.data), ini, fim):
                continue
            c = ids[h.cliente_id]
            pag = re.search(r"\(([^)]+)\)", h.texto or "")
            linhas.append([_data(h.data), "Planilha", None, c.fazenda, c.vendedor_nome, c.uf, None, _volume_texto(h.texto),
                           None, pag.group(1) if pag else None, "Registrada"])
            links.append(_link(user, c.id))
        ordem = sorted(range(len(linhas)), key=lambda i: linhas[i][0] or dt.datetime.min, reverse=True)
        linhas, links = [linhas[i] for i in ordem], [links[i] for i in ordem]
    volume = sum(l[7] or 0 for l in linhas)
    valor = sum(l[8] or 0 for l in linhas)
    n_portal = sum(1 for l in linhas if l[1] == "Portal")
    # Preco medio (Rafael, 2026-10-04, no lugar do volume medio): so as vendas que
    # TEM preco (pedidos do portal). A compra da planilha nao tem valor -- entrar
    # com o volume dela no divisor dava um preco falso (~R$ 3,70/t).
    com_preco = [l for l in linhas if l[8] and l[7]]
    volume_com_preco = sum(l[7] for l in com_preco)
    preco_medio = f"R$ {_br(sum(l[8] for l in com_preco) / volume_com_preco, 2)}/t" if volume_com_preco else "—"
    return {
        "totais": [("Vendas", _br(len(linhas))), ("Volume", f"{_br(volume)} t"), ("Valor (pedidos do portal)", f"R$ {_br(valor, 2)}"),
                   ("Preço médio (pedidos do portal)", preco_medio)],
        "colunas": [("Data", "data"), ("Origem", "texto"), ("Pedido", "id"), ("Cliente", "nome"), ("Vendedor", "texto"),
                    ("UF", "texto"), ("Produto", "texto"), ("Volume", "t"), ("Valor", "moeda"), ("Pagamento", "texto"),
                    ("Situação", "texto")],
        "linhas": linhas, "links": links, "col_link": 3,
        "nota": (f"{n_portal} pedido(s) gerados no portal e {len(linhas) - n_portal} compra(s) registradas na planilha "
                 "(a planilha não tem produto nem valor)." if len(linhas) - n_portal else None),
    }


def r_propostas(db, user, f):
    q = db.query(PropostaCRM).join(ClienteCRM, PropostaCRM.cliente_id == ClienteCRM.id).filter(
        PropostaCRM.status == STATUS_PROPOSTA_ABERTA)
    if user.role == "vendedor":
        q = q.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    elif f["vendedor"]:
        q = q.filter(ClienteCRM.vendedor_nome == f["vendedor"])
    if f["uf"]:
        q = q.filter(ClienteCRM.uf == f["uf"])
    if f["produto"]:
        q = q.filter(PropostaCRM.produto == f["produto"])
    agora = dt.datetime.utcnow()
    linhas, links = [], []
    for p in sorted(q.all(), key=lambda p: p.atualizado_em or p.criado_em):
        c = p.cliente
        ref = p.atualizado_em or p.criado_em
        linhas.append([p.numero, c.fazenda, c.vendedor_nome, c.uf, p.produto, p.volume, p.preco, p.valor_total(),
                       FORMA_CURTA.get(p.pagamento, p.pagamento), TEMPERATURA_LABEL.get(c.temperatura, "—"),
                       (agora - p.criado_em).days if p.criado_em else None, ref])
        links.append(_link(user, c.id, "propostas"))
    return {
        "totais": [("Propostas", _br(len(linhas))), ("Valor em negociação", f"R$ {_br(sum(l[7] for l in linhas), 2)}"),
                   ("Volume", f"{_br(sum(l[5] for l in linhas))} t"),
                   ("Paradas há 10+ dias", _br(sum(1 for l in linhas if l[11] and (agora - l[11]).days >= 10)))],
        "colunas": [("Proposta", "id"), ("Cliente", "nome"), ("Vendedor", "texto"), ("UF", "texto"), ("Produto", "texto"),
                    ("Volume", "t"), ("Preço (R$/t)", "moeda"), ("Valor", "moeda"), ("Pagamento", "texto"),
                    ("Clima", "texto"), ("Aberta há (dias)", "int"), ("Última atualização", "data")],
        "linhas": linhas, "links": links, "col_link": 1, "nota": None,
    }


FASES_ORDEM = ["a_contactar", "contactado", "proposta", "realizado", "nao_usara", "perdido"]


def _lista_clientes(db, user, ids, titulo, no_periodo=None, mostrar_vendedor=True):
    """Quem esta por tras de um numero do relatorio (Rafael, 2026-10-03: "nao
    so ver o numero dos clientes, mas quem foram"). `no_periodo`: {cliente_id:
    (data mais recente, texto do que aconteceu)} -- ordena pelo mais recente."""
    clientes = db.query(ClienteCRM).filter(ClienteCRM.id.in_(list(ids))).all() if ids else []
    ultimo, so_sem_data = _ultimo_contato_real(db, [c.id for c in clientes])
    if no_periodo:
        clientes.sort(key=lambda c: (no_periodo.get(c.id, (dt.datetime.min, ""))[0], c.fazenda), reverse=True)
    else:
        clientes.sort(key=lambda c: (c.fazenda or "").lower())
    colunas = [("Cliente", "nome")] + ([("Vendedor", "texto")] if mostrar_vendedor else []) + [
        ("UF", "texto"), ("Cidade", "texto"), ("Etapa", "texto")]
    if no_periodo:
        colunas.append(("No período", "longo"))
    colunas += [("Telefone", "tel"), ("Último contato", "data")]
    linhas = []
    for c in clientes:
        linha = [c.fazenda] + ([c.vendedor_nome] if mostrar_vendedor else []) + [c.uf, c.cidade, FASE_LABEL.get(c.fase)]
        if no_periodo:
            linha.append(no_periodo.get(c.id, (None, None))[1])
        linha += [c.telefone, ultimo.get(c.id) or ("Antes do portal, sem data" if c.id in so_sem_data else None)]
        linhas.append(linha)
    return {"titulo": titulo, "colunas": colunas, "linhas": linhas, "links": [_link(user, c.id) for c in clientes], "col_link": 0}


def _detalhe(db, user, f, linhas, colunas, ids_por_celula, rotulo_linha, no_periodo_por_celula=None, mostrar_vendedor=True):
    """Celulas com lista (i, j) e, se o usuario clicou numa (f["ver"]), a lista."""
    celulas = {k for k, v in ids_por_celula.items() if v}
    ver = f.get("ver")
    if not ver or ver not in celulas:
        return celulas, None
    i, j = ver
    ids = ids_por_celula[ver]
    n = len(ids)
    clientes = f"{n} cliente{'s' if n != 1 else ''}"
    valor = linhas[i][j]
    # Registros e propostas contam eventos: "Propostas: 23 em 16 clientes"
    titulo = f"{rotulo_linha(linhas[i])} · {colunas[j][0]}: " + (f"{_br(valor)} em {clientes}" if isinstance(valor, int) and valor != n else clientes)
    notas = (no_periodo_por_celula or {}).get(ver)
    return celulas, _lista_clientes(db, user, ids, titulo, notas, mostrar_vendedor)


FASES_MOVIMENTO = ["contactado", "proposta", "realizado", "nao_usara", "perdido"]


def r_funil_movimentos(db, user, f):
    """Funil do periodo (Rafael, 2026-10-03): quantos clientes ENTRARAM em cada
    etapa no periodo, por vendedor (carteira) ou estado. Um cliente conta uma
    vez por etapa. "Clientes a contactar" fica de fora: ninguem "entra" nela
    (a virada do ciclo em 1/nov nao grava movimento). A etapa de origem quase
    nunca foi gravada (planilha): aparece so quando existe, nunca inventada."""
    ini, fim, _ = _intervalo(f)
    por = f["agrupar"]
    clientes = {c.id: c for c in _clientes(db, user, f).all()}

    def grupo(c):
        return (c.vendedor_nome or "Sem vendedor") if por == "vendedor" else c.uf

    q = (db.query(ContatoCRM.cliente_id, ContatoCRM.data, ContatoCRM.fase_origem, ContatoCRM.fase_destino, ContatoCRM.texto)
         .filter(ContatoCRM.tipo == "mudanca_fase", ContatoCRM.cliente_id.in_(list(clientes))))
    if ini:
        q = q.filter(ContatoCRM.data >= dt.datetime.combine(ini, dt.time.min))
    if fim:
        q = q.filter(ContatoCRM.data <= dt.datetime.combine(fim, dt.time.max))
    ids = defaultdict(set)
    movimentos = defaultdict(list)  # (grupo, etapa ou "total", cliente) -> [(data, texto)]
    for cid, data, origem, destino, texto in q:
        data = _data(data)
        if data is None or destino not in FASES_MOVIMENTO:
            continue
        g = grupo(clientes[cid])
        de = FASE_LABEL.get(origem) if origem else None
        if not de:
            m = re.search(r":\s*(.+?)\s*->", texto or "")
            de = m.group(1) if m else None
        txt = f"{de + ' ' if de else ''}→ {FASE_LABEL.get(destino, destino)} {data.strftime('%d/%m')}"
        for chave in (destino, "total"):
            ids[(g, chave)].add(cid)
            movimentos[(g, chave, cid)].append((data, txt))
    grupos = sorted({grupo(c) for c in clientes.values()}, key=lambda g: (-len(ids[(g, "total")]), str(g)))
    linhas = [[g] + [len(ids[(g, fz)]) for fz in FASES_MOVIMENTO] + [len(ids[(g, "total")])] for g in grupos]
    colunas = ([("Vendedor" if por == "vendedor" else "Estado", "texto")] + [(FASE_LABEL[fz], "int") for fz in FASES_MOVIMENTO]
               + [("Clientes que se moveram", "int")])
    chaves = FASES_MOVIMENTO + ["total"]
    por_celula, notas = {}, {}
    for i, l in enumerate(linhas):
        for j, chave in enumerate(chaves, 1):
            por_celula[(i, j)] = ids[(l[0], chave)]
            if f.get("ver") == (i, j):
                notas[(i, j)] = {}
                for cid in ids[(l[0], chave)]:
                    evs = sorted(movimentos[(l[0], chave, cid)])
                    notas[(i, j)][cid] = (evs[-1][0], " · ".join(t for _, t in evs))
    celulas, ver = _detalhe(db, user, f, linhas, colunas, por_celula, lambda l: l[0], notas, mostrar_vendedor=por != "vendedor")

    def distintos(chave):
        return len(set().union(*(ids[(g, chave)] for g in grupos))) if grupos else 0

    return {
        "celulas": celulas, "ver": ver,
        "totais": [("Clientes que se moveram", _br(distintos("total"))), ("Entraram em proposta", _br(distintos("proposta"))),
                   ("Vendas fechadas", _br(distintos("realizado"))), ("Perdidos", _br(distintos("perdido")))],
        "colunas": colunas, "linhas": linhas, "links": [None] * len(linhas), "col_link": None,
        "nota": ("Movimentos no período: quantos clientes entraram em cada etapa (um cliente conta uma vez por etapa). "
                 "Clique num número para ver quem são e quando mudaram. A virada do ciclo (1º/nov) não conta como movimento."),
    }


def r_funil(db, user, f):
    if f.get("modo") == "movimentos":
        return r_funil_movimentos(db, user, f)
    por = f["agrupar"]
    grupos = defaultdict(Counter)
    ids = defaultdict(set)
    for c in _clientes(db, user, f).all():
        g = (c.vendedor_nome or "Sem vendedor") if por == "vendedor" else c.uf
        grupos[g][c.fase] += 1
        ids[(g, c.fase)].add(c.id)
        ids[(g, "total")].add(c.id)
    linhas = []
    for nome, cont in sorted(grupos.items(), key=lambda x: -sum(x[1].values())):
        total = sum(cont.values())
        linhas.append([nome] + [cont.get(fz, 0) for fz in FASES_ORDEM] + [total, cont.get("realizado", 0) / total if total else 0])
    total_geral = sum(l[7] for l in linhas)
    realizados = sum(l[4] for l in linhas)
    colunas = ([("Vendedor" if por == "vendedor" else "Estado", "texto")] + [(FASE_LABEL[fz], "int") for fz in FASES_ORDEM]
               + [("Total", "int"), ("% realizado", "pct")])
    por_celula = {}
    for i, l in enumerate(linhas):
        for j, fz in enumerate(FASES_ORDEM, 1):
            por_celula[(i, j)] = ids[(l[0], fz)]
        por_celula[(i, 7)] = ids[(l[0], "total")]
    celulas, ver = _detalhe(db, user, f, linhas, colunas, por_celula, lambda l: l[0], mostrar_vendedor=por != "vendedor")
    return {
        "celulas": celulas, "ver": ver,
        "totais": [("Clientes", _br(total_geral)), ("Em proposta", _br(sum(l[3] for l in linhas))), ("Realizados", _br(realizados)),
                   ("Conversão", f"{_br(realizados / total_geral * 100, 1) if total_geral else '0'}%")],
        "colunas": colunas,
        "linhas": linhas, "links": [None] * len(linhas), "col_link": None,
        "nota": "Foto de agora: a etapa atual de cada cliente da carteira. Clique num número para ver quem são os clientes.",
    }


SEM_RESPOSTA = ("nao_atendeu", "contato_invalido")
RESULTADO_CURTO = {"contato": "Conversamos", "nao_atendeu": "Não atendeu", "retornar_depois": "Pediu pra ligar depois",
                   "sem_interesse_agora": "Sem interesse agora", "contato_invalido": "Contato não funciona",
                   "proposta": "Proposta enviada", "venda": "Venda", "sem_interesse": "Sem interesse",
                   "perdido": "Perdido"}


def r_atividade(db, user, f):
    """Atividade pela CARTEIRA (vendedor do cliente), nao por quem registrou
    (Rafael, 2026-10-03): o historico da planilha nao tem autor, e o que o
    admin registra pelo vendedor conta pra carteira certa. "Trabalhado",
    venda e perda seguem a mesma conta da tela Desempenho
    (`_atividade_no_periodo`): qualquer registro no periodo, menos o
    "Importado da planilha"; venda/perda = mudanca de etapa pra
    realizado/perdido. Vendedor sem nenhuma atividade aparece com zeros --
    e justamente o que o relatorio precisa mostrar."""
    ini, fim, _ = _intervalo(f)
    por_semana = f["agrupar"] == "semana"
    clientes = {c.id: c for c in _clientes(db, user, f).all()}

    def nome(c):
        return c.vendedor_nome or "Sem vendedor"

    carteira = Counter(nome(c) for c in clientes.values())
    hoje = _hoje()
    atrasados = Counter(nome(c) for c in clientes.values()
                        if c.proximo_retorno_em and c.proximo_retorno_em < hoje and c.fase not in ("perdido", "nao_usara"))
    cont = defaultdict(Counter)
    trabalhados, vendas, perdidos = defaultdict(set), defaultdict(set), defaultdict(set)
    ids = defaultdict(set)             # (chave, metrica) -> clientes
    eventos = defaultdict(list)        # (chave, cliente) -> [(data, metricas, texto)]
    q = (db.query(ContatoCRM.cliente_id, ContatoCRM.tipo, ContatoCRM.texto, ContatoCRM.data, ContatoCRM.resultado,
                  ContatoCRM.fase_destino).filter(ContatoCRM.cliente_id.in_(list(clientes))))
    if ini:
        q = q.filter(ContatoCRM.data >= dt.datetime.combine(ini, dt.time.min))
    if fim:
        q = q.filter(ContatoCRM.data <= dt.datetime.combine(fim, dt.time.max))
    for cid, tipo, texto, data, resultado, destino in q:
        data = _data(data)
        if data is None or _e_artefato_import(tipo, texto):
            continue
        chave = data.date() - dt.timedelta(days=data.weekday()) if por_semana else nome(clientes[cid])
        trabalhados[chave].add(cid)
        metricas = {"trabalhados"}
        if tipo == "nota":
            cont[chave]["registros"] += 1
            metricas.add("registros")
            if resultado in SEM_RESPOSTA:
                cont[chave]["sem_resposta"] += 1
                metricas.add("sem_resposta")
            elif resultado:
                cont[chave]["conversamos"] += 1
                metricas.add("conversamos")
            o_que = RESULTADO_CURTO.get(resultado, "Comentário")
        elif tipo == "proposta":
            cont[chave]["propostas"] += 1
            metricas.add("propostas")
            o_que = "Proposta"
        elif tipo == "pedido":
            cont[chave]["pedidos"] += 1
            metricas.add("pedidos")
            o_que = "Pedido gerado"
        elif tipo == "mudanca_fase" and destino == "realizado":
            vendas[chave].add(cid)
            metricas.add("vendas")
            o_que = "Venda fechada"
        elif tipo == "mudanca_fase" and destino == "perdido":
            perdidos[chave].add(cid)
            metricas.add("perdidos")
            o_que = "Perdido"
        elif tipo == "mudanca_fase":
            o_que = f"Etapa: {FASE_LABEL.get(destino, destino)}"
        else:
            o_que = TIPO_LOG_LABEL.get(tipo, tipo)
        for m in metricas:
            ids[(chave, m)].add(cid)
        eventos[(chave, cid)].append((data, metricas, f"{o_que} {data.strftime('%d/%m')}"))

    def no_periodo(chave, metrica):
        """{cliente: (data mais recente, "Conversamos 12/09 · Proposta 15/09")} so com os eventos da metrica."""
        saida = {}
        for cid in ids[(chave, metrica)]:
            evs = sorted((e for e in eventos[(chave, cid)] if metrica in e[1]), key=lambda e: e[0])
            textos = Counter(e[2] for e in evs)
            ordem = list(dict.fromkeys(e[2] for e in evs))
            saida[cid] = (evs[-1][0], " · ".join(t + (f" ×{textos[t]}" if textos[t] > 1 else "") for t in ordem))
        return saida

    def numeros(chave, total_carteira):
        n = len(trabalhados[chave])
        return [n, n / total_carteira if total_carteira else None, cont[chave]["registros"], cont[chave]["conversamos"],
                cont[chave]["sem_resposta"], cont[chave]["propostas"], cont[chave]["pedidos"], len(vendas[chave]),
                len(perdidos[chave])]

    colunas_numeros = [("Clientes trabalhados", "int"), ("% da carteira", "pct"), ("Registros de contato", "int"),
                       ("Conversamos", "int"), ("Sem resposta", "int"), ("Propostas", "int"), ("Pedidos", "int"),
                       ("Vendas fechadas", "int"), ("Perdidos", "int")]
    total_carteira = len(clientes)
    if por_semana:
        # Toda semana do periodo, inclusive as vazias: semana zerada mostra quem parou.
        semanas = set(trabalhados)
        primeira = ini or (min(semanas) if semanas else None)
        ultima = fim or (max(semanas) if semanas else None)
        if primeira and ultima:
            seg = primeira - dt.timedelta(days=primeira.weekday())
            while seg <= ultima:
                semanas.add(seg)
                seg += dt.timedelta(days=7)
        linhas = [[seg] + numeros(seg, total_carteira) for seg in sorted(semanas, reverse=True)]
        colunas = [("Semana de", "data")] + colunas_numeros
        metrica_da_coluna = {1: "trabalhados", 3: "registros", 4: "conversamos", 5: "sem_resposta", 6: "propostas",
                             7: "pedidos", 8: "vendas", 9: "perdidos"}
    else:
        linhas = [[v, carteira[v]] + numeros(v, carteira[v]) + [atrasados[v]] for v in carteira]
        linhas.sort(key=lambda l: (-l[2], l[0]))
        colunas = ([("Vendedor", "texto"), ("Carteira", "int")] + colunas_numeros
                   + [("Retornos atrasados hoje", "int")])
        metrica_da_coluna = {2: "trabalhados", 4: "registros", 5: "conversamos", 6: "sem_resposta", 7: "propostas",
                             8: "pedidos", 9: "vendas", 10: "perdidos"}
        for c in clientes.values():
            ids[(nome(c), "carteira")].add(c.id)
            if c.proximo_retorno_em and c.proximo_retorno_em < hoje and c.fase not in ("perdido", "nao_usara"):
                ids[(nome(c), "atrasados")].add(c.id)
    por_celula, notas = {}, {}
    for i, l in enumerate(linhas):
        for j, metrica in metrica_da_coluna.items():
            por_celula[(i, j)] = ids[(l[0], metrica)]
            if f.get("ver") == (i, j):
                notas[(i, j)] = no_periodo(l[0], metrica)
        if not por_semana:
            por_celula[(i, 1)] = ids[(l[0], "carteira")]
            por_celula[(i, 11)] = ids[(l[0], "atrasados")]
            if f.get("ver") == (i, 11):
                notas[(i, 11)] = {c.id: (dt.datetime.combine(c.proximo_retorno_em, dt.time.min),
                                         f"Retorno era em {c.proximo_retorno_em.strftime('%d/%m')}")
                                  for c in clientes.values() if c.id in ids[(l[0], "atrasados")]}
    rotulo = (lambda l: "Semana de " + l[0].strftime("%d/%m/%Y")) if por_semana else (lambda l: l[0])
    celulas, ver = _detalhe(db, user, f, linhas, colunas, por_celula, rotulo, notas, mostrar_vendedor=por_semana)
    todos_trabalhados = set().union(*trabalhados.values()) if trabalhados else set()
    todas_vendas = set().union(*vendas.values()) if vendas else set()
    return {
        "totais": [("Clientes trabalhados", f"{_br(len(todos_trabalhados))} de {_br(total_carteira)}"),
                   ("Registros de contato", _br(sum(c["registros"] for c in cont.values()))),
                   ("Propostas", _br(sum(c["propostas"] for c in cont.values()))),
                   ("Pedidos", _br(sum(c["pedidos"] for c in cont.values()))),
                   ("Vendas fechadas", _br(len(todas_vendas)))],
        "colunas": colunas, "linhas": linhas, "links": [None] * len(linhas), "col_link": None,
        "celulas": celulas, "ver": ver,
        "nota": ("Conta pela carteira de cada vendedor, com a mesma regra da tela Desempenho. Clique num número para ver "
                 "quem são os clientes. Antes do portal a planilha só guardava mudança de etapa, proposta, compra e "
                 "retirada: ligações só aparecem a partir do portal."),
    }


def r_perdidos(db, user, f):
    ini, fim, _ = _intervalo(f)
    clientes = {c.id: c for c in _clientes(db, user, f).filter(ClienteCRM.fase.in_(("perdido", "nao_usara"))).all()}
    hist = defaultdict(list)
    for h in db.query(ContatoCRM).filter(ContatoCRM.cliente_id.in_(list(clientes))).order_by(ContatoCRM.data).all():
        hist[h.cliente_id].append(h)
    linhas, links = [], []
    for cid, c in clientes.items():
        hs = hist[cid]
        marco = next((h for h in reversed(hs) if h.tipo == "mudanca_fase" and h.fase_destino == c.fase), None)
        nota = next((h for h in reversed(hs) if h.tipo == "nota" and h.resultado in ("perdido", "sem_interesse")), None)
        quando = _data((marco or nota).data) if (marco or nota) else None
        if not _no_periodo(quando, ini, fim):
            continue
        teve_proposta = any(h.tipo == "proposta" and (quando is None or _data(h.data) is None or h.data <= quando) for h in hs)
        motivo = MOTIVO_PERDIDO.get(c.motivo_perdido) if c.fase == "perdido" else "Não usará"
        detalhe = (nota.texto or "").replace("Motivo: ", "", 1) if nota else None
        if motivo and detalhe and detalhe.startswith(motivo):
            detalhe = detalhe[len(motivo):].lstrip(" .")
        linhas.append([quando, c.fazenda, c.vendedor_nome, c.uf, FASE_LABEL.get(c.fase), motivo or "—", detalhe or "—",
                       "Depois da proposta" if teve_proposta else "Antes da proposta"])
        links.append(_link(user, c.id))
    ordem = sorted(range(len(linhas)), key=lambda i: linhas[i][0] or dt.datetime.min, reverse=True)
    linhas, links = [linhas[i] for i in ordem], [links[i] for i in ordem]
    return {
        "totais": [("Perdidos", _br(sum(1 for l in linhas if l[4] == FASE_LABEL["perdido"]))),
                   ("Não usará", _br(sum(1 for l in linhas if l[4] == FASE_LABEL["nao_usara"]))),
                   ("Antes da proposta", _br(sum(1 for l in linhas if l[7].startswith("Antes")))),
                   ("Depois da proposta", _br(sum(1 for l in linhas if l[7].startswith("Depois"))))],
        "colunas": [("Data", "data"), ("Cliente", "nome"), ("Vendedor", "texto"), ("UF", "texto"), ("Etapa", "texto"),
                    ("Motivo", "longo"), ("Detalhe", "longo"), ("Parou", "texto")],
        "linhas": linhas, "links": links, "col_link": 1,
        "nota": "Sem data = encerrado antes do portal (veio assim da planilha); só aparece em \"Todo o período\".",
    }


def _ultimo_contato_real(db, ids):
    """{cliente_id: data do ultimo contato real} e o conjunto de quem so tem
    contato antigo sem data (planilha) -- esse nao e "nunca contatado"."""
    q = (db.query(ContatoCRM.cliente_id, ContatoCRM.data)
         .filter(ContatoCRM.cliente_id.in_(ids), ContatoCRM.tipo.in_(TIPOS_CONTATO_REAL),
                 ~((ContatoCRM.tipo == "nota") & ContatoCRM.texto.like("Importado da planilha%"))))
    ultimo, sem_data = {}, set()
    for cid, data in q.all():
        data = _data(data)
        if data is None:
            sem_data.add(cid)
        elif cid not in ultimo or data > ultimo[cid]:
            ultimo[cid] = data
    return ultimo, sem_data - set(ultimo)


def r_sem_contato(db, user, f):
    dias = f["dias"]
    clientes = _clientes(db, user, f).filter(ClienteCRM.fase.notin_(("perdido", "nao_usara"))).all()
    ultimo, so_sem_data = _ultimo_contato_real(db, [c.id for c in clientes])
    agora = dt.datetime.utcnow()
    linhas, links, grupo = [], [], []
    for c in clientes:
        u = ultimo.get(c.id)
        sem = (agora - u).days if u else None
        if sem is not None and sem < dias:
            continue
        linhas.append([c.fazenda, c.vendedor_nome, c.uf, FASE_LABEL.get(c.fase), c.telefone,
                       u or ("Antes do portal, sem data" if c.id in so_sem_data else "Nunca"), sem])
        links.append(_link(user, c.id, "comentario"))
        grupo.append(0 if u is None and c.id not in so_sem_data else (1 if u is None else 2))
    ordem = sorted(range(len(linhas)), key=lambda i: (grupo[i], -(linhas[i][6] or 0)))
    linhas, links, grupo = [linhas[i] for i in ordem], [links[i] for i in ordem], [grupo[i] for i in ordem]
    return {
        "totais": [("Clientes", _br(len(linhas))), ("Nunca contatados", _br(grupo.count(0))),
                   ("Só contato antigo, sem data", _br(grupo.count(1))), (f"Há {dias}+ dias", _br(grupo.count(2)))],
        "colunas": [("Cliente", "nome"), ("Vendedor", "texto"), ("UF", "texto"), ("Etapa", "texto"), ("Telefone", "texto"),
                    ("Último contato", "data"), ("Dias sem contato", "int")],
        "linhas": linhas, "links": links, "col_link": 0,
        "nota": "Conta conversa, proposta, pedido, compra e retirada; o registro automático da importação não conta.",
    }


def r_market_share(db, user, f):
    clientes = _clientes(db, user, f).all()
    por_uf = defaultdict(list)
    for c in clientes:
        por_uf[c.uf].append(c)
    areas = {a.uf: a for a in db.query(AreaEstado).all()}
    linhas = []
    for uf in sorted(set(por_uf) | set(areas)):
        if f["uf"] and uf != f["uf"]:
            continue
        cs = por_uf.get(uf, [])
        area_cli = sum(c.area_plantada_ha or 0 for c in cs)
        area_comprou = sum(c.area_plantada_ha or 0 for c in cs if c.fase == "realizado")
        ref = areas.get(uf)
        linhas.append([uf, len(cs), area_cli, area_comprou, ref.area_agropecuaria_ha if ref else None,
                       (area_cli / ref.area_agropecuaria_ha) if ref and ref.area_agropecuaria_ha else None])
    placeholder = any(a.placeholder for a in areas.values())
    return {
        "totais": [("Clientes", _br(sum(l[1] for l in linhas))), ("Área dos clientes", f"{_br(sum(l[2] for l in linhas))} ha"),
                   ("Área de quem comprou", f"{_br(sum(l[3] for l in linhas))} ha")],
        "colunas": [("Estado", "texto"), ("Clientes", "int"), ("Área dos clientes (ha)", "num"), ("Área de quem comprou (ha)", "num"),
                    ("Área plantada do estado (ha)", "num"), ("% da área plantada do estado", "pct")],
        # Clicar no estado abre o funil dele (admin/logistica; o vendedor nao abre /crm/estado). O botao de buscar o
        # ano novo no IBGE fica aqui desde que o quadro "Clientes por estado" saiu do Inicio (Rafael, 2026-10-04).
        "linhas": linhas, "col_link": 0,
        "links": [f"/crm/estado/{l[0]}" if user.role in ("admin", "logistica") else None for l in linhas],
        "acao_ibge": user.role == "admin",
        "nota": ("A área de referência dos estados ainda é provisória (não confirmada com o IBGE)." if placeholder else
                 f"Área plantada do estado: {next(iter({a.fonte for a in areas.values()}), 'IBGE')}. No IBGE, soja e milho "
                 "safrinha na mesma terra contam 2 vezes (pesa em MT e GO)."),
    }


# ------------------------------------------------------------------ Cadastro

def r_cadastros(db, user, f):
    linhas, links = [], []
    cont = Counter()
    for c in _clientes(db, user, f).filter(ClienteCRM.fase.notin_(("perdido", "nao_usara"))).order_by(ClienteCRM.fazenda).all():
        falta = c.campos_faltando()
        if not falta:
            continue
        for x in falta:
            cont["telefone" if x.startswith("telefone") else x] += 1
        linhas.append([c.fazenda, c.vendedor_nome, c.uf, FASE_LABEL.get(c.fase), ", ".join(falta)])
        links.append(_link(user, c.id) and f"/crm/cliente/{c.id}/editar")
    return {
        "totais": [("Clientes", _br(len(linhas))), ("Sem área plantada", _br(cont["área plantada"])),
                   ("Telefone faltando ou inválido", _br(cont["telefone"])), ("Sem cidade", _br(cont["cidade"]))],
        "colunas": [("Cliente", "nome"), ("Vendedor", "texto"), ("UF", "texto"), ("Etapa", "texto"), ("Falta", "longo")],
        "linhas": linhas, "links": links, "col_link": 0, "nota": None,
    }


def r_contatos_resolver(db, user, f):
    clientes = _clientes(db, user, f).filter(ClienteCRM.precisa_ajuda.is_(True)).order_by(ClienteCRM.fazenda).all()
    desde = dict(db.query(ContatoCRM.cliente_id, func.max(ContatoCRM.data))
                 .filter(ContatoCRM.cliente_id.in_([c.id for c in clientes]),
                         ((ContatoCRM.tipo == "nota") & (ContatoCRM.resultado == "contato_invalido"))
                         | ((ContatoCRM.tipo == "dados") & ContatoCRM.texto.like("Enviado ao administrador%")))
                 .group_by(ContatoCRM.cliente_id).all())
    linhas = [[c.fazenda, c.vendedor_nome, c.uf, FASE_LABEL.get(c.fase), c.telefone, c.motivo_ajuda or "—", _data(desde.get(c.id))]
              for c in clientes]
    return {
        "totais": [("Casos em aberto", _br(len(linhas))), ("Vindos da planilha", _br(sum(1 for l in linhas if l[6] is None)))],
        "colunas": [("Cliente", "nome"), ("Vendedor", "texto"), ("UF", "texto"), ("Etapa", "texto"), ("Telefone atual", "texto"),
                    ("Motivo", "longo"), ("Desde", "data")],
        "linhas": linhas, "links": [_link(user, c.id) for c in clientes], "col_link": 0,
        "nota": "Sem data = caso que já veio marcado da planilha.",
    }


# ------------------------------------------------------------------ Logistica

def _a_retirar(db, user, f, so_vencidos=False):
    linhas, links = [], []
    # Pedido do portal ja lancado no NetSuite aparece uma vez so: pela linha do NetSuite
    for p in _pedidos_crm(db, user, f).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO, PedidoCRM.pedido_netsuite.is_(None)).all():
        if so_vencidos and not p.vencido():
            continue
        c = p.cliente
        saldo = max(0.0, p.volume_total_a_entregar() - (p.volume_retirado or 0))
        dias = p.dias_restantes()
        ton_dia = round(saldo / dias, 2) if dias and dias > 0 and saldo > 0 else None
        if p.vencido():
            sit = "Vencido"
        elif p.aguardando_pagamento():
            sit = "Aguardando pagamento"
        elif p.data_limite_retirada:
            sit = "No prazo"
        else:
            sit = "Sem prazo"
        linhas.append(["Portal", p.codigo, p.criado_em, c.fazenda, c.vendedor_nome, c.uf, p.produto, saldo,
                       p.data_limite_retirada, dias, ton_dia, sit])
        links.append(_link(user, c.id, "pedidos"))
    q = expedicao.query_em_aberto(db)
    if user.role == "vendedor":
        q = q.filter(Pedido.vendedor == user.vendedor_nome)
    elif f["vendedor"]:
        q = q.filter(Pedido.vendedor == f["vendedor"])
    if f["uf"]:
        q = q.filter(Pedido.uf == f["uf"])
    for p in q.all():
        if f["produto"] and p.categoria != f["produto"]:
            continue
        if so_vencidos and not p.vencido():
            continue
        sit = "Vencido" if p.vencido() else ("No prazo" if p.data_limite_retirada else "Sem prazo")
        linhas.append(["NetSuite", p.numero_pedido, p.data_pedido, p.cliente, p.vendedor, p.uf, p.produto, p.saldo,
                       p.data_limite_retirada, p.dias_restantes(), p.ton_dia_necessario(), sit])
        links.append(None)
    ordem = sorted(range(len(linhas)), key=lambda i: (linhas[i][11] != "Vencido", linhas[i][9] if linhas[i][9] is not None else 99999))
    return [linhas[i] for i in ordem], [links[i] for i in ordem]


COLUNAS_RETIRAR = [("Origem", "texto"), ("Pedido", "id"), ("Data", "data"), ("Cliente", "nome"), ("Vendedor", "texto"),
                   ("UF", "texto"), ("Produto", "texto"), ("Saldo a retirar", "t"), ("Prazo", "data"),
                   ("Dias restantes", "int"), ("t/dia necessário", "num"), ("Situação", "texto")]


def r_a_retirar(db, user, f):
    linhas, links = _a_retirar(db, user, f)
    return {
        "totais": [("Pedidos", _br(len(linhas))), ("Saldo a retirar", f"{_br(sum(l[7] or 0 for l in linhas))} t"),
                   ("Vencidos", _br(sum(1 for l in linhas if l[11] == "Vencido"))),
                   ("Sem prazo", _br(sum(1 for l in linhas if l[11] == "Sem prazo")))],
        "colunas": COLUNAS_RETIRAR, "linhas": linhas, "links": links, "col_link": 3,
        "nota": ("Pedidos do portal e do NetSuite em aberto (planilha Expedição: abas Expedição, Parado e Outros produtos). "
                 "Retiradas parciais dos pedidos do portal virão do NetSuite."),
    }


def r_vencidos(db, user, f):
    linhas, links = _a_retirar(db, user, f, so_vencidos=True)
    return {
        "totais": [("Pedidos vencidos", _br(len(linhas))), ("Saldo parado", f"{_br(sum(l[7] or 0 for l in linhas))} t"),
                   ("Mais atrasado", f"{_br(-min((l[9] or 0) for l in linhas))} dias" if linhas else "—")],
        "colunas": COLUNAS_RETIRAR, "linhas": linhas, "links": links, "col_link": 3, "nota": None,
    }


def r_retiradas(db, user, f):
    ini, fim, _ = _intervalo(f)
    finalizados = _pedidos_crm(db, user, f).filter(PedidoCRM.status == STATUS_PEDIDO_FINALIZADO).all()
    fech = _fechamentos(db, [p.numero for p in finalizados])
    linhas, links = [], []
    for p in finalizados:
        h = fech.get(p.numero)
        quando = _data(h.data) if h else None
        if not _no_periodo(quando, ini, fim):
            continue
        texto = (h.texto if h else "") or ""
        if "pago antecipado" in texto:
            como = "Retirada parcial — sobra virou crédito"
        elif "por retirada" in texto:
            como = "Retirada parcial — pedido ajustado ao retirado"
        elif h:
            como = "Retirou o total"
        else:
            como = "—"
        linhas.append([quando, "Portal", p.codigo, p.cliente.fazenda, p.cliente.vendedor_nome, p.produto, p.volume_retirado,
                       como, p.saldo_credito_valor])
        links.append(_link(user, p.cliente_id, "pedidos"))
    if not f["produto"]:
        ids = {c.id: c for c in _clientes(db, user, f).all()}
        for h in db.query(ContatoCRM).filter(ContatoCRM.tipo == "retirada", ContatoCRM.cliente_id.in_(list(ids))).all():
            if not _no_periodo(_data(h.data), ini, fim):
                continue
            c = ids[h.cliente_id]
            linhas.append([_data(h.data), "Planilha", None, c.fazenda, c.vendedor_nome, None, _volume_texto(h.texto), "—", None])
            links.append(_link(user, c.id))
    ordem = sorted(range(len(linhas)), key=lambda i: linhas[i][0] or dt.datetime.min, reverse=True)
    linhas, links = [linhas[i] for i in ordem], [links[i] for i in ordem]
    return {
        "totais": [("Registros", _br(len(linhas))), ("Retirado", f"{_br(sum(l[6] or 0 for l in linhas))} t"),
                   ("Retiradas parciais", _br(sum(1 for l in linhas if l[7].startswith("Retirada parcial")))),
                   ("Créditos gerados", f"R$ {_br(sum(l[8] or 0 for l in linhas), 2)}")],
        "colunas": [("Data", "data"), ("Origem", "texto"), ("Pedido", "id"), ("Cliente", "nome"), ("Vendedor", "texto"),
                    ("Produto", "texto"), ("Retirado", "t"), ("Como fechou", "longo"), ("Crédito gerado", "moeda")],
        "linhas": linhas, "links": links, "col_link": 3,
        "nota": "Portal: pedidos finalizados no período. Planilha: retiradas registradas antes do portal.",
    }


# ------------------------------------------------------------------ Financeiro

SITUACAO_PAG = {"sem_comprovante": "Aguardando comprovante", "em_conferencia": "Em conferência", "recusado": "Comprovante recusado",
                "diferenca": "Falta a diferença", "liberado": "Confirmado"}


def r_pagamentos(db, user, f):
    ini, fim, _ = _intervalo(f)
    linhas, links = [], []
    for p in _pedidos_crm(db, user, f).filter(PedidoCRM.pagamento == "A vista", PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all():
        if not _no_periodo(p.criado_em, ini, fim):
            continue
        sit = p.situacao_pagamento()
        if sit is None:
            sit_txt = "Confirmado" if p.pagamento_liberado_em else "Finalizado antes da regra"
        else:
            sit_txt = SITUACAO_PAG[sit]
        if f["situacao"] and sit_txt != f["situacao"]:
            continue
        comps = p.comprovantes
        conferido = max((c.conferido_em for c in comps if c.conferido_em), default=None)
        enviado = max((c.criado_em for c in comps), default=None)
        conferidor = next((c.conferido_por for c in sorted(comps, key=lambda c: c.conferido_em or dt.datetime.min, reverse=True)
                           if c.conferido_por), None)
        horas = round((conferido - min(c.criado_em for c in comps)).total_seconds() / 3600, 1) if conferido and comps else None
        linhas.append([p.codigo, p.criado_em, p.cliente.fazenda, p.cliente.vendedor_nome, p.valor_total(), sit_txt, len(comps),
                       enviado, conferido, conferidor, horas])
        links.append(_link(user, p.cliente_id, "pedidos"))
    cont = Counter(l[5] for l in linhas)
    return {
        "totais": [("Pedidos à vista", _br(len(linhas))), ("Aguardando comprovante", _br(cont["Aguardando comprovante"])),
                   ("Em conferência", _br(cont["Em conferência"])), ("Confirmados", _br(cont["Confirmado"])),
                   ("Valor sem pagamento confirmado", f"R$ {_br(sum(l[4] for l in linhas if l[5] not in ('Confirmado', 'Finalizado antes da regra')), 2)}")],
        "colunas": [("Pedido", "id"), ("Data", "data"), ("Cliente", "nome"), ("Vendedor", "texto"), ("Valor", "moeda"),
                    ("Situação", "texto"), ("Comprovantes", "int"), ("Último envio", "data"), ("Conferido em", "data"),
                    ("Conferido por", "texto"), ("Horas até conferir", "num")],
        "linhas": linhas, "links": links, "col_link": 2, "nota": None,
    }


def r_creditos(db, user, f):
    pedidos = _pedidos_crm(db, user, f).filter(PedidoCRM.saldo_credito_valor > 0).all()
    fech = _fechamentos(db, [p.numero for p in pedidos])
    linhas = [[p.cliente.fazenda, p.cliente.vendedor_nome, p.codigo, p.produto, p.saldo_credito_valor, p.saldo_credito,
               _data(fech[p.numero].data) if p.numero in fech else None] for p in pedidos]
    return {
        "totais": [("Clientes com crédito", _br(len({l[0] for l in linhas}))), ("Total em crédito", f"R$ {_br(sum(l[4] for l in linhas), 2)}")],
        "colunas": [("Cliente", "nome"), ("Vendedor", "texto"), ("Pedido de origem", "id"), ("Produto", "texto"),
                    ("Crédito", "moeda"), ("Equivalia (t)", "t"), ("Gerado em", "data")],
        "linhas": linhas, "links": [_link(user, p.cliente_id, "pedidos") for p in pedidos], "col_link": 0,
        "nota": "O valor em R$ é o que vale: quantas toneladas isso dá depende do preço da próxima negociação.",
    }


def r_prazo_safra(db, user, f):
    ini, fim, _ = _intervalo(f)
    linhas, links = [], []
    for p in _pedidos_crm(db, user, f).filter(PedidoCRM.pagamento.in_(("A prazo", "Plano safra")),
                                              PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all():
        if not _no_periodo(p.criado_em, ini, fim):
            continue
        if p.pagamento == "A prazo":
            como = FORMA_PRAZO.get(p.forma_prazo, "Forma não informada")
            if p.forma_prazo == "boleto":
                como += f" ({p.prazo_parcelas or '30'} dias)"
            elif p.forma_prazo == "periodo":
                como += f" ({p.prazo_periodo_dias} dias)"
            venc = None
            venc_txt = f"{(p.prazo_parcelas or '30').split('/')[0]} dias após a 1ª carga" if p.forma_prazo == "boleto" else "—"
        else:
            como = "Via " + (p.plano_safra_parceiro or "parceiro") if p.plano_safra_modalidade == "cessao" else "Direto com o cliente"
            venc, venc_txt = p.vencimento_pagamento, None
        linhas.append([p.codigo, p.criado_em, p.cliente.fazenda, p.cliente.vendedor_nome, p.pagamento, como, venc or venc_txt,
                       p.valor_total(), "Em aberto" if p.status == STATUS_PEDIDO_ABERTO else "Finalizado"])
        links.append(_link(user, p.cliente_id, "pedidos"))
    return {
        "totais": [("Pedidos", _br(len(linhas))), ("Valor combinado", f"R$ {_br(sum(l[7] for l in linhas), 2)}"),
                   ("A prazo", _br(sum(1 for l in linhas if l[4] == "A prazo"))), ("Plano safra", _br(sum(1 for l in linhas if l[4] == "Plano safra")))],
        "colunas": [("Pedido", "id"), ("Data", "data"), ("Cliente", "nome"), ("Vendedor", "texto"), ("Pagamento", "texto"),
                    ("Como paga", "longo"), ("Vencimento", "data"), ("Valor", "moeda"), ("Pedido está", "texto")],
        "linhas": linhas, "links": links, "col_link": 2,
        "nota": "Mostra o que foi combinado. Se o cliente já pagou só vai aparecer quando o NetSuite estiver ligado.",
    }


# ------------------------------------------------------------------ Administracao

def r_log(db, user, f):
    ini, fim, _ = _intervalo(f)
    ids = {c.id: c for c in _clientes(db, user, f).all()}
    q = (db.query(ContatoCRM).filter(ContatoCRM.cliente_id.in_(list(ids)))
         .order_by(ContatoCRM.data.desc()))
    linhas, links = [], []
    for h in q.limit(5000).all():
        if not _no_periodo(_data(h.data), ini, fim):
            continue
        c = ids[h.cliente_id]
        linhas.append([_data(h.data), TIPO_LOG_LABEL.get(h.tipo, h.tipo), c.fazenda, c.vendedor_nome, h.autor or "Sistema", h.texto])
        links.append(_link(user, c.id))
    return {
        "totais": [("Registros", _br(len(linhas))), ("Clientes", _br(len({l[2] for l in linhas})))],
        "colunas": [("Data", "data"), ("Tipo", "texto"), ("Cliente", "nome"), ("Vendedor", "texto"), ("Quem fez", "texto"),
                    ("Registro", "longo")],
        "linhas": linhas, "links": links, "col_link": 2, "nota": "Até 5.000 registros mais recentes.",
    }


# ------------------------------------------------------------------ Comissoes

def r_comissoes(db, user, f):
    """Comissao liberada no periodo: um recebimento por linha (o dinheiro que
    entrou x o percentual do pedido). Rafael, 2026-10-04."""
    from . import comissao
    ini, fim, _ = _intervalo(f)
    pedidos = _pedidos_crm(db, user, f).filter(PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all()
    linhas, links = [], []
    for p in pedidos:
        for r in p.recebimentos:
            if not _no_periodo(r.data, ini, fim):
                continue
            c = p.cliente
            linhas.append([r.data, p.codigo, c.fazenda, c.vendedor_nome, p.produto, r.valor,
                           r.comissao_pct, (r.valor * r.comissao_pct / 100) if r.comissao_pct is not None else None,
                           {"comprovante": "Comprovante à vista", "ajuste": "Ajuste: pedido à vista antigo"}.get(r.origem, "Registrado pelo financeiro")])
            links.append(_link(user, c.id, "pedidos"))
    ordem = sorted(range(len(linhas)), key=lambda i: linhas[i][0], reverse=True)
    linhas, links = [linhas[i] for i in ordem], [links[i] for i in ordem]
    liberada = sum(l[7] or 0 for l in linhas)
    a_liberar = sum(p.comissao_a_liberar() or 0 for p in pedidos)
    return {
        "totais": [("Comissão liberada", f"R$ {_br(liberada, 2)}"), ("Recebido", f"R$ {_br(sum(l[5] for l in linhas), 2)}"),
                   ("Recebimentos", _br(len(linhas))), ("A liberar (falta receber)", f"R$ {_br(a_liberar, 2)}")],
        "colunas": [("Recebido em", "data"), ("Pedido", "id"), ("Cliente", "nome"), ("Vendedor", "texto"), ("Produto", "texto"),
                    ("Valor recebido", "moeda"), ("% comissão", "num"), ("Comissão", "moeda"), ("Origem", "texto")],
        "linhas": linhas, "links": links, "col_link": 2,
        "nota": "Comissão só sobre o que a empresa recebeu: à vista quando o financeiro confirma o comprovante; carga a carga, "
                "a prazo e plano safra quando o financeiro registra o pagamento (Recebimentos). O percentual é o da regra do dia "
                "em que o pedido foi gerado. \"A liberar\" é o que ainda falta receber dos pedidos, em qualquer data.",
    }


# ------------------------------------------------------------------ Catalogo

V, L, FI, A = ("admin", "vendedor"), ("admin", "logistica", "vendedor"), ("admin", "financeiro", "vendedor"), ("admin",)
RELATORIOS = [
    {"chave": "vendas", "titulo": "Vendas no período", "setor": "Vendas", "icone": "trending-up", "perfis": V, "gerar": r_vendas,
     "descricao": "Pedidos por cliente, vendedor, produto, volume, valor e pagamento", "filtros": ["periodo", "vendedor", "uf", "produto"],
     "busca": "venda pedido faturamento volume"},
    {"chave": "propostas", "titulo": "Propostas em aberto", "setor": "Vendas", "icone": "file-text", "perfis": V, "gerar": r_propostas,
     "descricao": "Valor em negociação, idade da proposta e clima do cliente", "filtros": ["vendedor", "uf", "produto"],
     "busca": "proposta negociação pipeline"},
    {"chave": "funil", "titulo": "Funil por vendedor e estado", "setor": "Vendas", "icone": "filter", "perfis": V, "gerar": r_funil,
     "descricao": "Clientes em cada etapa agora, ou quem mudou de etapa no período", "filtros": ["modo", "periodo", "agrupar", "vendedor", "uf"],
     "agrupar": [("vendedor", "Vendedor"), ("uf", "Estado")],
     "modos": [("agora", "Foto de agora (etapa atual)"), ("movimentos", "Movimentos no período")], "periodo_so_no_modo": "movimentos", "busca": "funil etapa conversão carteira"},
    {"chave": "atividade", "titulo": "Atividade dos vendedores", "setor": "Vendas", "icone": "phone-call", "perfis": V, "gerar": r_atividade,
     "descricao": "Clientes trabalhados, contatos, propostas e vendas por vendedor ou semana", "filtros": ["periodo", "agrupar", "vendedor", "uf"],
     "agrupar": [("vendedor", "Vendedor"), ("semana", "Semana a semana")],
     "busca": "contato ligação atividade produtividade cobertura semana"},
    {"chave": "perdidos", "titulo": "Clientes perdidos e motivos", "setor": "Vendas", "icone": "user-x", "perfis": V, "gerar": r_perdidos,
     "descricao": "Por que perdemos e se foi antes ou depois da proposta", "filtros": ["periodo", "vendedor", "uf"],
     "busca": "perdido motivo concorrência não usará"},
    {"chave": "sem-contato", "titulo": "Carteira sem contato", "setor": "Vendas", "icone": "clock-alert", "perfis": V, "gerar": r_sem_contato,
     "descricao": "Clientes há mais de X dias sem conversa", "filtros": ["dias", "vendedor", "uf"], "busca": "sem contato parado esquecido"},
    {"chave": "market-share", "titulo": "Market share por estado", "setor": "Vendas", "icone": "map", "perfis": V, "gerar": r_market_share,
     "descricao": "Área atendida em relação à área do estado", "filtros": ["vendedor", "uf"], "busca": "market share área hectare estado"},
    {"chave": "cadastros", "titulo": "Cadastros incompletos", "setor": "Cadastro", "icone": "user-pen", "perfis": V, "gerar": r_cadastros,
     "descricao": "Sem área plantada, telefone ou cidade", "filtros": ["vendedor", "uf"], "busca": "cadastro área telefone cidade incompleto"},
    {"chave": "contatos-resolver", "titulo": "Contatos a resolver", "setor": "Cadastro", "icone": "phone-off", "perfis": V,
     "gerar": r_contatos_resolver, "descricao": "Número errado, sem WhatsApp, não retorna", "filtros": ["vendedor", "uf"],
     "busca": "número errado whatsapp telefone contato"},
    {"chave": "a-retirar", "titulo": "Pedidos a retirar", "setor": "Logística", "icone": "truck", "perfis": L, "gerar": r_a_retirar,
     "descricao": "Portal e NetSuite: saldo, prazo e toneladas por dia necessárias", "filtros": ["vendedor", "uf", "produto"],
     "busca": "retirada saldo carregamento prazo"},
    {"chave": "vencidos", "titulo": "Pedidos vencidos", "setor": "Logística", "icone": "triangle-alert", "perfis": L, "gerar": r_vencidos,
     "descricao": "Prazo de retirada estourado com saldo", "filtros": ["vendedor", "uf", "produto"], "busca": "vencido atrasado prazo"},
    {"chave": "retiradas", "titulo": "Retiradas e finalizações", "setor": "Logística", "icone": "package-check", "perfis": L,
     "gerar": r_retiradas, "descricao": "O que foi retirado, como cada pedido fechou e os créditos gerados", "filtros": ["periodo", "vendedor", "uf", "produto"],
     "busca": "retirada finalizado carregado"},
    {"chave": "comissoes", "titulo": "Comissões", "setor": "Financeiro", "icone": "percent", "perfis": FI, "gerar": r_comissoes,
     "descricao": "Comissão liberada no período (sobre o que os clientes pagaram) e quanto falta liberar", "filtros": ["periodo", "vendedor", "uf", "produto"],
     "busca": "comissão comissao vendedor recebimento pagamento"},
    {"chave": "pagamentos", "titulo": "Pagamentos à vista", "setor": "Financeiro", "icone": "banknote", "perfis": FI, "gerar": r_pagamentos,
     "descricao": "Aguardando, em conferência, confirmados e recusados", "filtros": ["periodo", "situacao", "vendedor", "uf"],
     "busca": "pagamento comprovante à vista financeiro conferência"},
    {"chave": "creditos", "titulo": "Créditos de clientes", "setor": "Financeiro", "icone": "wallet", "perfis": FI, "gerar": r_creditos,
     "descricao": "Valor pago e não retirado, por cliente", "filtros": ["vendedor", "uf", "produto"], "busca": "crédito saldo pago"},
    {"chave": "prazo-safra", "titulo": "A prazo e Plano safra", "setor": "Financeiro", "icone": "calendar-clock", "perfis": FI,
     "gerar": r_prazo_safra, "descricao": "Forma de pagamento combinada e datas de vencimento", "filtros": ["periodo", "vendedor", "uf", "produto"],
     "busca": "prazo boleto safra vencimento parceiro cessão"},
    {"chave": "log", "titulo": "Log de atividades", "setor": "Administração", "icone": "history", "perfis": A, "gerar": r_log,
     "descricao": "Tudo o que foi registrado nos clientes", "filtros": ["periodo", "vendedor", "uf"], "busca": "log histórico auditoria"},
]
POR_CHAVE = {r["chave"]: r for r in RELATORIOS}
SITUACOES = ["Aguardando comprovante", "Em conferência", "Comprovante recusado", "Falta a diferença", "Confirmado",
             "Finalizado antes da regra"]


def _filtros(request, user):
    q = request.query_params

    def data(nome):
        try:
            return dt.datetime.strptime(q.get(nome, ""), "%Y-%m-%d").date()
        except ValueError:
            return None
    try:
        dias = max(1, int(q.get("dias", "60")))
    except ValueError:
        dias = 60
    return {
        "periodo": q.get("periodo") if q.get("periodo") in dict(PERIODOS) else "ciclo",
        "de": data("de"), "ate": data("ate"),
        "vendedor": "" if user.role == "vendedor" else q.get("vendedor", ""),
        "uf": q.get("uf", "") if q.get("uf", "") in ESTADOS_OPERACAO else "",
        "produto": q.get("produto", "") if q.get("produto", "") in PRODUTOS else "",
        "dias": dias, "situacao": q.get("situacao", "") if q.get("situacao", "") in SITUACOES else "",
        "agrupar": q.get("agrupar", ""),
        "modo": q.get("modo", ""),
        "ver": tuple(int(x) for x in q.get("ver", "").split(".")) if re.fullmatch(r"\d+\.\d+", q.get("ver", "")) else None,
    }


def _agrupar(rel, valor, user):
    opcoes = [v for v, _ in rel.get("agrupar", []) if not (user.role == "vendedor" and v == "vendedor")]
    if not opcoes:
        return ""
    return valor if valor in opcoes else opcoes[0]


def _modo(rel, valor):
    opcoes = [v for v, _ in rel.get("modos", [])]
    if not opcoes:
        return ""
    return valor if valor in opcoes else opcoes[0]


def _usa_periodo(rel, f):
    return "periodo" in rel["filtros"] and (not rel.get("periodo_so_no_modo") or f.get("modo") == rel["periodo_so_no_modo"])


def _resumo_filtros(rel, f, user):
    partes = []
    if f.get("modo"):
        partes.append(dict(rel["modos"])[f["modo"]])
    if f.get("agrupar"):
        partes.append("Por " + dict(rel["agrupar"])[f["agrupar"]].lower())
    if _usa_periodo(rel, f):
        partes.append("Período: " + _intervalo(f)[2])
    if "dias" in rel["filtros"]:
        partes.append(f"Sem contato há {f['dias']}+ dias")
    if user.role == "vendedor":
        partes.append(f"Carteira: {user.vendedor_nome}")
    elif "vendedor" in rel["filtros"]:
        partes.append("Vendedor: " + (f["vendedor"] or "todos"))
    if "uf" in rel["filtros"]:
        partes.append("Estado: " + (f["uf"] or "todos"))
    if "produto" in rel["filtros"]:
        partes.append("Produto: " + (f["produto"] or "todos"))
    if "situacao" in rel["filtros"] and f["situacao"]:
        partes.append("Situação: " + f["situacao"])
    return " · ".join(partes)


def _visiveis(user):
    papel = "admin" if user.role == "balcao" else user.role  # Balcao de vendas ve o que o admin ve (2026-10-05)
    return [r for r in RELATORIOS if papel in r["perfis"]]


@router.get("/relatorios", response_class=HTMLResponse)
def relatorios(request: Request, user: User = Depends(require_role(*PERFIS)), db: Session = Depends(get_db)):
    visiveis = _visiveis(user)
    chave = request.query_params.get("r")
    rel = POR_CHAVE.get(chave) if chave else None
    if rel and rel not in _visiveis(user):
        rel = None
    resultado, f = None, _filtros(request, user)
    if rel:
        f["agrupar"] = _agrupar(rel, f["agrupar"], user)
        f["modo"] = _modo(rel, f["modo"])
        resultado = rel["gerar"](db, user, f)
    vendedores = []
    if user.role != "vendedor":
        vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct() if v})
    setores = [s for s in SETORES if any(r["setor"] == s for r in visiveis)]
    return _templates(request).TemplateResponse(request, "relatorios.html", {
        "user": user, "relatorios": visiveis, "setores": setores, "rel": rel, "res": resultado, "f": f,
        "periodos": PERIODOS, "estados": ESTADOS_OPERACAO, "produtos": PRODUTOS, "vendedores": vendedores,
        "situacoes": SITUACOES, "resumo_filtros": _resumo_filtros(rel, f, user) if rel else "",
        "limite": config.valor("relatorio_limite_tela"), "query": str(request.query_params), "gerado_em": dt.datetime.now(),
        "url_tudo": "/relatorios?" + urlencode({**dict(request.query_params), "periodo": "tudo", "de": "", "ate": ""}),
        "query_base": urlencode({k: v for k, v in request.query_params.items() if k != "ver"}),
        "migalhas": [("Relatórios", None)],
    })


@router.get("/relatorios/{chave}/excel")
def relatorio_excel(request: Request, chave: str, user: User = Depends(require_role(*PERFIS)), db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rel = POR_CHAVE.get(chave)
    if not rel or user.role not in rel["perfis"]:
        return RedirectResponse("/relatorios", status_code=303)
    f = _filtros(request, user)
    f["agrupar"] = _agrupar(rel, f["agrupar"], user)
    f["modo"] = _modo(rel, f["modo"])
    res = rel["gerar"](db, user, f)
    wb = Workbook()
    ws = wb.active
    ws.title = rel["titulo"][:31]
    ws["A1"] = rel["titulo"]
    ws["A1"].font = Font(bold=True, size=14, color="33247A")
    ws["A2"] = _resumo_filtros(rel, f, user)
    ws["A3"] = "Gerado em " + dt.datetime.now().strftime("%d/%m/%Y %H:%M") + " · Portal Calcário Integral"
    ws["A4"] = " · ".join(f"{k}: {v}" for k, v in res["totais"])
    for c in ("A2", "A3", "A4"):
        ws[c].font = Font(color="5B5969")
    if res.get("nota"):
        ws["A5"] = res["nota"]
        ws["A5"].font = Font(italic=True, color="8B899A")
    linha_cab = 7
    formatos = {"t": '#,##0.0 "t"', "num": "#,##0.0", "moeda": '"R$" #,##0.00', "pct": "0.0%", "int": "0", "data": "DD/MM/YYYY"}
    for i, (rotulo, _tipo) in enumerate(res["colunas"], 1):
        cel = ws.cell(row=linha_cab, column=i, value=rotulo)
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="33247A")
        cel.alignment = Alignment(vertical="center")
    for r, linha in enumerate(res["linhas"], linha_cab + 1):
        for i, ((_rotulo, tipo), valor) in enumerate(zip(res["colunas"], linha), 1):
            if isinstance(valor, dt.datetime):
                valor = valor.date() if tipo == "data" else valor
            cel = ws.cell(row=r, column=i, value=valor)
            if tipo in formatos and isinstance(valor, (int, float, dt.date)):
                cel.number_format = formatos[tipo]
    for i, (rotulo, tipo) in enumerate(res["colunas"], 1):
        largura = max([len(rotulo)] + [len(str(l[i - 1])) for l in res["linhas"][:300] if l[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, largura + 2))
    ws.freeze_panes = ws.cell(row=linha_cab + 1, column=1)
    if res["linhas"]:
        ws.auto_filter.ref = f"A{linha_cab}:{get_column_letter(len(res['colunas']))}{linha_cab + len(res['linhas'])}"
    ver = res.get("ver")
    if ver:
        # A lista que estava aberta na tela vai junto, numa aba propria
        ws2 = wb.create_sheet("Clientes")
        ws2["A1"] = ver["titulo"]
        ws2["A1"].font = Font(bold=True, size=13, color="33247A")
        ws2["A2"] = _resumo_filtros(rel, f, user)
        ws2["A2"].font = Font(color="5B5969")
        for i, (rotulo, _tipo) in enumerate(ver["colunas"], 1):
            cel = ws2.cell(row=4, column=i, value=rotulo)
            cel.font = Font(bold=True, color="FFFFFF")
            cel.fill = PatternFill("solid", fgColor="33247A")
        for r, linha in enumerate(ver["linhas"], 5):
            for i, ((_rotulo, tipo), valor) in enumerate(zip(ver["colunas"], linha), 1):
                if isinstance(valor, dt.datetime):
                    valor = valor.date()
                cel = ws2.cell(row=r, column=i, value=valor)
                if tipo == "data" and isinstance(valor, dt.date):
                    cel.number_format = "DD/MM/YYYY"
        for i, (rotulo, _tipo) in enumerate(ver["colunas"], 1):
            largura = max([len(rotulo)] + [len(str(l[i - 1])) for l in ver["linhas"][:300] if l[i - 1] is not None])
            ws2.column_dimensions[get_column_letter(i)].width = min(60, max(10, largura + 2))
        ws2.freeze_panes = "A5"
    saida = io.BytesIO()
    wb.save(saida)
    saida.seek(0)
    nome = f"relatorio_{chave}_{dt.date.today().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(saida, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})
