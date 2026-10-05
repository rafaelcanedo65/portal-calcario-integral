"""Pagina Regras (Rafael, 2026-10-03/04): "todas as regras do funcionamento do
portal, cada uma especificada e com suas condicoes", e o admin poder ajustar
ou desligar pelo proprio portal.

- Catalogo (REGRAS): descricao, condicoes, parametros e onde aparece. Parametro
  com `chave` e editavel (config.py); sem chave e so informativo (o valor e
  lido do codigo na hora).
- Etapa 2 (2026-10-04): o admin muda numeros e liga/desliga; antes de salvar a
  tela mostra o efeito (`previa`, com config.simular) e tudo vai pro historico
  (ConfigHistorico). "Voltar ao padrao" preenche os padroes e passa pela mesma
  previa.

Tipos (decisao do Rafael, 2026-10-03):
- ajustavel: numero/prazo que o admin muda (alguns tambem desligam);
- liga: alerta ou automacao que pode ser desligada sem estragar dado;
- travada: protege os dados (cadastro, duplicados, pagamento...) -- aparece
  com o porque, mas nao muda;
- funcionamento: como o sistema decide; mudar exige desenvolvimento."""
import datetime as dt
import json
from collections import Counter
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import config
from .auth import require_role
from .database import get_db
from .feedback import avisar_erro, avisar_sucesso
from .models import ConfigHistorico, RegraRevisao, User

router = APIRouter()

TIPOS = {
    "ajustavel": ("Ajustável", "O admin muda os números aqui."),
    "liga": ("Liga/desliga", "Pode ser desligada sem estragar dados."),
    "travada": ("Travada", "Protege os dados: aparece aqui, mas não muda."),
    "funcionamento": ("Funcionamento", "Explica como o portal decide; mudar exige desenvolvimento."),
}
MESES = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
MESES_EXTENSO = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
                 "novembro", "dezembro")


def _de(modulo, nome):
    """Valor ao vivo de uma constante do codigo (import tardio: evita ciclo)."""
    def ler():
        import importlib
        return getattr(importlib.import_module(f"app.{modulo}"), nome)
    return ler


def _p(nome, valor, unidade=""):
    """Parametro so informativo."""
    return {"nome": nome, "valor": valor, "unidade": unidade}


def _c(nome, chave, unidade=""):
    """Parametro editavel (config.py)."""
    return {"nome": nome, "chave": chave, "unidade": unidade}


GRUPOS = [
    ("ciclo", "Ciclo de vendas e etapas do cliente"),
    ("fila", "Fila de trabalho do vendedor"),
    ("cadastro", "Cadastro do cliente"),
    ("pedido", "Propostas, pedidos e pagamento"),
    ("comissao", "Comissão do vendedor"),
    ("logistica", "Logística"),
    ("local", "Local de entrega e rota"),
    ("planilha", "Planilha de Expedição (NetSuite)"),
    ("gestao", "Início do admin e relatórios"),
    ("acesso", "Acesso: quem vê o quê"),
]

REGRAS = [
    # ---------------- Ciclo de vendas e etapas
    {"id": "ciclo-virada", "grupo": "ciclo", "tipo": "ajustavel", "nome": "Virada do ciclo de vendas",
     "descricao": "Todo ano, numa data fixa, começa um novo ciclo de vendas (safra) e a carteira volta a ser prospectada.",
     "condicoes": [
         "Clientes em Perdido, Não usará, Realizado e Contactados voltam para \"A contactar\".",
         "Clientes em Proposta não mudam: já têm prioridade própria pela proposta parada.",
         "Histórico, último contato e pedidos não são apagados.",
         "Cliente que ainda tem pedido em aberto também volta, mas com uma nota própria e entra na fila como \"Pedido em aberto\".",
     ],
     "parametros": [_c("Dia da virada (dia/mês)", "ciclo_virada")],
     "atencao": "Se a nova data mudar o ciclo de hoje, a carteira de todos os vendedores é reiniciada na hora. O portal avisa e pede confirmação.",
     "onde": "Início do vendedor, fases do CRM"},
    {"id": "ciclo-contador", "grupo": "ciclo", "tipo": "ajustavel", "nome": "Contador para o novo ciclo", "liga": "liga_ciclo_contador",
     "descricao": "O Início do vendedor mostra quantos dias faltam para a virada do ciclo.",
     "condicoes": ["Só aparece quando faltam poucos dias, para não virar ruído o ano todo."],
     "parametros": [_c("Aparece quando faltam até", "ciclo_contador_dias", "dias")], "onde": "Início do vendedor"},
    {"id": "ciclo-etapa", "grupo": "ciclo", "tipo": "funcionamento", "nome": "Etapa do cliente muda pelo resultado do contato",
     "descricao": "Ninguém move cliente no funil na mão: o vendedor registra o resultado do contato e o portal decide a etapa.",
     "condicoes": [
         "A etapa só avança (A contactar → Contactados → Proposta → Realizado); nunca volta por um contato.",
         "\"Sem interesse\" leva a Não usará e \"Perdido\" a Perdido, de qualquer etapa.",
         "\"Contato não funciona\" não muda a etapa: o caso vai para o admin.",
         "Proposta ou pedido de outro produto não puxa o cliente para trás (ex.: Realizado não volta para Proposta).",
     ], "onde": "Ficha do cliente (registrar contato)"},

    # ---------------- Fila de trabalho do vendedor
    {"id": "fila-ordem", "grupo": "fila", "tipo": "ajustavel", "nome": "Ordem da fila de trabalho", "lista": "fila", "ordem": "ordem_fila",
     "descricao": "Cada cliente entra na fila por um único motivo: o primeiro desta lista que se aplicar, de cima para baixo.",
     "condicoes": [
         "Arraste pela alça (ou use as setas) para mudar a prioridade. Mudar a ordem muda o motivo de quem se encaixa em mais de um.",
         "Dentro do mesmo motivo, desempata pelo critério dele (t/dia, dias sem contato, área) e depois cliente quente antes do frio.",
         "Motivo desligado não entra; o cliente cai no próximo que se aplicar. Pedido vencido e pedido em aberto ficam sempre ligados.",
         "Cliente com \"contato não funciona\" sai da fila do vendedor e vai para o admin.",
     ],
     "itens": [
         {"chave": "vencido", "nome": "nome_fila_vencido", "descricao": "Pedido do portal com prazo vencido e sem retirada."},
         {"chave": "apertada", "nome": "nome_fila_apertada", "liga": "liga_fila_apertada",
          "descricao": "Precisaria retirar muito por dia até a data limite (saldo ÷ dias).",
          "parametros": [_c("A partir de", "retirada_apertada_t_dia", "t/dia")],
          "atencao": "Mesmo número da Logística: mudar aqui muda lá também."},
         {"chave": "oportunidade", "nome": "nome_fila_oportunidade", "liga": "liga_fila_oportunidade",
          "descricao": "A Logística avisou: caminhão indo para a região do cliente, frete mais barato. Sai quando o vendedor registra contato.",
          "parametros": [_c("Sem data do caminhão, vale por", "oportunidade_dias", "dias")]},
         {"chave": "pedido_aberto", "nome": "nome_fila_pedido_aberto",
          "descricao": "Voltou para a prospecção mas ainda tem pedido em aberto: confirmar se retira ou finaliza."},
         {"chave": "quente", "nome": "nome_fila_quente", "liga": "liga_fila_quente",
          "descricao": "Proposta aberta com cliente quente fica até ter resultado."},
         {"chave": "epoca", "nome": "nome_fila_epoca", "liga": "liga_fila_epoca",
          "descricao": "No mês em que costuma comprar e no anterior. Depois do contato, volta conforme a temperatura.",
          "parametros": [_c("Volta depois de (quente)", "fila_epoca_quente", "dias"), _c("(morno)", "fila_epoca_morno", "dias"),
                         _c("(frio ou sem avaliação)", "fila_epoca_frio", "dias")]},
         {"chave": "proposta", "nome": "nome_fila_proposta", "liga": "liga_fila_proposta",
          "descricao": "Proposta aberta parada (ligar para o cliente também tira da fila).",
          "parametros": [_c("Parada há", "fila_proposta_dias", "dias"), _c("Perto da virada, parada há", "fila_proposta_dias_perto", "dias"),
                         _c("\"Perto da virada\" = faltando até", "fila_proposta_janela", "dias")]},
         {"chave": "sem_contato", "nome": "nome_fila_sem_contato", "liga": "liga_fila_sem_contato",
          "descricao": "Cliente ativo sem contato. Entra aos poucos: mais tempo sem contato e maior área primeiro.",
          "parametros": [_c("Sem contato há", "fila_sem_contato_dias", "dias"), _c("Quantos por vez", "fila_sem_contato_lote", "clientes")]},
         {"chave": "primeiro_contato", "nome": "nome_fila_primeiro_contato", "liga": "liga_fila_primeiro_contato",
          "descricao": "Nunca contatado: cliente novo antes de lead da planilha, maior área primeiro. Sem telefone válido não entra.",
          "parametros": [_c("Quantos por vez", "fila_primeiro_contato_lote", "clientes")]},
     ],
     "onde": "Início e Fila do vendedor, ficha do cliente"},
    {"id": "fila-contato-invalido", "grupo": "fila", "tipo": "ajustavel", "nome": "Contato que não funciona", "liga": "liga_contato_automatico",
     "descricao": "Número errado, sem WhatsApp ou cliente que não retorna: o caso sai da fila do vendedor e vai para o admin conseguir outro contato.",
     "condicoes": [
         "Vai quando o vendedor marca \"Contato não funciona\" (sempre) ou, se ligado, depois de várias tentativas seguidas sem resposta.",
         "Motivos possíveis: " + "; ".join(["Número errado", "Número desativado", "Não recebe ligações", "Sem WhatsApp", "Atende outra pessoa"]) + ".",
     ],
     "parametros": [_c("Tentativas seguidas sem resposta", "contato_tentativas")],
     "onde": "Ficha do cliente, Início do admin (Contatos a resolver)"},

    # ---------------- Cadastro
    {"id": "cad-obrigatorios", "grupo": "cadastro", "tipo": "travada", "nome": "Campos obrigatórios do cliente novo",
     "descricao": "Sem estes dados o cliente não é cadastrado.",
     "condicoes": ["Nome da fazenda, estado, cidade, proprietário/contato e telefone.",
                   "A cidade tem que ser da lista oficial do IBGE do estado escolhido."],
     "parametros": [_p("Estados de operação", lambda: ", ".join(_de("models", "ESTADOS_OPERACAO")()))],
     "onde": "Cadastrar cliente, Editar cadastro"},
    {"id": "cad-telefone", "grupo": "cadastro", "tipo": "travada", "nome": "Telefone válido",
     "descricao": "Telefone brasileiro com DDD: 10 dígitos (fixo) ou 11 (celular), em qualquer formatação.",
     "condicoes": ["Telefone inválido bloqueia o cadastro e coloca o cliente na fila de cadastro."], "onde": "Cadastro do cliente"},
    {"id": "cad-duplicado", "grupo": "cadastro", "tipo": "travada", "nome": "Cliente duplicado",
     "descricao": "O portal não deixa cadastrar o mesmo cliente duas vezes.",
     "condicoes": ["Telefone, e-mail ou CNPJ já usados em outro cliente bloqueiam (aviso enquanto digita e de novo ao salvar).",
                   "Nome de fazenda parecido com um já cadastrado: avisa e pede confirmação."],
     "onde": "Cadastro do cliente"},
    {"id": "cad-completo", "grupo": "cadastro", "tipo": "travada", "nome": "Cadastro completo (inclui área plantada)",
     "descricao": "Área plantada é a base do market share. Cadastro sem ela (ou sem cidade, proprietário ou telefone válido) fica incompleto.",
     "condicoes": ["Cliente incompleto entra na fila de atualização de cadastro.", "E-mail, CNPJ, empresa e frota própria são opcionais."],
     "onde": "Fila de cadastro, Início do admin (clientes por estado), relatório Market share"},
    {"id": "cad-fila", "grupo": "cadastro", "tipo": "ajustavel", "nome": "Ordem da fila de atualização de cadastro", "lista": "cad", "ordem": "ordem_cad",
     "descricao": "Trabalho de \"quando der tempo\", separado da fila de trabalho. Os clientes são ordenados por estes critérios, de cima para baixo.",
     "condicoes": ["Arraste pela alça (ou use as setas) para mudar qual critério pesa mais. Critério desligado não conta.",
                   "Lead esperando o primeiro contato fica fora: o vendedor levanta os dados nesse contato.",
                   "Na entressafra a fila de cadastro aparece antes da fila de trabalho no Início."],
     "itens": [
         {"chave": "sem_telefone", "rotulo": "Sem telefone válido", "liga": "liga_cad_sem_telefone", "descricao": "Sem telefone nem dá para ligar."},
         {"chave": "ja_comprou", "rotulo": "Quem já comprou", "liga": "liga_cad_ja_comprou",
          "descricao": "Realizado, depois Proposta, Contactados e A contactar."},
         {"chave": "maior_area", "rotulo": "Maior área plantada", "liga": "liga_cad_maior_area", "descricao": "Área maior primeiro."},
     ],
     "parametros": [_p("Meses de entressafra", lambda: ", ".join(MESES[m - 1] for m in _de("crm_routes", "ENTRESSAFRA_MESES")()))],
     "onde": "Início do vendedor, Fila de cadastro"},
    {"id": "cad-categoria", "grupo": "cadastro", "tipo": "ajustavel", "nome": "Categoria do cliente (A, B, C)",
     "descricao": "Vem da planilha Controle quando existe; senão, é estimada pela área plantada.",
     "parametros": [_c("Categoria A a partir de", "categoria_a_ha", "ha"), _c("Categoria B a partir de", "categoria_b_ha", "ha")],
     "onde": "Ficha do cliente"},
    {"id": "cad-coordenada", "grupo": "cadastro", "tipo": "travada", "nome": "Coordenadas da fazenda",
     "descricao": "O ponto da fazenda vira o local de entrega dos pedidos e a rota da Logística.",
     "condicoes": ["Aceita o botão do celular, -3.0021, -47.3527, link do Google Maps e graus/minutos/segundos.",
                   "Texto que não é coordenada é recusado; a coordenada válida é gravada num formato só.",
                   "O botão do celular só funciona com o portal em endereço seguro (https)."],
     "onde": "Cadastro do cliente"},

    # ---------------- Propostas, pedidos e pagamento
    {"id": "ped-proposta", "grupo": "pedido", "tipo": "travada", "nome": "Proposta válida",
     "descricao": "O que uma proposta precisa ter para ser salva.",
     "condicoes": ["Produto da lista, volume e preço maiores que zero e forma de pagamento.",
                   "A prazo: informar o prazo. Plano safra: escolher direto ou cessão de crédito.",
                   "Cessão: o parceiro tem que estar cadastrado e ativo."],
     "onde": "Ficha do cliente (Proposta)"},
    {"id": "ped-duplicado", "grupo": "pedido", "tipo": "travada", "nome": "Pedido do mesmo produto em andamento",
     "descricao": "Se o cliente já tem pedido do mesmo produto em aberto, o vendedor confirma se é uma compra a mais ou se é a mesma venda "
                  "renegociada (aí renegocia o pedido existente, para não contar duas vendas).",
     "onde": "Gerar pedido"},
    {"id": "ped-gerar", "grupo": "pedido", "tipo": "travada", "nome": "Gerar pedido",
     "descricao": "Dados exigidos ao transformar a proposta em pedido.",
     "condicoes": ["Subsidiária que fatura (quando há subsidiária cadastrada para o produto).",
                   "Para quem fatura: o próprio cliente ou outro CPF/CNPJ (com nome e documento).",
                   "Data limite de retirada é opcional; se informada, não pode ser no passado. Sem ela, o pedido não entra no aviso de \"vencido sem retirada\" do vendedor."],
     "onde": "Gerar pedido"},
    {"id": "ped-avista", "grupo": "pedido", "tipo": "travada", "nome": "Pedido à vista",
     "descricao": "O cliente paga o total e só depois carrega: o carregamento só é liberado quando o financeiro confirma o comprovante.",
     "condicoes": ["O comprovante pode ir junto com o pedido ou depois.",
                   "Se o pedido ficar mais caro depois de pago, volta a esperar a diferença."],
     "parametros": [_p("Arquivos aceitos", lambda: ", ".join(e.strip(".").upper() for e in _de("crm_routes", "EXTENSOES_COMPROVANTE")())),
                    _p("Tamanho máximo", lambda: _de("crm_routes", "TAMANHO_MAX_COMPROVANTE")() // (1024 * 1024), "MB")],
     "onde": "Pedido no CRM, Pagamentos a confirmar"},
    {"id": "ped-financeiro", "grupo": "pedido", "tipo": "travada", "nome": "Conferência do financeiro",
     "descricao": "O financeiro confirma ou recusa o comprovante do pedido à vista.",
     "condicoes": ["O valor conferido tem que bater com o valor do pedido; se o pedido mudou enquanto ele conferia, precisa conferir de novo.",
                   "Ao confirmar, o carregamento é liberado e o vendedor é avisado. Ao recusar, informa o motivo."],
     "onde": "Pagamentos a confirmar"},
    {"id": "ped-aprazo", "grupo": "pedido", "tipo": "funcionamento", "nome": "Pedido a prazo",
     "descricao": "Carrega direto, sem aprovação do financeiro; o que fica combinado é como o cliente paga.",
     "condicoes": ["Boleto após a 1ª carga (parcela única de 30 dias ou 30/60/90). Regra combinada, mostrada no pedido: se a 1ª "
                   "parcela não for paga no prazo, o carregamento é bloqueado na portaria (o portal não bloqueia sozinho).",
                   "Sobre rodas: paga cada carga antes do próximo caminhão.",
                   "Por período: puxa durante alguns dias e paga ao fim de cada período (de 1 a 90 dias; padrão 7)."],
     "onde": "Gerar pedido"},
    {"id": "ped-safra", "grupo": "pedido", "tipo": "travada", "nome": "Plano safra",
     "descricao": "Direto com o cliente ou via parceiro (cessão de crédito): carrega agora e paga numa data combinada.",
     "condicoes": ["A data de pagamento precisa ser no futuro.",
                   "Cessão: obrigatório anexar o contrato assinado pelas 3 partes (cliente, parceiro e nós), em PDF ou foto."],
     "onde": "Gerar pedido"},
    {"id": "ped-parceiro", "grupo": "pedido", "tipo": "travada", "nome": "Parceiro de cessão de crédito",
     "descricao": "Parceiro de cessão de crédito só entra em contrato novo com CNPJ válido (14 números, com dígito verificador).",
     "condicoes": ["Parceiro novo só é cadastrado com CNPJ, e CNPJ que ainda não esteja em outro parceiro.",
                   "Parceiro sem CNPJ não pode ser escolhido em proposta nova ou editada, e o pedido com cessão não é gerado "
                   "(nem de proposta feita antes do bloqueio). Na lista ele aparece como \"sem CNPJ\".",
                   "Renegociar um pedido que já existe mantendo o mesmo parceiro continua permitido (não é contrato novo).",
                   "O admin completa o CNPJ em Parceiros (o Início do admin mostra quantos faltam)."],
     "onde": "Parceiros, Proposta, Início do admin"},
    {"id": "ped-codigo", "grupo": "pedido", "tipo": "funcionamento", "nome": "Número do pedido (PV e SO)",
     "descricao": "Pedido gerado no portal recebe um número PV (PV-0001...) e aparece \"aguardando NetSuite\" até ser ligado ao SO do NetSuite.",
     "condicoes": ["A ligação é automática pela planilha de Expedição ou informada na ficha do pedido."],
     "onde": "Pedidos, Logística"},
    {"id": "ped-destino", "grupo": "pedido", "tipo": "travada", "nome": "Venda para transportadora ou consultor: destino final",
     "descricao": "O produto não é entregue no endereço de quem comprou: o vendedor registra o destino final (para quais fazendas vai).",
     "condicoes": ["Aviso, sem obrigar: o pedido é gerado, mas fica marcado \"falta destino final\" até alguém informar.",
                   "Vale para cliente marcado como transportadora/consultor ou com \"transporte\" no nome."],
     "onde": "Pedido no CRM, Gerar pedido, Logística"},

    # ---------------- Logistica
    {"id": "log-ordem", "grupo": "logistica", "tipo": "ajustavel", "nome": "Prioridade da fila da Logística", "lista": "log", "ordem": "ordem_log",
     "descricao": "Cada pedido em aberto fica no primeiro nível desta lista que se aplicar, de cima para baixo. Fora de todos = em dia.",
     "condicoes": [
         "Arraste pela alça (ou use as setas) para mudar a prioridade. Mudar a ordem muda o nível de quem se encaixa em mais de um.",
         "Nível desligado não entra; o pedido cai no próximo que se aplicar. Vendedor respondeu, prazo vencido e sem data limite "
         "ficam sempre ligados (para não esquecer nenhum pedido).",
         "Enquanto espera a resposta do vendedor, o pedido fica fora da fila, a partir da posição de \"Cobrança sem resposta\".",
     ],
     "itens": [
         {"chave": "respondeu", "nome": "nome_log_respondeu", "descricao": "O vendedor respondeu a uma cobrança: ler e atualizar o pedido."},
         {"chave": "vencido", "nome": "nome_log_vencido", "descricao": "O prazo combinado passou e ainda tem saldo."},
         {"chave": "apertado", "nome": "nome_log_apertado", "liga": "liga_log_apertada",
          "descricao": "Precisaria retirar muito por dia até o prazo, ou o prazo está muito perto.",
          "parametros": [_c("Acima de", "retirada_apertada_t_dia", "t/dia"), _c("Ou faltando até", "log_dias_prazo_apertado", "dias")],
          "atencao": "O limite de t/dia é o mesmo da fila do vendedor."},
         {"chave": "sem_resposta", "nome": "nome_log_sem_resposta", "liga": "liga_log_cobranca",
          "descricao": "A Logística cobrou o vendedor e ele não respondeu.",
          "parametros": [_c("Sem resposta há", "log_dias_cobranca", "dias")]},
         {"chave": "parou", "nome": "nome_log_parou", "liga": "liga_log_parou",
          "descricao": "Já retirou, mas parou. Pedido à vista esperando pagamento não entra.",
          "parametros": [_c("Sem retirar há", "log_dias_parou", "dias")]},
         {"chave": "nunca", "nome": "nome_log_nunca", "liga": "liga_log_nunca",
          "descricao": "Nada retirado desde que foi emitido. Pedido à vista esperando pagamento não entra.",
          "parametros": [_c("Pedido há", "log_dias_nunca", "dias")]},
         {"chave": "sem_prazo", "nome": "nome_log_sem_prazo",
          "descricao": "O cliente não informou a data limite ao emitir o pedido: a Logística define."},
     ],
     "onde": "Fila da logística, ficha do pedido, Início do admin"},
    {"id": "log-sem-acao", "grupo": "logistica", "tipo": "ajustavel", "nome": "Pedido sem nenhuma ação",
     "descricao": "Indicador de pedidos esquecidos: sem retirada, anotação nem conversa no período.",
     "parametros": [_c("Período", "log_dias_sem_acao", "dias")], "onde": "Fila da logística, Início do admin"},
    {"id": "log-encerram", "grupo": "logistica", "tipo": "funcionamento", "nome": "Situações que encerram o pedido",
     "descricao": "Estas situações tiram o pedido da lista em aberto (vai para a aba Encerrados).",
     "parametros": [_p("Situações", lambda: ", ".join(_de("models", "SITUACOES_LOGISTICA")()[s] for s in _de("models", "SITUACOES_ENCERRAM")()))],
     "onde": "Logística"},
    {"id": "log-portal-vale", "grupo": "logistica", "tipo": "travada", "nome": "Anotação do portal vale mais que a planilha",
     "descricao": "Depois que a equipe anota um pedido no portal, a importação da planilha não troca mais a situação, o comentário nem a data limite.",
     "condicoes": ["Tudo fica no histórico do pedido, com quem e quando."], "onde": "Logística, ficha do pedido"},

    # ---------------- Local de entrega e rota
    {"id": "local-origem", "grupo": "local", "tipo": "funcionamento", "nome": "De onde vem o local de entrega",
     "descricao": "A planilha só traz o estado. O local vem do cadastro do cliente no CRM.",
     "condicoes": ["Coordenada da fazenda (ponto exato) ou, sem ela, o centro da cidade do cadastro (só se o estado bate com o de entrega).",
                   "Se o nome do pedido casou com o cadastro de uma transportadora pelo nome do dono, o pedido fica sem local (nunca o endereço dela).",
                   "A Logística pode informar ou corrigir na ficha do pedido; a importação não troca mais esse local."],
     "onde": "Logística, Pedidos por região"},
    {"id": "local-plantas", "grupo": "local", "tipo": "ajustavel", "nome": "Rota das plantas",
     "descricao": "A rota de estrada sai sempre das nossas plantas, nunca de onde a pessoa está, e aparece dentro do portal com km e tempo.",
     "condicoes": ["As duas plantas lado a lado; a mais curta aparece primeiro.",
                   "Sem o ponto exato, a rota sai do centro da cidade da planta e a tela avisa \"ponto aproximado\".",
                   "Calculada por serviço gratuito (OpenStreetMap); se falhar, mostra a linha reta e avisa.",
                   "O Google Maps fica só como botão para o motorista navegar."],
     "parametros": [_c("Ponto da planta de São Geraldo do Araguaia/PA", "planta_sao_geraldo"),
                    _c("Ponto da planta de Grajaú/MA", "planta_grajau")],
     "onde": "Ficha do pedido, Pedidos por região, ficha do cliente"},
    {"id": "local-oportunidade", "grupo": "local", "tipo": "funcionamento", "nome": "Oportunidade logística (clientes sem pedido na região)",
     "descricao": "Com o caminhão indo para uma cidade, a página Pedidos por região lista os clientes do cadastro ali que ainda não "
                  "têm pedido em aberto: frete mais barato é chance de venda.",
     "condicoes": ["Entram os clientes do cadastro dentro do raio que ainda não compraram nesta safra. Perdidos entram (o frete pode reconquistar).",
                   "Ficam fora \"Não usará\" e \"Realizado\": quem já comprou e ainda tem o que retirar está na aba Carregar; quem já retirou tudo não tem o que oferecer.",
                   "Ficam fora os que têm pedido em aberto no portal ou na planilha do NetSuite. A ligação é pelo nome e exclui TODOS os "
                   "cadastros parecidos (o CRM tem duplicados); mesmo assim, conferir antes de oferecer.",
                   "Local do cliente: coordenada da fazenda ou, sem ela, o centro da cidade do cadastro.",
                   "A Logística marca os clientes e avisa: o vendedor recebe o aviso, o cliente entra na fila de trabalho dele e o "
                   "envio fica no histórico do cliente (sem contar como contato). Cliente sem vendedor não é avisado."],
     "onde": "Pedidos por região, fila de trabalho e avisos do vendedor, ficha do cliente"},
    {"id": "local-raios", "grupo": "local", "tipo": "ajustavel", "nome": "Raios da busca \"caminhão indo para\"",
     "descricao": "Opções de raio para achar pedidos perto da cidade para onde vai um caminhão (frete retorno).",
     "condicoes": ["Distância em linha reta; a rota mostra a estrada."],
     "parametros": [_c("Raios (separados por vírgula)", "regiao_raios", "km")],
     "onde": "Pedidos por região"},

    # ---------------- Comissao do vendedor (Rafael, 2026-10-04)
    {"id": "com-recebido", "grupo": "comissao", "tipo": "travada", "nome": "Comissão só sobre o que a empresa recebeu",
     "descricao": "O vendedor só fica elegível à comissão quando o cliente paga; sem pagamento, sem comissão.",
     "condicoes": ["À vista: liberada quando o financeiro confirma o comprovante (o total do pedido).",
                   "Carga a carga, a prazo (boleto, por período) e plano safra: liberada a cada pagamento que o financeiro "
                   "registra em Recebimentos, só sobre aquele valor.",
                   "O vendedor recebe um aviso a cada comissão liberada e acompanha tudo em Meu desempenho.",
                   "Pedido cancelado não gera comissão. Recebimento lançado errado pode ser desfeito pelo financeiro.",
                   "Até a integração com o NetSuite, o que vale é o que o financeiro registra no portal."],
     "onde": "Pagamentos a confirmar, Recebimentos, Meu desempenho, relatório Comissões"},
    {"id": "com-calcario", "grupo": "comissao", "tipo": "ajustavel", "nome": "Calcário", "liga": "comissao_calcario_liga",
     "descricao": "Percentual pelo preço por tonelada do pedido.",
     "condicoes": ["Preço igual ao corte já paga o percentual \"a partir do corte\"."],
     "parametros": [_c("Preço de corte", "comissao_calcario_corte", "R$/t"), _c("Abaixo do corte", "comissao_calcario_abaixo", "%"),
                    _c("A partir do corte", "comissao_calcario_acima", "%")],
     "onde": "Gerar pedido, Meu desempenho, relatório Comissões"},
    {"id": "com-gesso", "grupo": "comissao", "tipo": "ajustavel", "nome": "Gesso agrícola", "liga": "comissao_gesso_liga",
     "descricao": "Percentual pelo preço por tonelada do pedido.",
     "condicoes": ["Preço igual ao corte já paga o percentual \"a partir do corte\"."],
     "parametros": [_c("Preço de corte", "comissao_gesso_corte", "R$/t"), _c("Abaixo do corte", "comissao_gesso_abaixo", "%"),
                    _c("A partir do corte", "comissao_gesso_acima", "%")],
     "onde": "Gerar pedido, Meu desempenho, relatório Comissões"},
    {"id": "com-sulfato", "grupo": "comissao", "tipo": "ajustavel", "nome": "Sulfato", "liga": "comissao_sulfato_liga",
     "descricao": "Desligada: sem comissão até definir os valores.",
     "condicoes": ["Preço igual ao corte já paga o percentual \"a partir do corte\"."],
     "parametros": [_c("Preço de corte", "comissao_sulfato_corte", "R$/t"), _c("Abaixo do corte", "comissao_sulfato_abaixo", "%"),
                    _c("A partir do corte", "comissao_sulfato_acima", "%")],
     "onde": "Gerar pedido, Meu desempenho, relatório Comissões"},
    {"id": "com-pedra", "grupo": "comissao", "tipo": "ajustavel", "nome": "Pedra britada", "liga": "comissao_pedra_liga",
     "descricao": "Desligada: sem comissão até definir os valores.",
     "condicoes": ["Preço igual ao corte já paga o percentual \"a partir do corte\"."],
     "parametros": [_c("Preço de corte", "comissao_pedra_corte", "R$/t"), _c("Abaixo do corte", "comissao_pedra_abaixo", "%"),
                    _c("A partir do corte", "comissao_pedra_acima", "%")],
     "onde": "Gerar pedido, Meu desempenho, relatório Comissões"},
    {"id": "com-gravado", "grupo": "comissao", "tipo": "funcionamento", "nome": "O percentual fica gravado no pedido",
     "descricao": "Mudar a regra só vale para as vendas novas: cada pedido guarda o percentual do dia em que foi gerado.",
     "condicoes": ["Renegociar o preço recalcula o percentual com a regra do dia da renegociação.",
                   "Os pedidos que já existiam quando a regra foi criada (04/10/2026) receberam a regra daquele dia.",
                   "Produto desligado: o pedido fica sem comissão, mesmo que a regra seja ligada depois."],
     "onde": "Gerar pedido, renegociar pedido"},

    # ---------------- Planilha
    {"id": "plan-abertos", "grupo": "planilha", "tipo": "funcionamento", "nome": "Quais pedidos estão em aberto",
     "descricao": "Pedido em aberto é o que está nestas abas da planilha de Expedição (alimentada pelo NetSuite).",
     "parametros": [_p("Abas", lambda: ", ".join(_de("models", "ABAS_EM_ABERTO")()))],
     "onde": "Logística, Fila da logística, relatórios"},
    {"id": "plan-somente-leitura", "grupo": "planilha", "tipo": "travada", "nome": "A planilha é só lida",
     "descricao": "O portal lê a planilha de Expedição e nunca escreve nela. A equipe anota no portal.",
     "onde": "Importação da planilha"},

    # ---------------- Gestao
    {"id": "ges-proposta-parada", "grupo": "gestao", "tipo": "ajustavel", "nome": "Propostas paradas (equipe)",
     "descricao": "No quadro da equipe do Início do admin, conta as propostas de cada vendedor sem movimento há algum tempo.",
     "parametros": [_c("Parada há", "admin_proposta_parada_dias", "dias")], "onde": "Início do admin"},
    {"id": "ges-relatorio-tela", "grupo": "gestao", "tipo": "ajustavel", "nome": "Linhas na tela dos relatórios",
     "descricao": "A tela mostra até um limite de linhas para não ficar pesada; o Excel traz todas.",
     "parametros": [_c("Até", "relatorio_limite_tela", "linhas")], "onde": "Relatórios"},

    # ---------------- Acesso
    {"id": "acesso-perfis", "grupo": "acesso", "tipo": "travada", "nome": "Quem vê o quê",
     "descricao": "Cada perfil vê só o que precisa para o seu trabalho.",
     "condicoes": ["Vendedor: só a própria carteira, mais os clientes finais dos pedidos dela (para o pós-venda).",
                   "Logística: Logística, fila, pedidos por região e relatórios.",
                   "Financeiro: pagamentos a confirmar, recebimentos e relatórios. Admin: tudo.",
                   "A carteira é ligada ao vendedor pelo nome, que precisa ser exatamente igual no usuário e no CRM."],
     "onde": "Todo o portal"},
]
POR_ID = {r["id"]: r for r in REGRAS}


def _nome_item(item):
    """Nome do nivel agora (editavel) ou rotulo fixo (criterio do cadastro)."""
    return config.valor(item["nome"]) if item.get("nome") else item["rotulo"]


def chaves_da_regra(r):
    """Chaves de config editaveis numa regra (liga/desliga primeiro)."""
    chaves = ([r["liga"]] if r.get("liga") else []) + [p["chave"] for p in r.get("parametros", []) if p.get("chave")]
    if r.get("lista"):
        chaves.append(r["ordem"])
        for it in r["itens"]:
            chaves += ([it["nome"]] if it.get("nome") else []) + ([it["liga"]] if it.get("liga") else [])
            chaves += [p["chave"] for p in it.get("parametros", [])]
    return list(dict.fromkeys(chaves))


def rotulo_da_chave():
    """chave -> (nome da regra, nome do campo), pro historico."""
    saida = {}
    for r in REGRAS:
        if r.get("liga"):
            saida.setdefault(r["liga"], (r["nome"], "Ligada"))
        for p in r.get("parametros", []):
            if p.get("chave"):
                saida.setdefault(p["chave"], (r["nome"], p["nome"]))
        if r.get("lista"):
            saida.setdefault(r["ordem"], (r["nome"], "Ordem"))
            for it in r["itens"]:
                padrao = config.padrao(it["nome"]) if it.get("nome") else it["rotulo"]
                if it.get("nome"):
                    saida.setdefault(it["nome"], (r["nome"], f"Nome de \"{padrao}\""))
                if it.get("liga"):
                    saida.setdefault(it["liga"], (r["nome"], f"\"{padrao}\" ligado"))
                for p in it.get("parametros", []):
                    saida.setdefault(p["chave"], (r["nome"], f"{padrao}: {p['nome']}"))
    return saida


def nomes_da_ordem(chave_ordem):
    """Pra mostrar uma ordem (lista de chaves) com os nomes de agora."""
    for r in REGRAS:
        if r.get("ordem") == chave_ordem:
            return {it["chave"]: _nome_item(it) for it in r["itens"]}
    return {}


def _no_campo(chave, v):
    """Valor como aparece no campo do formulario."""
    tipo = config.PARAMETROS[chave]["tipo"]
    if tipo == "dia_mes":
        return f"{v[0]}/{v[1]}"
    if tipo in ("lista_int", "ordem"):
        return ",".join(str(x) for x in v) if tipo == "ordem" else ", ".join(str(x) for x in v)
    if tipo == "coord":
        return v or ""
    if tipo == "decimal":
        return config.decimal_br(v)
    return v


def _param_editavel(p):
    ch = p["chave"]
    return dict(p, tipo=config.PARAMETROS[ch]["tipo"], valor=_no_campo(ch, config.valor(ch)),
                padrao=config.texto(ch, config.padrao(ch)), mudou=config.mudou(ch))


def catalogo():
    """Regras prontas pra tela: valores de agora, padrao e se mudou."""
    saida = []
    for r in REGRAS:
        item = dict(r)
        params = []
        for p in r.get("parametros", []):
            if p.get("chave"):
                params.append(_param_editavel(p))
            else:
                v = p["valor"]
                params.append(dict(p, valor=v() if callable(v) else v))
        item["parametros"] = params
        if r.get("liga"):
            item["ligada"] = config.ligada(r["liga"])
        if r.get("lista"):
            por_chave = {it["chave"]: it for it in r["itens"]}
            item["itens"] = [dict(por_chave[k], nome_atual=_nome_item(por_chave[k]),
                                  ligada=config.ligada(por_chave[k]["liga"]) if por_chave[k].get("liga") else True,
                                  parametros=[_param_editavel(p) for p in por_chave[k].get("parametros", [])])
                             for k in config.valor(r["ordem"])]
        item["chaves"] = chaves_da_regra(r)
        item["mudou"] = any(config.mudou(c) for c in item["chaves"])
        item["padroes"] = {c: _no_campo(c, config.padrao(c)) for c in item["chaves"]}
        saida.append(item)
    return saida


# ---------- previa do impacto (antes de salvar)

CHAVES_LOGISTICA = {"ordem_log", "retirada_apertada_t_dia", "liga_log_apertada", "log_dias_prazo_apertado", "liga_log_parou",
                    "log_dias_parou", "liga_log_nunca", "log_dias_nunca", "liga_log_cobranca", "log_dias_cobranca"}
CHAVES_VENDEDOR = {"ordem_fila", "retirada_apertada_t_dia", "liga_fila_apertada", "liga_fila_oportunidade", "oportunidade_dias", "liga_fila_quente", "liga_fila_epoca",
                   "fila_epoca_quente", "fila_epoca_morno", "fila_epoca_frio", "liga_fila_proposta", "fila_proposta_dias",
                   "fila_proposta_dias_perto", "fila_proposta_janela", "liga_fila_sem_contato", "fila_sem_contato_dias",
                   "fila_sem_contato_lote", "liga_fila_primeiro_contato", "fila_primeiro_contato_lote", "ciclo_virada"}
CHAVES_CADASTRO = {"ordem_cad", "liga_cad_sem_telefone", "liga_cad_ja_comprou", "liga_cad_maior_area"}


def _fila_logistica(db):
    from . import expedicao
    itens = expedicao.fila(db)
    return len(itens), Counter(n["chave"] for _, n in itens)


def _filas_vendedores(db):
    """Soma das filas de trabalho de todos os vendedores (cada um com seus lotes)."""
    from .crm_routes import _montar_fila_trabalho
    total = Counter()
    for u in db.query(User).filter(User.role == "vendedor", User.ativo.is_(True), User.vendedor_nome.isnot(None)):
        fila = _montar_fila_trabalho(db, SimpleNamespace(role="vendedor", vendedor_nome=u.vendedor_nome))[0]
        total.update(i["tier"] for i in fila)
    return sum(total.values()), total


def _previa_cadastro(db, novos):
    """A ordem do cadastro nao muda o total, so quem vem primeiro: mostra o
    comeco da fila do vendedor com mais clientes, antes e depois."""
    from .crm_routes import _montar_fila_base, _montar_fila_trabalho
    from .models import ClienteCRM
    from sqlalchemy import func
    maior = (db.query(ClienteCRM.vendedor_nome, func.count()).filter(ClienteCRM.vendedor_nome.isnot(None))
             .group_by(ClienteCRM.vendedor_nome).order_by(func.count().desc()).first())
    if not maior:
        return "Muda só a ordem da fila de cadastro."
    vend = SimpleNamespace(role="vendedor", vendedor_nome=maior[0])
    fila, clientes, *_, ids_leads = _montar_fila_trabalho(db, vend)
    fora = {i["cliente"].id for i in fila} | ids_leads

    def primeiros():
        return ", ".join(i["cliente"].fazenda for i in _montar_fila_base(clientes, fora)[:3]) or "ninguém"
    antes = primeiros()
    with config.simular(novos):
        depois = primeiros()
    if antes == depois:
        return f"Fila de cadastro de {maior[0]}: os primeiros continuam {antes}."
    return f"Fila de cadastro de {maior[0]} (o total não muda), primeiros: {antes} → {depois}."


def _comparar(titulo, unidade, antes, depois, rotulos):
    (n0, c0), (n1, c1) = antes, depois
    if n0 == n1 and c0 == c1:
        return f"{titulo}: não muda ({n0} {unidade})."
    partes = [f"{rotulos.get(k, k)} {c0.get(k, 0)} → {c1.get(k, 0)}" for k in sorted(set(c0) | set(c1), key=str)
              if c0.get(k, 0) != c1.get(k, 0)]
    return f"{titulo}: {n0} → {n1} {unidade}" + (f" ({'; '.join(partes)})" if partes else "") + "."


def previa(db, novos):
    """Frases com o efeito de trocar os valores atuais por `novos`. -> (frases, exige_confirmacao)"""
    from . import expedicao
    from .models import CicloVendas, ClienteCRM, PropostaCRM, STATUS_PROPOSTA_ABERTA, ciclo_rotulo, dias_para_virada_ciclo
    mudam = {c for c, v in novos.items() if v != config.valor(c)}
    if not mudam:
        return ["Nada muda: os valores são os que já valem."], False
    frases, confirmar = [], False
    with config.simular(novos):
        rot_niveis = {k: v[0] for k, v in expedicao.niveis().items()}
        from .crm_routes import TIER_CHAVE, rotulo_tier
        rot_tiers = {t: rotulo_tier(t) for t in TIER_CHAVE}
    if mudam & CHAVES_LOGISTICA:
        antes = _fila_logistica(db)
        with config.simular(novos):
            depois = _fila_logistica(db)
        frases.append(_comparar("Fila da logística", "pedidos", antes, depois, rot_niveis))
    if "log_dias_sem_acao" in mudam:
        a = expedicao.indicadores(db)["sem_acao_30"]
        with config.simular(novos):
            b = expedicao.indicadores(db)["sem_acao_30"]
        frases.append(f"Pedidos sem nenhuma ação: {a} → {b}.")
    if "ciclo_virada" in mudam:
        atual = (db.query(CicloVendas).first() or SimpleNamespace(ciclo_atual=ciclo_rotulo())).ciclo_atual
        with config.simular(novos):
            novo_rotulo = ciclo_rotulo()
            dias = dias_para_virada_ciclo()
        if novo_rotulo != atual:
            n = db.query(ClienteCRM).filter(ClienteCRM.fase.in_(("perdido", "nao_usara", "realizado", "contactado"))).count()
            frases.append(f"Atenção: com essa data o ciclo de hoje passa de {atual} para {novo_rotulo}. A carteira é reiniciada "
                          f"agora: {n} clientes voltam para \"A contactar\".")
            confirmar = True
        else:
            frases.append(f"O ciclo de hoje continua {atual}; a próxima virada fica a {dias} dias.")
    if mudam & CHAVES_VENDEDOR:
        antes = _filas_vendedores(db)
        with config.simular(novos):
            depois = _filas_vendedores(db)
        frases.append(_comparar("Filas de trabalho dos vendedores (somadas)", "clientes", antes, depois, rot_tiers))
    if mudam & {"fila_sem_contato_dias", "fila_sem_contato_lote", "liga_fila_sem_contato"}:
        # O lote esconde o tamanho real: mostra quantos passaram do limite (esperando a vez)
        ativos = [c.dias_desde_ultima_interacao() for c in db.query(ClienteCRM).filter(~ClienteCRM.fase.in_(("perdido", "nao_usara")))]

        def passaram(dias):
            return sum(1 for d in ativos if d is not None and d >= dias)
        lote = novos.get("fila_sem_contato_lote", config.valor("fila_sem_contato_lote"))
        frases.append(f"Clientes que passaram do limite sem contato: {passaram(config.valor('fila_sem_contato_dias'))} → "
                      f"{passaram(novos.get('fila_sem_contato_dias', config.valor('fila_sem_contato_dias')))} "
                      f"(entram na fila {lote} por vez, por vendedor).")
    if mudam & {"categoria_a_ha", "categoria_b_ha"}:
        clientes = db.query(ClienteCRM).filter(ClienteCRM.categoria_dado.is_(None), ClienteCRM.area_plantada_ha.isnot(None)).all()
        a = Counter(c.categoria() for c in clientes)
        with config.simular(novos):
            b = Counter(c.categoria() for c in clientes)
        frases.append("Categoria estimada pela área: " + "; ".join(f"{k} {a.get(k, 0)} → {b.get(k, 0)}" for k in "ABC") + ".")
    if mudam & {"liga_ciclo_contador", "ciclo_contador_dias"}:
        dias = dias_para_virada_ciclo()
        with config.simular(novos):
            aparece = config.ligada("liga_ciclo_contador") and dias <= config.valor("ciclo_contador_dias")
        frases.append(f"Hoje faltam {dias} dias para a virada: o contador {'aparece' if aparece else 'não aparece'} no Início do vendedor.")
    if "admin_proposta_parada_dias" in mudam:
        abertas = db.query(PropostaCRM).filter(PropostaCRM.status == STATUS_PROPOSTA_ABERTA).all()
        agora = dt.datetime.utcnow()

        def paradas(dias):
            return sum(1 for p in abertas if (p.atualizado_em or p.criado_em) and (p.atualizado_em or p.criado_em) < agora - dt.timedelta(days=dias))
        frases.append(f"Propostas paradas (todas as carteiras): {paradas(config.valor('admin_proposta_parada_dias'))} → "
                      f"{paradas(novos['admin_proposta_parada_dias'])}.")
    for chave, cidade, uf in (("planta_sao_geraldo", "São Geraldo do Araguaia", "PA"), ("planta_grajau", "Grajaú", "MA")):
        if chave in mudam:
            from . import geo
            if novos[chave]:
                ponto = tuple(float(x) for x in novos[chave].split(","))
                km = geo.distancia_km(ponto, geo.municipio(uf, cidade)[1])
                frases.append(f"Planta de {cidade}: ponto a {km:.0f} km do centro da cidade"
                              + (" — confira, parece longe demais." if km > 60 else ". As rotas passam a sair daqui."))
            else:
                frases.append(f"Planta de {cidade}: a rota volta a sair do centro da cidade (ponto aproximado).")
    if mudam & CHAVES_CADASTRO:
        frases.append(_previa_cadastro(db, novos))
    if mudam & {"ordem_log", "ordem_fila"}:
        mudou_contagem = any("→" in f for f in frases)
        frases.append("A fila passa a mostrar os grupos nessa ordem."
                      + ("" if mudou_contagem else " Hoje nenhum item se encaixa em mais de um desses níveis, então ninguém muda de grupo;"
                                                   " a nova ordem vale para os próximos."))
    if any(c.startswith(("nome_log_", "nome_fila_")) for c in mudam) and not mudam - {c for c in mudam if c.startswith(("nome_log_", "nome_fila_"))}:
        frases.append("Só muda o nome que aparece nas filas e nos resumos.")
    if mudam & {"liga_contato_automatico", "contato_tentativas"}:
        frases.append("Vale para as próximas tentativas registradas; quem já foi para o admin continua lá.")
    comissao_mudou = sorted({c.split("_")[1] for c in mudam if c.startswith("comissao_")})
    if comissao_mudou:
        from . import comissao
        from .models import PedidoCRM
        nomes = {v: comissao.PRODUTO_NOME[k] for k, v in comissao.PRODUTO_CHAVE.items()}
        for p in comissao_mudou:
            with config.simular(novos):
                if not config.ligada(f"comissao_{p}_liga"):
                    frases.append(f"{nomes[p]}: sem comissão nos pedidos novos.")
                    continue
                corte, abaixo, acima = (config.valor(f"comissao_{p}_{k}") for k in ("corte", "abaixo", "acima"))
            reais = comissao.reais
            frases.append(f"{nomes[p]}: abaixo de {reais(corte)}/t paga {config.decimal_br(abaixo)}%; a partir de "
                          f"{reais(corte)}/t paga {config.decimal_br(acima)}%."
                          + (" Atenção: abaixo do corte está pagando mais que a partir dele." if abaixo > acima else "")
                          + (" Atenção: está ligada com 0%; defina os percentuais." if abaixo == acima == 0 else "")
                          + (" Com corte zero, todo preço paga o percentual \"a partir do corte\"." if corte == 0 else ""))
        n = db.query(PedidoCRM).count()
        frases.append(f"Vale para os pedidos gerados ou renegociados a partir de agora. Os {n} pedidos que já existem "
                      "mantêm o percentual gravado.")
    if mudam & {"regiao_raios", "relatorio_limite_tela"}:
        frases.append("Vale na hora, na próxima vez que a página abrir.")
    return frases, confirmar


def _ler_formulario(regra, form):
    """Valores digitados pra cada chave da regra (checkbox ausente = desligado)."""
    nomes = rotulo_da_chave()
    novos = {}
    for chave in chaves_da_regra(regra):
        novos[chave] = config.ler(chave, form.get(f"v:{chave}", ""), nomes[chave][1] if chave != regra.get("liga") else regra["nome"])
    return novos


# ---------- rotas

@router.get("/admin/regras", response_class=HTMLResponse)
def pagina_regras(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    from .crm_routes import _templates
    regras = catalogo()
    revisoes = {r.regra_id: r for r in db.query(RegraRevisao).all()}
    por_grupo = [(chave, nome, [r for r in regras if r["grupo"] == chave]) for chave, nome in GRUPOS]
    contagem = {t: sum(r["tipo"] == t for r in regras) for t in TIPOS}
    nomes = rotulo_da_chave()
    historico = []
    for h in db.query(ConfigHistorico).order_by(ConfigHistorico.quando.desc(), ConfigHistorico.id.desc()).limit(200):
        regra, campo = nomes.get(h.chave, (h.chave, ""))

        def mostrar(bruto):
            if h.chave not in config.PARAMETROS:
                return bruto
            v = json.loads(bruto)
            if config.PARAMETROS[h.chave]["tipo"] == "ordem":  # ordem com os nomes de agora
                nomes_ordem = nomes_da_ordem(h.chave)
                return " › ".join(nomes_ordem.get(k, k) for k in v)
            return config.texto(h.chave, v)
        historico.append({"quando": h.quando, "quem": h.quem, "regra": regra, "campo": campo, "chave": h.chave,
                          "antes": mostrar(h.antes), "depois": mostrar(h.depois)})
    return _templates(request).TemplateResponse(request, "admin_regras.html", {
        "user": user, "grupos": por_grupo, "tipos": TIPOS, "contagem": contagem, "total": len(regras),
        "revisoes": revisoes, "historico": historico, "mudadas": sum(1 for r in regras if r["mudou"]),
        "migalhas": [("Início", "/admin/inicio"), ("Regras", None)],
    })


@router.post("/admin/regras/{regra_id}/previa")
async def previa_regra(request: Request, regra_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    regra = POR_ID.get(regra_id)
    if regra is None or not chaves_da_regra(regra):
        return JSONResponse({"erro": "Regra sem valores para mudar."}, status_code=400)
    form = await request.form()
    try:
        novos = _ler_formulario(regra, form)
        with config.simular(novos):
            config.validar_conjunto(novos)
    except config.ValorInvalido as e:
        return JSONResponse({"erro": str(e)})
    frases, confirmar = previa(db, novos)
    return JSONResponse({"frases": frases, "confirmar": confirmar})


@router.post("/admin/regras/{regra_id}/salvar")
async def salvar_regra(request: Request, regra_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    regra = POR_ID.get(regra_id)
    if regra is None or not chaves_da_regra(regra):
        return RedirectResponse("/admin/regras", status_code=303)
    form = await request.form()
    try:
        novos = _ler_formulario(regra, form)
        if "ciclo_virada" in novos and novos["ciclo_virada"] != config.valor("ciclo_virada"):
            _, confirmar = previa(db, {"ciclo_virada": novos["ciclo_virada"]})
            if confirmar and form.get("confirmo") != "1":
                raise config.ValorInvalido("Essa data reinicia a carteira agora: marque a confirmação antes de salvar.")
        mudancas = config.salvar(db, user.nome_completo, novos)
    except config.ValorInvalido as e:
        avisar_erro(request, f"Nada foi salvo. {e}")
        return RedirectResponse(f"/admin/regras#r-{regra_id}", status_code=303)
    from . import menu
    menu.limpar_cache_contadores()
    if mudancas:
        avisar_sucesso(request, f"\"{regra['nome']}\" atualizada. Já vale para todos; ficou no histórico.")
    else:
        avisar_sucesso(request, f"Nada mudou em \"{regra['nome']}\".")
    return RedirectResponse(f"/admin/regras#r-{regra_id}", status_code=303)


@router.post("/admin/regras/{regra_id}/revisao")
def revisar_regra(request: Request, regra_id: str, situacao: str = Form(""), comentario: str = Form(""),
                  user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """O admin marca se a descricao confere ou precisa corrigir (etapa 1)."""
    if regra_id not in POR_ID or situacao not in ("ok", "corrigir", "limpar"):
        return RedirectResponse("/admin/regras", status_code=303)
    rev = db.query(RegraRevisao).filter_by(regra_id=regra_id).first()
    if situacao == "limpar":
        if rev:
            db.delete(rev)
    else:
        if rev is None:
            rev = RegraRevisao(regra_id=regra_id)
            db.add(rev)
        rev.situacao = situacao
        rev.comentario = comentario.strip() or None
        rev.quem = user.nome_completo
        rev.quando = dt.datetime.utcnow()
    db.commit()
    nome = POR_ID[regra_id]["nome"]
    avisar_sucesso(request, {"ok": f"\"{nome}\": marcada como confere.", "corrigir": f"\"{nome}\": correção anotada.",
                             "limpar": f"\"{nome}\": revisão desfeita."}[situacao])
    return RedirectResponse(f"/admin/regras#r-{regra_id}", status_code=303)
