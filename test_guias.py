"""
Guias (versao 2.12): passo a passo de cada tarefa, com sites e anotacoes, um dentro do outro.

  - sem login nao ve nada; qualquer pessoa logada cria, edita e apaga;
  - site sem "https://" ganha; javascript:, data:, ftp: e lixo sao recusados (o link vira <a href> na tela);
  - guia dentro de guia: a lista mostra quantos guias/passos/sites tem cada um, o caminho volta certo;
  - anotacoes em blocos (nota, atencao, dica), bloco vazio sai, tipo desconhecido vira nota;
  - busca no titulo, nos passos, nas anotacoes e nos sites, com acento; "%" e "_" sao letra, nao curinga;
  - titulo vazio, passos demais, guia de cima inexistente: recusado sem gravar;
  - guia com outros dentro nao se apaga; apagar grava o retrato na auditoria.

Banco SEPARADO (fozesc_teste_guias), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_guias.py
"""
import os
import re

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_guias'
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
        from app.models.domain import AuditLog, Guia, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Operador', email='op@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Operador', active=True))
            db.session.commit()

        G = '/api/guias'
        assert http.get(G).status_code == 401
        assert http.post(G, json={'titulo': 'x'}).status_code == 401
        r = http.post('/api/auth/login', json={'email': 'op@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        contar = lambda: _contar(app, Guia)

        # ------------------------------------------------ invalidos: nada gravado
        for ruim, trecho in (({'titulo': '  '}, 'título'),
                             ({'titulo': 'NF', 'links': [{'url': 'javascript:alert(1)'}]}, 'Site inválido'),
                             ({'titulo': 'NF', 'links': [{'url': 'data:text/html,<script>x</script>'}]}, 'Site inválido'),
                             ({'titulo': 'NF', 'links': [{'url': 'ftp://arquivos.gov.br'}]}, 'Site inválido'),
                             ({'titulo': 'NF', 'links': [{'url': 'https://'}]}, 'Site inválido'),
                             ({'titulo': 'NF', 'links': [{'url': 'nfse gov br'}]}, 'Site inválido'),
                             ({'titulo': 'NF', 'passos': ['a'] * 101}, 'passos'),
                             ({'titulo': 'NF', 'passos': ['a' * 2001]}, 'passo'),
                             ({'titulo': 'NF', 'notas': [{'texto': 'a'}] * 51}, 'anotações'),
                             ({'titulo': 'NF', 'notas': [{'texto': 'a' * 5001}]}, 'anotação'),
                             ({'titulo': 'NF', 'pai_id': 999}, 'não existe'),
                             ({'titulo': 'NF', 'pai_id': True}, 'não existe')):
            r = http.post(G, headers=cab, json=ruim)
            assert r.status_code == 400 and trecho in r.get_json()['error'], (ruim, r.data)
        assert contar() == 0

        # ------------------------------------------------ guia com guias dentro
        r = http.post(G, headers=cab, json={'titulo': '  Nota   fiscal ', 'links': [
            {'titulo': 'NFS-e Foz', 'url': 'nfse.pmfi.pr.gov.br/login'}, {'titulo': '', 'url': '  '}]})
        assert r.status_code == 201, r.data
        nf = r.get_json()
        assert nf['titulo'] == 'Nota fiscal' and nf['links'] == [{'titulo': 'NFS-e Foz', 'url': 'https://nfse.pmfi.pr.gov.br/login'}]
        assert nf['caminho'] == [] and nf['filhos'] == [] and nf['atualizado_por'] == 'Operador'

        r = http.post(G, headers=cab, json={'pai_id': nf['id'], 'titulo': 'Para cliente',
                                            'passos': ['Entrar no site', '', '  Emitir com o CNPJ do cliente  '],
                                            'notas': [{'tipo': 'atencao', 'titulo': ' Código ', 'texto': 'Serviço: 17.01\nAlíquota 2%_x'},
                                                      {'tipo': '<b>', 'texto': 'Emissão até o dia 5'},
                                                      {'tipo': 'dica', 'titulo': ' ', 'texto': ' '}, 'lixo']})
        assert r.status_code == 201, r.data
        cli = r.get_json()
        assert cli['passos'] == ['Entrar no site', 'Emitir com o CNPJ do cliente'], 'passo vazio sai'
        assert cli['notas'] == [{'tipo': 'atencao', 'titulo': 'Código', 'texto': 'Serviço: 17.01\nAlíquota 2%_x'},
                                {'tipo': 'nota', 'titulo': '', 'texto': 'Emissão até o dia 5'}], cli['notas']
        r = http.post(G, headers=cab, json={'pai_id': cli['id'], 'titulo': 'Cliente sem CNPJ'})
        neto = r.get_json()
        assert r.status_code == 201 and neto['caminho'] == [{'id': nf['id'], 'titulo': 'Nota fiscal'},
                                                            {'id': cli['id'], 'titulo': 'Para cliente'}]
        http.post(G, headers=cab, json={'pai_id': nf['id'], 'titulo': 'para mim'})

        raiz = http.get(G, headers=cab).get_json()['filhos']
        assert raiz == [{'id': nf['id'], 'titulo': 'Nota fiscal', 'passos': 0, 'links': 1, 'guias': 2}], raiz
        dentro = http.get(f"{G}/{nf['id']}", headers=cab).get_json()['filhos']
        assert [(f['titulo'], f['passos'], f['guias']) for f in dentro] == [('Para cliente', 2, 1), ('para mim', 0, 0)], \
            'ordem alfabetica sem ligar para maiuscula'

        # ------------------------------------------------ editar
        r = http.put(f"{G}/{cli['id']}", headers=cab, json={'passos': ['Entrar', 'Emitir', 'Mandar o PDF']})
        assert r.status_code == 200 and r.get_json()['passos'] == ['Entrar', 'Emitir', 'Mandar o PDF']
        assert r.get_json()['titulo'] == 'Para cliente' and r.get_json()['notas'][0]['texto'].startswith('Serviço'), 'o resto fica'
        r = http.put(f"{G}/{cli['id']}", headers=cab, json={'titulo': 'Para cliente', 'links': [{'url': 'vbscript:x'}]})
        assert r.status_code == 400
        with app.app_context():
            assert db.session.get(Guia, cli['id']).links == [], 'site recusado nao grava nada'
        assert http.put(f'{G}/999', headers=cab, json={'titulo': 'x'}).status_code == 404

        # ------------------------------------------------ busca
        busca = lambda q: [g['titulo'] for g in http.get(G + '/busca', headers=cab, query_string={'q': q}).get_json()]
        assert busca('CLIENTE') == ['Cliente sem CNPJ', 'Para cliente']
        assert busca('mandar o pdf') == ['Para cliente'], 'acha no passo'
        assert busca('17.01') == ['Para cliente'], 'acha nas anotacoes'
        assert busca('emissão até') == ['Para cliente'] and busca('código') == ['Para cliente'], 'acha com acento'
        assert busca('pmfi') == ['Nota fiscal'], 'acha no site'
        assert busca('2%_x') == ['Para cliente'] and busca('%') == [] and busca('_') == [], '% e _ nao sao curinga'
        r = http.get(G + '/busca', headers=cab, query_string={'q': 'sem cnpj'}).get_json()
        assert r == [{'id': neto['id'], 'titulo': 'Cliente sem CNPJ', 'onde': 'Nota fiscal › Para cliente'}]

        # ------------------------------------------------ apagar
        r = http.delete(f"{G}/{nf['id']}", headers=cab)
        assert r.status_code == 400 and 'dentro' in r.get_json()['error']
        assert http.delete(f"{G}/{neto['id']}", headers=cab).status_code == 204
        assert http.delete(f"{G}/{cli['id']}", headers=cab).status_code == 204
        with app.app_context():
            ultimo = AuditLog.query.filter_by(target='Guias', action='DELETE').order_by(AuditLog.id.desc()).first()
            assert 'Para cliente' in ultimo.description and 'Mandar o PDF' in ultimo.description and '17.01' in ultimo.description
            assert ultimo.user_name == 'Operador'
        assert contar() == 2

        print("OK: guias - login obrigatorio, site so http/https (javascript:/data:/ftp: recusados, sem https ganha), "
              "guia dentro de guia com contagens e caminho, ordem alfabetica, editar so o que veio, busca no titulo/"
              "passos/anotacoes/sites sem curinga, invalidos sem gravar, guia com outros dentro nao se apaga, retrato "
              "na auditoria.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


def _contar(app, Guia):
    with app.app_context():
        return Guia.query.count()


if __name__ == '__main__':
    main()
