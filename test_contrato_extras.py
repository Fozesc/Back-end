"""
Contratos - parte 2 (versao 2.14): CPF/CNPJ conferidos, padroes do contrato nos Ajustes, "assinado" com senha e a
notificacao de cobranca.

  - digito do CPF e do CNPJ (inclusive o CNPJ com letras de 2026); so confere quando o numero muda;
  - Ajustes > Contratos: juros de mora, multa, honorarios, prazo, vias e cidade entram no contrato novo;
  - assinado: sem senha certa nada muda (NEGADO na auditoria); com a senha marca quem e quando; filtro na lista;
  - cobranca: so titulos vencidos/devolvidos sem pagar, juros de mora pro rata die capitalizados ao mes e multa
    sobre (valor + juros), com os numeros do contrato.

Banco SEPARADO (fozesc_teste_contrato_extra), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_contrato_extras.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_contrato_extra'
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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Contrato, User
        from app.utils.validacao import documento_valido
        from werkzeug.security import generate_password_hash

        for doc, ok in (('529.982.247-25', True), ('52998224725', True), ('529.982.247-24', False), ('111.111.111-11', False),
                        ('11.222.333/0001-81', True), ('11.222.333/0001-82', False), ('12.ABC.345/01DE-35', True),
                        ('12.ABC.345/01DE-36', False), ('', True), (None, True), ('1234', False), ('00.000.000/0000-00', False)):
            assert documento_valido(doc) is ok, (doc, ok)

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc ESC', cnpj='99.999.999/0001-99'))
            db.session.add(Client(name='Antigo', document='123.456.789-00'))
            db.session.commit()
        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        # ---------------------------------------------------- CPF/CNPJ
        r = http.post('/api/clients', headers=cab, json={'name': 'Errado', 'document': '529.982.247-24'})
        assert r.status_code == 400 and 'dígitos' in r.get_json()['error']
        r = http.post('/api/clients', headers=cab, json={'name': 'Nova Ltda', 'document': '12.ABC.345/01DE-35'})
        assert r.status_code == 201, r.data
        assert http.put('/api/clients/1', headers=cab, json={'name': 'Antigo', 'document': '123.456.789-00', 'phone': '1'}).status_code == 200, \
            'cadastro antigo com documento errado continua editavel'
        assert http.put('/api/clients/1', headers=cab, json={'document': '111.111.111-11'}).status_code == 400
        assert http.put('/api/clients/1', headers=cab, json={'rep_cpf': '1'}).status_code == 400
        assert http.put('/api/clients/1', headers=cab, json={'rep_cpf': '529.982.247-25'}).status_code == 200
        r = http.put('/api/settings/', headers=cab, json={'pqCnpj': '11.222.333/0001-82'})
        assert r.status_code == 400 and 'CNPJ da PQ' in r.get_json()['error']
        assert http.put('/api/settings/', headers=cab, json={'cnpj': '99.999.999/0001-99', 'taxaPadrao': 5}).status_code == 200, \
            'CNPJ gravado errado nao trava o salvar'
        assert http.put('/api/settings/', headers=cab, json={'repCpf': '529.982.247-24'}).status_code == 400

        # ---------------------------------------------------- padroes do contrato
        r = http.put('/api/settings/', headers=cab, json={'contratoJurosMora': '1,5', 'contratoMulta': 2, 'contratoHonorarios': 15,
                                                          'contratoPrazoReembolso': 15, 'contratoVias': 3, 'contratoCidade': 'Cascavel'})
        assert r.status_code == 200, r.data
        g = http.get('/api/settings/', headers=cab).get_json()
        assert (g['contratoJurosMora'], g['contratoHonorarios'], g['contratoPrazoReembolso'], g['contratoVias'], g['contratoCidade']) == \
            (1.5, 15, 15, 3, 'Cascavel')
        for ruim in ({'contratoVias': 0}, {'contratoPrazoReembolso': 'x'}, {'contratoMulta': 50}, {'contratoCidade': ' '}):
            assert http.put('/api/settings/', headers=cab, json=ruim).status_code == 400, ruim

        def bordero(empresa='Fozesc', valores=(1000, 2000)):
            r = http.post('/api/operations', headers=cab, json={
                'client_id': 2, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
                'account_source': 'Dinheiro', 'iof_enabled': False, 'empresa': empresa,
                'checks': [{'valor': v, 'vencimento': D(30 * i), 'tipo': 'PROMISSORIA', 'num_doc': f'{i}/{len(valores)}'}
                           for i, v in enumerate(valores, 1)]})
            assert r.status_code == 201, r.data
            return r.get_json()['id']

        op = bordero()
        d = http.get(f'/api/contratos/{op}', headers=cab).get_json()['dados']
        assert d['condicoes'] == {'jurosMora': 1.5, 'multa': 2.0, 'honorarios': 15.0, 'prazoReembolso': 15, 'vias': 3}
        assert d['local'] == 'Cascavel' and d['encargos']['jurosMora'] == '1,50%'

        # ---------------------------------------------------- assinado
        A = f'/api/contratos/{op}/assinado'
        assert http.put(A, headers=cab, json={'assinado': True}).status_code == 403
        assert http.put(A, headers=cab, json={'assinado': True, 'senha': 'errada'}).status_code == 403
        assert http.put(A, headers=cab, json={'assinado': 'sim', 'senha': SENHA}).status_code == 400
        with app.app_context():
            assert Contrato.query.filter_by(operation_id=op).first().assinado is False
            assert AuditLog.query.filter_by(action='NEGADO', target='Contrato').count() == 2
        r = http.put(A, headers=cab, json={'assinado': True, 'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['assinado'] is True and r.get_json()['assinado_por'] == 'Teste'
        op2 = bordero()
        lista = lambda f: [i['id'] for i in http.get('/api/contratos', headers=cab, query_string={'assinado': f}).get_json()['items']]
        assert lista('sim') == [op] and lista('nao') == [op2]
        abre = http.get(f'/api/contratos/{op}', headers=cab).get_json()
        assert abre['assinado'] is True and abre['assinado_em']
        r = http.put(A, headers=cab, json={'assinado': False, 'senha': SENHA})
        assert r.get_json() == {'assinado': False, 'assinado_por': None, 'assinado_em': None}
        assert http.put(f'/api/contratos/{bordero("PQ")}/assinado', headers=cab, json={'assinado': True, 'senha': SENHA}).status_code == 404

        # ---------------------------------------------------- cobranca
        from datetime import timedelta as td
        with app.app_context():
            t1, t2 = Check.query.filter_by(operation_id=op).order_by(Check.id).all()
            t1.due_date = HOJE - td(days=45)
            t2.due_date = HOJE - td(days=10)
            t2.status = 'Pago'
            db.session.commit()
        c = http.get(f'/api/contratos/{op}/cobranca', headers=cab).get_json()
        assert len(c['titulos']) == 1 and c['titulos'][0]['dias'] == 45 and c['titulos'][0]['numero'] == '1/2', 'pago fica de fora'
        t = c['titulos'][0]
        juros = round(1000 * ((1.015) ** (45 / 30) - 1), 2)
        assert t['juros'] == juros and t['multa'] == round((1000 + juros) * 0.02, 2) and t['total'] == round(1000 + juros + t['multa'], 2)
        assert c['totais']['total'] == t['total'] and c['condicoes']['prazoReembolso'] == 15 and c['contratante']['nome'] == 'Nova Ltda'
        sem = http.get(f'/api/contratos/{op2}/cobranca', headers=cab).get_json()
        assert sem['titulos'] == [] and sem['totais']['total'] == 0
        with app.app_context():
            x = Check.query.filter_by(operation_id=op2).order_by(Check.id).first()
            x.status = 'Devolvido'
            db.session.commit()
        assert len(http.get(f'/api/contratos/{op2}/cobranca', headers=cab).get_json()['titulos']) == 1, 'devolvido entra mesmo antes do vencimento'
        assert http.get('/api/contratos/999/cobranca', headers=cab).status_code == 404

        print("OK: CPF/CNPJ conferidos (CNPJ com letras, so quando muda), padroes do contrato nos Ajustes, assinado com "
              "senha (NEGADO, quem/quando, filtro, desmarcar), notificacao de cobranca (vencidos/devolvidos, mora pro rata "
              "die capitalizada, multa sobre valor + juros).")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
