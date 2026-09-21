"""Importa os clientes REAIS do CRM, extraidos do mockup painel-clientes.html
(variavel JS CLIENTS_DATA_BASE, que contem um export de verdade da planilha
"Controle" — ver app/extract_real_data.py). Substitui os dados de exemplo
gerados por crm_seed.py.

Uso:
    python -m app.extract_real_data      # gera crm_dados_reais.json
    python -m app.crm_import_real
"""
import datetime as dt
import json
import re
import unicodedata

from .auth import hash_password
from .database import Base, SessionLocal, engine
from .models import DATA_DESCONHECIDA, ClienteCRM, ContatoCRM, User

DADOS_JSON = "crm_dados_reais.json"

# Nomes de vendedor conhecidos no portal hoje (vieram da planilha de Pedidos).
VENDEDORES_PORTAL = [
    "Monica Silva Oliveira", "Zilma Bispo Reis", "Wagner Souza dos Santos",
    "Sidney Passos de Sousa", "Camila Freitas SANTOS", "Elaine Vasconcelos", "FABRICA",
]


def normalizar(txto):
    txto = unicodedata.normalize("NFKD", txto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]", "", txto.lower())


MAPA_NORMALIZADO = {normalizar(v): v for v in VENDEDORES_PORTAL}
# variantes/apelidos que devem apontar pro mesmo vendedor do portal
MAPA_NORMALIZADO["zilma"] = "Zilma Bispo Reis"
MAPA_NORMALIZADO["zilmabispo"] = "Zilma Bispo Reis"
MAPA_NORMALIZADO["sidney"] = "Sidney Passos de Sousa"
MAPA_NORMALIZADO["camila"] = "Camila Freitas SANTOS"
MAPA_NORMALIZADO["camilafreitass"] = "Camila Freitas SANTOS"
MAPA_NORMALIZADO["wagner"] = "Wagner Souza dos Santos"
MAPA_NORMALIZADO["monica"] = "Monica Silva Oliveira"
MAPA_NORMALIZADO["alana"] = "Allana Figueiredo"  # apelido curto da mesma pessoa
MAPA_NORMALIZADO["marcos"] = "Marcos vasconcelos ferreira"  # apelido curto da mesma pessoa

SEM_VENDEDOR = {"", "—", "vendedor", None}


def normalizar_vendedor(bruto):
    if not bruto or bruto.strip().lower() in SEM_VENDEDOR:
        return None
    chave = normalizar(bruto)
    return MAPA_NORMALIZADO.get(chave, bruto.strip())


def slugify(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    nome = re.sub(r"[^a-zA-Z0-9]+", ".", nome.strip().lower()).strip(".")
    return nome or "usuario"


def parse_data_br(txto):
    try:
        return dt.datetime.strptime(txto, "%d/%m/%Y")
    except (ValueError, TypeError):
        return None


def parse_ts(ms):
    if not ms:
        return None
    return dt.datetime.utcfromtimestamp(ms / 1000)


def montar_notas(registro):
    """Converte historico/compras/retiradas/proposta num conjunto de
    ContatoCRM, ja que ainda nao temos tabelas dedicadas pra cada um.
    Cada item: (tipo, data, texto, fase_destino_ou_None)."""
    notas = []
    for h in registro.get("historico") or []:
        data = parse_data_br(h.get("data")) or DATA_DESCONHECIDA
        notas.append(("nota", data, h.get("nota") or "", None))

    for c in registro.get("compras") or []:
        data = parse_data_br(c.get("data")) or DATA_DESCONHECIDA
        notas.append(("compra", data, f"Compra registrada: {c.get('volume')} ({c.get('pagamento')}).", None))

    for r in registro.get("retiradas") or []:
        data = parse_data_br(r.get("data")) or DATA_DESCONHECIDA
        notas.append(("retirada", data, f"Retirada registrada: {r.get('volume')}t.", None))

    if registro.get("propostaValor"):
        data = parse_ts(registro.get("propostaTimestamp")) or DATA_DESCONHECIDA
        partes = []
        if registro.get("propostaCalcarioVolume"):
            partes.append(f"Calcario {registro['propostaCalcarioVolume']}t a R$ {registro.get('propostaCalcarioPreco') or 0:.2f}")
        if registro.get("propostaGessoVolume"):
            partes.append(f"Gesso {registro['propostaGessoVolume']}t a R$ {registro.get('propostaGessoPreco') or 0:.2f}")
        texto = (f"Proposta: {' + '.join(partes) if partes else 'sem detalhe de produto'} · "
                 f"{registro.get('propostaPagamento') or '-'} ({registro.get('propostaPrazo') or '-'}) · "
                 f"Valor total: R$ {registro['propostaValor']:.2f}")
        notas.append(("proposta", data, texto, None))
        notas.append(("mudanca_fase", data, "Etapa: Proposta enviada.", "proposta"))

    if registro.get("fase") == "perdido" and registro.get("perdidoTimestamp"):
        notas.append(("mudanca_fase", parse_ts(registro["perdidoTimestamp"]), "Marcado como perdido.", "perdido"))
    if registro.get("fase") == "contactado" and registro.get("contactadoTimestamp"):
        notas.append(("mudanca_fase", parse_ts(registro["contactadoTimestamp"]), "Marcado como contactado.", "contactado"))
    if registro.get("fase") == "realizado" and registro.get("realizadoTimestamp"):
        notas.append(("mudanca_fase", parse_ts(registro["realizadoTimestamp"]), "Venda realizada.", "realizado"))

    notas.sort(key=lambda n: n[1])
    return notas


def importar():
    Base.metadata.create_all(bind=engine)
    with open(DADOS_JSON, encoding="utf-8") as f:
        dados = json.load(f)

    db = SessionLocal()
    try:
        apagados_contatos = db.query(ContatoCRM).delete()
        apagados_clientes = db.query(ClienteCRM).delete()
        db.commit()
        print(f"Removidos {apagados_clientes} clientes de exemplo e {apagados_contatos} contatos antigos.")

        vendedores_novos = set()
        importados = 0
        for registro in dados:
            vendedor = normalizar_vendedor(registro.get("vendedor"))
            if vendedor and vendedor not in VENDEDORES_PORTAL:
                vendedores_novos.add(vendedor)

            volume_retirado = sum((r.get("volume") or 0) for r in (registro.get("retiradas") or []))

            cliente = ClienteCRM(
                id_origem=registro.get("id"),
                fazenda=registro.get("nome") or f"Cliente {registro.get('id')}",
                proprietario=registro.get("proprietario") or None,
                empresa=registro.get("empresa") or None,
                telefone=registro.get("telefone") or None,
                email=registro.get("email") or None,
                cnpj=registro.get("cnpj") or None,
                cidade=registro.get("cidade") or None,
                uf=registro.get("uf") or "??",
                forma_pagamento=registro.get("formaPagamento") or None,
                frota_propria=(True if registro.get("frota") == "Sim" else (False if registro.get("frota") == "Não" else None)),
                area_plantada_ha=registro.get("area"),
                coordenadas=registro.get("coordenadas") or None,
                fase=registro.get("fase") or "a_contactar",
                vendedor_nome=vendedor,
                categoria_dado=registro.get("categoria"),
                volume_contratado=registro.get("volumeContratado"),
                volume_retirado=volume_retirado or None,
                proposta_valor=registro.get("propostaValor"),
                proposta_em=parse_ts(registro.get("propostaTimestamp")),
                temperatura=registro.get("temperatura"),
                precisa_ajuda=bool(registro.get("precisaAjudaContato")),
                motivo_ajuda=registro.get("motivoAjudaContato"),
            )
            db.add(cliente)
            db.flush()

            notas = montar_notas(registro)
            for tipo, data, texto, fase_destino in notas:
                if texto:
                    db.add(ContatoCRM(cliente_id=cliente.id, tipo=tipo, data=data, texto=texto, fase_destino=fase_destino))
            if notas:
                cliente.ultima_interacao_em = notas[-1][1]

            importados += 1

        db.commit()
        print(f"Importados {importados} clientes reais.")

        if vendedores_novos:
            print("\nVendedores encontrados nos dados que ainda nao tem login no portal:")
            criados = []
            for nome in sorted(vendedores_novos):
                if "/" in nome:
                    continue  # combo de dois vendedores, nao e uma pessoa de verdade
                username = slugify(nome)
                if db.query(User).filter_by(username=username).first():
                    continue
                senha = "troque-esta-senha"
                db.add(User(username=username, password_hash=hash_password(senha), nome_completo=nome,
                             role="vendedor", vendedor_nome=nome))
                criados.append((username, senha, nome))
            db.commit()
            for username, senha, nome in criados:
                print(f"  usuario={username:30s} senha={senha:20s} ({nome})")
    finally:
        db.close()


if __name__ == "__main__":
    importar()
