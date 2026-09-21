import datetime as dt
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import get_current_user, require_role
from .database import get_db
from .models import (ESTADOS_OPERACAO, FASE_COR, FASE_LABEL, FASES_CRM, MESES_PT, PRODUTOS,
                      RESULTADO_CONTATO, STATUS_PEDIDO_ABERTO, STATUS_PEDIDO_CANCELADO,
                      STATUS_PROPOSTA_ABERTA, STATUS_PROPOSTA_CONVERTIDA, SUBSIDIARIAS, AreaEstado,
                      ClienteCRM, ContatoAdicionalCRM, ContatoCRM, PedidoCRM, PropostaCRM, User,
                      calcular_avanco_fase)

router = APIRouter()

NOME_ESTADO = {
    "MA": "Maranhao", "PA": "Para", "TO": "Tocantins",
    "PI": "Piaui", "MT": "Mato Grosso", "GO": "Goias",
}

TIPO_LOG_LABEL = {
    "nota": "Contato", "mudanca_fase": "Mudanca de fase", "compra": "Compra",
    "retirada": "Retirada", "proposta": "Proposta", "agenda": "Agendamento", "dados": "Cadastro",
    "pedido": "Venda realizada", "pedido_cancelado": "Pedido cancelado",
}
TIPO_LOG_COR = {
    "nota": "", "mudanca_fase": "gold", "compra": "green",
    "retirada": "green", "proposta": "blue", "agenda": "mineral", "dados": "",
    "pedido": "green", "pedido_cancelado": "red",
}

PROPOSTA_NUMERO_BASE = 78300
PEDIDO_NUMERO_BASE = 105600


def _templates(request: Request):
    return request.app.state.templates


def encontrar_cliente_crm(db, nome_cliente_pedido):
    """Tentativa de achar o registro do CRM correspondente a um cliente de
    pedido (nao ha ainda um vinculo real tipo a aba 'Netsuite' da planilha
    Controle -- isso e so um match de nome, aproximado). Casa palavra por
    palavra (em vez da string inteira) pra tolerar diferencas de espacamento
    e pontuacao entre as duas bases, que sao reais (ex: espaco duplo)."""
    nome = re.sub(r"^\d+\s*", "", nome_cliente_pedido or "").strip()
    nome = nome.split(" - ")[0].strip()
    palavras = [p for p in re.split(r"\s+", nome) if len(p) > 2]
    if not palavras:
        return None
    padrao = "%" + "%".join(palavras) + "%"
    return db.query(ClienteCRM).filter(
        (ClienteCRM.fazenda.ilike(padrao)) | (ClienteCRM.empresa.ilike(padrao))
    ).first()


def _resumo_fases(query):
    """Conta clientes por fase para uma query ja filtrada (por uf e/ou vendedor)."""
    contagem = {f: 0 for f in FASES_CRM}
    for cliente in query.all():
        contagem[cliente.fase] = contagem.get(cliente.fase, 0) + 1
    return contagem


def _cliente_do_usuario(db, user, cliente_id):
    """Busca o cliente respeitando a carteira do vendedor (admin ve tudo)."""
    cliente = db.get(ClienteCRM, cliente_id)
    if cliente and user.role == "vendedor" and cliente.vendedor_nome != user.vendedor_nome:
        return None
    return cliente


def validar_telefone(telefone):
    """Telefone brasileiro: DDD + 8 digitos (fixo) ou DDD + 9 digitos
    (celular) -- 10 ou 11 digitos no total, ignorando qualquer formatacao
    (espaco, traco, parenteses). Retorna mensagem de erro, ou None se valido."""
    digitos = re.sub(r"\D", "", telefone or "")
    if len(digitos) not in (10, 11):
        return "Telefone invalido -- informe DDD + numero (8 digitos se fixo, 9 se celular)."
    return None


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
    cliente.volume_contratado = sum(p.volume for p in pedidos) or None
    cliente.volume_retirado = sum((p.volume_retirado or 0) for p in pedidos) or None


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


@router.get("/crm", response_class=HTMLResponse)
def crm_mapa(request: Request, vendedor: str = "", data_de: str = "", data_ate: str = "",
             user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    vendedores = sorted({
        v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v
    })

    relatorio_vendedor = None
    if vendedor:
        hoje = dt.date.today()
        data_ate_d = dt.datetime.strptime(data_ate, "%Y-%m-%d").date() if data_ate else hoje
        data_de_d = dt.datetime.strptime(data_de, "%Y-%m-%d").date() if data_de else (data_ate_d - dt.timedelta(days=30))
        inicio_dt = dt.datetime.combine(data_de_d, dt.time.min)
        fim_dt = dt.datetime.combine(data_ate_d, dt.time.max)

        clientes_vend = db.query(ClienteCRM).filter(ClienteCRM.vendedor_nome == vendedor).all()
        ids_clientes = [c.id for c in clientes_vend]

        tocados = 0
        progressao = {f: 0 for f in FASES_CRM}
        if ids_clientes:
            tocados = (db.query(ContatoCRM.cliente_id)
                         .filter(ContatoCRM.cliente_id.in_(ids_clientes),
                                 ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt)
                         .distinct().count())
            mudancas = (db.query(ContatoCRM.fase_destino, func.count(ContatoCRM.id))
                          .filter(ContatoCRM.cliente_id.in_(ids_clientes), ContatoCRM.tipo == "mudanca_fase",
                                  ContatoCRM.data >= inicio_dt, ContatoCRM.data <= fim_dt)
                          .group_by(ContatoCRM.fase_destino).all())
            for fase_destino, qtd in mudancas:
                if fase_destino in progressao:
                    progressao[fase_destino] = qtd

        valor_proposta = sum(c.proposta_valor or 0 for c in clientes_vend if c.fase == "proposta")
        volume_vendido = sum(c.volume_contratado or 0 for c in clientes_vend if c.fase == "realizado")
        volume_retirado = sum(c.volume_retirado or 0 for c in clientes_vend if c.fase == "realizado")

        relatorio_vendedor = {
            "vendedor": vendedor, "data_de": data_de_d, "data_ate": data_ate_d,
            "total_clientes": len(clientes_vend), "tocados": tocados, "progressao": progressao,
            "valor_proposta": valor_proposta, "volume_vendido": volume_vendido, "volume_retirado": volume_retirado,
        }

    estados = db.query(AreaEstado).all()
    dados_por_uf = []
    total_clientes = 0
    total_area_capturada = 0.0
    for estado in estados:
        clientes_uf = db.query(ClienteCRM).filter(ClienteCRM.uf == estado.uf).all()
        n_clientes = len(clientes_uf)
        area_capturada = sum(c.area_plantada_ha or 0 for c in clientes_uf)
        pct = (area_capturada / estado.area_agropecuaria_ha * 100) if estado.area_agropecuaria_ha else 0
        dados_por_uf.append({
            "uf": estado.uf, "nome": NOME_ESTADO.get(estado.uf, estado.uf),
            "clientes": n_clientes, "area_capturada": area_capturada,
            "area_total": estado.area_agropecuaria_ha, "pct": pct, "placeholder": estado.placeholder,
        })
        total_clientes += n_clientes
        total_area_capturada += area_capturada
    dados_por_uf.sort(key=lambda d: d["uf"])

    return _templates(request).TemplateResponse(request, "crm_mapa.html", {
        "user": user, "estados": dados_por_uf, "total_clientes": total_clientes,
        "total_area": total_area_capturada,
        "vendedores": vendedores, "fase_label": FASE_LABEL, "relatorio": relatorio_vendedor,
        "filtro_vendedor": vendedor, "filtro_data_de": data_de, "filtro_data_ate": data_ate,
    })


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
    })


@router.get("/crm/estado/{uf}", response_class=HTMLResponse)
def crm_funil_estado(request: Request, uf: str, user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    uf = uf.upper()
    query = db.query(ClienteCRM).filter(ClienteCRM.uf == uf)
    contagem = _resumo_fases(query)
    total = sum(contagem.values())
    estado = db.get(AreaEstado, uf)

    fases = [{
        "chave": f, "label": FASE_LABEL[f], "cor": FASE_COR[f], "qtd": contagem[f],
        "pct": (contagem[f] / total * 100) if total else 0,
    } for f in FASES_CRM]

    return _templates(request).TemplateResponse(request, "crm_funil.html", {
        "user": user, "uf": uf, "fases": fases, "total": total, "estado": estado,
        "voltar_url": "/crm", "base_url": f"/crm/estado/{uf}",
    })


def _ordenar_e_filtrar(clientes, cidade, ordenar, so_pendentes):
    if cidade:
        clientes = [c for c in clientes if c.cidade == cidade]
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
                        pendentes: str = "", user: User = Depends(require_role("admin", "logistica")),
                        db: Session = Depends(get_db)):
    uf = uf.upper()
    clientes = db.query(ClienteCRM).filter(ClienteCRM.uf == uf, ClienteCRM.fase == fase).all()
    cidades = sorted({c.cidade for c in clientes if c.cidade})
    clientes = _ordenar_e_filtrar(clientes, cidade, ordenar, pendentes == "1")
    return _templates(request).TemplateResponse(request, "crm_lista_clientes.html", {
        "user": user, "uf": uf, "fase": fase, "fase_label": FASE_LABEL.get(fase, fase),
        "clientes": clientes, "cidades": cidades, "voltar_url": f"/crm/estado/{uf}",
        "ficha_base_url": "/crm/cliente", "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes},
    })


def _contexto_ficha(db, user, cliente, erro=None, aba="dados"):
    historico = []
    produtos_estado = []
    pedidos = []
    if cliente:
        historico = db.query(ContatoCRM).filter_by(cliente_id=cliente.id).order_by(ContatoCRM.data.desc()).all()
        produtos_estado = [{"produto": p, **_estado_produto(db, cliente.id, p)} for p in PRODUTOS]
        pedidos = db.query(PedidoCRM).filter_by(cliente_id=cliente.id).order_by(PedidoCRM.id.desc()).all()
    return {
        "user": user, "cliente": cliente, "historico": historico,
        "fase_label": FASE_LABEL, "fase_cor": FASE_COR, "resultados": RESULTADO_CONTATO,
        "voltar_url": (f"/crm/estado/{cliente.uf}/fase/{cliente.fase}" if cliente and user.role != "vendedor" else "/vendedor/crm/carteira"),
        "produtos": PRODUTOS, "produtos_estado": produtos_estado, "pedidos": pedidos,
        "erro": erro, "aba_inicial": aba,
    }


def _erro_ficha(request, db, user, cliente, erro, aba="dados"):
    return _templates(request).TemplateResponse(request, "crm_ficha_cliente.html",
        _contexto_ficha(db, user, cliente, erro=erro, aba=aba))


# a rota /crm/cliente/novo tem que vir ANTES de /crm/cliente/{cliente_id} (int) --
# senao o FastAPI tenta casar "novo" com o path param inteiro e da erro 422.
@router.get("/crm/cliente/novo", response_class=HTMLResponse)
def crm_novo_cliente_form(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v})
    return _templates(request).TemplateResponse(request, "crm_novo_cliente.html", {
        "user": user, "vendedores": vendedores, "estados": ESTADOS_OPERACAO,
        "erro": None, "similares": None, "form": {},
    })


@router.post("/crm/cliente/novo")
def crm_novo_cliente(request: Request, fazenda: str = Form(...), cidade: str = Form(...), uf: str = Form(...),
                      proprietario: str = Form(...), telefone: str = Form(...), email: str = Form(""),
                      cnpj: str = Form(""), empresa: str = Form(""), area_plantada_ha: str = Form(""),
                      frota_propria: str = Form(""), coordenadas: str = Form(""),
                      vendedor: str = Form(""), confirmar_similar: str = Form(""),
                      user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    vendedor_nome = user.vendedor_nome if user.role == "vendedor" else (vendedor.strip() or None)
    vendedores = sorted({v for (v,) in db.query(ClienteCRM.vendedor_nome).distinct().all() if v})
    form = {"fazenda": fazenda, "cidade": cidade, "uf": uf, "proprietario": proprietario, "telefone": telefone,
            "email": email, "cnpj": cnpj, "empresa": empresa, "area_plantada_ha": area_plantada_ha,
            "frota_propria": frota_propria, "coordenadas": coordenadas,
            "vendedor": vendedor_nome or ""}

    def _erro(msg, similares=None):
        return _templates(request).TemplateResponse(request, "crm_novo_cliente.html", {
            "user": user, "vendedores": vendedores, "estados": ESTADOS_OPERACAO,
            "erro": msg, "similares": similares, "form": form,
        })

    if not fazenda.strip() or not cidade.strip() or not uf.strip() or not proprietario.strip():
        return _erro("Preencha nome da fazenda, cidade, estado e proprietario.")
    if uf.strip().upper() not in ESTADOS_OPERACAO:
        return _erro("Selecione um estado valido.")

    erro = validar_telefone(telefone)
    if erro:
        return _erro(erro)
    dup = encontrar_telefone_duplicado(db, telefone)
    if dup:
        return _erro(f"Esse telefone ja esta cadastrado para {dup['cliente_nome']}"
                     + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                     + " -- confira se nao e o mesmo cliente antes de cadastrar de novo.")

    if not confirmar_similar:
        similares = encontrar_clientes_similares(db, fazenda, vendedor_nome=vendedor_nome)
        if similares:
            return _erro(None, similares=similares)

    cliente = ClienteCRM(
        fazenda=fazenda.strip(), proprietario=proprietario.strip(), telefone=telefone.strip(),
        email=email.strip() or None, cnpj=cnpj.strip() or None, empresa=empresa.strip() or None,
        cidade=cidade.strip(), uf=uf.strip().upper(),
        frota_propria={"sim": True, "nao": False}.get(frota_propria.strip().lower()),
        coordenadas=coordenadas.strip() or None, fase="a_contactar", vendedor_nome=vendedor_nome,
    )
    try:
        cliente.area_plantada_ha = float(area_plantada_ha) if area_plantada_ha.strip() else None
    except ValueError:
        cliente.area_plantada_ha = None
    db.add(cliente)
    db.flush()
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", texto="Cliente cadastrado."))
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente.id}", status_code=303)


@router.get("/crm/cliente/{cliente_id}", response_class=HTMLResponse)
def crm_ficha_cliente(request: Request, cliente_id: int, aba: str = "dados",
                       user: User = Depends(require_role("admin", "logistica", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    return _templates(request).TemplateResponse(request, "crm_ficha_cliente.html",
        _contexto_ficha(db, user, cliente, aba=aba))


@router.post("/crm/cliente/{cliente_id}/nota")
def crm_add_nota(cliente_id: int, texto: str = Form(...), resultado: str = Form(""),
                  proximo_retorno: str = Form(""),
                  user: User = Depends(require_role("admin", "logistica", "vendedor")),
                  db: Session = Depends(get_db)):
    cliente = db.get(ClienteCRM, cliente_id)
    if cliente and texto.strip():
        prefixo = RESULTADO_CONTATO.get(resultado, "")
        texto_final = f"[{prefixo}] {texto.strip()}" if resultado else texto.strip()
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="nota", texto=texto_final))

        nova_fase = calcular_avanco_fase(cliente.fase, resultado)
        if nova_fase:
            de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL.get(nova_fase, nova_fase)}"
            cliente.fase = nova_fase
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase", texto=f"Etapa avancou automaticamente: {de_para}", fase_destino=nova_fase))

        if proximo_retorno:
            cliente.proximo_retorno_em = dt.datetime.strptime(proximo_retorno, "%Y-%m-%d").date()
            db.add(ContatoCRM(cliente_id=cliente.id, tipo="agenda",
                               texto=f"Novo contato agendado para {cliente.proximo_retorno_em.strftime('%d/%m/%Y')}."))

        cliente.ultima_interacao_em = dt.datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=comentario", status_code=303)


# ---------- cadastro do cliente ----------

@router.get("/crm/cliente/{cliente_id}/editar", response_class=HTMLResponse)
def crm_editar_cliente_form(request: Request, cliente_id: int,
                             user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    return _templates(request).TemplateResponse(request, "crm_editar_cliente.html", {
        "user": user, "cliente": cliente, "erro": None,
    })


@router.post("/crm/cliente/{cliente_id}/editar")
def crm_editar_cliente(request: Request, cliente_id: int, proprietario: str = Form(""), telefone: str = Form(""),
                        email: str = Form(""), cnpj: str = Form(""), empresa: str = Form(""),
                        cidade: str = Form(""), frota_propria: str = Form(""),
                        area_plantada_ha: str = Form(""), coordenadas: str = Form(""),
                        user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if cliente:
        telefone = telefone.strip()
        erro = None
        if not proprietario.strip() or not telefone or not cidade.strip():
            erro = "Proprietario/contato, telefone e cidade sao obrigatorios."
        else:
            erro = validar_telefone(telefone)
            if not erro:
                dup = encontrar_telefone_duplicado(db, telefone, excluir_cliente_id=cliente.id)
                if dup:
                    erro = (f"Esse telefone ja esta cadastrado para {dup['cliente_nome']}"
                            + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                            + " -- confira se nao e o mesmo cliente duplicado.")
        if erro:
            return _templates(request).TemplateResponse(request, "crm_editar_cliente.html", {
                "user": user, "cliente": cliente, "erro": erro,
            })
        cliente.proprietario = proprietario.strip()
        cliente.telefone = telefone
        cliente.email = email.strip() or None
        cliente.cnpj = cnpj.strip() or None
        cliente.empresa = empresa.strip() or None
        cliente.cidade = cidade.strip()
        cliente.frota_propria = {"sim": True, "nao": False}.get(frota_propria.strip().lower())
        try:
            cliente.area_plantada_ha = float(area_plantada_ha) if area_plantada_ha.strip() else None
        except ValueError:
            pass
        cliente.coordenadas = coordenadas.strip() or None
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados", texto="Dados do cliente atualizados."))
        cliente.ultima_interacao_em = dt.datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=dados", status_code=303)


@router.post("/crm/cliente/{cliente_id}/contato-adicional")
def crm_add_contato_adicional(request: Request, cliente_id: int, nome: str = Form(...), funcao: str = Form(...),
                               telefone: str = Form(...),
                               user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if not cliente:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    if not nome.strip() or not funcao.strip():
        return _erro_ficha(request, db, user, cliente, "Preencha nome e funcao do contato.", aba="dados")
    erro = validar_telefone(telefone)
    if not erro:
        dup = encontrar_telefone_duplicado(db, telefone)
        if dup:
            erro = (f"Esse telefone ja esta cadastrado para {dup['cliente_nome']}"
                    + (f" ({dup['pessoa']})" if dup["pessoa"] else "")
                    + " -- confira se nao e o mesmo cliente duplicado.")
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="dados")

    db.add(ContatoAdicionalCRM(cliente_id=cliente.id, nome=nome.strip(), funcao=funcao.strip(), telefone=telefone.strip()))
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="dados",
                       texto=f"Contato adicional cadastrado: {nome.strip()} ({funcao.strip()}) - {telefone.strip()}."))
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=dados", status_code=303)


@router.post("/crm/cliente/{cliente_id}/contato-adicional/{contato_id}/excluir")
def crm_excluir_contato_adicional(cliente_id: int, contato_id: int,
                                   user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if cliente:
        contato = db.get(ContatoAdicionalCRM, contato_id)
        if contato and contato.cliente_id == cliente.id:
            db.delete(contato)
            db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=dados", status_code=303)


# ---------- proposta / pedido ----------

def _validar_proposta_form(produto, volume, preco, pagamento, prazo):
    if produto not in PRODUTOS:
        return "Selecione o produto: Calcario ou Gesso."
    try:
        volume_f = float(volume)
        preco_f = float(preco)
    except ValueError:
        return "Preencha volume e preco com numeros validos."
    if volume_f <= 0 or preco_f <= 0:
        return "Volume e preco devem ser maiores que zero."
    if pagamento not in ("A vista", "A prazo"):
        return "Informe a forma de pagamento."
    if pagamento == "A prazo" and not prazo.strip():
        return "Informe qual e o prazo."
    return None


@router.post("/crm/cliente/{cliente_id}/proposta/nova")
def crm_nova_proposta(request: Request, cliente_id: int, produto: str = Form(...), volume: str = Form(...),
                       preco: str = Form(...), pagamento: str = Form(...), prazo: str = Form(""),
                       observacoes: str = Form(""),
                       user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    if not cliente:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(produto, volume, preco, pagamento, prazo)
    if not erro:
        estado = _estado_produto(db, cliente.id, produto)
        if estado["situacao"] != "livre":
            erro = f"Ja existe uma proposta de {produto} em aberto (no {estado['proposta'].numero}) para este cliente."

    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    numero = _proximo_numero(db, PropostaCRM, PROPOSTA_NUMERO_BASE)
    proposta = PropostaCRM(
        numero=numero, cliente_id=cliente.id, produto=produto, volume=float(volume), preco=float(preco),
        pagamento=pagamento, prazo=prazo.strip() if pagamento == "A prazo" else None,
        observacoes=observacoes.strip() or None,
    )
    db.add(proposta)

    de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL['proposta']}"
    if cliente.fase != "proposta":
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                           texto=f"Etapa avancou automaticamente: {de_para}", fase_destino="proposta"))
    cliente.fase = "proposta"
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    db.flush()
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="proposta",
                       texto=f"Proposta no {numero} registrada: {produto} {volume}t a R$ {float(preco):.2f}/t.",
                       numero=numero, produto=produto, volume=float(volume), preco=float(preco), pagamento=pagamento))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=propostas", status_code=303)


@router.post("/crm/cliente/{cliente_id}/proposta/{proposta_id}/editar")
def crm_editar_proposta(request: Request, cliente_id: int, proposta_id: int, volume: str = Form(...), preco: str = Form(...),
                         pagamento: str = Form(...), prazo: str = Form(""), observacoes: str = Form(""),
                         user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(proposta.produto, volume, preco, pagamento, prazo)
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="propostas")

    proposta.volume = float(volume)
    proposta.preco = float(preco)
    proposta.pagamento = pagamento
    proposta.prazo = prazo.strip() if pagamento == "A prazo" else None
    proposta.observacoes = observacoes.strip() or None
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="proposta",
                       texto=f"Proposta no {proposta.numero} atualizada: {proposta.produto} {volume}t a R$ {float(preco):.2f}/t.",
                       numero=proposta.numero, produto=proposta.produto, volume=proposta.volume,
                       preco=proposta.preco, pagamento=proposta.pagamento))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=propostas", status_code=303)


@router.get("/crm/cliente/{cliente_id}/proposta/{proposta_id}/gerar-pedido", response_class=HTMLResponse)
def crm_gerar_pedido_form(request: Request, cliente_id: int, proposta_id: int,
                           user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)
    return _templates(request).TemplateResponse(request, "crm_gerar_pedido.html", {
        "user": user, "cliente": cliente, "proposta": proposta,
        "subsidiarias": SUBSIDIARIAS.get(proposta.produto, []), "erro": None,
    })


@router.post("/crm/cliente/{cliente_id}/proposta/{proposta_id}/gerar-pedido")
def crm_gerar_pedido(request: Request, cliente_id: int, proposta_id: int, subsidiaria_cnpj: str = Form(""),
                      faturamento_tipo: str = Form(""), fat_nome: str = Form(""), fat_documento: str = Form(""),
                      fat_telefone: str = Form(""), fat_endereco: str = Form(""),
                      user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    proposta = db.get(PropostaCRM, proposta_id)
    if not cliente or not proposta or proposta.cliente_id != cliente.id or proposta.status != STATUS_PROPOSTA_ABERTA:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    subsidiarias = SUBSIDIARIAS.get(proposta.produto, [])
    subsidiaria = next((s for s in subsidiarias if s["cnpj"] == subsidiaria_cnpj), None)
    erro = None
    if not subsidiaria:
        erro = "Selecione a subsidiaria que vai faturar o pedido."
    elif faturamento_tipo == "cliente":
        pass
    elif faturamento_tipo == "terceiro":
        if not fat_nome.strip() or not fat_documento.strip():
            erro = "Preencha nome e CPF/CNPJ para faturamento em nome de terceiro."
    else:
        erro = "Selecione para quem sera faturado o pedido."

    if erro:
        return _templates(request).TemplateResponse(request, "crm_gerar_pedido.html", {
            "user": user, "cliente": cliente, "proposta": proposta, "subsidiarias": subsidiarias, "erro": erro,
        })

    if faturamento_tipo == "cliente":
        faturamento = {"nome": cliente.fazenda, "documento": cliente.cnpj or "Nao informado", "telefone": cliente.telefone}
    else:
        faturamento = {"nome": fat_nome.strip(), "documento": fat_documento.strip(),
                        "telefone": fat_telefone.strip() or None, "endereco": fat_endereco.strip() or None}

    numero = _proximo_numero(db, PedidoCRM, PEDIDO_NUMERO_BASE)
    pedido = PedidoCRM(
        numero=numero, cliente_id=cliente.id, proposta_id=proposta.id, produto=proposta.produto,
        volume=proposta.volume, preco=proposta.preco, pagamento=proposta.pagamento, prazo=proposta.prazo,
        observacoes=proposta.observacoes, subsidiaria_nome=subsidiaria["nome"], subsidiaria_cnpj=subsidiaria["cnpj"],
        faturamento_tipo=faturamento_tipo, faturamento_nome=faturamento["nome"],
        faturamento_documento=faturamento["documento"], faturamento_telefone=faturamento.get("telefone"),
        faturamento_endereco=faturamento.get("endereco"),
    )
    db.add(pedido)
    proposta.status = STATUS_PROPOSTA_CONVERTIDA
    de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL['realizado']}"
    if cliente.fase != "realizado":
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                           texto=f"Etapa avancou automaticamente: {de_para}", fase_destino="realizado"))
    cliente.fase = "realizado"
    cliente.ultima_interacao_em = dt.datetime.utcnow()
    db.flush()
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido",
                       texto=f"Pedido no {numero} gerado a partir da proposta no {proposta.numero}: {proposta.produto} {proposta.volume}t, faturado por {subsidiaria['nome']}.",
                       numero=numero, produto=proposta.produto, volume=proposta.volume,
                       preco=proposta.preco, pagamento=proposta.pagamento))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=pedidos", status_code=303)


@router.post("/crm/cliente/{cliente_id}/pedido/{pedido_id}/renegociar")
def crm_renegociar_pedido(request: Request, cliente_id: int, pedido_id: int, volume: str = Form(...),
                           preco: str = Form(...), pagamento: str = Form(...), prazo: str = Form(""),
                           user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    cliente = _cliente_do_usuario(db, user, cliente_id)
    pedido = db.get(PedidoCRM, pedido_id)
    if not cliente or not pedido or pedido.cliente_id != cliente.id or pedido.status != STATUS_PEDIDO_ABERTO:
        return RedirectResponse(f"/crm/cliente/{cliente_id}", status_code=303)

    erro = _validar_proposta_form(pedido.produto, volume, preco, pagamento, prazo)
    if erro:
        return _erro_ficha(request, db, user, cliente, erro, aba="pedidos")

    preco_antigo, volume_antigo, pagamento_antigo = pedido.preco, pedido.volume, pedido.pagamento
    pedido.preco = float(preco)
    pedido.volume = float(volume)
    pedido.pagamento = pagamento
    pedido.prazo = prazo.strip() if pagamento == "A prazo" else None
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido",
                       texto=(f"Pedido no {pedido.numero} renegociado: preco R$ {preco_antigo:.2f}/t -> R$ {pedido.preco:.2f}/t, "
                              f"volume {volume_antigo:g}t -> {pedido.volume:g}t, pagamento {pagamento_antigo} -> {pagamento}."),
                       numero=pedido.numero, produto=pedido.produto, volume=pedido.volume,
                       preco=pedido.preco, pagamento=pedido.pagamento))
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=pedidos", status_code=303)


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
    db.add(ContatoCRM(cliente_id=cliente.id, tipo="pedido_cancelado",
                       texto=f"Pedido no {pedido.numero} cancelado: {motivo.strip()}.",
                       numero=pedido.numero, produto=pedido.produto, volume=pedido.volume,
                       preco=pedido.preco, pagamento=pedido.pagamento))

    outros_ativos = db.query(PedidoCRM).filter(
        PedidoCRM.cliente_id == cliente.id, PedidoCRM.status == STATUS_PEDIDO_ABERTO, PedidoCRM.id != pedido.id
    ).count()
    if outros_ativos == 0:
        de_para = f"{FASE_LABEL.get(cliente.fase, cliente.fase)} -> {FASE_LABEL['contactado']}"
        db.add(ContatoCRM(cliente_id=cliente.id, tipo="mudanca_fase",
                           texto=f"Etapa avancou automaticamente: {de_para}", fase_destino="contactado"))
        cliente.fase = "contactado"
    _recalcular_resumo_comercial(db, cliente)
    db.commit()
    return RedirectResponse(f"/crm/cliente/{cliente_id}?aba=pedidos", status_code=303)


# ---------- area do vendedor ----------

@router.get("/vendedor/crm", response_class=HTMLResponse)
def crm_home_vendedor(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(ClienteCRM)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    total_clientes = query.count()
    novos_aviso = query.filter(ClienteCRM.fase == "a_contactar").count()

    compradores_mes = 0
    if user.role == "vendedor":
        mes_atual = dt.datetime.utcnow().month
        compradores_mes = sum(1 for c in query.all() if c.prioridade_sazonal(mes_atual) == 0)

    return _templates(request).TemplateResponse(request, "crm_vendedor_home.html", {
        "user": user, "total_clientes": total_clientes, "novos_aviso": novos_aviso,
        "compradores_mes": compradores_mes, "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
    })


@router.get("/vendedor/crm/carteira", response_class=HTMLResponse)
def crm_carteira_vendedor(request: Request, user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
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

    return _templates(request).TemplateResponse(request, "crm_carteira_vendedor.html", {
        "user": user, "total": total, "fases": fases, "por_estado": por_estado,
    })


@router.get("/vendedor/crm/fase/{fase}", response_class=HTMLResponse)
def crm_lista_vendedor(request: Request, fase: str, cidade: str = "", ordenar: str = "", pendentes: str = "",
                        user: User = Depends(require_role("admin", "vendedor")), db: Session = Depends(get_db)):
    query = db.query(ClienteCRM).filter(ClienteCRM.fase == fase)
    if user.role == "vendedor":
        query = query.filter(ClienteCRM.vendedor_nome == user.vendedor_nome)
    clientes = query.all()
    cidades = sorted({c.cidade for c in clientes if c.cidade})
    clientes = _ordenar_e_filtrar(clientes, cidade, ordenar, pendentes == "1")
    return _templates(request).TemplateResponse(request, "crm_lista_clientes.html", {
        "user": user, "uf": None, "fase": fase, "fase_label": FASE_LABEL.get(fase, fase),
        "clientes": clientes, "cidades": cidades, "voltar_url": "/vendedor/crm/carteira",
        "ficha_base_url": "/crm/cliente", "mes_atual_nome": MESES_PT[dt.datetime.utcnow().month - 1],
        "filtros": {"cidade": cidade, "ordenar": ordenar, "pendentes": pendentes},
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
    })
