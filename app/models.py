import datetime as dt

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .database import Base

ROLES = ("admin", "vendedor", "logistica")

# Estados onde a empresa atua hoje (usado nos filtros de Pedidos e no mapa do CRM)
ESTADOS_OPERACAO = ("MA", "PA", "TO", "PI", "MT", "GO")
PRODUTOS = ("Calcario", "Gesso")

# Sentinela usada na importacao do CRM (app/crm_import_real.py) pra data original
# ilegivel/ausente na planilha. Nunca "agora" -- contaminaria tanto o log de
# atividades (pareceria feito hoje) quanto a sazonalidade de compra (empurraria
# tudo pra Janeiro, que e o mes da sentinela).
DATA_DESCONHECIDA = dt.datetime(2020, 1, 1)

MESES_PT = ["Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho", "Julho",
            "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
MESES_PT_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    nome_completo = Column(String, nullable=False)
    role = Column(String, nullable=False)
    # For role == "vendedor": must match Pedido.vendedor exactly, to scope their carteira.
    vendedor_nome = Column(String, nullable=True, index=True)
    ativo = Column(Boolean, default=True, nullable=False)


class Pedido(Base):
    __tablename__ = "pedidos"

    id = Column(Integer, primary_key=True)
    subsidiaria = Column(String, nullable=True)
    numero_pedido = Column(String, unique=True, nullable=False, index=True)
    data_pedido = Column(Date, nullable=True)
    cliente = Column(String, nullable=False, index=True)
    status = Column(String, nullable=True)
    quant_total = Column(Float, default=0.0)
    faturado = Column(Float, default=0.0)
    saldo = Column(Float, default=0.0)
    valor_unitario = Column(Float, default=0.0)
    vendedor = Column(String, nullable=True, index=True)

    # Preenchidos futuramente pela integracao com o NetSuite (ainda nao
    # vem no export da saved search "Pedidos" que importamos hoje).
    uf = Column(String, nullable=True, index=True)
    produto = Column(String, nullable=True, index=True)

    data_limite_retirada = Column(Date, nullable=True)

    # Quando o cliente para de retirar (sem avisar ninguem), precisamos
    # perceber isso. So da pra saber comparando retiradas ao longo do tempo:
    # faturado_atualizado_em so muda quando o valor de "faturado" realmente
    # muda entre duas importacoes/sincronizacoes (ver import_data.py). Fica
    # None ate a primeira mudanca real ser observada -- antes disso nao
    # temos historico suficiente pra dizer que o pedido "parou".
    faturado_atualizado_em = Column(DateTime, nullable=True)

    # Resolucao manual de um pedido problematico (prazo vencido, parado etc).
    # Enquanto None, o pedido continua na lista de "em aberto".
    situacao_resolucao = Column(String, nullable=True)
    situacao_observacao = Column(String, nullable=True)
    situacao_definida_em = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    def dias_restantes(self):
        if not self.data_limite_retirada:
            return None
        return (self.data_limite_retirada - dt.date.today()).days

    def ton_dia_necessario(self):
        dias = self.dias_restantes()
        if dias is None or self.saldo <= 0:
            return None
        if dias <= 0:
            return None
        return round(self.saldo / dias, 2)

    def vencido(self):
        dias = self.dias_restantes()
        return dias is not None and dias <= 0 and self.saldo > 0

    def dias_parado(self):
        """Dias desde a ultima vez que o Faturado realmente mudou. None se
        ainda nao temos historico (so uma importacao feita ate agora)."""
        if not self.faturado_atualizado_em:
            return None
        return (dt.datetime.utcnow() - self.faturado_atualizado_em).days

    def possivel_pedido_parado(self, limite_dias=21):
        """Cliente comecou a retirar, parou, e ninguem percebeu ainda."""
        dias = self.dias_parado()
        return (dias is not None and dias >= limite_dias
                and self.faturado > 0 and self.saldo > 0)

    def prioridade_key(self):
        """Menor valor aparece primeiro. Ordena sempre pelo Ton/Dia
        Necessario (maior volume diario = mais atencao), do maior pro menor;
        pedidos sem prazo definido (portanto sem Ton/Dia calculavel) ficam
        depois, ordenados do mais antigo pro mais novo."""
        ton_dia = self.ton_dia_necessario()
        if ton_dia is not None:
            return (0, -ton_dia)
        data_ord = self.data_pedido or dt.date.today()
        return (1, data_ord.toordinal())


RESOLUCAO_LABEL = {
    "finalizado_parcial": "Finalizado (nao retirou tudo)",
    "renegociar": "Marcado para renegociacao",
    "aguardando_proximo_periodo": "Cliente com credito",
}


class MensagemPedido(Base):
    """Comunicacao interna entre logistica e vendedor sobre um pedido
    especifico (nao e WhatsApp de verdade, e um recado dentro do portal)."""
    __tablename__ = "pedido_mensagens"

    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey("pedidos.id"), nullable=False, index=True)
    autor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    autor_nome = Column(String, nullable=False)
    autor_role = Column(String, nullable=False)
    texto = Column(String, nullable=False)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)
    lida = Column(Boolean, default=False, nullable=False)

    pedido = relationship("Pedido", backref="mensagens")


FASES_CRM = ("a_contactar", "contactado", "proposta", "realizado", "nao_usara", "perdido")

FASE_LABEL = {
    "a_contactar": "Clientes a contactar",
    "contactado": "Contactados",
    "proposta": "Proposta",
    "realizado": "Realizado",
    "nao_usara": "Nao usara",
    "perdido": "Perdido",
}

FASE_COR = {
    "a_contactar": "clay",
    "contactado": "",
    "proposta": "blue",
    "realizado": "green",
    "nao_usara": "grey",
    "perdido": "red",
}

# Ordem natural do funil: um contato so avanca a fase pra frente nessa ordem,
# nunca volta. "nao_usara"/"perdido" sao desfechos, nao fazem parte da ordem.
ORDEM_FASE = ["a_contactar", "contactado", "proposta", "realizado"]
FASES_TERMINAIS = ("nao_usara", "perdido")

# Ao registrar um contato, o vendedor escolhe o RESULTADO (nao a fase em si).
# O sistema decide a fase daí — ninguem move cliente manualmente no funil.
RESULTADO_CONTATO = {
    "": "So uma observacao (sem mudar etapa)",
    "contato": "Fiz contato com o cliente",
    "proposta": "Enviei uma proposta",
    "venda": "Venda realizada",
    "sem_interesse": "Cliente sem interesse (nao usara)",
    "perdido": "Perdemos essa venda",
}

RESULTADO_PARA_FASE = {
    "contato": "contactado",
    "proposta": "proposta",
    "venda": "realizado",
    "sem_interesse": "nao_usara",
    "perdido": "perdido",
}


def calcular_avanco_fase(fase_atual, resultado):
    """Decide se/para onde a fase deve avancar dado o resultado de um
    contato. Retorna a nova fase, ou None se nao deve mudar."""
    alvo = RESULTADO_PARA_FASE.get(resultado)
    if not alvo:
        return None
    if alvo in FASES_TERMINAIS:
        return alvo if fase_atual != alvo else None
    idx_atual = ORDEM_FASE.index(fase_atual) if fase_atual in ORDEM_FASE else -1
    idx_alvo = ORDEM_FASE.index(alvo)
    return alvo if idx_alvo > idx_atual else None


class ClienteCRM(Base):
    """Cliente/fazenda no funil comercial (prospeccao -> venda), independente
    de ja ter pedidos no NetSuite ou nao. Importado de verdade da planilha
    "Controle" (via app/crm_import_real.py) a partir dos dados reais que
    estavam embutidos no mockup painel-clientes.html.
    """
    __tablename__ = "crm_clientes"

    id = Column(Integer, primary_key=True)
    id_origem = Column(Integer, nullable=True, index=True)
    fazenda = Column(String, nullable=False)
    proprietario = Column(String, nullable=True)
    empresa = Column(String, nullable=True)
    telefone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    cnpj = Column(String, nullable=True)
    cidade = Column(String, nullable=True)
    uf = Column(String, nullable=False, index=True)
    forma_pagamento = Column(String, nullable=True)
    frota_propria = Column(Boolean, nullable=True)
    area_plantada_ha = Column(Float, nullable=True)
    coordenadas = Column(String, nullable=True)
    fase = Column(String, nullable=False, default="a_contactar", index=True)
    vendedor_nome = Column(String, nullable=True, index=True)
    ultima_interacao_em = Column(DateTime, nullable=True)
    proximo_retorno_em = Column(Date, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)

    categoria_dado = Column(String, nullable=True)
    volume_contratado = Column(Float, nullable=True)
    volume_retirado = Column(Float, nullable=True)
    proposta_valor = Column(Float, nullable=True)
    proposta_em = Column(DateTime, nullable=True)
    temperatura = Column(String, nullable=True)
    precisa_ajuda = Column(Boolean, default=False, nullable=False)
    motivo_ajuda = Column(String, nullable=True)

    def categoria(self):
        """Categoria vinda da planilha Controle quando existe; senao, uma
        estimativa provisoria a partir da area plantada."""
        if self.categoria_dado:
            return self.categoria_dado
        if not self.area_plantada_ha:
            return None
        if self.area_plantada_ha >= 3000:
            return "A"
        if self.area_plantada_ha >= 800:
            return "B"
        return "C"

    def dias_desde_ultima_interacao(self):
        if not self.ultima_interacao_em:
            return None
        return (dt.datetime.utcnow() - self.ultima_interacao_em).days

    def meses_compra_historico(self):
        """Meses (1-12) em que esse cliente ja comprou antes, segundo o
        historico de contatos. Ignora a sentinela DATA_DESCONHECIDA -- data
        original ilegivel na planilha nao e um mes de compra de verdade."""
        return sorted({
            c.data.month for c in self.historico
            if c.tipo == "compra" and c.data and c.data != DATA_DESCONHECIDA
        })

    def meses_compra_texto(self):
        meses = self.meses_compra_historico()
        return ", ".join(MESES_PT_ABREV[m - 1] for m in meses) if meses else None

    def prioridade_sazonal(self, mes_referencia=None):
        """Quantos meses faltam ate o proximo mes em que esse cliente
        historicamente compra (0 = compra normalmente neste mes, 11 = acabou
        de passar a epoca dele). None quando nao ha historico de compra
        confiavel -- nesse caso nao da pra saber se vale a pena priorizar."""
        mes_referencia = mes_referencia or dt.datetime.utcnow().month
        meses = self.meses_compra_historico()
        if not meses:
            return None
        return min((m - mes_referencia) % 12 for m in meses)


class ContatoCRM(Base):
    """Historico de contato/mudanca de fase de um ClienteCRM."""
    __tablename__ = "crm_contatos"

    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    data = Column(DateTime, default=dt.datetime.utcnow)
    tipo = Column(String, default="nota")  # nota | mudanca_fase | compra | retirada | proposta | pedido
    texto = Column(String, nullable=False)
    # Preenchido so quando tipo == "mudanca_fase": pra qual fase o cliente foi.
    # Usado no relatorio de desempenho por vendedor (contar sem parsear texto).
    fase_destino = Column(String, nullable=True, index=True)

    # Preenchidos so quando tipo in (proposta, pedido): snapshot congelado dos
    # valores NAQUELE momento, pra renderizar um registro estruturado no
    # historico sem depender da PropostaCRM/PedidoCRM (que pode ter mudado
    # desde entao -- editar ou renegociar depois nao pode reescrever o passado).
    numero = Column(Integer, nullable=True)
    produto = Column(String, nullable=True)
    volume = Column(Float, nullable=True)
    preco = Column(Float, nullable=True)
    pagamento = Column(String, nullable=True)

    cliente = relationship("ClienteCRM", backref="historico")


class ContatoAdicionalCRM(Base):
    """Telefone extra de um ClienteCRM, com o nome e a funcao de quem atende
    nele (ex: o celular que a gente fala e do dono, mas pra fechar o pedido a
    conversa e com alguem de suprimentos -- precisa ficar claro quem e quem)."""
    __tablename__ = "crm_contatos_adicionais"

    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    nome = Column(String, nullable=False)
    funcao = Column(String, nullable=False)
    telefone = Column(String, nullable=False)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)

    cliente = relationship("ClienteCRM", backref="contatos_adicionais")


# Subsidiarias que faturam cada produto, com a regiao que cada uma atende
# (vem do painel-vendedor.html de referencia -- calcario e gesso tem CNPJs e
# tributacao proprios, por isso nunca podem ir no mesmo pedido).
SUBSIDIARIAS = {
    "Calcario": [
        {"nome": "Agrocal Calcarios do Para LTDA", "cnpj": "12.345.671/0001-00", "regiao": "PA"},
        {"nome": "Agrocal Calcarios do Maranhao LTDA", "cnpj": "12.345.672/0001-81", "regiao": "MA"},
        {"nome": "Agrocal Calcarios Centro-Oeste LTDA", "cnpj": "12.345.673/0001-62", "regiao": "MT / GO"},
        {"nome": "Agrocal Calcarios Tocantins-Piaui LTDA", "cnpj": "12.345.674/0001-43", "regiao": "TO / PI"},
    ],
    "Gesso": [
        {"nome": "Agrogesso do Araripe LTDA", "cnpj": "98.765.431/0001-50", "regiao": "PI"},
        {"nome": "Agrogesso Centro-Oeste LTDA", "cnpj": "98.765.432/0001-31", "regiao": "MT / GO"},
        {"nome": "Agrogesso Norte-Nordeste LTDA", "cnpj": "98.765.433/0001-12", "regiao": "PA / MA / TO"},
    ],
}

STATUS_PROPOSTA_ABERTA = "aberta"
STATUS_PROPOSTA_CONVERTIDA = "convertida"

STATUS_PEDIDO_ABERTO = "aberto"
STATUS_PEDIDO_CANCELADO = "cancelado"


class PropostaCRM(Base):
    """Proposta comercial feita a um cliente, para UM produto. Regra de
    negocio (do painel-vendedor.html de referencia): nao pode existir mais de
    uma proposta 'aberta' do mesmo produto pro mesmo cliente ao mesmo tempo --
    tem que editar a existente ou gerar o pedido dela antes de propor de novo."""
    __tablename__ = "crm_propostas"

    id = Column(Integer, primary_key=True)
    numero = Column(Integer, unique=True, index=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    produto = Column(String, nullable=False)
    volume = Column(Float, nullable=False)
    preco = Column(Float, nullable=False)
    pagamento = Column(String, nullable=False)
    prazo = Column(String, nullable=True)
    observacoes = Column(String, nullable=True)
    # aberta: ainda pode virar pedido ou ser editada. convertida: ja virou pedido.
    status = Column(String, nullable=False, default=STATUS_PROPOSTA_ABERTA)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)
    atualizado_em = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    cliente = relationship("ClienteCRM", backref="propostas")

    def valor_total(self):
        return (self.volume or 0) * (self.preco or 0)


class PedidoCRM(Base):
    """Pedido gerado a partir de uma PropostaCRM confirmada. Independente do
    Pedido (app.models.Pedido) importado do NetSuite pra Logistica -- esse
    aqui e o pedido nascendo no CRM, antes de existir integracao real."""
    __tablename__ = "crm_pedidos"

    id = Column(Integer, primary_key=True)
    numero = Column(Integer, unique=True, index=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    proposta_id = Column(Integer, ForeignKey("crm_propostas.id"), nullable=True)
    produto = Column(String, nullable=False)
    volume = Column(Float, nullable=False)
    # Preenchido quando existir integracao com o NetSuite -- de la vem o quanto
    # o cliente ja retirou de fato. Nunca e digitado pelo vendedor.
    volume_retirado = Column(Float, nullable=True)
    preco = Column(Float, nullable=False)
    pagamento = Column(String, nullable=False)
    prazo = Column(String, nullable=True)
    observacoes = Column(String, nullable=True)
    subsidiaria_nome = Column(String, nullable=True)
    subsidiaria_cnpj = Column(String, nullable=True)
    faturamento_tipo = Column(String, nullable=True)  # cliente | terceiro
    faturamento_nome = Column(String, nullable=True)
    faturamento_documento = Column(String, nullable=True)
    faturamento_telefone = Column(String, nullable=True)
    faturamento_endereco = Column(String, nullable=True)
    status = Column(String, nullable=False, default=STATUS_PEDIDO_ABERTO)
    motivo_cancelamento = Column(String, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)

    cliente = relationship("ClienteCRM", backref="pedidos_crm")
    proposta = relationship("PropostaCRM", backref="pedido")

    def valor_total(self):
        return (self.volume or 0) * (self.preco or 0)


class AreaEstado(Base):
    """Area agropecuaria de referencia por UF, usada para calcular % de
    mercado capturado no mapa do CRM. Marcar placeholder=True enquanto o
    numero nao vier de uma fonte oficial (IBGE/SIDRA) confirmada."""
    __tablename__ = "crm_area_estados"

    uf = Column(String, primary_key=True)
    area_agropecuaria_ha = Column(Float, nullable=False)
    fonte = Column(String, nullable=True)
    placeholder = Column(Boolean, default=True, nullable=False)
