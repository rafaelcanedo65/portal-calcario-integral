import datetime as dt
import re

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import backref, relationship

from .database import Base

# financeiro (2026-10-02): confere o comprovante do pedido a vista e libera o carregamento.
ROLES = ("admin", "vendedor", "logistica", "financeiro", "portaria", "balcao")
# Setores do portal (Rafael, 2026-10-05): cada um ve a sua area. Admin ve e faz tudo (inclusive pessoas e regras);
# Balcao de vendas ve tudo e opera vendas, mas nao cria login nem altera regras (so consulta Logistica, Financeiro,
# Regras e Log); Portaria so consulta se pode carregar. Regras de acesso em auth.require_role.
SETOR_ROTULO = {"admin": "Administrador", "balcao": "Balcão de vendas", "vendedor": "Vendedor", "logistica": "Logística",
                "financeiro": "Financeiro", "portaria": "Portaria"}
SETOR_DESCRICAO = {
    "admin": "Vê e faz tudo: cadastra pessoas e muda regras.",
    "balcao": "Vê todo o portal e faz vendas em qualquer carteira. Não cria login nem muda regras.",
    "vendedor": "Só a área de Vendas, com a carteira dele.",
    "logistica": "Só a área de Logística.",
    "financeiro": "Só a área do Financeiro (pagamentos e recebimentos).",
    "portaria": "Só a consulta \"pode carregar?\" dos pedidos.",
}

# Estados onde a empresa atua hoje (usado nos filtros de Pedidos e no mapa do CRM)
ESTADOS_OPERACAO = ("MA", "PA", "TO", "PI", "MT", "GO")
# Lista de produtos vendidos pela empresa (Rafael, 2026-09-23) -- "Calcario" e
# "Gesso" aqui sao os mesmos de sempre (calcario/gesso AGRICOLA, so encurtado
# no valor gravado no banco pra nao mexer em dado historico ja existente).
# Extensivel: adicionar mais aqui se surgir outro produto no catalogo.
PRODUTOS = ("Calcario", "Gesso", "Sulfato", "Pedra Britada")


def categoria_produto(nome):
    """Nome do produto no NetSuite/planilha de Expedicao ("CALCÁRIO",
    "GESSO AGRICOLA - BAG", "GIPSITA MARROADA", "SULFATO DE CÁLCIO BAG"...)
    -> a categoria de PRODUTOS usada nos filtros. Gipsita e o gesso natural.
    None se nao reconhecer (nunca chutar)."""
    t = (nome or "").upper()
    if "SULFATO" in t:
        return "Sulfato"
    if "CALC" in t:
        return "Calcario"
    if "GESSO" in t or "GIPSITA" in t:
        return "Gesso"
    return None


# Planilha "Expedição — o que falta retirar" (Rafael, 2026-10-03): as abas que
# a equipe da expedicao trabalha como "em aberto". "Saldo abaixo de 5%" e
# "Finalizados" ficam guardados (historico do cliente), mas fora da lista.
ABAS_EM_ABERTO = ("Expedição", "Parado - retirou outro pedido", "Outros produtos")
ABA_FORA_DA_PLANILHA = "Fora da planilha"
ROTULO_ABA = {"Expedição": "Em aberto", "Outros produtos": "Em aberto",
              "Parado - retirou outro pedido": "Parado (retirou outro pedido)",
              "Saldo abaixo de 5%": "Quase finalizado (saldo abaixo de 5%)", "Finalizados": "Finalizado",
              ABA_FORA_DA_PLANILHA: "Fora da planilha"}

# Situacao do pedido na Logistica (Rafael, 2026-10-03: "a equipe vai anotar no
# portal"). UMA lista so: a que a expedicao ja usava na planilha, mais "Cliente
# com credito" (do antigo botao Resolver, que deixou de existir).
SITUACOES_LOGISTICA = {
    "em_andamento": "Em andamento",
    "sem_retorno": "Sem retorno",
    "cobrar_vendedor": "Cobrar retorno do vendedor",
    "possivel_desistencia": "Possível desistência",
    "desistencia": "Desistência do pedido",
    "finalizado": "Finalizado",
    "cliente_com_credito": "Cliente com crédito",
}
# Tiram o pedido da lista em aberto (o NetSuite ainda pode estar com saldo)
SITUACOES_ENCERRAM = ("desistencia", "finalizado", "cliente_com_credito")
# Como a planilha escrevia -> chave
SITUACAO_DA_PLANILHA = {"EM ANDAMENTO": "em_andamento", "SEM RETORNO": "sem_retorno",
                        "COBRAR RETORNO DO VENDEDOR": "cobrar_vendedor", "POSSÍVEL DESISTÊNCIA": "possivel_desistencia",
                        "DESISTÊNCIA DO PEDIDO": "desistencia", "FINALIZADO": "finalizado"}
# Antigo "Resolver" (situacao_resolucao) -> situacao da lista unica, pra migrar dado antigo
RESOLUCAO_PARA_SITUACAO = {"finalizado_parcial": "finalizado", "renegociar": "cobrar_vendedor",
                           "aguardando_proximo_periodo": "cliente_com_credito"}
CAMPOS_ANOTACAO = {"situacao": "Situação", "data_limite": "Data limite", "comentario": "Comentário"}
# So rotulo no historico (nao e campo do formulario de anotacao)
ROTULO_HISTORICO = dict(CAMPOS_ANOTACAO, local="Local de entrega")
# Pedido.local_fonte: de onde veio o local de entrega e se e o ponto exato ou
# so o centro da cidade (o raio em km e a rota dependem disso)
LOCAL_FONTE_ROTULO = {
    "fazenda": "local exato da fazenda (cadastro do CRM)",
    "cidade": "centro da cidade (cadastro do CRM)",
    "destino final": "local exato do destino final",
    "destino final cidade": "centro da cidade do destino final",
    "logistica": "local exato informado pela Logística",
    "logistica cidade": "centro da cidade informada pela Logística",
}
LOCAL_EXATO = ("fazenda", "destino final", "logistica")
LOCAL_DA_LOGISTICA = ("logistica", "logistica cidade")  # a importacao da planilha nao troca

# Sentinela usada na importacao do CRM (app/crm_import_real.py) pra data original
# ilegivel/ausente na planilha. Nunca "agora" -- contaminaria tanto o log de
# atividades (pareceria feito hoje) quanto a sazonalidade de compra (empurraria
# tudo pra Janeiro, que e o mes da sentinela).
DATA_DESCONHECIDA = dt.datetime(2020, 1, 1)

MESES_PT = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho",
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
    # Presenca (Rafael, 2026-10-04: bolinha verde/cinza na equipe; presenca.py)
    ultimo_acesso = Column(DateTime, nullable=True)
    saiu_em = Column(DateTime, nullable=True)
    # Acessos (Rafael, 2026-10-05; acessos.py): e-mail pro "Esqueci minha senha"; senha temporaria = o portal obriga
    # a pessoa a criar a propria senha no proximo acesso
    email = Column(String, nullable=True)
    senha_temporaria = Column(Boolean, default=False, nullable=False)


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

    # Vem da planilha de Expedicao (endereco de entrega e nome do produto no
    # NetSuite); vazios nos pedidos importados antes dela.
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

    # LEGADO: o antigo botao "Resolver". Substituido por `situacao_logistica`
    # (lista unica); valores antigos migram por RESOLUCAO_PARA_SITUACAO.
    situacao_resolucao = Column(String, nullable=True)
    situacao_observacao = Column(String, nullable=True)
    situacao_definida_em = Column(DateTime, nullable=True)

    # Planilha "Expedição — o que falta retirar" (Google Drive, gerada do
    # NetSuite pela equipe da expedicao -- app/import_expedicao.py). Ate a
    # integracao direta e a fonte da Logistica: em que aba o pedido esta, a
    # data do ultimo carregamento e o que a equipe anotou nele.
    aba_planilha = Column(String, nullable=True)
    ultima_retirada = Column(Date, nullable=True)
    # Telefone do cliente no NetSuite (Rafael, 2026-10-03): a Logistica precisa
    # ligar pra combinar a data limite, e muitos clientes do NetSuite nao estao
    # ligados a um cadastro do CRM.
    telefone = Column(String, nullable=True)
    # Cidade da entrega (Rafael, 2026-10-03: frete retorno -- saber onde tem
    # pedido). A planilha so traz a UF; ate o NetSuite mandar a cidade de
    # entrega, vem do cadastro do cliente no CRM (cidade_fonte = "CRM"), e so
    # quando a UF do cadastro bate com a UF de entrega. Sem isso fica vazia.
    cidade = Column(String, nullable=True)
    cidade_fonte = Column(String, nullable=True)
    # Ponto de entrega no mapa (Rafael, 2026-10-03): a coordenada da fazenda
    # quando existe no cadastro (exata); senao o centro da cidade. Venda pra
    # transportadora usa o DESTINO FINAL (quem usa o produto), nunca o endereco
    # da transportadora. local_fonte diz de onde veio (LOCAL_FONTE_ROTULO) ou
    # "transportadora sem destino" (sem local ate alguem informar).
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    local_fonte = Column(String, nullable=True)

    @property
    def local_exato(self):
        return self.local_fonte in LOCAL_EXATO and self.latitude is not None

    # O que a equipe da Logistica anotou (chave de SITUACOES_LOGISTICA). Vinha da
    # planilha; desde 2026-10-03 a equipe anota no portal. `anotado_no_portal_em`
    # preenchido = o portal manda: a importacao da planilha nao mexe mais em
    # situacao, comentario nem data limite deste pedido.
    situacao_logistica = Column(String, nullable=True)
    comentario_logistica = Column(String, nullable=True)
    anotado_no_portal_em = Column(DateTime, nullable=True)

    # Colunas antigas que continuam nos bancos ja criados (carregado e NOT
    # NULL): sem uso, mas precisam estar aqui senao nenhum pedido novo entra.
    carregado = Column(Boolean, default=False, nullable=False)
    observacao_logistica = Column(String, nullable=True)

    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    @property
    def categoria(self):
        return categoria_produto(self.produto)

    def em_aberto(self):
        """Na lista da Logistica: tem saldo, esta numa aba "em aberto" da
        planilha (ou veio de antes dela) e a equipe nao encerrou."""
        return ((self.saldo or 0) > 0 and (self.aba_planilha is None or self.aba_planilha in ABAS_EM_ABERTO)
                and self.situacao_logistica not in SITUACOES_ENCERRAM)

    def precisa_prazo(self):
        """Fila da Logistica: em aberto e sem data limite de retirada."""
        return self.em_aberto() and not self.data_limite_retirada

    @property
    def rotulo_aba(self):
        return ROTULO_ABA.get(self.aba_planilha, "—")

    @property
    def situacao_rotulo(self):
        return SITUACOES_LOGISTICA.get(self.situacao_logistica)

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
        """Dias sem retirar. Com a planilha de Expedicao e a data do ultimo
        carregamento; sem ela, desde a ultima vez que o Faturado mudou entre
        duas importacoes. None se ainda nao ha como saber."""
        if self.ultima_retirada:
            return (dt.date.today() - self.ultima_retirada).days
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


class PedidoAnotacao(Base):
    """Historico do que a Logistica anotou no pedido (quem, quando, o que
    mudou). Origem "portal" ou "planilha" (historico trazido da aba "Histórico
    anotações"). Na planilha, quando so o "antes" ou so o "depois" estava
    preenchido, o texto exportado nao diz qual dos dois e -- entao vem em
    `registro` (o valor anotado), sem inventar antes/depois."""
    __tablename__ = "pedido_anotacoes"

    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey("pedidos.id"), nullable=False, index=True)
    quando = Column(DateTime, nullable=False)
    quem = Column(String, nullable=True)
    origem = Column(String, nullable=False, default="portal")
    campo = Column(String, nullable=False)  # situacao | data_limite | comentario
    antes = Column(String, nullable=True)
    depois = Column(String, nullable=True)
    registro = Column(String, nullable=True)

    pedido = relationship("Pedido", backref=backref("anotacoes", order_by="PedidoAnotacao.quando.desc()"))


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
    "nao_usara": "Não usará",
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

# Ciclo de vendas (safra): regra do Rafael, 2026-09-24 -- todo 1o de novembro
# a carteira comercial reinicia (fases que nao seja "Proposta" voltam pra "a
# contactar"). So funcoes puras de data aqui; a execucao do reset em si (que
# precisa de DB) mora em crm_routes.py junto de CicloVendas.
def _virada_do_ano(ano):
    """Dia da virada (1o de novembro por padrao) -- o admin muda na pagina
    Regras (config "ciclo_virada")."""
    from . import config  # tardio: config importa models
    dia, mes = config.valor("ciclo_virada")
    return dt.date(ano, mes, dia)


def ciclo_rotulo(hoje=None):
    """Rotulo do ciclo de vendas vigente numa data ("2026/2027") -- vira todo
    1o de novembro. Ex: 2026-09-24 -> "2025/2026"; 2026-11-02 -> "2026/2027"."""
    hoje = hoje or dt.date.today()
    if hoje >= _virada_do_ano(hoje.year):
        return f"{hoje.year}/{hoje.year + 1}"
    return f"{hoje.year - 1}/{hoje.year}"


def inicio_ciclo(hoje=None):
    """Data em que comecou o ciclo de vendas vigente (o ultimo 1o de novembro)."""
    hoje = hoje or dt.date.today()
    virada = _virada_do_ano(hoje.year)
    return virada if hoje >= virada else _virada_do_ano(hoje.year - 1)


def dias_para_virada_ciclo(hoje=None):
    """Quantos dias faltam pra proxima virada de ciclo (1o de novembro)."""
    hoje = hoje or dt.date.today()
    proxima = _virada_do_ano(hoje.year)
    if hoje >= proxima:
        proxima = _virada_do_ano(hoje.year + 1)
    return (proxima - hoje).days

# Ao registrar um contato, o vendedor escolhe o RESULTADO (nao a fase em si).
# O sistema decide a fase daí — ninguem move cliente manualmente no funil.
RESULTADO_CONTATO = {
    "": "Só uma observação (sem mudar etapa)",
    "contato": "Conversamos com o cliente",
    # Desfechos de uma tentativa de contato (Rafael, 2026-10-02): todos contam
    # como trabalho feito com o cliente (levam pra Contactados), mas ficam
    # registrados separados pra saber o que de fato aconteceu.
    "nao_atendeu": "Não atendeu",
    "retornar_depois": "Pediu pra ligar depois",
    "sem_interesse_agora": "Sem interesse agora",
    # Numero errado, sem WhatsApp etc. (Rafael, 2026-10-02): nao e contato e
    # nao muda a etapa -- vai pro administrador conseguir outro contato.
    "contato_invalido": "Contato não funciona",
    "proposta": "Enviei uma proposta",
    "venda": "Venda realizada",
    "sem_interesse": "Cliente sem interesse (não usará)",
    "perdido": "Perdemos essa venda",
}

# Cor do badge/card de cada resultado no Historico -- mesma paleta ja usada
# em fase_cor/log-badge, so pra manter o significado de cor consistente
# (venda=verde como em fase realizado, perdido=vermelho como em fase
# perdido, sem_interesse=cinza como em nao_usara).
RESULTADO_COR = {
    "contato": "mineral",
    "nao_atendeu": "grey",
    "retornar_depois": "mineral",
    "sem_interesse_agora": "gold",
    "contato_invalido": "red",
    "proposta": "blue",
    "venda": "green",
    "sem_interesse": "grey",
    "perdido": "red",
}

RESULTADO_PARA_FASE = {
    "contato": "contactado",
    "nao_atendeu": "contactado",
    "retornar_depois": "contactado",
    "sem_interesse_agora": "contactado",
    "proposta": "proposta",
    "venda": "realizado",
    "sem_interesse": "nao_usara",
    "perdido": "perdido",
}

# O que aconteceu com o contato que nao funciona -- os mesmos motivos que
# ja vinham da planilha ("precisaAjudaContato"/"motivoAjudaContato").
MOTIVOS_CONTATO_INVALIDO = ["Número errado", "Número desativado", "Não recebe ligações", "Sem WhatsApp",
                            "Atende outra pessoa"]
# Tentativas seguidas sem resposta que mandam o caso pro administrador: config
# "contato_tentativas" (3 por padrao), editavel na pagina Regras.

# Motivo estruturado ao marcar "perdido" -- pedido do Theo (consultoria de
# design, 2026-09-24): sem categoria fixa, "perdido" vira so um texto livre
# que ninguem consegue agregar depois (quantos perdemos por preco? por
# concorrencia?). A data de reavaliacao reaproveita o campo generico
# `proximo_retorno_em` que ja existe (mesmo que alimenta a Agenda) -- nao
# precisa de coluna nova pra isso.
MOTIVO_PERDIDO = {
    "preco": "Preço ou condição de pagamento",
    "concorrencia": "Fechou com concorrente",
    "sem_orcamento": "Sem orçamento/verba no momento",
    "sem_resposta": "Parou de responder",
    "fora_perfil": "Fora do perfil (não usa esse produto)",
    "outro": "Outro motivo",
}

# Temperatura do lead (quente/morno/frio) -- ja existia como coluna
# (`ClienteCRM.temperatura`) importada da planilha original, mas nunca foi
# usada em NENHUMA tela desde a importacao (569 clientes tem valor, mas era
# so uma foto congelada de setembro). Reativado 2026-09-24: Rafael queria uma
# forma de saber quais propostas estao "quentes" pra priorizar antes da
# virada do ciclo de vendas -- em vez de tentar inferir isso com IA lendo o
# texto livre do comentario (nao confiavel, sem infraestrutura de LLM nesse
# projeto), o vendedor classifica ele mesmo ao registrar contato, reusando o
# proprio vocabulario que a empresa ja tinha na planilha.
TEMPERATURA_LABEL = {
    "quente": "Quente",
    "morno": "Morno",
    "frio": "Frio",
}
TEMPERATURA_ORDEM = {"quente": 0, "morno": 1, "frio": 2}

# Forma de pagamento do CLIENTE (`ClienteCRM.forma_pagamento`) -- ja existia
# como coluna importada da planilha original (2026-09-25: achado real, 239
# clientes "A vista", 185 "A prazo", 7 "Plano safra"), mas so era exibida,
# nunca editavel nem atualizada desde a importacao (mesmo padrao morto de
# `temperatura` achado antes). Reativado a pedido do Rafael: o vendedor pode
# registrar que o cliente PEDIU plano safra (classificacao/demanda, pode
# nunca virar negocio de verdade), e quando um PedidoCRM real e gerado, isso
# e sobrescrito automaticamente com a forma efetivamente praticada.
FORMA_PAGAMENTO_OPCOES = ["A vista", "A prazo", "Plano safra"]

# "Plano safra" de verdade (em PropostaCRM/PedidoCRM.pagamento, nao so na
# classificacao do cliente acima) tem 2 modalidades -- explicado pelo Rafael
# (2026-09-25):
# - "direto": cliente bem avaliado pela empresa, financiamento pos-colheita
#   direto, sem cessao de credito com terceiro.
# - "cessao": cliente menos avaliado -- a empresa "vende" pro cliente mas
#   RECEBE de um parceiro (ex: Juparana, Gees, Agrex -- tradings/fomentadoras
#   de agricultores, clientes-parceiros de longa data), e o cliente final
#   paga o parceiro depois, normalmente em soja/milho na safra seguinte.
#   `plano_safra_parceiro` guarda o NOME do parceiro escolhido -- congelado
#   no momento (snapshot, mesmo principio de `numero`/`produto` no
#   ContatoCRM), mas so pode ser escolhido dentre os cadastrados em
#   `ParceiroCessao` (Rafael, 2026-09-25: "tem que estar cadastrado no nosso
#   banco de dados" -- nao aceita mais nome digitado na hora).
PLANO_SAFRA_MODALIDADE = {
    "direto": "Direto (sem cessão de crédito)",
    "cessao": "Cessão de crédito (via parceiro)",
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


def fase_e_avanco(fase_atual, fase_alvo):
    """True se mover fase_atual -> fase_alvo representa avanco de verdade na
    ordem linear (ou fase_atual e um desfecho fora da ordem, tipo perdido/
    nao_usara -- nesse caso qualquer fase da ordem linear conta como
    reativacao, nao regressao). Usado pra decidir se uma proposta/pedido de
    OUTRO produto pode mudar a fase do cliente: nao pode puxar ele de volta
    (ex: Realizado -> Proposta so porque comecou a vender outro produto)."""
    if fase_atual not in ORDEM_FASE:
        return True
    return ORDEM_FASE.index(fase_alvo) > ORDEM_FASE.index(fase_atual)


def formatar_telefone(bruto):
    """Telefone do NetSuite ("+5594991022125") -> "(94) 99102-2125". Se nao
    tiver o formato brasileiro (DDD + 8 ou 9 digitos), devolve como veio --
    nunca "corrige" um numero que pode estar errado."""
    digitos = re.sub(r"\D", "", bruto or "")
    if not digitos:
        return None
    if (bruto or "").strip().startswith("+"):
        # Com "+", vem o codigo do pais: 55 e Brasil; estrangeiro fica como veio
        if not digitos.startswith("55"):
            return bruto.strip()
        digitos = digitos[2:]
    elif digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]
    if len(digitos) == 11:
        return f"({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}"
    if len(digitos) == 10:
        return f"({digitos[:2]}) {digitos[2:6]}-{digitos[6:]}"
    if len(digitos) in (8, 9):
        # Comum no NetSuite: so o numero, sem o DDD -- mostra e avisa, nao inventa DDD
        return f"{digitos[:-4]}-{digitos[-4:]} (sem DDD)"
    return (bruto or "").strip()


_LIMITES_BRASIL = (-34.0, 6.0, -74.5, -28.0)  # lat min, lat max, lon min, lon max


def _no_brasil(lat, lon):
    return _LIMITES_BRASIL[0] <= lat <= _LIMITES_BRASIL[1] and _LIMITES_BRASIL[2] <= lon <= _LIMITES_BRASIL[3]


def _dms(graus, minutos, segundos):
    return graus + minutos / 60 + segundos / 3600


def _compacto(digitos, maior_grau):
    """Grau/minuto/segundo grudados ("030612" ou "824367"): tenta as divisoes
    possiveis (grau com 1 a 3 digitos, minuto com 2, o resto e segundo -- com
    decimal se sobrar mais de 2 digitos) e devolve as que fazem sentido."""
    for n in (2, 3, 1) if maior_grau > 9 else (2, 1):
        g, m, s = digitos[:n], digitos[n:n + 2], digitos[n + 2:]
        if len(m) != 2 or not s:
            continue
        seg = float(s[:2] + "." + s[2:]) if len(s) > 2 else float(s)
        if int(g) <= maior_grau and int(m) < 60 and seg < 60:
            yield _dms(int(g), int(m), seg)


def ler_coordenadas(texto):
    """Coordenada da fazenda como o vendedor digitou -> (lat, lon) em graus
    decimais, ou None se nao der pra entender ou cair fora do Brasil.
    Aceita: "-4.5342, -46.8854" (o botao do GPS grava assim), link do Google
    Maps, 4°32'03.2"S 46°53'07.3"W e os compactos que vieram da planilha
    ("030612S 0471616W" e "824367S4640155W")."""
    t = (texto or "").strip()
    if not t:
        return None
    # Link do Google Maps: o alfinete (!3d..!4d..) vale mais que o centro da tela (@)
    m = re.search(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)", t)
    if not m:
        m = re.search(r"(?:[@=]|/place/|/search/)(-?\d{1,2}\.\d+),\s*\+?(-?\d{1,3}\.\d+)", t)
    if not m:
        m = re.fullmatch(r"(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)", t)
    if not m:
        m = re.fullmatch(r"(-?\d{1,2},\d+)\s*[;\s]\s*(-?\d{1,3},\d+)", t)
    if m:
        lat, lon = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        return (lat, lon) if _no_brasil(lat, lon) else None
    hem = {"N": 1, "S": -1, "E": 1, "L": 1, "W": -1, "O": -1}
    m = re.fullmatch(r"(\d{1,3})\s*[°º]\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)?\s*[\"”″]?\s*([NSns])\s*[,;]?\s*"
                     r"(\d{1,3})\s*[°º]\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)?\s*[\"”″]?\s*([EWOLewol])", t)
    if m:
        lat = hem[m.group(4).upper()] * _dms(int(m.group(1)), int(m.group(2)), float((m.group(3) or "0").replace(",", ".")))
        lon = hem[m.group(8).upper()] * _dms(int(m.group(5)), int(m.group(6)), float((m.group(7) or "0").replace(",", ".")))
        return (lat, lon) if _no_brasil(lat, lon) else None
    m = re.fullmatch(r"(\d{5,8})\s*([NSns])\s*[,;]?\s*(\d{5,9})\s*([EWOLewol])", t)
    if m:
        for la in _compacto(m.group(1), 34):
            for lo in _compacto(m.group(3), 74):
                lat, lon = hem[m.group(2).upper()] * la, hem[m.group(4).upper()] * lo
                if _no_brasil(lat, lon):
                    return lat, lon
    return None


def validar_telefone(telefone):
    """Telefone brasileiro: DDD + 8 digitos (fixo) ou DDD + 9 digitos
    (celular) -- 10 ou 11 digitos no total, ignorando qualquer formatacao
    (espaco, traco, parenteses). Retorna mensagem de erro, ou None se valido.
    Mora em models.py (nao em crm_routes.py, onde nasceu) porque
    `ClienteCRM.campos_faltando()` tambem precisa dela, pra fila do "balcao"
    (Theo, 2026-09-24) pegar nao so telefone AUSENTE mas tambem PREENCHIDO
    com formato invalido (numero cortado, sequencia de zeros, etc)."""
    digitos = re.sub(r"\D", "", telefone or "")
    if len(digitos) not in (10, 11):
        return "Telefone inválido — informe DDD + número (8 dígitos se fixo, 9 se celular)."
    if re.fullmatch(r"0+", digitos[2:]):
        return "Telefone inválido — o número não pode ser uma sequência de zeros."
    return None


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
    # Distrito/regiao rural dentro do municipio (ex: "Batavo", "Rio Coco",
    # dentro do municipio de Balsas-MA) -- texto livre, o vendedor usa pra
    # saber exatamente onde a fazenda fica dentro do municipio. Separado de
    # `cidade` de proposito: `cidade` precisa ser sempre um municipio oficial
    # do IBGE (valida contra MUNICIPIOS_POR_UF), e uma regiao rural nao e.
    localidade = Column(String, nullable=True)
    uf = Column(String, nullable=False, index=True)
    forma_pagamento = Column(String, nullable=True)
    frota_propria = Column(Boolean, nullable=True)
    # Rafael (2026-09-25): duas caixas no cadastro, nenhuma marcada = produtor.
    # Serve pra saber quem compra como intermediario (ver PedidoDestinoFinal).
    e_consultor = Column(Boolean, nullable=False, default=False)
    e_transportadora = Column(Boolean, nullable=False, default=False)
    area_plantada_ha = Column(Float, nullable=True)
    coordenadas = Column(String, nullable=True)
    fase = Column(String, nullable=False, default="a_contactar", index=True)
    vendedor_nome = Column(String, nullable=True, index=True)
    ultima_interacao_em = Column(DateTime, nullable=True)
    proximo_retorno_em = Column(Date, nullable=True)
    # So preenchido quando fase == "perdido" -- chave de MOTIVO_PERDIDO.
    # Limpo automaticamente se o cliente for reativado depois (ver
    # crm_add_nota) pra nao mostrar um motivo antigo desatualizado.
    motivo_perdido = Column(String, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)

    categoria_dado = Column(String, nullable=True)
    volume_contratado = Column(Float, nullable=True)
    volume_retirado = Column(Float, nullable=True)
    proposta_valor = Column(Float, nullable=True)
    proposta_em = Column(DateTime, nullable=True)
    temperatura = Column(String, nullable=True)
    precisa_ajuda = Column(Boolean, default=False, nullable=False)
    motivo_ajuda = Column(String, nullable=True)

    def campos_faltando(self):
        """Lista (rotulos legiveis) dos campos basicos que faltam preencher
        nesse cadastro -- so os que da pra cobrar de QUALQUER cliente real
        (nao inclui email/cnpj/empresa/frota propria, esses sao genuinamente
        opcionais). Usado tanto pro aviso agregado na home do vendedor quanto
        pro filtro/indicador na lista de clientes."""
        faltando = []
        if not self.cidade:
            faltando.append("cidade")
        if not self.proprietario:
            faltando.append("proprietario")
        if not self.telefone or self.telefone == "—":
            faltando.append("telefone")
        elif validar_telefone(self.telefone):
            faltando.append("telefone (formato inválido)")
        if not self.area_plantada_ha:
            faltando.append("área plantada")
        return faltando

    def cadastro_incompleto(self):
        return bool(self.campos_faltando())

    def categoria(self):
        """Categoria vinda da planilha Controle quando existe; senao, uma
        estimativa provisoria a partir da area plantada."""
        if self.categoria_dado:
            return self.categoria_dado
        if not self.area_plantada_ha:
            return None
        from . import config  # tardio: config importa models
        if self.area_plantada_ha >= config.valor("categoria_a_ha"):
            return "A"
        if self.area_plantada_ha >= config.valor("categoria_b_ha"):
            return "B"
        return "C"

    def dias_desde_ultima_interacao(self):
        if not self.ultima_interacao_em:
            return None
        return (dt.datetime.utcnow() - self.ultima_interacao_em).days

    def meses_compra_historico(self):
        """Meses (1-12) em que esse cliente ja comprou antes, segundo o
        historico de contatos. Ignora a sentinela DATA_DESCONHECIDA -- data
        original ilegivel na planilha nao e um mes de compra de verdade.
        Produto recebido via parceiro/transportadora (PedidoDestinoFinal) conta
        tambem, mas so enquanto o pedido do parceiro nao for cancelado."""
        meses = {
            c.data.month for c in self.historico
            if c.tipo == "compra" and c.data and c.data != DATA_DESCONHECIDA
        }
        # So consulta os destinos quando o historico (ja carregado) mostra que
        # existe algum -- evita 1 query extra por cliente nas listas.
        if any(c.tipo == "destino_final" for c in self.historico):
            meses |= {d.criado_em.month for d in self.recebimentos_indiretos
                      if d.pedido.status != STATUS_PEDIDO_CANCELADO}
        return sorted(meses)

    def tipo_cliente(self):
        tipos = [t for t, marcado in (("Consultor", self.e_consultor), ("Transportadora", self.e_transportadora)) if marcado]
        return " e ".join(tipos) if tipos else "Produtor"

    def e_intermediario(self):
        return bool(self.e_consultor or self.e_transportadora)

    def entrega_em_outro_lugar(self):
        """Venda que NAO e entregue no endereco do cliente: consultor ou
        transportadora -- o local de entrega e o destino final, quem usa o
        produto (Rafael, 2026-10-03). Poucas transportadoras estao marcadas no
        cadastro, entao o nome tambem conta."""
        return self.e_intermediario() or "TRANSPORT" in (self.fazenda or "").upper()

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
    # Preenchido so quando tipo == "nota" -- chave de RESULTADO_CONTATO (ex:
    # "contato", "venda", "perdido"). Rafael achou o texto bruto "[Fiz contato
    # com o cliente] ..." feio (2026-09-24) -- antes o resultado ficava
    # colado dentro do proprio `texto` entre colchetes; agora fica separado
    # aqui, pro template renderizar como badge colorido (mesmo espirito dos
    # cards de proposta/pedido) em vez de texto corrido. Registros antigos
    # (de antes dessa coluna existir, com o prefixo "[...]" ja gravado dentro
    # de `texto`) ficam None -- o template cai no fallback de texto simples
    # nesse caso, mesmo padrao ja usado por `fase_origem` abaixo.
    resultado = Column(String, nullable=True)
    # Preenchidos so quando tipo == "mudanca_fase": de qual fase pra qual fase
    # o cliente foi. fase_destino ja existia (usado no relatorio de desempenho
    # por vendedor); fase_origem foi adicionado pra renderizar a mudanca de
    # fase no Historico de forma estruturada (badge -> badge) em vez de so
    # texto livre -- registros antigos (de antes dessa coluna existir) ficam
    # None, o template cai num fallback mais simples nesse caso.
    fase_destino = Column(String, nullable=True, index=True)
    fase_origem = Column(String, nullable=True)

    # Preenchidos so quando tipo in (proposta, pedido): snapshot congelado dos
    # valores NAQUELE momento, pra renderizar um registro estruturado no
    # historico sem depender da PropostaCRM/PedidoCRM (que pode ter mudado
    # desde entao -- editar ou renegociar depois nao pode reescrever o passado).
    numero = Column(Integer, nullable=True)
    produto = Column(String, nullable=True)
    volume = Column(Float, nullable=True)
    preco = Column(Float, nullable=True)
    pagamento = Column(String, nullable=True)
    # Registros de venda via parceiro: o OUTRO cliente envolvido -- no
    # historico do comprador aponta pro cliente final ("destino_registrado"),
    # no do cliente final aponta pro comprador ("destino_final"). Sem FK de
    # proposito: uma segunda FK pra crm_clientes deixaria ambiguo o
    # relationship `cliente` abaixo.
    cliente_relacionado_id = Column(Integer, nullable=True)

    # Quem fez a acao (User.nome_completo -- guardado como texto, nao FK, pra
    # sobreviver mesmo se o usuario for desativado/renomeado depois). None
    # pros registros que vieram da importacao original da planilha (o
    # historico mostra "Sistema" nesse caso, igual o painel-vendedor.html).
    autor = Column(String, nullable=True)

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

def codigo_pedido(numero):
    """Como o pedido gerado no portal aparece pra todo mundo: "PV-0001".
    Nunca so o numero (Rafael, 2026-10-03): os pedidos comecavam em 105601, os
    mesmos numeros de pedidos reais do NetSuite de outros clientes (ex.: 105601/
    SO2765). O prefixo deixa claro que e do portal; o numero do NetSuite (SO)
    entra quando a integracao existir."""
    return f"PV-{numero:04d}" if numero is not None else "—"


STATUS_PEDIDO_ABERTO = "aberto"
STATUS_PEDIDO_CANCELADO = "cancelado"
STATUS_PEDIDO_FINALIZADO = "finalizado"


class ParceiroCessao(Base):
    """Empresa parceira que pode receber cessao de credito de "Plano safra"
    (ex: Juparana, Gees, Agrex -- tradings/fomentadoras de agricultores,
    parceiras de longa data). Precisa estar cadastrada AQUI antes de poder
    ser escolhida numa proposta -- Rafael (2026-09-25) foi explicito que nao
    pode ser nome digitado na hora, tem que ser um cadastro de verdade."""
    __tablename__ = "crm_parceiros_cessao"

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False, unique=True)
    cnpj = Column(String, nullable=True)
    ativo = Column(Boolean, nullable=False, default=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)


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
    # Preenchidos so quando pagamento == "Plano safra" -- ver PLANO_SAFRA_MODALIDADE.
    plano_safra_modalidade = Column(String, nullable=True)
    plano_safra_parceiro = Column(String, nullable=True)
    observacoes = Column(String, nullable=True)
    # aberta: ainda pode virar pedido ou ser editada. convertida: ja virou pedido.
    status = Column(String, nullable=False, default=STATUS_PROPOSTA_ABERTA)
    # Marcado quando o vendedor opta por aplicar o credito pendente do
    # cliente (ver PedidoCRM.saldo_credito_valor) a esta proposta -- so vira
    # consumo de verdade no momento de GERAR o pedido (crm_gerar_pedido), nao
    # aqui: a proposta ainda pode ser editada/abandonada antes disso.
    usa_credito = Column(Boolean, nullable=False, default=False)
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
    # Enquanto o pedido esta "aberto" isso fica None (nao ha acompanhamento
    # continuo de retirada nesse app ainda). No momento de FINALIZAR o pedido
    # (ver crm_finalizar_pedido) o vendedor informa manualmente quanto foi
    # retirado de fato -- medida de FECHAMENTO definitiva, feita uma unica
    # vez, nao um campo editavel a vida toda do pedido. Isso vira automatico
    # quando existir integracao com o NetSuite (dado real da expedicao), mas
    # o Rafael decidiu (2026-09-23, depois de reconsiderar) que ate la o
    # vendedor digita esse numero na hora de finalizar, porque o cliente
    # precisa saber do saldo/credito dele AGORA, nao "quando o NetSuite
    # existir".
    volume_retirado = Column(Float, nullable=True)
    # Preenchidos pelo crm_finalizar_pedido quando o pedido e finalizado com
    # retirada PARCIAL e `condicao_pagamento == "antecipado"` -- e o volume/
    # valor que ficou pago mas nao retirado, guardado como credito do cliente
    # pra usar numa proxima negociacao. None nos demais casos (retirou tudo,
    # ou a condicao e "por_retirada"/padrao -- nesse caso o `volume` do
    # pedido e que e ajustado pra baixo em vez de gerar credito).
    # IMPORTANTE (Rafael, 2026-09-23): o credito de verdade e o VALOR
    # (`saldo_credito_valor`, em R$), nao o volume -- se o preco do produto
    # mudar entre agora e a proxima negociacao (ex: ano seguinte), a mesma
    # tonelada vale menos ou mais, mas o dinheiro que o cliente ja pagou
    # continua o mesmo. `saldo_credito` (tonelada) fica só como registro
    # historico de "quanto era isso, no preco daquele pedido especifico" --
    # nunca deve ser usado pra decidir quanto o cliente pode retirar hoje,
    # só `saldo_credito_valor` dividido pelo preco de HOJE faz isso direito.
    saldo_credito = Column(Float, nullable=True)
    saldo_credito_valor = Column(Float, nullable=True)
    # Combinado no momento de GERAR o pedido (nao na finalizacao) -- None
    # (padrao/retirada unica), "antecipado" (cliente ja pagou o total, pode
    # retirar aos poucos -- sobra vira credito) ou "por_retirada" (cliente
    # paga só o que retira -- sobra so reduz o volume oficial, sem credito).
    # Guardado aqui pra nao precisar perguntar de novo na hora de finalizar.
    condicao_pagamento = Column(String, nullable=True)
    # Preenchido em crm_gerar_pedido quando a PropostaCRM de origem tinha
    # `usa_credito=True` e havia credito disponivel pro produto -- e o valor
    # em R$ (ja pago antes, em outro pedido) que este pedido incorpora SEM
    # cobranca extra. Nao mexe em `volume`/`preco` (que continuam so a parte
    # NOVA sendo vendida) -- o volume fisico total que o cliente pode retirar
    # deste pedido e `volume_total_a_entregar()`, nao so `volume`.
    credito_aplicado_valor = Column(Float, nullable=True)
    # Snapshot JSON (lista de {pedido_id, numero, valor, toneladas}) de quais
    # pedidos tiveram o saldo zerado pra alimentar `credito_aplicado_valor`
    # aqui -- guardado pra poder DEVOLVER o credito certinho aos pedidos de
    # origem se este pedido for cancelado antes de ser retirado de verdade.
    credito_origem_json = Column(String, nullable=True)
    preco = Column(Float, nullable=False)
    pagamento = Column(String, nullable=False)
    prazo = Column(String, nullable=True)
    # Copiados da PropostaCRM de origem quando o pedido e gerado -- ver
    # PLANO_SAFRA_MODALIDADE em models.py.
    plano_safra_modalidade = Column(String, nullable=True)
    plano_safra_parceiro = Column(String, nullable=True)
    # So preenchido em pedidos "Plano safra" com modalidade "cessao" --
    # caminho do PDF/imagem do contrato assinado pelas 3 partes (cliente,
    # parceiro, empresa). Obrigatorio pra gerar o pedido nesse caso (Rafael,
    # 2026-09-25) -- ver crm_gerar_pedido.
    contrato_cessao_path = Column(String, nullable=True)
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
    # Combinada com o cliente no momento de GERAR o pedido (mesmo principio
    # ja usado em `app.models.Pedido.data_limite_retirada` pra Logistica) --
    # ate a integracao com o NetSuite existir, e o unico jeito de saber que
    # um pedido "aberto" esta parado sem ninguem perceber (cliente comprou,
    # nao retirou nada, e ninguem avisou o vendedor). Foi obrigatoria de
    # 2026-09-24 a 2026-09-25; voltou a ser opcional a pedido do Rafael. None =
    # prazo nao combinado (nunca inventar um).
    data_limite_retirada = Column(Date, nullable=True)
    # Pedido a vista (Rafael, 2026-10-02): "o carregamento so e liberado quando
    # confirmado o pagamento". O vendedor anexa o comprovante (ao gerar o
    # pedido ou depois, no proprio pedido -- ver PedidoComprovante) e o
    # FINANCEIRO (ou admin) confere e confirma: so a confirmacao preenche isto
    # e libera o carregamento. None = aguardando pagamento. Volta a None se o
    # pedido for renegociado pra um valor maior: o pagamento ja confirmado
    # nao cobre a diferenca.
    pagamento_liberado_em = Column(DateTime, nullable=True)
    pagamento_liberado_por = Column(String, nullable=True)
    # Comissao do vendedor (Rafael, 2026-10-04): o percentual e gravado quando o
    # pedido e gerado (ou renegociado) com a regra da pagina Regras daquele
    # momento -- mudar a regra so vale pra vendas novas. None = produto sem
    # regra de comissao. `comissao_regra` diz qual regra deu o percentual.
    comissao_pct = Column(Float, nullable=True)
    comissao_regra = Column(String, nullable=True)
    # Pedido a prazo (Rafael, 2026-10-02): carrega direto, sem aprovacao do
    # financeiro, e paga de uma de tres formas (FORMA_PRAZO). `prazo_parcelas`
    # so no boleto ("30" ou "30/60/90", dias contados da 1a carga);
    # `prazo_periodo_dias` so no "por periodo". O bloqueio na portaria (1a
    # parcela vencida, carga sobre rodas nao paga) depende das cargas e dos
    # pagamentos, que vem do NetSuite -- por enquanto a regra so e mostrada.
    forma_prazo = Column(String, nullable=True)
    prazo_parcelas = Column(String, nullable=True)
    prazo_periodo_dias = Column(Integer, nullable=True)
    # Plano safra (Rafael, 2026-10-02): direto com o cliente ou via empresa
    # parceira (cessao, com contrato assinado pelas 3 partes) -- nos dois casos
    # o cliente carrega e paga numa data combinada, normalmente bem longa
    # (ex.: 01/05/2027).
    vencimento_pagamento = Column(Date, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)
    # O mesmo pedido no NetSuite (Rafael, 2026-10-03: "tem que ser SO"). O SO
    # quem cria e o NetSuite, ao lancar o pedido -- o portal nunca inventa um.
    # Ligado sozinho pela planilha de Expedicao (cliente, produto, volume e data
    # batem) ou informado na ficha. Ligado, o pedido passa a aparecer pelo SO e
    # o carregamento vem do faturado do NetSuite.
    pedido_netsuite = Column(String, nullable=True, index=True)
    vinculado_em = Column(DateTime, nullable=True)
    vinculado_por = Column(String, nullable=True)

    cliente = relationship("ClienteCRM", backref="pedidos_crm")
    proposta = relationship("PropostaCRM", backref="pedido")

    @property
    def codigo(self):
        """SO do NetSuite quando ja foi lancado; ate la, o provisorio PV-0001."""
        return self.pedido_netsuite or codigo_pedido(self.numero)

    @property
    def codigo_portal(self):
        return codigo_pedido(self.numero)

    def aguardando_netsuite(self):
        return not self.pedido_netsuite and self.status != STATUS_PEDIDO_CANCELADO

    def sincronizar_com_netsuite(self, pedido_ns):
        """Ligado ao pedido do NetSuite: carregamento real e data limite.
        A data limite vale a da Logistica; se ela nao tem, leva a que o
        vendedor combinou ao gerar o pedido."""
        if self.status == STATUS_PEDIDO_ABERTO:
            self.volume_retirado = pedido_ns.faturado
        if pedido_ns.data_limite_retirada:
            self.data_limite_retirada = pedido_ns.data_limite_retirada
        elif self.data_limite_retirada:
            pedido_ns.data_limite_retirada = self.data_limite_retirada
            return True  # a Logistica recebeu a data do vendedor
        return False

    def valor_total(self):
        return (self.volume or 0) * (self.preco or 0)

    # ---- Comissao: so e elegivel o que a empresa ja recebeu (Rafael, 2026-10-04)
    def recebido(self):
        """Quanto o cliente ja pagou deste pedido (RecebimentoPedido): a vista
        quando o financeiro confirma o comprovante; os outros, cada pagamento
        que o financeiro registra (carga a carga, parcela, plano safra)."""
        return min(sum(r.valor for r in self.recebimentos), self.valor_total())

    def a_receber(self):
        return max(self.valor_total() - self.recebido(), 0)

    def comissao_prevista(self):
        return None if self.comissao_pct is None else self.valor_total() * self.comissao_pct / 100

    def comissao_elegivel(self):
        return None if self.comissao_pct is None else self.recebido() * self.comissao_pct / 100

    def comissao_a_liberar(self):
        return None if self.comissao_pct is None else self.a_receber() * self.comissao_pct / 100

    def regra_prazo(self):
        """A regra combinada do a prazo, em uma frase (None se nao e a prazo)."""
        if self.pagamento != "A prazo":
            return None
        if self.forma_prazo == "boleto":
            primeira = (self.prazo_parcelas or "30").split("/")[0]
            parcelas = "parcela única" if "/" not in (self.prazo_parcelas or "30") else f"parcelas de {self.prazo_parcelas} dias"
            return (f"Boleto após a 1ª carga ({parcelas}). Se a 1ª parcela não for paga em {primeira} dias, "
                    "o carregamento é bloqueado na portaria até o pagamento.")
        if self.forma_prazo == "sobre_rodas":
            return "Pagamento sobre rodas: o cliente paga cada carga antes do próximo caminhão carregar."
        if self.forma_prazo == "periodo":
            return f"O cliente puxa durante {self.prazo_periodo_dias or 7} dias e paga ao fim de cada período."
        return "Forma do a prazo não informada."

    def regra_pagamento(self):
        """Como o cliente paga, em uma frase -- a prazo e plano safra (None no
        a vista, que tem o caminho do comprovante)."""
        if self.pagamento == "A prazo":
            return self.regra_prazo()
        if self.pagamento == "Plano safra":
            quando = (f"paga em {self.vencimento_pagamento.strftime('%d/%m/%Y')}" if self.vencimento_pagamento
                      else "data de pagamento não informada")
            if self.plano_safra_modalidade == "cessao":
                return (f"Plano safra via {self.plano_safra_parceiro or 'parceiro'}: {quando}, conforme o contrato "
                        "de cessão assinado pelas 3 partes.")
            return f"Plano safra direto com o cliente: carrega agora e {quando}."
        return None

    def exige_comprovante(self):
        """So pedido a vista: o cliente paga o total e depois retira."""
        return self.pagamento == "A vista"

    def aguardando_pagamento(self):
        """Pedido a vista em aberto sem pagamento confirmado: carregamento
        bloqueado (e, por consequencia, nao ha retirada pra finalizar)."""
        return self.status == STATUS_PEDIDO_ABERTO and self.exige_comprovante() and not self.pagamento_liberado_em

    def comprovantes_pendentes(self):
        return [c for c in self.comprovantes if c.status == COMPROVANTE_PENDENTE]

    def situacao_pagamento(self):
        """Onde o pedido a vista em aberto esta no caminho ate o carregamento.
        None pra pedido que nao exige comprovante ou que nao esta em aberto.
        - sem_comprovante: vendedor ainda nao anexou nada
        - em_conferencia: tem comprovante esperando o financeiro
        - recusado: o financeiro recusou o ultimo comprovante
        - diferenca: pagamento confirmado, mas o pedido ficou mais caro depois
        - liberado: pagamento confirmado, pode carregar"""
        if self.status != STATUS_PEDIDO_ABERTO or not self.exige_comprovante():
            return None
        if self.pagamento_liberado_em:
            return "liberado"
        if self.comprovantes_pendentes():
            return "em_conferencia"
        if not self.comprovantes:
            return "sem_comprovante"
        return "recusado" if self.comprovantes[-1].status == COMPROVANTE_RECUSADO else "diferenca"

    def dias_restantes(self):
        if not self.data_limite_retirada:
            return None
        return (self.data_limite_retirada - dt.date.today()).days

    def vencido(self):
        """Mesmo principio da Logistica: prazo estourado e ainda tem volume
        pra entregar. Pedido CRM nao tem acompanhamento continuo de retirada
        enquanto 'aberto' (so no Finalizar) -- entao 'aberto' JA significa
        que o saldo inteiro (`volume_total_a_entregar()`) ainda esta em
        aberto, nenhuma retirada foi registrada ainda."""
        if self.status != STATUS_PEDIDO_ABERTO:
            return False
        dias = self.dias_restantes()
        return dias is not None and dias <= 0

    def ton_dia_necessario(self):
        """Mesma metrica de urgencia da Logistica (saldo / dias restantes)."""
        if self.status != STATUS_PEDIDO_ABERTO:
            return None
        dias = self.dias_restantes()
        if dias is None or dias <= 0:
            return None
        saldo = self.volume_total_a_entregar()
        if saldo <= 0:
            return None
        return round(saldo / dias, 2)

    def toneladas_credito_aplicado(self):
        """Quantas toneladas o credito aplicado (R$) vale HOJE, ao preco
        deste proprio pedido -- recalcula sozinho se o pedido for renegociado
        (mesma logica ja usada pro hint da aba Proposta: valor fixo, tonelada
        varia com o preco)."""
        if not self.credito_aplicado_valor or not self.preco:
            return 0.0
        return self.credito_aplicado_valor / self.preco

    def volume_total_a_entregar(self):
        """Volume fisico total que o cliente pode retirar deste pedido:
        o que foi contratado/cobrado (`volume`) MAIS as toneladas cobertas
        pelo credito ja pago antes (`credito_aplicado_valor`), que nao geram
        cobranca nova."""
        return (self.volume or 0) + self.toneladas_credito_aplicado()

    def volume_destino_alocado(self):
        return sum(d.volume for d in self.destinos_finais)

    def volume_destino_pendente(self):
        return max(0.0, (self.volume or 0) - self.volume_destino_alocado())


class PedidoDestinoFinal(Base):
    """Venda faturada pra um parceiro/transportadora/consultor (dono do
    PedidoCRM), mas com o produto indo fisicamente pra um cliente final nosso
    -- padrao conhecido como "venda a ordem"/entrega a terceiro. A regra
    fiscal (qual nota emitir) fica com o setor fiscal/NetSuite; aqui so
    registramos pra quem o produto foi de verdade. Um pedido pode ser rateado
    entre varios clientes finais, e nao precisa fechar 100% do volume -- a
    transportadora nem sempre informa todos os destinos (Rafael, 2026-09-25)."""
    __tablename__ = "crm_pedido_destino_final"

    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey("crm_pedidos.id"), nullable=False, index=True)
    cliente_final_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    volume = Column(Float, nullable=False)
    observacoes = Column(String, nullable=True)
    autor = Column(String, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)

    pedido = relationship("PedidoCRM", backref="destinos_finais")
    cliente_final = relationship("ClienteCRM", backref="recebimentos_indiretos")


# Formas de pagamento a prazo combinadas com o cliente (Rafael, 2026-10-02)
FORMA_PRAZO = {
    "boleto": "Boleto após a 1ª carga",
    "sobre_rodas": "Pagamento sobre rodas",
    "periodo": "Puxa por período e paga depois",
}
PARCELAS_BOLETO = ("30", "30/60/90")

COMPROVANTE_PENDENTE = "pendente"
COMPROVANTE_CONFIRMADO = "confirmado"
COMPROVANTE_RECUSADO = "recusado"
# Motivos de recusa oferecidos ao financeiro ("Outro" exige detalhe)
MOTIVOS_RECUSA_COMPROVANTE = ["Valor não confere com o pedido", "Pagamento não caiu na conta",
                              "Comprovante de agendamento, não de pagamento", "Comprovante ilegível", "Outro"]


class PedidoComprovante(Base):
    """Comprovante de pagamento de um pedido a vista. Pode haver mais de um
    (cliente pagou em duas transferencias, ou pagou a diferenca depois de uma
    renegociacao). O arquivo tem dado bancario do cliente: fica em
    uploads/comprovantes_pagamento, fora de static/, e so sai pela rota que
    confere a carteira (mesmo cuidado do contrato de cessao).

    Comprovante nao e dinheiro na conta (pode ser agendamento, ou falso): o
    financeiro (ou admin) confere cada um e confirma ou recusa (Rafael,
    2026-10-02). So a confirmacao libera o carregamento."""
    __tablename__ = "crm_pedido_comprovantes"

    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey("crm_pedidos.id"), nullable=False, index=True)
    arquivo = Column(String, nullable=False)
    nome_original = Column(String, nullable=True)
    autor = Column(String, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)
    status = Column(String, nullable=False, default=COMPROVANTE_PENDENTE)  # pendente | confirmado | recusado
    conferido_em = Column(DateTime, nullable=True)
    conferido_por = Column(String, nullable=True)
    motivo_recusa = Column(String, nullable=True)

    pedido = relationship("PedidoCRM", backref=backref("comprovantes", order_by="PedidoComprovante.criado_em"))


class AvisoCRM(Base):
    """Caixa de avisos do vendedor (ideia do painel-vendedor.html de
    referencia, construida de verdade em 2026-09-25): notificacao de algo que
    aconteceu com a carteira dele e que ele nao fez -- cliente novo colocado
    por outra pessoa, cliente dele que recebeu produto via transportadora de
    outro vendedor. `vendedor_nome` e o destinatario (mesma chave da carteira,
    ClienteCRM.vendedor_nome)."""
    __tablename__ = "crm_avisos"

    id = Column(Integer, primary_key=True)
    vendedor_nome = Column(String, nullable=False, index=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=True)
    tipo = Column(String, nullable=False)  # cliente_novo | recebeu_via_parceiro | pedido_parceiro_cancelado
    titulo = Column(String, nullable=False)
    texto = Column(String, nullable=False)
    autor = Column(String, nullable=True)
    lido = Column(Boolean, nullable=False, default=False)
    lido_em = Column(DateTime, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow)
    # Para onde o aviso leva quando nao e a ficha do cliente (ex.: a conversa
    # do pedido quando a Logistica cobra retorno do vendedor)
    link = Column(String, nullable=True)

    cliente = relationship("ClienteCRM")


class CicloVendas(Base):
    """Singleton (1 linha so) que registra qual ciclo de vendas ja foi
    processado. `ciclo_atual` e o rotulo ("2026/2027", ver `ciclo_rotulo()`);
    `resetado_em` fica None ate a primeira virada de verdade acontecer (a
    linha e criada so pra "assumir" o ciclo corrente na primeira vez que o
    sistema roda essa checagem, sem resetar nada retroativamente)."""
    __tablename__ = "ciclo_vendas"

    id = Column(Integer, primary_key=True)
    ciclo_atual = Column(String, nullable=False)
    resetado_em = Column(DateTime, nullable=True)


class ImportacaoPlanilha(Base):
    """Cada leitura da planilha de Expedicao: de quando sao os dados (o
    "Atualizado em" da propria planilha) e quando entraram no portal. A
    Logistica mostra isso -- dado de expedicao sem data engana."""
    __tablename__ = "importacoes_planilha"

    id = Column(Integer, primary_key=True)
    fonte = Column(String, nullable=False)
    atualizada_em = Column(DateTime, nullable=True)
    importada_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    linhas = Column(Integer, nullable=False, default=0)
    resumo = Column(String, nullable=True)


class AreaEstado(Base):
    """Area de referencia por UF pro % de mercado (quadro "Clientes por estado"
    do Inicio do admin e relatorio Market share). Desde 2026-10-04 e a area
    plantada de lavouras do IBGE (PAM, ibge_area.py; o nome da coluna ficou do
    tempo do placeholder). placeholder=True = numero sem fonte oficial."""
    __tablename__ = "crm_area_estados"

    uf = Column(String, primary_key=True)
    area_agropecuaria_ha = Column(Float, nullable=False)
    fonte = Column(String, nullable=True)
    placeholder = Column(Boolean, default=True, nullable=False)


class RotaCache(Base):
    """Rota de estrada ja calculada (planta -> local de entrega), pra nao pedir
    de novo ao servico gratuito de rotas (rotas.py). Estrada quase nao muda;
    a chave e o par de pontos arredondado (~1 m)."""
    __tablename__ = "rotas_cache"

    id = Column(Integer, primary_key=True)
    chave = Column(String, nullable=False, unique=True, index=True)
    km = Column(Float, nullable=False)
    minutos = Column(Float, nullable=False)
    trajeto = Column(String, nullable=False)  # polyline codificada (formato do Google/OSRM)
    calculada_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)


class RegraRevisao(Base):
    """Pagina Regras, etapa 1 (Rafael, 2026-10-03): o admin confere cada regra
    do catalogo (regras.py) -- "ok" ou "corrigir" com o que esta errado. Uma
    linha por regra (a ultima revisao)."""
    __tablename__ = "regras_revisao"

    id = Column(Integer, primary_key=True)
    regra_id = Column(String, nullable=False, unique=True, index=True)
    situacao = Column(String, nullable=False)  # "ok" | "corrigir"
    comentario = Column(String, nullable=True)
    quem = Column(String, nullable=True)
    quando = Column(DateTime, default=dt.datetime.utcnow, nullable=False)


class ConfigRegra(Base):
    """Pagina Regras, etapa 2 (Rafael, 2026-10-04): valor que o admin mudou.
    So guarda o que difere do padrao (config.PARAMETROS); sem linha = padrao.
    `valor` em JSON."""
    __tablename__ = "config_regras"

    chave = Column(String, primary_key=True)
    valor = Column(String, nullable=False)
    alterado_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    alterado_por = Column(String, nullable=True)


class ConfigHistorico(Base):
    """Toda mudanca de regra: quem, quando, de quanto pra quanto (JSON)."""
    __tablename__ = "config_historico"

    id = Column(Integer, primary_key=True)
    chave = Column(String, nullable=False, index=True)
    antes = Column(String, nullable=True)
    depois = Column(String, nullable=True)
    quem = Column(String, nullable=True)
    quando = Column(DateTime, default=dt.datetime.utcnow, nullable=False)


class OportunidadeLogistica(Base):
    """Oportunidade logistica (Rafael, 2026-10-04): a Logistica sabe de caminhao
    indo pra uma regiao (frete mais barato) e manda pro vendedor clientes dali
    que ainda nao tem pedido em aberto. Fica na fila de trabalho do vendedor ate
    `valida_ate` ou ate ele registrar um contato real com o cliente depois de
    `criada_em` (crm_routes._oportunidades_abertas)."""
    __tablename__ = "oportunidades_logisticas"

    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("crm_clientes.id"), nullable=False, index=True)
    vendedor_nome = Column(String, nullable=True)
    destino = Column(String, nullable=False)        # "Paragominas/PA"
    raio_km = Column(Integer, nullable=True)
    distancia_km = Column(Integer, nullable=True)   # do cliente ate o destino do caminhao (linha reta)
    data_caminhao = Column(Date, nullable=True)
    valida_ate = Column(Date, nullable=False)
    recado = Column(String, nullable=True)
    criada_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    criada_por = Column(String, nullable=True)


class RecebimentoPedido(Base):
    """Dinheiro que entrou de um pedido do portal (Rafael, 2026-10-04: a
    comissao do vendedor so e elegivel quando a empresa recebe). A vista: criado
    sozinho quando o financeiro confirma o comprovante (origem "comprovante").
    Carga a carga, a prazo e plano safra: o financeiro registra cada pagamento
    (origem "financeiro"). Ate a integracao com o NetSuite, e a unica fonte."""
    __tablename__ = "crm_recebimentos"

    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey("crm_pedidos.id"), nullable=False, index=True)
    valor = Column(Float, nullable=False)
    data = Column(Date, nullable=False)
    origem = Column(String, nullable=False, default="financeiro")
    observacao = Column(String, nullable=True)
    registrado_por = Column(String, nullable=True)
    registrado_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    comissao_pct = Column(Float, nullable=True)  # o percentual do pedido quando o dinheiro entrou

    pedido = relationship("PedidoCRM", backref=backref("recebimentos", order_by="RecebimentoPedido.data"))


class SenhaToken(Base):
    """Link de "Esqueci minha senha" (acessos.py): guarda so o hash do token, vale 30 minutos e uma vez so."""
    __tablename__ = "senha_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    expira_em = Column(DateTime, nullable=False)
    usado_em = Column(DateTime, nullable=True)


class PedidoSenha(Base):
    """Pedido de nova senha de quem nao tem e-mail cadastrado (ou com o envio de e-mail falhando): vira tarefa no
    Inicio do admin, que redefine a senha em Pessoas e acessos."""
    __tablename__ = "pedidos_senha"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    motivo = Column(String, nullable=True)
    criado_em = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    atendido_em = Column(DateTime, nullable=True)
    atendido_por = Column(String, nullable=True)

    user = relationship("User")
