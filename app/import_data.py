"""Importa pedidos de uma planilha exportada do NetSuite (mesmo formato da
aba 'Pedidos' de CALCARIO 2026) para o banco do portal, e cria os usuarios
iniciais (admin, logistica, um por vendedor encontrado na planilha).

Uso:
    python -m app.import_data "C:\\Claude\\CALCARIO 2026 (1).xlsx"
"""
import datetime as dt
import re
import sys
import unicodedata

import openpyxl

from .auth import hash_password
from .database import Base, SessionLocal, engine
from .models import Pedido, User

COL = {"subsidiaria": 2, "pedido": 3, "data": 4, "cliente": 5, "status": 6,
       "quant_total": 7, "faturado": 8, "saldo": 9, "valor_unitario": 10, "vendedor": 11}


def slugify(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    nome = re.sub(r"[^a-zA-Z0-9]+", ".", nome.strip().lower()).strip(".")
    return nome or "usuario"


def parse_data(valor):
    if not valor:
        return None
    if isinstance(valor, dt.datetime):
        return valor.date()
    if isinstance(valor, dt.date):
        return valor
    try:
        return dt.datetime.strptime(str(valor).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def to_float(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def importar(caminho_xlsx: str):
    Base.metadata.create_all(bind=engine)
    wb = openpyxl.load_workbook(caminho_xlsx, data_only=True)
    ws = wb["Pedidos"]

    db = SessionLocal()
    vendedores_encontrados = set()
    importados = 0
    try:
        for row in ws.iter_rows(min_row=2, values_only=False):
            pedido_num = row[COL["pedido"] - 1].value
            if not pedido_num or "/SO" not in str(pedido_num):
                continue

            vendedor = row[COL["vendedor"] - 1].value
            if vendedor:
                vendedores_encontrados.add(str(vendedor).strip())

            existente = db.query(Pedido).filter_by(numero_pedido=str(pedido_num)).first()
            alvo = existente or Pedido(numero_pedido=str(pedido_num))

            novo_faturado = to_float(row[COL["faturado"] - 1].value)
            if existente and novo_faturado != existente.faturado:
                alvo.faturado_atualizado_em = dt.datetime.utcnow()

            alvo.subsidiaria = row[COL["subsidiaria"] - 1].value
            alvo.data_pedido = parse_data(row[COL["data"] - 1].value)
            alvo.cliente = str(row[COL["cliente"] - 1].value or "").strip()
            alvo.status = row[COL["status"] - 1].value
            alvo.quant_total = to_float(row[COL["quant_total"] - 1].value)
            alvo.faturado = novo_faturado
            alvo.saldo = to_float(row[COL["saldo"] - 1].value)
            alvo.valor_unitario = to_float(row[COL["valor_unitario"] - 1].value)
            alvo.vendedor = str(vendedor).strip() if vendedor else None

            if not existente:
                db.add(alvo)
            importados += 1

        db.commit()

        criados = []

        if not db.query(User).filter_by(username="admin").first():
            senha = "troque-esta-senha"
            db.add(User(username="admin", password_hash=hash_password(senha),
                         nome_completo="Administrador", role="admin"))
            criados.append(("admin", senha, "admin"))

        if not db.query(User).filter_by(username="logistica").first():
            senha = "troque-esta-senha"
            db.add(User(username="logistica", password_hash=hash_password(senha),
                         nome_completo="Logistica", role="logistica"))
            criados.append(("logistica", senha, "logistica"))

        # Confere comprovante de pedido a vista e libera o carregamento (2026-10-02)
        if not db.query(User).filter_by(username="financeiro").first():
            senha = "troque-esta-senha"
            db.add(User(username="financeiro", password_hash=hash_password(senha),
                         nome_completo="Financeiro", role="financeiro"))
            criados.append(("financeiro", senha, "financeiro"))

        for nome_vendedor in sorted(vendedores_encontrados):
            username = slugify(nome_vendedor)
            if db.query(User).filter_by(username=username).first():
                continue
            senha = "troque-esta-senha"
            db.add(User(username=username, password_hash=hash_password(senha),
                         nome_completo=nome_vendedor, role="vendedor", vendedor_nome=nome_vendedor))
            criados.append((username, senha, f"vendedor ({nome_vendedor})"))

        db.commit()

        print(f"Pedidos importados/atualizados: {importados}")
        if criados:
            print("\nUsuarios criados (troque a senha no primeiro acesso):")
            for username, senha, papel in criados:
                print(f"  usuario={username:20s} senha={senha:20s} papel={papel}")
        else:
            print("Nenhum usuario novo criado (ja existiam).")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python -m app.import_data <caminho_para_planilha.xlsx>")
        sys.exit(1)
    importar(sys.argv[1])
