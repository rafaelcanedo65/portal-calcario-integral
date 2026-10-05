"""Area plantada de cada estado, do IBGE: base do market share (area plantada dos
clientes / area plantada do estado), no quadro "Clientes por estado" do Inicio do
admin e no relatorio Market share.

Rafael (2026-10-04) escolheu a PAM (Producao Agricola Municipal, tabela 5457 do
SIDRA): "area plantada ou destinada a colheita" do total das lavouras temporarias e
permanentes. E anual (o IBGE publica o ano anterior em set/out), entao da pra
atualizar todo ano. Ressalva: a PAM soma cultura por cultura -- soja + milho
safrinha na mesma terra contam 2 vezes (pesa em MT/GO, pouco em PA/MA/TO/PI).

So vai pro IBGE o codigo dos estados (nenhum dado de cliente).

Uso: python -m app.ibge_area            (mostra o que mudaria)
     python -m app.ibge_area --gravar   (grava)
Ou o botao "Buscar ano novo" no Inicio do admin."""
import json
import sys
import urllib.request

from .models import AreaEstado, ClienteCRM

TABELA, VARIAVEL, TOTAL_LAVOURAS = 5457, 8331, 0  # c782/0 = Total das lavouras temporarias e permanentes
CODIGO_UF = {"RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17, "MA": 21, "PI": 22, "CE": 23, "RN": 24,
             "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41, "SC": 42,
             "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53}
UF_DO_CODIGO = {str(v): k for k, v in CODIGO_UF.items()}


def fonte(ano):
    return f"IBGE · PAM {ano} (área plantada de lavouras)"


def buscar(ufs, anos=3, timeout=30):
    """-> (ano, {uf: hectares}) do ano mais recente em que o IBGE ja tem o numero
    de TODOS os estados pedidos. Erro de rede/IBGE sobe como excecao."""
    codigos = ",".join(str(CODIGO_UF[u]) for u in sorted(ufs))
    url = (f"https://apisidra.ibge.gov.br/values/t/{TABELA}/n3/{codigos}/v/{VARIAVEL}/p/last%20{anos}"
           f"/c782/{TOTAL_LAVOURAS}/f/c")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        linhas = json.loads(r.read().decode("utf-8"))[1:]
    por_ano = {}
    for linha in linhas:
        try:
            valor = float(linha["V"])
        except ValueError:  # "..." / "-" = ainda nao publicado
            continue
        por_ano.setdefault(int(linha["D3C"]), {})[UF_DO_CODIGO[linha["D1C"]]] = valor
    completos = [a for a, v in por_ano.items() if set(v) >= set(ufs)]
    if not completos:
        raise ValueError("o IBGE não devolveu a área de todos os estados")
    ano = max(completos)
    return ano, {u: por_ano[ano][u] for u in ufs}


def ufs_do_portal(db):
    """Estados que ja estao no quadro + estados que tem cliente."""
    ufs = {a.uf for a in db.query(AreaEstado).all()}
    ufs |= {u for (u,) in db.query(ClienteCRM.uf).distinct() if u}
    return sorted(u for u in ufs if u in CODIGO_UF)


def atualizar(db, gravar=True):
    """-> (ano, [(uf, antes, depois)]). Grava com a fonte e placeholder=False."""
    ufs = ufs_do_portal(db)
    ano, areas = buscar(ufs)
    mudancas = []
    for uf in ufs:
        linha = db.get(AreaEstado, uf)
        antes = linha.area_agropecuaria_ha if linha else None
        mudancas.append((uf, antes, areas[uf]))
        if gravar:
            if linha is None:
                linha = AreaEstado(uf=uf, area_agropecuaria_ha=areas[uf])
                db.add(linha)
            linha.area_agropecuaria_ha = areas[uf]
            linha.fonte = fonte(ano)
            linha.placeholder = False
    if gravar:
        db.commit()
    return ano, mudancas


if __name__ == "__main__":
    from .database import SessionLocal
    banco = SessionLocal()
    gravar = "--gravar" in sys.argv
    ano, mudancas = atualizar(banco, gravar=gravar)
    print(f"IBGE, PAM {ano}:")
    for uf, antes, depois in mudancas:
        print(f"  {uf}: {antes if antes is None else f'{antes:,.0f}'} -> {depois:,.0f} ha")
    print("GRAVADO" if gravar else "(simulacao: nada gravado; use --gravar)")
