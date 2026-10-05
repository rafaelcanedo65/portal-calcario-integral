"""Renumera os pedidos do portal: 1056xx -> xx (aparece como PV-00xx). Ver
GUIA_DESENVOLVEDOR.md, secao 7. Ja aplicado no banco do prototipo (03/10/2026);
num banco ja renumerado ele para sozinho (os numeros nao comecam mais em 1056).
Uso: python renumerar_banco.py <caminho do portal.db> [gravar]   (sem "gravar" so mostra)."""
import json
import re
import sqlite3
import sys

if len(sys.argv) < 2:
    sys.exit("Uso: python renumerar_banco.py <caminho do portal.db> [gravar]")
GRAVAR = "gravar" in sys.argv[2:]
d = sqlite3.connect(sys.argv[1])
antigos = [n for (n,) in d.execute("select numero from crm_pedidos order by numero")]
mapa = {n: n - 105600 for n in antigos}
if not all(105601 <= n <= 105699 for n in antigos):
    sys.exit(f"Numeros fora de 105601-105699 ({antigos[:3]}...): banco ja renumerado? Nada feito.")
codigo = lambda n: f"PV-{n:04d}"  # noqa: E731
TIPOS_PEDIDO = ("pedido", "pedido_cancelado", "pedido_finalizado", "destino_final", "destino_registrado")
# "Pedido no 105601" / "pedido nº 105601" / "105601" solto -> "PV-0001" (nunca um 1056xx/SOxxxx do NetSuite)
PADRAO = re.compile(r"(?:\b(?:nº|no|Nº)\s)?\b(1056\d\d)\b(?!/SO)")


def trocar_texto(t):
    if not t:
        return t
    return PADRAO.sub(lambda m: codigo(mapa[int(m.group(1))]) if int(m.group(1)) in mapa else m.group(0), t)


mudancas = []
for cid, tipo, numero, texto in d.execute("select id, tipo, numero, texto from crm_contatos"):
    novo_num = mapa.get(numero) if tipo in TIPOS_PEDIDO and numero in mapa else numero
    novo_txt = trocar_texto(texto)
    if novo_num != numero or novo_txt != texto:
        mudancas.append(("crm_contatos", cid, numero, novo_num, texto, novo_txt))
for aid, titulo, texto in d.execute("select id, titulo, texto from crm_avisos"):
    nt, nx = trocar_texto(titulo), trocar_texto(texto)
    if (nt, nx) != (titulo, texto):
        mudancas.append(("crm_avisos", aid, titulo, nt, texto, nx))
for pid, js in d.execute("select id, credito_origem_json from crm_pedidos where credito_origem_json is not null"):
    lista = json.loads(js)
    for item in lista:
        item["numero"] = mapa.get(item["numero"], item["numero"])
    mudancas.append(("credito_json", pid, js, json.dumps(lista), None, None))

print(f"pedidos: {len(antigos)} ({antigos[0]}..{antigos[-1]} -> {codigo(mapa[antigos[0]])}..{codigo(mapa[antigos[-1]])})")
print("mudancas:", {t: sum(1 for m in mudancas if m[0] == t) for t in ("crm_contatos", "crm_avisos", "credito_json")})
for m in mudancas[:5] + [m for m in mudancas if m[0] != "crm_contatos"][:4]:
    print("  ", m[0], m[1], "|", str(m[4] if m[0] != "credito_json" else m[2])[:90], "\n      ->", str(m[5] if m[0] != "credito_json" else m[3])[:90])
# nada de 1056xx do portal pode sobrar sem trocar
sobra = [m for m in mudancas if m[5] and re.search(r"\b1056\d\d\b(?!/SO)", m[5])]
print("textos que ainda teriam 1056xx:", len(sobra))

if GRAVAR:
    for n in antigos:
        d.execute("update crm_pedidos set numero = ? where numero = ?", (mapa[n], n))
    for m in mudancas:
        if m[0] == "crm_contatos":
            d.execute("update crm_contatos set numero = ?, texto = ? where id = ?", (m[3], m[5], m[1]))
        elif m[0] == "crm_avisos":
            d.execute("update crm_avisos set titulo = ?, texto = ? where id = ?", (m[3], m[5], m[1]))
        else:
            d.execute("update crm_pedidos set credito_origem_json = ? where id = ?", (m[3], m[1]))
    d.commit()
    print("GRAVADO. pedidos agora:", d.execute("select min(numero), max(numero), count(*) from crm_pedidos").fetchone())
