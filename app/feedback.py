"""Confirmacao depois de salvar (Rafael, 2026-10-02: "va para onde ficou salvo
... leve para o proximo passo"). A acao guarda a mensagem na sessao e a
proxima pagina mostra uma vez (base.html), com link pro proximo passo quando
houver. O redirect leva junto uma ancora (#proposta-12, #pedido-3...) e a
pagina rola ate o item salvo e destaca ele."""


def avisar_sucesso(request, texto, link=None, rotulo=None):
    request.session["aviso_tela"] = {"texto": texto, "link": link, "rotulo": rotulo}


def avisar_erro(request, texto):
    """Mesmo aviso, com cara de problema: nada foi salvo e por que."""
    request.session["aviso_tela"] = {"texto": texto, "link": None, "rotulo": None, "erro": True}
