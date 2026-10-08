"""
O juros e o liquido gravados em cada cheque sao EXATAMENTE os da tela do Borderô:

  1. borderô criado pela tela grava centavo por centavo o que a tela mostrou (com e
     sem IOF), e caixa, total de juros e IOF do borderô sao a soma desses mesmos numeros;
  2. numero adulterado (juros que nao confere com a conta) e' recusado sem gravar nada;
  3. tela antiga (sem mandar os centavos) grava a mesma conta, calculada no servidor;
  4. alinhar_juros.py corrige borderô antigo que confere com o caixa e nao mexe no que
     nao confere.

A conta "da tela" vem do proprio Front-end/src/utils/calculoBordero.js (via node).
Roda em banco SEPARADO (fozesc_teste_bordero), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_bordero_juros.py
"""
import json
import os
import re
import subprocess
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_bordero'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
UTIL = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Front-end', 'src', 'utils', 'calculoBordero.js'))
VENCS = ['2026-10-29', '2026-11-30', '2026-12-29', '2027-01-29', '2027-03-01',
         '2027-03-29', '2027-04-29', '2027-05-31', '2027-06-29', '2027-07-29']
_app = _db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def tela(valor, data_op, vencs, taxa, comp, iof):
    """As linhas do borderô como a tela calcula (calcularDias + calcularLinha)."""
    codigo = (f"import('{UTIL}').then(m => console.log(JSON.stringify({json.dumps(vencs)}.map(v => {{"
              f" const dias = m.calcularDias('{data_op}', v, {comp});"
              f" return {{ vencimento: v, valor: {valor}, dias, ...m.calcularLinha({{ valor: {valor}, dias,"
              f" taxaMensal: {taxa}, iofEnabled: {str(iof).lower()}, iofBase: 0.38, iofDiario: 0.0082 }}) }}; }}))))")
    return json.loads(subprocess.run(['node', '-e', codigo], capture_output=True, text=True, check=True).stdout)


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import Check, Client, CompanySettings, Operation, Transaction, User
        from werkzeug.security import generate_password_hash
        import alinhar_juros

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(CompanySettings(company_name='Teste', iof_rate=0.38, iof_daily_rate=0.0082))
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            cli = Client(name='Cliente Teste', standard_rate=4.0)
            db.session.add(cli)
            db.session.commit()
            cli_id = cli.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def payload(linhas, iof, com_centavos=True):
            return {'client_id': cli_id, 'operation_date': '2026-09-29', 'taxa_mensal': 4, 'dias_compensacao': 2,
                    'account_source': 'Dinheiro', 'iof_amount': round(sum(l['iof'] for l in linhas), 2),
                    'total_net': round(sum(l['liquido'] for l in linhas), 2),
                    'iof_enabled': iof, 'iof_base': 0.38, 'iof_diario': 0.0082,
                    'checks': [{'valor': l['valor'], 'vencimento': l['vencimento'], 'banco': '', 'num_doc': f'{i}/10',
                                'emitente': 'Emitente', **({'juros': l['juros'], 'iof': l['iof'], 'liquido': l['liquido']}
                                                           if com_centavos else {})}
                               for i, l in enumerate(linhas, 1)]}

        def gravado(op_id):
            with app.app_context():
                op = db.session.get(Operation, op_id)
                chs = Check.query.filter_by(operation_id=op_id).order_by(Check.id).all()
                tx = Transaction.query.filter_by(operation_id=op_id, category='Compra de Ativos').one()
                return op, [(c.interest_amount, c.net_amount, c.days) for c in chs], tx.amount

        # ------------------------------ 1. com e sem IOF: centavo por centavo da tela
        for iof in (False, True):
            linhas = tela(1375.82, '2026-09-29', VENCS, 4, 2, iof)
            r = http.post('/api/operations', headers=cab, json=payload(linhas, iof))
            assert r.status_code == 201, r.data
            op, chs, caixa = gravado(r.get_json()['id'])
            assert chs == [(l['juros'], l['liquido'], l['dias']) for l in linhas], (chs, linhas)
            assert op.total_interest == round(sum(l['juros'] for l in linhas), 2)
            assert op.iof_amount == round(sum(l['iof'] for l in linhas), 2)
            assert op.total_net_value == round(sum(l['liquido'] for l in linhas), 2) == -caixa
            if not iof:
                assert op.total_interest == 3515.17 and op.total_net_value == 10243.03, op.total_interest

        # --------------------------------------- 2. adulterado: recusa e nao grava nada
        with app.app_context():
            antes = (Operation.query.count(), Check.query.count(), Transaction.query.count())
        corpo = payload(linhas, True)
        corpo['checks'][4]['juros'] = round(corpo['checks'][4]['juros'] - 0.05, 2)
        corpo['checks'][4]['liquido'] = round(corpo['checks'][4]['liquido'] + 0.05, 2)
        r = http.post('/api/operations', headers=cab, json=corpo)
        assert r.status_code == 400 and 'não confere' in r.get_json()['error'], r.data
        with app.app_context():
            assert (Operation.query.count(), Check.query.count(), Transaction.query.count()) == antes

        # ------------------- 3. tela antiga (sem os centavos): servidor faz a mesma conta
        r = http.post('/api/operations', headers=cab, json=payload(linhas, True, com_centavos=False))
        assert r.status_code == 201, r.data
        _, chs, _ = gravado(r.get_json()['id'])
        assert chs == [(l['juros'], l['liquido'], l['dias']) for l in linhas]

        # ---- 5. comissao: parte dos juros pela conta da tela; nao mexe em juros/liquido/caixa
        linhas = tela(1375.82, '2026-09-29', VENCS, 4, 2, False)
        codigo = (f"import('{UTIL}').then(m => console.log(JSON.stringify(m.calcularComissao("
                  f"{{ juros: {sum(l['juros'] for l in linhas)}, comissao: 1, taxaMensal: 4 }}))))")
        da_tela = json.loads(subprocess.run(['node', '-e', codigo], capture_output=True, text=True, check=True).stdout)
        r = http.post('/api/operations', headers=cab, json={**payload(linhas, False), 'comissao': 1})
        assert r.status_code == 201, r.data
        assert r.get_json()['comissao_valor'] == da_tela['valor'] == 878.79, (r.get_json()['comissao_valor'], da_tela)
        op, chs, caixa = gravado(r.get_json()['id'])
        assert op.comissao == 1 and chs == [(l['juros'], l['liquido'], l['dias']) for l in linhas]
        assert op.total_interest == 3515.17 and op.total_net_value == 10243.03 == -caixa, 'comissao mexeu no borderô'
        # no caixa: saida do borderô + comissao (sai hoje) + juros sem a comissao (so informativo), ligadas
        with app.app_context():
            ls = Transaction.query.filter_by(operation_id=op.id).order_by(Transaction.id).all()
            # 1a linha (so informativa) = total que sai do banco; as de baixo: cliente + comissao
            assert [(t.category, t.type, t.amount, t.valor_informativo) for t in ls] == [
                ('Informativo', 'saida', 0.0, 11121.82),
                ('Compra de Ativos', 'saida', -10243.03, None),
                ('Comissão', 'saida', -878.79, None)], [(t.category, t.amount, t.valor_informativo) for t in ls]
            assert ls[0].valor_informativo == round(-ls[1].amount - ls[2].amount, 2), 'total = cliente + comissao'
            assert {t.grupo_id for t in ls} == {ls[0].id}, 'as 3 linhas ligadas pelo id da primeira'
            assert ls[0].description.startswith('Pgto Borderô #') and ls[1].description.startswith('Cliente recebe - Borderô #')
            assert len({t.origin for t in ls}) == 1 and ls[2].description.startswith('Comissão (25% dos juros) - Borderô #')
        saldo_dinheiro = http.get('/api/transactions/balances', headers=cab).get_json()['bruto']['dinheiro_total']
        lista = http.get('/api/transactions?per_page=3', headers=cab).get_json()
        assert {l['grupo_id'] for l in lista['items']} == {ls[0].id}
        assert [l['valor_informativo'] for l in lista['items'] if l['valor_informativo'] is not None] == [11121.82]
        assert round(lista['summary']['saidas'], 2) == 11121.82, 'o total informativo nao soma de novo no caixa'
        assert http.put(f"/api/transactions/{ls[0].id}", headers=cab, json={'valor': 10}).status_code == 400, \
            'linha informativa nao se edita'
        with app.app_context():
            from app.models.domain import AuditLog
            primeiro = Check.query.filter_by(operation_id=op.id).order_by(Check.id).first().id
            assert 'Comissão: 1 de 4 pontos da taxa (25.0% dos juros) = R$ 878.79' in \
                AuditLog.query.filter_by(target='Borderô').order_by(AuditLog.id.desc()).first().description
        b = http.get(f'/api/checks/{primeiro}', headers=cab).get_json()['bordero']
        assert (b['comissao'], b['comissao_valor']) == (1, 878.79), b

        with app.app_context():
            antes = (Operation.query.count(), Check.query.count(), Transaction.query.count())
        for ruim in (4.01, -1, 'abc', 'NaN'):
            r = http.post('/api/operations', headers=cab, json={**payload(linhas, False), 'comissao': ruim})
            assert r.status_code == 400 and 'omissão' in r.get_json()['error'], (ruim, r.data)
        with app.app_context():
            assert (Operation.query.count(), Check.query.count(), Transaction.query.count()) == antes
        # cheque negativo ou com juros maior que o proprio valor: recusa sem gravar nada
        sem_centavos = payload(linhas, False, com_centavos=False)
        negativo = {**sem_centavos, 'checks': [{**sem_centavos['checks'][0], 'valor': -10}]}
        longe = {**sem_centavos, 'checks': [{**sem_centavos['checks'][0], 'vencimento': '2030-01-01'}]}
        for corpo, trecho in ((negativo, 'maior que zero'), (longe, 'passam do valor'),
                              ({**sem_centavos, 'checks': []}, 'sem cheques')):
            r = http.post('/api/operations', headers=cab, json=corpo)
            assert r.status_code == 400 and trecho in r.get_json()['error'], (trecho, r.data)
        with app.app_context():
            assert (Operation.query.count(), Check.query.count(), Transaction.query.count()) == antes

        r = http.post('/api/operations', headers=cab, json={**payload(linhas, False), 'comissao': 4})
        assert r.get_json()['comissao_valor'] == 3515.17, 'comissao igual a taxa = os juros inteiros'
        r = http.post('/api/operations', headers=cab, json=payload(linhas, False))
        assert r.status_code == 201 and r.get_json()['comissao_valor'] == 0 and r.get_json()['comissao'] == 0
        with app.app_context():
            sem = Transaction.query.filter_by(operation_id=r.get_json()['id']).all()
            assert len(sem) == 1 and sem[0].grupo_id is None, 'sem comissao continua uma linha so'
        saldo_depois = http.get('/api/transactions/balances', headers=cab).get_json()['bruto']['dinheiro_total']
        assert round(saldo_dinheiro - saldo_depois, 2) == round(10243.03 + 3515.17 + 10243.03, 2), 'comissao 4 de 4 + borderô sem comissao'

        # ------------ 4. alinhar_juros: borderô gravado com a formula antiga, caixa certo
        linhas = tela(1375.82, '2026-09-29', VENCS, 4, 2, False)
        with app.app_context():
            op = Operation(client_id=cli_id, client_name_snapshot='Cliente Teste', operation_date=date(2026, 9, 29),
                           monthly_rate=4, compensation_days=2, iof_amount=0, total_face_value=13758.2,
                           total_interest=2657.37, total_net_value=10243.03, status='Finalizada')
            ruim = Operation(client_id=cli_id, client_name_snapshot='Cliente Teste', operation_date=date(2026, 8, 25),
                             monthly_rate=6, compensation_days=2, iof_amount=0, total_face_value=19000,
                             total_interest=935.68, total_net_value=18064.32, status='Finalizada')
            db.session.add_all([op, ruim])
            db.session.flush()
            for l in linhas:                                   # formula antiga: liquido = valor / fator
                antigo = round(l['valor'] / (1.04 ** (l['dias'] / 30)), 2)
                db.session.add(Check(operation_id=op.id, due_date=date.fromisoformat(l['vencimento']), days=l['dias'],
                                     amount=l['valor'], interest_amount=round(l['valor'] - antigo, 2),
                                     net_amount=antigo, status='Aguardando'))
            for _ in range(2):
                db.session.add(Check(operation_id=ruim.id, due_date=date(2026, 9, 18), days=26, amount=9500,
                                     interest_amount=467.84, net_amount=9032.16, status='Aguardando'))
            db.session.commit()
            op_id, ruim_id = op.id, ruim.id
        alinhar_juros.main(gravar=True, app=app)
        op, chs, _ = None, None, None
        with app.app_context():
            op = db.session.get(Operation, op_id)
            chs = [(c.interest_amount, c.net_amount) for c in Check.query.filter_by(operation_id=op_id).order_by(Check.id)]
            assert chs == [(l['juros'], l['liquido']) for l in linhas] and op.total_interest == 3515.17
            ruim = db.session.get(Operation, ruim_id)
            assert ruim.total_interest == 935.68, "mexeu em borderô que nao confere com o caixa"
            assert {c.interest_amount for c in Check.query.filter_by(operation_id=ruim_id)} == {467.84}

        print("OK: cheque grava exatamente o juros/IOF/liquido da tela (com e sem IOF), caixa e totais "
              "sao a soma dos mesmos numeros, adulterado recusado sem gravar, tela antiga da o mesmo "
              "resultado, comissao igual a da tela sem mexer no liquido (invalida recusada), cheque negativo ou "
              "com juros maior que o valor recusado, "
              "e alinhar_juros corrige so o borderô que confere com o caixa.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
