"""Paginas iniciais no mesmo padrao (Rafael, 2026-10-04: "estabeleca essa logica de pagina para todas as paginas
iniciais do vendedor, logistica e financeiro"): tarefas no topo como prioridade (unico bloco colorido, cartoes com o
verbo da acao e o que ela trava, em ordem de urgencia), numeros neutros embaixo e uma lista curta pra comecar, numa
tela sem rolagem. Macros em templates/_inicio.html; estilos .ai-* no style.css.

Pagamento a vista e PRIORIDADE MAXIMA ("para os caminhoes carregarem dependem da liberacao, a equipe nao pode perder
tempo"). E uma corrente: o vendedor manda o comprovante -> o financeiro (ou admin) confere -> a logistica carrega.
Cada Inicio mostra o seu elo como o cartao vermelho "Prioridade maxima"; admin e financeiro tambem veem a faixa
vermelha em todas as paginas enquanto houver comprovante esperando (base.html, menu.menu_lateral)."""
import datetime as dt
from collections import Counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import config, expedicao, presenca
from .auth import require_role
from .database import get_db
from .models import (COMPROVANTE_CONFIRMADO, COMPROVANTE_RECUSADO, STATUS_PEDIDO_ABERTO, ImportacaoPlanilha, Pedido,
                     PedidoComprovante, PedidoCRM, RecebimentoPedido, User)

router = APIRouter()
DIAS_SEMANA = ("Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")
LIBERADO_RECENTE_DIAS = 3  # pagamento confirmado ha ate N dias e ainda sem carregar: tarefa nº 1 da Logistica


def saudacao(user):
    agora = dt.datetime.now()
    texto = "Bom dia" if agora.hour < 12 else ("Boa tarde" if agora.hour < 18 else "Boa noite")
    nome = (user.nome_completo or "").split(" ")[0]
    hoje = agora.date()
    return (f"{texto}, {nome}" if nome else texto), f"{DIAS_SEMANA[hoje.weekday()]}, {hoje.day} de {MESES[hoje.month - 1]}"


def ha(quando):
    return presenca._ha(dt.datetime.utcnow() - quando) if quando else None


def reais(v):
    from .comissao import reais as _r
    return _r(v or 0)


def reais_compacto(v):
    v = v or 0
    if v >= 1_000_000:
        return "R$ " + f"{v / 1_000_000:.1f}".replace(".", ",") + " mi"
    if v >= 10_000:
        return f"R$ {v / 1_000:.0f} mil"
    return reais(v)


def _link_pedido(p):
    return f"/crm/cliente/{p.cliente_id}?aba=pedidos#pedido-{p.id}"


# ---------------------------------------------------------------- pagamento a vista (prioridade maxima)

def pagamentos_em_conferencia(db):
    """Comprovantes de pedido a vista esperando o financeiro, do que espera ha mais tempo."""
    from .financeiro_routes import _pedidos_em_conferencia
    itens = []
    for p in _pedidos_em_conferencia(db):
        desde = min(c.criado_em for c in p.comprovantes_pendentes())
        itens.append({"pedido": p, "desde": desde, "valor": p.a_receber() or p.valor_total()})
    return itens


def resumo_urgente(db):
    """Faixa vermelha do base.html (admin e financeiro, em todas as paginas)."""
    itens = pagamentos_em_conferencia(db)
    if not itens:
        return None
    return {"n": len(itens), "ha": ha(itens[0]["desde"]), "link": f"/financeiro/pagamentos#pagamento-{itens[0]['pedido'].id}"}


def cartao_conferir(itens):
    """Cartao "Prioridade maxima" do admin e do financeiro."""
    if not itens:
        return None
    p, n = itens[0]["pedido"], len(itens)
    detalhe = f"{p.codigo} · {p.cliente.fazenda} · {reais(itens[0]['valor'])} · esperando {ha(itens[0]['desde'])}"
    if n > 1:
        detalhe += f" · e mais {n - 1}"
    return {"acao": "Conferir pagamento à vista" if n == 1 else f"Conferir {n} pagamentos à vista",
            "detalhe": detalhe, "efeito": "o caminhão só carrega depois da conferência", "botao": "Conferir agora",
            "link": f"/financeiro/pagamentos#pagamento-{p.id}",
            "texto": "Comprovante de pedido à vista esperando conferência: o carregamento fica travado até alguém conferir."}


def pedidos_sem_pagamento_do_vendedor(db, user):
    """Elo do vendedor: pedido a vista sem comprovante, com comprovante recusado ou com diferenca a pagar
    (recusado primeiro, depois o mais antigo)."""
    from .models import ClienteCRM
    q = (db.query(PedidoCRM).join(ClienteCRM, PedidoCRM.cliente_id == ClienteCRM.id)
           .filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO, PedidoCRM.pagamento == "A vista",
                   PedidoCRM.pagamento_liberado_em.is_(None)))
    if user.role == "vendedor":
        q = q.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    ordem = {"recusado": 0, "diferenca": 1, "sem_comprovante": 2}
    itens = [p for p in q.all() if p.situacao_pagamento() in ordem]
    itens.sort(key=lambda p: (ordem[p.situacao_pagamento()], p.criado_em))
    return itens


def cartao_enviar_comprovante(pedidos):
    if not pedidos:
        return None
    p, n = pedidos[0], len(pedidos)
    situacao = p.situacao_pagamento()
    acao = {"recusado": "Comprovante recusado: enviar outro", "diferenca": "Enviar comprovante da diferença"}.get(
        situacao, "Enviar comprovante de pagamento")
    if n > 1:
        acao = f"Enviar comprovante de {n} pedidos à vista"
    detalhe = f"{p.codigo} · {p.cliente.fazenda} · {reais(p.a_receber() or p.valor_total())}"
    if situacao == "recusado" and p.comprovantes and p.comprovantes[-1].motivo_recusa:
        detalhe += f" · recusado: {p.comprovantes[-1].motivo_recusa}"
    if n > 1:
        detalhe += f" · e mais {n - 1}"
    return {"acao": acao, "detalhe": detalhe, "efeito": "o caminhão só carrega com o pagamento confirmado",
            "botao": "Enviar agora", "link": _link_pedido(p),
            "texto": "Pedido à vista: o carregamento só é liberado depois que o financeiro confere o comprovante."}


def liberados_para_carregar(db):
    """Elo da Logistica: a vista com pagamento confirmado ha pouco e ainda sem carregamento registrado."""
    limite = dt.datetime.utcnow() - dt.timedelta(days=LIBERADO_RECENTE_DIAS)
    pedidos = (db.query(PedidoCRM).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO, PedidoCRM.pagamento == "A vista",
                                          PedidoCRM.pagamento_liberado_em >= limite)
                 .order_by(PedidoCRM.pagamento_liberado_em).all())
    planilha = {p.numero_pedido: p for p in db.query(Pedido).filter(
        Pedido.numero_pedido.in_([p.pedido_netsuite for p in pedidos if p.pedido_netsuite] or [""]))}
    itens = []
    for p in pedidos:
        ns = planilha.get(p.pedido_netsuite) if p.pedido_netsuite else None
        if ns is not None and ns.ultima_retirada and ns.ultima_retirada >= p.pagamento_liberado_em.date():
            continue  # ja carregou depois da liberacao
        itens.append({"pedido": p, "ns": ns,
                      "link": f"/logistica?abrir={ns.id}#row-{ns.id}" if ns is not None else _link_pedido(p)})
    return itens


def cartao_carregar(itens):
    if not itens:
        return None
    p, n = itens[0]["pedido"], len(itens)
    detalhe = (f"{p.codigo} · {p.cliente.fazenda} · {p.volume_total_a_entregar():,.0f} t".replace(",", ".")
               + f" · confirmado {ha(p.pagamento_liberado_em)}")
    if n > 1:
        detalhe += f" · e mais {n - 1}"
    return {"acao": "Pagamento confirmado: carregar" if n == 1 else f"{n} pedidos com pagamento confirmado: carregar",
            "detalhe": detalhe, "efeito": "o cliente já pagou e espera o caminhão", "botao": "Carregar agora",
            "link": itens[0]["link"],
            "texto": f"Pedido à vista com pagamento confirmado nos últimos {LIBERADO_RECENTE_DIAS} dias e sem carregamento registrado."}


# ---------------------------------------------------------------- Inicio do Financeiro

@router.get("/financeiro/inicio", response_class=HTMLResponse)
def financeiro_inicio(request: Request, user: User = Depends(require_role("admin", "financeiro")), db: Session = Depends(get_db)):
    from .crm_routes import _templates
    from .financeiro_routes import _a_receber
    hoje = dt.date.today()
    agora = dt.datetime.utcnow()
    conferir = pagamentos_em_conferencia(db)
    a_receber = _a_receber(db)
    vencidos = [p for p in a_receber if p.vencimento_pagamento and p.vencimento_pagamento <= hoje]
    tarefas = []
    if vencidos:
        tarefas.append({"nivel": "trava", "acao": "Conferir planos safra vencidos", "acao_um": "Conferir plano safra vencido",
                        "botao": "Conferir agora",
                        "n": len(vencidos), "efeito": "Venceu: o cliente pagou?", "link": "/financeiro/recebimentos",
                        "texto": "Plano safra com data de pagamento vencida e sem recebimento registrado."})
    if a_receber:
        tarefas.append({"nivel": "atencao", "acao": "Registrar recebimentos", "acao_um": "Registrar recebimento",
                        "botao": "Registrar agora",
                        "n": len(a_receber), "efeito": "Libera a comissão do vendedor", "link": "/financeiro/recebimentos",
                        "texto": "Pedidos com valor a receber: registre cada pagamento que entrar (carga a carga, a prazo, plano safra)."})
    # Numeros: conferencia (tempo que o caminhao espera) e recebimentos
    conferidos = (db.query(PedidoComprovante).filter(PedidoComprovante.conferido_em >= agora - dt.timedelta(days=30))
                    .all())
    tempos = [(c.conferido_em - c.criado_em).total_seconds() / 3600 for c in conferidos
              if c.status == COMPROVANTE_CONFIRMADO and c.criado_em]
    inicio_mes = hoje.replace(day=1)
    recebidos_mes = db.query(RecebimentoPedido).filter(RecebimentoPedido.data >= inicio_mes).all()
    movimentos = ([{"quando": c.conferido_em, "o_que": "Pagamento à vista confirmado" if c.status == COMPROVANTE_CONFIRMADO
                    else "Comprovante recusado", "pedido": c.pedido, "valor": c.pedido.valor_total(), "quem": c.conferido_por,
                    "recusado": c.status == COMPROVANTE_RECUSADO}
                   for c in db.query(PedidoComprovante).filter(PedidoComprovante.conferido_em.isnot(None))
                   .order_by(PedidoComprovante.conferido_em.desc()).limit(6)]
                  + [{"quando": r.registrado_em, "o_que": "Recebimento registrado" if r.origem != "ajuste" else "Ajuste: pedido antigo",
                      "pedido": r.pedido, "valor": r.valor, "quem": r.registrado_por, "recusado": False}
                     for r in db.query(RecebimentoPedido).order_by(RecebimentoPedido.registrado_em.desc()).limit(6)])
    movimentos.sort(key=lambda m: m["quando"], reverse=True)
    titulo, data = saudacao(user)
    return _templates(request).TemplateResponse(request, "financeiro_inicio.html", {
        "user": user, "saudacao": titulo, "data_extenso": data, "urgente": cartao_conferir(conferir), "tarefas": tarefas,
        "n_conferir": len(conferir),
        "conferidos_hoje": sum(1 for c in conferidos if (c.conferido_em - dt.timedelta(hours=3)).date() == hoje),
        "tempo_medio": (sum(tempos) / len(tempos)) if tempos else None,
        "recusados_30": sum(1 for c in conferidos if c.status == COMPROVANTE_RECUSADO),
        "recebido_mes": sum(r.valor for r in recebidos_mes),
        "comissao_mes": sum(r.valor * r.comissao_pct / 100 for r in recebidos_mes if r.comissao_pct is not None),
        "total_a_receber": sum(p.a_receber() for p in a_receber), "n_a_receber": len(a_receber),
        "movimentos": movimentos[:5], "ha": ha, "reais": reais, "reais_compacto": reais_compacto,
        "migalhas": [("Início", None)],
    })


# ---------------------------------------------------------------- Inicio da Logistica

# (varios, um, o que trava, botao)
VERBO_LOG = {"respondeu": ("Ler respostas dos vendedores", "Ler resposta do vendedor", "Vendedor respondeu", "Ler agora"),
             "vencido": ("Resolver prazos vencidos", "Resolver prazo vencido", "Prazo estourado", "Resolver agora"),
             "apertado": ("Acelerar retiradas", "Acelerar retirada", "Ritmo não fecha o prazo", "Acelerar agora"),
             "sem_resposta": ("Cobrar de novo", "Cobrar de novo", "Vendedor não respondeu", "Cobrar agora"),
             "parou": ("Cobrar retiradas paradas", "Cobrar retirada parada", "Cliente parou de retirar", "Cobrar agora"),
             "nunca": ("Cobrar a 1ª retirada", "Cobrar a 1ª retirada", "Nenhuma retirada ainda", "Cobrar agora"),
             "sem_prazo": ("Combinar data limite", "Combinar data limite", "Sem data combinada", "Definir agora")}
MAX_TAREFAS = 3  # com o botao, cabem 3 cartoes lado a lado; o resto vira "e mais" no rodape


@router.get("/logistica/inicio", response_class=HTMLResponse)
def logistica_inicio(request: Request, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    from .crm_routes import _templates
    itens = expedicao.fila(db)
    ind = expedicao.indicadores(db)
    cont = Counter(n["chave"] for _, n in itens)
    tarefas = []
    for chave, (rotulo, _cor) in expedicao.niveis().items():
        if cont[chave]:
            muitos, um, efeito, botao = VERBO_LOG[chave]
            tarefas.append({"nivel": "trava" if chave in ("vencido", "apertado") else "atencao", "acao": muitos, "acao_um": um,
                            "botao": botao,
                            "n": cont[chave], "efeito": efeito, "link": f"/logistica/fila#nivel-{chave}",
                            "texto": f"{rotulo}: {cont[chave]} pedido{'s' if cont[chave] != 1 else ''} na fila."})
    mais = sum(t["n"] for t in tarefas[MAX_TAREFAS:])
    ultima = db.query(ImportacaoPlanilha).order_by(ImportacaoPlanilha.importada_em.desc()).first()
    aguardando = [p for p in db.query(PedidoCRM).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO, PedidoCRM.pagamento == "A vista")
                  if p.aguardando_pagamento()]
    titulo, data = saudacao(user)
    return _templates(request).TemplateResponse(request, "logistica_inicio.html", {
        "user": user, "saudacao": titulo, "data_extenso": data, "urgente": cartao_carregar(liberados_para_carregar(db)),
        "tarefas": tarefas[:MAX_TAREFAS], "mais_na_fila": mais, "ind": ind, "n_aguardando": len(aguardando),
        "primeiros": itens[:4], "ultima_planilha": ultima, "vol": expedicao.volumes(db), "ha": ha, "dias_sem_acao": config.valor("log_dias_sem_acao"),
        "migalhas": [("Início", None)],
    })
