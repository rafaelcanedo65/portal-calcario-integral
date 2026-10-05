"""Financeiro: confere o comprovante de pagamento dos pedidos a vista e
libera (ou nao) o carregamento.

Rafael (2026-10-02): "o carregamento so e liberado quando confirmado o
pagamento" e "o financeiro ou admin precisa confirmar" -- comprovante anexado
pelo vendedor nao e dinheiro na conta (pode ser agendamento, ou falso).
"""
import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import comissao
from .auth import require_role
from .crm_routes import _avisar, _templates
from .database import get_db
from .feedback import avisar_erro, avisar_sucesso
from .models import (COMPROVANTE_CONFIRMADO, COMPROVANTE_PENDENTE, COMPROVANTE_RECUSADO, MOTIVOS_RECUSA_COMPROVANTE,
                     STATUS_PEDIDO_ABERTO, STATUS_PEDIDO_CANCELADO, ContatoCRM, PedidoComprovante, PedidoCRM,
                     RecebimentoPedido, User)

router = APIRouter()
PERFIS_FINANCEIRO = ("admin", "financeiro")


def _pedidos_em_conferencia(db):
    """Pedidos a vista em aberto com comprovante esperando conferencia, do
    que esta esperando ha mais tempo pro mais recente."""
    pedidos = (db.query(PedidoCRM).join(PedidoComprovante, PedidoComprovante.pedido_id == PedidoCRM.id)
                 .filter(PedidoComprovante.status == COMPROVANTE_PENDENTE, PedidoCRM.status == STATUS_PEDIDO_ABERTO,
                         PedidoCRM.pagamento == "A vista")
                 .distinct().all())
    return sorted(pedidos, key=lambda p: min(c.criado_em for c in p.comprovantes_pendentes()))


def contar_pagamentos_em_conferencia(db):
    return len(_pedidos_em_conferencia(db))


@router.get("/financeiro/pagamentos", response_class=HTMLResponse)
def financeiro_pagamentos(request: Request, user: User = Depends(require_role(*PERFIS_FINANCEIRO)),
                          db: Session = Depends(get_db)):
    return _pagina(request, db, user)


def _pagina(request, db, user, erro=None):
    em_conferencia = _pedidos_em_conferencia(db)
    # So pra o financeiro saber o que vem por ai: pedido a vista em aberto que
    # ainda depende do vendedor (sem comprovante, recusado, diferenca).
    esperando_vendedor = [p for p in db.query(PedidoCRM).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO,
                                                                 PedidoCRM.pagamento == "A vista")
                                                         .order_by(PedidoCRM.criado_em).all()
                          if p.situacao_pagamento() in ("sem_comprovante", "recusado", "diferenca")]
    conferidos = (db.query(PedidoComprovante)
                    .filter(PedidoComprovante.status.in_((COMPROVANTE_CONFIRMADO, COMPROVANTE_RECUSADO)))
                    .order_by(PedidoComprovante.conferido_em.desc()).limit(20).all())
    return _templates(request).TemplateResponse(request, "financeiro_pagamentos.html", {
        "user": user, "em_conferencia": em_conferencia, "esperando_vendedor": esperando_vendedor,
        "conferidos": conferidos, "motivos_recusa": MOTIVOS_RECUSA_COMPROVANTE, "erro": erro,
        "agora": dt.datetime.utcnow(),
        "migalhas": [("Pagamentos a confirmar", None)],
    }, status_code=400 if erro else 200)


def _pedido_para_conferir(db, pedido_id, valor_conferido):
    """Devolve (pedido, erro). Confere que ainda ha o que conferir e que o
    pedido nao mudou de valor enquanto o financeiro olhava o comprovante."""
    pedido = db.get(PedidoCRM, pedido_id)
    if (not pedido or pedido.status != STATUS_PEDIDO_ABERTO or not pedido.exige_comprovante()
            or not pedido.comprovantes_pendentes()):
        return None, "Esse pagamento não está mais esperando conferência (o pedido mudou ou já foi conferido)."
    try:
        valor = float(valor_conferido)
    except ValueError:
        valor = None
    if valor is None or abs(valor - pedido.valor_total()) > 0.009:
        return None, (f"O valor do pedido {pedido.codigo} mudou enquanto você conferia "
                      f"(agora R$ {pedido.valor_total():,.2f}). Confira o comprovante de novo."
                      .replace(",", "§").replace(".", ",").replace("§", "."))
    return pedido, None


@router.post("/financeiro/pagamentos/{pedido_id}/confirmar")
def financeiro_confirmar(request: Request, pedido_id: int, valor_conferido: str = Form(""),
                         user: User = Depends(require_role(*PERFIS_FINANCEIRO)), db: Session = Depends(get_db)):
    pedido, erro = _pedido_para_conferir(db, pedido_id, valor_conferido)
    if erro:
        return _pagina(request, db, user, erro)
    agora = dt.datetime.utcnow()
    for c in pedido.comprovantes_pendentes():
        c.status = COMPROVANTE_CONFIRMADO
        c.conferido_em = agora
        c.conferido_por = user.nome_completo
    pedido.pagamento_liberado_em = agora
    pedido.pagamento_liberado_por = user.nome_completo
    cliente = pedido.cliente
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pagamento", autor=user.nome_completo,
                       texto=f"Pagamento do pedido {pedido.codigo} confirmado pelo financeiro: carregamento liberado."))
    _avisar(db, cliente.vendedor_nome, cliente, "pagamento_confirmado",
            f"Pagamento confirmado: pedido {pedido.codigo} liberado",
            f"{cliente.fazenda}: o financeiro confirmou o pagamento. O carregamento está liberado.", user.nome_completo)
    # A vista pago = dinheiro recebido: libera a comissao (so a diferenca, se o
    # pedido ficou mais caro depois de um pagamento ja confirmado)
    diferenca = pedido.a_receber()
    if diferenca >= 0.01:
        comissao.registrar_recebimento(db, pedido, diferenca, dt.date.today(), user.nome_completo, origem="comprovante",
                                       observacao="comprovante do à vista confirmado")
    db.commit()
    avisar_sucesso(request, f"Pagamento do pedido {pedido.codigo} confirmado. Carregamento liberado e vendedor avisado.")
    return RedirectResponse("/financeiro/pagamentos", status_code=303)


@router.post("/financeiro/pagamentos/{pedido_id}/recusar")
def financeiro_recusar(request: Request, pedido_id: int, valor_conferido: str = Form(""), motivo: str = Form(""),
                       detalhe: str = Form(""), user: User = Depends(require_role(*PERFIS_FINANCEIRO)),
                       db: Session = Depends(get_db)):
    pedido, erro = _pedido_para_conferir(db, pedido_id, valor_conferido)
    if not erro and motivo not in MOTIVOS_RECUSA_COMPROVANTE:
        erro = "Escolha o motivo da recusa."
    elif not erro and motivo == "Outro" and not detalhe.strip():
        erro = "Escreva o motivo da recusa."
    if erro:
        return _pagina(request, db, user, erro)
    texto_motivo = detalhe.strip() if motivo == "Outro" else (motivo + (f" ({detalhe.strip()})" if detalhe.strip() else ""))
    agora = dt.datetime.utcnow()
    for c in pedido.comprovantes_pendentes():
        c.status = COMPROVANTE_RECUSADO
        c.conferido_em = agora
        c.conferido_por = user.nome_completo
        c.motivo_recusa = texto_motivo
    cliente = pedido.cliente
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pagamento", autor=user.nome_completo,
                       texto=f"Comprovante do pedido {pedido.codigo} recusado pelo financeiro: {texto_motivo}."))
    _avisar(db, cliente.vendedor_nome, cliente, "comprovante_recusado",
            f"Comprovante recusado: pedido {pedido.codigo}",
            f"{cliente.fazenda}: {texto_motivo}. Envie outro comprovante para liberar o carregamento.", user.nome_completo)
    db.commit()
    avisar_sucesso(request, f"Comprovante do pedido {pedido.codigo} recusado. O vendedor foi avisado.")
    return RedirectResponse("/financeiro/pagamentos", status_code=303)


# ---------------- Recebimentos (Rafael, 2026-10-04): a comissao do vendedor so e
# liberada sobre o que a empresa recebeu. A vista entra sozinho na confirmacao do
# comprovante; carga a carga, a prazo e plano safra, o financeiro registra aqui.

def _a_receber(db):
    """Pedidos com valor a receber, menos os a vista em aberto (esses seguem o
    caminho do comprovante, em Pagamentos a confirmar)."""
    pedidos = db.query(PedidoCRM).filter(PedidoCRM.status != STATUS_PEDIDO_CANCELADO).order_by(PedidoCRM.criado_em).all()
    return [p for p in pedidos if p.a_receber() >= 0.01 and not (p.pagamento == "A vista" and p.status == STATUS_PEDIDO_ABERTO)]


def _forma(p):
    if p.pagamento == "A prazo":
        return {"boleto": f"A prazo · boleto {p.prazo_parcelas or '30'} dias", "sobre_rodas": "Carga a carga (sobre rodas)",
                "periodo": f"A prazo · paga a cada {p.prazo_periodo_dias or 7} dias"}.get(p.forma_prazo, "A prazo")
    if p.pagamento == "Plano safra":
        return "Plano safra" + (f" · paga em {p.vencimento_pagamento.strftime('%d/%m/%Y')}" if p.vencimento_pagamento else "")
    return "À vista"


@router.get("/financeiro/recebimentos", response_class=HTMLResponse)
def financeiro_recebimentos(request: Request, user: User = Depends(require_role(*PERFIS_FINANCEIRO)),
                            db: Session = Depends(get_db)):
    ultimos = db.query(RecebimentoPedido).order_by(RecebimentoPedido.registrado_em.desc()).limit(30).all()
    pedidos = _a_receber(db)
    grupos = [
        ("A prazo, carga a carga e plano safra", None, [(p, _forma(p)) for p in pedidos if p.pagamento != "A vista"]),
        ("À vista sem pagamento confirmado no portal",
         "Pedidos de antes da conferência do financeiro no portal. Registre o pagamento para liberar a comissão do vendedor.",
         [(p, _forma(p)) for p in pedidos if p.pagamento == "A vista"]),
    ]
    return _templates(request).TemplateResponse(request, "financeiro_recebimentos.html", {
        "user": user, "grupos": [g for g in grupos if g[2]], "n_a_receber": len(pedidos), "ultimos": ultimos,
        "nome_produto": comissao.PRODUTO_NOME,
        "hoje": dt.date.today(), "reais": comissao.reais,
        "migalhas": [("Recebimentos", None)],
    })


@router.post("/financeiro/recebimentos/{pedido_id}")
def financeiro_registrar_recebimento(request: Request, pedido_id: int, valor: str = Form(""), data: str = Form(""),
                                     observacao: str = Form(""), user: User = Depends(require_role(*PERFIS_FINANCEIRO)),
                                     db: Session = Depends(get_db)):
    pedido = db.get(PedidoCRM, pedido_id)
    voltar = f"/financeiro/recebimentos#rec-{pedido_id}"
    if pedido is None or pedido.status == STATUS_PEDIDO_CANCELADO:
        avisar_erro(request, "Pedido não encontrado ou cancelado. Nada foi registrado.")
        return RedirectResponse("/financeiro/recebimentos", status_code=303)
    try:
        bruto = valor.replace("R$", "").replace(" ", "")
        if "," in bruto:
            bruto = bruto.replace(".", "").replace(",", ".")
        v = round(float(bruto), 2)
        d = dt.date.fromisoformat(data) if data else dt.date.today()
    except ValueError:
        avisar_erro(request, "Informe o valor (ex.: 15.000,00) e a data. Nada foi registrado.")
        return RedirectResponse(voltar, status_code=303)
    falta = pedido.a_receber()
    if v <= 0 or v > round(falta, 2) + 0.001:
        avisar_erro(request, f"O valor precisa ser maior que zero e no máximo o que falta receber ({comissao.reais(falta)}). Nada foi registrado.")
        return RedirectResponse(voltar, status_code=303)
    if d > dt.date.today():
        avisar_erro(request, "A data do recebimento não pode ser no futuro. Nada foi registrado.")
        return RedirectResponse(voltar, status_code=303)
    liberada = comissao.registrar_recebimento(db, pedido, v, d, user.nome_completo, observacao=observacao.strip()[:200] or None)
    db.commit()
    avisar_sucesso(request, f"Recebido {comissao.reais(v)} do pedido {pedido.codigo}."
                   + (f" Comissão de {comissao.reais(liberada)} liberada para {pedido.cliente.vendedor_nome or 'o vendedor'}."
                      if liberada is not None else " Produto sem regra de comissão."))
    return RedirectResponse(voltar, status_code=303)


@router.post("/financeiro/recebimentos/{recebimento_id}/desfazer")
def financeiro_desfazer_recebimento(request: Request, recebimento_id: int, user: User = Depends(require_role(*PERFIS_FINANCEIRO)),
                                    db: Session = Depends(get_db)):
    """Lancamento errado: apaga o recebimento (a comissao volta a ficar a liberar)
    e registra no historico do cliente quem desfez."""
    r = db.get(RecebimentoPedido, recebimento_id)
    if r is None or r.origem not in ("financeiro", "ajuste"):
        avisar_erro(request, "Só dá para desfazer recebimento registrado aqui (o do comprovante à vista fica). Nada mudou.")
        return RedirectResponse("/financeiro/recebimentos", status_code=303)
    pedido = r.pedido
    db.add(ContatoCRM(cliente_id=pedido.cliente_id, tipo="pagamento", autor=user.nome_completo,
                      texto=f"Recebimento de {comissao.reais(r.valor)} do pedido {pedido.codigo} desfeito (lançamento errado)."))
    db.delete(r)
    db.commit()
    avisar_sucesso(request, f"Recebimento de {comissao.reais(r.valor)} do pedido {pedido.codigo} desfeito.")
    return RedirectResponse("/financeiro/recebimentos", status_code=303)
