"""Inicio do admin (Rafael, 2026-10-03): mesmo conceito do Inicio do vendedor --
o que precisa ser feito, com o botao pra fazer -- mas com o trabalho de quem
gerencia: (1) o que SO o admin resolve, (2) como esta a equipe (quem precisa de
atencao), (3) a Logistica e (4) os numeros de vendas da empresa."""
import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import config, crm_routes, expedicao, financeiro_routes, inicio, presenca
from .auth import require_role
from .database import get_db
from .models import STATUS_PROPOSTA_ABERTA, AreaEstado, ClienteCRM, ContatoCRM, ParceiroCessao, PropostaCRM, User

router = APIRouter()


def _resumo_equipe(equipe):
    """Linha da Equipe fechada (Rafael, 2026-10-04: "esconder todos os nomes, abrir clicando"): so os totais."""
    carteira = sum(v["carteira"] for v in equipe)
    return {"n": len(equipe), "online": sum(1 for v in equipe if v["online"]),
            "cobertura": round(sum(v["tocados"] for v in equipe) / carteira * 100) if carteira else 0,
            "atrasados": sum(v["atrasados"] for v in equipe), "paradas": sum(v["propostas_paradas"] for v in equipe),
            "sem_uso": sum(1 for v in equipe if not v["ultimo"])}


def _equipe(db, hoje):
    """Uma linha por vendedor do portal, quem precisa de atencao primeiro.
    Trabalhados = mesma conta da tela Desempenho (_atividade_no_periodo)."""
    vendedores = (db.query(User).filter(User.role == "vendedor", User.ativo.is_(True), User.vendedor_nome.isnot(None))
                  .order_by(User.nome_completo).all())
    por_vendedor = {}
    for c in db.query(ClienteCRM).all():
        por_vendedor.setdefault(c.vendedor_nome, []).append(c)
    ini = dt.datetime.combine(hoje - dt.timedelta(days=30), dt.time.min)
    fim = dt.datetime.combine(hoje, dt.time.max)
    limite_proposta = dt.datetime.utcnow() - dt.timedelta(days=config.valor("admin_proposta_parada_dias"))
    linhas = []
    for u in vendedores:
        clientes = por_vendedor.get(u.vendedor_nome, [])
        ids = [c.id for c in clientes]
        tocados = crm_routes._atividade_no_periodo(db, ids, ini, fim)[0] if ids else set()
        atrasados = sum(1 for c in clientes if c.proximo_retorno_em and c.proximo_retorno_em < hoje
                        and c.fase not in ("perdido", "nao_usara"))
        paradas = (db.query(PropostaCRM).filter(PropostaCRM.cliente_id.in_(ids), PropostaCRM.status == STATUS_PROPOSTA_ABERTA,
                                                 func.coalesce(PropostaCRM.atualizado_em, PropostaCRM.criado_em) < limite_proposta)
                   .count() if ids else 0)
        ultimo = db.query(func.max(ContatoCRM.data)).filter(ContatoCRM.autor == u.nome_completo).scalar()
        if isinstance(ultimo, str):
            ultimo = dt.datetime.fromisoformat(ultimo)
        base = {"r": "atividade", "periodo": "30d", "vendedor": u.vendedor_nome}
        linhas.append({
            "nome": u.vendedor_nome, "carteira": len(clientes), "tocados": len(tocados),
            "online": presenca.online(u), "presenca": presenca.descricao(u),
            "cobertura": round(len(tocados) / len(clientes) * 100) if clientes else 0,
            "atrasados": atrasados, "propostas_paradas": paradas,
            "ultimo": ultimo, "dias_sem_registro": (hoje - ultimo.date()).days if ultimo else None,
            "link": "/relatorios?" + urlencode(base),
            "link_tocados": "/relatorios?" + urlencode({**base, "ver": "0.2"}) + "#detalhe",
            "link_atrasados": "/relatorios?" + urlencode({**base, "ver": "0.11"}) + "#detalhe",
            "link_propostas": "/relatorios?" + urlencode({"r": "propostas", "vendedor": u.vendedor_nome}),
        })
    # Quem precisa de atencao primeiro: retorno atrasado, depois quem trabalhou menos a carteira
    linhas.sort(key=lambda l: (-l["atrasados"], l["cobertura"], l["nome"]))
    nomes_usuarios = {u.vendedor_nome for u in vendedores}
    sem_dono = {nome or "Sem vendedor": len(cs) for nome, cs in por_vendedor.items() if nome not in nomes_usuarios}
    return linhas, sem_dono


@router.get("/admin/inicio", response_class=HTMLResponse)
def admin_inicio(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    hoje = dt.date.today()
    equipe, sem_dono = _equipe(db, hoje)
    contatos = (db.query(ClienteCRM).filter(ClienteCRM.precisa_ajuda.is_(True), ClienteCRM.fase.notin_(("perdido", "nao_usara")))
                .count())
    parceiros = (db.query(ParceiroCessao).filter(ParceiroCessao.ativo.is_(True),
                                                  or_(ParceiroCessao.cnpj.is_(None), ParceiroCessao.cnpj == "")).count())
    n_sem_dono = sum(sem_dono.values())
    # Tarefas do topo (Rafael, 2026-10-04: "nao estao como prioridade, apenas mais uma informacao"): cada uma com o
    # verbo da acao, o que ela trava e a ordem de urgencia ("peso"). nivel "trava" = para a operacao (vermelho);
    # "atencao" = alguem esta parado (ambar). Tarefa nova: e so acrescentar aqui.
    # Pagamento a vista e PRIORIDADE MAXIMA (Rafael, 2026-10-04): cartao vermelho proprio acima das tarefas
    urgente = inicio.cartao_conferir(inicio.pagamentos_em_conferencia(db))
    pendencias = [
        {"peso": 2, "nivel": "atencao", "acao": "Resolver contatos", "acao_um": "Resolver contato", "botao": "Resolver agora", "icone": "phone-off",
         "n": contatos, "link": "/vendedor/crm/fila", "efeito": "Vendedor sem contato", "desde": None,
         "texto": "Número errado, sem WhatsApp ou cliente que não retorna: consiga outro contato para o vendedor.",
         "rotulo": "Contatos a resolver"},
        {"peso": 3, "nivel": "trava", "acao": "Completar CNPJ", "acao_um": "Completar CNPJ", "botao": "Completar agora", "icone": "handshake",
         "n": parceiros, "link": "/crm/parceiros", "efeito": "Trava contrato novo", "desde": None,
         "texto": "Parceiro de cessão sem CNPJ não pode entrar em contrato novo.", "rotulo": "Parceiros sem CNPJ"},
        {"peso": 4, "nivel": "atencao", "acao": "Ver carteiras sem dono", "acao_um": "Ver carteira sem dono", "botao": "Ver agora", "icone": "user-x",
         "n": n_sem_dono, "link": "/relatorios?r=funil", "efeito": "Clientes que ninguém atende", "desde": None,
         "texto": "Clientes cujo vendedor não tem usuário no portal (ninguém vê a carteira): "
                  + ", ".join(f"{k} ({v})" for k, v in sorted(sem_dono.items(), key=lambda x: -x[1])) + ".",
         "rotulo": "Clientes sem vendedor"},
    ]
    from .models import PedidoSenha
    pedidos_senha = db.query(PedidoSenha).filter(PedidoSenha.atendido_em.is_(None)).count()
    pendencias.append({"peso": 2.5, "nivel": "trava", "acao": "Redefinir senhas", "acao_um": "Redefinir senha", "botao": "Redefinir agora",
                       "icone": "key-round", "n": pedidos_senha, "link": "/admin/usuarios#pedidos-senha",
                       "efeito": "Pessoa sem acesso ao portal", "desde": None,
                       "texto": "Pessoa sem e-mail cadastrado pediu senha nova: gere uma temporária em Pessoas e acessos.",
                       "rotulo": "Pedidos de senha"})
    tarefas = sorted((p for p in pendencias if p["n"]), key=lambda p: p["peso"])
    em_dia = [p["rotulo"] for p in pendencias if not p["n"]]
    ind = expedicao.indicadores(db)
    logistica = {"vencidos": ind["por_nivel"]["vencido"], "apertados": ind["por_nivel"]["apertado"],
                 "parados": ind["por_nivel"]["parou"] + ind["por_nivel"]["nunca"], "sem_prazo": ind["sem_prazo"],
                 "na_fila": ind["na_fila"], "carga_dia": ind["carga_dia"], "sem_acao_30": ind["sem_acao_30"],
                 "abertos": ind["em_aberto"], "nome_parou": config.valor("nome_log_parou"), "nome_nunca": config.valor("nome_log_nunca")}
    titulo, data_extenso = inicio.saudacao(user)
    return crm_routes._templates(request).TemplateResponse(request, "admin_inicio.html", {
        "user": user, "saudacao": titulo, "data_extenso": data_extenso,
        "tarefas": tarefas, "em_dia": em_dia, "urgente": urgente,
        "equipe": equipe, "resumo_equipe": _resumo_equipe(equipe), "logistica": logistica, "dias_proposta": config.valor("admin_proposta_parada_dias"),
        "kpis": crm_routes._indicadores_home(db, db.query(ClienteCRM).all(), hoje), "vol": expedicao.volumes(db, hoje),

        "migalhas": [("Início", None)],
    })


@router.post("/admin/area-estados/atualizar")
def atualizar_area_estados(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Busca no IBGE a area plantada dos estados do ano mais recente (PAM). O IBGE
    publica o ano anterior em set/out: 1 clique por ano."""
    from fastapi.responses import RedirectResponse
    from . import ibge_area
    from .feedback import avisar_erro, avisar_sucesso
    voltar = RedirectResponse("/relatorios?r=market-share&periodo=tudo", status_code=303)
    antes = {e.uf: (e.area_agropecuaria_ha, e.fonte) for e in db.query(AreaEstado).all()}
    try:
        ano, mudancas = ibge_area.atualizar(db)
    except Exception as e:  # rede, IBGE fora do ar, formato novo
        avisar_erro(request, f"Não consegui buscar no IBGE agora ({type(e).__name__}). Nada mudou; tente mais tarde.")
        return voltar
    if all(antes.get(uf) == (depois, ibge_area.fonte(ano)) for uf, _, depois in mudancas):
        avisar_sucesso(request, f"Já está com o dado mais recente do IBGE (PAM {ano}).")
    else:
        avisar_sucesso(request, f"Área plantada dos estados atualizada: IBGE, PAM {ano}.")
    return voltar


@router.get("/api/equipe-online")
def equipe_online(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Bolinhas da equipe no Inicio, atualizadas a cada minuto sem recarregar a pagina."""
    agora = dt.datetime.utcnow()
    return {u.vendedor_nome: {"online": presenca.online(u, agora), "titulo": presenca.descricao(u, agora)}
            for u in db.query(User).filter(User.role == "vendedor", User.ativo.is_(True), User.vendedor_nome.isnot(None))}
