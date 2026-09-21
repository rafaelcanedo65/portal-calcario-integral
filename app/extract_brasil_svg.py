"""Extrai so os paths dos estados (grupo 'Destaque') do SVG bruto do Brasil
(baixado do Wikimedia Commons) e monta um SVG limpo, so com os 27 estados,
prontos pra estilizar/linkar no mapa do CRM.
"""
import xml.etree.ElementTree as ET

SRC = r"C:\Users\rafae\AppData\Local\Temp\brasil_bruto.svg"
OUT = r"C:\Claude\portal\app\static\brasil_estados.json"

NS = {"svg": "http://www.w3.org/2000/svg"}

tree = ET.parse(SRC)
root = tree.getroot()
view_box = root.get("viewBox")
print("viewBox original:", view_box)

estados = {}
for path in root.iter("{http://www.w3.org/2000/svg}path"):
    pid = path.get("id") or ""
    if not pid.startswith("state-"):
        continue
    uf = pid.split("-")[1].upper()
    d = path.get("d")
    titulo_el = path.find("svg:title", NS)
    nome = titulo_el.text if titulo_el is not None else uf
    estados[uf] = {"d": d, "nome": nome}

print(f"Encontrados {len(estados)} estados")
for uf in sorted(estados):
    print(" ", uf, estados[uf]["nome"], len(estados[uf]["d"]), "chars")

import re


def bbox_do_path(d):
    """Calcula a caixa delimitadora de um path SVG (suporta M/m,L/l,H/h,V/v,
    C/c,Z/z -- o suficiente pros paths deste mapa, todos poligonais)."""
    tokens = re.findall(r"[MmLlHhVvCcZz]|-?\d+(?:\.\d+)?", d)
    x = y = 0.0
    start_x = start_y = 0.0
    cmd = None
    xs, ys = [], []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            continue
        if cmd in ("M", "m"):
            nx, ny = float(tokens[i]), float(tokens[i + 1])
            x, y = (nx, ny) if cmd == "M" else (x + nx, y + ny)
            start_x, start_y = x, y
            i += 2
            cmd = "L" if cmd == "M" else "l"
        elif cmd in ("L", "l"):
            nx, ny = float(tokens[i]), float(tokens[i + 1])
            x, y = (nx, ny) if cmd == "L" else (x + nx, y + ny)
            i += 2
        elif cmd in ("H", "h"):
            nx = float(tokens[i])
            x = nx if cmd == "H" else x + nx
            i += 1
        elif cmd in ("V", "v"):
            ny = float(tokens[i])
            y = ny if cmd == "V" else y + ny
            i += 1
        elif cmd in ("C", "c"):
            vals = [float(tokens[i + k]) for k in range(6)]
            if cmd == "c":
                vals = [vals[0] + x, vals[1] + y, vals[2] + x, vals[3] + y, vals[4] + x, vals[5] + y]
            xs.extend([vals[0], vals[2], vals[4]])
            ys.extend([vals[1], vals[3], vals[5]])
            x, y = vals[4], vals[5]
            i += 6
        elif cmd in ("Z", "z"):
            x, y = start_x, start_y
        else:
            i += 1
            continue
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


for uf, info in estados.items():
    x0, y0, x1, y1 = bbox_do_path(info["d"])
    info["cx"] = round((x0 + x1) / 2)
    info["cy"] = round((y0 + y1) / 2)

import json
with open(OUT, "w", encoding="utf-8") as f:
    json.dump({"viewBox": view_box, "estados": estados}, f, ensure_ascii=False)
print("Salvo em", OUT)
