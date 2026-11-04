from flask import Blueprint, request, jsonify
from app.services.operation_service import OperationService
from flask_jwt_extended import get_jwt, jwt_required
from app.schemas.operation_schema import operation_schema, operations_schema
from app.controllers.contrato_controller import gerar_contrato_se_faltar
from app.controllers import limite_senha, so_admin
from app.services.check_service import CheckService

bp = Blueprint('operations', __name__, url_prefix='/api/operations')
service = OperationService()

@bp.route('', methods=['POST'])
@jwt_required()
def store():
    try:
        new_operation = service.create_operation(request.json)
        contrato = gerar_contrato_se_faltar(new_operation, get_jwt().get('name', 'Sistema'))
        return jsonify({**operation_schema.dump(new_operation), 'contrato_gerado': contrato}), 201

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Erro no Controller: {e}")
        return jsonify({'error': 'Erro interno ao processar borderô'}), 500


@bp.route('', methods=['GET'])
@jwt_required()
def index():
    # Paginado: com o histórico da planilha importado são 4 mil borderôs, e mandar
    # todos de uma vez travava a tela. O formato ({items, total, pages}) é o mesmo
    # que a tela de Borderô já esperava receber.
    resultado = service.get_paginated(
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 20, type=int),
        sort_by=request.args.get('sort_by', 'id'),
        sort_order=request.args.get('sort_order', 'desc'),
    )
    resultado['items'] = operations_schema.dump(resultado['items'])
    return jsonify(resultado)


@bp.route('/client/<int:client_id>', methods=['GET'])
@jwt_required()
def get_by_client(client_id):

    try:
        return jsonify(service.get_by_client(client_id, request.args.get('page', 1, type=int),
                                             request.args.get('per_page', 10, type=int)))
    except Exception:
        return jsonify({'error': 'Não foi possível carregar os borderôs do cliente'}), 500


@bp.route('/<int:op_id>/documento', methods=['GET'])
@jwt_required()
def documento(op_id):
    """Dados do borderô e das promissorias para baixar o PDF de novo (ficha do cliente, titulo, borderô)."""
    dados = service.documento(op_id)
    if dados is None:
        return jsonify({'error': 'Borderô não encontrado'}), 404
    return jsonify(dados)


@bp.route('/<int:op_id>/empresa', methods=['PUT'])
@jwt_required()
def mudar_empresa(op_id):
    """Empresa do borderô (Fozesc ou PQ): corrige os antigos e o que foi lancado na empresa errada."""
    try:
        r = service.mudar_empresa(op_id, (request.get_json(silent=True) or {}).get('empresa'), get_jwt().get('name', 'Sistema'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if r is None:
        return jsonify({'error': 'Borderô não encontrado'}), 404
    from app import db
    from app.models.domain import Operation
    r['contrato_gerado'] = gerar_contrato_se_faltar(db.session.get(Operation, op_id), get_jwt().get('name', 'Sistema'))
    return jsonify(r)


@bp.route('/<int:op_id>/exclusao', methods=['GET'])
@jwt_required()
def previa_exclusao(op_id):
    """O que apagar o borderô inteiro muda no caixa. Nada e' gravado."""
    dados = CheckService().delete_bordero(op_id, simular=True)
    if dados is None:
        return jsonify({'error': 'Borderô não encontrado'}), 404
    return jsonify(dados)


@bp.route('/<int:op_id>', methods=['DELETE'])
@limite_senha
@so_admin
def apagar(op_id):
    """Apaga o borderô e todos os titulos dele, desfazendo no caixa. Exige a senha no corpo."""
    try:
        dados = CheckService().delete_bordero(op_id, (request.get_json(silent=True) or {}).get('senha'))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    if dados is None:
        return jsonify({'error': 'Borderô não encontrado'}), 404
    return jsonify(dados)
