"""
Ponta a ponta (versao 2.14): os numeros batem entre borderô, caixa, documentos, contrato, Dashboard e Titulos,
por empresa, com cheque e promissoria, IOF ligado e desligado, comissao e saida dividida.

  - documento do borderô: valor/juros/IOF/liquido = soma dos titulos; o caixa saiu exatamente o liquido;
  - contrato: valor antecipado = valor de face, Quadro 03 = liquido, IOF adicional + diario = total (e 0% sem IOF);
    data = a do borderô; numeros do borderô da operação coerentes (CET >= taxa efetiva, iguais sem IOF);
  - Dashboard e Titulos: PQ + Fozesc = ambas (carteira, lucro depois de receber, resumo da lista);
  - juntar clientes leva os dados para contrato do cadastro apagado.

Banco SEPARADO (fozesc_teste_ponta), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_ponta_a_ponta.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_ponta'
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
        from app.models.domain import Client, CompanySettings, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc ESC', cnpj='11.111.111/0001-11', pq_nome='PQ Ltda',
                                           iof_rate=0.38, iof_daily_rate=0.0041))
            db.session.add_all([Client(name='Ana Empresa Ltda', document='11.222.333/0001-81'),
                                Client(name='Bruno Pessoa', document='111.444.777-35')])
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        brl = lambda t: float(str(t).replace('.', '').replace(',', '.'))
        perto = lambda a, b, tol=0.011: abs(a - b) <= tol

        def bordero(cliente, empresa, titulos, iof=True, comissao=0, partes=None, data=HOJE):
            r = http.post('/api/operations', headers=cab, json={
                'client_id': cliente, 'operation_date': data.isoformat(), 'taxa_mensal': 5, 'dias_compensacao': 2,
                'account_source': 'Dinheiro', 'iof_enabled': iof, 'iof_base': 0.38, 'iof_diario': 0.0041,
                'empresa': empresa, 'comissao': comissao, **({'partes': partes} if partes else {}),
                'checks': [{'valor': v, 'vencimento': D(d), 'tipo': tipo, 'emitente': 'Emitente X', 'num_doc': f'{i}'}
                           for i, (v, d, tipo) in enumerate(titulos, 1)]})
            assert r.status_code == 201, r.data
            return r.get_json()

        ontem = HOJE - timedelta(days=1)
        b1 = bordero(1, 'Fozesc', [(10000, 30, 'PROMISSORIA'), (10000, 60, 'PROMISSORIA')], comissao=1, data=ontem)
        liq_parcial = 3000
        b2 = bordero(2, 'PQ', [(5000, 45, 'CHEQUE'), (2500, 20, 'CHEQUE')], iof=False)
        b3 = bordero(2, 'PQ', [(1200, 15, 'CHEQUE')],
                     partes=None)
        assert b1['contrato_gerado'] is True and b2['contrato_gerado'] is False

        for b in (b1, b2, b3):
            d = http.get(f"/api/operations/{b['id']}/documento", headers=cab).get_json()
            t = d['titulos']
            assert perto(d['valor_total'], sum(x['valor'] for x in t)) and perto(d['juros_total'], sum(x['juros'] for x in t))
            assert perto(d['iof_total'], sum(x['iof'] for x in t), 0.02) and perto(d['liquido_total'], sum(x['liquido'] for x in t))
            assert perto(d['valor_total'] - d['juros_total'] - d['iof_total'], d['liquido_total'], 0.02), d
            with app.app_context():
                saiu = -sum(x.amount for x in Transaction.query.filter_by(operation_id=b['id'], category='Compra de Ativos'))
            assert perto(saiu, d['liquido_total']), ('caixa', saiu, d['liquido_total'])

            c = http.get(f"/api/contratos/{b['id']}", headers=cab).get_json()
            s, o = c['sugestao'], c['operacao']
            assert brl(s['valorAntecipado']) == round(d['valor_total'], 2) and brl(s['encargos']['valorAntecipado']) == round(d['liquido_total'], 2)
            assert perto(brl(s['encargos']['iofAdicional']) + brl(s['encargos']['iofDiario']), d['iof_total']) and brl(s['encargos']['iofTotal']) == round(d['iof_total'], 2)
            assert s['data'] == d['data'], 'o contrato leva a data do borderô'
            assert [brl(x['valor']) for x in s['titulos']] == [x['valor'] for x in t]
            if d['iof_total'] == 0:
                assert s['encargos']['iofAdicionalPct'] == '0,00%' and s['encargos']['iofDiarioPct'] == '0,00%', 'sem IOF: 0%'
                assert o['cet_mes'] == round(o['taxa_efetiva_mes'], 2)
            else:
                assert o['cet_mes'] > o['taxa_efetiva_mes'] > 5
            assert o['qtd_titulos'] == len(t) and perto(o['liquido'], d['liquido_total'])

        # Dashboard e Titulos: PQ + Fozesc = ambas
        dash = lambda e=None: http.get('/api/dashboard', headers=cab, query_string={'empresa': e} if e else {}).get_json()['kpis']
        lista = lambda e=None: http.get('/api/checks', headers=cab, query_string={'per_page': 50, **({'empresa': e} if e else {})}).get_json()
        for chave in ('carteira', 'lucro', 'inadimplencia'):
            assert perto(dash('PQ')[chave] + dash('Fozesc')[chave], dash()[chave]), chave
        assert perto(lista('PQ')['resumo']['valor'] + lista('Fozesc')['resumo']['valor'], lista()['resumo']['valor'])
        assert dash('Fozesc')['carteira'] == 20000 and dash('PQ')['carteira'] == 8700

        # recebe um titulo da Fozesc: lucro so na Fozesc
        tid = next(i['id'] for i in lista('Fozesc')['items'])
        r = http.patch(f'/api/checks/{tid}/status', headers=cab, json={'status': 'Pago', 'payment_data': {'method': 'BB'}})
        assert r.status_code == 200, r.data
        assert dash('Fozesc')['lucro'] > 0 and dash('PQ')['lucro'] == 0 and perto(dash()['lucro'], dash('Fozesc')['lucro'])
        assert http.get(f"/api/contratos/{b1['id']}", headers=cab).get_json()['operacao']['situacao'] == 'Em aberto'

        # juntar clientes: os dados para contrato do cadastro apagado vao para o que fica (se estiver vazio la)
        r = http.post('/api/clients', headers=cab, json={'name': 'Ana Empresa (dup)', 'rep_nome': 'Carlos', 'rep_cpf': '529.982.247-25', 'rg': '9'})
        assert r.status_code == 201, r.data
        with app.app_context():
            dup = Client.query.filter_by(name='Ana Empresa (dup)').first().id
        assert http.put('/api/clients/1', headers=cab, json={'rep_cpf': '111.444.777-35'}).status_code == 200
        r = http.post('/api/clients/merge', headers=cab, json={'origem_id': dup, 'destino_id': 1, 'senha': SENHA})
        assert r.status_code == 200, r.data
        c1 = http.get('/api/clients/1', headers=cab).get_json()
        assert (c1['rep_nome'], c1['rep_cpf'], c1['rg']) == ('Carlos', '111.444.777-35', '9'), 'vazio herda, preenchido fica'

        print("OK: ponta a ponta - documento = soma dos titulos e o caixa saiu o liquido; contrato (valor antecipado, "
              "Quadro 03, IOF adicional + diario, 0% sem IOF, data do borderô) e borderô da operação coerentes; "
              "Dashboard e Titulos PQ + Fozesc = ambas; lucro na empresa certa; juntar clientes guarda os dados para contrato.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
