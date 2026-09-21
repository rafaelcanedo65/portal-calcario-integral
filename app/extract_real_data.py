"""Extrai o array CLIENTS_DATA_BASE embutido no mockup painel-clientes.html
(dado real da planilha Controle) e salva como JSON limpo pra inspecionar
antes de importar de verdade.
"""
import json
import re

SRC = r"C:\Claude\frontend_referencias\painel-clientes.html"
OUT = r"C:\Claude\portal\crm_dados_reais.json"


def extrair_array(texto, marcador):
    idx = texto.index(marcador)
    inicio = texto.index("[", idx)
    profundidade = 0
    dentro_string = False
    escape = False
    aspas = ""
    for i in range(inicio, len(texto)):
        ch = texto[i]
        if dentro_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == aspas:
                dentro_string = False
        else:
            if ch in ("'", '"'):
                dentro_string = True
                aspas = ch
            elif ch == "[":
                profundidade += 1
            elif ch == "]":
                profundidade -= 1
                if profundidade == 0:
                    return texto[inicio:i + 1]
    raise ValueError("Nao fechou o array")


with open(SRC, encoding="utf-8") as f:
    html = f.read()

bruto = extrair_array(html, "const CLIENTS_DATA_BASE = ")
dados = json.loads(bruto)

print(f"Total de clientes: {len(dados)}")
print("Chaves do primeiro registro:", list(dados[0].keys()))
from collections import Counter
print("Por UF:", Counter(d.get("uf") for d in dados))
print("Por fase:", Counter(d.get("fase") for d in dados))
print("Vendedores distintos:", sorted(set(d.get("vendedor") for d in dados if d.get("vendedor"))))

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dados, f, ensure_ascii=False, indent=1)
print(f"\nSalvo em {OUT}")
