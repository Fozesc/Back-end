"""
Previa do contrato antes de efetivar (versao 2.14.1), POST /api/operations/previa com o mesmo corpo do Efetivar:
  - devolve o contrato e o borderô da operação com o numero que o borderô vai ganhar;
  - nao grava nada (borderô, titulos, caixa, contrato, auditoria) e nao gasta o numero: o Efetivar ganha o mesmo;
  - o contrato e o borderô da operação da previa sao IGUAIS aos gravados depois do Efetivar;
  - erro de dado volta 400 sem gravar.
Banco SEPARADO (fozesc_teste_previa_contrato), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_previa_contrato.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_previa_contrato'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
D = lambda n: (HOJE + timedelta(days=n)).isoformat()
_apps = []
_db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def main():
    global _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Contrato, Operation, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _apps.append(app)
        _db = db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Operador Teste', email='op@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Operador', active=True))
            db.session.add(CompanySettings(company_name='Fozesc ESC', cnpj='11.444.777/0001-61', address='Av. Exemplo, 1',
                                           phone='(45) 3333-0000', email='contato@exemplo.com', esc=True,
                                           contrato_juros_mora=1.5, iof_rate=0.38, iof_daily_rate=0.0082))
            db.session.add(Client(name='Alfa Teste Ltda', document='11.222.333/0001-81', address='Rua B, 2', city='Foz', state='PR'))
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'op@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def contagem():
            with app.app_context():
                return (Operation.query.count(), Check.query.count(), Transaction.query.count(),
                        Contrato.query.count(), AuditLog.query.count())

        corpo = lambda **extra: {
            'client_id': 1, 'operation_date': D(0), 'taxa_mensal': 5, 'dias_compensacao': 1, 'account_source': 'BB',
            'comissao': 1, 'empresa': 'Fozesc', 'iof_enabled': True, 'iof_base': 0.38, 'iof_diario': 0.0082,
            'checks': [{'valor': 1500, 'vencimento': D(30), 'tipo': 'PROMISSORIA', 'num_doc': '1/2'},
                       {'valor': 2500, 'vencimento': D(60), 'tipo': 'PROMISSORIA', 'num_doc': '2/2'}], **extra}

        # um borderô antes, para o numero nao ser 1 por acaso
        assert http.post('/api/operations', headers=cab, json=corpo(empresa='PQ')).status_code == 201
        antes = contagem()

        r = http.post('/api/operations/previa', headers=cab, json=corpo())
        assert r.status_code == 200, r.data
        previa = r.get_json()
        assert previa['dados']['numero'] == '2' and previa['bordero']['id'] == 2 and previa['bordero']['empresa'] == 'Fozesc', previa['bordero']
        assert previa['operacao']['operador'] == 'Operador Teste' and previa['operacao']['qtd_titulos'] == 2
        assert previa['dados']['encargos']['jurosMora'] == '1,50%' and previa['dados']['contratada']['esc'] is True
        assert contagem() == antes, 'a previa nao grava nada'
        assert http.post('/api/operations/previa', headers=cab, json=corpo()).get_json()['dados']['numero'] == '2', \
            'previa de novo: mesmo numero (a sequencia nao andou)'

        r = http.post('/api/operations/previa', headers=cab, json=corpo(client_id=999))
        assert r.status_code == 400 and 'Cliente' in r.get_json()['error'], r.data
        r = http.post('/api/operations/previa', headers=cab, json=corpo(checks=[{'valor': 100, 'vencimento': D(30), 'juros': 99, 'liquido': 1}]))
        assert r.status_code == 400 and 'não confere' in r.get_json()['error'], r.data
        assert contagem() == antes

        # efetivar: o mesmo numero e o mesmo contrato / borderô da operação
        r = http.post('/api/operations', headers=cab, json=corpo())
        assert r.status_code == 201 and r.get_json()['id'] == 2 and r.get_json()['contrato_gerado'] is True, r.data
        gravado = http.get('/api/contratos/2', headers=cab).get_json()
        assert gravado['dados'] == previa['dados'], 'contrato da previa = contrato gravado'
        assert gravado['operacao'] == previa['operacao'], (gravado['operacao'], previa['operacao'])
        assert gravado['bordero'] == previa['bordero']

        print("OK: previa do contrato antes de efetivar - mesmo numero sem gastar a sequencia, nada gravado, "
              "contrato e borderô da operação iguais aos gravados depois, erro de dado 400.")
    finally:
        for a in _apps:
            with a.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
