"""
Duas empresas (versao 2.14): cada borderô (e seus titulos) e' de uma empresa, PQ (padrao) ou Fozesc.

  - borderô sem empresa vai para a PQ; o antigo (empresa vazia no banco) conta como PQ;
  - Titulos: filtro por empresa na lista, no resumo, no relatorio (cabecalho com o nome da empresa) e no lote;
  - Dashboard: lucro, carteira, inadimplencia, vencimentos e grafico de juros por empresa (PQ + Fozesc = todas);
    capital continua o mesmo; risco por emitente filtra tambem;
  - mudar a empresa de um borderô: so Fozesc/PQ, auditado, e o titulo passa a aparecer na outra.

Banco SEPARADO (fozesc_teste_empresas), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_empresas.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_empresas'
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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Operation, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc ESC', cnpj='11.111.111/0001-11', pq_nome='PQ Ltda',
                                           pq_cnpj='22.222.222/0001-22', capital_social=50000))
            db.session.add_all([Client(name='Ana'), Client(name='Bia')])
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def bordero(cliente, valor, empresa=None, emitente='Emitente X', dias=30):
            r = http.post('/api/operations', headers=cab, json={
                'client_id': cliente, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
                'account_source': 'Dinheiro', 'iof_enabled': False, **({'empresa': empresa} if empresa else {}),
                'checks': [{'valor': valor, 'vencimento': D(dias), 'emitente': emitente}]})
            assert r.status_code == 201, r.data
            return r.get_json()['id']

        op_pq = bordero(1, 1000, 'PQ', 'Emitente PQ')
        op_fz = bordero(2, 2000, 'Fozesc', 'Emitente FZ')
        op_padrao = bordero(1, 400)
        op_antigo = bordero(2, 300, 'Fozesc', dias=0)
        with app.app_context():
            assert db.session.get(Operation, op_padrao).empresa == 'PQ', 'sem empresa = PQ'
            db.session.get(Operation, op_antigo).empresa = None
            db.session.commit()

        lista = lambda **f: http.get('/api/checks', headers=cab, query_string={'per_page': 50, **f}).get_json()
        valores = lambda **f: sorted(i['valor_bruto'] for i in lista(**f)['items'])
        assert valores(empresa='PQ') == [300, 400, 1000], 'PQ inclui o antigo sem empresa'
        assert valores(empresa='Fozesc') == [2000]
        assert valores() == [300, 400, 1000, 2000] and valores(empresa='xyz') == [300, 400, 1000, 2000]
        assert lista(empresa='Fozesc')['resumo']['valor'] == 2000 and lista(empresa='PQ')['resumo']['valor'] == 1700
        assert {i['empresa'] for i in lista(empresa='PQ')['items']} == {'PQ'}, 'a lista diz a empresa de cada titulo'

        rel = http.get('/api/checks/relatorio', headers=cab, query_string={'empresa': 'PQ'}).get_json()
        assert (rel['empresa'], rel['cnpj'], rel['filtro_empresa'], len(rel['itens'])) == ('PQ Ltda', '22.222.222/0001-22', 'PQ', 3)
        rel = http.get('/api/checks/relatorio', headers=cab).get_json()
        assert (rel['empresa'], rel['filtro_empresa'], len(rel['itens'])) == ('Fozesc ESC', None, 4)

        dash = lambda e=None: http.get('/api/dashboard', headers=cab, query_string={'empresa': e} if e else {}).get_json()
        todas, pq, fz = dash(), dash('PQ'), dash('Fozesc')
        assert todas['kpis']['carteira'] == 3700 and pq['kpis']['carteira'] == 1700 and fz['kpis']['carteira'] == 2000
        assert pq['kpis']['capital'] == fz['kpis']['capital'] == todas['kpis']['capital'] == 50000, 'capital e das duas'
        assert pq['vencem_hoje']['total'] == 300 and fz['vencem_hoje']['quantidade'] == 0
        assert sum(pq['charts']['evolution']['profit_data']) + sum(fz['charts']['evolution']['profit_data']) == \
            sum(todas['charts']['evolution']['profit_data'])
        assert [u['valor'] for u in fz['upcoming']] == [2000] and pq['empresa'] == 'PQ' and todas['empresa'] is None

        risco = lambda e: [x['emitente'] for x in http.get('/api/checks/emitentes/risco', headers=cab,
                                                             query_string={'empresa': e}).get_json()['maiores']]
        assert 'Emitente FZ' in risco('Fozesc') and 'Emitente FZ' not in risco('PQ') and 'Emitente PQ' in risco('PQ')
        r_fz = http.get('/api/checks/emitentes/risco', headers=cab, query_string={'empresa': 'Fozesc'}).get_json()
        assert r_fz['total_em_aberto'] == 2000 and r_fz['maiores'][0]['pct'] == 100, 'percentual dentro da empresa'

        # mudar a empresa de um borderô
        assert http.put(f'/api/operations/{op_antigo}/empresa', headers=cab, json={'empresa': 'Itau'}).status_code == 400
        assert http.put('/api/operations/9999/empresa', headers=cab, json={'empresa': 'PQ'}).status_code == 404
        r = http.put(f'/api/operations/{op_antigo}/empresa', headers=cab, json={'empresa': 'Fozesc'})
        assert r.status_code == 200 and r.get_json()['empresa'] == 'Fozesc'
        assert valores(empresa='Fozesc') == [300, 2000] and dash('Fozesc')['kpis']['carteira'] == 2300
        with app.app_context():
            tid = Check.query.filter_by(operation_id=op_antigo).first().id
            assert 'empresa PQ (padrão) -> Fozesc' in AuditLog.query.filter_by(target='Borderô').order_by(AuditLog.id.desc()).first().description
        det = http.get(f'/api/checks/{tid}', headers=cab).get_json()
        assert det['bordero']['empresa'] == 'Fozesc' and det['bordero']['empresa_gravada'] is True and det['empresa'] == 'Fozesc'

        # relatorio gerencial por empresa (lucro e inadimplencia); o resumo geral (caixa) e' sempre das duas
        with app.app_context():
            Check.query.filter_by(operation_id=op_fz).first().status = 'Juridico'
            db.session.commit()
        periodo = {'inicio': (HOJE - timedelta(days=1)).isoformat(), 'fim': D(60)}
        rel = lambda tipo, e=None: http.get('/api/reports/resumo', headers=cab, query_string={
            'tipo': tipo, **periodo, **({'empresa': e} if e else {})}).get_json()
        linha = lambda r, rotulo: next(l['valor'] for l in r['linhas'] if l['label'].startswith(rotulo))
        gerado = {e: linha(rel('lucro', e), 'Juros gerado') for e in (None, 'PQ', 'Fozesc')}
        assert gerado['PQ'] > 0 and gerado['Fozesc'] > 0 and round(gerado['PQ'] + gerado['Fozesc'], 2) == gerado[None], gerado
        assert (rel('lucro', 'PQ')['empresa'], rel('lucro', 'PQ')['filtro_empresa']) == ('PQ Ltda', 'PQ')
        assert (rel('lucro', 'Fozesc')['empresa'], rel('lucro')['filtro_empresa']) == ('Fozesc ESC', None)
        atraso = {e: linha(rel('inadimplencia', e), 'Total em atraso') for e in (None, 'PQ', 'Fozesc')}
        assert atraso == {None: 2000, 'PQ': 0, 'Fozesc': 2000}, atraso
        assert rel('geral', 'PQ')['filtro_empresa'] is None, 'o caixa e um so'

        # aviso de contratos da tela inicial e filtro "com titulo vencido"
        p = http.get('/api/contratos/pendencias', headers=cab).get_json()
        assert p == {'nao_assinados': 2, 'com_atraso': 1}, p
        r = http.get('/api/contratos', headers=cab, query_string={'atraso': 'sim'}).get_json()
        assert [c['id'] for c in r['items']] == [op_fz], r

        print("OK: duas empresas - borderô sem empresa = PQ (antigo tambem), filtro de titulos (lista, resumo, relatorio "
              "com o nome da empresa), dashboard por empresa (PQ + Fozesc = todas, capital igual), risco por emitente, "
              "mudar a empresa do borderô auditado.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
