"""
Reputacao do cliente (nota pelo historico de pagamento) e risco por emitente.

Reputacao: cada titulo que ja venceu vale pontos (em dia 100, prorrogado 70, pago atrasado 50,
devolvido e depois pago 40, vencido em aberto 20, devolvido/juridico 0) e a nota e' a media
pesada pelo valor de face. A vencer nao conta; ate 3 dias depois do vencimento ainda e' em dia.
Menos de 3 titulos avaliados = "Sem histórico". Devolvido/juridico em aberto = no maximo Regular.

Risco por emitente: quem concentra mais dinheiro em aberto e quem mais deu problema, com o nome
comparado sem maiuscula e sem espaco sobrando.

Banco SEPARADO (fozesc_teste_reputacao), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_reputacao.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_reputacao'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = _db = None
HOJE = date.today()
D = lambda n: HOJE + timedelta(days=n)


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
        from app.models.domain import Check, CheckExtension, Client, CompanySettings, Operation, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Operador', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            clientes = {n: Client(name=n) for n in ('Ana', 'Bruno', 'Carla', 'Davi', 'Elisa')}
            db.session.add_all(clientes.values())
            db.session.flush()
            ops = {}
            for n, c in clientes.items():
                ops[n] = Operation(client_id=c.id, operation_date=D(-60), monthly_rate=4)
                db.session.add(ops[n])
            db.session.flush()

            def ch(cliente, valor, venc, status='Aguardando', pago_em=None, emitente='Fulano', **kw):
                c = Check(operation_id=ops[cliente].id, amount=valor, due_date=D(venc), original_due_date=D(venc),
                          status=status, payment_date=D(pago_em) if pago_em is not None else None,
                          issuer_name=emitente, **kw)
                db.session.add(c)
                return c

            # Ana: um de cada
            ch('Ana', 1000, -30, 'Pago', -27, emitente='Joao Bom')               # em dia (3 dias de folga)
            ch('Ana', 1000, -20, 'Pago', -10)                                    # pago 10 dias atrasado
            prorrogado = ch('Ana', 1000, -15, 'Pago', -15)                       # prorrogado e pago
            ch('Ana', 500, -12, 'Devolvido', emitente='Maria Ruim')              # devolvido
            ch('Ana', 200, -2, emitente='Maria Ruim')                            # venceu ha 2 dias: ainda nao conta
            ch('Ana', 500, -10, emitente='  maria ruim ')                        # vencido em aberto
            ch('Ana', 300, 10, emitente='Joao Bom')                              # a vencer
            ch('Ana', 1000, -40, 'Pago', -30, fine_amount=50)                    # devolvido e depois pago
            # Bruno: tudo em dia (um deles e' historico fora do calculo, e conta)
            for v in (-30, -20, -10):
                ch('Bruno', 100, v, 'Pago', v, emitente='Joao Bom', fora_do_calculo=(v == -30))
            # Carla: tudo em dia, mas um titulo pequeno no juridico
            for v in (-30, -20, -10):
                ch('Carla', 100, v, 'Pago', v - 1)
            ch('Carla', 10, -50, 'Juridico', emitente='Maria Ruim')
            # Davi: so 2 titulos vencidos = pouco historico
            ch('Davi', 100, -30, 'Pago', -30)
            ch('Davi', 100, -20, 'Pago', -20)
            db.session.flush()
            db.session.add(CheckExtension(check_id=prorrogado.id, old_due_date=D(-25), new_due_date=D(-15),
                                          days_added=10, fee_amount=10))
            db.session.commit()
            ids = {n: c.id for n, c in clientes.items()}

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        # ------------------------------------------------------------ reputacao
        lista = {c['name']: c for c in http.get('/api/clients', headers=cab).get_json()['items']}
        ana = lista['Ana']['reputacao']
        assert ana['contagem'] == {'em_dia': 1, 'atrasado': 1, 'prorrogado': 1, 'devolvido': 1, 'vencido': 1,
                                   'devolvido_pago': 1, 'juridico': 0, 'a_vencer': 2}, ana['contagem']
        # (1000*100 + 1000*50 + 1000*70 + 500*0 + 500*20 + 1000*40) / 5000 = 54
        assert (ana['nota'], ana['nivel'], ana['avaliados'], ana['atraso_medio_dias']) == (54, 'Regular', 6, 10), ana
        assert (lista['Ana']['valor_em_aberto'], lista['Ana']['cheques_ativos'], lista['Ana']['cheques_totais']) == (1500, 4, 8), \
            'em aberto do limite de credito continua o mesmo de antes'
        bruno = lista['Bruno']['reputacao']
        assert (bruno['nota'], bruno['nivel'], bruno['avaliados']) == (100, 'Excelente', 3), 'historico fora do calculo conta'
        carla = lista['Carla']['reputacao']
        assert carla['nota'] == 97 and carla['nivel'] == 'Regular', 'juridico em aberto segura em Regular'
        assert lista['Davi']['reputacao']['nivel'] == 'Sem histórico' and lista['Davi']['reputacao']['nota'] is None
        assert lista['Elisa']['reputacao']['nivel'] == 'Sem histórico' and lista['Elisa']['cheques_totais'] == 0

        ficha = http.get(f"/api/clients/{ids['Ana']}", headers=cab).get_json()
        assert ficha['reputacao'] == ana, 'a ficha mostra a mesma reputacao da lista'
        assert (ficha['valor_em_aberto'], ficha['valor_devolvido'], ficha['cheques_totais']) == (1500, 500, 8), ficha

        # borderôs da ficha em paginas
        with app.app_context():
            for i in range(12):
                db.session.add(Operation(client_id=ids['Elisa'], operation_date=D(-i), monthly_rate=4))
            db.session.commit()
        B = f"/api/operations/client/{ids['Elisa']}"
        p1 = http.get(B, headers=cab).get_json()
        assert (len(p1['items']), p1['total'], p1['pages'], p1['page']) == (10, 13, 2, 1), p1   # 12 + o do comeco do teste
        assert p1['items'][0]['date'] == D(0).isoformat() and p1['items'][-1]['date'] == D(-9).isoformat(), 'mais novo primeiro'
        p2 = http.get(B + '?page=2', headers=cab).get_json()
        assert len(p2['items']) == 3 and not {o['id'] for o in p1['items']} & {o['id'] for o in p2['items']}
        assert len(http.get(B + '?per_page=999', headers=cab).get_json()['items']) == 13, 'teto 50, cabem os 13'
        ana_ops = http.get(f"/api/operations/client/{ids['Ana']}", headers=cab).get_json()['items']
        assert len(ana_ops) == 1 and len(ana_ops[0]['cheques']) == 8, 'titulos vem junto com o borderô'

        # ------------------------------------------- endereco do cadastro e' gravado
        end = {'name': 'Fabio', 'address': 'Rua Almirante Barroso, 100', 'neighborhood': 'Centro',
               'city': 'Foz do Iguaçu', 'state': 'pr', 'zip_code': '85851-010', 'email': 'fabio@x.com'}
        r = http.post('/api/clients', headers=cab, json=end)
        assert r.status_code in (200, 201), r.data
        fid = next(c['id'] for c in http.get('/api/clients?search=Fabio', headers=cab).get_json()['items'])
        ficha_f = http.get(f'/api/clients/{fid}', headers=cab).get_json()
        assert (ficha_f['neighborhood'], ficha_f['city'], ficha_f['state'], ficha_f['zip_code']) == \
            ('Centro', 'Foz do Iguaçu', 'PR', '85851-010'), ficha_f
        assert http.put(f'/api/clients/{fid}', headers=cab, json={'city': 'Joinville', 'state': 'sc'}).status_code == 200
        ficha_f = http.get(f'/api/clients/{fid}', headers=cab).get_json()
        assert (ficha_f['city'], ficha_f['state'], ficha_f['address']) == ('Joinville', 'SC', 'Rua Almirante Barroso, 100'), \
            'edita so o que veio (UF em maiuscula) e o resto fica'
        da_lista = next(c for c in http.get('/api/clients?search=Fabio', headers=cab).get_json()['items'])
        assert da_lista['neighborhood'] == 'Centro', 'a lista traz o endereco (a edicao abre preenchida)'

        # ------------------------------------------------------- risco por emitente
        R = '/api/checks/emitentes/risco'
        assert http.get(R).status_code == 401
        risco = http.get(R, headers=cab).get_json()
        assert risco['total_em_aberto'] == 1510, risco
        maria = risco['problemas'][0]
        assert (maria['emitente'].strip().upper(), maria['devolvidos'], maria['vencidos'], maria['juridico'],
                maria['em_aberto'], maria['clientes']) == ('MARIA RUIM', 1, 2, 1, 1210, 2), maria
        assert [m['emitente'].strip().upper() for m in risco['problemas']] == ['MARIA RUIM'], 'quem nunca deu problema nao entra'
        assert risco['maiores'][0]['emitente'].strip().upper() == 'MARIA RUIM' and risco['maiores'][0]['pct'] == 80.1
        assert [m['emitente'] for m in risco['maiores']][1] == 'Joao Bom'
        um = http.get(R, headers=cab, query_string={'nome': '  MARIA   ruim'}).get_json()
        assert um['devolvidos'] == 1 and um['titulos_abertos'] == 4, um
        assert http.get(R, headers=cab, query_string={'nome': 'Ninguem'}).get_json() is None
        assert http.get(R, headers=cab, query_string={'nome': ''}).get_json() is None

        print("OK: endereco do cliente gravado e editado, ficha com totais e borderos em paginas, reputacao (em dia, atrasado, prorrogado, devolvido, vencido com 3 dias de folga, devolvido e pago, "
              "media pelo valor, pouco historico, juridico segura em Regular, historico fora do calculo conta), "
              "em aberto do cliente igual ao de antes, e risco por emitente (concentracao, problemas, nome sem "
              "maiuscula/espaco, um emitente so).")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
