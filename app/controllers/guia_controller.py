from datetime import datetime
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, jwt_required
from sqlalchemy import Text, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from app import db
from app.models.domain import Guia
from app.services.audit_service import AuditService
from app.utils.validacao import ler_texto

bp = Blueprint('guias', __name__)
audit = AuditService()
MAX_GUIAS = 2000
MAX_LINKS, MAX_PASSOS = 20, 100
MAX_PASSO = 2000
MAX_NOTAS, MAX_NOTA = 50, 5000
TIPOS_NOTA = ('nota', 'atencao', 'dica')


def _quem():
    return get_jwt().get('name', 'Sistema')


def _url(texto):
    """So http/https: o link vira um <a href> na tela, entao "javascript:..." nunca pode passar."""
    url = str(texto or '').strip()
    if '://' not in url:
        url = 'https://' + url
    try:
        partes = urlsplit(url)
        partes.port
    except ValueError:
        partes = None
    if not partes or partes.scheme.lower() not in ('http', 'https') or not partes.hostname \
            or len(url) > 500 or any(c.isspace() for c in url):
        raise ValueError(f'Site inválido: {str(texto)[:80]}')
    return url


def _ler(guia, dados):
    if 'titulo' in dados or not guia.titulo:
        guia.titulo = ler_texto(dados.get('titulo'), 'o título', 120, obrigatorio=True)
    if 'links' in dados:
        links = dados.get('links') or []
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise ValueError(f'No máximo {MAX_LINKS} sites por guia')
        guia.links = [{'titulo': ler_texto(l.get('titulo'), 'o nome do site', 100) or '', 'url': _url(l.get('url'))}
                      for l in links if isinstance(l, dict) and str(l.get('url') or '').strip()]
    if 'passos' in dados:
        passos = dados.get('passos') or []
        if not isinstance(passos, list):
            raise ValueError('Passo a passo inválido')
        passos = [str(p).strip() for p in passos if str(p or '').strip()]
        if len(passos) > MAX_PASSOS:
            raise ValueError(f'No máximo {MAX_PASSOS} passos por guia')
        if any(len(p) > MAX_PASSO for p in passos):
            raise ValueError(f'Cada passo pode ter no máximo {MAX_PASSO} letras')
        guia.passos = passos
    if 'notas' in dados:
        notas = dados.get('notas') or []
        if not isinstance(notas, list) or len(notas) > MAX_NOTAS:
            raise ValueError(f'No máximo {MAX_NOTAS} anotações por guia')
        lista = []
        for n in notas:
            if not isinstance(n, dict):
                continue
            titulo = ler_texto(n.get('titulo'), 'o título da anotação', 100) or ''
            texto = str(n.get('texto') or '').strip()
            if len(texto) > MAX_NOTA:
                raise ValueError(f'Cada anotação pode ter no máximo {MAX_NOTA} letras')
            if titulo or texto:
                lista.append({'tipo': n.get('tipo') if n.get('tipo') in TIPOS_NOTA else 'nota',
                              'titulo': titulo, 'texto': texto})
        guia.notas = lista
    guia.atualizado_por = str(_quem())[:100]
    guia.atualizado_em = datetime.now()


def _caminho(guia):
    caminho, pai = [], guia.pai_id
    while pai and len(caminho) < 50:
        p = db.session.get(Guia, pai)
        if not p:
            break
        caminho.insert(0, {'id': p.id, 'titulo': p.titulo})
        pai = p.pai_id
    return caminho


def _filhos(pai_id):
    neto = aliased(Guia)
    netos = db.session.query(func.count(neto.id)).filter(neto.pai_id == Guia.id).correlate(Guia).scalar_subquery()
    linhas = (db.session.query(Guia.id, Guia.titulo, func.json_array_length(Guia.passos),
                               func.json_array_length(Guia.links), netos)
              .filter(Guia.pai_id.is_(None) if pai_id is None else Guia.pai_id == pai_id)
              .order_by(func.lower(Guia.titulo), Guia.id).limit(500).all())
    return [{'id': i, 'titulo': t, 'passos': p or 0, 'links': l or 0, 'guias': n} for i, t, p, l, n in linhas]


def _completo(guia):
    return {'id': guia.id, 'pai_id': guia.pai_id, 'titulo': guia.titulo, 'links': guia.links or [],
            'passos': guia.passos or [], 'notas': guia.notas or [],
            'atualizado_por': guia.atualizado_por,
            'atualizado_em': guia.atualizado_em.strftime('%d/%m/%Y %H:%M') if guia.atualizado_em else None,
            'caminho': _caminho(guia), 'filhos': _filhos(guia.id)}


@bp.route('', methods=['GET'])
@jwt_required()
def raiz():
    return jsonify({'filhos': _filhos(None)})


@bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def detalhe(id):
    return jsonify(_completo(db.get_or_404(Guia, id)))


@bp.route('/busca', methods=['GET'])
@jwt_required()
def busca():
    q = ' '.join(request.args.get('q', '', type=str).split())[:100]
    if len(q) < 2:
        return jsonify([])
    # jsonb::text devolve "ç" como ç (o json guarda \u00e7, e a busca por "emissão" nao achava)
    texto = lambda coluna: cast(cast(coluna, JSONB), Text)
    achados = (Guia.query.filter(or_(*(campo.icontains(q, autoescape=True) for campo in
                                       (Guia.titulo, texto(Guia.passos), texto(Guia.notas), texto(Guia.links)))))
               .order_by(func.lower(Guia.titulo), Guia.id).limit(30).all())
    return jsonify([{'id': g.id, 'titulo': g.titulo, 'onde': ' › '.join(c['titulo'] for c in _caminho(g))}
                    for g in achados])


@bp.route('', methods=['POST'])
@jwt_required()
def criar():
    dados = request.get_json(silent=True) or {}
    pai_id = dados.get('pai_id')
    if pai_id is not None and (not isinstance(pai_id, int) or isinstance(pai_id, bool) or not db.session.get(Guia, pai_id)):
        return jsonify({'error': 'O guia de cima não existe mais'}), 400
    if Guia.query.count() >= MAX_GUIAS:
        return jsonify({'error': f'Limite de {MAX_GUIAS} guias'}), 400
    guia = Guia(pai_id=pai_id)
    try:
        _ler(guia, dados)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db.session.add(guia)
    db.session.commit()
    audit.log_action(_quem(), 'CREATE', 'Guias', f'Criou o guia #{guia.id}: {guia.titulo}')
    return jsonify(_completo(guia)), 201


@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def editar(id):
    guia = db.get_or_404(Guia, id)
    antes = guia.titulo
    try:
        _ler(guia, request.get_json(silent=True) or {})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    db.session.commit()
    audit.log_action(_quem(), 'UPDATE', 'Guias', f'Editou o guia #{guia.id}: {antes}'
                     + (f' -> {guia.titulo}' if antes != guia.titulo else ''))
    return jsonify(_completo(guia))


@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def apagar(id):
    guia = db.get_or_404(Guia, id)
    if Guia.query.filter_by(pai_id=id).first():
        return jsonify({'error': 'Este guia tem outros guias dentro: apague os de dentro primeiro'}), 400
    retrato = (f'Apagou o guia #{guia.id}: {guia.titulo} | sites: '
               f"{', '.join(l['url'] for l in guia.links or []) or '-'} | passos: {' / '.join(guia.passos or []) or '-'}"
               f" | anotações: {' / '.join(n['titulo'] + ': ' + n['texto'] for n in guia.notas or []) or '-'}")[:4000]
    db.session.delete(guia)
    db.session.commit()
    audit.log_action(_quem(), 'DELETE', 'Guias', retrato)
    return '', 204
