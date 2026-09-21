"""Cliente minimo para consultar o NetSuite via SuiteQL (REST), usando
OAuth 1.0 Token-Based Authentication (TBA).

As credenciais nunca ficam no codigo: vem do arquivo .env (veja .env.example),
que fica so na maquina de quem roda o portal e nunca deve ser commitado
ou colado em chat.
"""
import os

import requests
from dotenv import load_dotenv
from requests_oauthlib import OAuth1

load_dotenv()

ACCOUNT_ID = os.environ.get("NETSUITE_ACCOUNT_ID", "")


class NetSuiteConfigError(RuntimeError):
    pass


def _host():
    if not ACCOUNT_ID:
        raise NetSuiteConfigError("NETSUITE_ACCOUNT_ID nao configurado no .env")
    return ACCOUNT_ID.lower().replace("_", "-") + ".suitetalk.api.netsuite.com"


def _auth():
    consumer_key = os.environ.get("NETSUITE_CONSUMER_KEY")
    consumer_secret = os.environ.get("NETSUITE_CONSUMER_SECRET")
    token_id = os.environ.get("NETSUITE_TOKEN_ID")
    token_secret = os.environ.get("NETSUITE_TOKEN_SECRET")
    if not all([consumer_key, consumer_secret, token_id, token_secret]):
        raise NetSuiteConfigError("Credenciais do NetSuite incompletas no .env")
    return OAuth1(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=token_id,
        resource_owner_secret=token_secret,
        signature_method="HMAC-SHA256",
        realm=ACCOUNT_ID,
    )


def testar_conexao():
    """Faz uma consulta SuiteQL minima so para validar as credenciais."""
    url = f"https://{_host()}/services/rest/query/v1/suiteql"
    resp = requests.post(
        url,
        auth=_auth(),
        headers={"Content-Type": "application/json", "Prefer": "transient"},
        json={"q": "SELECT id, companyname FROM customer FETCH FIRST 5 ROWS ONLY"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def buscar_pedidos_abertos():
    """Exemplo de consulta de pedidos de venda em aberto via SuiteQL.

    Os nomes de campo/tabela padrao do NetSuite estao aqui, mas o saved
    search original ('Pedidos') pode ter filtros ou campos customizados
    (subsidiaria, status traduzido, etc.) que precisam ser conferidos e
    ajustados com quem administra o NetSuite antes de usar em producao.
    """
    url = f"https://{_host()}/services/rest/query/v1/suiteql"
    query = """
        SELECT
            t.tranid AS pedido,
            t.trandate AS data_pedido,
            t.status AS status,
            c.companyname AS cliente,
            t.foreigntotal AS valor_total
        FROM transaction t
        JOIN customer c ON c.id = t.entity
        WHERE t.type = 'SalesOrd'
          AND t.status NOT IN ('SalesOrd:F', 'SalesOrd:G')
        ORDER BY t.trandate DESC
        FETCH FIRST 50 ROWS ONLY
    """
    resp = requests.post(
        url,
        auth=_auth(),
        headers={"Content-Type": "application/json", "Prefer": "transient"},
        json={"q": query},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    import json
    print("Testando conexao com NetSuite...")
    try:
        resultado = testar_conexao()
        print("Conexao OK. Resposta:")
        print(json.dumps(resultado, indent=2, ensure_ascii=False))
    except NetSuiteConfigError as e:
        print(f"Configuracao incompleta: {e}")
        print("Copie .env.example para .env e preencha as credenciais do sandbox.")
    except requests.HTTPError as e:
        print(f"Erro HTTP {e.response.status_code}: {e.response.text}")
