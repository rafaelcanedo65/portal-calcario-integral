"""Envio de e-mail do portal (recuperar senha). Rafael (2026-10-05) escolheu "link por e-mail".

Configuracao por variaveis de ambiente (arquivo .env do servidor, nunca no codigo):
  SMTP_HOST, SMTP_PORT (587), SMTP_USUARIO, SMTP_SENHA, SMTP_REMETENTE, SMTP_SSL ("1" = SSL direto, porta 465)
  PORTAL_URL: endereco do portal usado no link (ex.: https://portal.integral.com.br)

Sem SMTP_HOST o portal nao envia: grava o e-mail em emails_teste/ (modo de teste, so pra conferir) e a pagina
Pessoas e acessos avisa o admin."""
import datetime as dt
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
PASTA_TESTE = Path(__file__).resolve().parent.parent / "emails_teste"


def configurado():
    return bool(os.environ.get("SMTP_HOST"))


def url_portal():
    return os.environ.get("PORTAL_URL", "http://localhost:8422").rstrip("/")


def enviar(para, assunto, texto):
    """-> True se saiu (ou foi gravado no modo de teste). Erro de envio sobe como excecao."""
    if not configurado():
        PASTA_TESTE.mkdir(exist_ok=True)
        with open(PASTA_TESTE / "emails.log", "a", encoding="utf-8") as f:
            f.write(f"--- {dt.datetime.now():%d/%m/%Y %H:%M:%S}\nPara: {para}\nAssunto: {assunto}\n\n{texto}\n\n")
        return True
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_REMETENTE") or os.environ.get("SMTP_USUARIO")
    msg["To"] = para
    msg["Subject"] = assunto
    msg.set_content(texto)
    host, porta = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    if os.environ.get("SMTP_SSL") == "1":
        conexao = smtplib.SMTP_SSL(host, porta, timeout=20)
    else:
        conexao = smtplib.SMTP(host, porta, timeout=20)
        conexao.starttls()
    with conexao:
        if os.environ.get("SMTP_USUARIO"):
            conexao.login(os.environ["SMTP_USUARIO"], os.environ.get("SMTP_SENHA", ""))
        conexao.send_message(msg)
    return True
