"""Rota de estrada das nossas plantas ate o cliente, mostrada DENTRO do portal
(Rafael, 2026-10-03: "quando entra no google maps nao consegui voltar para
dentro do portal" e "nao pode usar a localizacao do usuario para definir rota,
tem que ter a opcao das nossas duas plantas").

- Origem: sempre as plantas (PLANTAS), as duas lado a lado com km e tempo --
  nunca onde a pessoa esta.
- Calculo: servico gratuito do OpenStreetMap (OSRM, router.project-osrm.org),
  sem chave. So vai pra la o par de coordenadas (nada de nome de cliente). E um
  servidor de demonstracao, sem garantia: cada rota calculada fica guardada
  (RotaCache) e, se o servico falhar, a tela mostra a linha reta e diz que a
  estrada nao foi calculada -- nunca um numero inventado. Na producao, se o uso
  crescer: chave gratuita do OpenRouteService ou servidor OSRM proprio.
- O Google Maps fica so como botao "para o motorista navegar", ja com a planta
  como origem."""
import json
import threading
import time
import urllib.request

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import config, geo
from .auth import get_current_user
from .database import get_db
from .models import LOCAL_FONTE_ROTULO, Pedido, RotaCache, User

router = APIRouter()

# Plantas (Rafael, 2026-10-03). O ponto exato e informado pelo admin na pagina
# Regras (config "planta_sao_geraldo"/"planta_grajau"); sem ele usa o centro da
# cidade e a tela avisa "ponto aproximado".
PLANTAS = (
    {"chave": "sao_geraldo", "cidade": "São Geraldo do Araguaia", "uf": "PA", "config": "planta_sao_geraldo"},
    {"chave": "grajau", "cidade": "Grajaú", "uf": "MA", "config": "planta_grajau"},
)
OSRM = "https://router.project-osrm.org/route/v1/driving/{olon},{olat};{dlon},{dlat}?overview=full&geometries=polyline"
TEMPO_LIMITE = 15  # segundos por rota
_trava = threading.Lock()
_ultima_chamada = [0.0]


def plantas():
    """-> [{chave, nome, coord, aproximado}] (nome = "Cidade/UF")."""
    lista = []
    for p in PLANTAS:
        exato = config.valor(p["config"])  # "lat, lon" ou None
        coord = tuple(float(x) for x in exato.split(",")) if exato else geo.municipio(p["uf"], p["cidade"])[1]
        lista.append({"chave": p["chave"], "nome": f"{p['cidade']}/{p['uf']}", "coord": tuple(coord),
                      "aproximado": not exato})
    return lista


def _decodificar(polyline):
    """Polyline codificada (precisao 5) -> [[lat, lon], ...]."""
    pontos, i, lat, lon = [], 0, 0, 0
    while i < len(polyline):
        for eixo in (0, 1):
            resultado, deslocamento = 0, 0
            while True:
                b = ord(polyline[i]) - 63
                i += 1
                resultado |= (b & 0x1F) << deslocamento
                deslocamento += 5
                if b < 0x20:
                    break
            delta = ~(resultado >> 1) if resultado & 1 else resultado >> 1
            if eixo == 0:
                lat += delta
            else:
                lon += delta
        pontos.append([lat / 1e5, lon / 1e5])
    return pontos


def calcular_rota(db, origem, destino):
    """-> {"km", "minutos", "trajeto": [[lat, lon]...]} ou None se o servico
    nao respondeu (nao guarda falha: tenta de novo na proxima vez)."""
    chave = f"{origem[0]:.5f},{origem[1]:.5f}>{destino[0]:.5f},{destino[1]:.5f}"
    salva = db.query(RotaCache).filter_by(chave=chave).first()
    if salva is None:
        url = OSRM.format(olat=origem[0], olon=origem[1], dlat=destino[0], dlon=destino[1])
        with _trava:  # o servidor de demonstracao pede no maximo ~1 pedido por segundo
            espera = 1.0 - (time.time() - _ultima_chamada[0])
            if espera > 0:
                time.sleep(espera)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PortalCalcarioIntegral/1.0 (logistica interna)"})
                dados = json.load(urllib.request.urlopen(req, timeout=TEMPO_LIMITE))
            except Exception:
                return None
            finally:
                _ultima_chamada[0] = time.time()
        if dados.get("code") != "Ok" or not dados.get("routes"):
            return None
        r = dados["routes"][0]
        salva = RotaCache(chave=chave, km=r["distance"] / 1000, minutos=r["duration"] / 60, trajeto=r["geometry"])
        db.add(salva)
        db.commit()
    return {"km": salva.km, "minutos": salva.minutos, "trajeto": _decodificar(salva.trajeto)}


def rotas_ate(db, destino):
    """Rota de cada planta ate `destino` (lat, lon), da mais curta pra mais longa."""
    saida = []
    for p in plantas():
        r = calcular_rota(db, p["coord"], destino)
        saida.append({
            "planta": p["nome"], "chave": p["chave"], "origem": p["coord"], "aproximado": p["aproximado"],
            "linha_reta_km": round(geo.distancia_km(p["coord"], destino)),
            "km": round(r["km"]) if r else None, "minutos": round(r["minutos"]) if r else None,
            "trajeto": r["trajeto"] if r else None,
            "google": geo.link_rota(destino, origem=p["coord"]),
        })
    saida.sort(key=lambda x: (x["km"] is None, x["km"] if x["km"] is not None else x["linha_reta_km"]))
    return saida


def _sem_destino(texto):
    return JSONResponse({"erro": texto}, status_code=200)


@router.get("/api/rota")
def api_rota(request: Request, pedido: int = 0, cliente: int = 0, uf: str = "", cidade: str = "",
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Destino: um pedido da Logistica, um cliente do CRM (fazenda ou cidade do
    cadastro) ou uma cidade (bolha do mapa de regiao)."""
    if pedido:
        if user.role not in ("admin", "logistica"):
            return JSONResponse({"erro": "Sem acesso."}, status_code=403)
        p = db.get(Pedido, pedido)
        if p is None:
            return _sem_destino("Pedido não encontrado.")
        if p.latitude is None:
            if p.local_fonte == "transportadora sem destino":
                return _sem_destino("Venda para transportadora sem destino final: informe a cidade de entrega na ficha do pedido.")
            return _sem_destino("Este pedido ainda não tem local de entrega. Informe na ficha do pedido (\"Informar local\").")
        destino = (p.latitude, p.longitude)
        titulo = p.cliente
        descricao = (f"{p.cidade}/{p.uf}" if p.cidade else (p.uf or "")) + " · " + LOCAL_FONTE_ROTULO.get(p.local_fonte, "")
        exato = p.local_exato
    elif cliente:
        if user.role not in ("admin", "vendedor", "logistica"):
            return JSONResponse({"erro": "Sem acesso."}, status_code=403)
        from .crm_routes import _cliente_do_usuario  # import tardio: crm_routes carrega o app inteiro
        from .models import ler_coordenadas
        c = _cliente_do_usuario(db, user, cliente, permitir_via_parceiro=True)  # mesma regra da ficha
        if c is None:
            return JSONResponse({"erro": "Cliente fora da sua carteira."}, status_code=403)
        ponto = ler_coordenadas(c.coordenadas) if c.coordenadas else None
        achado = None if ponto else geo.municipio(c.uf, c.cidade)
        if not ponto and not achado:
            return _sem_destino("O cadastro deste cliente não tem coordenada nem cidade reconhecida.")
        destino = ponto or achado[1]
        titulo = c.fazenda
        descricao = ("coordenada do cadastro" if ponto else f"centro da cidade ({achado[0]}/{c.uf}); sem coordenada da fazenda no cadastro")
        exato = bool(ponto)
    else:
        if user.role not in ("admin", "logistica"):
            return JSONResponse({"erro": "Sem acesso."}, status_code=403)
        achado = geo.municipio(uf, cidade)
        if not achado:
            return _sem_destino("Cidade não encontrada.")
        destino = achado[1]
        titulo = f"{achado[0]}/{uf.upper()}"
        descricao = "centro da cidade"
        exato = False
    rotas = rotas_ate(db, destino)
    return JSONResponse({"titulo": titulo, "descricao": descricao, "exato": exato, "destino": destino, "rotas": rotas})
