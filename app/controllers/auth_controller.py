from flask import Blueprint, request, jsonify
from app.services.auth_service import AuthService
from app.services.audit_service import AuditService
from app.schemas.user_schema import user_schema
from flask_jwt_extended import create_access_token, jwt_required, get_jwt
from app.models.domain import TokenBlocklist 
from app import db, limiter

# por IP (o nginx manda o IP real) e so conta a senha ERRADA: ninguem fica testando senha a
# vontade, e o escritorio entrando normal nunca esbarra
LIMITE_LOGIN = '10 per minute;30 per hour'

bp = Blueprint('auth', __name__, url_prefix='/api/auth')
service = AuthService()
audit_service = AuditService()

@bp.route('/login', methods=['POST'])
@limiter.limit(LIMITE_LOGIN, deduct_when=lambda resposta: resposta.status_code == 401,
               error_message='Muitas tentativas de login. Espere alguns minutos e tente de novo.')
def login():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Email e senha são obrigatórios'}), 400

    user = service.login(email, password)

    if user:

        access_token = create_access_token(
            identity=str(user.id), 
            additional_claims={"role": user.role, "name": user.name}
        )

        audit_service.log_action(
            user_name=user.name,
            action='LOGIN',
            target='Sistema',
            description=f'Usuário {user.email} realizou login.'
        )

        return jsonify({
            'message': 'Login realizado com sucesso',
            'user': user_schema.dump(user),
            'token': access_token 
        })
    else:
        audit_service.log_action(
            user_name='Desconhecido',
            action='NEGADO',
            target='Login',
            description=f'Tentativa de login com senha errada ou usuário inexistente: {str(email)[:100]}'
        )
        return jsonify({'error': 'Credenciais inválidas'}), 401
    
@bp.route('/logout', methods=['POST'])
@jwt_required()
def logout():

    claims = get_jwt()
    jti = claims["jti"] 
    user_name = claims.get('name', 'Usuário Desconhecido')


    try:

        blocked_token = TokenBlocklist(jti=jti)
        db.session.add(blocked_token)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Erro ao encerrar sessão'}), 500

    
    audit_service.log_action(
        user_name=user_name,
        action='LOGOUT',
        target='Sistema',
        description=f'O usuário {user_name} encerrou a sessão e o token foi invalidado.'
    )
    
    return jsonify({'message': 'Logout realizado com sucesso (Token invalidado)'}), 200