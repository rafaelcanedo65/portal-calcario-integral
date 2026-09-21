"""Popula o CRM (crm_clientes, crm_contatos, crm_area_estados) com dados de
EXEMPLO, so para validar as telas de design antes de ligar na planilha
"Controle" de verdade (que ainda precisa ser entendida/organizada).

Uso:
    python -m app.crm_seed
"""
import datetime as dt
import random

from .database import Base, SessionLocal, engine
from .models import FASES_CRM, AreaEstado, ClienteCRM, ContatoCRM, User

random.seed(42)

# Placeholder: area agropecuaria por UF (hectares). NAO sao numeros oficiais
# ainda -- marcar como placeholder ate confirmar com fonte IBGE/SIDRA real.
AREA_ESTADOS_PLACEHOLDER = {
    "MA": 15_000_000,
    "PA": 24_000_000,
    "TO": 17_000_000,
    "PI": 12_000_000,
    "MT": 62_000_000,
    "GO": 29_000_000,
}

CIDADES_POR_UF = {
    "MA": ["Balsas", "Riachao", "Barra do Corda", "Imperatriz", "Tasso Fragoso"],
    "PA": ["Paragominas", "Redencao", "Maraba", "Santana do Araguaia", "Tailandia", "Conceicao do Araguaia"],
    "TO": ["Gurupi", "Araguaina", "Paraiso do Tocantins", "Formoso do Araguaia"],
    "PI": ["Urucui", "Bom Jesus", "Corrente", "Baixa Grande do Ribeiro"],
    "MT": ["Rondonopolis", "Sinop", "Sorriso", "Primavera do Leste"],
    "GO": ["Rio Verde", "Jatai", "Mineiros", "Cristalina"],
}

NOMES = ["Carlos", "Marcos", "Ana", "Julia", "Roberto", "Fernanda", "Paulo", "Luciana",
         "Ricardo", "Camila", "Eduardo", "Patricia", "Gustavo", "Renata", "Bruno",
         "Vanessa", "Sergio", "Aline", "Diego", "Marina"]
SOBRENOMES = ["Silva", "Souza", "Oliveira", "Santos", "Pereira", "Almeida", "Costa",
              "Ferreira", "Rodrigues", "Carvalho", "Martins", "Barbosa"]
SUFIXOS_FAZENDA = ["Agropecuaria", "Fazenda", "Agro", "Grupo"]

FORMAS_PAGAMENTO = ["Boleto 30 dias", "A vista", "Boleto 60 dias", None]

VENDEDORES = [
    "Monica Silva Oliveira", "Zilma Bispo Reis", "Wagner Souza dos Santos",
    "Sidney Passos de Sousa", "Camila Freitas SANTOS", "Elaine Vasconcelos",
]

# distribuicao aproximada de fase, inspirada no mockup (maioria ainda no topo do funil)
PESOS_FASE = {"a_contactar": 35, "contactado": 25, "proposta": 12,
              "realizado": 18, "nao_usara": 4, "perdido": 6}


def nome_aleatorio():
    return f"{random.choice(NOMES)} {random.choice(SOBRENOMES)}"


def fazenda_aleatoria(cidade):
    return f"{random.choice(SUFIXOS_FAZENDA)} {random.choice(SOBRENOMES)} - {cidade}"


def sortear_fase():
    fases = list(PESOS_FASE.keys())
    pesos = list(PESOS_FASE.values())
    return random.choices(fases, weights=pesos, k=1)[0]


def gerar_clientes(qtd_por_uf=14):
    clientes = []
    for uf, cidades in CIDADES_POR_UF.items():
        for _ in range(qtd_por_uf):
            cidade = random.choice(cidades)
            proprietario = nome_aleatorio()
            fase = sortear_fase()
            area = round(random.uniform(80, 6000), 1)
            dias_atras = random.randint(0, 120)
            clientes.append(ClienteCRM(
                fazenda=fazenda_aleatoria(cidade),
                proprietario=proprietario,
                empresa=f"{proprietario.split()[0].upper()} EMPREENDIMENTOS AGRICOLAS LTDA" if random.random() > 0.4 else None,
                telefone=f"{random.randint(63, 99)} 9{random.randint(1000,9999)}-{random.randint(1000,9999)}",
                email=None,
                cnpj=None,
                cidade=cidade,
                uf=uf,
                forma_pagamento=random.choice(FORMAS_PAGAMENTO),
                frota_propria=random.random() > 0.6,
                area_plantada_ha=area,
                coordenadas=None,
                fase=fase,
                vendedor_nome=random.choice(VENDEDORES),
                ultima_interacao_em=dt.datetime.utcnow() - dt.timedelta(days=dias_atras),
                proximo_retorno_em=(dt.date.today() + dt.timedelta(days=random.randint(-3, 25)))
                if random.random() > 0.75 else None,
            ))
    return clientes


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(ClienteCRM).count() > 0:
            print("Ja existem clientes no CRM, nada foi alterado. "
                  "Apague a tabela crm_clientes manualmente se quiser regerar os dados de exemplo.")
            return

        for uf, area in AREA_ESTADOS_PLACEHOLDER.items():
            db.add(AreaEstado(uf=uf, area_agropecuaria_ha=area,
                               fonte="valor de exemplo, nao confirmado com IBGE/SIDRA ainda",
                               placeholder=True))

        clientes = gerar_clientes()
        db.add_all(clientes)
        db.commit()

        for c in random.sample(clientes, k=min(30, len(clientes))):
            db.add(ContatoCRM(cliente_id=c.id, tipo="nota", texto="Importado da planilha de controle (exemplo)."))
        db.commit()

        print(f"CRM populado com {len(clientes)} clientes de exemplo em {len(AREA_ESTADOS_PLACEHOLDER)} estados.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
