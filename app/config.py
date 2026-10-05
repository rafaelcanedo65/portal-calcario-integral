"""Valores das regras que o admin muda pela pagina Regras (etapa 2, Rafael,
2026-10-04). Cada numero/liga-desliga tem um padrao aqui (o que valia no
codigo); a tabela `config_regras` so guarda o que o admin mudou, e
`config_historico` guarda toda mudanca (quem, quando, de -> para).

Uso no codigo: `config.valor("log_dias_parou")`. E lido na hora (cache de
poucos segundos), entao a mudanca vale pra todos sem reiniciar o servidor.

`simular({...})` troca valores so durante um bloco, so nesta thread: e assim
que a tela mostra a previa ("a fila passa de 370 para 352") antes de salvar."""
import contextlib
import datetime as dt
import json
import threading
import time

# tipo: int | liga | dia_mes ([dia, mes]) | coord ("lat, lon" ou None = centro da cidade) | lista_int
#       | texto (nome de nivel) | ordem (lista de chaves, na ordem de prioridade)
# Ordem das filas (Rafael, 2026-10-04: arrastar pra definir a prioridade). Os nomes
# padrao sao os que ja apareciam nas telas.
ORDEM_LOGISTICA = ["respondeu", "vencido", "apertado", "sem_resposta", "parou", "nunca", "sem_prazo"]
NOMES_LOGISTICA = {"respondeu": "Vendedor respondeu", "vencido": "Prazo vencido", "apertado": "Retirada apertada",
                   "sem_resposta": "Cobrança sem resposta", "parou": "Parou de puxar", "nunca": "Nunca puxou",
                   "sem_prazo": "Sem data limite"}
ORDEM_VENDEDOR = ["vencido", "apertada", "oportunidade", "pedido_aberto", "quente", "epoca", "proposta", "sem_contato",
                  "primeiro_contato"]
NOMES_VENDEDOR = {"vencido": "Pedido vencido", "apertada": "Retirada apertada", "oportunidade": "Oportunidade logística",
                  "pedido_aberto": "Pedido em aberto",
                  "quente": "Proposta quente", "epoca": "Época de compra", "proposta": "Proposta sem retorno",
                  "sem_contato": "Sem contato", "primeiro_contato": "Primeiro contato"}
ORDEM_CADASTRO = ["sem_telefone", "ja_comprou", "maior_area"]

PARAMETROS = {
    "ordem_log": {"tipo": "ordem", "padrao": ORDEM_LOGISTICA},
    **{f"nome_log_{k}": {"tipo": "texto", "padrao": v} for k, v in NOMES_LOGISTICA.items()},
    "ordem_fila": {"tipo": "ordem", "padrao": ORDEM_VENDEDOR},
    **{f"nome_fila_{k}": {"tipo": "texto", "padrao": v} for k, v in NOMES_VENDEDOR.items()},
    "ordem_cad": {"tipo": "ordem", "padrao": ORDEM_CADASTRO},
    **{f"liga_cad_{k}": {"tipo": "liga", "padrao": True} for k in ORDEM_CADASTRO},
    # Ciclo de vendas
    "ciclo_virada": {"tipo": "dia_mes", "padrao": [1, 11]},
    "liga_ciclo_contador": {"tipo": "liga", "padrao": True},
    "ciclo_contador_dias": {"tipo": "int", "padrao": 31, "min": 1, "max": 120},
    # Fila de trabalho do vendedor
    "retirada_apertada_t_dia": {"tipo": "int", "padrao": 250, "min": 1, "max": 20000},  # vale pro vendedor e pra Logistica
    "liga_fila_apertada": {"tipo": "liga", "padrao": True},
    "liga_fila_oportunidade": {"tipo": "liga", "padrao": True},
    "oportunidade_dias": {"tipo": "int", "padrao": 7, "min": 1, "max": 60},  # validade sem data do caminhao
    "liga_fila_quente": {"tipo": "liga", "padrao": True},
    "liga_fila_epoca": {"tipo": "liga", "padrao": True},
    "fila_epoca_quente": {"tipo": "int", "padrao": 3, "min": 1, "max": 90},
    "fila_epoca_morno": {"tipo": "int", "padrao": 7, "min": 1, "max": 90},
    "fila_epoca_frio": {"tipo": "int", "padrao": 14, "min": 1, "max": 90},
    "liga_fila_proposta": {"tipo": "liga", "padrao": True},
    "fila_proposta_dias": {"tipo": "int", "padrao": 10, "min": 1, "max": 180},
    "fila_proposta_dias_perto": {"tipo": "int", "padrao": 3, "min": 1, "max": 180},
    "fila_proposta_janela": {"tipo": "int", "padrao": 30, "min": 0, "max": 180},
    "liga_fila_sem_contato": {"tipo": "liga", "padrao": True},
    "fila_sem_contato_dias": {"tipo": "int", "padrao": 60, "min": 7, "max": 365},
    "fila_sem_contato_lote": {"tipo": "int", "padrao": 10, "min": 1, "max": 500},
    "liga_fila_primeiro_contato": {"tipo": "liga", "padrao": True},
    "fila_primeiro_contato_lote": {"tipo": "int", "padrao": 15, "min": 1, "max": 500},
    "liga_contato_automatico": {"tipo": "liga", "padrao": True},
    "contato_tentativas": {"tipo": "int", "padrao": 3, "min": 1, "max": 20},
    # Cadastro
    "categoria_a_ha": {"tipo": "int", "padrao": 3000, "min": 1, "max": 1000000},
    "categoria_b_ha": {"tipo": "int", "padrao": 800, "min": 1, "max": 1000000},
    # Logistica
    "liga_log_apertada": {"tipo": "liga", "padrao": True},
    "log_dias_prazo_apertado": {"tipo": "int", "padrao": 7, "min": 0, "max": 90},
    "liga_log_parou": {"tipo": "liga", "padrao": True},
    "log_dias_parou": {"tipo": "int", "padrao": 15, "min": 1, "max": 365},
    "liga_log_nunca": {"tipo": "liga", "padrao": True},
    "log_dias_nunca": {"tipo": "int", "padrao": 15, "min": 1, "max": 365},
    "liga_log_cobranca": {"tipo": "liga", "padrao": True},
    "log_dias_cobranca": {"tipo": "int", "padrao": 3, "min": 1, "max": 60},
    "log_dias_sem_acao": {"tipo": "int", "padrao": 30, "min": 1, "max": 365},
    # Local de entrega e rota
    "planta_sao_geraldo": {"tipo": "coord", "padrao": None},
    "planta_grajau": {"tipo": "coord", "padrao": None},
    "regiao_raios": {"tipo": "lista_int", "padrao": [50, 100, 150, 200, 300], "min": 5, "max": 2000},
    # Comissao do vendedor (Rafael, 2026-10-04): abaixo do preco de corte paga um
    # percentual, a partir dele outro. Sulfato e pedra britada sem regra ate ele definir.
    **{f"comissao_{p}_{k}": v for p, padroes in {
        "calcario": {"liga": {"tipo": "liga", "padrao": True}, "corte": 70.0, "abaixo": 2.0, "acima": 3.0},
        "gesso": {"liga": {"tipo": "liga", "padrao": True}, "corte": 60.0, "abaixo": 2.0, "acima": 3.0},
        "sulfato": {"liga": {"tipo": "liga", "padrao": False}, "corte": 0.0, "abaixo": 0.0, "acima": 0.0},
        "pedra": {"liga": {"tipo": "liga", "padrao": False}, "corte": 0.0, "abaixo": 0.0, "acima": 0.0},
    }.items() for k, v in {
        "liga": padroes["liga"],
        "corte": {"tipo": "decimal", "padrao": padroes["corte"], "min": 0, "max": 100000},
        "abaixo": {"tipo": "decimal", "padrao": padroes["abaixo"], "min": 0, "max": 30},
        "acima": {"tipo": "decimal", "padrao": padroes["acima"], "min": 0, "max": 30},
    }.items()},
    # Gestao
    "admin_proposta_parada_dias": {"tipo": "int", "padrao": 10, "min": 1, "max": 365},
    "relatorio_limite_tela": {"tipo": "int", "padrao": 500, "min": 50, "max": 10000},
}

TTL = 10  # segundos: outro processo do servidor ve a mudanca em ate 10 s
_cache = {"quando": 0.0, "dados": None}
_trava = threading.Lock()
_simulacao = threading.local()


def _do_banco():
    from .database import SessionLocal
    from .models import ConfigRegra
    db = SessionLocal()
    try:
        return {c.chave: json.loads(c.valor) for c in db.query(ConfigRegra).all()}
    finally:
        db.close()


def _salvos():
    with _trava:
        if _cache["dados"] is None or time.monotonic() - _cache["quando"] > TTL:
            _cache["dados"] = _do_banco()
            _cache["quando"] = time.monotonic()
        return _cache["dados"]


def limpar_cache():
    with _trava:
        _cache["dados"] = None


def padrao(chave):
    return PARAMETROS[chave]["padrao"]


def valor(chave):
    """Valor que vale agora (simulado > salvo pelo admin > padrao)."""
    simulado = getattr(_simulacao, "valores", None)
    if simulado and chave in simulado:
        return simulado[chave]
    salvos = _salvos()
    if chave not in salvos:
        return PARAMETROS[chave]["padrao"]
    if PARAMETROS[chave]["tipo"] == "ordem":
        return _completar_ordem(chave, salvos[chave])
    return salvos[chave]


def _completar_ordem(chave, salva):
    """Ordem salva antes de existir um nivel novo: o novo entra na posicao padrao
    (e nivel que deixou de existir sai)."""
    padrao = PARAMETROS[chave]["padrao"]
    lista = [k for k in salva if k in padrao]
    for i, k in enumerate(padrao):
        if k not in lista:
            lista.insert(min(i, len(lista)), k)
    return lista


def ligada(chave):
    return bool(valor(chave))


def mudou(chave):
    return chave in _salvos()


@contextlib.contextmanager
def simular(valores):
    """Durante o bloco, nesta thread, `valor()` devolve estes valores."""
    antes = getattr(_simulacao, "valores", None)
    _simulacao.valores = dict(antes or {}, **valores)
    try:
        yield
    finally:
        _simulacao.valores = antes


# ---------- leitura do formulario e validacao

class ValorInvalido(ValueError):
    pass


def ler(chave, texto, rotulo):
    """Texto do formulario -> valor do tipo certo, ou ValorInvalido com a frase pro admin."""
    p = PARAMETROS[chave]
    tipo = p["tipo"]
    texto = (texto or "").strip()
    if tipo == "liga":
        return texto in ("1", "on", "true", "sim")
    if tipo == "int":
        try:
            n = int(texto.replace(".", ""))
        except ValueError:
            raise ValorInvalido(f"\"{rotulo}\": use um número inteiro.")
        if not p["min"] <= n <= p["max"]:
            raise ValorInvalido(f"\"{rotulo}\": use um número de {p['min']} a {p['max']:,}.".replace(",", "."))
        return n
    if tipo == "dia_mes":
        try:
            dia, mes = (int(x) for x in texto.split("/"))
            dt.date(2025, mes, dia)  # ano qualquer nao bissexto: 29/02 nao vale
        except ValueError:
            raise ValorInvalido(f"\"{rotulo}\": use dia/mês válidos (ex.: 1/11).")
        return [dia, mes]
    if tipo == "coord":
        if not texto:
            return None
        from .models import ler_coordenadas
        ponto = ler_coordenadas(texto)
        if not ponto:
            raise ValorInvalido(f"\"{rotulo}\": não reconhecemos a coordenada. Use -6.3947, -48.5592 ou cole o link do Google Maps.")
        return f"{ponto[0]:.6f}, {ponto[1]:.6f}"
    if tipo == "decimal":
        bruto = texto.replace(" ", "").replace("R$", "").replace("%", "")
        if "," in bruto:
            bruto = bruto.replace(".", "").replace(",", ".")
        try:
            n = round(float(bruto), 2)
        except ValueError:
            raise ValorInvalido(f"\"{rotulo}\": use um número (ex.: 70 ou 2,5).")
        if not p["min"] <= n <= p["max"]:
            raise ValorInvalido(f"\"{rotulo}\": use um número de {p['min']} a {p['max']:,}.".replace(",", "."))
        return n
    if tipo == "texto":
        if not 2 <= len(texto) <= 40:
            raise ValorInvalido(f"\"{rotulo}\": o nome precisa ter de 2 a 40 letras.")
        return texto
    if tipo == "ordem":
        chaves = [x.strip() for x in texto.split(",") if x.strip()]
        if sorted(chaves) != sorted(p["padrao"]):
            raise ValorInvalido(f"\"{rotulo}\": a ordem precisa ter cada nível uma vez só.")
        return chaves
    if tipo == "lista_int":
        try:
            nums = [int(x) for x in texto.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise ValorInvalido(f"\"{rotulo}\": separe os números por vírgula (ex.: 50, 100, 150).")
        if not 1 <= len(nums) <= 8 or any(not p["min"] <= n <= p["max"] for n in nums):
            raise ValorInvalido(f"\"{rotulo}\": de 1 a 8 números, cada um de {p['min']} a {p['max']}.")
        return sorted(set(nums))
    raise ValorInvalido(rotulo)


def validar_conjunto(valores):
    """Regras entre campos (com os valores que ficariam valendo)."""
    def v(chave):
        return valores[chave] if chave in valores else valor(chave)
    if v("categoria_a_ha") <= v("categoria_b_ha"):
        raise ValorInvalido("A categoria A precisa começar numa área maior que a da categoria B.")


def texto(chave, v):
    """Valor pra mostrar no historico."""
    tipo = PARAMETROS[chave]["tipo"]
    if tipo == "liga":
        return "ligada" if v else "desligada"
    if tipo == "dia_mes":
        return f"{v[0]}/{v[1]}"
    if tipo == "coord":
        return v or "centro da cidade"
    if tipo in ("lista_int", "ordem"):
        return ", ".join(str(x) for x in v)
    if tipo == "texto":
        return v
    if tipo == "decimal":
        return decimal_br(v)
    return f"{v:,}".replace(",", ".") if isinstance(v, int) else str(v)


def decimal_br(v):
    """70.0 -> "70"; 2.5 -> "2,5"; 69.99 -> "69,99"."""
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def salvar(db, quem, novos):
    """Grava o que mudou (so as chaves cujo valor difere do atual), com
    historico. Valor igual ao padrao apaga a linha (volta a seguir o padrao).
    -> lista de (chave, antes, depois) que mudaram."""
    from .models import ConfigHistorico, ConfigRegra
    validar_conjunto(novos)
    mudancas = []
    for chave, novo in novos.items():
        atual = valor(chave)
        if novo == atual:
            continue
        linha = db.get(ConfigRegra, chave)
        if novo == padrao(chave):
            if linha:
                db.delete(linha)
        else:
            if linha is None:
                linha = ConfigRegra(chave=chave, valor="")
                db.add(linha)
            linha.valor = json.dumps(novo)
            linha.alterado_em = dt.datetime.utcnow()
            linha.alterado_por = quem
        db.add(ConfigHistorico(chave=chave, antes=json.dumps(atual), depois=json.dumps(novo), quem=quem))
        mudancas.append((chave, atual, novo))
    db.commit()
    limpar_cache()
    return mudancas
