"""
Historico Mensal: lucro bruto do mes, grafico do lucro de todos os meses e lancamentos paginados.

Lucro bruto = juros dos borderos feitos no mes (sem os fora do calculo) + juros de prorrogacao,
imposto e multa que entraram no caixa no mes ('Multas e Juros') - comissoes que sairam no mes.
IOF so informa. O grafico vai do primeiro ao ultimo mes, com zero no mes sem nada.

Banco SEPARADO (fozesc_teste_historico), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_historico.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_historico'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
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
        from app.models.domain import Check, Client, CompanySettings, Operation, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        MARCO, JANEIRO = date(2026, 3, 10), date(2026, 1, 20)
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Operador', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Ana')
            db.session.add(cli)
            db.session.flush()
            op_marco = Operation(client_id=cli.id, operation_date=MARCO, monthly_rate=4, iof_amount=4.5)
            op_jan = Operation(client_id=cli.id, operation_date=JANEIRO, monthly_rate=4, iof_amount=3)
            op_velho = Operation(client_id=cli.id, operation_date=MARCO, monthly_rate=4, iof_amount=99)
            db.session.add_all([op_marco, op_jan, op_velho])
            db.session.flush()
            db.session.add_all([
                Check(operation_id=op_marco.id, amount=1000, due_date=date(2026, 4, 10), interest_amount=100),
                Check(operation_id=op_marco.id, amount=500, due_date=date(2026, 4, 20), interest_amount=50),
                Check(operation_id=op_velho.id, amount=9000, due_date=date(2026, 4, 1), interest_amount=999,
                      status='Pago', fora_do_calculo=True),                        # historico: nao conta
                Check(operation_id=op_jan.id, amount=2000, due_date=date(2026, 2, 20), interest_amount=200),
            ])
            t = lambda dia, valor, tipo, cat, desc: Transaction(date=dia, amount=valor, type=tipo, origin='Dinheiro',
                                                                 category=cat, description=desc)
            db.session.add_all([
                t(MARCO, -30, 'saida', 'Comissão', 'Comissão (2 de 4 dos juros) - Borderô #1'),
                t(date(2026, 3, 15), 20, 'entrada', 'Multas e Juros', 'Juros de prorrogação - Cheque #1 - Joao'),
                t(date(2026, 3, 16), 10, 'entrada', 'Multas e Juros', 'Imposto Cheque #2 - Joao (5 dias)'),
                t(date(2026, 3, 17), 5, 'entrada', 'Multas e Juros', 'Multa Devolução Cheque #3 - Joao (2%)'),
                t(date(2026, 3, 18), 7, 'entrada', 'Multas e Juros', 'Juros cobrados a mao'),
                t(date(2026, 4, 2), 1000, 'entrada', 'Multas e Juros', 'Juros de prorrogação - abril'),  # outro mes
                t(MARCO, -58000, 'saida', 'Compra de Ativos', 'Pgto Borderô'),                  # nao e' lucro
            ])
            db.session.add_all([t(date(2026, 3, 1 + i % 28), 1, 'entrada', 'Geral', f'lanc {i}') for i in range(60)])
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        # ------------------------------------------------------------ lucro bruto do mes
        m = http.get('/api/history/month/2026/3', headers=cab).get_json()
        lb = m['lucro_bruto']
        assert (lb['juros_borderos'], lb['qtd_borderos'], lb['prorrogacao'], lb['imposto'], lb['multa'], lb['outros'],
                lb['comissoes'], lb['iof']) == (150, 1, 20, 10, 5, 7, 30, 4.5), lb
        assert lb['lucro_bruto'] == 150 + 20 + 10 + 5 + 7 - 30 == 162, lb
        assert m['resumo']['lucro'] == 150, 'o juros do card antigo continua o mesmo'

        # ------------------------------------------------------------ lancamentos paginados
        assert (m['lancamentos_total'], m['lancamentos_paginas'], len(m['lancamentos'])) == (66, 2, 50), \
            (m['lancamentos_total'], m['lancamentos_paginas'], len(m['lancamentos']))
        p2 = http.get('/api/history/month/2026/3?page=2', headers=cab).get_json()
        assert len(p2['lancamentos']) == 16 and p2['lancamentos_pagina'] == 2
        assert not {x['id'] for x in p2['lancamentos']} & {x['id'] for x in m['lancamentos']}, 'pagina 2 nao repete'
        assert len(http.get('/api/history/month/2026/3?per_page=5000', headers=cab).get_json()['lancamentos']) == 66, \
            'pedir 5000 cai no teto de 100 (e o mes tem 66)'

        # ------------------------------------------------------------ meses e grafico
        meses = http.get('/api/history/months', headers=cab).get_json()
        assert [(x['year'], x['month'], x['label']) for x in meses] == [
            (2026, 4, 'Abril/2026'), (2026, 3, 'Março/2026'), (2026, 1, 'Janeiro/2026')], meses
        g = http.get('/api/history/lucro', headers=cab).get_json()
        assert [(x['label'], x['lucro_bruto']) for x in g] == [
            ('Janeiro/2026', 200), ('Fevereiro/2026', 0), ('Março/2026', 162), ('Abril/2026', 1000)], g
        assert http.get('/api/history/lucro').status_code == 401

        print("OK: lucro bruto do mes (juros dos borderos + prorrogacao + imposto + multa - comissoes, sem fora do "
              "calculo nem compra de ativos, IOF a parte), lancamentos em paginas de 50 (teto 100), seletor so com "
              "os nomes dos meses e grafico de todos os meses com zero no mes vazio.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
