import datetime as dt
import json
import os
import re
import uuid
from collections import Counter
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import get_current_user, require_role
from . import comissao, config, menu
from .feedback import avisar_sucesso
from .database import get_db
from .models import (DATA_DESCONHECIDA, codigo_pedido, ESTADOS_OPERACAO, FASE_COR, FASE_LABEL, FASES_CRM, FORMA_PAGAMENTO_OPCOES,
                      MESES_PT, MOTIVO_PERDIDO, PLANO_SAFRA_MODALIDADE, PRODUTOS, RESULTADO_CONTATO, RESULTADO_COR,
                      STATUS_PEDIDO_ABERTO, STATUS_PEDIDO_CANCELADO, STATUS_PEDIDO_FINALIZADO, STATUS_PROPOSTA_ABERTA,
                      STATUS_PROPOSTA_CONVERTIDA, SUBSIDIARIAS, FORMA_PRAZO, PARCELAS_BOLETO, inicio_ciclo,
                      MOTIVOS_CONTATO_INVALIDO, TEMPERATURA_LABEL, TEMPERATURA_ORDEM, AreaEstado,
                      AvisoCRM, CicloVendas, ClienteCRM, ContatoAdicionalCRM, ContatoCRM, ParceiroCessao, PedidoComprovante,
                      OportunidadeLogistica, Pedido, PedidoCRM, PedidoDestinoFinal, PropostaCRM, User, calcular_avanco_fase, ciclo_rotulo, dias_para_virada_ciclo, fase_e_avanco,
                      validar_telefone, ler_coordenadas)
from . import geo

router = APIRouter()

NOME_ESTADO = {
    "MA": "Maranhão", "PA": "Pará", "TO": "Tocantins",
    "PI": "Piauí", "MT": "Mato Grosso", "GO": "Goiás",
}


def _mig_raiz(user):
    """Primeira migalha: a pagina inicial de cada papel (o "Mapa do CRM" virou
    quadro do Inicio do admin, 2026-10-04)."""
    if user.role == "vendedor":
        return ("Início", "/vendedor/crm")
    if user.role == "logistica":
        return ("Fila da logística", "/logistica/fila")
    return ("Início", "/admin/inicio")


def _migalhas_estado(user, uf):
    return [_mig_raiz(user), (f"{NOME_ESTADO.get(uf, uf)} ({uf})", None)]


def _migalhas_fase(user, uf, fase):
    return [_mig_raiz(user), (f"{NOME_ESTADO.get(uf, uf)} ({uf})", f"/crm/estado/{uf}"),
            (FASE_LABEL.get(fase, fase), None)]


def _migalhas_fase_vendedor(fase):
    return [("Início", "/vendedor/crm"), ("Carteira", "/vendedor/crm/carteira"),
            (FASE_LABEL.get(fase, fase), None)]


def _migalhas_novo_cliente(user):
    if user.role == "vendedor":
        return [("Início", "/vendedor/crm"), ("Carteira", "/vendedor/crm/carteira"), ("Novo cliente", None)]
    return [_mig_raiz(user), ("Novo cliente", None)]


# Listas de trabalho da home do vendedor de onde ele abre a ficha de um
# cliente (?via=...). Rafael (2026-09-25): se ele veio de Avisos, o caminho
# tem que ser CRM > Avisos > cliente, pra voltar e seguir pro proximo aviso
# sem passar pela Carteira.
ORIGENS_FICHA = {
    "avisos": ("Avisos", "/vendedor/crm/avisos"),
    "fila": ("Fila de trabalho", "/vendedor/crm/fila"),
    "agenda": ("Agenda", "/vendedor/crm/agenda"),
    "pedidos": ("Pedidos em aberto", "/vendedor/crm/pedidos"),
    "base": ("Fila de atualização de cadastro", "/vendedor/crm/base"),
}


def _via_ficha(request, cliente_id):
    """De qual lista a ficha foi aberta. Depois de salvar algo na ficha, o
    redirect perde o ?via=, mas o navegador manda a pagina anterior (a propria
    ficha ou uma sub-pagina dela) como Referer -- herda de la."""
    via = request.query_params.get("via", "")
    if via in ORIGENS_FICHA:
        return via
    ref = urlparse(request.headers.get("referer", ""))
    if ref.path == f"/crm/cliente/{cliente_id}" or ref.path.startswith(f"/crm/cliente/{cliente_id}/"):
        via = parse_qs(ref.query).get("via", [""])[0]
        if via in ORIGENS_FICHA:
            return via
    return None


def _url_ficha(cliente_id, aba, via=None, ancora=None):
    """Endereco da ficha depois de salvar: aba certa, origem preservada e
    ancora do item salvo (a pagina rola ate ele e destaca -- base.html)."""
    url = f"/crm/cliente/{cliente_id}?aba={aba}" + (f"&via={via}" if via else "")
    return url + (f"#{ancora}" if ancora else "")


def _seguir_lista(db, user, via, cliente_id):
    """Proximo passo depois de salvar: o proximo cliente da fila de onde o
    vendedor veio (trabalho ou cadastro), ou a volta pra lista de origem
    (agenda, avisos, pedidos). Retorna (link, rotulo) ou (None, None)."""
    if via in ("fila", "base"):
        fila, clientes, _, _, _, ids_leads = _montar_fila_trabalho(db, user)
        if via == "base":
            fila = _montar_fila_base(clientes, {i["cliente"].id for i in fila} | ids_leads)
        proximo = next((i for i in fila if i["cliente"].id != cliente_id), None)
        if proximo:
            return proximo["link"], f"Próximo da fila: {proximo['cliente'].fazenda}"
    if via in ORIGENS_FICHA:
        rotulo, url = ORIGENS_FICHA[via]
        return url, f"Voltar para {rotulo}"
    return None, None


def _migalhas_ate_cliente(user, cliente, cliente_e_leaf=True, via=None):
    """Migalhas ate a ficha do cliente. A tela mostra so o ultimo link, como
    "Voltar para ..." (Rafael, 2026-10-02), por isso o nivel da fase diz de
    qual lista ele e. Com `cliente_e_leaf=False`, o nome do
    cliente vira link (pra quem quiser acrescentar mais um nivel depois, tipo
    'Editar cadastro' ou 'Gerar pedido')."""
    cliente_url = None if cliente_e_leaf else f"/crm/cliente/{cliente.id}" + (f"?via={via}" if via else "")
    if via in ORIGENS_FICHA:
        return [("Início", "/vendedor/crm"), ORIGENS_FICHA[via], (cliente.fazenda, cliente_url)]
    if user.role == "vendedor":
        return [("Início", "/vendedor/crm"), ("Carteira", "/vendedor/crm/carteira"),
                (f"Carteira · {FASE_LABEL.get(cliente.fase, cliente.fase)}", f"/vendedor/crm/fase/{cliente.fase}"),
                (cliente.fazenda, cliente_url)]
    return [_mig_raiz(user), (f"{NOME_ESTADO.get(cliente.uf, cliente.uf)} ({cliente.uf})", f"/crm/estado/{cliente.uf}"),
            (f"{cliente.uf} · {FASE_LABEL.get(cliente.fase, cliente.fase)}", f"/crm/estado/{cliente.uf}/fase/{cliente.fase}"),
            (cliente.fazenda, cliente_url)]

# Lista oficial de municipios por estado (fonte: API publica do IBGE,
# servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios),
# baixada uma vez e congelada em arquivo -- usada tanto pra popular o select
# de cidade no cadastro (via fetch no navegador) quanto pra validar aqui no
# servidor que a cidade enviada realmente pertence ao estado escolhido.
_MUNICIPIOS_PATH = os.path.join(os.path.dirname(__file__), "static", "municipios_por_uf.json")
with open(_MUNICIPIOS_PATH, encoding="utf-8") as _f:
    MUNICIPIOS_POR_UF = json.load(_f)

# Contratos de cessao de credito (Plano safra) -- documento com dado sensivel
# de 3 partes (cliente, parceiro, empresa), por isso fica FORA de `static/`
# (nunca servido publicamente sem passar por autenticacao) -- so acessivel
# via rota propria que confere login antes de entregar o arquivo.
CONTRATOS_CESSAO_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads", "contratos_cessao")
os.makedirs(CONTRATOS_CESSAO_DIR, exist_ok=True)
# Comprovantes de pagamento de pedido a vista -- dado bancario do cliente,
# mesmo cuidado: fora de `static/`, so pela rota que confere a carteira.
COMPROVANTES_PAGAMENTO_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads", "comprovantes_pagamento")
os.makedirs(COMPROVANTES_PAGAMENTO_DIR, exist_ok=True)
EXTENSOES_COMPROVANTE = (".pdf", ".jpg", ".jpeg", ".png")
TAMANHO_MAX_COMPROVANTE = 10 * 1024 * 1024

TIPO_LOG_LABEL = {
    "nota": "Contato", "mudanca_fase": "Mudança de fase", "compra": "Compra",
    "retirada": "Retirada", "proposta": "Proposta", "agenda": "Agendamento", "dados": "Cadastro",
    "pedido": "Venda realizada", "pedido_cancelado": "Pedido cancelado", "pedido_finalizado": "Pedido finalizado",
    "destino_final": "Recebeu via parceiro", "destino_registrado": "Destino final registrado",
    "pagamento": "Pagamento", "oportunidade": "Oportunidade logística",
}
TIPO_LOG_COR = {
    "nota": "", "mudanca_fase": "gold", "compra": "green",
    "retirada": "green", "proposta": "blue", "agenda": "mineral", "dados": "",
    "pedido": "green", "pedido_cancelado": "red", "pedido_finalizado": "mineral",
    "destino_final": "clay", "destino_registrado": "clay", "pagamento": "green",
}

PROPOSTA_NUMERO_BASE = 78300
# Pedido do portal comeca em 1 e aparece como PV-0001 (models.codigo_pedido)
PEDIDO_NUMERO_BASE = 0


def _templates(request: Request):
    return request.app.state.templates


def _consulta_cliente_crm(db, nome_cliente_pedido):
    nome = re.sub(r"^\d+\s*", "", nome_cliente_pedido or "").strip()
    nome = nome.split(" - ")[0].strip()
    palavras = [p for p in re.split(r"\s+", nome) if len(p) > 2]
    if not palavras:
        return None
    padrao = "%" + "%".join(palavras) + "%"
    return db.query(ClienteCRM).filter((ClienteCRM.fazenda.ilike(padrao)) | (ClienteCRM.empresa.ilike(padrao)))


def encontrar_cliente_crm(db, nome_cliente_pedido):
    """Tentativa de achar o registro do CRM correspondente a um cliente de
    pedido (nao ha ainda um vinculo real tipo a aba 'Netsuite' da planilha
    Controle -- isso e so um match de nome, aproximado). Casa palavra por
    palavra (em vez da string inteira) pra tolerar diferencas de espacamento
    e pontuacao entre as duas bases, que sao reais (ex: espaco duplo)."""
    consulta = _consulta_cliente_crm(db, nome_cliente_pedido)
    return consulta.first() if consulta is not None else None


def encontrar_clientes_crm(db, nome_cliente_pedido):
    """TODOS os cadastros que casam com o nome do pedido. O CRM tem duplicados
    ("GRUPO ANVERSA" x "DANIEL ANVERSA E OUTROS", varios "JUPARANA"): pra dizer
    "este cliente NAO tem pedido" e mais seguro excluir todos (Rafael, 2026-10-04)."""
    consulta = _consulta_cliente_crm(db, nome_cliente_pedido)
    return consulta.all() if consulta is not None else []


def _resumo_fases(query):
    """Conta clientes por fase para uma query ja filtrada (por uf e/ou vendedor)."""
    contagem = {f: 0 for f in FASES_CRM}
    for cliente in query.all():
        contagem[cliente.fase] = contagem.get(cliente.fase, 0) + 1
    return contagem


def _cliente_do_usuario(db, user, cliente_id, permitir_via_parceiro=False):
    """Busca o cliente respeitando a carteira do vendedor (admin ve tudo).

    `permitir_via_parceiro`: libera cliente de OUTRA carteira quando ele
    recebeu produto de um pedido da carteira deste vendedor (Rafael,
    2026-09-26: clicar no historico e ir direto pro cliente final, pra fazer o
    pos-venda). So a ficha e o registro de contato usam isso -- proposta,
    pedido e cadastro continuam so com o dono da carteira."""
    cliente = db.get(ClienteCRM, cliente_id)
    if cliente and user.role == "vendedor" and cliente.vendedor_nome != user.vendedor_nome:
        if permitir_via_parceiro and _recebeu_de_pedido_da_carteira(db, cliente.id, user.vendedor_nome):
            return cliente
        return None
    return cliente


def _acesso_limitado(user, cliente):
    """Vendedor vendo cliente de outra carteira (liberado pelo destino final)."""
    return bool(cliente and user.role == "vendedor" and cliente.vendedor_nome != user.vendedor_nome)


def _recebeu_de_pedido_da_carteira(db, cliente_id, vendedor_nome):
    return (db.query(PedidoDestinoFinal)
              .join(PedidoCRM, PedidoDestinoFinal.pedido_id == PedidoCRM.id)
              .join(ClienteCRM, PedidoCRM.cliente_id == ClienteCRM.id)
              .filter(PedidoDestinoFinal.cliente_final_id == cliente_id,
                      ClienteCRM.vendedor_nome == vendedor_nome)
              .first() is not None)


def _so_digitos(telefone):
    return re.sub(r"\D", "", telefone or "")


def encontrar_telefone_duplicado(db, telefone, excluir_cliente_id=None):
    """Procura esse telefone (comparando so os digitos, ignora formatacao) em
    QUALQUER cliente do CRM -- telefone principal ou contato adicional --
    pra evitar que o mesmo numero fique cadastrado em mais de um cliente por
    engano (o vendedor as vezes nao percebe que o cliente ja esta na carteira)."""
    alvo = _so_digitos(telefone)
    if not alvo:
        return None
    for cid, nome, tel in db.query(ClienteCRM.id, ClienteCRM.fazenda, ClienteCRM.telefone).filter(ClienteCRM.telefone.isnot(None)):
        if cid != excluir_cliente_id and _so_digitos(tel) == alvo:
            return {"cliente_id": cid, "cliente_nome": nome, "pessoa": None}
    contatos = (db.query(ContatoAdicionalCRM.cliente_id, ContatoAdicionalCRM.nome, ContatoAdicionalCRM.funcao,
                          ContatoAdicionalCRM.telefone, ClienteCRM.fazenda)
                  .join(ClienteCRM, ContatoAdicionalCRM.cliente_id == ClienteCRM.id))
    for cid, nome_pessoa, funcao, tel, fazenda in contatos:
        if cid != excluir_cliente_id and _so_digitos(tel) == alvo:
            return {"cliente_id": cid, "cliente_nome": fazenda, "pessoa": f"{nome_pessoa} ({funcao})"}
    return None


def encontrar_email_duplicado(db, email, excluir_cliente_id=None):
    """Mesma ideia de encontrar_telefone_duplicado, pro campo email (comparado
    sem diferenciar maiusculas/minusculas)."""
    alvo = (email or "").strip().lower()
    if not alvo:
        return None
    cliente = (db.query(ClienteCRM.id, ClienteCRM.fazenda)
                 .filter(func.lower(ClienteCRM.email) == alvo).first())
    if cliente and cliente[0] != excluir_cliente_id:
        return {"cliente_id": cliente[0], "cliente_nome": cliente[1], "pessoa": None}
    return None


def encontrar_cnpj_duplicado(db, cnpj, excluir_cliente_id=None):
    """Mesma ideia de encontrar_telefone_duplicado, pro campo CNPJ/CPF
    (comparado so pelos digitos, ignora formatacao)."""
    alvo = _so_digitos(cnpj)
    if not alvo:
        return None
    for cid, nome, doc in db.query(ClienteCRM.id, ClienteCRM.fazenda, ClienteCRM.cnpj).filter(ClienteCRM.cnpj.isnot(None)):
        if cid != excluir_cliente_id and _so_digitos(doc) == alvo:
            return {"cliente_id": cid, "cliente_nome": nome, "pessoa": None}
    return None


def encontrar_clientes_similares(db, nome, vendedor_nome=None, limite=5):
    """Clientes com nome parecido (match palavra por palavra, mesmo criterio
    de encontrar_cliente_crm) -- so um aviso, nao bloqueia, porque nomes
    parecidos podem ser fazendas diferentes de verdade. Filtra pela carteira
    do vendedor quando informado, que e o caso real que motivou isso: o
    vendedor nao lembrar que ja tem esse cliente na propria carteira."""
    palavras = [p for p in re.split(r"\s+", nome.strip()) if len(p) > 2]
    if not palavras:
        return []
    padrao = "%" + "%".join(palavras) + "%"
    query = db.query(ClienteCRM).filter(ClienteCRM.fazenda.ilike(padrao))
    if vendedor_nome:
        query = query.filter(ClienteCRM.vendedor_nome == vendedor_nome)
    return query.limit(limite).all()


def _proximo_numero(db, model, base):
    maior = db.query(func.max(model.numero)).scalar()
    return max(maior or 0, base) + 1


def _recalcular_resumo_comercial(db, cliente):
    """Mantem os campos-resumo legados de ClienteCRM (usados no relatorio de
    desempenho por vendedor e no mapa) coerentes com as propostas/pedidos
    estruturados do CRM. Sem isso, gerar/renegociar/cancelar por aqui deixaria
    esses relatorios desatualizados sem ninguem perceber."""
    abertas = db.query(PropostaCRM).filter_by(cliente_id=cliente.id, status=STATUS_PROPOSTA_ABERTA).all()
    cliente.proposta_valor = sum(p.valor_total() for p in abertas) or None
    cliente.proposta_em = max((p.criado_em for p in abertas), default=None)

    pedidos = db.query(PedidoCRM).filter(
        PedidoCRM.cliente_id == cliente.id, PedidoCRM.status != STATUS_PEDIDO_CANCELADO
    ).all()
    # Volume que chegou via parceiro/transportadora (PedidoDestinoFinal) conta
    # como contratado pelo cliente final. `volume_retirado` fica so com os
    # pedidos diretos: a retirada do pedido do parceiro e um numero unico, nao
    # da pra saber quanto dele foi pra cada destino sem estimar.
    destinos = (db.query(PedidoDestinoFinal)
                  .join(PedidoCRM, PedidoDestinoFinal.pedido_id == PedidoCRM.id)
                  .filter(PedidoDestinoFinal.cliente_final_id == cliente.id,
                          PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all())
    cliente.volume_contratado = (sum(p.volume for p in pedidos) + sum(d.volume for d in destinos)) or None
    cliente.volume_retirado = sum((p.volume_retirado or 0) for p in pedidos) or None


def _canal_venda(comprador):
    return "transportadora" if comprador.e_transportadora else ("consultor" if comprador.e_consultor else "parceiro")


def _avisar(db, vendedor_nome, cliente, tipo, titulo, texto, autor):
    """Cria um aviso na caixa do vendedor. Cliente sem vendedor = ninguem pra
    avisar (nao inventa destinatario)."""
    if vendedor_nome:
        db.add(AvisoCRM(vendedor_nome=vendedor_nome, cliente_id=cliente.id if cliente else None,
                        tipo=tipo, titulo=titulo, texto=texto, autor=autor))


def _tem_compra_ativa(db, cliente_id, excluir_pedido_id=None):
    """Existe registro real de compra conosco ainda valido pra esse cliente:
    pedido direto aberto/finalizado, ou produto recebido via parceiro num
    pedido nao cancelado. `excluir_pedido_id` = pedido sendo cancelado agora
    (o status dele ainda nao foi gravado no banco)."""
    diretos = db.query(PedidoCRM).filter(
        PedidoCRM.cliente_id == cliente_id,
        PedidoCRM.status.in_([STATUS_PEDIDO_ABERTO, STATUS_PEDIDO_FINALIZADO]))
    indiretos = (db.query(PedidoDestinoFinal)
                   .join(PedidoCRM, PedidoDestinoFinal.pedido_id == PedidoCRM.id)
                   .filter(PedidoDestinoFinal.cliente_final_id == cliente_id,
                           PedidoCRM.status != STATUS_PEDIDO_CANCELADO))
    if excluir_pedido_id:
        diretos = diretos.filter(PedidoCRM.id != excluir_pedido_id)
        indiretos = indiretos.filter(PedidoCRM.id != excluir_pedido_id)
    return diretos.count() > 0 or indiretos.count() > 0


def _resumo_base(db, clientes, inicio_dt, fim_dt):
    """Trabalho de base no periodo (fila de cadastro e novos clientes):
    clientes cadastrados pelo CRM (nao importados) e cadastros editados."""
    novos = sum(1 for c in clientes if c.id_origem is None and c.criado_em and inicio_dt <= c.criado_em <= fim_dt)
    ids = [c.id for c in clientes]
    atualizados = 0
    if ids:
        atualizados = (db.query(ContatoCRM.cliente_id)
                         .filter(ContatoCRM.cliente_id.in_(ids), ContatoCRM.tipo == "dados",
                                 ContatoCRM.texto == "Dados do cliente atualizados.",
                                 ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt)
                         .distinct().count())
    return {"novos": novos, "atualizados": atualizados}


def _resumo_via_parceiro(db, ids_compradores, inicio_dt, fim_dt):
    """Clientes finais atendidos via parceiro/transportadora no periodo, a
    partir dos pedidos cujo COMPRADOR esta na lista (a carteira do vendedor
    que fez a venda pro parceiro)."""
    if not ids_compradores:
        return {"clientes": 0, "volume": 0.0, "pedidos": 0}
    destinos = (db.query(PedidoDestinoFinal)
                  .join(PedidoCRM, PedidoDestinoFinal.pedido_id == PedidoCRM.id)
                  .filter(PedidoCRM.cliente_id.in_(ids_compradores),
                          PedidoCRM.status != STATUS_PEDIDO_CANCELADO,
                          PedidoDestinoFinal.criado_em >= inicio_dt, PedidoDestinoFinal.criado_em <= fim_dt)
                  .all())
    return {"clientes": len({d.cliente_final_id for d in destinos}),
            "volume": sum(d.volume for d in destinos),
            "pedidos": len({d.pedido_id for d in destinos})}


def _estado_produto(db, cliente_id, produto):
    """Situacao da PROPOSTA de um produto pra um cliente: livre (pode propor)
    ou com proposta aberta (editar ou gerar pedido). So bloqueia duplicidade
    de PROPOSTA -- ter um pedido ja ativo desse produto nao impede uma nova
    proposta, porque o cliente pode querer comprar mais do mesmo produto."""
    proposta = (db.query(PropostaCRM)
                  .filter_by(cliente_id=cliente_id, produto=produto, status=STATUS_PROPOSTA_ABERTA)
                  .order_by(PropostaCRM.id.desc()).first())
    if proposta:
        return {"situacao": "proposta_aberta", "proposta": proposta}
    return {"situacao": "livre", "proposta": None}


def _credito_disponivel(db, cliente_id, produto):
    """Soma o credito (R$) que o cliente tem pendente pra um produto,
    olhando os pedidos ja finalizados dele com saldo_credito_valor > 0."""
    pedidos = db.query(PedidoCRM).filter(
        PedidoCRM.cliente_id == cliente_id, PedidoCRM.produto == produto,
        PedidoCRM.saldo_credito_valor.isnot(None), PedidoCRM.saldo_credito_valor > 0,
    ).all()
    return sum(p.saldo_credito_valor for p in pedidos), pedidos


def _dividir_volume_com_credito(usar_credito, credito_valor, volume_total_informado, preco):
    """Quando o vendedor marca 'usar credito', o que ele digita no campo
    Volume passa a significar o TOTAL que o cliente vai receber (mais
    intuitivo pro vendedor, que pensa em toneladas totais combinadas com o
    cliente) -- aqui descontamos as toneladas de credito (ao preco desta
    proposta) pra achar o volume NOVO/cobrado de verdade, que e o que fica
    guardado em PropostaCRM.volume (valor_total() continua = volume*preco).
    Retorna (usa_credito_final, volume_novo, erro)."""
    usa_credito_final = bool(usar_credito) and credito_valor > 0
    if not usa_credito_final:
        return False, volume_total_informado, None
    credito_ton = credito_valor / preco
    if volume_total_informado < credito_ton:
        erro = (f"O volume total informado ({volume_total_informado:.1f}t) é menor que o crédito "
                f"disponível ({credito_ton:.1f}t) — aumente o volume ou desmarque a aplicação do crédito.")
        return True, volume_total_informado, erro
    return True, volume_total_informado - credito_ton, None


@router.get("/api/busca-crm-clientes")
def busca_crm_clientes(q: str = "", user: User = Depends(require_role("admin", "logistica", "vendedor")), db: Session = Depends(get_db)):
    termo = q.strip()
    if len(termo) < 2:
        return JSONResponse([])
    coringa = f"%{termo}%"
    query = db.query(ClienteCRM).filter(
        (ClienteCRM.fazenda.ilike(coringa)) | (ClienteCRM.proprietario.ilike(coringa)) | (ClienteCRM.empresa.ilike(coringa))
    )
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes = query.limit(10).all()
    return JSONResponse([{"id": c.id, "label": f"{c.fazenda} — {c.cidade}, {c.uf}"} for c in clientes])


@router.get("/api/busca-cliente-destino")
def busca_cliente_destino(q: str = "", user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Busca de cliente final pra vincular a um pedido de parceiro. Nao filtra
    pela carteira do vendedor: o produto pode ter ido pra fazenda de um
    cliente de outro vendedor -- justamente o caso que o Rafael quer enxergar
    (cliente e transportadora cotando com a gente ao mesmo tempo)."""
    termo = q.strip()
    if len(termo) < 2:
        return JSONResponse([])
    coringa = f"%{termo}%"
    clientes = (db.query(ClienteCRM)
                  .filter((ClienteCRM.fazenda.ilike(coringa)) | (ClienteCRM.proprietario.ilike(coringa))
                          | (ClienteCRM.empresa.ilike(coringa)))
                  .limit(10).all())
    return JSONResponse([{"id": c.id, "label": f"{c.fazenda} — {c.cidade or 'cidade ?'}, {c.uf}",
                          "vendedor": c.vendedor_nome} for c in clientes])


@router.get("/api/crm-municipios/{uf}")
def crm_municipios(uf: str, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    return JSONResponse(MUNICIPIOS_POR_UF.get(uf.strip().upper(), []))


def _coordenadas_do_form(texto):
    """Campo "Coordenadas da fazenda" -> (valor pra gravar, erro). Grava
    padronizado ("-3.002100, -47.352700"), que e o que o botao do celular
    preenche; texto que nao e coordenada nao entra (vira local de entrega e
    rota da Logistica -- Rafael, 2026-10-03)."""
    texto = (texto or "").strip()
    if not texto:
        return None, None
    ponto = ler_coordenadas(texto)
    if not ponto:
        return None, (f"\"{texto[:60]}\" no campo Coordenadas não é uma coordenada. Use o botão \"Usar minha localização\", "
                      "o formato -3.0021, -47.3527 ou cole o link do Google Maps. Se não souber, deixe em branco.")
    return f"{ponto[0]:.6f}, {ponto[1]:.6f}", None


@router.post("/api/crm-ler-coordenadas")
def crm_ler_coordenadas(texto: str = Form(""), user: User = Depends(require_role("admin", "vendedor"))):
    """Conferencia ao vivo do campo de coordenadas (mesma leitura do salvar)."""
    valor, erro = _coordenadas_do_form(texto)
    if not valor:
        return JSONResponse({"ok": False})
    lat, lon = (float(x) for x in valor.split(","))
    return JSONResponse({"ok": True, "texto": valor, "link": geo.link_ponto((lat, lon))})


@router.get("/api/crm-verificar-duplicado")
def crm_verificar_duplicado(campo: str, valor: str, excluir_id: int = 0,
                             user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Checagem ao vivo (enquanto o vendedor digita) de telefone/email/cnpj ja
    cadastrados em OUTRO cliente -- complementa o bloqueio que ja acontece no
    submit, avisando mais cedo pra nao fazer o vendedor preencher a ficha
    toda antes de descobrir que o cliente ja existe."""
    excluir = excluir_id or None
    if campo == "telefone":
        dup = encontrar_telefone_duplicado(db, valor, excluir_cliente_id=excluir)
    elif campo == "email":
        dup = encontrar_email_duplicado(db, valor, excluir_cliente_id=excluir)
    elif campo == "cnpj":
        dup = encontrar_cnpj_duplicado(db, valor, excluir_cliente_id=excluir)
    else:
        return JSONResponse({"existe": False})
    if not dup:
        return JSONResponse({"existe": False})
    return JSONResponse({"existe": True, "cliente_id": dup["cliente_id"], "cliente_nome": dup["cliente_nome"], "pessoa": dup.get("pessoa")})


@router.get("/crm")
def crm_mapa(user: User = Depends(require_role("admin", "logistica"))):
    """O antigo "Mapa do CRM" (Rafael, 2026-10-04): os numeros por estado ficam no
    relatorio Market share (cada estado abre o funil); link antigo vai pra la."""
    destino = "/relatorios?r=market-share&periodo=tudo" if user.role in ("admin", "balcao") else "/logistica/inicio"
    return RedirectResponse(destino, status_code=303)


@router.get("/crm/log", response_class=HTMLResponse)
def crm_log(request: Request, vendedor: str = "", data_de: str = "", data_ate: str = "",
            user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    hoje = dt.date.today()
    data_de_d = dt.datetime.strptime(data_de, "%Y-%m-%d").date() if data_de else hoje
    data_ate_d = dt.datetime.strptime(data_ate, "%Y-%m-%d").date() if data_ate else hoje
    inicio = dt.datetime.combine(data_de_d, dt.time.min)
    fim = dt.datetime.combine(data_ate_d, dt.time.max)

    query = (db.query(ContatoCRM, ClienteCRM)
               .join(ClienteCRM, ContatoCRM.cliente_id == ClienteCRM.id)
               .filter(ContatoCRM.data >= inicio, ContatoCRM.data <= fim))
    if vendedor:
        query = query.filter(ClienteCRM.vendedor_nome == vendedor)
    linhas = query.order_by(ContatoCRM.data.desc()).limit(500).all()

    vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v})

    atividades = [{
        "data": contato.data, "vendedor": cliente.vendedor_nome or "Sem vendedor",
        "cliente": cliente.fazenda, "cliente_id": cliente.id,
        "tipo": contato.tipo, "tipo_label": TIPO_LOG_LABEL.get(contato.tipo, contato.tipo),
        "tipo_cor": TIPO_LOG_COR.get(contato.tipo, ""), "texto": contato.texto,
    } for contato, cliente in linhas]

    return _templates(request).TemplateResponse(request, "crm_log.html", {
        "user": user, "atividades": atividades, "vendedores": vendedores,
        "filtros": {"vendedor": vendedor, "data_de": data_de_d.isoformat(), "data_ate": data_ate_d.isoformat()},
        "atingiu_limite": len(atividades) >= 500,
        "migalhas": [_mig_raiz(user), ("Log de atividades", None)],
    })


# ---------- parceiros de cessao de credito (Plano safra) ----------

@router.get("/crm/parceiros", response_class=HTMLResponse)
def crm_parceiros(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    parceiros = db.query(ParceiroCessao).order_by(ParceiroCessao.ativo.desc(), ParceiroCessao.nome).all()
    return _templates(request).TemplateResponse(request, "crm_parceiros.html", {
        "user": user, "parceiros": parceiros, "erro": None,
        "migalhas": [_mig_raiz(user), ("Parceiros de Plano Safra", None)],
    })


def _cnpj_valido(digitos):
    """Confere os 2 digitos verificadores do CNPJ (recusa numero inventado)."""
    if len(digitos) != 14 or digitos == digitos[0] * 14:
        return False
    pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for n in (12, 13):
        soma = sum(int(digitos[i]) * pesos[i] for i in range(n))
        dv = 0 if soma % 11 < 2 else 11 - soma % 11
        if int(digitos[n]) != dv:
            return False
        pesos = [6] + pesos
    return True


def _validar_cnpj_parceiro(db, cnpj, excluir_id=None):
    """CNPJ do parceiro da cessao e obrigatorio (Rafael, 2026-10-02). Devolve
    (cnpj_formatado, parceiro_com_esse_cnpj, erro)."""
    digitos = _so_digitos(cnpj)
    if not digitos:
        return None, None, "Informe o CNPJ do parceiro."
    if not _cnpj_valido(digitos):
        return None, None, "CNPJ inválido. Confira os 14 números."
    formatado = f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"
    mesmo = next((p for p in db.query(ParceiroCessao).filter(ParceiroCessao.cnpj.isnot(None))
                  if _so_digitos(p.cnpj) == digitos and p.id != excluir_id), None)
    return formatado, mesmo, None


def _pagina_parceiros(request, db, user, erro=None):
    parceiros = db.query(ParceiroCessao).order_by(ParceiroCessao.ativo.desc(), ParceiroCessao.nome).all()
    return _templates(request).TemplateResponse(request, "crm_parceiros.html", {
        "user": user, "parceiros": parceiros, "erro": erro,
        "migalhas": [_mig_raiz(user), ("Parceiros de Plano Safra", None)],
    }, status_code=400 if erro else 200)


@router.post("/crm/parceiros")
def crm_criar_parceiro(request: Request, nome: str = Form(...), cnpj: str = Form(""),
                        user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    nome = " ".join(nome.split())
    erro = None
    if not nome:
        erro = "Informe o nome do parceiro."
    elif db.query(ParceiroCessao).filter(func.lower(ParceiroCessao.nome) == nome.lower()).first():
        erro = "Já existe um parceiro cadastrado com esse nome."
    if not erro:
        cnpj_fmt, mesmo, erro = _validar_cnpj_parceiro(db, cnpj)
        if mesmo:
            erro = f"Esse CNPJ já está cadastrado como {mesmo.nome}."
    if erro:
        return _pagina_parceiros(request, db, user, erro)
    db.add(ParceiroCessao(nome=nome, cnpj=cnpj_fmt, ativo=True))
    db.commit()
    avisar_sucesso(request, f"Parceiro {nome} cadastrado.")
    return RedirectResponse("/crm/parceiros", status_code=303)


@router.post("/crm/parceiros/{parceiro_id}/cnpj")
def crm_cnpj_parceiro(request: Request, parceiro_id: int, cnpj: str = Form(""),
                      user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Completar o CNPJ dos parceiros cadastrados antes de ele ser obrigatorio."""
    parceiro = db.get(ParceiroCessao, parceiro_id)
    if not parceiro:
        return RedirectResponse("/crm/parceiros", status_code=303)
    cnpj_fmt, mesmo, erro = _validar_cnpj_parceiro(db, cnpj, excluir_id=parceiro.id)
    if mesmo:
        erro = f"Esse CNPJ já está cadastrado como {mesmo.nome}."
    if erro:
        return _pagina_parceiros(request, db, user, f"{parceiro.nome}: {erro}")
    parceiro.cnpj = cnpj_fmt
    db.commit()
    avisar_sucesso(request, f"CNPJ de {parceiro.nome} salvo.")
    return RedirectResponse("/crm/parceiros", status_code=303)


@router.post("/crm/parceiros/rapido")
def crm_criar_parceiro_rapido(nome: str = Form(""), cnpj: str = Form(""),
                              user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Cadastro do parceiro de cessao direto da proposta, sem sair dela (Rafael,
    2026-10-02: "o usuario nao tem que voltar tudo e cadastrar o parceiro").
    O vendedor tambem pode -- a pagina Parceiros e so do admin, que continua
    podendo desativar. Nome e CNPJ obrigatorios; parceiro que ja existe (mesmo
    nome, sem diferenciar maiusculas, ou mesmo CNPJ) e reaproveitado em vez
    de duplicar."""
    nome = " ".join(nome.split())
    if len(nome) < 2:
        return JSONResponse({"ok": False, "erro": "Informe o nome do parceiro."}, status_code=400)
    cnpj_fmt, mesmo_cnpj, erro = _validar_cnpj_parceiro(db, cnpj)
    if erro:
        return JSONResponse({"ok": False, "erro": erro}, status_code=400)
    mesmo_nome = db.query(ParceiroCessao).filter(func.lower(ParceiroCessao.nome) == nome.lower()).first()
    if mesmo_nome and mesmo_cnpj and mesmo_nome.id != mesmo_cnpj.id:
        return JSONResponse({"ok": False, "erro": f"Esse CNPJ já está cadastrado como {mesmo_cnpj.nome}."}, status_code=400)
    existente = mesmo_nome or mesmo_cnpj
    if existente and not existente.ativo:
        return JSONResponse({"ok": False, "erro": f"{existente.nome} já está cadastrado, mas desativado. "
                                                  "Peça ao administrador para reativar em Parceiros."}, status_code=400)
    if existente:
        if existente.cnpj and _so_digitos(existente.cnpj) != _so_digitos(cnpj_fmt):
            return JSONResponse({"ok": False, "erro": f"{existente.nome} já está cadastrado com outro CNPJ ({existente.cnpj})."},
                                status_code=400)
        if not existente.cnpj:
            existente.cnpj = cnpj_fmt  # completa o cadastro antigo, que nao tinha CNPJ
            db.commit()
        return JSONResponse({"ok": True, "nome": existente.nome, "existente": True})
    db.add(ParceiroCessao(nome=nome, cnpj=cnpj_fmt, ativo=True))
    db.commit()
    return JSONResponse({"ok": True, "nome": nome, "existente": False})


@router.post("/crm/parceiros/{parceiro_id}/alternar-ativo")
def crm_alternar_parceiro(request: Request, parceiro_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    parceiro = db.get(ParceiroCessao, parceiro_id)
    if parceiro:
        # Nunca apaga -- so desativa/reativa. Propostas antigas ja guardam o
        # nome como snapshot (ver PropostaCRM.plano_safra_parceiro), entao
        # desativar um parceiro nao afeta historico, so impede escolhe-lo em
        # propostas NOVAS.
        parceiro.ativo = not parceiro.ativo
        db.commit()
        avisar_sucesso(request, f"Parceiro {parceiro.nome} {'reativado' if parceiro.ativo else 'desativado'}.")
    return RedirectResponse("/crm/parceiros", status_code=303)


@router.get("/crm/estado/{uf}", response_class=HTMLResponse)
def crm_funil_estado(request: Request, uf: str, cidade: str = "", ordenar: str = "", pendentes: str = "",
                      incompleto: str = "",
                      user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    uf = uf.upper()
    clientes_uf = db.query(ClienteCRM).filter(ClienteCRM.uf == uf).all()
    contagem = {f: 0 for f in FASES_CRM}
    for c in clientes_uf:
        contagem[c.fase] = contagem.get(c.fase, 0) + 1
    total = sum(contagem.values())
    estado = db.get(AreaEstado, uf)

    fases = [{
        "chave": f, "label": FASE_LABEL[f], "cor": FASE_COR[f], "qtd": contagem[f],
        "pct": (contagem[f] / total * 100) if total else 0,
    } for f in FASES_CRM]

    # Visao geral com TODOS os clientes do estado, sem filtrar por fase --
    # pro admin/logistica que quer ver a carteira inteira daquele estado de
    # uma vez (ex: ordenar por contato mais antigo, ou filtrar por cidade),
    # em vez de entrar fase por fase no funil.
    cidades = sorted({c.cidade for c in clientes_uf if c.cidade})
    clientes = _ordenar_e_filtrar(clientes_uf, cidade, ordenar, pendentes == "1", incompleto == "1")

    return _templates(request).TemplateResponse(request, "crm_funil.html", {
        "user": user, "uf": uf, "fases": fases, "total": total, "estado": estado,
        "voltar_url": _mig_raiz(user)[1], "base_url": f"/crm/estado/{uf}",
        "fase": None, "fase_label_dict": FASE_LABEL, "fase_cor_dict": FASE_COR,
        "clientes": clientes, "cidades": cidades, "ficha_base_url": "/crm/cliente",
        "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes, "incompleto": incompleto},
        "migalhas": _migalhas_estado(user, uf),
    })


def _ordenar_e_filtrar(clientes, cidade, ordenar, so_pendentes, so_incompleto=False):
    if cidade:
        clientes = [c for c in clientes if c.cidade == cidade]
    if so_incompleto:
        clientes = [c for c in clientes if c.cadastro_incompleto()]
    if so_pendentes:
        clientes = [c for c in clientes if c.precisa_ajuda
                    or c.dias_desde_ultima_interacao() is None
                    or c.dias_desde_ultima_interacao() >= 14]
    if ordenar == "recente":
        clientes.sort(key=lambda c: c.ultima_interacao_em or dt.datetime.min, reverse=True)
    elif ordenar == "antigo":
        clientes.sort(key=lambda c: c.ultima_interacao_em or dt.datetime.min)
    elif ordenar == "sazonalidade":
        mes_atual = dt.datetime.utcnow().month
        # Quem compra agora (ou esta perto de comprar) primeiro; quem nao tem
        # historico de compra confiavel entra depois, do contato mais antigo
        # pro mais recente (mesmo criterio de "antigo" pra esses).
        def chave(c):
            p = c.prioridade_sazonal(mes_atual)
            if p is None:
                return (1, 0, c.ultima_interacao_em or dt.datetime.min)
            return (0, p, c.ultima_interacao_em or dt.datetime.min)
        clientes.sort(key=chave)
    else:
        clientes.sort(key=lambda c: c.fazenda)
    return clientes


@router.get("/crm/estado/{uf}/fase/{fase}", response_class=HTMLResponse)
def crm_lista_clientes(request: Request, uf: str, fase: str, cidade: str = "", ordenar: str = "",
                        pendentes: str = "", incompleto: str = "",
                        user: User = Depends(require_role("admin", "logistica")),
                        db: Session = Depends(get_db)):
    uf = uf.upper()
    clientes = db.query(ClienteCRM).filter(ClienteCRM.uf == uf, ClienteCRM.fase == fase).all()
    cidades = sorted({c.cidade for c in clientes if c.cidade})
    clientes = _ordenar_e_filtrar(clientes, cidade, ordenar, pendentes == "1", incompleto == "1")
    return _templates(request).TemplateResponse(request, "crm_lista_clientes.html", {
        "user": user, "uf": uf, "fase": fase, "fase_label": FASE_LABEL.get(fase, fase),
        "clientes": clientes, "cidades": cidades, "voltar_url": f"/crm/estado/{uf}",
        "ficha_base_url": "/crm/cliente", "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes, "incompleto": incompleto},
        "migalhas": _migalhas_fase(user, uf, fase),
    })


def _agrupar_historico_por_periodo(itens):
    """Agrupa o restante do Historico (depois dos fixados) por periodo, pra
    a aba nao virar uma lista infinita conforme o relacionamento com o
    cliente envelhece -- pedido do Rafael (2026-09-24): mes atual sempre
    visivel; meses anteriores DO MESMO ANO colapsam um por mes (clica pra
    abrir); anos anteriores colapsam um por ANO INTEIRO (nao por mes -- so
    o ano corrente e detalhado por mes). E SO agrupamento de exibicao --
    nenhum ContatoCRM e apagado, movido ou alterado, so a forma de mostrar.
    `itens` precisa vir ordenado por data desc (como `historico` ja vem).
    Retorna (itens_mes_atual, grupos_colapsados)."""
    hoje = dt.date.today()
    atuais = []
    por_mes, ordem_mes = {}, []
    por_ano, ordem_ano = {}, []
    for h in itens:
        d = h.data
        if d.year == hoje.year and d.month == hoje.month:
            atuais.append(h)
        elif d.year == hoje.year:
            chave = (d.year, d.month)
            if chave not in por_mes:
                por_mes[chave] = []
                ordem_mes.append(chave)
            por_mes[chave].append(h)
        else:
            if d.year not in por_ano:
                por_ano[d.year] = []
                ordem_ano.append(d.year)
            por_ano[d.year].append(h)

    grupos = []
    for (ano, mes) in ordem_mes:
        grupos.append({"label": f"{MESES_PT[mes - 1]} de {ano}", "itens": por_mes[(ano, mes)]})
    for ano in ordem_ano:
        grupos.append({"label": str(ano), "itens": por_ano[ano]})
    return atuais, grupos


def _orientacao_contato(cliente, pedidos, propostas_abertas, recebimentos):
    """Roteiro curto pro "Registrar contato" (Rafael, 2026-09-25: orientar o
    vendedor sobre o que conversar). Olha primeiro o que existe de verdade --
    pedido, proposta, produto recebido via parceiro -- e so depois a fase, que
    sozinha engana (ex: fase "proposta" importada sem PropostaCRM real).
    `resultado` e a opcao pre-selecionada em "O que aconteceu?"."""
    meses = cliente.meses_compra_texto()
    if meses and cliente.prioridade_sazonal() == 0:
        epoca = f" Costuma comprar em {meses} — bom momento pra conversar."
    elif meses:
        epoca = f" Costuma comprar em {meses}."
    else:
        epoca = ""

    if cliente.fase in ("perdido", "nao_usara"):
        motivo = MOTIVO_PERDIDO.get(cliente.motivo_perdido) if cliente.motivo_perdido else None
        return {"titulo": "Reativação", "cor": "grey",
                "contexto": f"Cliente marcado como {FASE_LABEL[cliente.fase]}" + (f" (motivo: {motivo})." if motivo else "."),
                "perguntas": ["O que mudou desde a última conversa?",
                              "Tem necessidade de produto pra próxima safra?",
                              'Se houver abertura, marque "Cliente voltou a negociar" — ele volta pro funil.'],
                "exemplo": "Ex: continua comprando de outro fornecedor; pediu pra ligar de novo em marco.",
                "resultado": ""}

    vencidos = [p for p in pedidos if p.vencido()]
    if vencidos:
        p = vencidos[0]
        dias = -p.dias_restantes()
        return {"titulo": "Retirada atrasada", "cor": "red",
                "contexto": f"Pedido {p.codigo} ({p.produto}) passou do prazo há {dias} dia{'s' if dias != 1 else ''} sem retirada.",
                "perguntas": ["O cliente ainda vai retirar? Quando?",
                              "Tem algum problema de frete, pagamento ou armazenagem?",
                              "Se não vai retirar tudo, combine finalizar o pedido pelo volume retirado."],
                "exemplo": "Ex: vai retirar o restante até dia 20, estava sem caminhão disponível.",
                "resultado": "contato"}

    ativos = [p for p in pedidos if p.status == STATUS_PEDIDO_ABERTO]
    ativos_indiretos = [d for d in recebimentos if d.pedido.status == STATUS_PEDIDO_ABERTO]
    if ativos or ativos_indiretos:
        perguntas = ["Como está sendo o carregamento/entrega? Teve algum problema?",
                     "O produto chegou em boas condições? A qualidade atendeu?",
                     "Já retirou boa parte? Vai precisar de mais produto nesta safra?"]
        if ativos_indiretos:
            d = ativos_indiretos[0]
            comprador = d.pedido.cliente
            canal = "transportadora" if comprador.e_transportadora else ("consultor" if comprador.e_consultor else "parceiro")
            contexto = (f"Recebeu {d.volume:g}t de {d.pedido.produto} via {canal} {comprador.fazenda} "
                        f"(pedido {d.pedido.codigo}).")
            perguntas.append("Pra próxima compra, dá pra fechar direto com a gente? Se ele precisa do frete junto, anote.")
        else:
            p = ativos[0]
            contexto = f"Pedido {p.codigo} ({p.produto}, {p.volume:g}t) em andamento."
        return {"titulo": "Pós-venda", "cor": "green", "contexto": contexto, "perguntas": perguntas,
                "exemplo": "Ex: carregamento ok, qualidade aprovada, deve precisar de mais 200t em novembro.",
                "resultado": "contato"}

    if propostas_abertas:
        p = max(propostas_abertas, key=lambda x: x.atualizado_em or x.criado_em)
        ref = p.atualizado_em or p.criado_em
        dias = (dt.datetime.utcnow() - ref).days if ref else None
        quando = f" há {dias} dia{'s' if dias != 1 else ''}" if dias is not None else ""
        return {"titulo": "Retorno da proposta", "cor": "blue",
                "contexto": f"Proposta no {p.numero} ({p.produto}, {p.volume:g}t a R$ {p.preco:.2f}/t) enviada{quando}.",
                "perguntas": ["O cliente já avaliou a proposta?",
                              "Alguma objeção de preço, prazo ou forma de pagamento?",
                              "O que falta pra fechar? Combine a data do próximo retorno."],
                "exemplo": "Ex: achou o preço alto, vai comparar com outro fornecedor e responde até sexta.",
                "resultado": "contato"}

    if cliente.fase == "realizado" or pedidos or recebimentos:
        return {"titulo": "Relacionamento e próxima compra", "cor": "green",
                "contexto": "Cliente já comprou com a gente." + epoca,
                "perguntas": ["Como foi o resultado do produto na lavoura?",
                              "Qual a previsão de compra pra próxima safra (produto e volume)?",
                              "Tem outra área ou produto que possamos atender?"],
                "exemplo": "Ex: resultado bom na lavoura, previsão de 500t pra próxima safra, compra em agosto.",
                "resultado": "contato"}

    if cliente.fase in ("contactado", "proposta"):
        return {"titulo": "Retomar contato e abrir negociação", "cor": "gold",
                "contexto": "Já houve contato, mas não tem proposta aberta no sistema." + epoca,
                "perguntas": ["Qual a necessidade pra próxima safra (produto e volume)?",
                              "Quando costuma comprar e quem decide a compra?",
                              "Tem interesse em receber uma proposta? Se sim, registre na aba Proposta."],
                "exemplo": "Ex: planta 1.500 ha de soja, compra em agosto, pediu proposta de 300t de calcário.",
                "resultado": "contato"}

    return {"titulo": "Primeiro contato", "cor": "mineral",
            "contexto": "Cliente ainda não foi contatado." + epoca,
            "perguntas": ["Apresente a empresa e os produtos.",
                          "Confirme área plantada, culturas e se já usa corretivo.",
                          "Quem decide a compra e em que época costuma comprar?"],
            "exemplo": "Ex: falei com o gerente, 800 ha de soja, ainda não usa calcário, pediu retorno em outubro.",
            "resultado": "contato"}


# Desfechos do "Registrar contato" (Rafael, 2026-10-02): o vendedor diz o que
# aconteceu com um toque, e o proximo contato ja vem sugerido pelo desfecho.
# Todos levam o cliente pra Contactados (houve trabalho com ele), mas ficam
# registrados separados. Retorno padrao em dias; None = sem retorno automatico.
CONVERSAMOS = {"valor": "contato", "rotulo": "Conversamos", "icone": "phone-call", "retorno": 7}
NAO_ATENDEU = {"valor": "nao_atendeu", "rotulo": "Não atendeu", "icone": "phone-missed", "retorno": 2}
RETORNAR_DEPOIS = {"valor": "retornar_depois", "rotulo": "Pediu pra ligar depois", "icone": "clock", "retorno": 7}
SEM_INTERESSE_AGORA = {"valor": "sem_interesse_agora", "rotulo": "Sem interesse agora", "icone": "circle-pause", "retorno": 30}
# Numero errado / sem WhatsApp (Rafael, 2026-10-02): nao muda a etapa nem agenda
# retorno -- o caso vai pro administrador conseguir outro numero ou meio de contato.
CONTATO_INVALIDO = {"valor": "contato_invalido", "rotulo": "Número errado / sem WhatsApp", "icone": "phone-off", "retorno": None}
RETORNO_POR_CLIMA = {"quente": 3, "morno": 7, "frio": 30}
MOTIVOS_SEM_INTERESSE = ["Já comprou de outro", "Preço", "Sem necessidade nesta safra", "Está no plantio", "Outro"]


def _formulario_contato(cliente, propostas_abertas, acesso_limitado=False):
    if acesso_limitado:
        # Cliente de outra carteira aberto pelo destino final: so o contato.
        return {"resultados": [{**CONVERSAMOS, "rotulo": "Conversamos (pós-venda)", "retorno": None},
                               {**NAO_ATENDEU, "retorno": None}],
                "anotacao": True, "temperatura": False, "motivo": False, "agenda": False,
                "dica": f"Cliente da carteira de {cliente.vendedor_nome}: etapa, proposta e agenda continuam com essa carteira."}
    return {**_formulario_contato_por_fase(cliente, propostas_abertas), "agenda": True}


def _formulario_contato_por_fase(cliente, propostas_abertas):
    """Desfechos do "Registrar contato" que fazem sentido na fase atual (Rafael,
    2026-09-25: "nao faz sentido o cliente ja estar em realizado e a gente dar
    essa sugestao"). Encerrar cliente (nao usara/perdido) fica nos botoes do
    topo da ficha, que pedem produto/motivo."""
    if cliente.fase == "realizado":
        return {"resultados": [{**CONVERSAMOS, "rotulo": "Conversamos (pós-venda)", "retorno": 30}, NAO_ATENDEU, RETORNAR_DEPOIS,
                               CONTATO_INVALIDO],
                "anotacao": True, "temperatura": False, "motivo": False,
                "dica": "Cliente quer comprar mais? Registre uma nova proposta na aba Proposta."}
    if cliente.fase in ("perdido", "nao_usara"):
        # Tentativa sem resposta nao reabre cliente encerrado: so a volta de verdade.
        return {"resultados": [{**CONVERSAMOS, "rotulo": "Cliente voltou a negociar", "icone": "rotate-ccw"}],
                "anotacao": True, "temperatura": False, "motivo": False,
                "dica": "Voltar a negociar devolve o cliente pro funil, em Contactados."}
    if propostas_abertas:
        return {"resultados": [{**CONVERSAMOS, "rotulo": "Conversamos sobre a proposta"}, NAO_ATENDEU, RETORNAR_DEPOIS,
                               CONTATO_INVALIDO],
                "anotacao": True, "temperatura": True, "motivo": False,
                "dica": "Cliente aceitou? Gere o pedido na aba Proposta. Desistiu? Use \"Perdido\" no topo da ficha."}
    return {"resultados": [CONVERSAMOS, NAO_ATENDEU, RETORNAR_DEPOIS, SEM_INTERESSE_AGORA, CONTATO_INVALIDO],
            "anotacao": True, "temperatura": True, "motivo": True,
            "dica": "Cliente quer proposta? Registre na aba Proposta. Sem interesse de vez? Use \"Não usará\" ou \"Perdido\" no topo da ficha."}


def _data_epoca_compra(cliente, hoje):
    """Retorno "na época de compra": 1 mes antes do proximo mes em que o
    cliente costuma comprar (a mesma antecedencia da fila). None sem historico."""
    meses = cliente.meses_compra_historico()
    if not meses:
        return None
    falta = min((m - hoje.month) % 12 for m in meses)
    ano, mes = hoje.year + (hoje.month - 1 + falta) // 12, (hoje.month - 1 + falta) % 12 + 1
    alvo = dt.date(ano, mes, 1) - dt.timedelta(days=30)
    return alvo if alvo > hoje else hoje + dt.timedelta(days=7)


def _dias_ultimo_contato_real(historico):
    """Dias desde o ultimo contato DE VERDADE (mesma regra da fila:
    TIPOS_CONTATO_REAL, sem o registro "Importado da planilha"). O campo
    `ultima_interacao_em` nao serve pro resumo da ficha: pros importados ele
    e a data da importacao, e a ficha diria "ha 26 dias" pra quem nunca foi
    contatado. None = nenhum contato registrado."""
    datas = [h.data for h in historico
             if h.tipo in TIPOS_CONTATO_REAL and h.data and h.data != DATA_DESCONHECIDA
             and not (h.texto or "").startswith("Importado da planilha")]
    return (dt.datetime.utcnow() - max(datas)).days if datas else None


def _caminho_venda(cliente, historico, propostas_abertas, pedidos):
    """Caminho da venda no topo da ficha, no estilo do rastreio dos Correios
    (Rafael, 2026-10-02): UM TRILHO POR NEGOCIO, como cada encomenda tem o
    seu rastreio -- cliente pode ter varios pedidos, do mesmo produto ou de
    produtos diferentes. O contato e do cliente (fica uma vez so, no topo);
    cada proposta aberta e cada pedido em aberto ganham uma linha com o
    proprio trilho; o que ja foi concluido no ciclo fica recolhido.

    A ordem das etapas depende do pagamento: a vista paga antes de carregar
    (Pedido > Pagamento > Carga); a prazo e plano safra carregam antes e
    pagam depois (Pedido > Carga > Pagamento). Dado que nao existe aparece
    como falta, nunca inventado."""
    encerrado = cliente.fase in ("perdido", "nao_usara")

    def br(valor, casas=0):
        return f"{valor:,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")

    def fechamento(pd):
        return next((h for h in historico if h.tipo == "pedido_finalizado" and h.numero == pd.numero), None)

    def forma(obj):
        if obj.pagamento == "A vista":
            return "à vista"
        if obj.pagamento == "A prazo":
            fp = getattr(obj, "forma_prazo", None)
            return "a prazo" + (f" · {FORMA_PRAZO[fp].lower()}" if fp in FORMA_PRAZO else "")
        if obj.pagamento == "Plano safra":
            via = f"via {obj.plano_safra_parceiro}" if obj.plano_safra_modalidade == "cessao" else "direto"
            venc = getattr(obj, "vencimento_pagamento", None)
            return f"plano safra {via}" + (f" · paga em {venc.strftime('%d/%m/%Y')}" if venc else "")
        return (obj.pagamento or "").lower()

    def trilho(pagamento, estados, datas):
        ordem = (["proposta", "pedido", "pagamento", "carga", "fim"] if pagamento == "A vista"
                 else ["proposta", "pedido", "carga", "pagamento", "fim"])
        rotulos = {"proposta": "Proposta", "pedido": "Pedido", "pagamento": "Pagamento", "carga": "Carregamento", "fim": "Fim"}
        return [{"chave": c, "rotulo": rotulos[c], "estado": estados.get(c, "futuro"), "quando": datas.get(c),
                 "pct": pcts.get(c)} for c in ordem]

    def pct_carregado(pd):
        """% ja carregado do pedido em aberto (volume_retirado, que vem do
        NetSuite). None = ainda sem dado -- nunca mostrar 0% inventado."""
        total = pd.volume_total_a_entregar()
        if pd.volume_retirado is None or not total:
            return None
        return min(100, round(pd.volume_retirado / total * 100))

    pcts = {}

    negocios = []
    for pd in sorted((p for p in pedidos if p.status == STATUS_PEDIDO_ABERTO), key=lambda p: p.id):
        origem = pd.proposta
        datas = {"proposta": (origem.atualizado_em or origem.criado_em) if origem else None, "pedido": pd.criado_em}
        estados = {"proposta": "feito", "pedido": "feito"}
        neg = {"tipo": "pedido", "obj": pd, "titulo": f"{pd.produto} · pedido {pd.codigo}",
               "sub": f"{br(pd.volume_total_a_entregar())} t · {forma(pd)}", "acao": None, "status": None}
        if pd.pagamento == "A vista":
            sit = pd.situacao_pagamento()
            if sit == "liberado":
                estados.update(pagamento="feito", carga="atual")
                datas["pagamento"] = pd.pagamento_liberado_em
                neg["status"], neg["cor"], neg["acao"] = "liberado para carregar", "green", "finalizar"
            else:
                estados["pagamento"] = "atual"
                neg["status"], neg["cor"] = {
                    "em_conferencia": ("comprovante em conferência pelo financeiro", "blue"),
                    "recusado": (f"comprovante recusado: {pd.comprovantes[-1].motivo_recusa}" if pd.comprovantes else "comprovante recusado", "red"),
                    "diferenca": ("falta o comprovante da diferença", "gold"),
                }.get(sit, ("aguardando o comprovante", "gold"))
                neg["acao"] = None if sit == "em_conferencia" else "comprovante"
        else:
            estados["carga"] = "atual"
            neg["status"], neg["cor"], neg["acao"] = "carregando", "green", "finalizar"
            neg["regra"] = pd.regra_pagamento()
        if pd.vencido():
            neg["status"], neg["cor"] = "prazo de retirada vencido", "red"
        elif pd.data_limite_retirada and neg["acao"] == "finalizar":
            neg["status"] += f" até {pd.data_limite_retirada.strftime('%d/%m')}"
        pcts = {"carga": pct_carregado(pd)} if estados.get("carga") == "atual" else {}
        neg["etapas"] = trilho(pd.pagamento, estados, datas)
        pcts = {}
        negocios.append(neg)

    for p in ([] if encerrado else propostas_abertas):
        negocios.append({
            "tipo": "proposta", "obj": p, "titulo": f"{p.produto} · proposta nº {p.numero}",
            "sub": f"{br(p.volume)} t a R$ {br(p.preco, 2)}/t · {forma(p)}",
            "status": "esperando o cliente aceitar", "cor": "blue", "acao": "gerar_pedido",
            "etapas": trilho(p.pagamento, {"proposta": "atual"}, {"proposta": p.atualizado_em or p.criado_em}),
        })

    # Concluidos neste ciclo (pedido finalizado gerado desde o ultimo 1o de novembro)
    inicio = dt.datetime.combine(inicio_ciclo(), dt.time())
    concluidos = []
    for pd in pedidos:
        if pd.status == STATUS_PEDIDO_FINALIZADO and pd.criado_em and pd.criado_em >= inicio:
            fim = fechamento(pd)
            concluidos.append({"obj": pd, "quando": fim.data if fim else None,
                               "sub": f"{br(pd.volume_retirado or 0)} t retiradas · {forma(pd)}"})

    contato = next((h for h in historico if h.tipo == "nota" and h.resultado
                    and not (h.texto or "").startswith("Importado da planilha")), None)
    contato_ok = bool(contato) or cliente.fase in ("contactado", "proposta", "realizado") or bool(negocios)

    # Cliente encerrado (perdido / nao usara): o trilho mostra em que etapa a
    # venda parou -- antes ou depois da proposta (Rafael, 2026-10-02).
    encerramento = None
    if encerrado:
        marco = next((h for h in historico if h.tipo == "mudanca_fase" and h.fase_destino == cliente.fase), None)
        nota = next((h for h in historico if h.tipo == "nota" and h.resultado in ("perdido", "sem_interesse")), None)
        teve_proposta = any(h.tipo == "proposta" for h in historico) or bool(propostas_abertas)
        quando = (marco or nota).data if (marco or nota) else None
        perdido = cliente.fase == "perdido"
        motivo = MOTIVO_PERDIDO.get(cliente.motivo_perdido) if perdido else None
        # A nota grava "Motivo: <motivo>. <detalhe>" -- so o detalhe, sem repetir o motivo
        detalhe = (nota.texto or "").replace("Motivo: ", "", 1).strip() if nota else ""
        if motivo and detalhe.startswith(motivo):
            detalhe = detalhe[len(motivo):].lstrip(" .")
        encerramento = {
            "rotulo": FASE_LABEL.get(cliente.fase, cliente.fase), "perdido": perdido,
            "quando": quando, "autor": (marco or nota).autor if (marco or nota) else None,
            "motivo": motivo, "detalhe": detalhe or None,
            "reavaliar": cliente.proximo_retorno_em,
            "etapas": [
                {"chave": "contato", "rotulo": "Contato", "estado": "feito" if contato_ok else "pulado", "quando": contato.data if contato else None},
                {"chave": "proposta", "rotulo": "Proposta", "estado": "feito" if teve_proposta else "pulado", "quando": None},
                {"chave": "encerrado", "rotulo": FASE_LABEL.get(cliente.fase, cliente.fase),
                 "estado": "perdido" if perdido else "nao_usara", "quando": quando},
            ],
        }
    return {
        "encerramento": encerramento,
        "contato": {"feito": contato_ok,
                    "resumo": RESULTADO_CONTATO.get(contato.resultado, contato.resultado) if contato else None,
                    "quando": contato.data if contato else None},
        "negocios": negocios, "concluidos": concluidos,
        # Sem negocio aberto: o proximo passo e propor (de novo, se ja comprou neste ciclo)
        "propor": contato_ok and not negocios and not encerrado,
        "venda_antes_do_portal": cliente.fase == "realizado" and not pedidos,
    }


def _contexto_ficha(db, user, cliente, erro=None, aba="dados", via=None, extra=None):
    historico = []
    historico_fixados = 0
    propostas_abertas = []
    produtos_disponiveis = []
    pedidos = []
    propostas_por_numero = {}
    proposta_substituida_em = {}
    creditos_por_produto = {}
    if cliente:
        historico = db.query(ContatoCRM).filter_by(cliente_id=cliente.id).order_by(ContatoCRM.data.desc()).all()
        propostas_abertas = (db.query(PropostaCRM)
                               .filter_by(cliente_id=cliente.id, status=STATUS_PROPOSTA_ABERTA)
                               .order_by(PropostaCRM.id.asc()).all())
        # Proposta em aberto fica fixada no topo do Historico -- só sai de la
        # quando vira pedido (a PropostaCRM.status vira "convertida", some de
        # propostas_abertas sozinha) ou o cliente e marcado perdido. Marcar
        # perdido NAO fecha a PropostaCRM em si (ela continua "aberta" no
        # banco, editavel se o vendedor reabrir o cliente depois) -- so para
        # de ser FIXADA, que e so uma questao de destaque visual. So fixa o
        # registro MAIS RECENTE de cada proposta aberta (o historico ja vem
        # ordenado por data desc, entao o primeiro que aparecer pra cada
        # numero e o mais recente); edicoes antigas da mesma proposta ficam
        # no lugar cronologico normal.
        numeros_abertos = {p.numero for p in propostas_abertas} if cliente.fase != "perdido" else set()
        if numeros_abertos:
            fixados, vistos, resto = [], set(), []
            for h in historico:
                if h.tipo == "proposta" and h.numero in numeros_abertos and h.numero not in vistos:
                    fixados.append(h)
                    vistos.add(h.numero)
                else:
                    resto.append(h)
            historico = fixados + resto
            historico_fixados = len(fixados)
        produtos_ocupados = {p.produto for p in propostas_abertas}
        produtos_disponiveis = [p for p in PRODUTOS if p not in produtos_ocupados]
        pedidos = db.query(PedidoCRM).filter_by(cliente_id=cliente.id).order_by(PedidoCRM.id.desc()).all()
        # pro historico poder mostrar "Editar proposta"/"Gerar pedido" direto no
        # registro certo -- precisa do id da PropostaCRM viva, o snapshot no
        # ContatoCRM so tem o numero.
        propostas_por_numero = {p.numero: p for p in db.query(PropostaCRM).filter_by(cliente_id=cliente.id).all()}
        # Cada edicao gera um registro novo da mesma proposta: so o mais recente
        # e a versao que vale (acoes ficam nele); os anteriores sao so registro
        # e mostram quando foram substituidos. Historico vem do mais novo pro
        # mais antigo, entao o primeiro de cada numero e a versao vigente.
        versoes_proposta = {}
        for h in sorted((h for h in historico if h.tipo == "proposta" and h.numero), key=lambda h: h.data, reverse=True):
            versoes_proposta.setdefault(h.numero, []).append(h)
        for versoes in versoes_proposta.values():
            for mais_nova, antiga in zip(versoes, versoes[1:]):
                proposta_substituida_em[antiga.id] = mais_nova.data
        # Credito pendente do cliente, por produto, em R$ (nunca em tonelada --
        # o preco muda de uma negociacao pra outra, o dinheiro que o cliente ja
        # pagou nao). Somado caso existam varios pedidos com sobra do mesmo
        # produto ao longo do tempo.
        for pd in pedidos:
            if pd.saldo_credito_valor:
                creditos_por_produto[pd.produto] = creditos_por_produto.get(pd.produto, 0) + pd.saldo_credito_valor

    # Resumo/proxima acao no topo da ficha (prototipo de redesign, Theo,
    # 2026-09-24): mesma priorizacao da fila de trabalho da home do vendedor
    # (ver _motivo_prioritario), so que calculada pra UM cliente so, pra dar
    # o mesmo sinal de "por que esse cliente precisa de atencao" direto na
    # ficha -- sem precisar voltar pra home pra lembrar o motivo.
    motivo_prioritario = None
    if cliente:
        pedidos_vencidos_deste = [pd for pd in pedidos if pd.vencido()]
        pedidos_abertos_deste = [pd for pd in pedidos if pd.status == STATUS_PEDIDO_ABERTO]
        mes_atual = dt.datetime.utcnow().month
        mes_nome = MESES_PT[mes_atual - 1]
        motivo = _motivo_prioritario(
            cliente, {cliente.id: pedidos_vencidos_deste} if pedidos_vencidos_deste else {},
            {cliente.id: pedidos_abertos_deste} if pedidos_abertos_deste else {},
            {cliente.id: propostas_abertas} if propostas_abertas else {}, mes_atual, mes_nome,
            dias_para_virada_ciclo(), oportunidade=_oportunidades_abertas(db, [cliente.id]).get(cliente.id))
        if motivo:
            tier, texto, aba_acao, _ordem, _etiqueta = motivo
            motivo_prioritario = {"tier": tier, "texto": texto, "aba": aba_acao, "cor": COR_TIER.get(tier, "mineral")}

    historico_pinned = historico[:historico_fixados]
    historico_atual, historico_grupos = _agrupar_historico_por_periodo(historico[historico_fixados:])

    recebimentos = (sorted(cliente.recebimentos_indiretos, key=lambda d: d.criado_em, reverse=True)
                    if cliente else [])
    ids_relacionados = {h.cliente_relacionado_id for h in historico if h.cliente_relacionado_id}
    clientes_relacionados = ({c.id: c for c in db.query(ClienteCRM).filter(ClienteCRM.id.in_(ids_relacionados))}
                             if ids_relacionados else {})

    contexto = {
        "user": user, "cliente": cliente, "historico": historico, "historico_fixados": historico_fixados,
        "historico_pinned": historico_pinned, "historico_atual": historico_atual, "historico_grupos": historico_grupos,
        "fase_label": FASE_LABEL, "fase_cor": FASE_COR, "resultados": RESULTADO_CONTATO,
        "voltar_url": (f"/crm/estado/{cliente.uf}/fase/{cliente.fase}" if cliente and user.role != "vendedor" else "/vendedor/crm/carteira"),
        "produtos": PRODUTOS, "propostas_abertas": propostas_abertas, "produtos_disponiveis": produtos_disponiveis,
        "pedidos": pedidos, "propostas_por_numero": propostas_por_numero, "proposta_substituida_em": proposta_substituida_em, "creditos_por_produto": creditos_por_produto,
        "erro": erro, "aba_inicial": aba, "motivo_prioritario": motivo_prioritario,
        "motivos_perdido": MOTIVO_PERDIDO, "temperaturas": TEMPERATURA_LABEL, "resultado_cor": RESULTADO_COR,
        "plano_safra_modalidades": PLANO_SAFRA_MODALIDADE,
        "parceiros_cadastrados": db.query(ParceiroCessao).filter_by(ativo=True).order_by(ParceiroCessao.nome).all(),
        "destinos_cancelados": ({d.pedido.numero for d in cliente.recebimentos_indiretos
                                 if d.pedido.status == STATUS_PEDIDO_CANCELADO} if cliente else set()),
        "clientes_relacionados": clientes_relacionados,
        "pedidos_por_numero": {p.numero: p for p in pedidos},
        "recebimentos": recebimentos,
        "orientacao": _orientacao_contato(cliente, pedidos, propostas_abertas, recebimentos) if cliente else None,
        "form_contato": _formulario_contato(cliente, propostas_abertas, _acesso_limitado(user, cliente)) if cliente else None,
        "acesso_limitado": _acesso_limitado(user, cliente),
        "via": via,
        "dias_ultimo_contato": _dias_ultimo_contato_real(historico),
        "motivos_sem_interesse": MOTIVOS_SEM_INTERESSE, "retorno_por_clima": RETORNO_POR_CLIMA,
        "motivos_contato_invalido": MOTIVOS_CONTATO_INVALIDO, "tentativas_ajuda": config.valor("contato_tentativas"),
        "contato_automatico": config.ligada("liga_contato_automatico"),
        "migalhas": _migalhas_ate_cliente(user, cliente, via=via) if cliente else [],
        # Caminho da venda (topo da ficha) + o que o Gerar pedido, que abre
        # dentro dele, precisa (Rafael, 2026-10-02: nada de pagina separada)
        "caminho_venda": _caminho_venda(cliente, historico, propostas_abertas, pedidos) if cliente else None,
        "subsidiarias_por_produto": {p.produto: SUBSIDIARIAS.get(p.produto, []) for p in propostas_abertas},
        # Pedido em andamento do mesmo produto: o Gerar pedido pede pra confirmar
        # se e compra a mais ou renegociacao (Rafael, 2026-10-02 -- evitar contar
        # a mesma venda duas vezes)
        "pedidos_ativos_por_produto": _pedidos_ativos_por_produto(pedidos),
        "gerar_pedido_aberto": None, "gerar_pedido_valores": {},
    }
    contexto.update(extra or {})
    return contexto


def _erro_ficha(request, db, user, cliente, erro, aba="dados", extra=None):
    via = _via_ficha(request, cliente.id) if cliente else None
    return _templates(request).TemplateResponse(request, "crm_ficha_cliente.html",
        _contexto_ficha(db, user, cliente, erro=erro, aba=aba, via=via, extra=extra))


# a rota /crm/cliente/novo tem que vir ANTES de /crm/cliente/{cliente_id} (int) --
# senao o FastAPI tenta casar "novo" com o path param inteiro e da erro 422.
@router.get("/crm/cliente/novo", response_class=HTMLResponse)
def crm_novo_cliente_form(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v})
    return _templates(request).TemplateResponse(request, "crm_novo_cliente.html", {
        "user": user, "vendedores": vendedores, "estados": ESTADOS_OPERACAO,
        "municipios_por_uf": MUNICIPIOS_POR_UF,
        "erro": None, "similares": None, "form": {},
        "migalhas": _migalhas_novo_cliente(user),
    })


@router.post("/crm/cliente/novo")
def crm_novo_cliente(request: Request, fazenda: str = Form(...), cidade: str = Form(...), uf: str = Form(...),
                      localidade: str = Form(""),
                      proprietario: str = Form(...), telefone: str = Form(...), email: str = Form(""),
                      cnpj: str = Form(""), empresa: str = Form(""), area_plantada_ha: str = Form(""),
                      frota_propria: str = Form(""), coordenadas: str = Form(""),
                      e_consultor: str = Form(""), e_transportadora: str = Form(""),
                      vendedor: str = Form(""), confirmar_similar: str = Form(""),
                      user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    vendedor_nome = user.vendedor_nome if user.role == "vendedor" else (vendedor.strip() or None)
    vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v})
    form = {"fazenda": fazenda, "cidade": cidade, "uf": uf, "localidade": localidade, "proprietario": proprietario,
            "telefone": telefone, "email": email, "cnpj": cnpj, "empresa": empresa,
            "area_plantada_ha": area_plantada_ha, "frota_propria": frota_propria, "coordenadas": coordenadas,
            "e_consultor": bool(e_consultor), "e_transportadora": bool(e_transportadora),
            "vendedor": vendedor_nome or ""}

    def _erro(msg, similares=None):
        return _templates(request).TemplateResponse(request, "crm_novo_cliente.html", {
            "user": user, "vendedores": vendedores, "estados": ESTADOS_OPERACAO,
            "municipios_por_uf": MUNICIPIOS_POR_UF,
            "erro": msg, "similares": similares, "form": form,
            "migalhas": _migalhas_novo_cliente(user),
        })

    if not fazenda.strip() or not cidade.strip() or not uf.strip() or not proprietario.strip():
        return _erro("Preencha nome da fazenda, cidade, estado e proprietário.")
    if uf.strip().upper() not in ESTADOS_OPERACAO:
        return _erro("Selecione um estado válido.")
    if cidade.strip() not in MUNICIPIOS_POR_UF.get(uf.strip().upper(), []):
        return _erro("Selecione uma cidade válida da lista (escolha o estado primeiro).")

    erro = validar_telefone(telefone)
    if erro:
        return _erro(erro)
    coordenadas, erro = _coordenadas_do_form(coordenadas)
    if erro:
        return _erro(erro)
    dup = encontrar_telefone_duplicado(db, telefone)
    if dup:
        return _erro(f"Esse telefone já está cadastrado para {dup['cliente_nome']}"
                     + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                     + " — confira se não é o mesmo cliente antes de cadastrar de novo.")
    dup = encontrar_email_duplicado(db, email)
    if dup:
        return _erro(f"Esse e-mail já está cadastrado para {dup['cliente_nome']}"
                     + " — confira se não é o mesmo cliente antes de cadastrar de novo.")
    dup = encontrar_cnpj_duplicado(db, cnpj)
    if dup:
        return _erro(f"Esse CNPJ/CPF já está cadastrado para {dup['cliente_nome']}"
                     + " — confira se não é o mesmo cliente antes de cadastrar de novo.")

    if not confirmar_similar:
        similares = encontrar_clientes_similares(db, fazenda, vendedor_nome=vendedor_nome)
        if similares:
            return _erro(None, similares=similares)

    cliente = ClienteCRM(
        fazenda=fazenda.strip(), proprietario=proprietario.strip(), telefone=telefone.strip(),
        email=email.strip() or None, cnpj=cnpj.strip() or None, empresa=empresa.strip() or None,
        cidade=cidade.strip(), localidade=localidade.strip() or None, uf=uf.strip().upper(),
        frota_propria={"sim": True, "nao": False}.get(frota_propria.strip().lower()),
        coordenadas=coordenadas, fase="a_contactar", vendedor_nome=vendedor_nome,
        e_consultor=bool(e_consultor), e_transportadora=bool(e_transportadora),
    )
    try:
        cliente.area_plantada_ha = float(area_plantada_ha) if area_plantada_ha.strip() else None
    except ValueError:
        cliente.area_plantada_ha = None
    db.add(cliente)
    db.flush()
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", texto="Cliente cadastrado.", autor=user.nome_completo))
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    if vendedor_nome and user.vendedor_nome != vendedor_nome:
        _avisar(db, vendedor_nome, cliente, "cliente_novo", "Novo cliente na sua carteira",
                f"{cliente.fazenda} ({cliente.cidade}/{cliente.uf}) foi cadastrado na sua carteira por "
                f"{user.nome_completo}. Faça o primeiro contato.", user.nome_completo)
    db.commit()
    avisar_sucesso(request, f"{cliente.fazenda} cadastrado. Próximo passo: registre o primeiro contato.")
    return RedirectResponse(_url_ficha(cliente.id, "comentario", ancora="form-contato"), status_code=303)


@router.get("/crm/cliente/{cliente_id}", response_class=HTMLResponse)
def crm_ficha_cliente(request: Request, cliente_id: int, aba: str = "dados",
                       user: User = Depends(require_role("admin", "logistica", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id, permitir_via_parceiro=True)
    return _templates(request).TemplateResponse(request, "crm_ficha_cliente.html",
        _contexto_ficha(db, user, cliente, aba=aba, via=_via_ficha(request, cliente_id)))


@router.post("/crm/cliente/{cliente_id}/nota")
def crm_add_nota(request: Request, cliente_id: int, texto: str = Form(""), resultado: str = Form(None),
                  proximo_retorno: str = Form(""), origem: str = Form(""), produtos: list[str] = Form([]),
                  motivo_perdido: str = Form(""), temperatura: str = Form(""),
                  retorno: str = Form(""), motivos: list[str] = Form([]),
                  aba_retorno: str = Form("comentario"), motivo_contato: str = Form(""), pedir_ajuda: str = Form(""),
                  user: User = Depends(require_role("admin", "logistica", "vendedor")),
                  db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id, permitir_via_parceiro=True)
    if not cliente:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    if aba_retorno not in ("dados", "comentario", "propostas", "pedidos"):
        aba_retorno = "comentario"

    # "anotacao" = so uma anotacao, sem contato (resultado vazio no banco).
    # Sem resultado nenhum (atalho "+ Comentario" antigo) com texto = anotacao.
    if resultado is None:
        if not texto.strip():
            return _erro_ficha(request, db, user, cliente, "Escolha como foi o contato.", aba="comentario")
        resultado = "anotacao"
    if resultado == "anotacao":
        resultado = ""
        if not texto.strip():
            return _erro_ficha(request, db, user, cliente, "Escreva a anotação.", aba="comentario")
    form = _formulario_contato(cliente, [], _acesso_limitado(user, cliente))
    # Desfechos validos pra fase (ex: "nao atendeu" nao reabre cliente perdido);
    # os de encerrar/negocio seguem pras validacoes proprias logo abaixo.
    permitidos = {""} | {r["valor"] for r in form["resultados"]} | {"sem_interesse", "perdido", "proposta", "venda"}
    if resultado not in permitidos:
        return _erro_ficha(request, db, user, cliente, "Essa opção não vale para este cliente agora.", aba="comentario")

    if _acesso_limitado(user, cliente):
        # Cliente de outra carteira aberto pelo destino final: so registra o
        # contato (pos-venda). Etapa, temperatura, pagamento e agenda sao do
        # dono da carteira.
        if resultado not in ("", "contato", "nao_atendeu"):
            return _erro_ficha(request, db, user, cliente,
                                f"Este cliente é da carteira de {cliente.vendedor_nome} — por aqui você só registra o contato.",
                                aba="comentario")
        temperatura = proximo_retorno = retorno = ""

    if resultado == "sem_interesse" and origem == "finalizar" and not produtos:
        return _erro_ficha(request, db, user, cliente,
                            "Selecione pelo menos um produto que o cliente não vai usar.", aba="dados")
    if resultado == "contato_invalido" and motivo_contato not in MOTIVOS_CONTATO_INVALIDO:
        return _erro_ficha(request, db, user, cliente, "Escolha o que aconteceu com o contato.", aba="comentario")
    if resultado == "perdido" and motivo_perdido not in MOTIVO_PERDIDO:
        return _erro_ficha(request, db, user, cliente,
                            "Selecione o motivo da perda.", aba="dados")
    if resultado in ("proposta", "venda"):
        # Achado do Rafael (2026-09-24): deixar o vendedor "declarar" proposta
        # enviada/venda realizada so por comentario, sem produto/volume/preco
        # por tras, criava exatamente o mesmo problema das "propostas
        # fantasma" (fase diz uma coisa, nenhum PropostaCRM/PedidoCRM real
        # existe) -- so que gerando NOVOS fantasmas dai pra frente, nao so um
        # resquicio da importacao antiga. As abas Proposta/Pedido ja avancam
        # a fase sozinhas quando o negocio de verdade e registrado (ver
        # crm_nova_proposta/crm_gerar_pedido) -- esse atalho fica bloqueado
        # aqui tambem (nao so escondido no <select>) pra cobrir um POST
        # direto, nao so quem usa a tela normalmente.
        return _erro_ficha(request, db, user, cliente,
                            "Pra registrar proposta enviada ou venda realizada, use a aba Proposta "
                            "(com produto, volume, preço e pagamento) — assim o negócio de verdade "
                            "fica registrado, não só um comentário.", aba="comentario")
    if resultado in ("sem_interesse", "perdido") and cliente.fase == "realizado":
        # Rafael (2026-09-25): cliente que ja comprou nao vira "nao usara"/
        # "perdido" por um comentario -- o que acontece com ele depois e
        # tratado no reinicio de ciclo ou cancelando o pedido de verdade.
        return _erro_ficha(request, db, user, cliente,
                            "Cliente em Realizado não pode ser marcado como não usará ou perdido por aqui.",
                            aba="comentario")

    if temperatura in TEMPERATURA_LABEL and resultado in ("contato", ""):
        cliente.temperatura = temperatura
    elif resultado == "sem_interesse_agora":
        cliente.temperatura = "frio"

    # Observacao e opcional nos desfechos de contato: sem texto, o historico
    # registra uma frase curta do proprio desfecho.
    texto_padrao = {"contato": "Conversa registrada.", "nao_atendeu": "Ligação sem resposta.",
                    "retornar_depois": "Cliente pediu para retornar depois.",
                    "sem_interesse_agora": "Sem interesse no momento.",
                    "contato_invalido": "Caso enviado ao administrador para conseguir outro contato."}
    if texto.strip() or resultado in texto_padrao:
        texto_corpo = texto.strip() or texto_padrao[resultado]
        if resultado == "contato_invalido":
            texto_corpo = f"{motivo_contato}. {texto_corpo}"
        if resultado == "sem_interesse_agora":
            escolhidos = [m for m in motivos if m in MOTIVOS_SEM_INTERESSE]
            if escolhidos:
                texto_corpo = f"Motivo: {', '.join(escolhidos)}. {texto_corpo}"
        if produtos:
            texto_corpo = f"Não irá usar: {', '.join(produtos)}. {texto_corpo}"
        if resultado == "perdido":
            texto_corpo = f"Motivo: {MOTIVO_PERDIDO[motivo_perdido]}. {texto_corpo}"
        # O resultado ("Fiz contato", "Enviei uma proposta" etc) fica num
        # campo proprio (nao mais colado como "[prefixo]" dentro do texto) --
        # Rafael achou o texto bruto feio (2026-09-24), o template agora
        # renderiza isso como badge colorido, no mesmo espirito dos cards de
        # proposta/pedido, em vez de texto corrido.
        nota = ContatoCRM(cliente_id=cliente.id, tipo="nota", texto=texto_corpo,
                          resultado=resultado or None, autor=user.nome_completo)
        db.add(nota)

        nova_fase = calcular_avanco_fase(cliente.fase, resultado)
        if not nova_fase and cliente.fase == "a_contactar" and resultado != "contato_invalido":
            # Qualquer comentario registrado (mesmo sem marcar um "resultado"
            # especifico) ja e prova de que o cliente foi contactado -- nao
            # faz sentido exigir o vendedor selecionar isso explicitamente
            # todo santo registro so pra sair de "a contactar".
            nova_fase = "contactado"
        if nova_fase:
            fase_origem = cliente.fase
            de_para = f"{FASE_LABEL.get(fase_origem, fase_origem)} -> {FASE_LABEL.get(nova_fase, nova_fase)}"
            cliente.fase = nova_fase
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase", texto=f"Etapa avançou automaticamente: {de_para}",
                               fase_origem=fase_origem, fase_destino=nova_fase, autor=user.nome_completo))
            if nova_fase == "perdido":
                cliente.motivo_perdido = motivo_perdido
            else:
                # Reativado (ou movido pra outro desfecho) -- o motivo antigo
                # de perda nao se aplica mais, senao a ficha mostraria um
                # motivo desatualizado pra um cliente que voltou a ser
                # trabalhado.
                cliente.motivo_perdido = None

        # Proximo contato: o que o vendedor escolheu, senao o padrao do desfecho
        # (nao atendeu = 2 dias, sem interesse agora = 1 mes, conversa = pelo clima).
        hoje = dt.date.today()
        data_retorno = None
        if retorno == "data" or (not retorno and proximo_retorno):
            data_retorno = dt.datetime.strptime(proximo_retorno, "%Y-%m-%d").date() if proximo_retorno else None
        elif retorno == "epoca":
            data_retorno = _data_epoca_compra(cliente, hoje)
        elif retorno in ("2", "7", "15", "30"):
            data_retorno = hoje + dt.timedelta(days=int(retorno))
        elif not _acesso_limitado(user, cliente):
            opcao = next((r for r in form["resultados"] if r["valor"] == resultado), None)
            dias = opcao["retorno"] if opcao else None
            if resultado == "contato" and form["temperatura"]:
                dias = RETORNO_POR_CLIMA.get(temperatura, dias)
            data_retorno = hoje + dt.timedelta(days=dias) if dias else None
        if data_retorno:
            cliente.proximo_retorno_em = data_retorno
            texto_agenda = ("Reavaliar cliente perdido em " if resultado == "perdido" else "Novo contato agendado para ")
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="agenda",
                               texto=f"{texto_agenda}{cliente.proximo_retorno_em.strftime('%d/%m/%Y')}.", autor=user.nome_completo))

        # Contato com problema vai pro administrador (Rafael, 2026-10-02): numero
        # errado / sem WhatsApp na hora; "nao atendeu" quando o vendedor pede
        # ajuda ou na 3a tentativa seguida sem resposta (todo "nao atendeu"
        # inundaria a fila do admin). Conversa de verdade encerra o caso.
        ajuda_texto = ""
        if resultado == "contato_invalido":
            cliente.precisa_ajuda = True
            cliente.motivo_ajuda = motivo_contato + (f" — {texto.strip()}" if texto.strip() else "")
            cliente.proximo_retorno_em = None
            ajuda_texto = " O caso foi para o administrador conseguir outro número ou meio de contato."
        elif resultado == "nao_atendeu":
            seguidas = 1
            for h in db.query(ContatoCRM).filter(ContatoCRM.cliente_id == cliente.id, ContatoCRM.tipo == "nota",
                                                 ContatoCRM.resultado.isnot(None), ContatoCRM.id != nota.id)                                          .order_by(ContatoCRM.data.desc()).all():
                if h.resultado != "nao_atendeu":
                    break
                seguidas += 1
            if pedir_ajuda or (config.ligada("liga_contato_automatico") and seguidas >= config.valor("contato_tentativas")):
                cliente.precisa_ajuda = True
                cliente.motivo_ajuda = (f"Cliente não retorna: {seguidas} tentativa{'s' if seguidas != 1 else ''} sem resposta"
                                        + (" (vendedor pediu ajuda)" if pedir_ajuda else ""))
                ajuda_texto = f" {seguidas}ª tentativa sem resposta: o caso foi para o administrador."
                db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", autor=user.nome_completo,
                                   texto=f"Enviado ao administrador: {cliente.motivo_ajuda}."))
        elif resultado == "contato" and cliente.precisa_ajuda:
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", autor=user.nome_completo,
                               texto=f"Contato voltou a funcionar (conversa registrada): caso encerrado. Era: {cliente.motivo_ajuda}."))
            cliente.precisa_ajuda = False
            cliente.motivo_ajuda = None

        cliente.ultima_interacao_em = dt.datetime.utcnow()
        db.commit()

        via = _via_ficha(request, cliente.id)
        if resultado in ("sem_interesse", "perdido"):
            aviso = f"Cliente marcado como {FASE_LABEL.get(cliente.fase, cliente.fase)}."
        elif resultado:
            aviso = f"Contato registrado: {RESULTADO_CONTATO.get(resultado, resultado)}."
        else:
            aviso = "Anotação salva."
        if data_retorno:
            aviso += (" Reavaliação em " if resultado == "perdido" else " Próximo contato em ") + data_retorno.strftime("%d/%m/%Y") + "."
        aviso += ajuda_texto
        link, rotulo = _seguir_lista(db, user, via, cliente.id)
        avisar_sucesso(request, aviso, link, rotulo)
        return RedirectResponse(_url_ficha(cliente_id, aba_retorno, via, f"hist-{nota.id}"), status_code=303)
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba={aba_retorno}", status_code=303)


@router.post("/crm/cliente/{cliente_id}/contato-resolvido")
def crm_contato_resolvido(request: Request, cliente_id: int, telefone: str = Form(""), observacao: str = Form(""),
                          user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """O administrador conseguiu outro numero ou meio de contato pro vendedor
    (Rafael, 2026-10-02). Encerra o caso, avisa o vendedor e poe o cliente na
    agenda dele de hoje."""
    cliente = db.get(ClienteCRM, cliente_id)
    if not cliente or not cliente.precisa_ajuda:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    obs = " ".join(observacao.split())
    if not obs:
        return _erro_ficha(request, db, user, cliente,
                            "Escreva para o vendedor o que foi resolvido (novo contato, com quem falar...).", aba="dados")
    novo_tel = telefone.strip()
    texto_tel = ""
    if novo_tel:
        erro = validar_telefone(novo_tel)
        if erro:
            return _erro_ficha(request, db, user, cliente, erro, aba="dados")
        texto_tel = f" Telefone trocado de {cliente.telefone or '—'} para {novo_tel}."
        cliente.telefone = novo_tel
    motivo = cliente.motivo_ajuda or "contato não funcionava"
    cliente.precisa_ajuda = False
    cliente.motivo_ajuda = None
    hoje = dt.date.today()
    cliente.proximo_retorno_em = hoje
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", autor=user.nome_completo,
                       texto=f"Contato resolvido pelo administrador (era: {motivo}).{texto_tel} Para o vendedor: {obs}"))
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="agenda", autor=user.nome_completo,
                       texto=f"Novo contato agendado para {hoje.strftime('%d/%m/%Y')}."))
    _avisar(db, cliente.vendedor_nome, cliente, "contato_resolvido", f"Novo contato: {cliente.fazenda}",
            (f"Novo telefone: {novo_tel}. " if novo_tel else "") + obs, user.nome_completo)
    db.commit()
    via = _via_ficha(request, cliente.id)
    link, rotulo = _seguir_lista(db, user, via, cliente.id)
    avisar_sucesso(request, f"Caso resolvido. {cliente.vendedor_nome or 'O vendedor'} foi avisado e o cliente entrou "
                            f"na agenda de hoje.", link, rotulo)
    return RedirectResponse(_url_ficha(cliente_id, "dados", via, "dados-cliente"), status_code=303)


# ---------- cadastro do cliente ----------

@router.get("/crm/cliente/{cliente_id}/editar", response_class=HTMLResponse)
def crm_editar_cliente_form(request: Request, cliente_id: int,
                             user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    migalhas = _migalhas_ate_cliente(user, cliente, cliente_e_leaf=False, via=_via_ficha(request, cliente.id)) + [("Editar cadastro", None)] if cliente else []
    return _templates(request).TemplateResponse(request, "crm_editar_cliente.html", {
        "user": user, "cliente": cliente, "erro": None, "migalhas": migalhas,
        "municipios": MUNICIPIOS_POR_UF.get(cliente.uf, []) if cliente else [],
    })


@router.post("/crm/cliente/{cliente_id}/editar")
def crm_editar_cliente(request: Request, cliente_id: int, proprietario: str = Form(""), telefone: str = Form(""),
                        email: str = Form(""), cnpj: str = Form(""), empresa: str = Form(""),
                        cidade: str = Form(""), localidade: str = Form(""), frota_propria: str = Form(""),
                        area_plantada_ha: str = Form(""), coordenadas: str = Form(""),
                        e_consultor: str = Form(""), e_transportadora: str = Form(""),
                        user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if cliente:
        telefone = telefone.strip()
        erro = None
        if not proprietario.strip() or not telefone or not cidade.strip():
            erro = "Proprietário/contato, telefone e cidade são obrigatórios."
        elif cidade.strip() not in MUNICIPIOS_POR_UF.get(cliente.uf, []):
            erro = "Selecione uma cidade válida da lista (mesma lista oficial usada no cadastro novo)."
        else:
            erro = validar_telefone(telefone)
            if not erro:
                dup = encontrar_telefone_duplicado(db, telefone, excluir_cliente_id=cliente.id)
                if dup:
                    erro = (f"Esse telefone já está cadastrado para {dup['cliente_nome']}"
                            + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                            + " — confira se não é o mesmo cliente duplicado.")
            if not erro:
                dup = encontrar_email_duplicado(db, email, excluir_cliente_id=cliente.id)
                if dup:
                    erro = f"Esse e-mail já está cadastrado para {dup['cliente_nome']} — confira se não é o mesmo cliente duplicado."
            if not erro:
                dup = encontrar_cnpj_duplicado(db, cnpj, excluir_cliente_id=cliente.id)
                if dup:
                    erro = f"Esse CNPJ/CPF já está cadastrado para {dup['cliente_nome']} — confira se não é o mesmo cliente duplicado."
            if not erro:
                coordenadas_lidas, erro = _coordenadas_do_form(coordenadas)
        if erro:
            migalhas = _migalhas_ate_cliente(user, cliente, cliente_e_leaf=False, via=_via_ficha(request, cliente.id)) + [("Editar cadastro", None)]
            return _templates(request).TemplateResponse(request, "crm_editar_cliente.html", {
                "user": user, "cliente": cliente, "erro": erro, "migalhas": migalhas,
                "municipios": MUNICIPIOS_POR_UF.get(cliente.uf, []), "coordenadas_digitadas": coordenadas,
            })
        cliente.proprietario = proprietario.strip()
        cliente.telefone = telefone
        cliente.email = email.strip() or None
        cliente.cnpj = cnpj.strip() or None
        cliente.empresa = empresa.strip() or None
        cliente.cidade = cidade.strip()
        cliente.localidade = localidade.strip() or None
        cliente.frota_propria = {"sim": True, "nao": False}.get(frota_propria.strip().lower())
        cliente.e_consultor = bool(e_consultor)
        cliente.e_transportadora = bool(e_transportadora)
        try:
            cliente.area_plantada_ha = float(area_plantada_ha) if area_plantada_ha.strip() else None
        except ValueError:
            pass
        cliente.coordenadas = coordenadas_lidas
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", texto="Dados do cliente atualizados.", autor=user.nome_completo))
        cliente.ultima_interacao_em = dt.datetime.utcnow()
        db.commit()
        via = _via_ficha(request, cliente.id)
        link, rotulo = _seguir_lista(db, user, via, cliente.id)
        avisar_sucesso(request, "Cadastro atualizado.", link, rotulo)
        return RedirectResponse(_url_ficha(cliente_id, "dados", via, "dados-cliente"), status_code=303)
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=dados", status_code=303)


@router.post("/crm/cliente/{cliente_id}/contato-adicional")
def crm_add_contato_adicional(request: Request, cliente_id: int, nome: str = Form(...), funcao: str = Form(...),
                               telefone: str = Form(...),
                               user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if not cliente:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    if not nome.strip() or not funcao.strip():
        return _erro_ficha(request, db, user, cliente, "Preencha nome e função do contato.", aba="dados")
    erro = validar_telefone(telefone)
    if not erro:
        dup = encontrar_telefone_duplicado(db, telefone)
        if dup:
            erro = (f"Esse telefone já está cadastrado para {dup['cliente_nome']}"
                    + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                    + " — confira se não é o mesmo cliente duplicado.")
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="dados")

    db.add(ContatoAdicionalCRM(cliente_id=cliente.id, nome=nome.strip(), funcao=funcao.strip(), telefone=telefone.strip()))
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados",
                       texto=f"Contato adicional cadastrado: {nome.strip()} ({funcao.strip()}) - {telefone.strip()}.",
                       autor=user.nome_completo))
    db.commit()
    avisar_sucesso(request, f"Contato adicional salvo: {nome.strip()}.")
    return RedirectResponse(_url_ficha(cliente_id, "dados", _via_ficha(request, cliente.id), "contatos-adicionais"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/contato-adicional/{contato_id}/excluir")
def crm_excluir_contato_adicional(request: Request, cliente_id: int, contato_id: int,
                                   user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if cliente:
        contato = db.get(ContatoAdicionalCRM, contato_id)
        if contato and contato.cliente_id == cliente.id:
            db.delete(contato)
            db.commit()
            avisar_sucesso(request, "Contato adicional removido.")
    return RedirectResponse(_url_ficha(cliente_id, "dados", _via_ficha(request, cliente_id), "contatos-adicionais"), status_code=303)


# ---------- proposta / pedido ----------

def _erro_parceiro_sem_cnpj(db, nome):
    """Parceiro sem CNPJ nao entra em contrato novo (Rafael, 2026-10-04: "bloqueia").
    -> frase de erro ou None."""
    parceiro = db.query(ParceiroCessao).filter_by(nome=(nome or "").strip()).first()
    if parceiro is not None and not parceiro.cnpj:
        return (f"O parceiro {parceiro.nome} está sem CNPJ e não pode entrar em contrato novo. "
                "O administrador completa o CNPJ em Parceiros; depois é só tentar de novo.")
    return None


def _validar_proposta_form(db, produto, volume, preco, pagamento, prazo, plano_safra_modalidade="", plano_safra_parceiro="",
                           parceiro_atual=None):
    """`parceiro_atual`: na renegociacao de um pedido que ja existe, manter o
    mesmo parceiro nao e contrato novo -- nao exige o CNPJ."""
    if produto not in PRODUTOS:
        return "Selecione um produto válido."
    try:
        volume_f = float(volume)
        preco_f = float(preco)
    except ValueError:
        return "Preencha volume e preço com números válidos."
    if volume_f <= 0 or preco_f <= 0:
        return "Volume e preço devem ser maiores que zero."
    if pagamento not in ("A vista", "A prazo", "Plano safra"):
        return "Informe a forma de pagamento."
    if pagamento == "A prazo" and not prazo.strip():
        return "Informe qual é o prazo."
    if pagamento == "Plano safra":
        if plano_safra_modalidade not in PLANO_SAFRA_MODALIDADE:
            return "Selecione a modalidade do plano safra (direto ou cessão de crédito)."
        if plano_safra_modalidade == "cessao":
            if not plano_safra_parceiro.strip():
                return "Selecione qual parceiro vai fazer a cessão de crédito."
            # Rafael (2026-09-25): "o parceiro da cessao tem que estar
            # cadastrado no nosso banco de dados" -- nao aceita mais nome
            # digitado na hora, so parceiro registrado e ativo em ParceiroCessao.
            if not db.query(ParceiroCessao).filter_by(nome=plano_safra_parceiro.strip(), ativo=True).first():
                return "Esse parceiro não está cadastrado (ou está inativo). Escolha \"+ Cadastrar parceiro\" na lista para cadastrar ali mesmo."
            if plano_safra_parceiro.strip() != (parceiro_atual or ""):
                erro_cnpj = _erro_parceiro_sem_cnpj(db, plano_safra_parceiro)
                if erro_cnpj:
                    return erro_cnpj
    return None


@router.post("/crm/cliente/{cliente_id}/proposta/nova")
def crm_nova_proposta(request: Request, cliente_id: int, produto: str = Form(...), volume: str = Form(...),
                       preco: str = Form(...), pagamento: str = Form(...), prazo: str = Form(""),
                       plano_safra_modalidade: str = Form(""), plano_safra_parceiro: str = Form(""),
                       observacoes: str = Form(""), usar_credito: str = Form(""),
                       user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if not cliente:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(db, produto, volume, preco, pagamento, prazo, plano_safra_modalidade, plano_safra_parceiro)
    if not erro:
        estado = _estado_produto(db, cliente.id, produto)
        if estado["situacao"] != "livre":
            erro = f"Já existe uma proposta de {produto} em aberto (no {estado['proposta'].numero}) para este cliente."

    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    credito_valor, _ = _credito_disponivel(db, cliente.id, produto)
    usa_credito_final, volume_final, erro = _dividir_volume_com_credito(
        usar_credito, credito_valor, float(volume), float(preco))
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    numero = _proximo_numero(db, PropostaCRM, PROPOSTA_NUMERO_BASE)
    proposta = PropostaCRM(
        numero=numero, cliente_id=cliente.id, produto=produto, volume=volume_final, preco=float(preco),
        pagamento=pagamento, prazo=prazo.strip() if pagamento == "A prazo" else None,
        plano_safra_modalidade=plano_safra_modalidade if pagamento == "Plano safra" else None,
        plano_safra_parceiro=(plano_safra_parceiro.strip() or None) if (pagamento == "Plano safra" and plano_safra_modalidade == "cessao") else None,
        observacoes=observacoes.strip() or None, usa_credito=usa_credito_final,
    )
    db.add(proposta)

    if cliente.fase != "proposta" and fase_e_avanco(cliente.fase, "proposta"):
        de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL['proposta']}"
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                           texto=f"Etapa avançou automaticamente: {de_para}",
                           fase_origem=cliente.fase, fase_destino="proposta", autor=user.nome_completo))
        cliente.fase = "proposta"
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    db.flush()
    credito_texto = ""
    if usa_credito_final:
        credito_ton = credito_valor / float(preco)
        credito_texto = (f" Inclui crédito de R$ {credito_valor:.2f} (equivalente a {credito_ton:.1f}t ao preço "
                          f"desta proposta) — volume total a entregar: {volume_final + credito_ton:.1f}t.")
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="proposta",
                       texto=f"Proposta no {numero} registrada: {produto} {volume_final:g}t a R$ {float(preco):.2f}/t.{credito_texto}",
                       numero=numero, produto=produto, volume=volume_final, preco=float(preco), pagamento=pagamento,
                       autor=user.nome_completo))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    avisar_sucesso(request, f"Proposta nº {numero} registrada: {produto}, {volume_final:g} t. "
                            "Quando o cliente aceitar, use \"Gerar pedido\" nela.")
    return RedirectResponse(_url_ficha(cliente_id, "propostas", _via_ficha(request, cliente.id), f"proposta-{proposta.id}"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/proposta/{proposta_id}/editar")
def crm_editar_proposta(request: Request, cliente_id: int, proposta_id: int, volume: str = Form(...), preco: str = Form(...),
                         pagamento: str = Form(...), prazo: str = Form(""),
                         plano_safra_modalidade: str = Form(""), plano_safra_parceiro: str = Form(""),
                         observacoes: str = Form(""), usar_credito: str = Form(""),
                         user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(db, proposta.produto, volume, preco, pagamento, prazo, plano_safra_modalidade, plano_safra_parceiro)
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    credito_valor, _ = _credito_disponivel(db, cliente.id, proposta.produto)
    usa_credito_final, volume_final, erro = _dividir_volume_com_credito(
        usar_credito, credito_valor, float(volume), float(preco))
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    proposta.volume = volume_final
    proposta.preco = float(preco)
    proposta.pagamento = pagamento
    proposta.prazo = prazo.strip() if pagamento == "A prazo" else None
    proposta.plano_safra_modalidade = plano_safra_modalidade if pagamento == "Plano safra" else None
    proposta.plano_safra_parceiro = (plano_safra_parceiro.strip() or None) if (pagamento == "Plano safra" and plano_safra_modalidade == "cessao") else None
    proposta.observacoes = observacoes.strip() or None
    proposta.usa_credito = usa_credito_final
    credito_texto = ""
    if usa_credito_final:
        credito_ton = credito_valor / float(preco)
        credito_texto = (f" Inclui crédito de R$ {credito_valor:.2f} (equivalente a {credito_ton:.1f}t ao preço "
                          f"desta proposta) — volume total a entregar: {volume_final + credito_ton:.1f}t.")
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="proposta",
                       texto=f"Proposta no {proposta.numero} atualizada: {proposta.produto} {volume_final:g}t a R$ {float(preco):.2f}/t.{credito_texto}",
                       numero=proposta.numero, produto=proposta.produto, volume=proposta.volume,
                       preco=proposta.preco, pagamento=proposta.pagamento, autor=user.nome_completo))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    avisar_sucesso(request, f"Proposta nº {proposta.numero} atualizada.")
    return RedirectResponse(_url_ficha(cliente_id, "propostas", _via_ficha(request, cliente.id), f"proposta-{proposta.id}"), status_code=303)


def _pedidos_ativos_por_produto(pedidos):
    ativos = {}
    for pd in pedidos:
        if pd.status == STATUS_PEDIDO_ABERTO:
            ativos.setdefault(pd.produto, []).append(pd)
    return ativos


def _ler_comprovante(arquivo):
    """Le o comprovante enviado. Devolve (conteudo, erro)."""
    if not (arquivo and arquivo.filename):
        return None, "Escolha o arquivo do comprovante de pagamento."
    if os.path.splitext(arquivo.filename)[1].lower() not in EXTENSOES_COMPROVANTE:
        return None, "O comprovante precisa ser um PDF ou uma foto/print (jpg, jpeg ou png)."
    conteudo = arquivo.file.read()
    if not conteudo:
        return None, "O arquivo do comprovante está vazio."
    if len(conteudo) > TAMANHO_MAX_COMPROVANTE:
        return None, "O comprovante passa de 10 MB. Envie um print ou uma foto menor."
    return conteudo, None


def _guardar_comprovante(db, pedido, nome_original, conteudo, user):
    """Grava o comprovante como pendente. Quem libera o carregamento e o
    financeiro (ou admin), ao confirmar -- ver financeiro_routes."""
    extensao = os.path.splitext(nome_original)[1].lower()
    nome_arquivo = f"pedido_{pedido.codigo}_{uuid.uuid4().hex}{extensao}"
    with open(os.path.join(COMPROVANTES_PAGAMENTO_DIR, nome_arquivo), "wb") as f:
        f.write(conteudo)
    db.add(PedidoComprovante(pedido=pedido, arquivo=nome_arquivo, nome_original=nome_original[:200],
                             autor=user.nome_completo))


@router.get("/crm/cliente/{cliente_id}/proposta/{proposta_id}/gerar-pedido", response_class=HTMLResponse)
def crm_gerar_pedido_form(request: Request, cliente_id: int, proposta_id: int,
                           user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    # Gerar pedido nao e mais pagina separada (Rafael, 2026-10-02): abre dentro
    # do caminho da venda, na ficha. Link antigo cai la com o formulario aberto.
    via = _via_ficha(request, cliente.id)
    return RedirectResponse(f"/crm/cliente/{cliente_id}?abrir=gp-{proposta.id}" + (f"&via={via}" if via else ""),
                            status_code=303)


@router.post("/crm/cliente/{cliente_id}/proposta/{proposta_id}/gerar-pedido")
def crm_gerar_pedido(request: Request, cliente_id: int, proposta_id: int, subsidiaria_cnpj: str = Form(""),
                      faturamento_tipo: str = Form(""), fat_nome: str = Form(""), fat_documento: str = Form(""),
                      fat_telefone: str = Form(""), fat_endereco: str = Form(""), condicao_pagamento: str = Form(""),
                      data_limite_retirada: str = Form(""), contrato_cessao: UploadFile = File(None),
                      comprovante_pagamento: UploadFile = File(None), forma_prazo: str = Form(""),
                      prazo_parcelas: str = Form(""), prazo_periodo_dias: str = Form(""),
                      vencimento_pagamento: str = Form(""), confirma_adicional: str = Form(""),
                      user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    subsidiarias = SUBSIDIARIAS.get(proposta.produto, [])
    subsidiaria = next((s for s in subsidiarias if s["cnpj"] == subsidiaria_cnpj), None)
    erro = None
    # Contrato novo de cessao: parceiro sem CNPJ bloqueia antes de qualquer outra
    # conferencia (vale tambem pra proposta feita antes do bloqueio)
    erro_cnpj = (_erro_parceiro_sem_cnpj(db, proposta.plano_safra_parceiro)
                 if proposta.pagamento == "Plano safra" and proposta.plano_safra_modalidade == "cessao" else None)
    # Ja existe pedido do mesmo produto em andamento? Pode ser compra a mais
    # (legitimo) ou a mesma venda renegociada -- essa precisa ir pro "Renegociar"
    # do pedido existente, senao conta como duas vendas (Rafael, 2026-10-02).
    em_andamento = (db.query(PedidoCRM)
                      .filter_by(cliente_id=cliente.id, produto=proposta.produto, status=STATUS_PEDIDO_ABERTO)
                      .order_by(PedidoCRM.id).all())
    if em_andamento and confirma_adicional != "adicional":
        numeros = ", ".join(p.codigo for p in em_andamento)
        erro = (f"Este cliente já tem pedido de {proposta.produto} em andamento ({numeros}). Confirme se é uma compra "
                f"a mais ou, se for a mesma venda com nova negociação, renegocie o pedido que já existe.")
    data_limite_d = None
    if subsidiarias and not subsidiaria:
        erro = "Selecione a subsidiária que vai faturar o pedido."
    elif faturamento_tipo == "cliente":
        pass
    elif faturamento_tipo == "terceiro":
        if not fat_nome.strip() or not fat_documento.strip():
            erro = "Preencha nome e CPF/CNPJ para faturamento em nome de terceiro."
    else:
        erro = "Selecione para quem será faturado o pedido."

    # Opcional (Rafael, 2026-09-25) -- sem ela o pedido so nao entra no aviso
    # de "vencido sem retirada".
    if not erro and data_limite_retirada.strip():
        try:
            data_limite_d = dt.datetime.strptime(data_limite_retirada.strip(), "%Y-%m-%d").date()
        except ValueError:
            erro = "Data limite pra retirada inválida."
        else:
            if data_limite_d < dt.date.today():
                erro = "A data limite pra retirada não pode ser no passado."

    # Rafael (2026-09-25): "para avancar nessa modalidade pra gerar pedido e
    # obrigatorio o vendedor anexar o contrato de cessao de credito assinado
    # pelas 3 partes" -- so exigido nesse caso especifico, o resto do fluxo
    # de gerar pedido continua igual.
    exige_contrato = proposta.pagamento == "Plano safra" and proposta.plano_safra_modalidade == "cessao"
    extensoes_aceitas = (".pdf", ".jpg", ".jpeg", ".png")
    if not erro and exige_contrato:
        if not (contrato_cessao and contrato_cessao.filename):
            erro = "Anexe o contrato de cessão de crédito assinado (cliente, parceiro e nós) pra gerar este pedido."
        elif os.path.splitext(contrato_cessao.filename)[1].lower() not in extensoes_aceitas:
            erro = "O contrato precisa ser um PDF ou uma foto/scan (jpg, jpeg ou png)."

    # Pedido a vista (Rafael, 2026-10-02): o cliente paga o total e retira, e o
    # carregamento so e liberado com o comprovante. Pode anexar ja aqui ou
    # depois, no proprio pedido (o cliente costuma pagar depois de receber o
    # pedido); sem ele o pedido nasce aguardando pagamento.
    a_vista = proposta.pagamento == "A vista"
    comprovante = None
    if not erro and a_vista and comprovante_pagamento and comprovante_pagamento.filename:
        comprovante, erro = _ler_comprovante(comprovante_pagamento)

    # A prazo (Rafael, 2026-10-02): carrega direto, sem aprovacao do
    # financeiro; o que precisa ficar combinado e COMO o cliente paga.
    a_prazo = proposta.pagamento == "A prazo"
    periodo_dias = None
    if not erro and a_prazo:
        if forma_prazo not in FORMA_PRAZO:
            erro = "Escolha como o cliente vai pagar o a prazo."
        elif forma_prazo == "boleto" and prazo_parcelas not in PARCELAS_BOLETO:
            erro = "Escolha as parcelas do boleto."
        elif forma_prazo == "periodo":
            try:
                periodo_dias = int(prazo_periodo_dias)
            except ValueError:
                periodo_dias = 0
            if not 1 <= periodo_dias <= 90:
                erro = "Informe o período em dias (de 1 a 90)."

    # Plano safra (Rafael, 2026-10-02): direto ou via parceiro, o cliente
    # carrega e paga numa data combinada, normalmente bem longa.
    safra = proposta.pagamento == "Plano safra"
    vencimento_d = None
    if not erro and safra:
        try:
            vencimento_d = dt.datetime.strptime(vencimento_pagamento.strip(), "%Y-%m-%d").date()
        except ValueError:
            erro = "Informe a data em que o cliente vai pagar o plano safra."
        else:
            if vencimento_d <= dt.date.today():
                erro = "A data de pagamento do plano safra precisa ser no futuro."

    erro = erro_cnpj or erro
    if erro:
        # Volta pra ficha com o formulario aberto no mesmo lugar e o que ja
        # tinha sido preenchido (arquivo o navegador nao deixa reenviar)
        return _erro_ficha(request, db, user, cliente, erro, extra={
            "gerar_pedido_aberto": proposta.id,
            "gerar_pedido_valores": {"subsidiaria_cnpj": subsidiaria_cnpj, "faturamento_tipo": faturamento_tipo,
                                     "fat_nome": fat_nome, "fat_documento": fat_documento, "fat_telefone": fat_telefone,
                                     "fat_endereco": fat_endereco, "condicao_pagamento": condicao_pagamento,
                                     "data_limite_retirada": data_limite_retirada, "forma_prazo": forma_prazo,
                                     "prazo_parcelas": prazo_parcelas, "prazo_periodo_dias": prazo_periodo_dias,
                                     "vencimento_pagamento": vencimento_pagamento, "confirma_adicional": confirma_adicional},
        })

    if faturamento_tipo == "cliente":
        faturamento = {"nome": cliente.fazenda, "documento": cliente.cnpj or "Não informado", "telefone": cliente.telefone}
    else:
        faturamento = {"nome": fat_nome.strip(), "documento": fat_documento.strip(),
                        "telefone": fat_telefone.strip() or None, "endereco": fat_endereco.strip() or None}

    numero = _proximo_numero(db, PedidoCRM, PEDIDO_NUMERO_BASE)
    pedido = PedidoCRM(
        numero=numero, cliente_id=cliente.id, proposta_id=proposta.id, produto=proposta.produto,
        volume=proposta.volume, preco=proposta.preco, pagamento=proposta.pagamento, prazo=proposta.prazo,
        plano_safra_modalidade=proposta.plano_safra_modalidade, plano_safra_parceiro=proposta.plano_safra_parceiro,
        observacoes=proposta.observacoes,
        subsidiaria_nome=subsidiaria["nome"] if subsidiaria else None,
        subsidiaria_cnpj=subsidiaria["cnpj"] if subsidiaria else None,
        faturamento_tipo=faturamento_tipo, faturamento_nome=faturamento["nome"],
        faturamento_documento=faturamento["documento"], faturamento_telefone=faturamento.get("telefone"),
        faturamento_endereco=faturamento.get("endereco"),
        # A vista = pagou o total antes de retirar: se nao retirar tudo, a
        # sobra vira credito (regra do "antecipado" no Finalizar). Por isso
        # a pergunta "como vai funcionar a retirada" nem aparece pra a vista.
        # A prazo e plano safra pagam depois o que carregaram: se nao retirar
        # tudo, o volume oficial vira o retirado, sem credito (regra do
        # "por_retirada" no Finalizar). Por isso a pergunta "como vai
        # funcionar a retirada" saiu do Gerar pedido.
        condicao_pagamento="antecipado" if a_vista else "por_retirada",
        vencimento_pagamento=vencimento_d,
        data_limite_retirada=data_limite_d,
        forma_prazo=forma_prazo if a_prazo else None,
        prazo_parcelas=prazo_parcelas if a_prazo and forma_prazo == "boleto" else None,
        prazo_periodo_dias=periodo_dias if a_prazo and forma_prazo == "periodo" else None,
    )

    if exige_contrato:
        extensao = os.path.splitext(contrato_cessao.filename)[1].lower()
        nome_arquivo = f"pedido_{numero}_{uuid.uuid4().hex}{extensao}"
        with open(os.path.join(CONTRATOS_CESSAO_DIR, nome_arquivo), "wb") as f:
            f.write(contrato_cessao.file.read())
        pedido.contrato_cessao_path = nome_arquivo

    pagamento_texto = ""
    if a_vista:
        if comprovante:
            _guardar_comprovante(db, pedido, comprovante_pagamento.filename, comprovante, user)
            pagamento_texto = (" Pagamento à vista: comprovante enviado para conferência do financeiro; o carregamento "
                               "é liberado quando ele confirmar.")
        else:
            pagamento_texto = " Pagamento à vista: aguardando o comprovante para liberar o carregamento."

    credito_texto = ""
    if proposta.usa_credito:
        credito_valor, pedidos_origem = _credito_disponivel(db, cliente.id, proposta.produto)
        if credito_valor > 0:
            pedido.credito_aplicado_valor = credito_valor
            pedido.credito_origem_json = json.dumps([
                {"pedido_id": o.id, "numero": o.numero, "valor": o.saldo_credito_valor, "toneladas": o.saldo_credito}
                for o in pedidos_origem
            ])
            for origem in pedidos_origem:
                origem.saldo_credito = None
                origem.saldo_credito_valor = None
            toneladas_credito = credito_valor / proposta.preco
            credito_texto = (f" Crédito pendente do cliente aplicado: R$ {credito_valor:.2f} (equivalente a "
                              f"{toneladas_credito:.1f}t ao preço desta venda) — volume total a entregar neste "
                              f"pedido: {proposta.volume + toneladas_credito:.1f}t.")

    comissao.gravar(pedido)  # percentual da comissao com a regra de agora (pagina Regras)
    db.add(pedido)
    # Pedido de verdade e o sinal mais confiavel de forma de pagamento que
    # existe -- sobrescreve qualquer classificacao anterior (seja da
    # importacao antiga, seja de uma preferencia que o vendedor relatou por
    # comentario). Rafael pediu (2026-09-25): "contabilizar a forma de
    # pagamento pro cadastro dele" quando o cliente avancar pra pedido.
    cliente.forma_pagamento = pedido.pagamento
    proposta.status = STATUS_PROPOSTA_CONVERTIDA
    if cliente.fase != "realizado" and fase_e_avanco(cliente.fase, "realizado"):
        de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL['realizado']}"
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                           texto=f"Etapa avançou automaticamente: {de_para}",
                           fase_origem=cliente.fase, fase_destino="realizado", autor=user.nome_completo))
        cliente.fase = "realizado"
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    db.flush()
    faturado_por = f", faturado por {subsidiaria['nome']}" if subsidiaria else ""
    if em_andamento:
        credito_texto += (" Compra a mais, além do" + ("s pedidos " if len(em_andamento) > 1 else " pedido ")
                          + ", ".join(p.codigo for p in em_andamento) + " ainda em andamento.")
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido",
                       texto=f"Pedido {codigo_pedido(numero)} gerado a partir da proposta no {proposta.numero}: {proposta.produto} {proposta.volume}t{faturado_por}.{credito_texto}{pagamento_texto}",
                       numero=numero, produto=proposta.produto, volume=proposta.volume,
                       preco=proposta.preco, pagamento=proposta.pagamento, autor=user.nome_completo))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    if pedido.situacao_pagamento() == "em_conferencia":
        proximo = " Comprovante enviado ao financeiro: o carregamento é liberado quando ele confirmar o pagamento."
    elif pedido.aguardando_pagamento():
        proximo = " Próximo passo: anexe o comprovante de pagamento para liberar o carregamento."
    elif cliente.entrega_em_outro_lugar():
        proximo = (" Próximo passo: registre o destino final (para quais fazendas vai o produto): "
                   "é o endereço de entrega que a Logística usa.")
    else:
        proximo = " Acompanhe a retirada por aqui."
    avisar_sucesso(request, f"Pedido nº {numero} gerado.{proximo}")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.get("/crm/cliente/{cliente_id}/pedido/{pedido_id}/contrato-cessao")
def crm_baixar_contrato_cessao(cliente_id: int, pedido_id: int,
                                user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    # Documento sensivel (dados de 3 partes) -- so entrega se o pedido for
    # mesmo desse cliente E o vendedor tiver acesso a ele (mesma checagem de
    # carteira usada no resto do CRM), nunca via caminho estatico publico.
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or not pedido.contrato_cessao_path:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    caminho = os.path.join(CONTRATOS_CESSAO_DIR, pedido.contrato_cessao_path)
    if not os.path.isfile(caminho):
        return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=pedidos", status_code=303)
    return FileResponse(caminho, filename=f"contrato_cessao_pedido_{pedido.codigo}{os.path.splitext(caminho)[1]}")


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/comprovante")
def crm_anexar_comprovante(request: Request, cliente_id: int, pedido_id: int, comprovante: UploadFile = File(None),
                            user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Comprovante de pagamento de pedido a vista anexado depois de gerar o
    pedido (ou um a mais: pagou em duas vezes, pagou a diferenca de uma
    renegociacao, o anterior foi recusado). Vai pra conferencia do
    financeiro, que e quem libera o carregamento."""
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if (not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status != STATUS_PEDIDO_ABERTO
            or not pedido.exige_comprovante()):
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    conteudo, erro = _ler_comprovante(comprovante)
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="pedidos")

    _guardar_comprovante(db, pedido, comprovante.filename, conteudo, user)
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pagamento", autor=user.nome_completo,
                       texto=f"Comprovante de pagamento do pedido {pedido.codigo} enviado para conferência do financeiro."))
    db.commit()
    if pedido.pagamento_liberado_em:
        avisar_sucesso(request, f"Comprovante anexado ao pedido {pedido.codigo} e enviado ao financeiro.")
    else:
        avisar_sucesso(request, f"Comprovante enviado ao financeiro. O carregamento do pedido {pedido.codigo} "
                                f"é liberado quando ele confirmar o pagamento.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


def _achar_pedido_netsuite(db, texto):
    """Aceita "106605/SO3451", "SO3451" ou "106605" (como aparece na
    planilha de Expedicao / NetSuite)."""
    t = re.sub(r"\s+", "", (texto or "")).upper()
    if not t:
        return None
    exato = db.query(Pedido).filter(func.upper(Pedido.numero_pedido) == t).first()
    if exato:
        return exato
    if t.startswith("SO"):
        return db.query(Pedido).filter(func.upper(Pedido.numero_pedido).like(f"%/{t}")).first()
    if t.isdigit():
        return db.query(Pedido).filter(Pedido.numero_pedido.like(f"{t}/%")).first()
    return None


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/netsuite")
def crm_ligar_netsuite(request: Request, cliente_id: int, pedido_id: int, numero: str = Form(""),
                       user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Pedido do portal ja lancado no NetSuite que a planilha nao ligou
    sozinha: quem lancou informa o numero (Rafael, 2026-10-03: o pedido tem
    que aparecer pelo SO do NetSuite)."""
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status == STATUS_PEDIDO_CANCELADO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    pns = _achar_pedido_netsuite(db, numero)
    if not pns:
        return _erro_ficha(request, db, user, cliente,
                           f"Não achamos o pedido \"{numero.strip()}\" do NetSuite. Confira o número (ex.: 106605/SO3451). "
                           f"Pedido lançado depois da última leitura da planilha de Expedição só aparece na próxima.",
                           aba="pedidos")
    outro = db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite == pns.numero_pedido, PedidoCRM.id != pedido.id).first()
    if outro:
        return _erro_ficha(request, db, user, cliente,
                           f"O pedido {pns.numero_pedido} do NetSuite já está ligado ao pedido {outro.codigo_portal} do portal.",
                           aba="pedidos")
    pedido.pedido_netsuite = pns.numero_pedido
    pedido.vinculado_em = dt.datetime.utcnow()
    pedido.vinculado_por = user.nome_completo
    pedido.sincronizar_com_netsuite(pns)
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", autor=user.nome_completo,
                      texto=f"Pedido {pedido.codigo_portal} lançado no NetSuite como {pns.numero_pedido} (informado por {user.nome_completo})."))
    db.commit()
    avisar_sucesso(request, f"Pedido {pedido.codigo_portal} ligado ao {pns.numero_pedido} do NetSuite ({pns.cliente}). "
                            f"Confira se é o mesmo cliente.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/netsuite/desligar")
def crm_desligar_netsuite(request: Request, cliente_id: int, pedido_id: int,
                          user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Ligacao errada (so o admin desfaz)."""
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or not pedido.pedido_netsuite:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    antigo = pedido.pedido_netsuite
    pedido.pedido_netsuite = None
    pedido.vinculado_em = None
    pedido.vinculado_por = None
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", autor=user.nome_completo,
                      texto=f"Pedido {pedido.codigo_portal} desligado do {antigo} do NetSuite."))
    db.commit()
    avisar_sucesso(request, f"Pedido {pedido.codigo_portal} desligado do {antigo}.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.get("/crm/cliente/{cliente_id}/pedido/{pedido_id}/comprovante/{comprovante_id}")
def crm_ver_comprovante(cliente_id: int, pedido_id: int, comprovante_id: int,
                        user: User = Depends(require_role("admin", "vendedor", "financeiro")), db: Session = Depends(get_db)):
    # Dado bancario do cliente: so pro dono da carteira, admin e financeiro
    # (que confere), nunca por caminho estatico. Abre no navegador (inline).
    cliente = _cliente_do_usuario(db, user, cliente_id)
    comp = db.get(PedidoComprovante, comprovante_id)
    if not cliente or not comp or comp.pedido_id != pedido_id or comp.pedido.cliente_id != cliente.id:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    caminho = os.path.join(COMPROVANTES_PAGAMENTO_DIR, comp.arquivo)
    if not os.path.isfile(caminho):
        return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=pedidos", status_code=303)
    return FileResponse(caminho, filename=f"comprovante_pedido_{comp.pedido.codigo}{os.path.splitext(caminho)[1]}",
                        content_disposition_type="inline")


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/renegociar")
def crm_renegociar_pedido(request: Request, cliente_id: int, pedido_id: int, volume: str = Form(...),
                           preco: str = Form(...), pagamento: str = Form(...), prazo: str = Form(""),
                           plano_safra_modalidade: str = Form(""), plano_safra_parceiro: str = Form(""),
                           user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status != STATUS_PEDIDO_ABERTO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(db, pedido.produto, volume, preco, pagamento, prazo, plano_safra_modalidade, plano_safra_parceiro,
                                  parceiro_atual=pedido.plano_safra_parceiro if pedido.plano_safra_modalidade == "cessao" else None)
    if not erro and float(volume) < pedido.volume_destino_alocado() - 0.009:
        erro = (f"Esse pedido já tem {pedido.volume_destino_alocado():g}t com destino final registrado — "
                f"o volume não pode ficar abaixo disso.")
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="pedidos")

    preco_antigo, volume_antigo, pagamento_antigo = pedido.preco, pedido.volume, pedido.pagamento
    valor_antigo = pedido.valor_total()
    pedido.preco = float(preco)
    pedido.volume = float(volume)
    pedido.pagamento = pagamento
    comissao.gravar(pedido)  # preco novo: percentual com a regra de agora
    pedido.prazo = prazo.strip() if pagamento == "A prazo" else None
    pedido.plano_safra_modalidade = plano_safra_modalidade if pagamento == "Plano safra" else None
    pedido.plano_safra_parceiro = (plano_safra_parceiro.strip() or None) if (pagamento == "Plano safra" and plano_safra_modalidade == "cessao") else None
    # A condicao segue o pagamento (ver crm_gerar_pedido): a vista pagou o
    # total antes (sobra vira credito); a prazo e plano safra pagam depois o
    # que retiraram.
    if pagamento != pagamento_antigo:
        pedido.condicao_pagamento = "antecipado" if pagamento == "A vista" else "por_retirada"
    # O pagamento confirmado cobre o valor de quando foi conferido: pedido a
    # vista que ficou mais caro volta a aguardar pagamento (comprovante da
    # diferenca, que tambem passa pelo financeiro).
    pagamento_texto = ""
    if pedido.exige_comprovante() and pedido.pagamento_liberado_em and pedido.valor_total() > valor_antigo + 0.009:
        pedido.pagamento_liberado_em = None
        pedido.pagamento_liberado_por = None
        pagamento_texto = (f" O valor subiu de R$ {valor_antigo:.2f} para R$ {pedido.valor_total():.2f}: carregamento "
                           f"bloqueado até anexar o comprovante da diferença.")
    # Renegociar pedido tambem e um "pedido real", entao atualiza o cadastro
    # do cliente da mesma forma que gerar um pedido novo atualiza.
    cliente.forma_pagamento = pagamento
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido",
                       texto=(f"Pedido {pedido.codigo} renegociado: preço R$ {preco_antigo:.2f}/t -> R$ {pedido.preco:.2f}/t, "
                              f"volume {volume_antigo:g}t -> {pedido.volume:g}t, pagamento {pagamento_antigo} -> {pagamento}.{pagamento_texto}"),
                       numero=pedido.numero, produto=pedido.produto, volume=pedido.volume,
                       preco=pedido.preco, pagamento=pedido.pagamento, autor=user.nome_completo))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    if pedido.situacao_pagamento() in ("sem_comprovante", "recusado", "diferenca"):
        avisar_sucesso(request, f"Pedido {pedido.codigo} renegociado. Próximo passo: anexe o comprovante de pagamento"
                                f"{' da diferença' if pagamento_texto else ''} para liberar o carregamento.")
    else:
        avisar_sucesso(request, f"Pedido {pedido.codigo} renegociado.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/cancelar")
def crm_cancelar_pedido(request: Request, cliente_id: int, pedido_id: int, motivo: str = Form(...),
                         user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status != STATUS_PEDIDO_ABERTO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    if not motivo.strip():
        return _erro_ficha(request, db, user, cliente, "Informe o motivo do cancelamento.", aba="pedidos")

    pedido.status = STATUS_PEDIDO_CANCELADO
    pedido.motivo_cancelamento = motivo.strip()

    credito_texto = ""
    if pedido.credito_origem_json:
        # Este pedido nunca vai acontecer de verdade -- o credito que ele
        # tinha consumido de pedidos anteriores (ja pago pelo cliente antes)
        # tem que voltar pros pedidos de origem, senao o cliente perde esse
        # dinheiro sem nunca ter recebido o produto por ele.
        origem_lista = json.loads(pedido.credito_origem_json)
        for item in origem_lista:
            origem = db.get(PedidoCRM, item["pedido_id"])
            if origem:
                origem.saldo_credito = item["toneladas"]
                origem.saldo_credito_valor = item["valor"]
        credito_texto = f" Crédito de R$ {pedido.credito_aplicado_valor:.2f} que tinha sido aplicado a este pedido foi devolvido ao cliente."

    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido_cancelado",
                       texto=f"Pedido {pedido.codigo} cancelado: {motivo.strip()}.{credito_texto}",
                       numero=pedido.numero, produto=pedido.produto, volume=pedido.volume,
                       preco=pedido.preco, pagamento=pedido.pagamento, autor=user.nome_completo))

    # Clientes finais que iam receber produto deste pedido. Rafael
    # (2026-09-25): cancelou, a compra nao aconteceu -- so continua Realizado
    # se sobrar outro registro real de compra com a gente; senao volta pra
    # fase em que estava antes do produto via parceiro leva-lo pra Realizado.
    for destino in pedido.destinos_finais:
        cf = destino.cliente_final
        db.add(ContatoCRM(cliente_id=cf.id, tipo="nota",
                           texto=(f"Pedido {pedido.codigo} (comprado por {cliente.fazenda}), que traria "
                                  f"{destino.volume:g}t de {pedido.produto} pra este cliente, foi cancelado."),
                           autor=user.nome_completo))
        if cf.vendedor_nome != cliente.vendedor_nome:
            _avisar(db, cf.vendedor_nome, cf, "pedido_parceiro_cancelado",
                    f"Pedido via {_canal_venda(cliente)} cancelado",
                    f"O pedido {pedido.codigo} de {cliente.fazenda}, que traria {destino.volume:g}t de "
                    f"{pedido.produto} pra {cf.fazenda}, foi cancelado. Motivo: {motivo.strip()}.",
                    user.nome_completo)
        if cf.fase != "realizado" or _tem_compra_ativa(db, cf.id, excluir_pedido_id=pedido.id):
            continue
        # Entrada mais recente em Realizado: se foi registrada por aqui (tem
        # fase de origem), foi uma compra que nao vale mais -- volta pra la.
        # Se veio da planilha/importacao (sem origem) ou o cliente ja saiu de
        # Realizado depois dela, o Realizado atual vem de outra fonte: mantem.
        ultima = (db.query(ContatoCRM)
                    .filter(ContatoCRM.cliente_id == cf.id, ContatoCRM.tipo == "mudanca_fase",
                            (ContatoCRM.fase_destino == "realizado") | (ContatoCRM.fase_origem == "realizado"))
                    .order_by(ContatoCRM.id.desc()).first())
        if not ultima or ultima.fase_destino != "realizado" or not ultima.fase_origem:
            continue
        tem_proposta = db.query(PropostaCRM).filter(
            PropostaCRM.cliente_id == cf.id, PropostaCRM.status == STATUS_PROPOSTA_ABERTA).count() > 0
        # "Proposta" so com proposta aberta de verdade -- senao seria uma
        # proposta fantasma (mesma regra do cancelamento de pedido direto).
        if tem_proposta:
            nova_fase = "proposta"
        elif ultima.fase_origem == "proposta":
            nova_fase = "contactado"
        else:
            nova_fase = ultima.fase_origem
        if nova_fase != cf.fase:
            db.add(ContatoCRM(cliente_id=cf.id, tipo="mudanca_fase",
                               texto=(f"Etapa atualizada automaticamente (pedido do parceiro cancelado): "
                                      f"{FASE_LABEL['realizado']} -> {FASE_LABEL.get(nova_fase, nova_fase)}"),
                               fase_origem=cf.fase, fase_destino=nova_fase, autor=user.nome_completo))
            cf.fase = nova_fase

    # Conta tambem pedidos FINALIZADOS de outro produto e produto recebido via
    # parceiro -- sao compras reais, entao continuam justificando o cliente
    # ficar em "Realizado" mesmo depois desse cancelamento aqui.
    if not _tem_compra_ativa(db, cliente.id, excluir_pedido_id=pedido.id):
        # Sem mais nenhum pedido ativo, o cliente nao e mais "realizado" --
        # mas se ainda tiver uma proposta em aberto de OUTRO produto, o
        # destino certo e "proposta" (ainda tem negociacao rolando), nao
        # "contactado" (que sugeriria nao ter nada em andamento).
        tem_proposta_aberta = db.query(PropostaCRM).filter(
            PropostaCRM.cliente_id == cliente.id, PropostaCRM.status == STATUS_PROPOSTA_ABERTA
        ).count() > 0
        nova_fase = "proposta" if tem_proposta_aberta else "contactado"
        if nova_fase != cliente.fase:
            de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL[nova_fase]}"
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                               texto=f"Etapa atualizada automaticamente: {de_para}",
                               fase_origem=cliente.fase, fase_destino=nova_fase, autor=user.nome_completo))
            cliente.fase = nova_fase
    # Sessao usa autoflush=False: sem isso o recalculo ainda ve o pedido como
    # nao-cancelado e continua somando o volume dele.
    db.flush()
    _recalcular_resumo_comercial(db, cliente)
    for destino in pedido.destinos_finais:
        _recalcular_resumo_comercial(db, destino.cliente_final)
    db.commit()
    avisar_sucesso(request, f"Pedido {pedido.codigo} cancelado.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/destino-final")
def crm_adicionar_destino_final(request: Request, cliente_id: int, pedido_id: int,
                                 cliente_final_id: str = Form(""), volume: str = Form(...), observacoes: str = Form(""),
                                 user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Registra que parte (ou todo) o produto de um pedido faturado pra um
    parceiro/transportadora foi entregue na fazenda de um cliente final. O
    cliente final avanca no funil normal e ganha a compra na sazonalidade,
    mas o registro no historico dele fica marcado como "via parceiro"."""
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status == STATUS_PEDIDO_CANCELADO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    cliente_final = db.get(ClienteCRM, int(cliente_final_id)) if cliente_final_id.strip().isdigit() else None
    erro = None
    if not cliente_final:
        erro = "Selecione o cliente final na busca (se ele ainda não existe, cadastre antes)."
    elif cliente_final.id == cliente.id:
        erro = "O destino final não pode ser o próprio comprador do pedido."
    else:
        try:
            volume_f = float(volume)
        except ValueError:
            volume_f = 0
        pendente = pedido.volume_destino_pendente()
        if volume_f <= 0:
            erro = "Informe o volume (t) que foi pra esse cliente."
        elif volume_f > pendente + 0.009:
            erro = f"Só restam {pendente:g}t sem destino neste pedido — o volume informado passa disso."
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="pedidos")

    db.add(PedidoDestinoFinal(pedido_id=pedido.id, cliente_final_id=cliente_final.id, volume=volume_f,
                               observacoes=observacoes.strip() or None, autor=user.nome_completo))
    db.add(ContatoCRM(cliente_id=cliente_final.id, tipo="destino_final",
                       texto=(f"Recebeu {volume_f:g}t de {pedido.produto} do pedido {pedido.codigo}, "
                              f"faturado para {cliente.fazenda}."
                              + (f" Obs: {observacoes.strip()}" if observacoes.strip() else "")),
                       numero=pedido.numero, produto=pedido.produto, volume=volume_f,
                       cliente_relacionado_id=cliente.id, autor=user.nome_completo))
    if cliente_final.fase != "realizado" and fase_e_avanco(cliente_final.fase, "realizado"):
        de_para = f"{FASE_LABEL.get(cliente_final.fase, cliente_final.fase)} -> {FASE_LABEL['realizado']}"
        db.add(ContatoCRM(cliente_id=cliente_final.id, tipo="mudanca_fase",
                           texto=f"Etapa avançou automaticamente (produto recebido via parceiro): {de_para}",
                           fase_origem=cliente_final.fase, fase_destino="realizado", autor=user.nome_completo))
        cliente_final.fase = "realizado"
    cliente_final.ultima_interacao_em = dt.datetime.utcnow()
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="destino_registrado",
                       texto=(f"Pedido {pedido.codigo}: {volume_f:g}t destinados ao cliente final {cliente_final.fazenda}."
                              + (f" Obs: {observacoes.strip()}" if observacoes.strip() else "")),
                       numero=pedido.numero, produto=pedido.produto, volume=volume_f,
                       cliente_relacionado_id=cliente_final.id, autor=user.nome_completo))
    if cliente_final.vendedor_nome != cliente.vendedor_nome:
        # O dono da carteira do cliente final nao participou dessa venda --
        # sem o aviso ele nunca saberia que o cliente dele recebeu produto.
        canal = _canal_venda(cliente)
        _avisar(db, cliente_final.vendedor_nome, cliente_final, "recebeu_via_parceiro",
                f"Seu cliente recebeu produto via {canal}",
                f"{cliente_final.fazenda} recebeu {volume_f:g}t de {pedido.produto} via {canal} {cliente.fazenda} "
                f"(pedido {pedido.codigo}, venda de {cliente.vendedor_nome or user.nome_completo}). "
                f"Faça o pós-venda com o cliente.", user.nome_completo)
    db.flush()
    _recalcular_resumo_comercial(db, cliente_final)
    db.commit()
    avisar_sucesso(request, f"Destino registrado: {volume_f:g} t para {cliente_final.fazenda}.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/finalizar")
def crm_finalizar_pedido(request: Request, cliente_id: int, pedido_id: int, volume_retirado: str = Form(...),
                          user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    """Marca o pedido como encerrado -- a venda aconteceu de verdade e
    terminou (diferente de cancelar, onde a venda NAO aconteceu). O vendedor
    informa quanto foi retirado de fato, como medida de FECHAMENTO (uma vez
    so, nao um campo aberto pra editar sempre) -- isso vira automatico
    quando existir integracao com o NetSuite, mas ate la o Rafael decidiu
    que o cliente precisa saber do saldo dele agora, entao o vendedor digita.

    A `condicao_pagamento` (combinada la no Gerar Pedido) decide o que fazer
    com a diferenca entre o volume CONTRATADO (pago) e o volume retirado:
    - "antecipado" (cliente ja pagou o total): o volume/valor oficial do
      pedido continua o contratado -- a sobra vira `saldo_credito` do
      cliente pra usar numa proxima negociacao.
    - "por_retirada" (ou padrao, sem condicao especial): o volume/valor
      oficial do pedido vira o que foi retirado de fato -- sem credito, ja
      que o cliente nunca pagou pelo resto e nao precisamos entregar mais
      nada.

    Se este pedido tinha CREDITO JA APLICADO (`credito_aplicado_valor`, de um
    pedido anterior -- ver crm_gerar_pedido), o volume fisico disponivel pra
    retirar e maior que o `volume` contratado (`volume_total_a_entregar()`).
    Essas toneladas de credito ja foram pagas antes, entao nunca "encolhem"
    de graca -- se sobrar credito nao retirado aqui, ele so continua sendo
    credito de novo (revalorizado ao preco DESTE pedido), independente da
    `condicao_pagamento` daqui (que so rege a parte NOVA, recem-contratada)."""
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status != STATUS_PEDIDO_ABERTO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    # Sem pagamento confirmado o carregamento nao foi liberado: nao ha retirada pra fechar.
    if pedido.aguardando_pagamento():
        return _erro_ficha(request, db, user, cliente,
                            f"O pedido {pedido.codigo} é à vista e o pagamento ainda não foi confirmado pelo financeiro. "
                            f"Sem isso o carregamento não é liberado, então não há retirada para finalizar.", aba="pedidos")

    credito_ton = pedido.toneladas_credito_aplicado()
    total_a_entregar = pedido.volume_total_a_entregar()
    try:
        retirado = float(volume_retirado)
    except ValueError:
        return _erro_ficha(request, db, user, cliente, "Informe um volume retirado válido.", aba="pedidos")
    if retirado <= 0 or retirado > total_a_entregar:
        return _erro_ficha(request, db, user, cliente,
                            f"O volume retirado deve ser maior que zero e não pode passar do volume total disponível ({total_a_entregar:.1f}t).",
                            aba="pedidos")

    pago_retirado = min(retirado, pedido.volume)
    credito_retirado = max(0.0, retirado - pedido.volume)
    credito_nao_retirado = credito_ton - credito_retirado
    sobra = pedido.volume - pago_retirado
    pedido.volume_retirado = retirado
    # Credito ja aplicado (pago antes, em outro pedido) nunca "some" de graca
    # se nao for retirado agora -- so continua sendo credito, revalorizado ao
    # preco DESTE pedido. Isso e somado ao credito que a parte NOVA do pedido
    # (abaixo) possa gerar.
    novo_saldo_ton = credito_nao_retirado

    if sobra > 0.009 and pedido.condicao_pagamento == "antecipado":
        novo_saldo_ton += sobra
        texto = (f"Pedido {pedido.codigo} finalizado: {pago_retirado:.1f}t retirados de {pedido.volume:.1f}t "
                 f"contratados (pago antecipado)")
    elif sobra > 0.009:
        pedido.volume = pago_retirado
        texto = (f"Pedido {pedido.codigo} finalizado: {pago_retirado:.1f}t retirados (pagamento por retirada) — "
                 f"volume oficial do pedido ajustado pra {pago_retirado:.1f}t")
    else:
        texto = f"Pedido {pedido.codigo} finalizado: {pago_retirado:.1f}t retirados (volume total contratado)"

    if credito_ton > 0.009:
        texto += (f", mais {credito_retirado:.1f}t retirados do crédito já aplicado "
                  f"(de R$ {pedido.credito_aplicado_valor:.2f})")

    if novo_saldo_ton > 0.009:
        pedido.saldo_credito = novo_saldo_ton
        pedido.saldo_credito_valor = novo_saldo_ton * pedido.preco
        texto += (f" — saldo de R$ {pedido.saldo_credito_valor:.2f} (equivalente a {novo_saldo_ton:.1f}t ao preço "
                  f"desta venda) fica registrado como crédito do cliente pra próxima negociação.")
    else:
        pedido.saldo_credito = None
        pedido.saldo_credito_valor = None
        texto += ", sem crédito."
    pedido.status = STATUS_PEDIDO_FINALIZADO

    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido_finalizado", texto=texto,
                       numero=pedido.numero, produto=pedido.produto, volume=pedido.volume,
                       preco=pedido.preco, pagamento=pedido.pagamento, autor=user.nome_completo))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    avisar_sucesso(request, f"Pedido {pedido.codigo} finalizado.")
    return RedirectResponse(_url_ficha(cliente_id, "pedidos", _via_ficha(request, cliente.id), f"pedido-{pedido.id}"), status_code=303)


# ---------- ciclo de vendas (safra) ----------

def _executar_reset_ciclo(db, novo_rotulo):
    """Reinicio da carteira comercial pro novo ciclo de vendas -- regra do
    Rafael (2026-09-24): a cada 1o de novembro, tudo que estiver em
    Perdido/Nao usara/Realizado/Contactados volta pra "a contactar" (nova
    safra, todo cliente vira oportunidade de novo). Clientes em "Proposta"
    NAO sao tocados -- ja tem prioridade propria via "proposta parada" na
    fila. `ultima_interacao_em` e os campos de resumo (volume/proposta) NAO
    sao mexidos, ficam como historico; `PedidoCRM` nunca e tocado.

    Cliente que ainda tem PedidoCRM aberto (retirada pendente) TAMBEM volta
    pra "a contactar" (pedido do Rafael: "o cliente volta a prospectar"),
    mas ganha uma nota diferenciada -- e uma situacao de contato diferente
    das demais (confirmar se ainda vai retirar ou se o pedido deve fechar),
    nao um lead frio comum. Ver `_motivo_prioritario` (tier de "pedido aberto
    com cliente em prospeccao") pra como isso vira aviso pro vendedor."""
    fases_resetar = ("perdido", "nao_usara", "realizado", "contactado")
    clientes = db.query(ClienteCRM).filter(ClienteCRM.fase.in_(fases_resetar)).all()
    ids = [c.id for c in clientes]

    com_pedido_aberto = set()
    if ids:
        for (cid,) in (db.query(PedidoCRM.cliente_id)
                         .filter(PedidoCRM.cliente_id.in_(ids), PedidoCRM.status == STATUS_PEDIDO_ABERTO)
                         .distinct().all()):
            com_pedido_aberto.add(cid)

    for c in clientes:
        fase_origem = c.fase
        c.fase = "a_contactar"
        if c.id in com_pedido_aberto:
            texto = (f"Reinicio do ciclo de vendas {novo_rotulo}: carteira volta pra prospecção, mas "
                      "este cliente AINDA TEM pedido em aberto — confirme se ele vai retirar o produto "
                      "ou se o pedido deve ser finalizado.")
        else:
            texto = f"Reinicio do ciclo de vendas {novo_rotulo}: carteira reiniciada pra nova safra."
        db.add(ContatoCRM(cliente_id=c.id, tipo="mudanca_fase", texto=texto,
                           fase_origem=fase_origem, fase_destino="a_contactar", autor="Sistema"))

    ciclo = db.query(CicloVendas).first()
    if ciclo is None:
        ciclo = CicloVendas(ciclo_atual=novo_rotulo)
        db.add(ciclo)
    else:
        ciclo.ciclo_atual = novo_rotulo
    ciclo.resetado_em = dt.datetime.utcnow()
    db.commit()
    return len(clientes), len(com_pedido_aberto)


def garantir_ciclo_atualizado(db):
    """Checagem "preguicosa" chamada nas paginas principais do CRM -- sem
    scheduler de verdade no projeto ainda (nao ha infraestrutura de cron/job
    agendado), entao o reset acontece sozinho na primeira vez que ALGUEM
    acessa o sistema depois da virada (nao precisa do servidor estar de pe
    exatamente a meia-noite de 1o/nov). Na primeira execucao de sempre (linha
    ainda nao existe) so REGISTRA o ciclo atual, nao reseta nada -- senao
    ligar essa funcionalidade pela primeira vez em setembro ja apagaria fase
    de todo mundo por engano."""
    rotulo_esperado = ciclo_rotulo()
    ciclo = db.query(CicloVendas).first()
    if ciclo is None:
        db.add(CicloVendas(ciclo_atual=rotulo_esperado, resetado_em=None))
        db.commit()
        return
    if ciclo.ciclo_atual != rotulo_esperado:
        _executar_reset_ciclo(db, rotulo_esperado)


# ---------- area do vendedor ----------

# Regras da fila de trabalho (Rafael, 2026-09-25). Os numeros (250 t/dia, 3/7/14
# dias da epoca de compra, 10/3 dias da proposta, 60 dias sem contato, lotes de
# 10 e 15) e o liga/desliga de cada motivo vem de config.py (pagina Regras).
ENTRESSAFRA_MESES = (12, 1, 2, 3)   # plantio: poucas compras, foco em contato/cadastro/novos clientes


# Motivo da fila de trabalho <-> numero do "tier" (cor e etiqueta). A ORDEM de
# prioridade e os nomes vem da pagina Regras (config "ordem_fila"/"nome_fila_*").
TIER_CHAVE = {0: "vencido", 1: "apertada", 2: "pedido_aberto", 3: "quente", 4: "epoca", 5: "proposta",
              6: "sem_contato", 7: "primeiro_contato", 8: "oportunidade"}
CHAVE_TIER = {v: k for k, v in TIER_CHAVE.items()}


def _oportunidades_abertas(db, ids_clientes):
    """cliente_id -> OportunidadeLogistica ainda aberta (a mais recente): dentro
    da validade e sem contato real registrado depois que ela foi criada."""
    if not ids_clientes:
        return {}
    hoje = dt.date.today()
    abertas = {}
    for o in (db.query(OportunidadeLogistica)
                .filter(OportunidadeLogistica.cliente_id.in_(ids_clientes), OportunidadeLogistica.valida_ate >= hoje)
                .order_by(OportunidadeLogistica.criada_em)):
        abertas[o.cliente_id] = o
    if abertas:
        for cid, quando in (db.query(ContatoCRM.cliente_id, func.max(ContatoCRM.data))
                              .filter(ContatoCRM.cliente_id.in_(list(abertas)), ContatoCRM.tipo.in_(TIPOS_CONTATO_REAL))
                              .group_by(ContatoCRM.cliente_id)):
            if quando and quando > abertas[cid].criada_em:
                del abertas[cid]  # o vendedor ja tratou
    return abertas


def _posicao_tier():
    """tier -> posicao na ordem que o admin arrastou (Contato a resolver sempre antes)."""
    pos = {CHAVE_TIER[k]: i for i, k in enumerate(config.valor("ordem_fila"))}
    pos[-1] = -1
    return pos


def _motivo_prioritario(cliente, pedidos_vencidos_por_cliente, pedidos_abertos_por_cliente,
                         propostas_abertas_por_cliente, mes_atual, mes_nome, dias_ate_ciclo, lead=None, oportunidade=None):
    """Um unico motivo (o mais urgente) que justifica esse cliente aparecer na
    fila de trabalho hoje -- fila curta e priorizada de ACOES COMERCIAIS, nao
    um espelho do funil inteiro (ideia do Theo, 2026-09-24).

    Ordem PADRAO (o admin muda na pagina Regras): 0 pedido vencido; 1 retirada
    apertada (>= config "retirada_apertada_t_dia"); 2 pedido aberto com cliente
    de volta a prospeccao; 3 proposta aberta com cliente QUENTE (fica ate ter
    resultado); 4 mes de compra habitual agora ou no proximo mes (volta apos
    contato conforme a temperatura); 5 proposta sem retorno; 6 sem contato;
    7 primeiro contato. Os lotes (6 e 7) sao cortados em
    `_montar_fila_trabalho`; cadastro incompleto e outra fila
    (`_montar_fila_base`).

    "A contactar sem contato recente" e cadastro incompleto fora da
    entressafra NAO entram: seriam 181/268 e 116/268 da Zilma, afogando as
    acoes de venda de verdade.

    Retorna (tier, texto, aba_acao, ordem, etiqueta) ou None. `texto` e a
    frase completa (banner da ficha); `etiqueta` e a versao curta da lista;
    `ordem` desempata dentro do tier (menor primeiro).

    Desde 2026-10-04 cada motivo e uma conferencia separada, avaliada na ordem
    da pagina Regras (config "ordem_fila"): o cliente fica no PRIMEIRO motivo
    que se aplicar. `lead` ({"novo": bool}) so vem da fila de trabalho: o
    primeiro contato precisa do historico de contatos (a ficha nao avalia).
    `oportunidade`: OportunidadeLogistica aberta do cliente (caminhao indo pra
    regiao dele -- frete mais barato; Rafael, 2026-10-04)."""
    pedidos_a = pedidos_abertos_por_cliente.get(cliente.id) or []
    dias_sem_contato = cliente.dias_desde_ultima_interacao()
    propostas_v = propostas_abertas_por_cliente.get(cliente.id)

    def vencido():
        pedidos_v = pedidos_vencidos_por_cliente.get(cliente.id)
        if pedidos_v:
            pior = max(pedidos_v, key=lambda p: -p.dias_restantes())
            dias = -pior.dias_restantes()
            return (0, f"Pedido {pior.codigo} vencido há {dias} dia{'s' if dias != 1 else ''} sem retirada",
                    "pedidos", 0, f"Pedido vencido · {dias} dia{'s' if dias != 1 else ''}")

    def apertada():
        # Antecipar antes de vencer: se pra cumprir o prazo o cliente precisa
        # retirar muito por dia, ja e hora de ligar (Rafael, 2026-09-25). Sem
        # carregamento individual (so vem do NetSuite), o saldo e o volume todo.
        if not config.ligada("liga_fila_apertada"):
            return None
        limite_t = config.valor("retirada_apertada_t_dia")
        apertados = [(p, t) for p, t in ((p, p.ton_dia_necessario()) for p in pedidos_a) if t is not None and t >= limite_t]
        if apertados:
            p, ton_dia = max(apertados, key=lambda x: x[1])
            return (1, f"Pedido {p.codigo} precisa retirar {ton_dia:.0f} t/dia até "
                       f"{p.data_limite_retirada.strftime('%d/%m')} — antecipe com o cliente", "pedidos", -ton_dia,
                    f"Retirada {ton_dia:.0f} t/dia")

    def pedido_aberto():
        # Cliente voltou pra fase inicial (tipicamente pelo reinicio do ciclo,
        # ver _executar_reset_ciclo) mas AINDA tem pedido aberto: o vendedor
        # confirma se ainda vai retirar ou se o pedido deve ser finalizado
        # (Rafael, 2026-09-24) -- nao e um lead frio comum.
        if pedidos_a and cliente.fase in ("a_contactar", "contactado"):
            saldo = sum(p.volume_total_a_entregar() for p in pedidos_a)
            return (2, f"Ainda tem {saldo:.0f}t em aberto do pedido {pedidos_a[0].codigo} — confirme "
                       "se vai retirar ou finalize o pedido", "pedidos", 0, f"Pedido em aberto · {saldo:.0f} t")

    def quente():
        # Negociacao quente fica na fila ate ter resultado; mais tempo sem contato primeiro
        if propostas_v and cliente.temperatura == "quente" and config.ligada("liga_fila_quente"):
            p = max(propostas_v, key=lambda x: x.valor_total())
            quando = (f"último contato há {dias_sem_contato} dia{'s' if dias_sem_contato != 1 else ''}"
                      if dias_sem_contato is not None else "sem contato registrado")
            return (3, f"Negociação quente: proposta de {p.produto} — acompanhar até fechar ({quando})",
                    "comentario", -(dias_sem_contato or 0), "Proposta quente")

    def epoca():
        # Entra no mes em que o cliente costuma comprar E no anterior (Rafael,
        # 2026-09-25); depois do contato volta conforme a temperatura.
        sazonal = cliente.prioridade_sazonal(mes_atual)
        if cliente.fase not in ("perdido", "nao_usara") and sazonal in (0, 1) and config.ligada("liga_fila_epoca"):
            espera = {"quente": config.valor("fila_epoca_quente"),
                      "morno": config.valor("fila_epoca_morno")}.get(cliente.temperatura, config.valor("fila_epoca_frio"))
            if dias_sem_contato is None or dias_sem_contato >= espera:
                mes_compra = mes_nome if sazonal == 0 else MESES_PT[mes_atual % 12]
                if sazonal == 0:
                    texto = f"Costuma comprar em {mes_compra} — bom momento pra contato"
                else:
                    texto = f"Costuma comprar em {mes_compra} — contate com antecedência"
                return (4, texto, "comentario", sazonal, f"Compra em {mes_compra.lower()}")

    def proposta():
        # PropostaCRM REAL (aberta), nunca `cliente.fase`. Conta do mais recente
        # entre a ultima alteracao da proposta e o ultimo contato (ligar tambem
        # tira da fila). Perto da virada do ciclo o prazo encurta.
        if not (propostas_v and config.ligada("liga_fila_proposta")):
            return None
        mais_parada = min(propostas_v, key=lambda p: p.atualizado_em or p.criado_em)
        datas = [d for d in (mais_parada.atualizado_em or mais_parada.criado_em, cliente.ultima_interacao_em) if d]
        referencia = max(datas) if datas else None
        dias_parada = (dt.datetime.utcnow() - referencia).days if referencia else None
        perto = dias_ate_ciclo <= config.valor("fila_proposta_janela")
        limite = config.valor("fila_proposta_dias_perto") if perto else config.valor("fila_proposta_dias")
        if dias_parada is None or dias_parada >= limite:
            detalhe = f"há {dias_parada} dias" if dias_parada is not None else "recém-criada"
            urgencia = " — virada do ciclo se aproxima" if perto else ""
            return (5, f"Proposta de {mais_parada.produto} sem retorno {detalhe}{urgencia}", "comentario", 0,
                    f"Proposta sem retorno · {dias_parada} dias" if dias_parada is not None else "Proposta sem retorno")

    def sem_contato():
        # Todo cliente ativo precisa de contato de tempos em tempos (Rafael,
        # 2026-09-24). Entra em lote (`_montar_fila_trabalho`): muitos clientes
        # tem a importacao (02/09) como ultimo contato e vencem juntos.
        limite_contato = config.valor("fila_sem_contato_dias")
        if (config.ligada("liga_fila_sem_contato") and cliente.fase not in ("perdido", "nao_usara")
                and dias_sem_contato is not None and dias_sem_contato >= limite_contato):
            return (6, f"Sem contato há {dias_sem_contato} dias — passou do limite de {limite_contato} dias combinado", "comentario",
                    (-dias_sem_contato, -(cliente.area_plantada_ha or 0)), f"Sem contato · {dias_sem_contato} dias")

    def primeiro_contato():
        if lead is None or not config.ligada("liga_fila_primeiro_contato"):
            return None
        novo = lead["novo"]
        return (7, "Cliente novo: fazer o primeiro contato" if novo else "Primeiro contato: nunca foi contatado", "comentario",
                (0 if novo else 1, -(cliente.area_plantada_ha or 0), cliente.fazenda), "Cliente novo" if novo else "Primeiro contato")

    def oportunidade_logistica():
        # A Logistica avisou: caminhao indo pra regiao do cliente, frete mais barato
        if oportunidade is None or not config.ligada("liga_fila_oportunidade"):
            return None
        ate = oportunidade.valida_ate.strftime("%d/%m")
        caminhao = f"caminhão indo para {oportunidade.destino}" + (
            f" em {oportunidade.data_caminhao.strftime('%d/%m')}" if oportunidade.data_caminhao else "")
        texto = (f"Oportunidade logística: {caminhao} — frete mais barato para a região. Ofereça até {ate}."
                 + (f" Recado da Logística: {oportunidade.recado}" if oportunidade.recado else ""))
        return (8, texto, "comentario", (oportunidade.valida_ate, -(cliente.area_plantada_ha or 0)), f"Frete p/ região · até {ate}")

    conferencias = {"vencido": vencido, "apertada": apertada, "pedido_aberto": pedido_aberto, "quente": quente,
                    "epoca": epoca, "proposta": proposta, "sem_contato": sem_contato, "primeiro_contato": primeiro_contato,
                    "oportunidade": oportunidade_logistica}
    for chave in config.valor("ordem_fila"):
        motivo = conferencias[chave]()
        if motivo:
            return motivo
    return None


def _montar_fila_trabalho(db, user):
    """Monta a fila de trabalho INTEIRA (sem cortar), ordenada por urgencia.
    Extraido de `crm_home_vendedor` pra poder ser reusado tambem na pagina
    "ver todos" (`crm_fila_trabalho_vendedor`) -- a home so mostra os 8
    primeiros, a pagina dedicada mostra todos com a mesma logica e o mesmo
    visual, sem duplicar a query nem o calculo de tier."""
    query = db.query(ClienteCRM)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes_query = query.all()

    # Contato que nao funciona (numero errado, sem WhatsApp, nao retorna --
    # Rafael, 2026-10-02): o trabalho e do ADMIN, conseguir outro numero ou
    # meio de contato. Sai da fila do vendedor (nao tem o que fazer ate la) e
    # entra no topo da fila do admin.
    contato_a_resolver = [c for c in clientes_query if c.precisa_ajuda and c.fase not in ("perdido", "nao_usara")]
    ids_a_resolver = {c.id for c in contato_a_resolver}

    ids_clientes = [c.id for c in clientes_query]
    pedidos_vencidos_por_cliente = {}
    pedidos_abertos_por_cliente = {}
    propostas_abertas_por_cliente = {}
    if ids_clientes:
        pedidos_abertos_query = (db.query(PedidoCRM)
                                    .filter(PedidoCRM.cliente_id.in_(ids_clientes), PedidoCRM.status == STATUS_PEDIDO_ABERTO)
                                    .all())
        for p in pedidos_abertos_query:
            pedidos_abertos_por_cliente.setdefault(p.cliente_id, []).append(p)
            if p.vencido():
                pedidos_vencidos_por_cliente.setdefault(p.cliente_id, []).append(p)

        propostas_abertas_query = (db.query(PropostaCRM)
                                      .filter(PropostaCRM.cliente_id.in_(ids_clientes), PropostaCRM.status == STATUS_PROPOSTA_ABERTA)
                                      .all())
        for p in propostas_abertas_query:
            propostas_abertas_por_cliente.setdefault(p.cliente_id, []).append(p)

    mes_atual = dt.datetime.utcnow().month
    mes_nome = MESES_PT[mes_atual - 1]
    dias_ate_ciclo = dias_para_virada_ciclo()

    # Primeiro contato: cliente cadastrado no CRM ainda sem contato, ou lead --
    # "a contactar", sem contato real E que nunca avancou de fase (so "a
    # contactar" nao serve: a virada de 1/nov poe a carteira inteira la). Sem
    # telefone valido fica na fila de cadastro, nem da pra ligar. Calculado antes
    # do laco porque a posicao dele na prioridade e configuravel.
    candidatos = [c for c in clientes_query
                  if c.id not in ids_a_resolver and c.fase not in ("perdido", "nao_usara")
                  and not any(f.startswith("telefone") for f in c.campos_faltando())]
    com_contato, ja_avancou = _historico_de_contato(db, [c.id for c in candidatos])
    leads = {}
    for c in candidatos:
        if c.id in com_contato:
            continue
        novo = c.id_origem is None
        if not novo and (c.fase != "a_contactar" or c.id in ja_avancou):
            continue
        leads[c.id] = {"novo": novo}

    oportunidades = _oportunidades_abertas(db, [c.id for c in clientes_query])
    fila = []
    for c in clientes_query:
        if c.id in ids_a_resolver:
            continue
        motivo = _motivo_prioritario(c, pedidos_vencidos_por_cliente, pedidos_abertos_por_cliente,
                                      propostas_abertas_por_cliente, mes_atual, mes_nome, dias_ate_ciclo, lead=leads.get(c.id),
                                      oportunidade=oportunidades.get(c.id))
        if motivo:
            tier, texto, aba, ordem, etiqueta = motivo
            link = f"/crm/cliente/{c.id}?via=fila" + (f"&aba={aba}" if aba != "dados" else "")
            fila.append({"cliente": c, "tier": tier, "texto": texto, "etiqueta": etiqueta, "cor": COR_TIER[tier],
                         "sub": _local_cliente(c), "link": link, "ordem": ordem})
    # Ordem dos motivos = a da pagina Regras; dentro do motivo, o desempate
    # proprio dele (t/dia, dias sem contato...), depois cliente "quente" antes de "frio".
    posicao = _posicao_tier()
    fila.sort(key=lambda item: (posicao[item["tier"]], item["ordem"], TEMPERATURA_ORDEM.get(item["cliente"].temperatura, 1),
                                 item["cliente"].fazenda))
    # Todos os leads sem outro motivo (inclusive fora do lote, ou com o primeiro
    # contato desligado) ficam fora da fila de cadastro: o proximo passo deles e
    # o primeiro contato, onde o vendedor levanta area/cidade -- senao os 135
    # leads da Monica que nao couberam no lote inflavam a fila de cadastro.
    com_outro_motivo = {i["cliente"].id for i in fila if i["tier"] != 7}
    ids_leads = set(leads) - com_outro_motivo
    # Lotes: "sem contato" e "primeiro contato" entram aos poucos (o Recier tem 440 leads)
    limites = {6: config.valor("fila_sem_contato_lote"), 7: config.valor("fila_primeiro_contato_lote")}
    vistos = Counter()
    cortada = []
    for item in fila:
        t = item["tier"]
        if t in limites:
            vistos[t] += 1
            if vistos[t] > limites[t]:
                continue
        cortada.append(item)
    fila = cortada
    if user.role == "admin":
        fila = [{"cliente": c, "tier": -1, "cor": COR_TIER[-1], "etiqueta": "Contato a resolver", "ordem": 0,
                 "sub": " · ".join(x for x in (c.motivo_ajuda or "Contato não funciona", c.vendedor_nome, _local_cliente(c)) if x),
                 "texto": f"{c.motivo_ajuda or 'Contato não funciona'} — consiga outro número ou meio de contato"
                          + (f" para {c.vendedor_nome}" if c.vendedor_nome else ""),
                 "link": f"/crm/cliente/{c.id}?via=fila"}
                for c in sorted(contato_a_resolver, key=lambda c: c.fazenda)] + fila
    return fila, clientes_query, pedidos_vencidos_por_cliente, mes_atual, mes_nome, ids_leads


# Tipos de registro que provam contato real com o cliente (o "Importado da
# planilha" e artefato da importacao, nao conta).
TIPOS_CONTATO_REAL = ("nota", "proposta", "pedido", "compra", "retirada", "destino_final",
                      "pedido_cancelado", "pedido_finalizado")
ORDEM_FASE_BASE = {"realizado": 0, "proposta": 1, "contactado": 2, "a_contactar": 3}
CAMPO_CURTO = {"telefone": "Telefone", "telefone (formato inválido)": "Telefone",
               "área plantada": "Área", "cidade": "Cidade", "proprietario": "Proprietário"}
COR_TIER = {-1: "red", 0: "red", 1: "red", 2: "clay", 3: "blue", 4: "gold", 5: "blue", 6: "mineral", 7: "green", 8: "gold"}


def _local_cliente(c):
    return f"{c.cidade}, {c.uf}" if c.cidade else c.uf


def _historico_de_contato(db, ids):
    """(clientes com contato real registrado, clientes que ja avancaram de fase)."""
    if not ids:
        return set(), set()
    com_contato = {cid for (cid,) in db.query(ContatoCRM.cliente_id)
                   .filter(ContatoCRM.cliente_id.in_(ids), ContatoCRM.tipo.in_(TIPOS_CONTATO_REAL),
                           ~ContatoCRM.texto.like("Importado da planilha%")).distinct()}
    ja_avancou = {cid for (cid,) in db.query(ContatoCRM.cliente_id)
                  .filter(ContatoCRM.cliente_id.in_(ids), ContatoCRM.tipo == "mudanca_fase",
                          ContatoCRM.fase_destino.notin_(["a_contactar"])).distinct()}
    return com_contato, ja_avancou


# Criterios de ordem da fila de cadastro (a ordem e o liga/desliga vem da pagina Regras)
CRITERIOS_CADASTRO = {
    "sem_telefone": lambda c, sem_telefone: 0 if sem_telefone else 1,   # sem telefone valido nem da pra ligar
    "ja_comprou": lambda c, sem_telefone: ORDEM_FASE_BASE.get(c.fase, 9),  # quem ja comprou primeiro
    "maior_area": lambda c, sem_telefone: -(c.area_plantada_ha or 0),
}


def _montar_fila_base(clientes, ids_fila_contatos):
    """Fila de atualizacao de cadastro (Rafael, 2026-09-25): trabalho de
    "quando der tempo", separado da fila de trabalho. Foco no cliente, nao no
    que falta -- o campo faltante vira so uma etiqueta discreta. Ordem: sem
    telefone valido (nem da pra ligar), depois quem ja comprou, maior area."""
    criterios = [k for k in config.valor("ordem_cad") if config.ligada(f"liga_cad_{k}")]  # pagina Regras
    itens = []
    for c in clientes:
        if c.fase in ("perdido", "nao_usara") or c.id in ids_fila_contatos:
            continue
        faltando = c.campos_faltando()
        if not faltando:
            continue
        sem_telefone = any(f.startswith("telefone") for f in faltando)
        sub = f"{_local_cliente(c)} · {FASE_LABEL.get(c.fase, c.fase)}"
        if c.area_plantada_ha:
            sub += f" · {c.area_plantada_ha:,.0f} ha".replace(",", ".")
        itens.append({"cliente": c, "cor": "grey", "sub": sub,
                      "etiqueta": " · ".join(dict.fromkeys(CAMPO_CURTO.get(f, f) for f in faltando)),
                      "link": f"/crm/cliente/{c.id}?via=base",
                      "ordem": tuple(CRITERIOS_CADASTRO[k](c, sem_telefone) for k in criterios) + (c.fazenda,)})
    itens.sort(key=lambda i: i["ordem"])
    return itens


@router.get("/vendedor/crm", response_class=HTMLResponse)
def crm_home_vendedor(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    garantir_ciclo_atualizado(db)
    fila, clientes_query, _, mes_atual, _, ids_leads = _montar_fila_trabalho(db, user)
    total_clientes = len(clientes_query)
    fila_total = len(fila)

    # Contador regressivo pro novo ciclo de vendas (Rafael, 2026-09-24):
    # so mostra em outubro (<=31 dias faltando), pra nao virar ruido o ano
    # inteiro. Ideia que ja estava no mockup original do painel-vendedor.html
    # ("45 DIAS P/ SAFRA 27/28") e nunca tinha sido construida de verdade.
    dias_ate_ciclo = dias_para_virada_ciclo()
    proximo_ciclo = ciclo_rotulo(dt.date.today() + dt.timedelta(days=dias_ate_ciclo))

    avisos_nao_lidos = _query_avisos(db, user).filter_by(lido=False).count()
    fila_base = _montar_fila_base(clientes_query, {i["cliente"].id for i in fila} | ids_leads)
    hoje = dt.date.today()
    comeco_ciclo = dt.datetime.combine(inicio_ciclo(hoje), dt.time.min)  # mesma data da regra do ciclo
    novos_no_ciclo = sum(1 for c in clientes_query if c.criado_em and c.criado_em >= comeco_ciclo)
    menu.guardar_contadores(user, fila_total, len(fila_base), avisos_nao_lidos)

    from . import inicio
    titulo, data_extenso = inicio.saudacao(user)
    # Tarefas (Rafael, 2026-10-04): prioridade maxima = comprovante do pedido a vista; depois os motivos da fila na
    # ordem da pagina Regras (sem "Contato a resolver": esse esta com o admin) e os cadastros
    cont = Counter(i["tier"] for i in fila if i["tier"] != -1)
    posicao = _posicao_tier()
    tarefas = []
    for t in sorted(cont, key=lambda t: posicao[t]):
        muitos, um, efeito, nivel, botao = VERBO_FILA[TIER_CHAVE[t]]
        tarefas.append({"nivel": nivel, "acao": muitos, "acao_um": um, "botao": botao, "n": cont[t], "efeito": efeito,
                        "link": "/vendedor/crm/fila", "texto": f"{rotulo_tier(t)}: {cont[t]} cliente{'s' if cont[t] != 1 else ''} na fila de trabalho."})
    cadastro = {"nivel": "atencao", "acao": "Completar cadastros", "acao_um": "Completar cadastro", "botao": "Completar agora",
                "n": len(fila_base),
                "efeito": "Base do market share", "link": "/vendedor/crm/base",
                "texto": "Clientes sem área plantada, telefone ou outro dado do cadastro."} if fila_base else None
    entressafra = mes_atual in ENTRESSAFRA_MESES
    if cadastro:
        tarefas = ([cadastro] + tarefas) if entressafra else (tarefas[:2] + [cadastro] + tarefas[2:])
    mostrar = tarefas[:3]  # com o botao, cabem 3 cartoes lado a lado; o resto vira "e mais" no rodape
    resto = sum(t["n"] for t in tarefas[3:])
    rodape = ("Entressafra: foco em completar cadastros, fazer o 1º contato com os leads e não deixar ninguém passar de "
              f"{config.valor('fila_sem_contato_dias')} dias sem contato." if entressafra else None)
    if resto:
        rodape = (rodape + " · " if rodape else "") + f"e mais {resto} na fila de trabalho"
    ids = [c.id for c in clientes_query]
    pedidos_carteira = (db.query(PedidoCRM).filter(PedidoCRM.cliente_id.in_(ids), PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all()
                        if ids else [])

    return _templates(request).TemplateResponse(request, "crm_vendedor_home.html", {
        "urgente": inicio.cartao_enviar_comprovante(inicio.pedidos_sem_pagamento_do_vendedor(db, user)),
        "tarefas": mostrar, "rodape": rodape, "primeiros": [i for i in fila if i["tier"] != -1][:4],
        "comissao": comissao.resumo(db, pedidos_carteira, hoje.replace(day=1), hoje),
        "reais": inicio.reais, "reais_compacto": inicio.reais_compacto,
        "user": user, "total_clientes": total_clientes, "avisos_nao_lidos": avisos_nao_lidos,
        "entressafra": mes_atual in ENTRESSAFRA_MESES, "novos_no_ciclo": novos_no_ciclo,
        "fila_total": fila_total, "fila_resumo": _resumo_fila(fila),
        "fila_base_total": len(fila_base), "fila_base_resumo": _resumo_cadastro(fila_base),
        "dias_ate_ciclo": (dias_ate_ciclo if config.ligada("liga_ciclo_contador")
                           and dias_ate_ciclo <= config.valor("ciclo_contador_dias") else None), "proximo_ciclo": proximo_ciclo,
        "saudacao": titulo, "data_extenso": data_extenso,
        "kpis": _indicadores_home(db, clientes_query, hoje),
        "migalhas": [("Início", None)],
    })


# Tarefas do Inicio do vendedor: verbo (varios, um), o que trava e o nivel (vermelho = trava a venda/retirada)
VERBO_FILA = {  # (varios, um, o que trava, nivel, botao)
    "vencido": ("Resolver pedidos vencidos", "Resolver pedido vencido", "Prazo estourado", "trava", "Resolver agora"),
    "apertada": ("Acelerar retiradas", "Acelerar retirada", "Não fecha no prazo", "trava", "Acelerar agora"),
    "oportunidade": ("Oferecer o frete da região", "Oferecer o frete da região", "Caminhão indo para lá", "atencao", "Oferecer agora"),
    "pedido_aberto": ("Acompanhar pedidos em aberto", "Acompanhar pedido em aberto", "Saldo a retirar", "atencao", "Ver agora"),
    "quente": ("Fechar clientes quentes", "Fechar cliente quente", "Pronto para comprar", "atencao", "Ligar agora"),
    "epoca": ("Ligar na época de compra", "Ligar na época de compra", "Mês de compra do cliente", "atencao", "Ligar agora"),
    "proposta": ("Retomar propostas", "Retomar proposta", "Proposta parada", "atencao", "Retomar agora"),
    "sem_contato": ("Retomar contato", "Retomar contato", "Muito tempo sem falar", "atencao", "Ligar agora"),
    "primeiro_contato": ("Fazer o 1º contato", "Fazer o 1º contato", "Lead ainda sem contato", "atencao", "Ligar agora"),
}


def rotulo_tier(t):
    """Nome do motivo (o admin muda na pagina Regras)."""
    return "Contato a resolver" if t == -1 else config.valor(f"nome_fila_{TIER_CHAVE[t]}")


def _resumo_fila(fila):
    """Quantos clientes por motivo, na ordem de prioridade (cartao da home)."""
    cont = Counter(i["tier"] for i in fila)
    posicao = _posicao_tier()
    return [{"rotulo": rotulo_tier(t), "qtd": n, "cor": COR_TIER[t]} for t, n in sorted(cont.items(), key=lambda x: posicao[x[0]])]


def _resumo_cadastro(fila_base):
    """Quantos clientes sem cada campo (um cliente pode faltar mais de um)."""
    cont = Counter(campo for i in fila_base for campo in i["etiqueta"].split(" · "))
    return [{"rotulo": f"Sem {campo.lower()}", "qtd": n, "cor": "grey"} for campo, n in cont.most_common()]


DIAS_SEMANA_PT = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
MESES_EXTENSO = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                 "setembro", "outubro", "novembro", "dezembro"]


def _reais_compacto(valor):
    if valor >= 1_000_000:
        return "R$ " + f"{valor / 1_000_000:.1f}".replace(".", ",") + " mi"
    if valor >= 1_000:
        return f"R$ {valor / 1_000:.0f} mil"
    return f"R$ {valor:.0f}"


def _indicadores_home(db, clientes, hoje):
    """4 indicadores da tela inicial, com a MESMA conta do Meu desempenho
    (ultimos 30 dias). Variacao so na cobertura, contra os 30 dias anteriores:
    ela depende do esforco do vendedor. Volume mes a mes engana num negocio
    sazonal (agosto sempre vende mais que setembro) -- a comparacao justa e
    com o mesmo periodo da safra passada, que ainda nao existe no sistema."""
    ids = [c.id for c in clientes]
    por_id = {c.id: c for c in clientes}

    def periodo(dias_fim, dias_inicio):
        return (dt.datetime.combine(hoje - dt.timedelta(days=dias_inicio), dt.time.min),
                dt.datetime.combine(hoje - dt.timedelta(days=dias_fim), dt.time.max))

    tocados, vendas_ids, _ = _atividade_no_periodo(db, ids, *periodo(0, 30))
    tocados_ant, _, _ = _atividade_no_periodo(db, ids, *periodo(31, 61))
    total = len(ids)
    cobertura = (len(tocados) / total * 100) if total else 0
    cobertura_ant = (len(tocados_ant) / total * 100) if total else 0
    propostas, pedidos = _pipeline_atual(db, ids)
    return {
        "total": total, "tocados": len(tocados), "cobertura": round(cobertura),
        "cobertura_delta": round(cobertura - cobertura_ant) if total else None,
        "vendas": len(vendas_ids), "volume_vendido": sum((por_id[cid].volume_contratado or 0) for cid in vendas_ids),
        "pedidos": len(pedidos), "volume_a_retirar": sum(p.volume_total_a_entregar() for p in pedidos),
        "pedidos_vencidos": sum(1 for p in pedidos if p.vencido()),
        "propostas": len(propostas), "valor_propostas": _reais_compacto(sum(p.valor_total() for p in propostas)),
    }


def _query_avisos(db, user):
    """Vendedor ve so os avisos da carteira dele; admin ve todos."""
    query = db.query(AvisoCRM)
    if user.role == "vendedor":
        query = query.filter(AvisoCRM.vendedor_nome == user.vendedor_nome)
    return query


@router.get("/vendedor/crm/avisos", response_class=HTMLResponse)
def crm_avisos(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    avisos = _query_avisos(db, user).order_by(AvisoCRM.criado_em.desc()).limit(200).all()
    return _templates(request).TemplateResponse(request, "crm_avisos.html", {
        "user": user, "avisos": avisos, "nao_lidos": sum(1 for a in avisos if not a.lido),
        "migalhas": [("Início", "/vendedor/crm"), ("Avisos", None)],
    })


@router.get("/vendedor/crm/avisos/{aviso_id}/abrir")
def crm_abrir_aviso(aviso_id: int, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    aviso = _query_avisos(db, user).filter(AvisoCRM.id == aviso_id).first()
    if not aviso:
        return RedirectResponse("/vendedor/crm/avisos", status_code=303)
    if not aviso.lido:
        aviso.lido = True
        aviso.lido_em = dt.datetime.utcnow()
        db.commit()
    if aviso.link:
        return RedirectResponse(aviso.link, status_code=303)
    if not aviso.cliente_id:
        return RedirectResponse("/vendedor/crm/avisos", status_code=303)
    aba = "comentario" if aviso.tipo == "cliente_novo" else "pedidos"
    return RedirectResponse(f"/crm/cliente/{aviso.cliente_id}?aba={aba}&via=avisos", status_code=303)


@router.post("/vendedor/crm/avisos/marcar-todos")
def crm_marcar_avisos_lidos(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    agora = dt.datetime.utcnow()
    for aviso in _query_avisos(db, user).filter_by(lido=False).all():
        aviso.lido = True
        aviso.lido_em = agora
    db.commit()
    avisar_sucesso(request, "Todos os avisos marcados como lidos.")
    return RedirectResponse("/vendedor/crm/avisos", status_code=303)


@router.get("/vendedor/crm/base", response_class=HTMLResponse)
def crm_fila_base_vendedor(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    fila, clientes_query, _pv, _mes_atual, _mes_nome, ids_leads = _montar_fila_trabalho(db, user)
    itens = _montar_fila_base(clientes_query, {i["cliente"].id for i in fila} | ids_leads)
    return _templates(request).TemplateResponse(request, "crm_fila_base.html", {
        "user": user, "itens": itens,
        "migalhas": [("Início", "/vendedor/crm"), ("Fila de atualização de cadastro", None)],
    })


@router.get("/vendedor/crm/fila", response_class=HTMLResponse)
def crm_fila_trabalho_vendedor(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    fila, _clientes_query, _pv, _mes_atual, _mes_nome, _ids_leads = _montar_fila_trabalho(db, user)
    return _templates(request).TemplateResponse(request, "crm_fila_trabalho.html", {
        "user": user, "fila": fila,
        "migalhas": [("Início", "/vendedor/crm"), ("Fila de trabalho", None)],
    })


def _e_artefato_import(tipo, texto):
    return tipo == "nota" and texto and "Importado da planilha" in texto


def _atividade_no_periodo(db, ids_clientes, inicio_dt, fim_dt):
    """Quem foi tocado, vendido e perdido no periodo -- base do Meu desempenho
    e dos indicadores da tela inicial (mesma conta nas duas telas)."""
    tocados_ids, vendas_ids, perdidos_ids = set(), set(), set()
    if not ids_clientes:
        return tocados_ids, vendas_ids, perdidos_ids
    # Cobertura: clientes com pelo menos 1 atividade de verdade no periodo
    # (qualquer tipo, menos o "nota: Importado" que e artefato do import, nao
    # atividade do vendedor).
    for cid, tipo, texto in (db.query(ContatoCRM.cliente_id, ContatoCRM.tipo, ContatoCRM.texto)
                                .filter(ContatoCRM.cliente_id.in_(ids_clientes),
                                        ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt).all()):
        if not _e_artefato_import(tipo, texto):
            tocados_ids.add(cid)
    # Vendas e perdas no periodo (mudanca_fase pra realizado/perdido).
    for cid, destino in (db.query(ContatoCRM.cliente_id, ContatoCRM.fase_destino)
                            .filter(ContatoCRM.cliente_id.in_(ids_clientes), ContatoCRM.tipo == "mudanca_fase",
                                    ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt).all()):
        if destino == "realizado":
            vendas_ids.add(cid)
        elif destino == "perdido":
            perdidos_ids.add(cid)
    return tocados_ids, vendas_ids, perdidos_ids


def _pipeline_atual(db, ids_clientes):
    """Propostas e pedidos em aberto agora (nao dependem do periodo) -- mesma
    logica do relatorio do admin. Pedido aberto = ainda nao teve NENHUMA
    retirada registrada nesse app; mesmo principio da Logistica pra saber
    quantos estao com o prazo de retirada vencido."""
    if not ids_clientes:
        return [], []
    propostas = (db.query(PropostaCRM)
                   .filter(PropostaCRM.cliente_id.in_(ids_clientes), PropostaCRM.status == STATUS_PROPOSTA_ABERTA)
                   .all())
    pedidos = (db.query(PedidoCRM)
                 .filter(PedidoCRM.cliente_id.in_(ids_clientes), PedidoCRM.status == STATUS_PEDIDO_ABERTO)
                 .all())
    return propostas, pedidos


@router.get("/vendedor/crm/desempenho", response_class=HTMLResponse)
def crm_desempenho_vendedor(request: Request, data_de: str = "", data_ate: str = "", vendedor: str = "",
                             user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    hoje = dt.date.today()
    data_ate_d = dt.datetime.strptime(data_ate, "%Y-%m-%d").date() if data_ate else hoje
    data_de_d = dt.datetime.strptime(data_de, "%Y-%m-%d").date() if data_de else (data_ate_d - dt.timedelta(days=30))
    inicio_dt = dt.datetime.combine(data_de_d, dt.time.min)
    fim_dt = dt.datetime.combine(data_ate_d, dt.time.max)

    query = db.query(ClienteCRM)
    vendedores = []
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    else:
        # admin ve a empresa toda ou um vendedor (antes ficava no "Mapa do CRM")
        vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct() if v})
        if vendedor not in vendedores:
            vendedor = ""
        if vendedor:
            query = query.filter(ClienteCRM.vendedor_nome == vendedor)
    clientes = query.all()
    total_carteira = len(clientes)
    ids_clientes = [c.id for c in clientes]
    clientes_por_id = {c.id: c for c in clientes}

    tocados_ids, vendas_ids, perdidos_ids = _atividade_no_periodo(db, ids_clientes, inicio_dt, fim_dt)
    tocados = len(tocados_ids)
    cobertura_pct = (tocados / total_carteira * 100) if total_carteira else 0
    vendas = len(vendas_ids)
    perdidos = len(perdidos_ids)
    conversao_pct = (vendas / tocados * 100) if tocados else 0
    perda_pct = (perdidos / tocados * 100) if tocados else 0

    # Funil: quantos avancaram pra cada fase no periodo (mesma logica do
    # relatorio que o admin ja tinha em /crm).
    progressao = {f: 0 for f in FASES_CRM}
    if ids_clientes:
        for fase_destino, qtd in (db.query(ContatoCRM.fase_destino, func.count(ContatoCRM.id))
                                     .filter(ContatoCRM.cliente_id.in_(ids_clientes), ContatoCRM.tipo == "mudanca_fase",
                                             ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt)
                                     .group_by(ContatoCRM.fase_destino).all()):
            if fase_destino in progressao:
                progressao[fase_destino] = qtd

    propostas_abertas, pedidos_abertos = _pipeline_atual(db, ids_clientes)
    valor_pipeline = sum(p.valor_total() for p in propostas_abertas)
    volume_pedidos_abertos = sum(p.volume_total_a_entregar() for p in pedidos_abertos)
    pedidos_vencidos = [p for p in pedidos_abertos if p.vencido()]

    # Volume vendido no periodo e ticket medio (toneladas) -- usa o campo
    # resumo `volume_contratado` (mantido por _recalcular_resumo_comercial
    # pros clientes com PedidoCRM novo, e vem direto da planilha pros
    # importados). Nao tem valor em R$ consolidado confiavel pra vendas
    # antigas, entao o indicador financeiro fica só no pipeline aberto.
    volume_vendido_periodo = sum((clientes_por_id[cid].volume_contratado or 0) for cid in vendas_ids)
    ticket_medio_ton = (volume_vendido_periodo / vendas) if vendas else 0

    # Ciclo medio de venda: dias entre o INICIO do relacionamento comercial de
    # verdade (primeiro contato/proposta/mudanca de fase) e a venda -- só pros
    # clientes que venderam dentro do periodo escolhido. "compra"/"retirada"
    # sao fatos de COMPRAS PASSADAS do cliente (historico antigo importado da
    # planilha, as vezes de anos atras, e sem data confiavel -- cai na
    # sentinela DATA_DESCONHECIDA), nao atividade do vendedor nesse ciclo de
    # venda -- por isso ficam de fora do calculo, senao o "ciclo" vira uma
    # distancia ate um registro de compra antigo sem relacao nenhuma com o
    # trabalho do vendedor agora.
    ciclos_dias = []
    for cid in vendas_ids:
        inicio_real, fim_venda = None, None
        for data, tipo, texto, fase_destino in (db.query(ContatoCRM.data, ContatoCRM.tipo, ContatoCRM.texto, ContatoCRM.fase_destino)
                                                    .filter(ContatoCRM.cliente_id == cid)
                                                    .order_by(ContatoCRM.data.asc()).all()):
            if _e_artefato_import(tipo, texto):
                continue
            if tipo in ("compra", "retirada"):
                continue
            if data == DATA_DESCONHECIDA:
                continue
            if inicio_real is None:
                inicio_real = data
            if tipo == "mudanca_fase" and fase_destino == "realizado":
                fim_venda = data
        if inicio_real and fim_venda and fim_venda > inicio_real:
            ciclos_dias.append((fim_venda - inicio_real).days)
    ciclo_medio_dias = (sum(ciclos_dias) / len(ciclos_dias)) if ciclos_dias else None
    ciclo_amostra = len(ciclos_dias)
    via_parceiro = _resumo_via_parceiro(db, ids_clientes, inicio_dt, fim_dt)
    base = _resumo_base(db, clientes, inicio_dt, fim_dt)

    # Comissao (Rafael, 2026-10-04): pedidos do portal da carteira, o que mexeu
    # por ultimo primeiro (pagamento recebido ou pedido novo)
    pedidos_comissao = (db.query(PedidoCRM).filter(PedidoCRM.cliente_id.in_(ids_clientes), PedidoCRM.status != STATUS_PEDIDO_CANCELADO)
                          .all() if ids_clientes else [])
    pedidos_comissao.sort(key=lambda p: max([p.criado_em.date()] + [r.data for r in p.recebimentos]), reverse=True)
    return _templates(request).TemplateResponse(request, "crm_desempenho_vendedor.html", {
        "user": user, "data_de": data_de_d, "data_ate": data_ate_d, "vendedores": vendedores, "filtro_vendedor": vendedor,
        "total_carteira": total_carteira, "tocados": tocados, "cobertura_pct": cobertura_pct,
        "vendas": vendas, "conversao_pct": conversao_pct, "ciclo_amostra": ciclo_amostra,
        "perdidos": perdidos, "perda_pct": perda_pct,
        "progressao": progressao, "valor_pipeline": valor_pipeline,
        "pedidos_abertos": len(pedidos_abertos), "volume_pedidos_abertos": volume_pedidos_abertos,
        "pedidos_vencidos": len(pedidos_vencidos),
        "volume_vendido_periodo": volume_vendido_periodo, "ticket_medio_ton": ticket_medio_ton,
        "ciclo_medio_dias": ciclo_medio_dias, "via_parceiro": via_parceiro, "base": base,
        "comissao": comissao.resumo(db, pedidos_comissao, data_de_d, data_ate_d), "pedidos_comissao": pedidos_comissao[:12],
        "n_pedidos_comissao": len(pedidos_comissao), "reais": comissao.reais, "situacao_comissao": comissao.situacao, "decimal_br": config.decimal_br,
        "migalhas": [("Início", "/vendedor/crm"), ("Meu desempenho", None)],
    })


@router.get("/vendedor/crm/pedidos", response_class=HTMLResponse)
def crm_pedidos_vendedor(request: Request, vencidos: str = "",
                          user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(ClienteCRM)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    ids_clientes = [c.id for c in query.all()]

    pedidos = []
    if ids_clientes:
        pedidos = (db.query(PedidoCRM)
                     .filter(PedidoCRM.cliente_id.in_(ids_clientes), PedidoCRM.status == STATUS_PEDIDO_ABERTO)
                     .all())
    total_abertos = len(pedidos)
    if vencidos == "1":
        pedidos = [p for p in pedidos if p.vencido()]

    # Mesma prioridade da Logistica: vencido primeiro (mais atrasado primeiro),
    # depois por Ton/Dia Necessario (maior urgencia primeiro), depois sem
    # prazo nenhum (mais antigo primeiro).
    def chave_ordenacao(p):
        if p.vencido():
            return (0, p.dias_restantes() or 0)
        ton_dia = p.ton_dia_necessario()
        if ton_dia is not None:
            return (1, -ton_dia)
        return (2, p.criado_em.toordinal() if p.criado_em else 0)
    pedidos.sort(key=chave_ordenacao)

    return _templates(request).TemplateResponse(request, "crm_pedidos_vendedor.html", {
        "user": user, "pedidos": pedidos, "total_abertos": total_abertos, "filtro_vencidos": vencidos,
        "migalhas": [("Início", "/vendedor/crm"), ("Pedidos em aberto", None)],
    })


@router.get("/vendedor/crm/carteira", response_class=HTMLResponse)
def crm_carteira_vendedor(request: Request, cidade: str = "", ordenar: str = "", pendentes: str = "",
                           incompleto: str = "",
                           user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(ClienteCRM)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes = query.all()

    contagem = _resumo_fases(db.query(ClienteCRM).filter(ClienteCRM.id.in_([c.id for c in clientes])) if clientes else query)
    total = len(clientes)
    fases = [{
        "chave": f, "label": FASE_LABEL[f], "cor": FASE_COR[f], "qtd": contagem.get(f, 0),
        "pct": (contagem.get(f, 0) / total * 100) if total else 0,
    } for f in FASES_CRM]

    por_estado = {}
    for c in clientes:
        info = por_estado.setdefault(c.uf, {"clientes": 0, "area": 0.0})
        info["clientes"] += 1
        info["area"] += c.area_plantada_ha or 0

    # Distribuicao por forma de pagamento (Rafael, 2026-09-25): "entender
    # qual % da carteira compra a vista, quantos com prazo e quantos plano
    # safra". "A vista" conta tanto o valor novo quanto "À vista" (com
    # acento, como veio da importacao original da planilha) -- mesmo
    # significado, so grafias diferentes ao longo do tempo.
    contagem_pgto = {}
    for c in clientes:
        if not c.forma_pagamento:
            continue
        chave = "A vista" if c.forma_pagamento == "À vista" else c.forma_pagamento
        contagem_pgto[chave] = contagem_pgto.get(chave, 0) + 1
    nao_classificados = total - sum(contagem_pgto.values())
    forma_pagamento_dist = [{
        "label": opcao, "qtd": contagem_pgto.get(opcao, 0),
        "pct": (contagem_pgto.get(opcao, 0) / total * 100) if total else 0,
    } for opcao in FORMA_PAGAMENTO_OPCOES]

    # Visao geral com TODOS os clientes da carteira, sem filtrar por fase --
    # mesma ideia ja existente na tela do admin (/crm/estado/{uf}), so que
    # escopada aos clientes do proprio vendedor (ou de todos, se for admin
    # olhando essa tela).
    cidades = sorted({c.cidade for c in clientes if c.cidade})
    clientes_lista = _ordenar_e_filtrar(list(clientes), cidade, ordenar, pendentes == "1", incompleto == "1")

    return _templates(request).TemplateResponse(request, "crm_carteira_vendedor.html", {
        "user": user, "total": total, "fases": fases, "por_estado": por_estado,
        "forma_pagamento_dist": forma_pagamento_dist, "nao_classificados": nao_classificados,
        "clientes": clientes_lista, "cidades": cidades, "fase": None,
        "fase_label_dict": FASE_LABEL, "fase_cor_dict": FASE_COR, "ficha_base_url": "/crm/cliente",
        "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes, "incompleto": incompleto},
        "migalhas": [("Início", "/vendedor/crm"), ("Carteira", None)],
    })


@router.get("/vendedor/crm/fase/{fase}", response_class=HTMLResponse)
def crm_lista_vendedor(request: Request, fase: str, cidade: str = "", ordenar: str = "", pendentes: str = "",
                        incompleto: str = "",
                        user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(ClienteCRM).filter(ClienteCRM.fase == fase)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes = query.all()
    cidades = sorted({c.cidade for c in clientes if c.cidade})
    clientes = _ordenar_e_filtrar(clientes, cidade, ordenar, pendentes == "1", incompleto == "1")
    return _templates(request).TemplateResponse(request, "crm_lista_clientes.html", {
        "user": user, "uf": None, "fase": fase, "fase_label": FASE_LABEL.get(fase, fase),
        "clientes": clientes, "cidades": cidades, "voltar_url": "/vendedor/crm/carteira",
        "ficha_base_url": "/crm/cliente", "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes, "incompleto": incompleto},
        "migalhas": _migalhas_fase_vendedor(fase),
    })


@router.get("/vendedor/crm/agenda", response_class=HTMLResponse)
def crm_agenda(request: Request, ano: int = None, mes: int = None,
               dia: int = None, user: User = Depends(require_role("admin", "vendedor")),
               db: Session = Depends(get_db)):
    import calendar as calendar_mod

    hoje = dt.date.today()
    ano = ano or hoje.year
    mes = mes or hoje.month

    query = db.query(ClienteCRM).filter(ClienteCRM.proximo_retorno_em.isnot(None))
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes = query.all()

    retornos_por_dia = {}
    for c in clientes:
        if c.proximo_retorno_em.year == ano and c.proximo_retorno_em.month == mes:
            retornos_por_dia.setdefault(c.proximo_retorno_em.day, []).append(c)

    cal = calendar_mod.Calendar(firstweekday=6)  # domingo primeiro
    semanas = cal.monthdayscalendar(ano, mes)

    dia_selecionado = dia
    clientes_do_dia = retornos_por_dia.get(dia_selecionado, []) if dia_selecionado else []

    mes_anterior = (ano, mes - 1) if mes > 1 else (ano - 1, 12)
    mes_seguinte = (ano, mes + 1) if mes < 12 else (ano + 1, 1)

    return _templates(request).TemplateResponse(request, "crm_agenda.html", {
        "user": user, "ano": ano, "mes": mes, "mes_nome": MESES_PT[mes - 1],
        "semanas": semanas, "retornos_por_dia": retornos_por_dia, "hoje": hoje,
        "total_retornos": len(clientes), "dia_selecionado": dia_selecionado,
        "clientes_do_dia": clientes_do_dia,
        "mes_anterior": mes_anterior, "mes_seguinte": mes_seguinte,
        "migalhas": [("Início", "/vendedor/crm"), ("Agenda", None)],
    })
