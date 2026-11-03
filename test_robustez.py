"""
Robustez da versao 2.11.3: dado ruim na tela nao pode quebrar o sistema nem sujar o banco.
Cada bloco abaixo FALHAVA na 2.11.2:

  1. JSON com NaN/Infinity ou mal formado: 400 com mensagem, nunca NaN gravado nem pagina HTML;
     erro inesperado: 500 com {"error"} generico (sem detalhe interno) e nada gravado.
  2. Ajustes (taxas/IOF/multa/empresa) e capital/saldos iniciais: so o Admin, e com cada valor
     conferido (texto, negativo, fora da faixa, vazio -> 400 e nada muda).
  3. Fluxo de Caixa funciona sem a linha de configuracoes (banco novo / restaurado).
  4. Cadastro de cliente: nome obrigatorio, limite/taxa conferidos, CPF/CNPJ repetido avisado.
  5. Editar lancamento do caixa: valor/tipo/conta/data pedem a senha (descricao nao).
  6. Datas: ano fora de 2000-2100 recusado (caixa, borderô, calendario); grafico de lucro ignora
     data absurda que ja esteja no banco.
  7. Rota de exportacao esquecida (/api/reports/export) nao existe mais.
  8. Nota de cliente: so quem escreveu ou o Admin edita/apaga.
  9. Login errado com texto que nao e' e-mail (senha digitada no campo errado) nao vai para a auditoria.
 10. Confirmacoes por senha: 10 erros no minuto (somando as telas) travam com 429, ate a senha certa.

Banco SEPARADO (fozesc_teste_robustez), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_robustez.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_robustez'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
_app = _db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Client, ClientNote, CompanySettings, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            for nome, email, cargo in (('Chefe', 'chefe@fozesc.com', 'Admin'), ('Op', 'op@fozesc.com', 'Operador')):
                db.session.add(User(name=nome, email=email, password_hash=generate_password_hash(SENHA), role=cargo))
            db.session.commit()
            assert app.config['JWT_ACCESS_TOKEN_EXPIRES'] == timedelta(hours=15)

        def entrar(email):
            r = http.post('/api/auth/login', json={'email': email, 'password': SENHA})
            return {'Authorization': 'Bearer ' + r.get_json()['token']}
        admin, op = entrar('chefe@fozesc.com'), entrar('op@fozesc.com')
        json_cru = lambda cab, metodo, url, corpo: getattr(http, metodo)(
            url, headers={**cab, 'Content-Type': 'application/json'}, data=corpo)

        # ---------------------------------------------- 3. caixa sem configuracoes
        r = http.get('/api/transactions/balances', headers=op)
        assert r.status_code == 200 and r.get_json()['capital_total'] == 0, r.data

        # ---------------------------------------------- 1. NaN / Infinity / JSON quebrado / erro inesperado
        for corpo in ('{"capital_social": NaN}', '{"capital_social": Infinity}', '{"capital_social": -Infinity}'):
            r = json_cru(admin, 'put', '/api/transactions/initial-balances', corpo)
            assert r.status_code == 400 and r.is_json, (corpo, r.status_code, r.data)
        r = json_cru(admin, 'put', '/api/settings/', '{"taxaPadrao": 4,,}')
        assert r.status_code == 400 and r.is_json and 'error' in r.get_json(), r.data
        r = json_cru(op, 'post', '/api/clients', '{"name": "X", "credit_limit": NaN}')
        assert r.status_code == 400, 'NaN no limite tem que ser recusado'
        with app.app_context():
            assert not Client.query.filter_by(name='X').first()
        from app.services import history_service

        def explode(*a, **k):
            raise RuntimeError('detalhe interno que nao pode vazar: senha=xyz')
        original = history_service.HistoryService.get_available_months
        history_service.HistoryService.get_available_months = explode
        try:
            r = http.get('/api/history/months', headers=op)
        finally:
            history_service.HistoryService.get_available_months = original
        assert r.status_code == 500 and r.is_json and 'xyz' not in r.get_data(as_text=True), r.data
        assert r.get_json()['error'].startswith('Erro interno'), r.data

        # ---------------------------------------------- 2. Ajustes e saldos: so Admin, valores conferidos
        S = '/api/settings/'
        assert http.put(S, headers=op, json={'taxaPadrao': 9}).status_code == 403
        assert http.put('/api/transactions/initial-balances', headers=op, json={'capital_social': 1}).status_code == 403
        for ruim in ({'taxaPadrao': 'abc'}, {'taxaPadrao': -1}, {'taxaPadrao': 101}, {'iof_rate': 11},
                     {'diasCompensacaoPadrao': 31}, {'fine_rate': ''}, {'nomeEmpresa': '   '},
                     {'taxaPadrao': 5, 'iof_rate': 'x'}):
            r = http.put(S, headers=admin, json=ruim)
            assert r.status_code == 400 and r.get_json()['error'], (ruim, r.data)
        with app.app_context():
            s = CompanySettings.query.first()
            assert s is None or s.default_monthly_rate != 5, 'um campo ruim nao deixa o resto meio gravado'
        r = http.put(S, headers=admin, json={'taxaPadrao': '4,5', 'diasCompensacaoPadrao': 3, 'iof_rate': 0.38,
                                              'nomeEmpresa': 'Fozesc', 'cnpj': '1' * 40})
        assert r.status_code == 200, r.data
        assert (r.get_json()['taxaPadrao'], r.get_json()['diasCompensacaoPadrao'], len(r.get_json()['cnpj'])) == (4.5, 3, 20)
        for ruim in ({'capital_social': 'abc'}, {'capital_social': -5}, {'saldo_inicial_bb': '1.234,56'}):
            assert http.put('/api/transactions/initial-balances', headers=admin, json=ruim).status_code == 400, ruim
        r = http.put('/api/transactions/initial-balances', headers=admin, json={'capital_social': 1000, 'saldo_inicial_bb': -50})
        assert r.status_code == 200, r.data
        assert http.get('/api/transactions/balances', headers=op).get_json()['capital_total'] == 1000

        # ---------------------------------------------- 4. cadastro de cliente
        C = '/api/clients'
        for ruim, trecho in (({}, 'nome'), ({'name': '  '}, 'nome'), ({'name': 'Ana', 'standard_rate': 150}, 'Taxa'),
                             ({'name': 'Ana', 'credit_limit': 'muito'}, 'Limite'), ({'name': 'Ana', 'credit_limit': -1}, 'Limite')):
            r = http.post(C, headers=op, json=ruim)
            assert r.status_code == 400 and trecho in r.get_json()['error'], (ruim, r.data)
        r = http.post(C, headers=op, json={'name': 'Ana Souza', 'document': '123.456.789-00', 'credit_limit': '', 'standard_rate': ''})
        assert r.status_code == 201, r.data
        ana = r.get_json()['id']
        r = http.post(C, headers=op, json={'name': 'Ana S.', 'document': '12345678900'})
        assert r.status_code == 400 and 'Ana Souza' in r.get_json()['error'], 'mesmo CPF escrito diferente'
        r = http.post(C, headers=op, json={'name': 'Bia', 'document': '98765432100'})
        bia = r.get_json()['id']
        assert http.put(f'{C}/{ana}', headers=op, json={'name': 'Ana Souza', 'document': '123.456.789-00', 'phone': '45 9'}).status_code == 200
        assert http.put(f'{C}/{bia}', headers=op, json={'document': '123.456.789-00'}).status_code == 400
        assert http.put(f'{C}/99999', headers=op, json={'name': 'x'}).status_code == 404
        with app.app_context():
            a = db.session.get(Client, ana)
            assert (a.credit_limit, a.standard_rate, a.phone) == (0, 4.0, '45 9')

        # ---------------------------------------------- 5. editar o caixa
        T = '/api/transactions'
        r = http.post(T, headers=op, json={'valor': 100, 'tipo': 'entrada', 'origem': 'Dinheiro', 'descricao': 'aporte'})
        tid = r.get_json()['id']
        r = http.put(f'{T}/{tid}', headers=op, json={'descricao': 'aporte do sócio', 'valor': 100, 'tipo': 'entrada'})
        assert r.status_code == 200 and r.get_json()['descricao'] == 'aporte do sócio', 'so a descricao nao pede senha'
        for mudanca in ({'valor': 0.01}, {'tipo': 'saida'}, {'origem': 'BB'}, {'data': (HOJE - timedelta(days=3)).isoformat()}):
            r = http.put(f'{T}/{tid}', headers=op, json={'valor': 100, 'tipo': 'entrada', **mudanca})
            assert r.status_code == 403, (mudanca, r.data)
        with app.app_context():
            t = db.session.get(Transaction, tid)
            assert (t.amount, t.type, t.origin, t.date) == (100, 'entrada', 'Dinheiro', HOJE), 'senha errada nao muda nada'
            assert AuditLog.query.filter_by(action='NEGADO', target='FluxoCaixa').count() == 4
        r = http.put(f'{T}/{tid}', headers=op, json={'valor': 80, 'tipo': 'entrada', 'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['valor'] == 80

        # ---------------------------------------------- 6. datas
        assert http.post(T, headers=op, json={'valor': 1, 'tipo': 'entrada', 'data': '1902-05-01'}).status_code == 400
        r = http.post('/api/calendario/eventos', headers=op, json={'titulo': 'x', 'data': '2206-01-01'})
        assert r.status_code == 400 and '2206' in r.get_json()['error']
        r = http.post('/api/operations', headers=op, json={
            'client_id': ana, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'Dinheiro', 'checks': [{'valor': 100, 'vencimento': '2206-01-01'}]})
        assert r.status_code == 400 and 'vencimento' in r.get_json()['error'], r.data
        with app.app_context():
            db.session.add(Transaction(date=date(1902, 9, 1), description='Juros de prorrogação - digitado errado',
                                       amount=10, type='entrada', origin='Dinheiro', category='Multas e Juros'))
            db.session.commit()
        g = http.get('/api/history/lucro', headers=op).get_json()
        assert all(m['year'] >= 2000 for m in g) and len(g) < 1200, 'data absurda ja gravada nao vira seculos no grafico'

        # ---------------------------------------------- 7. rota de exportacao removida
        r = http.post('/api/reports/export', headers=admin, json={'type': 'geral'})
        assert r.status_code == 404 and r.is_json

        # ---------------------------------------------- 8. notas do cliente
        N = f'{C}/{ana}/notas'
        nota_op = http.post(N, headers=op, json={'texto': 'nota da operadora'}).get_json()['id']
        nota_adm = http.post(N, headers=admin, json={'texto': 'nota do chefe'}).get_json()['id']
        r = http.put(f'{N}/{nota_adm}', headers=op, json={'texto': 'mexi'})
        assert r.status_code == 403 and 'Chefe' in r.get_json()['error']
        assert http.delete(f'{N}/{nota_adm}', headers=op).status_code == 403
        assert http.put(f'{N}/{nota_op}', headers=op, json={'texto': 'corrigida'}).status_code == 200, 'a autora edita'
        assert http.put(f'{N}/{nota_op}', headers=admin, json={'texto': 'revisada'}).status_code == 200, 'o Admin edita'
        assert http.delete(f'{N}/{nota_op}', headers=admin).status_code == 204
        with app.app_context():
            assert db.session.get(ClientNote, nota_adm).texto == 'nota do chefe'

        # ---------------------------------------------- 9. login errado nao grava senha
        http.post('/api/auth/login', json={'email': 'MinhaSenha@123', 'password': 'x'})
        http.post('/api/auth/login', json={'email': 'naoexiste@fozesc.com', 'password': 'x'})
        with app.app_context():
            descs = [a.description for a in AuditLog.query.filter_by(action='NEGADO', target='Login')]
            assert len(descs) == 2 and not any('MinhaSenha' in d for d in descs), descs
            assert any('naoexiste@fozesc.com' in d for d in descs)

        # ---------------------------------------------- 10. senha errada nas confirmacoes trava (somando as telas)
        erros = 0
        tentativas = [('delete', f'{T}/{tid}', {'senha': 'errada'}),
                      ('put', f'{T}/{tid}', {'valor': 1, 'tipo': 'entrada', 'senha': 'errada'})]
        # as 4 senhas erradas do bloco 5 ja contaram
        while True:
            metodo, url, corpo = tentativas[erros % 2]
            r = getattr(http, metodo)(url, headers=op, json=corpo)
            if r.status_code == 429:
                break
            assert r.status_code == 403, r.data
            erros += 1
            assert erros < 20, 'nao travou'
        assert erros + 4 == 10, f'travou depois de {erros + 4} erros (esperado 10)'
        assert 'senhas erradas' in r.get_json()['error'], r.data
        r = http.put(f'{T}/{tid}', headers=op, json={'valor': 70, 'tipo': 'entrada', 'senha': SENHA})
        assert r.status_code == 429, 'travado vale ate para a senha certa'

        print("OK: NaN/Infinity/JSON quebrado recusados com 400 e erro inesperado vira 500 generico, Ajustes e saldos "
              "so do Admin e conferidos, caixa sem configuracoes, cadastro de cliente conferido (CPF repetido), editar "
              "o caixa pede senha, datas fora de 2000-2100 recusadas (e o grafico ignora as ja gravadas), exportacao "
              "removida, nota so da autora ou do Admin, senha no campo de e-mail nao vai para a auditoria e senha "
              "errada nas confirmacoes trava apos 10.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
