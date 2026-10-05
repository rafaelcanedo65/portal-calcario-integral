"""Importa a planilha "Expedição — o que falta retirar" (Google Drive) para a
Logistica (tabela `pedidos`). Rafael, 2026-10-03.

A planilha e gerada do NetSuite pela equipe da expedicao (aba Config filtra
desde 01/01/2026) e e onde a equipe anota situacao, comentario e data limite.
Ate a integracao direta com o NetSuite, ela e a fonte da Logistica do portal.

Entrada: o texto da planilha exportado pelo conector do Google Drive (JSON com
"contentSnippet", ou o texto puro). Esse texto vem "achatado": celula vazia
some e numero com virgula decimal nao vem entre aspas -- por isso cada linha e
lida por ancoras fixas (pedido, datas, os 3 numeros Total/Faturado/Saldo, "R$").
A leitura confere os totais de cada aba com o cabecalho da propria planilha e
nao grava nada se nao baterem.

Uso:  python -m app.import_expedicao <arquivo> [--gravar]
Sem --gravar so mostra o que mudaria.

Regras:
- Chave: numero do pedido ("105608/SO2776"). Pedido com 2 produtos vira 1
  linha (quantidades somadas, produtos juntos, aba da linha mais "aberta").
- A equipe anota no PORTAL (Rafael, 2026-10-03). Pedido ja anotado no portal
  (`anotado_no_portal_em`) nunca tem situacao, comentario ou data limite
  mexidos pela planilha. Nos outros (transicao), vem a anotacao mais nova da
  aba Anotacoes, com a situacao convertida para a lista unica do portal.
- O historico da aba "Histórico anotações" entra em `pedido_anotacoes`
  (origem "planilha"), sem repetir o que ja entrou.
- Pedido gerado no portal (PV) e ligado ao pedido do NetSuite quando um so
  combina: mesmo cliente, mesma categoria de produto, mesmo volume (1%) e
  pedido lancado entre 2 dias antes e 60 dias depois. Ligado, recebe o
  carregamento real e a data limite (ver PedidoCRM.sincronizar_com_netsuite).
- Pedido do portal que sumiu da planilha fica marcado "Fora da planilha" e sai
  da lista em aberto (nada e apagado).
- Vendedor: o NetSuite abrevia o nome do meio ("Zilma B Reis"); casa com o
  usuario do portal pelo nome ("Zilma Bispo Reis") quando so um combina.
"""
import datetime as dt
import json
import re
import sys
import unicodedata

from .database import Base, SessionLocal, engine
from .models import (ABA_FORA_DA_PLANILHA, SITUACAO_DA_PLANILHA, STATUS_PEDIDO_CANCELADO, ContatoCRM, ImportacaoPlanilha,
                     Pedido, PedidoAnotacao, PedidoCRM, User, categoria_produto, formatar_telefone, ler_coordenadas,
                     LOCAL_DA_LOGISTICA)

FONTE = "Expedição — o que falta retirar"
ABAS = ("Expedição", "Parado - retirou outro pedido", "Outros produtos", "Saldo abaixo de 5%", "Finalizados")
# Quanto mais cedo na lista, mais "aberto": decide a aba do pedido com 2 produtos
PRIORIDADE_ABA = {aba: i for i, aba in enumerate(ABAS)}
NUM = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
LINHA = re.compile(
    r"^(?P<sub>[^,]*),(?P<ped>(?:\d+/)?SO\d+),(?P<data>\d\d/\d\d/\d{4}),(?P<meio1>.*?),"
    rf"(?P<qt>{NUM}),(?P<fat>{NUM}),(?P<saldo>{NUM}),(?P<meio2>.*?),?R\$ (?P<vu>[\d.]+,\d\d),"
    r"(?P<fim>.*)$")
HISTORICO = re.compile(
    r"^(?P<quando>\d\d/\d\d/\d{4} \d\d:\d\d:\d\d),(?P<quem>[^,]*),(?P<origem>[^,]*),(?P<ped>(?:\d+/)?SO\d+),"
    r"(?P<prod>[^,]*),(?P<campo>SITUAÇÃO|DATA LIMITE|COMENTÁRIO),?(?P<resto>.*)$")
CAMPO_HISTORICO = {"SITUAÇÃO": "situacao", "DATA LIMITE": "data_limite", "COMENTÁRIO": "comentario"}
ANOTACAO = re.compile(
    r"^(?P<ped>(?:\d+/)?SO\d+),(?P<prod>[^,]*),(?P<lim>\d\d/\d\d/\d{4})?,?(?P<com>.*?),?"
    r"(?P<quando>\d\d/\d\d/\d{4} \d\d:\d\d),?(?P<sit>[^,]*)$")
UFS = {"MA", "PA", "TO", "GO", "MT", "PI", "DF", "BA", "MG", "SP", "MS", "CE", "RO", "AP", "AM", "RR", "AC", "PR", "SC",
       "RS", "RJ", "ES", "SE", "AL", "PE", "PB", "RN"}


def _num(t):
    return float(t.replace(".", "").replace(",", "."))


def _data(t):
    return dt.datetime.strptime(t, "%d/%m/%Y").date() if t else None


def _abas(texto):
    partes = re.split(r"^# (.+)$", texto, flags=re.M)
    return {partes[i].strip(): partes[i + 1] for i in range(1, len(partes), 2)}


def _linhas_pedido(bloco, aba):
    pedidos, falhas = [], []
    for linha in bloco.split("\n"):
        linha = linha.rstrip()
        if not re.match(r"^[^,]*,(?:\d+/)?SO\d+,", linha):
            continue
        m = LINHA.match(linha)
        if not m:
            falhas.append(linha)
            continue
        g = m.groupdict()
        meio1 = g["meio1"].split(",")
        tel = next((i for i, x in enumerate(meio1) if x.startswith("+")), None)
        cliente, depois = (",".join(meio1[:tel]), meio1[tel + 1:]) if tel is not None else (meio1[0], meio1[1:])
        telefone = meio1[tel] if tel is not None else None
        fim = g["fim"].split(",")
        resto = fim[1:]
        pedidos.append({
            "aba": aba, "subsidiaria": g["sub"], "pedido": g["ped"], "data": _data(g["data"]), "cliente": cliente.strip(),
            "produto": depois[0].strip() if depois else "", "status": depois[1].strip() if len(depois) > 1 else "",
            "total": _num(g["qt"]), "faturado": _num(g["fat"]), "saldo": _num(g["saldo"]), "valor_unitario": _num(g["vu"]),
            "vendedor": " ".join(fim[0].replace("\xa0", " ").split()), "telefone": telefone,
            "ultima_retirada": _data(next((x for x in resto if re.fullmatch(r"\d\d/\d\d/\d{4}", x)), None)),
            "uf": next((x for x in reversed(resto) if x in UFS), None),
        })
    return pedidos, falhas


def _anotacoes(bloco):
    por_pedido = {}
    for linha in bloco.split("\n"):
        m = ANOTACAO.match(linha.rstrip())
        if not m:
            continue
        g = m.groupdict()
        quando = dt.datetime.strptime(g["quando"], "%d/%m/%Y %H:%M")
        atual = por_pedido.get(g["ped"])
        if atual is None or quando > atual["quando"]:
            por_pedido[g["ped"]] = {"quando": quando, "limite": _data(g["lim"]), "comentario": g["com"].strip(" ,") or None,
                                    "situacao": g["sit"].strip() or None}
    return por_pedido


def _historico(bloco):
    """Linhas da aba "Histórico anotações". So a troca de data com as duas datas
    tem antes/depois certos; o resto vira `registro` (o texto exportado nao diz
    se o valor era o antigo ou o novo)."""
    itens = []
    for linha in bloco.split("\n"):
        m = HISTORICO.match(linha.rstrip())
        if not m:
            continue
        g = m.groupdict()
        campo = CAMPO_HISTORICO[g["campo"]]
        resto = g["resto"].strip().strip(",")
        item = {"pedido": g["ped"], "quando": dt.datetime.strptime(g["quando"], "%d/%m/%Y %H:%M:%S"),
                "quem": None if g["quem"] == "(não identificado)" else g["quem"], "campo": campo,
                "antes": None, "depois": None, "registro": None}
        datas = re.fullmatch(r"(\d\d/\d\d/\d{4}),(\d\d/\d\d/\d{4})", resto)
        if campo == "data_limite" and datas:
            item["antes"], item["depois"] = datas.groups()
        elif campo == "situacao" and resto.upper() in SITUACAO_DA_PLANILHA:
            item["registro"] = resto.capitalize()
        else:
            item["registro"] = resto or None
        itens.append(item)
    return itens


def ler(texto):
    """-> (pedidos por numero, anotacoes por numero, data da planilha). Levanta
    erro se alguma linha nao for lida ou se os totais nao baterem."""
    abas = _abas(texto)
    linhas = []
    for aba in ABAS:
        peds, falhas = _linhas_pedido(abas[aba], aba)
        if falhas:
            raise ValueError(f"{len(falhas)} linha(s) da aba {aba} nao foram lidas, ex.: {falhas[0][:120]}")
        cab = re.search(r"QUANT\. TOTAL: ([\d.,]+)", abas[aba])
        if cab and abs(_num(cab.group(1)) - sum(p["total"] for p in peds)) > 0.01:
            raise ValueError(f"Aba {aba}: total lido nao bate com o cabecalho ({cab.group(1)})")
        linhas += peds
    quando = re.search(r"Atualizado em (\d\d/\d\d/\d{4}) às (\d\d:\d\d)", abas["Expedição"])
    atualizada = dt.datetime.strptime(" ".join(quando.groups()), "%d/%m/%Y %H:%M") if quando else None
    pedidos = {}
    for p in sorted(linhas, key=lambda p: PRIORIDADE_ABA[p["aba"]]):
        atual = pedidos.get(p["pedido"])
        if atual is None:
            pedidos[p["pedido"]] = dict(p)
            continue
        # Mesmo pedido com outro produto: soma e junta (a aba ja e a mais aberta)
        for campo in ("total", "faturado", "saldo"):
            atual[campo] += p[campo]
        if p["produto"] and p["produto"] not in atual["produto"]:
            atual["produto"] = f"{atual['produto']} + {p['produto']}"
        if p["ultima_retirada"] and (not atual["ultima_retirada"] or p["ultima_retirada"] > atual["ultima_retirada"]):
            atual["ultima_retirada"] = p["ultima_retirada"]
    return pedidos, _anotacoes(abas.get("Anotações", "")), atualizada, _historico(abas.get("Histórico anotações", ""))


def _palavras(nome):
    t = unicodedata.normalize("NFD", nome or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z]+", t) if len(w) > 1]


def mapa_vendedores(db, nomes):
    """Nome do NetSuite -> vendedor_nome do usuario do portal, quando um so
    combina: mesmo primeiro nome e as palavras de um contidas no outro."""
    usuarios = [u.vendedor_nome for u in db.query(User).filter(User.vendedor_nome.isnot(None))]
    mapa = {}
    for nome in nomes:
        p = _palavras(nome)
        if not p:
            continue
        candidatos = [u for u in usuarios if _palavras(u) and _palavras(u)[0] == p[0]
                      and (set(p) <= set(_palavras(u)) or set(_palavras(u)) <= set(p))]
        if len(candidatos) == 1:
            mapa[nome] = candidatos[0]
    return mapa


def preencher_local(db, pedidos):
    """Local de entrega de cada pedido (cidade + ponto no mapa). A planilha so
    tem a UF; ate o NetSuite mandar o endereco de entrega:
    - o que a Logistica informou na ficha do pedido nunca e trocado;
    - venda pra TRANSPORTADORA/consultor usa o destino final registrado no
      pedido do portal (quem usa o produto); sem destino, fica sem local e com
      aviso -- nunca o endereco da transportadora (Rafael, 2026-10-03);
    - senao, o cadastro do cliente no CRM: a coordenada da fazenda (exata) e a
      cidade (so se a UF do cadastro bate com a de entrega e existe no IBGE).
    -> quantos ficaram com local."""
    from .crm_routes import encontrar_cliente_crm  # import tardio: crm_routes carrega o app inteiro
    from . import geo
    cache = {}
    destinos = {}
    for pc in db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite.isnot(None)):
        if pc.destinos_finais:
            destinos[pc.pedido_netsuite] = max(pc.destinos_finais, key=lambda d: d.volume or 0).cliente_final
    n = 0
    for p in pedidos:
        if p.local_fonte in LOCAL_DA_LOGISTICA:
            n += 1
            continue
        if p.cliente not in cache:
            cache[p.cliente] = encontrar_cliente_crm(db, p.cliente)
        c = cache[p.cliente]
        fonte = "fazenda"
        # Faturado pra transportadora ("transporte" no nome do NetSuite; no CRM so 1
        # de ~21 estava marcada) ou pra intermediario marcado no CRM: o endereco
        # dele nao e o da entrega
        if "TRANSPORT" in (p.cliente or "").upper() or (c is not None and c.e_intermediario()):
            c = destinos.get(p.numero_pedido)
            fonte = "destino final"
            if c is None:
                p.cidade, p.cidade_fonte, p.latitude, p.longitude = None, None, None, None
                p.local_fonte = "transportadora sem destino"
                continue
        elif c is not None and c.entrega_em_outro_lugar():
            # Pedido no nome de pessoa/fazenda que casou (pelo nome do dono) com o
            # cadastro da transportadora dele -- ex.: "SERGIO GUIMARAES - FAZ SAO
            # CARLOS" x "COCAL TRANSPORTES". O endereco da transportadora nao serve
            # e nao sabemos se e venda pra ela: fica sem local, sem chutar.
            c = None
        cidade = geo.municipio(c.uf, c.cidade) if c and c.cidade and (fonte == "destino final" or c.uf == p.uf) else None
        ponto = ler_coordenadas(c.coordenadas) if c else None
        p.cidade = cidade[0] if cidade else None
        p.cidade_fonte = "CRM" if cidade else None
        if ponto:
            p.latitude, p.longitude, p.local_fonte = ponto[0], ponto[1], fonte
        elif cidade:
            p.latitude, p.longitude, p.local_fonte = cidade[1][0], cidade[1][1], ("destino final cidade" if fonte == "destino final" else "cidade")
        else:
            p.latitude, p.longitude, p.local_fonte = None, None, None
        n += p.latitude is not None
    return n


def vincular_pedidos_do_portal(db, por_numero, atualizada):
    """Liga PV -> pedido do NetSuite quando um so combina, e atualiza os ja
    ligados (carregamento e data limite). -> (novos ligados, n sincronizados)."""
    from .crm_routes import encontrar_cliente_crm  # import tardio: crm_routes carrega o app inteiro
    ja_ligados = {p.pedido_netsuite for p in db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite.isnot(None))}
    cliente_crm = {}

    def cliente_de(pns):
        if pns.cliente not in cliente_crm:
            c = encontrar_cliente_crm(db, pns.cliente)
            cliente_crm[pns.cliente] = c.id if c else None
        return cliente_crm[pns.cliente]

    ligados = []
    soltos = db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite.is_(None), PedidoCRM.status != STATUS_PEDIDO_CANCELADO).all()
    for pcrm in soltos:
        inicio = (pcrm.criado_em or dt.datetime.utcnow()).date() - dt.timedelta(days=2)
        fim = inicio + dt.timedelta(days=62)
        candidatos = [pns for pns in por_numero.values()
                      if pns.numero_pedido not in ja_ligados and pns.data_pedido and inicio <= pns.data_pedido <= fim
                      and categoria_produto(pns.produto) == categoria_produto(pcrm.produto)
                      and abs((pns.quant_total or 0) - (pcrm.volume or 0)) <= max(1.0, 0.01 * (pcrm.volume or 0))
                      and cliente_de(pns) == pcrm.cliente_id]
        if len(candidatos) != 1:
            continue
        pns = candidatos[0]
        pcrm.pedido_netsuite = pns.numero_pedido
        pcrm.vinculado_em = dt.datetime.utcnow()
        pcrm.vinculado_por = "planilha de Expedição"
        ja_ligados.add(pns.numero_pedido)
        db.add(ContatoCRM(cliente_id=pcrm.cliente_id, tipo="dados", autor="Planilha de Expedição",
                          texto=(f"Pedido {pcrm.codigo_portal} lançado no NetSuite como {pns.numero_pedido} "
                                 f"(ligado pela planilha de Expedição{' de ' + atualizada.strftime('%d/%m/%Y %H:%M') if atualizada else ''}).")))
        ligados.append((pcrm, pns))
    sincronizados = 0
    for pcrm in db.query(PedidoCRM).filter(PedidoCRM.pedido_netsuite.isnot(None)).all():
        pns = por_numero.get(pcrm.pedido_netsuite)
        if pns is not None:
            pcrm.sincronizar_com_netsuite(pns)
            sincronizados += 1
    return ligados, sincronizados


def importar(caminho, gravar=False):
    bruto = open(caminho, encoding="utf-8").read()
    try:
        texto = json.loads(bruto)["contentSnippet"]
    except (ValueError, KeyError):
        texto = bruto
    pedidos, anotacoes, atualizada, historico = ler(texto)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        vendedores = mapa_vendedores(db, {p["vendedor"] for p in pedidos.values()})
        existentes = {p.numero_pedido: p for p in db.query(Pedido).all()}
        novos = atualizados = mudou_faturado = 0
        agora = dt.datetime.utcnow()
        for numero, p in pedidos.items():
            alvo = existentes.get(numero)
            if alvo is None:
                alvo = Pedido(numero_pedido=numero, cliente=p["cliente"])
                db.add(alvo)
                novos += 1
            else:
                atualizados += 1
                if abs((alvo.faturado or 0) - p["faturado"]) > 0.009:
                    alvo.faturado_atualizado_em = agora
                    mudou_faturado += 1
            nota = anotacoes.get(numero, {}) if not alvo.anotado_no_portal_em else {}
            alvo.subsidiaria = p["subsidiaria"]
            alvo.data_pedido = p["data"]
            alvo.cliente = p["cliente"]
            alvo.status = p["status"]
            alvo.quant_total = p["total"]
            alvo.faturado = p["faturado"]
            alvo.saldo = p["saldo"]
            alvo.valor_unitario = p["valor_unitario"]
            alvo.vendedor = vendedores.get(p["vendedor"], p["vendedor"])
            if alvo.local_fonte not in LOCAL_DA_LOGISTICA:  # a Logistica corrigiu o local de entrega
                alvo.uf = p["uf"]
            alvo.produto = p["produto"] or None
            alvo.telefone = formatar_telefone(p.get("telefone"))
            alvo.aba_planilha = p["aba"]
            alvo.ultima_retirada = p["ultima_retirada"]
            if not alvo.anotado_no_portal_em:
                alvo.situacao_logistica = SITUACAO_DA_PLANILHA.get((nota.get("situacao") or "").upper())
                alvo.comentario_logistica = nota.get("comentario")
                if nota.get("limite"):
                    alvo.data_limite_retirada = nota["limite"]
        fora = [p for n, p in existentes.items() if n not in pedidos]
        for p in fora:
            p.aba_planilha = ABA_FORA_DA_PLANILHA
        db.flush()
        por_numero = {p.numero_pedido: p for p in db.query(Pedido).all()}

        # Historico da planilha (sem repetir)
        ja = {(a.pedido_id, a.quando, a.campo) for a in db.query(PedidoAnotacao).filter_by(origem="planilha")}
        n_hist = 0
        for h in historico:
            ped = por_numero.get(h["pedido"])
            if ped is None or (ped.id, h["quando"], h["campo"]) in ja:
                continue
            db.add(PedidoAnotacao(pedido_id=ped.id, quando=h["quando"], quem=h["quem"], origem="planilha",
                                  campo=h["campo"], antes=h["antes"], depois=h["depois"], registro=h["registro"]))
            ja.add((ped.id, h["quando"], h["campo"]))
            n_hist += 1

        ligados, sincronizados = vincular_pedidos_do_portal(db, por_numero, atualizada)
        n_cidade = preencher_local(db, por_numero.values())
        resumo = (f"{len(pedidos)} pedidos ({novos} novos, {atualizados} atualizados, {mudou_faturado} com carregamento novo); "
                  f"{len(fora)} fora da planilha; {len(anotacoes)} anotações; {n_hist} registros de histórico; "
                  f"vendedores casados: {len(vendedores)}; pedidos do portal ligados ao NetSuite: {len(ligados)} novos, "
                  f"{sincronizados} atualizados; com local de entrega: {n_cidade}")
        for pcrm, pns in ligados:
            print(f"  ligado: {pcrm.codigo_portal} -> {pns.numero_pedido} ({pns.cliente})")
        print("Planilha atualizada em:", atualizada)
        print(resumo)
        print("Vendedores:", {k: v for k, v in vendedores.items() if k != v})
        print("Fora da planilha:", [(p.numero_pedido, p.cliente) for p in fora])
        if gravar:
            db.add(ImportacaoPlanilha(fonte=FONTE, atualizada_em=atualizada, linhas=len(pedidos), resumo=resumo))
            db.commit()
            print("GRAVADO.")
        else:
            db.rollback()
            print("(nada gravado -- use --gravar)")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python -m app.import_expedicao <arquivo> [--gravar]")
        sys.exit(1)
    importar(sys.argv[1], gravar="--gravar" in sys.argv)
