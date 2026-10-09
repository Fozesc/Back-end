"""
Calendario (cheques por vencimento, eventos/notas/lembretes, filtro por cliente) e a
previsao do caixa no metodo do sistema financeiro: saldo do caixa + a receber - a pagar
ate a data; vencido fica a parte. Tambem a regra do caixa: lancamento com data futura
entra na conta (o saldo e' tudo que foi lancado) e nao e' somado de novo na previsao.
Banco SEPARADO (fozesc_teste_calendario), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_calendario.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_calendario'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = _db = None
HOJE = date.today()
D = lambda n: (HOJE + timedelta(days=n)).isoformat()


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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Evento, Operation, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            a, b = Client(name='Lucas'), Client(name='Maria')
            db.session.add_all([a, b])
            db.session.flush()
            op_a = Operation(client_id=a.id, operation_date=HOJE - timedelta(days=30), monthly_rate=4)
            op_b = Operation(client_id=b.id, operation_date=HOJE - timedelta(days=30), monthly_rate=4)
            db.session.add_all([op_a, op_b])
            db.session.flush()

            def ch(op, num, dias, valor, status='Aguardando', fora=False):
                db.session.add(Check(operation_id=op.id, number=num, due_date=HOJE + timedelta(days=dias),
                                     amount=valor, status=status, issuer_name=f'Emitente {num}', fora_do_calculo=fora))
            ch(op_a, 'A1', 5, 1000)
            ch(op_a, 'A2', -3, 300)                      # vencido
            ch(op_b, 'B1', 40, 500)
            ch(op_b, 'B2', 10, 800, status='Pago')       # pago: nao aparece
            ch(op_b, 'B3', 7, 900, fora=True)            # fora do calculo: nao aparece
            db.session.add(Transaction(date=HOJE - timedelta(days=10), description='aporte', amount=2000,
                                       type='entrada', origin='Dinheiro', category='Aporte'))
            db.session.add(Transaction(date=HOJE + timedelta(days=2), description='agendado', amount=-400,
                                       type='saida', origin='Dinheiro', category='Geral'))   # gravado com data futura
            db.session.commit()
            id_a, id_b = a.id, b.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        # ------------------------------- caixa: data futura entra na conta como qualquer outra
        saldo = lambda: http.get('/api/transactions/balances', headers=cab).get_json()['bruto']['dinheiro_total']
        assert saldo() == 1600, 'o lancado com data futura (-400) ja conta no saldo'
        r = http.post('/api/transactions', headers=cab, json={'valor': 10, 'tipo': 'entrada', 'origem': 'Dinheiro', 'data': D(3)})
        assert r.status_code == 201 and saldo() == 1610, r.data
        assert http.post('/api/transactions', headers=cab, json={'valor': 10, 'data': 'amanha'}).status_code == 400
        assert http.delete(f"/api/transactions/{r.get_json()['id']}", headers=cab, json={'senha': SENHA}).status_code == 200

        # ------------------------------------------------------------------ eventos
        E = '/api/calendario/eventos'
        assert http.post(E, json={'titulo': 'x', 'data': D(1)}).status_code == 401
        for ruim in ({'data': D(1)}, {'titulo': 'x', 'data': 'amanha'}, {'titulo': 'x', 'data': D(1), 'tipo': 'festa'},
                     {'titulo': 'x', 'data': D(1), 'valor': 10}, {'titulo': 'x', 'data': D(1), 'valor': 0, 'previsao': 'saida'},
                     {'titulo': 'x', 'data': D(1), 'cliente_id': 99999}):
            assert http.post(E, headers=cab, json=ruim).status_code == 400, ruim
        novo = lambda **kw: http.post(E, headers=cab, json=kw).get_json()
        e1 = novo(titulo='Pagar aluguel', data=D(15), valor=700, previsao='saida')
        e2 = novo(titulo='Ligar para o Lucas', data=D(5), tipo='nota', cliente_id=id_a, descricao='combinar a troca')
        e3 = novo(titulo='Receber acordo', data=D(50), tipo='lembrete', valor=200, previsao='entrada')
        e4 = novo(titulo='Já pago', data=D(20), valor=999, previsao='saida', concluido=True)
        e5 = novo(titulo='Visita Maria', data=D(8), cliente_id=id_b)
        assert e2['autor'] == 'Teste' and e2['cliente'] == 'Lucas' and e1['valor'] == 700

        # --------------------------------------------- previsao (metodo do financeiro)
        p = http.get('/api/calendario/previsao', headers=cab).get_json()
        assert p['saldo'] == 1600 and (p['vencido'], p['qtd_vencidos']) == (300, 1), p
        assert [(h['dias'], h['a_receber'], h['a_pagar'], h['saldo_previsto']) for h in p['horizontes']] == [
            (30, 1000, 700, 1900), (60, 1700, 700, 2600), (90, 1700, 700, 2600)], p['horizontes']

        # --------------------------------------------------------------- o mes
        C = '/api/calendario'
        assert http.get(C, headers=cab, query_string={'inicio': D(0), 'fim': D(70)}).status_code == 400, 'teto do periodo'
        m = http.get(C, headers=cab, query_string={'inicio': D(-5), 'fim': D(50)}).get_json()
        assert [(c['numero'], c['status']) for c in m['cheques']] == [('A2', 'Atrasado'), ('A1', 'Aguardando'), ('B1', 'Aguardando')], m['cheques']
        assert {e['titulo'] for e in m['eventos']} == {'Pagar aluguel', 'Ligar para o Lucas', 'Receber acordo', 'Já pago', 'Visita Maria'}
        sp = m['saldo_previsto']
        assert D(-1) not in sp, 'dia que ja passou nao tem previsao'
        assert (sp[D(0)], sp[D(2)], sp[D(5)], sp[D(15)], sp[D(40)], sp[D(50)]) == (1600, 1600, 2600, 1900, 2400, 2600), sp
        so_lucas = http.get(C, headers=cab, query_string={'inicio': D(-5), 'fim': D(50), 'cliente_id': id_a}).get_json()
        assert [c['numero'] for c in so_lucas['cheques']] == ['A2', 'A1'] and [e['titulo'] for e in so_lucas['eventos']] == ['Ligar para o Lucas']

        # --------------------------------------------------- editar, concluir e apagar
        r = http.put(f"{E}/{e1['id']}", headers=cab, json={**e1, 'concluido': True})
        assert r.status_code == 200 and r.get_json()['concluido'] is True
        p = http.get('/api/calendario/previsao', headers=cab).get_json()
        assert p['horizontes'][0]['a_pagar'] == 0, 'evento concluido sai da previsao'
        assert http.put(f'{E}/99999', headers=cab, json=e1).status_code == 404
        assert http.delete(f"{E}/{e3['id']}", headers=cab).status_code == 204
        assert http.delete(f"{E}/{e3['id']}", headers=cab).status_code == 404

        # juntar cadastros leva os eventos
        r = http.post('/api/clients/merge', headers=cab, json={'origem_id': id_b, 'destino_id': id_a, 'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert db.session.get(Evento, e5['id']).client_id == id_a
            acoes = sorted(l.action for l in AuditLog.query.filter_by(target='Calendario'))
            assert acoes == ['CREATE'] * 5 + ['DELETE', 'UPDATE'], acoes

        print("OK: calendario com cheques em aberto por vencimento (pago e fora do calculo fora, vencido marcado), "
              "eventos/notas/lembretes validados, filtro por cliente, previsao 30/60/90 = saldo do caixa + a receber "
              "- a pagar (vencido a parte, concluido fora), saldo previsto dia a dia, e data futura no saldo sem contar 2 vezes.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
