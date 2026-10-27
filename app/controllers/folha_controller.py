import re
import unicodedata
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import and_, case, func

from app import db
from app.models.domain import Check, Client, CompanySettings, Folha, Operation, Transaction, Vale

bp = Blueprint('folhas', __name__)
MAX_FOLHAS = 50
MAX_TEXTO = 50000


def _usuario():
    return int(get_jwt_identity())


def _da_pessoa(id):
    return Folha.query.filter_by(id=id, user_id=_usuario()).first_or_404()


def _aplicar(folha, dados):
    if 'titulo' in dados:
        titulo = ' '.join(str(dados.get('titulo') or '').split())[:100]
        if not titulo:
            return 'O título não pode ficar vazio'
        folha.titulo = titulo
    if 'conteudo' in dados:
        conteudo = str(dados.get('conteudo') or '')
        if len(conteudo) > MAX_TEXTO:
            return 'A folha passou do tamanho máximo'
        folha.conteudo = conteudo
    return None


@bp.route('', methods=['GET'])
@jwt_required()
def listar():
    folhas = Folha.query.filter_by(user_id=_usuario()).order_by(Folha.criado_em, Folha.id).all()
    return jsonify([f.to_dict() for f in folhas])


@bp.route('', methods=['POST'])
@jwt_required()
def criar():
    if Folha.query.filter_by(user_id=_usuario()).count() >= MAX_FOLHAS:
        return jsonify({'error': f'Limite de {MAX_FOLHAS} folhas: apague alguma antes'}), 400
    folha = Folha(user_id=_usuario(), titulo='Nova folha', conteudo='')
    erro = _aplicar(folha, request.get_json(silent=True) or {})
    if erro:
        return jsonify({'error': erro}), 400
    db.session.add(folha)
    db.session.commit()
    return jsonify(folha.to_dict()), 201


@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def editar(id):
    folha = _da_pessoa(id)
    erro = _aplicar(folha, request.get_json(silent=True) or {})
    if erro:
        return jsonify({'error': erro}), 400
    db.session.commit()
    return jsonify(folha.to_dict())


@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def apagar(id):
    db.session.delete(_da_pessoa(id))
    db.session.commit()
    return '', 204


def slug(texto):
    """'Banco do Brasil' -> 'banco_do_brasil'. A tela usa a mesma regra no que se digita."""
    texto = unicodedata.normalize('NFD', str(texto or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '_', texto).strip('_')


@bp.route('/refs', methods=['GET'])
@jwt_required()
def refs():
    """Os @valores do sistema que as folhas podem usar, com o valor de agora."""
    lista = []
    add = lambda chave, valor, dica: lista.append({'key': chave, 'value': round(float(valor or 0), 4), 'hint': dica})

    # caixa: a mesma regra de conta do saldo (Fluxo de Caixa)
    from app.services.history_service import bank_key
    saldos = {'BRASIL': 0.0, 'CAIXA': 0.0, 'DINHEIRO': 0.0}
    for origem, valor in db.session.query(Transaction.origin, func.sum(case(
            (Transaction.type == 'entrada', func.abs(Transaction.amount)), else_=-func.abs(Transaction.amount)))
    ).group_by(Transaction.origin):
        saldos[bank_key(origem)] += float(valor or 0)
    add('caixa', sum(saldos.values()), 'saldo de todas as contas')
    add('caixa.dinheiro', saldos['DINHEIRO'], 'saldo do Dinheiro')
    add('caixa.bb', saldos['BRASIL'], 'saldo do Banco do Brasil')
    add('caixa.caixa', saldos['CAIXA'], 'saldo da Caixa Econômica')

    # carteira: o que falta receber (sem o historico fora do calculo), Aguardando vencido = Atrasado
    hoje = date.today()
    situacao = case((and_(Check.status == 'Aguardando', Check.due_date < hoje), 'Atrasado'), else_=Check.status)
    por = dict(db.session.query(situacao, func.sum(Check.amount))
               .filter(Check.status != 'Pago', Check.fora_do_calculo.is_(False)).group_by(situacao).all())
    add('carteira', sum(float(v or 0) for v in por.values()), 'tudo que falta receber')
    for chave, status, dica in (('a_vencer', 'Aguardando', 'a vencer'), ('atrasado', 'Atrasado', 'atrasado'),
                                ('devolvido', 'Devolvido', 'devolvido'), ('juridico', 'Juridico', 'no jurídico')):
        add(f'carteira.{chave}', por.get(status), f'títulos {dica}')

    vale_aberto = db.session.query(func.coalesce(func.sum(Vale.valor), 0.0)).filter(Vale.status == 'Aberto').scalar()
    vale_pago = db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0)).join(
        Vale, Transaction.vale_id == Vale.id).filter(Vale.status == 'Aberto', Transaction.type == 'entrada').scalar()
    add('vales', float(vale_aberto) - float(vale_pago), 'vales em aberto')

    s = CompanySettings.query.first()
    if s:
        add('taxa', s.default_monthly_rate, 'taxa padrão ao mês (%)')
        add('iof', s.iof_rate, 'IOF fixo (%)')
        add('iof_dia', s.iof_daily_rate, 'IOF diário (%)')
        add('multa', s.fine_rate, 'multa de devolução (%)')

    # clientes: em aberto, limite, disponivel e taxa
    aberto = dict(db.session.query(Operation.client_id, func.sum(Check.amount))
                  .join(Check, Check.operation_id == Operation.id)
                  .filter(Check.status != 'Pago', Check.fora_do_calculo.is_(False))
                  .group_by(Operation.client_id).all())
    usados = set()
    for c in Client.query.order_by(Client.name, Client.id).all():
        base = slug(c.name) or 'sem_nome'
        chave, n = base, 2
        while chave in usados:
            chave, n = f'{base}_{n}', n + 1
        usados.add(chave)
        em_aberto = float(aberto.get(c.id) or 0)
        add(f'cliente.{chave}', em_aberto, f'{c.name}: em aberto')
        add(f'cliente.{chave}.limite', c.credit_limit, f'{c.name}: limite de crédito')
        add(f'cliente.{chave}.disponivel', float(c.credit_limit or 0) - em_aberto, f'{c.name}: limite - em aberto')
        add(f'cliente.{chave}.taxa', c.standard_rate, f'{c.name}: taxa (%)')
    return jsonify(lista)
