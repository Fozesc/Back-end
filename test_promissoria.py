"""
Borderô de promissorias (versao 2.13): o titulo grava o tipo que a tela mandou.

  - tipo PROMISSORIA grava type=PROMISSORIA e o numero da parcela; a lista de titulos devolve tipo PROMISSORIA;
  - sem tipo continua CHEQUE (tela antiga); tipo desconhecido e' recusado sem gravar nada;
  - a conta (juros, liquido, caixa) e' a mesma do cheque;
  - Ajustes guardam a 2a empresa (PQ): nome, CNPJ e endereco;
  - /operations/<id>/documento devolve o borderô como no dia (empresa, avalista, valores de origem).

Banco SEPARADO (fozesc_teste_promissoria), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_promissoria.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_promissoria'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
D = lambda n: (HOJE + timedelta(days=n)).isoformat()
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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Operation, Transaction, User
        from app.services.operation_service import calcular_linha
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Ana')
            db.session.add(cli)
            db.session.commit()
            id_cli = cli.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def bordero(tipo):
            return http.post('/api/operations', headers=cab, json={
                'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
                'account_source': 'Dinheiro', 'iof_enabled': False,
                'checks': [{'valor': 1000, 'vencimento': D(30 * i), 'num_doc': f'{i}/2', 'emitente': 'Ana',
                            **({'tipo': tipo} if tipo is not None else {})} for i in (1, 2)]})

        with app.app_context():
            antes = (Operation.query.count(), Check.query.count(), Transaction.query.count())
        for ruim in ('BOLETO', ['PROMISSORIA'], 'promissoria'):
            r = bordero(ruim)
            assert r.status_code == 400 and 'tipo inválido' in r.get_json()['error'], (ruim, r.data)
        with app.app_context():
            assert (Operation.query.count(), Check.query.count(), Transaction.query.count()) == antes, 'nada gravado'

        r = bordero('PROMISSORIA')
        assert r.status_code == 201, r.data
        op = r.get_json()['id']
        r2 = bordero(None)
        assert r2.status_code == 201, r2.data
        with app.app_context():
            proms = Check.query.filter_by(operation_id=op).order_by(Check.id).all()
            assert [(c.type, c.number, c.bank) for c in proms] == [('PROMISSORIA', '1/2', ''), ('PROMISSORIA', '2/2', '')]
            esperado = [round(calcular_linha(1000, 30 * i, 4)[2], 2) for i in (1, 2)]
            assert [c.net_amount for c in proms] == esperado, 'mesma conta do cheque'
            assert {c.type for c in Check.query.filter_by(operation_id=r2.get_json()['id'])} == {'CHEQUE'}, 'sem tipo = cheque'
            assert 'Promissória 1/2' in AuditLog.query.filter_by(target='Borderô').order_by(AuditLog.id.desc()).offset(1).first().description
        lista = http.get('/api/checks', headers=cab, query_string={'per_page': 50}).get_json()
        itens = lista.get('items', lista) if isinstance(lista, dict) else lista
        assert sorted(i['tipo'] for i in itens) == ['CHEQUE', 'CHEQUE', 'PROMISSORIA', 'PROMISSORIA'], itens

        # as duas empresas do topo do borderô: a PQ tem nome, CNPJ e endereco proprios nos Ajustes
        r = http.put('/api/settings/', headers=cab, json={'pqNome': '  PQ  Fomento ', 'pqCnpj': '11.222.333/0001-81',
                                                          'pqEndereco': 'Rua B, 20 - Foz'})
        assert r.status_code == 200, r.data
        g = http.get('/api/settings/', headers=cab).get_json()
        assert (g['pqNome'], g['pqCnpj'], g['pqEndereco'], g['nomeEmpresa']) == \
            ('PQ Fomento', '11.222.333/0001-81', 'Rua B, 20 - Foz', 'Fozesc Teste'), g
        assert http.put('/api/settings/', headers=cab, json={'pqCnpj': '1' * 25}).status_code == 400, \
            'CNPJ com digito errado e recusado'
        with app.app_context():
            assert any('Nome PQ: None -> PQ Fomento' in a.description for a in AuditLog.query.filter_by(target='Configuracao'))

        # ------------------------------------------------ documento do borderô (baixar de novo depois)
        r = http.post('/api/operations', headers=cab, json={
            'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'Dinheiro', 'iof_enabled': False, 'empresa': 'Itau', 'checks': [
                {'valor': 500, 'vencimento': D(30), 'tipo': 'PROMISSORIA'}]})
        assert r.status_code == 400 and 'Empresa inválida' in r.get_json()['error']
        r = http.post('/api/operations', headers=cab, json={
            'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'BB', 'iof_enabled': False, 'empresa': 'PQ', 'notes': 'obs do dia',
            'avalista_nome': ' José  Avalista ', 'avalista_documento': '987.654.321-00',
            'checks': [{'valor': 1000, 'vencimento': D(30 * i), 'num_doc': f'{i}/2', 'emitente': 'Ana', 'tipo': 'PROMISSORIA'}
                       for i in (1, 2)]})
        assert r.status_code == 201, r.data
        op_pq = r.get_json()['id']
        with app.app_context():
            c = Check.query.filter_by(operation_id=op_pq).order_by(Check.id).first()
            c.original_amount, c.amount, c.original_due_date, c.due_date = 1000, 400, c.due_date, c.due_date + timedelta(days=15)
            cli = db.session.get(Client, id_cli)
            cli.document, cli.address, cli.city, cli.state = '111.222.333-44', 'Rua A, 1', 'Foz', 'PR'
            db.session.commit()
        d = http.get(f'/api/operations/{op_pq}/documento', headers=cab).get_json()
        assert d['empresa'] == {'chave': 'PQ', 'nome': 'PQ Fomento', 'cnpj': '11.222.333/0001-81', 'endereco': 'Rua B, 20 - Foz'}, d['empresa']
        assert d['avalista'] == {'nome': 'José Avalista', 'documento': '987.654.321-00'}
        assert d['cliente'] == {'nome': 'Ana', 'documento': '111.222.333-44', 'telefone': '', 'endereco': 'Rua A, 1 - Foz/PR'}
        assert [(t['tipo'], t['numero'], t['valor'], t['vencimento']) for t in d['titulos']] == \
            [('PROMISSORIA', '1/2', 1000, D(30)), ('PROMISSORIA', '2/2', 1000, D(60))], 'valor e vencimento de origem'
        assert d['conta_saida'] == 'BB' and d['observacao'] == 'obs do dia' and d['liquido_total'] == round(
            sum(t['liquido'] for t in d['titulos']), 2)
        antigo = http.get(f'/api/operations/{op}/documento', headers=cab).get_json()
        assert antigo['empresa']['chave'] == 'PQ' and antigo['empresa']['nome'] == 'PQ Fomento', 'borderô sem empresa = PQ (padrao)'
        assert http.get('/api/operations/9999/documento', headers=cab).status_code == 404
        assert http.get(f'/api/operations/{op_pq}/documento').status_code == 401

        print("OK: documento do borderô para baixar de novo (empresa e avalista gravados, cliente, valor/vencimento "
              "de origem, sem empresa = PQ, empresa invalida recusada), Ajustes com a 2a empresa (PQ: nome, CNPJ, endereco, auditado), borderô de promissorias grava o tipo e o numero da parcela (sem banco), mesma conta do cheque, "
              "sem tipo continua cheque, tipo desconhecido recusado sem gravar, lista de titulos mostra o tipo.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
