"""
Apagar o borderô inteiro e apagar cliente sem borderô; filtro de conta no caixa e no historico:
  - a previa do borderô (GET /operations/<id>/exclusao) junta as mudancas de todos os titulos e nao grava nada;
  - DELETE /operations/<id> pede a senha e deixa o caixa como antes do borderô (titulos, contrato e recebimentos saem);
  - cliente com borderô nao pode ser apagado; sem borderô sai com a senha (notas juntas, evento fica sem o cliente);
  - ?conta=DINHEIRO|BRASIL|CAIXA filtra a lista do caixa (com os totais) e os lancamentos do historico;
  - so o Admin apaga borderô, cliente, titulo, lancamento do caixa, vale e junta cadastros (Operador: 403, nada muda).
Banco SEPARADO (fozesc_teste_apagar_bordero), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_apagar_bordero_cliente.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_apagar_bordero'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
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
        from app.models.domain import (AuditLog, Check, Client, ClientNote, CompanySettings, Contrato, Evento,
                                       Operation, Transaction, User)
        from werkzeug.security import generate_password_hash

        app = create_app()
        _apps.append(app)
        _db = db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(User(name='Operador', email='op@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Operador', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli, vazio = Client(name='Lucas'), Client(name='Repetido')
            db.session.add_all([cli, vazio])
            db.session.commit()
            id_cli, id_vazio = cli.id, vazio.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        saldos = lambda: http.get('/api/transactions/balances', headers=cab).get_json()['bruto']
        assert http.post('/api/transactions', headers=cab, json={
            'type': 'entrada', 'amount': 50000, 'origin': 'BB', 'description': 'aporte'}).status_code in (200, 201)
        assert http.post('/api/transactions', headers=cab, json={
            'type': 'entrada', 'amount': 700, 'origin': 'Dinheiro', 'description': 'troco'}).status_code in (200, 201)
        antes = saldos()

        r = http.post('/api/operations', headers=cab, json={
            'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'BB', 'comissao': 2, 'empresa': 'Fozesc',
            'checks': [{'valor': 1000, 'vencimento': (HOJE + timedelta(days=30)).isoformat(), 'num_doc': '1'},
                       {'valor': 2000, 'vencimento': (HOJE + timedelta(days=60)).isoformat(), 'num_doc': '2'},
                       {'valor': 3000, 'vencimento': (HOJE + timedelta(days=90)).isoformat(), 'num_doc': '3'}]})
        assert r.status_code == 201, r.data
        op_id = r.get_json()['id']
        with app.app_context():
            ids = [c.id for c in Check.query.filter_by(operation_id=op_id).order_by(Check.id)]
            assert Contrato.query.filter_by(operation_id=op_id).count() == 1
        assert http.patch(f'/api/checks/{ids[0]}/status', headers=cab,
                          json={'status': 'Pago', 'payment_data': {'method': 'Dinheiro'}}).status_code == 200
        depois = saldos()
        assert depois != antes

        # ------------------------------------------------ filtro de conta (caixa e historico)
        lista = lambda conta: http.get('/api/transactions', headers=cab, query_string={'conta': conta, 'per_page': 100}).get_json()
        dinheiro, bb = lista('DINHEIRO'), lista('BRASIL')
        assert {i['origem'] if 'origem' in i else i.get('origin') for i in dinheiro['items']} <= {'Dinheiro', 'Sistema (Dinheiro)'}, dinheiro['items']
        assert dinheiro['summary']['entradas'] == 1700.0, dinheiro['summary']
        assert bb['summary']['entradas'] == 50000.0 and bb['summary']['saidas'] > 0, bb['summary']
        assert lista('CAIXA')['total'] == 0
        assert lista('')['total'] == dinheiro['total'] + bb['total']
        h = lambda conta: http.get(f'/api/history/month/{HOJE.year}/{HOJE.month}', headers=cab,
                                   query_string={'conta': conta}).get_json()
        assert h('DINHEIRO')['lancamentos_total'] == 2 and h('CAIXA')['lancamentos_total'] == 0
        assert h('')['lancamentos_total'] == h('DINHEIRO')['lancamentos_total'] + h('BRASIL')['lancamentos_total']

        # ------------------------------------------------ Operador nao apaga nada disso (com a senha certa)
        r = http.post('/api/auth/login', json={'email': 'op@fozesc.com', 'password': SENHA})
        op_cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        with app.app_context():
            tx = Transaction.query.filter_by(operation_id=op_id).first().id
        for url in (f'/api/operations/{op_id}', f'/api/checks/{ids[1]}', f'/api/clients/{id_vazio}',
                    f'/api/transactions/{tx}', '/api/vales/1'):
            r = http.delete(url, headers=op_cab, json={'senha': SENHA})
            assert r.status_code == 403 and 'administrador' in r.get_json()['error'], (url, r.data)
        r = http.post('/api/clients/merge', headers=op_cab, json={'origem_id': id_vazio, 'destino_id': id_cli, 'senha': SENHA})
        assert r.status_code == 403, r.data
        assert saldos() == depois
        with app.app_context():
            assert db.session.get(Operation, op_id) and db.session.get(Client, id_vazio) and db.session.get(Transaction, tx)

        # ------------------------------------------------ previa do borderô inteiro: mostra e nao grava
        r = http.get(f'/api/operations/{op_id}/exclusao', headers=cab)
        assert r.status_code == 200, r.data
        previa = r.get_json()
        assert previa['titulos'] == 3 and previa['bordero_apagado'] is True, previa
        assert all(c['para'] is None for c in previa['caixa']), 'todas as linhas do borderô saem'
        assert len({(c['descricao'], c['data']) for c in previa['caixa']}) == len(previa['caixa']), 'cada linha uma vez so'
        assert saldos() == depois, 'a previa nao pode mudar o caixa'

        # ------------------------------------------------ senha
        assert http.delete(f'/api/operations/{op_id}', headers=cab, json={'senha': 'errada'}).status_code == 403
        assert saldos() == depois
        r = http.delete(f'/api/operations/{op_id}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json() == previa, (r.get_json(), previa)
        assert saldos() == antes, (saldos(), antes)
        with app.app_context():
            assert db.session.get(Operation, op_id) is None
            assert Check.query.filter_by(operation_id=op_id).count() == 0
            assert Transaction.query.filter_by(operation_id=op_id).count() == 0
            assert Contrato.query.filter_by(operation_id=op_id).count() == 0
            logs = [l.description for l in AuditLog.query.filter_by(target='Borderô')]
            assert any('Senha incorreta' in d for d in logs) and any('[confirmado com senha]' in d for d in logs), logs
        assert http.get(f'/api/operations/{op_id}/exclusao', headers=cab).status_code == 404

        # ------------------------------------------------ cliente: com borderô nao apaga
        r = http.post('/api/operations', headers=cab, json={
            'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'BB', 'checks': [{'valor': 500, 'vencimento': (HOJE + timedelta(days=30)).isoformat()}]})
        assert r.status_code == 201
        r = http.delete(f'/api/clients/{id_cli}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 400 and 'borderô' in r.get_json()['error'], r.data

        with app.app_context():
            db.session.add(ClientNote(client_id=id_vazio, autor='Teste', texto='cadastro repetido'))
            db.session.add(Evento(data=HOJE, titulo='ligar', autor='Teste', client_id=id_vazio))
            db.session.commit()
        assert http.delete(f'/api/clients/{id_vazio}', headers=cab, json={'senha': 'errada'}).status_code == 403
        r = http.delete(f'/api/clients/{id_vazio}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        assert http.get(f'/api/clients/{id_vazio}', headers=cab).status_code == 404
        assert http.delete(f'/api/clients/{id_vazio}', headers=cab, json={'senha': SENHA}).status_code == 404
        with app.app_context():
            assert ClientNote.query.filter_by(client_id=id_vazio).count() == 0
            ev = Evento.query.filter_by(titulo='ligar').one()
            assert ev.client_id is None
            logs = [l.description for l in AuditLog.query.filter_by(target='Cliente')]
            assert any("nome='Repetido'" in d for d in logs) and any('Senha incorreta' in d for d in logs), logs

        print("OK: borderô inteiro sai com senha (previa igual, caixa volta, contrato junto), cliente com borderô "
              "nao sai e sem borderô sai com senha, filtro de conta no caixa e no historico.")
    finally:
        for a in _apps:
            with a.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
