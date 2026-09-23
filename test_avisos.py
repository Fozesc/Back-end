"""
Confere os dois avisos novos do dia a dia:

1. `GET /api/checks/emitentes` - sugestao de emitente pelo que ja existe no banco,
   do mais usado para o menos. Serve para o mesmo emitente nao entrar escrito de
   tres jeitos diferentes (o problema que deu trabalho na importacao da planilha).
2. `vencem_hoje` no dashboard - quantos cheques vencem HOJE. De proposito nao
   acumula o que venceu antes (pedido do Lucas: aviso so do dia).

Roda em um banco SEPARADO (fozesc_teste_avisos), criado e apagado pelo proprio
teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_avisos.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_avisos'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)

# tem que ser ANTES de importar o app: o Config le DATABASE_URL do ambiente
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = None
_db = None


def recria_banco():
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    cur.execute(f'CREATE DATABASE {BANCO_TESTE}')
    cur.close()
    conn.close()


def apaga_banco():
    global _db, _app
    if _db is not None and _app is not None:
        with _app.app_context():
            _db.engine.dispose()
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    cur.close()
    conn.close()


def main():
    global _app, _db
    recria_banco()
    try:
        from app import create_app, db
        from app.models.domain import (Check, Client, CompanySettings, Operation, User)
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()

        hoje = date.today()
        ontem = hoje - timedelta(days=1)
        amanha = hoje + timedelta(days=1)

        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()

            op = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                           operation_date=hoje, total_face_value=0.0,
                           total_interest=0.0, total_net_value=0.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()

            def cheque(venc, emitente, valor=100.0, status='Aguardando', fora=False):
                return Check(operation_id=op.id, due_date=venc, amount=valor,
                             interest_amount=0.0, net_amount=valor, status=status,
                             issuer_name=emitente, number='1', fora_do_calculo=fora)

            db.session.add_all([
                # vencem HOJE e contam: 2 cheques, 300 no total
                cheque(hoje, 'Joao da Silva', 100.0),
                cheque(hoje, 'Joao da Silva', 200.0, status='Prorrogado'),
                # hoje, mas nao contam
                cheque(hoje, 'Joao da Silva', 999.0, status='Pago'),
                cheque(hoje, 'Historico Antigo', 888.0, fora=True),
                # venceu ONTEM: o aviso e' so do dia, entao NAO pode entrar
                cheque(ontem, 'Maria Souza', 500.0),
                cheque(ontem, 'Maria Souza', 500.0),
                # vence amanha
                cheque(amanha, 'Maria Souza', 700.0),
                # emitentes com frequencias diferentes, para conferir a ordem
                cheque(amanha, 'Maria Souza', 10.0),
                cheque(amanha, 'Pedro Antunes', 10.0),
                cheque(amanha, '', 10.0),
                cheque(amanha, None, 10.0),
            ])
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}

        # --- 1. vencem hoje: so o dia, so o que conta ------------------------
        d = http.get('/api/dashboard', headers=cab).get_json()['vencem_hoje']
        assert d['data'] == hoje.strftime('%Y-%m-%d'), d
        assert d['quantidade'] == 2, f"Pago e fora_do_calculo nao entram: {d}"
        assert d['total'] == 300.0, f"os 2 de ontem nao podem entrar: {d}"

        # --- 2. sugestao de emitentes ----------------------------------------
        todos = http.get('/api/checks/emitentes', headers=cab).get_json()
        # Ordem por frequencia (Maria 4x, Joao 3x), empate resolvido pelo nome.
        # 'Historico Antigo' e' de cheque fora_do_calculo e ENTRA de proposito: os
        # 8.254 cheques pagos da planilha sao justamente onde estao os nomes reais
        # de emitente - tira-los esvaziaria a sugestao.
        assert todos == ['Maria Souza', 'Joao da Silva', 'Historico Antigo', 'Pedro Antunes'], todos
        assert '' not in todos and None not in todos, 'nome vazio/nulo nao e sugestao'

        # filtro por trecho, sem diferenciar maiuscula
        assert http.get('/api/checks/emitentes?q=jo', headers=cab).get_json() == ['Joao da Silva']
        assert http.get('/api/checks/emitentes?q=SOUZA', headers=cab).get_json() == ['Maria Souza']
        assert http.get('/api/checks/emitentes?q=zzz', headers=cab).get_json() == []

        # limite: pedido absurdo nao vira lista ilimitada
        assert len(http.get('/api/checks/emitentes?limit=1', headers=cab).get_json()) == 1
        assert len(http.get('/api/checks/emitentes?limit=9999', headers=cab).get_json()) == 4

        # --- 3. exige login ---------------------------------------------------
        assert http.get('/api/checks/emitentes').status_code == 401
        assert http.get('/api/dashboard').status_code == 401

        print("OK: aviso de vencimento so do dia (ignora ontem, Pago e fora do calculo), "
              "sugestao de emitente ordenada por frequencia (historico incluido de "
              "proposito), filtro sem diferenciar maiuscula, vazio/nulo fora, limite "
              "respeitado e rotas exigindo login.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
