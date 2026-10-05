"""Onde ficam os pedidos (Rafael, 2026-10-03: a Logistica sabe de caminhao indo
pra uma regiao e quer frete retorno -- precisa ver onde tem pedido).

Coordenadas dos municipios: static/municipios_coordenadas.json (IBGE, via
github.com/kelvins/municipios-brasileiros, baixado em 03/10/2026). O centro do
municipio entra quando nao temos o ponto exato da fazenda: serve pra regiao e
raio em km. O mapa da tela e o OpenStreetMap (Leaflet) e a rota de estrada
abre no Google Maps (link_rota) -- sem chave nem custo."""
import json
import math
import os
import unicodedata

_ARQ = os.path.join(os.path.dirname(__file__), "static", "municipios_coordenadas.json")
_dados = None
_indice = None


def _normalizar(nome):
    t = unicodedata.normalize("NFD", nome or "").encode("ascii", "ignore").decode().lower()
    return " ".join(t.replace("'", " ").replace("-", " ").split())


def _carregar():
    global _dados, _indice
    if _dados is None:
        _dados = json.load(open(_ARQ, encoding="utf-8"))
        _indice = {(uf, _normalizar(nome)): (nome, coord)
                   for uf, cidades in _dados["municipios"].items() for nome, coord in cidades.items()}
    return _dados


def municipio(uf, cidade):
    """-> (nome oficial, (lat, lon)) ou None. Tolera acento, maiuscula e hifen."""
    if not uf or not cidade:
        return None
    _carregar()
    achado = _indice.get((uf.upper(), _normalizar(cidade)))
    return (achado[0], tuple(achado[1])) if achado else None


def ufs():
    return sorted(_carregar()["municipios"])


def cidades_da_uf(uf):
    return sorted(_carregar()["municipios"].get((uf or "").upper(), {}))


def distancia_km(a, b):
    """Linha reta entre dois pontos (lat, lon). A estrada e mais longa."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def link_rota(destino, origem):
    """Botao "Navegar no Google Maps" da janela de rota (rotas.py): abre o Google
    Maps (o app, no celular) com a rota pronta. Sem chave nem custo.
    destino/origem: (lat, lon) ou texto ("Paragominas, PA"). A origem e
    obrigatoria: sem ela o Google parte de onde a pessoa esta, e a rota tem que
    sair das nossas plantas (Rafael, 2026-10-03)."""
    from urllib.parse import urlencode

    def lugar(x):
        return f"{x[0]:.6f},{x[1]:.6f}" if isinstance(x, (tuple, list)) else f"{x}, Brasil"

    params = {"api": "1", "origin": lugar(origem), "destination": lugar(destino), "travelmode": "driving"}
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def link_ponto(coord):
    return f"https://www.google.com/maps/search/?api=1&query={coord[0]:.6f},{coord[1]:.6f}"
