"""Portaria (Rafael, 2026-10-05: escolheu "consultar se pode carregar"): uma tela so de consulta. Busca o cliente ou o
pedido e responde em verde "Pode carregar", vermelho "Nao carregar" ou ambar "Conferir", com o motivo.

Regra:
- Pedido gerado no portal (PedidoCRM): segue o pagamento que o portal acompanha. A vista so carrega com o pagamento
  conferido pelo financeiro. A prazo: carrega direto; no boleto, se a 1a parcela venceu e nao foi paga, nao carrega
  (o portal nao acompanha boleto -> "conferir"); sobre rodas, cada carga e paga antes da proxima -> "conferir".
  Plano safra: carrega. Pedido encerrado ou cancelado: nao carrega.
- Pedido que so existe no NetSuite (planilha de Expedicao): o portal nao sabe do pagamento; segue a aprovacao do
  NetSuite ("Aprovacao ... pendente" = nao carrega) e a situacao da Logistica (encerrado = nao carrega). Prazo de
  retirada vencido -> "conferir com a Logistica"."""
import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import expedicao, inicio
from .auth import require_role
from .database import get_db
from .models import (SITUACOES_ENCERRAM, SITUACOES_LOGISTICA, STATUS_PEDIDO_ABERTO, ClienteCRM, Pedido, PedidoCRM,
                     User, codigo_pedido)

router = APIRouter()
SIM, NAO, CONFERIR = "sim", "nao", "conferir"


def _t(v):
    return f"{v or 0:,.0f} t".replace(",", ".")


def veredito_portal(p):
    """-> (situacao, titulo, motivo)."""
    if p.status != STATUS_PEDIDO_ABERTO:
        return NAO, "Não carregar", f"Pedido {'cancelado' if p.status == 'cancelado' else 'encerrado'} no portal."
    if p.pagamento == "A vista":
        sit = p.situacao_pagamento()
        if sit == "liberado":
            return SIM, "Pode carregar", f"Pagamento à vista confirmado em {(p.pagamento_liberado_em - dt.timedelta(hours=3)).strftime('%d/%m às %H:%M')}."
        return NAO, "Não carregar", {
            "sem_comprovante": "À vista sem pagamento: o vendedor ainda não mandou o comprovante.",
            "em_conferencia": "À vista: o comprovante está esperando o financeiro conferir.",
            "recusado": "À vista: o financeiro recusou o comprovante.",
            "diferenca": "À vista: falta pagar a diferença do pedido.",
        }.get(sit, "À vista sem pagamento confirmado.")
    if p.pagamento == "A prazo":
        if p.forma_prazo == "boleto":
            return CONFERIR, "Pode carregar · conferir boleto", "A prazo no boleto: se a 1ª parcela venceu e não foi paga, não carregar (o portal não acompanha boleto)."
        if p.forma_prazo == "sobre_rodas":
            return CONFERIR, "Conferir a carga anterior", "Sobre rodas: cada carga é paga antes do próximo caminhão."
        return SIM, "Pode carregar", f"A prazo: paga a cada {p.prazo_periodo_dias or 7} dias."
    if p.pagamento == "Plano safra":
        quando = f" Paga em {p.vencimento_pagamento.strftime('%d/%m/%Y')}." if p.vencimento_pagamento else ""
        return SIM, "Pode carregar", "Plano safra." + quando
    return CONFERIR, "Conferir", "Forma de pagamento não informada no pedido."


def veredito_planilha(p):
    if "aprova" in (p.status or "").lower():
        return NAO, "Não carregar", f"Pedido ainda não aprovado no NetSuite ({p.status})."
    if p.situacao_logistica in SITUACOES_ENCERRAM:
        return NAO, "Não carregar", f"Encerrado pela Logística ({SITUACOES_LOGISTICA.get(p.situacao_logistica, p.situacao_logistica)})."
    if (p.saldo or 0) <= 0:
        return NAO, "Não carregar", "Sem saldo a retirar."
    if p.vencido():
        return CONFERIR, "Conferir com a Logística", f"Prazo de retirada venceu em {p.data_limite_retirada.strftime('%d/%m/%Y')}."
    return SIM, "Pode carregar", f"Pedido aprovado no NetSuite ({p.status or 'sem status'})."


def _cartao_portal(p):
    sit, titulo, motivo = veredito_portal(p)
    return {"sit": sit, "titulo": titulo, "motivo": motivo, "codigo": p.codigo, "cliente": p.cliente.fazenda,
            "cidade": " · ".join(x for x in (p.cliente.cidade, p.cliente.uf) if x), "produto": p.produto,
            "saldo": _t(p.volume_total_a_entregar()), "pagamento": p.pagamento, "origem": "Pedido do portal",
            "prazo": p.data_limite_retirada}


def _cartao_planilha(p):
    sit, titulo, motivo = veredito_planilha(p)
    return {"sit": sit, "titulo": titulo, "motivo": motivo, "codigo": p.numero_pedido, "cliente": p.cliente,
            "cidade": " · ".join(x for x in (p.cidade, p.uf) if x), "produto": p.produto, "saldo": _t(p.saldo),
            "pagamento": None, "origem": "Pedido do NetSuite", "prazo": p.data_limite_retirada}


def buscar(db, q):
    """Pedidos em aberto (portal e NetSuite) pelo codigo, numero ou nome do cliente. -> lista de cartoes."""
    termo = (q or "").strip()
    if len(termo) < 2:
        return []
    like = f"%{termo.lower()}%"
    cartoes, vistos_ns = [], set()
    numero = termo.upper().replace("PV-", "").replace("PV", "").strip()
    q_portal = db.query(PedidoCRM).join(ClienteCRM, PedidoCRM.cliente_id == ClienteCRM.id).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO)
    filtros = [func.lower(ClienteCRM.fazenda).like(like), func.lower(func.coalesce(PedidoCRM.pedido_netsuite, "")).like(like)]
    if numero.isdigit():
        filtros.append(PedidoCRM.numero == int(numero))
    for p in q_portal.filter(or_(*filtros)).order_by(PedidoCRM.criado_em.desc()).limit(15):
        cartoes.append(_cartao_portal(p))
        if p.pedido_netsuite:
            vistos_ns.add(p.pedido_netsuite)
    for p in (expedicao.query_em_aberto(db).filter(or_(func.lower(Pedido.cliente).like(like), func.lower(Pedido.numero_pedido).like(like)))
              .order_by(Pedido.data_pedido.desc()).limit(15)):
        if p.numero_pedido not in vistos_ns:
            cartoes.append(_cartao_planilha(p))
    ordem = {NAO: 0, CONFERIR: 1, SIM: 2}
    return sorted(cartoes, key=lambda c: ordem[c["sit"]])


@router.get("/portaria", response_class=HTMLResponse)
def portaria(request: Request, q: str = "", user: User = Depends(require_role("admin", "portaria")), db: Session = Depends(get_db)):
    from .crm_routes import _templates
    liberados = [_cartao_portal(i["pedido"]) for i in inicio.liberados_para_carregar(db)]
    aguardando = [_cartao_portal(p) for p in db.query(PedidoCRM).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO,
                                                                         PedidoCRM.pagamento == "A vista")
                  .order_by(PedidoCRM.criado_em.desc()) if p.aguardando_pagamento()]
    titulo, data = inicio.saudacao(user)
    return _templates(request).TemplateResponse(request, "portaria.html", {
        "user": user, "q": q, "resultados": buscar(db, q) if q else None, "liberados": liberados, "aguardando": aguardando,
        "saudacao": titulo, "data_extenso": data, "migalhas": [("Portaria", None)],
    })
