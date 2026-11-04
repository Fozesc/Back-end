"""
Contrato de desconto (versao 2.14, em construcao): montado com os dados do sistema, editado e guardado.

  - sugestao: contratada = empresa do borderô (Ajustes), contratante = cliente (endereco em 2 linhas), titulos do
    borderô (promissoria com o CPF/CNPJ do cliente), encargos com o IOF separado em adicional e diario (fecha com o total);
  - salvar guarda o que a tela mandou; o proximo contrato do mesmo cliente ja traz o representante, e o da mesma
    empresa traz e-mail e representante da contratada;
  - contrato incompleto ou grande demais e' recusado; sem login nada; apagar o borderô apaga o contrato.

Banco SEPARADO (fozesc_teste_contrato), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_contrato.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_contrato'
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


def _auditoria(app, AuditLog, alvo):
    with app.app_context():
        return AuditLog.query.filter_by(target=alvo).all()


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Contrato, User
        from app.services.operation_service import calcular_linha
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste', cnpj='11.111.111/0001-11', address='Av. A, 1',
                                           phone='(45) 1111-1111', pq_nome='Empresa Teste Ltda', pq_cnpj='11.444.777/0001-61',
                                           pq_endereco='Av. Republica Argentina'))
            cli = Client(name='Alfa Teste Ltda', document='11.222.333/0001-81', phone='(45) 9992-0000', email='r@x.com',
                         address='Rua Sao Francisco', neighborhood='Campos do Iguacu', city='Foz do Iguacu', state='PR')
            pf = Client(name='Joao da Silva', document='111.444.777-35', address='Rua B', city='Foz', state='PR')
            db.session.add_all([cli, pf])
            db.session.commit()
            id_cli, id_pf = cli.id, pf.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def bordero(cliente, empresa, valores=(1000, 2000)):
            r = http.post('/api/operations', headers=cab, json={
                'client_id': cliente, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 5.6784, 'dias_compensacao': 0,
                'account_source': 'Dinheiro', 'iof_enabled': True, 'iof_base': 0.38, 'iof_diario': 0.0041, 'empresa': empresa,
                'checks': [{'valor': v, 'vencimento': D(30 * i), 'num_doc': f'{i}/{len(valores)}', 'tipo': 'PROMISSORIA'}
                           for i, v in enumerate(valores, 1)]})
            assert r.status_code == 201, r.data
            return r.get_json()['id']

        op1 = bordero(id_cli, 'PQ')
        assert http.get(f'/api/contratos/{op1}').status_code == 401
        r = http.get(f'/api/contratos/{op1}', headers=cab).get_json()
        assert r['salvo'] is False and r['dados'] == r['sugestao']
        s = r['sugestao']
        assert (s['numero'], s['valorAntecipado'], s['local']) == (str(op1), '3.000,00', 'Foz do Iguaçu')
        assert (s['contratada']['nome'], s['contratada']['cnpj'], s['contratada']['email']) == ('Empresa Teste Ltda', '11.444.777/0001-61', '')
        assert s['contratante']['endereco'] == 'Rua Sao Francisco, Campos do Iguacu\nFoz do Iguacu - PR'
        assert s['contratante']['rep']['nome'] == '', 'empresa (CNPJ): representante em branco no 1o contrato'
        assert s['titulos'] == [
            {'tipo': 'Nota promissória', 'numero': '1/2', 'emitente': 'Alfa Teste Ltda', 'documento': '11.222.333/0001-81', 'vencimento': '/'.join(reversed(D(30).split('-'))), 'valor': '1.000,00'},
            {'tipo': 'Nota promissória', 'numero': '2/2', 'emitente': 'Alfa Teste Ltda', 'documento': '11.222.333/0001-81', 'vencimento': '/'.join(reversed(D(60).split('-'))), 'valor': '2.000,00'}]
        e = s['encargos']
        iofs = [calcular_linha(v, 30 * i, 5.6784, True, 0.38, 0.0041)[1] for i, v in enumerate((1000, 2000), 1)]
        brl = lambda t: float(t.replace('.', '').replace(',', '.'))
        assert (e['taxaJuros'], e['iofAdicionalPct'], e['iofDiarioPct'], e['jurosMora']) == ('5,6784', '0,38%', '0,0041%', '1,00%')
        assert brl(e['iofAdicional']) == 11.40 and round(brl(e['iofAdicional']) + brl(e['iofDiario']), 2) == round(sum(iofs), 2) == brl(e['iofTotal'])

        # numeros do "Borderô da operação de desconto"
        o = http.get(f'/api/contratos/{op1}', headers=cab).get_json()['operacao']
        assert (o['situacao'], o['qtd_titulos'], o['valor_total'], o['operador'], o['cliente_desde']) == ('Em aberto', 2, 3000, 'Teste', HOJE.isoformat())
        assert o['prazo_medio'] == 50 and o['taxa_periodo'] == round(o['juros'] / 3000 * 100, 6)
        from app.controllers.contrato_controller import _taxa_mensal
        assert abs(_taxa_mensal([(1000, 30)], 1000 / 1.05) - 0.05) < 1e-9 and _taxa_mensal([(1000, 30)], 1000) == 0.0
        assert _taxa_mensal([(1000, 0)], 900) is None and _taxa_mensal([], 1) is None
        assert 5.6784 < o['taxa_efetiva_mes'] < o['cet_mes'], 'com juros compostos a efetiva fica acima da nominal e o CET (com IOF) acima dela'
        assert abs(o['cet_ano'] - ((1 + o['cet_mes'] / 100) ** 12 - 1) * 100) < 0.2

        # salvar com o representante e as testemunhas digitados
        s['contratante']['rep'] = {**s['contratante']['rep'], 'nome': 'Carlos Teste', 'cpf': '111.444.777-35', 'rg': '123'}
        s['contratada']['email'] = 'contato@exemplo.com'
        s['contratada']['rep']['nome'] = 'Fulano Teste'
        s['testemunhas'][0]['nome'] = 'Testemunha Um'
        r = http.put(f'/api/contratos/{op1}', headers=cab, json={'dados': s})
        assert r.status_code == 200, r.data
        r = http.get(f'/api/contratos/{op1}', headers=cab).get_json()
        assert r['salvo'] and r['dados']['contratante']['rep']['nome'] == 'Carlos Teste' and r['dados']['testemunhas'][0]['nome'] == 'Testemunha Um'
        assert r['atualizado_por'] == 'Teste'

        # proximo contrato: mesmo cliente traz o representante; mesma empresa traz e-mail/representante da contratada
        op2 = bordero(id_cli, 'PQ', (500,))
        s2 = http.get(f'/api/contratos/{op2}', headers=cab).get_json()['sugestao']
        assert s2['contratante']['rep']['nome'] == 'Carlos Teste' and s2['contratada']['email'] == 'contato@exemplo.com'
        assert s2['contratada']['rep']['nome'] == 'Fulano Teste' and s2['testemunhas'][0]['nome'] == ''
        op3 = bordero(id_pf, 'Fozesc', (800,))
        r3 = http.get(f'/api/contratos/{op3}', headers=cab).get_json()
        s3 = r3['sugestao']
        assert r3['salvo'] and r3['atualizado_por'] == 'Teste' and r3['dados'] == s3, 'borderô da Fozesc ja nasce com o contrato'
        assert http.get(f'/api/operations/{op3}/documento', headers=cab).get_json()['tem_contrato'] is True
        assert http.get(f'/api/operations/{op2}/documento', headers=cab).get_json()['tem_contrato'] is False, 'PQ nao gera sozinho'
        assert s3['contratada']['nome'] == 'Fozesc Teste' and s3['contratada']['telefone'] == '(45) 1111-1111'
        assert s3['contratada']['email'] == '', 'outra empresa nao herda o e-mail da PQ'
        assert (s3['contratante']['rep']['nome'], s3['contratante']['rep']['cpf']) == ('Joao da Silva', '111.444.777-35'), 'pessoa fisica: representante e o proprio cliente'

        # invalidos
        for ruim in ({}, {'dados': {'titulos': []}}, {'dados': {'contratante': {}, 'titulos': ['x' * 900] * 200}},
                     {'dados': {'contratante': {}, 'titulos': [[[[[['fundo']]]]]]}}):
            assert http.put(f'/api/contratos/{op2}', headers=cab, json=ruim).status_code == 400, ruim
        assert http.get('/api/contratos/9999', headers=cab).status_code == 404

        lista = http.get('/api/contratos', headers=cab).get_json()
        assert lista['total'] == 2 and [i['id'] for i in lista['items']] == [op3, op1] and lista['items'][1]['cliente'] == 'Alfa Teste Ltda'
        with app.app_context():
            assert AuditLog.query.filter_by(target='Contrato', action='CREATE').count() == 2
            assert AuditLog.query.filter(AuditLog.description.like('Gerou automaticamente o contrato do borderô #%')).count() == 1

        # a lista traz todos: borderô da Fozesc sem contrato (feito antes do automatico) ganha o dele ao listar
        op4 = bordero(id_pf, 'Fozesc', (300,))
        with app.app_context():
            Contrato.query.filter_by(operation_id=op4).delete()
            db.session.commit()
        lista = http.get('/api/contratos', headers=cab).get_json()
        assert op4 in [i['id'] for i in lista['items']] and lista['total'] == 3, 'Fozesc sem contrato entra na lista'
        busca = lambda q: [i['id'] for i in http.get('/api/contratos', headers=cab, query_string={'q': q}).get_json()['items']]
        assert sorted(busca('joao')) == sorted([op3, op4]) and busca(f'#{op1}') == [op1] and busca('%') == []

        # borderô que passa para a Fozesc ganha o contrato; o que ja tem contrato nao e' sobrescrito
        r = http.put(f'/api/operations/{op2}/empresa', headers=cab, json={'empresa': 'Fozesc'})
        assert r.status_code == 200 and r.get_json()['contrato_gerado'] is True
        assert http.get(f'/api/contratos/{op2}', headers=cab).get_json()['salvo'] is True
        http.put(f'/api/operations/{op1}/empresa', headers=cab, json={'empresa': 'Fozesc'})
        assert http.get(f'/api/contratos/{op1}', headers=cab).get_json()['dados']['testemunhas'][0]['nome'] == 'Testemunha Um', \
            'contrato editado continua o mesmo'

        # dados para contrato no cadastro do cliente e nos Ajustes (opcionais) entram no contrato
        r = http.put(f'/api/clients/{id_cli}', headers=cab, json={'rep_nome': '  Carlos  Teixeira ', 'rep_cpf': '111.444.777-35',
                                                                   'rep_rg': '1234567 SSP/PR', 'rep_estado_civil': 'Casado'})
        assert r.status_code == 200, r.data
        cli_json = http.get(f'/api/clients/{id_cli}', headers=cab).get_json()
        assert (cli_json['rep_nome'], cli_json['rep_rg'], cli_json['rg']) == ('Carlos Teixeira', '1234567 SSP/PR', None)
        assert any('dados para contrato' in a for a in [x.description for x in _auditoria(app, AuditLog, 'Cliente')])
        http.put(f'/api/clients/{id_pf}', headers=cab, json={'rg': '9.876.543', 'nacionalidade': 'Brasileira', 'estado_civil': 'Solteira'})
        r = http.put('/api/settings/', headers=cab, json={'email': 'contato@exemplo.com', 'repNome': 'Fulano Teste', 'repCpf': '935.411.347-80',
                                                          'pqTelefone': '(45) 3333-4444', 'pqEmail': 'pq@x.com', 'pqRepNome': 'Socio PQ',
                                                          'esc': True, 'pqEsc': 'false'})
        assert r.status_code == 200, r.data
        g = http.get('/api/settings/', headers=cab).get_json()
        assert (g['email'], g['repNome'], g['pqTelefone'], g['pqRepNome'], g['esc'], g['pqEsc']) == \
            ('contato@exemplo.com', 'Fulano Teste', '(45) 3333-4444', 'Socio PQ', True, False)
        op5 = bordero(id_cli, 'Fozesc', (700,))
        d5 = http.get(f'/api/contratos/{op5}', headers=cab).get_json()['dados']
        assert (d5['contratada']['email'], d5['contratada']['rep']['nome'], d5['contratada']['rep']['cpf'], d5['contratada']['esc']) == \
            ('contato@exemplo.com', 'Fulano Teste', '935.411.347-80', True)
        assert (d5['contratante']['rep']['nome'], d5['contratante']['rep']['rg'], d5['contratante']['rep']['estadoCivil']) == \
            ('Carlos Teixeira', '1234567 SSP/PR', 'Casado'), 'representante do cadastro do cliente'
        op6 = bordero(id_pf, 'PQ', (700,))
        d6 = http.get(f'/api/contratos/{op6}', headers=cab).get_json()['sugestao']
        assert (d6['contratada']['telefone'], d6['contratada']['email'], d6['contratada']['rep']['nome'], d6['contratada']['esc']) == \
            ('(45) 3333-4444', 'pq@x.com', 'Socio PQ', False)
        assert (d6['contratante']['rep']['nome'], d6['contratante']['rep']['rg'], d6['contratante']['rep']['nacionalidade']) == \
            ('Joao da Silva', '9.876.543', 'Brasileira'), 'pessoa fisica: RG/nacionalidade do cadastro'

        # apagar o borderô (ultimo titulo) leva o contrato junto
        http.put(f'/api/contratos/{op3}', headers=cab, json={'dados': s3})
        with app.app_context():
            tid = Check.query.filter_by(operation_id=op3).first().id
        r = http.delete(f'/api/checks/{tid}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['bordero_apagado'] is True, r.data
        with app.app_context():
            assert Contrato.query.filter_by(operation_id=op3).count() == 0

        print("OK: contrato montado com empresa/cliente/titulos/encargos (IOF adicional + diario = total), salvo como "
              "editado, proximo contrato lembra representantes e e-mail, pessoa fisica e' a propria representante, "
              "invalidos recusados, lista, auditoria, apagar o borderô leva o contrato.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
