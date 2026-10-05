"""
Confere os filtros da tela Titulos (GET /api/checks/):

1. as datas De/Ate valem ao pe da letra - vencido, Juridico e Devolvido so aparecem
   se o vencimento estiver no periodo escolhido;
2. "Aguardando" so traz o que ainda nao venceu (o vencido aparece como Atrasado);
3. "Atrasado" traz o Aguardando vencido;
4. o resumo (Total do filtro) e a acao em lote usam exatamente os mesmos filtros;
5. ninguem pede mais de 100 por pagina.

Roda em um banco SEPARADO (fozesc_teste_filtros), criado e apagado pelo proprio
teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_filtros_titulos.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_filtros'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = None
_db = None


def sql_admin(comando):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(comando)
    cur.close()
    conn.close()


def apaga_banco():
    if _db is not None and _app is not None:
        with _app.app_context():
            _db.engine.dispose()
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    sql_admin(f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import Check, Client, CompanySettings, Operation, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        hoje = date.today()
        d = lambda n: hoje + timedelta(days=n)

        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()
            op = Operation(client_id=cli.id, client_name_snapshot=cli.name, operation_date=d(-300),
                           total_face_value=0.0, total_interest=0.0, total_net_value=0.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()
            op_id = op.id
            ids = {}
            for nome, venc, status in (('a_vencer', d(10), 'Aguardando'),
                                       ('vencido', d(-30), 'Aguardando'),
                                       ('juridico', d(-200), 'Juridico'),
                                       ('devolvido', d(-50), 'Devolvido'),
                                       ('pago', d(-20), 'Pago'),
                                       ('longe', d(60), 'Aguardando')):
                c = Check(operation_id=op.id, due_date=venc, amount=100.0, interest_amount=0.0,
                          net_amount=100.0, status=status, issuer_name=nome, number=nome)
                db.session.add(c)
                db.session.flush()
                ids[nome] = c.id
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}
        nome_do = {v: k for k, v in ids.items()}

        def lista(**filtros):
            q = '&'.join(f'{k}={v}' for k, v in filtros.items())
            r = http.get(f'/api/checks/?per_page=40&{q}', headers=cab)
            assert r.status_code == 200, r.data
            corpo = r.get_json()
            achados = {nome_do[i['id']] for i in corpo['items']}
            assert corpo['resumo']['qtd'] == len(achados) == corpo['total'], (filtros, corpo['resumo'])
            return achados

        em_aberto = 'Aguardando,Atrasado,Devolvido,Juridico'

        assert lista() == set(ids)
        assert lista(status='Aguardando') == {'a_vencer', 'longe'}
        assert lista(status='Atrasado') == {'vencido'}
        assert lista(status='Aguardando,Atrasado') == {'a_vencer', 'vencido', 'longe'}
        assert lista(status=em_aberto) == {'a_vencer', 'vencido', 'juridico', 'devolvido', 'longe'}

        assert lista(date_start=hoje.isoformat()) == {'a_vencer', 'longe'}
        assert lista(date_start=d(30).isoformat(), date_end=d(90).isoformat()) == {'longe'}
        assert lista(date_start=d(-60).isoformat(), date_end=d(-10).isoformat(),
                     status=em_aberto) == {'vencido', 'devolvido'}
        assert lista(date_end=d(-100).isoformat()) == {'juridico'}

        r = http.get(f'/api/checks/?per_page=40&status={em_aberto}', headers=cab).get_json()
        assert {s['status']: s['qtd'] for s in r['resumo']['por_status']} == \
            {'Aguardando': 2, 'Atrasado': 1, 'Juridico': 1, 'Devolvido': 1}, r['resumo']
        assert {i['id']: i['status'] for i in r['items']}[ids['vencido']] == 'Atrasado'

        r = http.patch('/api/checks/calculo', headers=cab,
                       json={'fora': True, 'filtros': {'date_start': hoje.isoformat()}})
        assert r.status_code == 200 and r.get_json()['alterados'] == 2, r.data
        assert lista(calculo='fora') == {'a_vencer', 'longe'}
        http.patch('/api/checks/calculo', headers=cab, json={'fora': False, 'ids': list(ids.values())})
        assert lista(calculo='fora') == set()

        with app.app_context():
            db.session.add_all([Check(operation_id=op_id, due_date=d(-400), amount=1.0, interest_amount=0.0,
                                      net_amount=1.0, status='Pago', issuer_name='extra', number=str(n))
                                for n in range(105)])
            db.session.commit()
        r = http.get('/api/checks/?per_page=100000', headers=cab).get_json()
        assert len(r['items']) == 100 and r['total'] == len(ids) + 105, (len(r['items']), r['total'])

        print("OK: datas De/Ate obedecidas ao pe da letra (vencido, Juridico e Devolvido so "
              "entram se estiverem no periodo), Aguardando sem os vencidos, Atrasado com o "
              "Aguardando vencido, resumo e acao em lote com os mesmos filtros, e no maximo "
              "100 por pagina.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
