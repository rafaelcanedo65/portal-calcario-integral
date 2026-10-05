"""Regras da Logistica num lugar so (Rafael, 2026-10-03): o que esta em
aberto, o que vai pra fila da Logistica e como a equipe anota no pedido.
Usado pela pagina da Logistica, pela fila, pelo menu (contador) e pelos
relatorios -- mesma conta em todo lugar."""
import datetime as dt

from sqlalchemy import func, or_

from . import config
from .models import (ABAS_EM_ABERTO, CAMPOS_ANOTACAO, SITUACOES_ENCERRAM, SITUACOES_LOGISTICA, AvisoCRM, MensagemPedido,
                     Pedido, PedidoAnotacao, PedidoCRM)


def query_em_aberto(db, encerrados=False):
    """Pedidos com saldo nas abas que a expedicao trabalha (ou de antes da
    planilha). encerrados=True: os que a equipe encerrou (Finalizado,
    Desistencia, Cliente com credito) mas o NetSuite ainda tem saldo."""
    q = db.query(Pedido).filter(Pedido.saldo > 0, or_(Pedido.aba_planilha.in_(ABAS_EM_ABERTO), Pedido.aba_planilha.is_(None)))
    encerra = Pedido.situacao_logistica.in_(SITUACOES_ENCERRAM)
    return q.filter(encerra) if encerrados else q.filter(or_(Pedido.situacao_logistica.is_(None), ~encerra))


# ------------------------------------------------------------ prioridade da fila
# Rafael, 2026-10-03: "a Logistica precisa fazer o carregamento acontecer sem
# apertar e sem esquecer nenhum pedido". Cada pedido em aberto cai no PRIMEIRO
# nivel que se aplicar (ordem = urgencia); fora de todos, esta em dia.
# Os numeros (15 dias sem puxar, 250 t/dia, 7 dias, 3 dias de cobranca) e o
# liga/desliga de cada nivel vem de config.py -- o admin muda na pagina Regras.

# Cor de cada nivel e a ordem/nomes PADRAO (o admin muda ordem e nomes na pagina
# Regras: use niveis() pra ordem e nomes de agora)
NIVEIS_FILA = {
    "respondeu": ("Vendedor respondeu", "azul"),
    "vencido": ("Prazo vencido", "vermelho"),
    "apertado": ("Retirada apertada", "laranja"),
    "sem_resposta": ("Cobrança sem resposta", "amarelo"),
    "parou": ("Parou de puxar", "amarelo"),
    "nunca": ("Nunca puxou", "amarelo"),
    "sem_prazo": ("Sem data limite", "azul"),
}


def _t(v):
    return f"{v:,.1f}".replace(",", "§").replace(".", ",").replace("§", ".")


def contexto_fila(db, pedidos):
    """O que a regra precisa e nao esta no pedido: ultima mensagem da conversa,
    quando o vendedor foi cobrado e o pedido do portal ligado (pagamento)."""
    ids = [p.id for p in pedidos]
    numeros = [p.numero_pedido for p in pedidos]
    ultima_msg, cobrado, portal = {}, {}, {}
    if ids:
        for m in db.query(MensagemPedido).filter(MensagemPedido.pedido_id.in_(ids)).order_by(MensagemPedido.criado_em):
            ultima_msg[m.pedido_id] = m
        rotulo = SITUACOES_LOGISTICA["cobrar_vendedor"]
        for a in (db.query(PedidoAnotacao).filter(PedidoAnotacao.pedido_id.in_(ids), PedidoAnotacao.campo == "situacao")
                  .order_by(PedidoAnotacao.quando)):
            if a.depois == rotulo or a.registro in (rotulo, "Cobrou o vendedor de novo"):
                cobrado[a.pedido_id] = a.quando
        for pc in db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite.in_(numeros)):
            portal[pc.pedido_netsuite] = pc
    return {"msg": ultima_msg, "cobrado": cobrado, "portal": portal}


def niveis():
    """Niveis na ordem e com os nomes que o admin definiu na pagina Regras:
    {chave: (rotulo, cor)}. NIVEIS_FILA guarda so as cores (e a ordem padrao)."""
    return {k: (config.valor(f"nome_log_{k}"), NIVEIS_FILA[k][1]) for k in config.valor("ordem_log")}


_ESPERANDO = object()  # cobrou o vendedor ha pouco: fica fora da fila ate o prazo de resposta


def nivel_fila(pedido, ctx, hoje=None):
    """-> {chave, rotulo, cor, motivo, ordem} ou None (em dia, ou esperando o
    vendedor responder). Usado pela fila E pelo proximo passo da ficha.

    Cada nivel e uma conferencia separada; o pedido fica no PRIMEIRO que se
    aplicar, na ordem que o admin arrastou na pagina Regras (config
    "ordem_log"). A espera pela resposta do vendedor vale na posicao de
    "Cobranca sem resposta": antes dela, so os niveis que estao acima."""
    hoje = hoje or dt.date.today()
    saldo = pedido.saldo or 0
    ordem = config.valor("ordem_log")
    posicao = {k: i for i, k in enumerate(ordem)}

    def nivel(chave, motivo, desempate):
        return {"chave": chave, "rotulo": config.valor(f"nome_log_{chave}"), "cor": NIVEIS_FILA[chave][1], "motivo": motivo,
                "ordem": (posicao[chave], desempate)}

    pcrm = ctx["portal"].get(pedido.numero_pedido)
    aguardando_pagamento = pcrm is not None and pcrm.aguardando_pagamento()

    def respondeu():
        msg = ctx["msg"].get(pedido.id)
        if msg and msg.autor_role == "vendedor" and (not msg.lida or pedido.situacao_logistica == "cobrar_vendedor"):
            return nivel("respondeu", f"{msg.autor_nome}: \u201c{msg.texto[:90]}\u201d", msg.criado_em.timestamp())

    def vencido():
        if pedido.vencido():
            dias = -pedido.dias_restantes()
            return nivel("vencido", f"Venceu há {dias} dia{'s' if dias != 1 else ''} ({pedido.data_limite_retirada.strftime('%d/%m')}); "
                                    f"faltam {_t(saldo)} t", -dias)

    def apertado():
        ton_dia = pedido.ton_dia_necessario()
        dias = pedido.dias_restantes()
        if (config.ligada("liga_log_apertada") and ton_dia
                and (ton_dia > config.valor("retirada_apertada_t_dia")
                     or (dias is not None and dias <= config.valor("log_dias_prazo_apertado")))):
            return nivel("apertado", f"Precisa de {_t(ton_dia)} t por dia até {pedido.data_limite_retirada.strftime('%d/%m')} "
                                     f"({dias} dia{'s' if dias != 1 else ''})", -ton_dia)

    def sem_resposta():
        if pedido.situacao_logistica != "cobrar_vendedor":
            return None
        quando = ctx["cobrado"].get(pedido.id)
        dias_cobr = (hoje - quando.date()).days if quando else None
        if dias_cobr is not None and dias_cobr < config.valor("log_dias_cobranca"):
            return _ESPERANDO  # volta sozinho se ele nao responder
        if config.ligada("liga_log_cobranca"):
            return nivel("sem_resposta", (f"{pedido.vendedor or 'O vendedor'} foi cobrado há {dias_cobr} dias e não respondeu"
                                          if dias_cobr is not None else f"{pedido.vendedor or 'O vendedor'} foi cobrado e não respondeu"),
                         -(dias_cobr or 999))

    def parou():
        # A vista esperando pagamento nao pode carregar: nao e "parado"
        if aguardando_pagamento or not config.ligada("liga_log_parou") or not pedido.ultima_retirada:
            return None
        parado = (hoje - pedido.ultima_retirada).days
        if parado >= config.valor("log_dias_parou"):
            return nivel("parou", f"Sem retirar há {parado} dias (última em {pedido.ultima_retirada.strftime('%d/%m')}); "
                                  f"faltam {_t(saldo)} t", -parado)

    def nunca():
        if (aguardando_pagamento or not config.ligada("liga_log_nunca") or pedido.ultima_retirada
                or (pedido.faturado or 0) or not pedido.data_pedido):
            return None
        idade = (hoje - pedido.data_pedido).days
        if idade >= config.valor("log_dias_nunca"):
            return nivel("nunca", f"Pedido de {idade} dias sem nenhuma retirada; {_t(saldo)} t", -idade)

    def sem_prazo():
        if not pedido.data_limite_retirada:
            return nivel("sem_prazo", f"{_t(saldo)} t sem data limite combinada", -saldo)

    conferencias = {"respondeu": respondeu, "vencido": vencido, "apertado": apertado, "sem_resposta": sem_resposta,
                    "parou": parou, "nunca": nunca, "sem_prazo": sem_prazo}
    for chave in ordem:
        achado = conferencias[chave]()
        if achado is _ESPERANDO:
            return None
        if achado:
            return achado
    return None


def fila(db):
    """[(pedido, nivel)] na ordem de prioridade."""
    pedidos = query_em_aberto(db).all()
    ctx = contexto_fila(db, pedidos)
    hoje = dt.date.today()
    itens = [(p, n) for p in pedidos for n in [nivel_fila(p, ctx, hoje)] if n]
    itens.sort(key=lambda x: x[1]["ordem"])
    return itens


def volumes(db, hoje=None):
    """Vendido e expedido na safra (planilha do NetSuite: pedidos desde o inicio do ciclo) e quanto falta entregar
    (saldo dos pedidos em aberto). Rafael (2026-10-04): "Expedido: tudo que foi entregue em toneladas" e "o admin ter
    uma ideia facil de quanto falta entregar". Saldo de pedido finalizado nao entra no "falta": virou credito ou foi
    encerrado. % entregue = expedido / (expedido + falta)."""
    from .models import ciclo_rotulo, inicio_ciclo
    desde = inicio_ciclo(hoje)
    vendido, expedido, n = (db.query(func.coalesce(func.sum(Pedido.quant_total), 0), func.coalesce(func.sum(Pedido.faturado), 0),
                                     func.count(Pedido.id))
                              .filter(Pedido.data_pedido >= desde).one())
    abertos = query_em_aberto(db).all()
    falta = sum(p.saldo or 0 for p in abertos)
    total = expedido + falta
    return {"desde": desde, "safra": ciclo_rotulo(hoje), "vendido": vendido, "expedido": expedido, "pedidos": n,
            "falta": falta, "abertos": len(abertos), "pct": round(expedido / total * 100) if total else None}


def contar_fila(db):
    return len(fila(db))


def indicadores(db, itens=None):
    """Sem apertar e sem esquecer: carga por dia necessaria (pedidos com prazo),
    quantos sem prazo e quantos sem nenhuma acao no periodo (30 dias por padrao)."""
    pedidos = query_em_aberto(db).all()
    hoje = dt.date.today()
    limite = dt.datetime.combine(hoje - dt.timedelta(days=config.valor("log_dias_sem_acao")), dt.time.min)
    ids = [p.id for p in pedidos]
    com_acao = set()
    if ids:
        com_acao |= {pid for (pid,) in db.query(PedidoAnotacao.pedido_id).filter(PedidoAnotacao.pedido_id.in_(ids),
                                                                                 PedidoAnotacao.quando >= limite)}
        com_acao |= {pid for (pid,) in db.query(MensagemPedido.pedido_id).filter(MensagemPedido.pedido_id.in_(ids),
                                                                                 MensagemPedido.criado_em >= limite)}
    com_prazo = [p for p in pedidos if p.data_limite_retirada and not p.vencido()]
    sem_acao = [p for p in pedidos if p.id not in com_acao
                and not (p.ultima_retirada and (hoje - p.ultima_retirada).days < 30)]
    itens = itens if itens is not None else fila(db)
    por_nivel = {chave: 0 for chave in NIVEIS_FILA}
    for _, n in itens:
        por_nivel[n["chave"]] += 1
    return {"em_aberto": len(pedidos), "carga_dia": sum(p.ton_dia_necessario() or 0 for p in com_prazo),
            "com_prazo": len(com_prazo), "sem_prazo": sum(1 for p in pedidos if not p.data_limite_retirada),
            "sem_acao_30": len(sem_acao), "na_fila": len(itens), "por_nivel": por_nivel}


def _agora():
    # Hora de Brasilia, a mesma do historico que veio da planilha (as duas
    # origens ficam na mesma linha do tempo do pedido).
    return dt.datetime.now()


def _texto(campo, valor):
    if valor is None or valor == "":
        return None
    if campo == "situacao":
        return SITUACOES_LOGISTICA.get(valor, valor)
    if campo == "data_limite":
        return valor.strftime("%d/%m/%Y")
    return valor


def anotar(db, pedido, user, situacao=None, data_limite=None, comentario=None, so=(), mensagem=None):
    """Aplica o que mudou, registra no historico (quem/quando/antes/depois) e
    marca o pedido como anotado no portal (a planilha nao mexe mais nele).
    `so` limita os campos tocados (ex.: so a data, pela fila). `mensagem`: o
    que vai pro vendedor quando a situacao e "Cobrar retorno do vendedor"
    (sem ela, vai o comentario); com mensagem, cobra de novo mesmo se a
    situacao ja era essa. -> campos mudados."""
    novos = {"situacao": situacao or None, "data_limite": data_limite, "comentario": (comentario or "").strip() or None}
    atuais = {"situacao": pedido.situacao_logistica, "data_limite": pedido.data_limite_retirada,
              "comentario": pedido.comentario_logistica}
    mudados = []
    for campo in CAMPOS_ANOTACAO:
        if so and campo not in so:
            continue
        if novos[campo] == atuais[campo]:
            continue
        db.add(PedidoAnotacao(pedido_id=pedido.id, quando=_agora(), quem=user.nome_completo, origem="portal", campo=campo,
                              antes=_texto(campo, atuais[campo]), depois=_texto(campo, novos[campo])))
        mudados.append(campo)
    cobrar_de_novo = bool(mensagem) and novos["situacao"] == "cobrar_vendedor" and "situacao" not in mudados \
        and (not so or "situacao" in so)
    if cobrar_de_novo:
        db.add(PedidoAnotacao(pedido_id=pedido.id, quando=_agora(), quem=user.nome_completo, origem="portal",
                              campo="situacao", registro="Cobrou o vendedor de novo"))
        pedido.anotado_no_portal_em = dt.datetime.utcnow()
        cobrar_vendedor(db, pedido, user, mensagem)
        return ["cobranca"]
    if not mudados:
        return mudados
    if "situacao" in mudados:
        pedido.situacao_logistica = novos["situacao"]
    if "comentario" in mudados:
        pedido.comentario_logistica = novos["comentario"]
    if "data_limite" in mudados:
        pedido.data_limite_retirada = novos["data_limite"]
        # O pedido do portal ligado a este passa a ter a mesma data
        for pcrm in db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite == pedido.numero_pedido):
            pcrm.data_limite_retirada = novos["data_limite"]
    pedido.anotado_no_portal_em = dt.datetime.utcnow()
    if "situacao" in mudados and novos["situacao"] == "cobrar_vendedor":
        cobrar_vendedor(db, pedido, user, mensagem or novos["comentario"])
    return mudados


# ---------------------------------------------------------------- ficha do pedido
# Mesmo conceito da ficha do cliente no CRM (Rafael, 2026-10-03): onde o pedido
# esta (etapas), o que precisa ser feito agora (proximo passo) e o botao certo.

def pedido_do_portal(db, pedido):
    """O pedido gerado no portal (PV) ligado a este pedido do NetSuite, se houver."""
    return db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite == pedido.numero_pedido).first()


def _pct_carregado(pedido):
    if not pedido.quant_total:
        return None
    return max(0, min(100, round((pedido.faturado or 0) / pedido.quant_total * 100)))


def etapas_pedido(pedido, pcrm=None):
    """Emitido -> [Pagamento, so quando se sabe] -> Prazo combinado -> Carregando
    X% -> Finalizado. Estado: feito | atual | futuro. Pagamento so aparece em
    pedido a vista do portal (no NetSuite puro a planilha nao diz se foi pago)."""
    finalizado = pedido.aba_planilha == "Finalizados" or (pedido.saldo or 0) <= 0
    encerrado = pedido.situacao_logistica in SITUACOES_ENCERRAM

    def etapa(chave, estado, rotulos, quando=None, pct=None):
        # O nome diz o estado (Rafael, 2026-10-03): "Prazo combinado" em destaque
        # parecia ja combinado -- na etapa atual o nome e o que falta fazer.
        # pct so no carregamento (a bolinha com a porcentagem); nas outras, None.
        return {"chave": chave, "estado": estado, "rotulo": rotulos.get(estado, rotulos["futuro"]), "quando": quando,
                "pct": pct}

    etapas = [etapa("emitido", "feito", {"feito": "Emitido", "futuro": "Emitido"}, pedido.data_pedido)]
    pagamento_ok = True
    if pcrm is not None and pcrm.exige_comprovante():
        pagamento_ok = bool(pcrm.pagamento_liberado_em) or pcrm.status != "aberto"
        etapas.append(etapa("pagamento", "feito" if pagamento_ok else "atual",
                            {"feito": "Pagamento confirmado", "atual": "Aguardando pagamento", "futuro": "Pagamento"},
                            pcrm.pagamento_liberado_em))
    prazo_ok = bool(pedido.data_limite_retirada)
    estado_prazo = "feito" if prazo_ok or finalizado else ("atual" if pagamento_ok else "futuro")
    etapas.append(etapa("prazo", estado_prazo,
                        {"feito": "Prazo combinado" if prazo_ok else "Prazo", "atual": "Combinar prazo", "futuro": "Prazo"},
                        pedido.data_limite_retirada))
    carregando = "feito" if finalizado else ("atual" if pagamento_ok and (prazo_ok or (pedido.faturado or 0) > 0) else "futuro")
    etapas.append(etapa("carga", carregando, {"feito": "Carregado", "atual": "Carregando", "futuro": "Carregamento"},
                        pedido.ultima_retirada, None if finalizado else _pct_carregado(pedido)))
    if encerrado:
        motivo = SITUACOES_LOGISTICA[pedido.situacao_logistica]
        etapas.append(etapa("fim", "encerrado", {"encerrado": motivo, "futuro": motivo}))
    else:
        etapas.append(etapa("fim", "feito" if finalizado else "futuro", {"feito": "Finalizado", "futuro": "Finalizado"}))
    return etapas


def mensagens_do_pedido(db, pedido, limite=5):
    msgs = (db.query(MensagemPedido).filter(MensagemPedido.pedido_id == pedido.id)
            .order_by(MensagemPedido.criado_em.desc()).limit(limite).all())
    return list(reversed(msgs))


def _dias(d):
    return (dt.date.today() - d).days


def _t(v):
    return f"{v:,.1f}".replace(",", "§").replace(".", ",").replace("§", ".")


def proximo_passo(db, pedido, pcrm=None):
    """O que a Logistica precisa fazer agora com este pedido e quais botoes
    mostrar. Usa a mesma regra da fila (nivel_fila), pra fila e ficha dizerem a
    mesma coisa. -> {titulo, texto, cor, acoes}."""
    saldo = pedido.saldo or 0
    retirada = (f"última retirada em {pedido.ultima_retirada.strftime('%d/%m')}" if pedido.ultima_retirada
                else "nada retirado ainda")
    vendedor = pedido.vendedor or "o vendedor"
    if pedido.situacao_logistica in SITUACOES_ENCERRAM:
        return {"titulo": f"Encerrado pela equipe: {SITUACOES_LOGISTICA[pedido.situacao_logistica]}",
                "texto": "Fora da lista em aberto. Se o cliente voltar a retirar, reabra o pedido.",
                "cor": "cinza", "acoes": ["reabrir"]}
    if pedido.aba_planilha == "Finalizados" or saldo <= 0:
        return {"titulo": "Pedido finalizado no NetSuite", "texto": f"Carregado {_t(pedido.faturado or 0)} t de {_t(pedido.quant_total or 0)} t.",
                "cor": "verde", "acoes": []}
    ctx = contexto_fila(db, [pedido])
    n = nivel_fila(pedido, ctx)
    chave = n["chave"] if n else None
    prazo_txt = pedido.data_limite_retirada.strftime("%d/%m") if pedido.data_limite_retirada else None
    if chave == "respondeu":
        m = ctx["msg"][pedido.id]
        return {"titulo": f"{m.autor_nome} respondeu", "texto": f"\u201c{m.texto[:160]}\u201d. Veja a conversa e atualize o pedido.",
                "cor": "azul", "acoes": ["conversa", "data"]}
    if chave == "vencido":
        dias = -pedido.dias_restantes()
        return {"titulo": f"O prazo venceu há {dias} dia{'s' if dias != 1 else ''} ({prazo_txt})",
                "texto": f"Faltam {_t(saldo)} t; {retirada}. Combine uma nova data com o cliente ou encerre o pedido.",
                "cor": "vermelho", "acoes": ["data", "cobrar", "encerrar"]}
    if chave == "apertado":
        return {"titulo": f"Retirada apertada: {_t(pedido.ton_dia_necessario())} t por dia até {prazo_txt}",
                "texto": f"Faltam {_t(saldo)} t em {pedido.dias_restantes()} dias. Confirme com o cliente se ele dá conta ou combine outra data.",
                "cor": "laranja", "acoes": ["cobrar", "data"]}
    if pcrm is not None and pcrm.aguardando_pagamento() and chave in (None, "sem_prazo"):
        return {"titulo": "Aguardando o financeiro confirmar o pagamento",
                "texto": "Pedido à vista: o carregamento só é liberado depois da confirmação. Já dá para combinar a data com o cliente.",
                "cor": "amarelo", "acoes": ["data"]}
    if chave == "sem_resposta":
        return {"titulo": f"{n['motivo']}", "texto": "Cobre de novo ou fale direto com o cliente.",
                "cor": "amarelo", "acoes": ["cobrar", "conversa"]}
    if chave == "parou":
        return {"titulo": f"Parou de puxar há {(dt.date.today() - pedido.ultima_retirada).days} dias",
                "texto": f"Última retirada em {pedido.ultima_retirada.strftime('%d/%m')}; faltam {_t(saldo)} t"
                         + (f" até {prazo_txt}" if prazo_txt else "") + ". Fale com o cliente ou cobre o vendedor.",
                "cor": "amarelo", "acoes": ["cobrar", "data", "encerrar"]}
    if chave == "nunca":
        return {"titulo": f"{(dt.date.today() - pedido.data_pedido).days} dias sem nenhuma retirada",
                "texto": f"Pedido de {pedido.data_pedido.strftime('%d/%m/%Y')}; {_t(saldo)} t. Ligue para o cliente e combine a data.",
                "cor": "amarelo", "acoes": ["data", "cobrar", "encerrar"]}
    if chave == "sem_prazo":
        return {"titulo": "Combinar a data limite de retirada com o cliente",
                "texto": f"Faltam {_t(saldo)} t; pedido de {pedido.data_pedido.strftime('%d/%m/%Y') if pedido.data_pedido else 'data não informada'}; {retirada}.",
                "cor": "azul", "acoes": ["data", "cobrar"]}
    if pedido.situacao_logistica == "cobrar_vendedor":
        quando = ctx["cobrado"].get(pedido.id)
        return {"titulo": f"Aguardando o retorno de {vendedor}" + (f" desde {quando.strftime('%d/%m')}" if quando else ""),
                "texto": f"A resposta chega na conversa do pedido. Sem resposta em {config.valor("log_dias_cobranca")} dias, ele volta para a fila.",
                "cor": "amarelo", "acoes": ["conversa", "cobrar"]}
    dias = pedido.dias_restantes()
    ton_dia = pedido.ton_dia_necessario()
    return {"titulo": "Em dia", "texto": (f"Prazo {pedido.data_limite_retirada.strftime('%d/%m/%Y')} (faltam {dias} dias)"
                                          + (f", {_t(ton_dia)} t por dia" if ton_dia else "") + f"; {retirada}."),
            "cor": "verde", "acoes": []}


def cobrar_vendedor(db, pedido, user, comentario):
    """"Cobrar retorno do vendedor" avisa o vendedor sozinho: mensagem na
    conversa do pedido + aviso no sino, que leva direto pra conversa."""
    from .crm_routes import encontrar_cliente_crm  # import tardio: crm_routes carrega o app inteiro
    texto = (f"A Logística pede um retorno sobre o pedido {pedido.numero_pedido} "
             f"(saldo de {pedido.saldo:,.1f} t)".replace(",", "§").replace(".", ",").replace("§", ".")
             + (f": {comentario}" if comentario else "."))
    db.add(MensagemPedido(pedido_id=pedido.id, autor_id=user.id, autor_nome=user.nome_completo, autor_role=user.role,
                          texto=texto))
    if pedido.vendedor:
        cliente = encontrar_cliente_crm(db, pedido.cliente)
        db.add(AvisoCRM(vendedor_nome=pedido.vendedor, cliente_id=cliente.id if cliente else None,
                        tipo="cobrar_retorno_logistica", titulo=f"A Logística pede retorno: {pedido.cliente}",
                        texto=texto, autor=user.nome_completo, link=f"/pedidos/{pedido.id}/conversa#fim-conversa"))
