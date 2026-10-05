"""Comissao do vendedor (Rafael, 2026-10-04).

Percentual: por produto, um preco de corte (R$/t). Abaixo do corte paga um
percentual; A PARTIR dele (preco igual ao corte inclusive), outro. Padrao:
calcario corte R$ 70 (2% / 3%), gesso agricola corte R$ 60 (2% / 3%). Sulfato e
pedra britada: sem regra ate ele definir. Tudo editavel na pagina Regras.

O percentual fica GRAVADO no pedido quando ele e gerado ou renegociado
(PedidoCRM.comissao_pct/comissao_regra): mudar a regra so vale pra vendas novas.

Elegivel = o que a empresa ja recebeu x percentual (RecebimentoPedido). A
vista: quando o financeiro confirma o comprovante. Carga a carga, a prazo e
plano safra: cada pagamento que o financeiro registra. Sem pagamento, sem
comissao. Pedido cancelado nao gera comissao."""
from . import config

PRODUTO_CHAVE = {"Calcario": "calcario", "Gesso": "gesso", "Sulfato": "sulfato", "Pedra Britada": "pedra"}
PRODUTO_NOME = {"Calcario": "Calcário", "Gesso": "Gesso agrícola", "Sulfato": "Sulfato", "Pedra Britada": "Pedra britada"}


def _reais(v):
    return f"R$ {v:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


def calcular(produto, preco):
    """-> (percentual ou None, frase da regra aplicada)."""
    chave = PRODUTO_CHAVE.get(produto)
    nome = PRODUTO_NOME.get(produto, produto or "produto")
    if chave is None or not config.ligada(f"comissao_{chave}_liga"):
        return None, f"{nome}: sem regra de comissão"
    corte = config.valor(f"comissao_{chave}_corte")
    if (preco or 0) >= corte:
        pct = config.valor(f"comissao_{chave}_acima")
        return pct, f"{nome} a {_reais(preco or 0)}/t: a partir de {_reais(corte)} = {config.decimal_br(pct)}%"
    pct = config.valor(f"comissao_{chave}_abaixo")
    return pct, f"{nome} a {_reais(preco or 0)}/t: abaixo de {_reais(corte)} = {config.decimal_br(pct)}%"


def gravar(pedido):
    """Grava no pedido o percentual da regra de agora (gerar ou renegociar)."""
    pedido.comissao_pct, pedido.comissao_regra = calcular(pedido.produto, pedido.preco)


def reais(v):
    return _reais(v)


def situacao(pedido):
    """Frase curta de onde a comissao do pedido esta."""
    if pedido.comissao_pct is None:
        return "sem regra de comissão"
    if pedido.status == "cancelado":
        return "pedido cancelado: sem comissão"
    if pedido.a_receber() < 0.01:
        return "toda liberada"
    if pedido.recebido() >= 0.01:
        return "parte liberada; o resto quando o cliente pagar"
    if pedido.pagamento == "A vista":
        return "liberada quando o financeiro confirmar o pagamento"
    return "liberada conforme o cliente pagar"


def resumo(db, pedidos, inicio=None, fim=None):
    """Comissao de um conjunto de pedidos do portal (nao cancelados):
    liberada no periodo (pelos recebimentos com data no periodo) e a liberar
    (o que falta receber, em qualquer data)."""
    liberada = 0.0
    for p in pedidos:
        for r in p.recebimentos:
            if r.comissao_pct is not None and (inicio is None or r.data >= inicio) and (fim is None or r.data <= fim):
                liberada += r.valor * r.comissao_pct / 100
    a_liberar = sum(p.comissao_a_liberar() or 0 for p in pedidos)
    return {"liberada": liberada, "a_liberar": a_liberar,
            "sem_regra": sum(1 for p in pedidos if p.comissao_pct is None)}


def registrar_recebimento(db, pedido, valor, data, quem, origem="financeiro", observacao=None):
    """Grava o dinheiro que entrou, o registro no historico do cliente e o aviso
    de comissao liberada pro vendedor. -> comissao liberada (R$) ou None."""
    from .crm_routes import _avisar
    from .models import ContatoCRM, RecebimentoPedido
    db.add(RecebimentoPedido(pedido_id=pedido.id, valor=valor, data=data, origem=origem, observacao=observacao,
                             registrado_por=quem, comissao_pct=pedido.comissao_pct))
    cliente = pedido.cliente
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pagamento", autor=quem,
                      texto=f"Recebido {_reais(valor)} do pedido {pedido.codigo} em {data.strftime('%d/%m/%Y')}"
                            + (f" ({observacao})" if observacao else "") + "."))
    if pedido.comissao_pct is None:
        return None
    comissao = valor * pedido.comissao_pct / 100
    _avisar(db, cliente.vendedor_nome, cliente, "comissao_liberada", f"Comissão liberada: pedido {pedido.codigo}",
            f"{cliente.fazenda} pagou {_reais(valor)}: comissão de {_reais(comissao)} ({config.decimal_br(pedido.comissao_pct)}%) liberada.",
            quem)
    return comissao
