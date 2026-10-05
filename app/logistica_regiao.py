"""Pedidos por regiao (Rafael, 2026-10-03): "as vezes ele sabe de caminhoes indo
pra uma determinada regiao e tem um frete retorno pra aquela regiao, entao saber
aonde temos pedido e importante".

- "Caminhao indo para [cidade] num raio de [km]": pedidos em aberto mais perto
  primeiro (linha reta). "Rota" abre, dentro do portal, a estrada saindo das
  nossas duas plantas (rotas.py) -- nunca de onde a pessoa esta.
- Mapa (OpenStreetMap, via Leaflet): fazenda com local exato vira um alfinete;
  pedido que so tem a cidade entra numa bolha por cidade, do tamanho das
  toneladas a retirar.
- Tabela UF -> cidade, com quantos estao na fila (precisam da equipe).
- Oportunidade logistica (Rafael, 2026-10-04): com o caminhao indo pra regiao,
  lista os clientes do cadastro ali que ainda nao tem pedido em aberto (frete
  mais barato = chance de venda). A Logistica marca e avisa os vendedores; o
  cliente entra na fila de trabalho do vendedor (OportunidadeLogistica).

O local de cada pedido ja vem gravado (Pedido.latitude/longitude/local_fonte,
ver import_expedicao.preencher_local e a ficha do pedido): pedido sem local
conhecido aparece contado por UF, nunca posto num lugar inventado."""
import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import config, expedicao, geo, rotas
from .auth import require_role
from .crm_routes import _templates
from .database import get_db
from .feedback import avisar_erro, avisar_sucesso
from .models import (ESTADOS_OPERACAO, FASE_COR, FASE_LABEL, STATUS_PEDIDO_ABERTO, AvisoCRM, ClienteCRM, ContatoCRM,
                     OportunidadeLogistica, PedidoCRM, User, ler_coordenadas)

router = APIRouter()


def _ponto_cliente(c):
    """-> ((lat, lon), exato) ou (None, False): coordenada da fazenda ou centro da cidade do cadastro."""
    ponto = ler_coordenadas(c.coordenadas) if c.coordenadas else None
    if ponto:
        return ponto, True
    achado = geo.municipio(c.uf, c.cidade)
    return (achado[1], False) if achado else (None, False)


FASES_FORA_DE_OFERECER = ("nao_usara", "realizado")


def clientes_sem_pedido(db, centro, raio, pedidos_abertos):
    """Clientes do cadastro ate `raio` km de `centro` pra OFERECER o frete, mais
    perto primeiro. Ficam fora (Rafael, 2026-10-04):
    - "Nao usara";
    - "Realizado": ja comprou nesta safra -- se ainda tem o que retirar, o pedido
      ja esta na aba Carregar; se ja retirou tudo, nao ha o que oferecer;
    - quem tem pedido em aberto: do portal (PedidoCRM) ou da planilha do NetSuite
      casado pelo NOME com TODOS os cadastros parecidos (o CRM tem duplicados).
    Perdidos entram (o frete pode reconquistar). -> (lista, quantos sem local)"""
    from .crm_routes import _oportunidades_abertas, encontrar_clientes_crm
    com_pedido = {cid for (cid,) in db.query(PedidoCRM.cliente_id).filter(PedidoCRM.status == STATUS_PEDIDO_ABERTO)}
    vistos = set()
    for p in pedidos_abertos:
        if p.cliente not in vistos:
            vistos.add(p.cliente)
            com_pedido.update(c.id for c in encontrar_clientes_crm(db, p.cliente))
    lista, sem_local = [], 0
    for c in db.query(ClienteCRM).filter(~ClienteCRM.fase.in_(FASES_FORA_DE_OFERECER)):
        if c.id in com_pedido:
            continue
        ponto, exato = _ponto_cliente(c)
        if ponto is None:
            sem_local += 1
            continue
        d = geo.distancia_km(centro, ponto)
        if d <= raio:
            lista.append({"cliente": c, "dist": d, "exato": exato, "lat": ponto[0], "lon": ponto[1]})
    lista.sort(key=lambda x: (x["dist"], -(x["cliente"].area_plantada_ha or 0)))
    abertas = _oportunidades_abertas(db, [x["cliente"].id for x in lista])
    for x in lista:
        x["oportunidade"] = abertas.get(x["cliente"].id)
    return lista, sem_local


@router.get("/logistica/regiao", response_class=HTMLResponse)
def pedidos_por_regiao(request: Request, uf_destino: str = "", cidade_destino: str = "", raio: int = 100,
                       uf: str = "", cidade: str = "",
                       user: User = Depends(require_role("admin", "logistica")), db: Session = Depends(get_db)):
    pedidos = expedicao.query_em_aberto(db).all()
    ctx = expedicao.contexto_fila(db, pedidos)
    hoje = dt.date.today()
    nivel = {p.id: expedicao.nivel_fila(p, ctx, hoje) for p in pedidos}
    com_local = [p for p in pedidos if p.latitude is not None]

    # Tabela UF -> cidade
    por_uf = {}
    for p in pedidos:
        u = por_uf.setdefault(p.uf or "—", {"pedidos": 0, "saldo": 0.0, "na_fila": 0, "cidades": {}, "sem_cidade": 0,
                                           "saldo_sem_cidade": 0.0})
        u["pedidos"] += 1
        u["saldo"] += p.saldo or 0
        u["na_fila"] += bool(nivel[p.id])
        if p.cidade:
            c = u["cidades"].setdefault(p.cidade, {"pedidos": 0, "saldo": 0.0, "na_fila": 0})
            c["pedidos"] += 1
            c["saldo"] += p.saldo or 0
            c["na_fila"] += bool(nivel[p.id])
        else:
            u["sem_cidade"] += 1
            u["saldo_sem_cidade"] += p.saldo or 0
    ufs = sorted(por_uf.items(), key=lambda x: -x[1]["saldo"])
    for _, u in ufs:
        u["cidades"] = sorted(u["cidades"].items(), key=lambda x: -x[1]["saldo"])
    # Guia da tela vazia: onde ha mais pedido hoje
    top_cidades = sorted(((nome_uf, nome, c) for nome_uf, u in ufs for nome, c in u["cidades"]), key=lambda x: -x[2]["saldo"])[:6]

    # Mapa: alfinete por fazenda (local exato), bolha por cidade (so o centro da cidade)
    alfinetes, bolhas = [], {}
    for p in com_local:
        n = nivel[p.id]
        if p.local_exato:
            alfinetes.append({"id": p.id, "lat": p.latitude, "lon": p.longitude, "cliente": p.cliente, "pedido": p.numero_pedido,
                              "cidade": f"{p.cidade}/{p.uf}" if p.cidade else (p.uf or ""), "saldo": round(p.saldo or 0),
                              "fila": n["rotulo"] if n else None})
        else:
            b = bolhas.setdefault((p.uf, p.cidade), {"uf": p.uf, "cidade": p.cidade, "lat": p.latitude, "lon": p.longitude,
                                                     "pedidos": 0, "saldo": 0, "na_fila": 0})
            b["pedidos"] += 1
            b["saldo"] += round(p.saldo or 0)
            b["na_fila"] += bool(n)

    # Caminhao indo para...
    destino, proximos, erro_destino, oportunidades, sem_local = None, [], None, [], 0
    raios = config.valor("regiao_raios")
    raio = raio if raio in raios else (100 if 100 in raios else raios[len(raios) // 2])
    if cidade_destino:
        achado = geo.municipio(uf_destino, cidade_destino)
        if not achado:
            erro_destino = f"Não achamos \"{cidade_destino}\" em {uf_destino or 'nenhum estado'}. Escolha a cidade da lista."
        else:
            destino = {"cidade": achado[0], "uf": uf_destino.upper(), "lat": achado[1][0], "lon": achado[1][1], "raio_km": raio}
            for p in com_local:
                d = geo.distancia_km(achado[1], (p.latitude, p.longitude))
                if d <= raio:
                    proximos.append((d, p, nivel[p.id]))
            proximos.sort(key=lambda x: x[0])
            oportunidades, sem_local = clientes_sem_pedido(db, achado[1], raio, pedidos)

    # Pedidos de uma cidade (clique no mapa ou na tabela)
    da_cidade = []
    if uf and cidade:
        da_cidade = sorted((p for p in pedidos if p.uf == uf and p.cidade == cidade),
                           key=lambda p: (nivel[p.id]["ordem"] if nivel[p.id] else (99, 0)))
    elif uf and cidade == "" and request.query_params.get("sem_cidade"):
        da_cidade = sorted((p for p in pedidos if p.uf == uf and not p.cidade),
                           key=lambda p: (nivel[p.id]["ordem"] if nivel[p.id] else (99, 0)))

    ufs_destino = list(ESTADOS_OPERACAO) + [u for u in geo.ufs() if u not in ESTADOS_OPERACAO]
    return _templates(request).TemplateResponse(request, "logistica_regiao.html", {
        "user": user, "ufs": ufs, "top_cidades": top_cidades, "alfinetes": alfinetes, "bolhas": list(bolhas.values()),
        "plantas": [{"nome": p["nome"], "lat": p["coord"][0], "lon": p["coord"][1], "aproximado": p["aproximado"]} for p in rotas.plantas()],
        "destino": destino, "proximos": proximos, "erro_destino": erro_destino,
        "oportunidades": oportunidades, "sem_local": sem_local, "fase_label": FASE_LABEL, "fase_cor": FASE_COR,
        "oport_mapa": [{"id": x["cliente"].id, "lat": x["lat"], "lon": x["lon"], "nome": x["cliente"].fazenda,
                        "vendedor": x["cliente"].vendedor_nome or "sem vendedor", "fase": FASE_LABEL.get(x["cliente"].fase, x["cliente"].fase),
                        "dist": round(x["dist"]), "exato": x["exato"]} for x in oportunidades],
        "oport_dias": config.valor("oportunidade_dias"), "hoje": hoje,
        "raios": raios, "raio": raio, "uf_destino": uf_destino.upper() or "PA", "cidade_destino": cidade_destino,
        "ufs_destino": ufs_destino, "cidades_destino": geo.cidades_da_uf(uf_destino or "PA"),
        "uf": uf, "cidade": cidade, "da_cidade": da_cidade, "nivel": nivel,
        "total": len(pedidos), "n_local": len(com_local), "n_exato": len(alfinetes),
        "n_sem_destino": sum(p.local_fonte == "transportadora sem destino" for p in pedidos),
        "saldo_total": sum(p.saldo or 0 for p in pedidos),
        "migalhas": [("Fila da logística", "/logistica/fila"), ("Pedidos por região", None)],
    })


@router.post("/logistica/oportunidades")
async def enviar_oportunidades(request: Request, user: User = Depends(require_role("admin", "logistica")),
                               db: Session = Depends(get_db)):
    """A Logistica marca clientes da regiao e avisa os vendedores: cria a
    OportunidadeLogistica (entra na fila de trabalho do vendedor), o aviso e um
    registro no historico do cliente (que NAO conta como contato)."""
    from .crm_routes import _oportunidades_abertas
    form = await request.form()
    uf_destino = (form.get("uf_destino") or "").upper()
    cidade_destino = form.get("cidade_destino") or ""
    raio = int(form.get("raio") or 0) if str(form.get("raio") or "").isdigit() else 0
    voltar = "/logistica/regiao?" + urlencode({"uf_destino": uf_destino, "cidade_destino": cidade_destino, "raio": raio}) + "#oferecer"
    achado = geo.municipio(uf_destino, cidade_destino)
    ids = [int(x) for x in form.getlist("cliente") if str(x).isdigit()]
    hoje = dt.date.today()
    data_caminhao = None
    texto_data = (form.get("data_caminhao") or "").strip()
    if texto_data:
        try:
            data_caminhao = dt.date.fromisoformat(texto_data)
        except ValueError:
            data_caminhao = None
    if not achado:
        avisar_erro(request, "Informe para onde vai o caminhão antes de avisar os vendedores.")
        return RedirectResponse(voltar, status_code=303)
    if not ids:
        avisar_erro(request, "Marque pelo menos um cliente. Nada foi enviado.")
        return RedirectResponse(voltar, status_code=303)
    if texto_data and (data_caminhao is None or data_caminhao < hoje):
        avisar_erro(request, "A data do caminhão precisa ser hoje ou depois. Nada foi enviado.")
        return RedirectResponse(voltar, status_code=303)
    destino = f"{achado[0]}/{uf_destino}"
    valida_ate = data_caminhao or hoje + dt.timedelta(days=config.valor("oportunidade_dias"))
    recado = (form.get("recado") or "").strip()[:300] or None
    abertas = _oportunidades_abertas(db, ids)
    criadas, ja_avisados, sem_vendedor, vendedores = 0, 0, 0, set()
    for c in db.query(ClienteCRM).filter(ClienteCRM.id.in_(ids), ~ClienteCRM.fase.in_(FASES_FORA_DE_OFERECER)):
        if c.id in abertas:
            ja_avisados += 1
            continue
        if not c.vendedor_nome:
            sem_vendedor += 1  # ninguem pra avisar: nao inventa destinatario
            continue
        ponto, _ = _ponto_cliente(c)
        dist = round(geo.distancia_km(achado[1], ponto)) if ponto else None
        db.add(OportunidadeLogistica(cliente_id=c.id, vendedor_nome=c.vendedor_nome, destino=destino, raio_km=raio or None,
                                     distancia_km=dist, data_caminhao=data_caminhao, valida_ate=valida_ate, recado=recado,
                                     criada_por=user.nome_completo))
        caminhao = f"Caminhão indo para {destino}" + (f" em {data_caminhao.strftime('%d/%m')}" if data_caminhao else "")
        texto = (f"{caminhao}: frete mais barato para a região de {c.fazenda}"
                 + (f" (a {dist} km)" if dist is not None else "") + f". Ofereça até {valida_ate.strftime('%d/%m')}."
                 + (f" Recado: {recado}" if recado else ""))
        db.add(AvisoCRM(vendedor_nome=c.vendedor_nome, cliente_id=c.id, tipo="oportunidade_logistica",
                        titulo=f"Oportunidade logística: {c.fazenda}", texto=texto, autor=user.nome_completo,
                        link=f"/crm/cliente/{c.id}?via=fila&aba=comentario"))
        db.add(ContatoCRM(cliente_id=c.id, tipo="oportunidade", autor=user.nome_completo,
                          texto=f"Logística avisou {c.vendedor_nome}: {texto}"))
        criadas += 1
        vendedores.add(c.vendedor_nome)
    db.commit()
    partes = []
    if ja_avisados:
        partes.append(f"{ja_avisados} já tinha{'m' if ja_avisados != 1 else ''} oportunidade aberta")
    if sem_vendedor:
        partes.append(f"{sem_vendedor} sem vendedor (ninguém para avisar)")
    if criadas:
        avisar_sucesso(request, f"{criadas} oportunidade{'s' if criadas != 1 else ''} enviada{'s' if criadas != 1 else ''} para "
                                f"{len(vendedores)} vendedor{'es' if len(vendedores) != 1 else ''}: entra{'m' if criadas != 1 else ''} na fila de trabalho até "
                                f"{valida_ate.strftime('%d/%m')}." + (f" ({'; '.join(partes)}.)" if partes else ""))
    else:
        avisar_erro(request, "Nenhuma oportunidade enviada: " + "; ".join(partes) + ".")
    return RedirectResponse(voltar, status_code=303)
