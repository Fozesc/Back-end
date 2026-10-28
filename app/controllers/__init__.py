from functools import wraps
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity


def so_admin(rota):
    """Rota so para Admin. Confere o cargo no BANCO, nao no token: quem perdeu o cargo perde
    o acesso na hora, sem esperar o login vencer."""
    @wraps(rota)
    @jwt_required()
    def protegida(*args, **kwargs):
        from app import db
        from app.models.domain import User
        usuario = db.session.get(User, int(get_jwt_identity()))
        if not usuario or usuario.role != 'Admin':
            return jsonify({'error': 'Só o administrador pode fazer isso'}), 403
        return rota(*args, **kwargs)
    return protegida


def register_blueprints(app):
    # Importações
    from .client_controller import bp as client_bp
    from .operation_controller import bp as operation_bp
    from .check_controller import bp as check_bp
    from .transaction_controller import bp as transaction_bp 
    from .settings_controller import bp as settings_bp
    from .dashboard_controller import bp as dashboard_bp
    from .user_controller import bp as user_bp
    from .auth_controller import bp as auth_bp
    from .audit_controller import bp as audit_bp
    from .report_controller import bp as report_bp
    from .history_controller import bp as history_bp
    from .vale_controller import bp as vale_bp
    from .calendario_controller import bp as calendario_bp
    from .folha_controller import bp as folha_bp
    from .recorrente_controller import bp as recorrente_bp

    # --- CORREÇÃO: ADICIONANDO OS PREFIXOS ---
    # Sem isso, o frontend chama /api/x e o backend não sabe onde está.
    
    app.register_blueprint(client_bp, url_prefix='/api/clients')
    app.register_blueprint(operation_bp, url_prefix='/api/operations')
    
    # AQUI É O QUE ESTAVA DANDO ERRO 404 NOS CHEQUES
    app.register_blueprint(check_bp, url_prefix='/api/checks')
    
    app.register_blueprint(transaction_bp, url_prefix='/api/transactions')
    app.register_blueprint(settings_bp, url_prefix='/api/settings')
    app.register_blueprint(dashboard_bp, url_prefix='/api/dashboard')
    app.register_blueprint(user_bp, url_prefix='/api/users')
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(audit_bp, url_prefix='/api/audit')
    app.register_blueprint(report_bp, url_prefix='/api/reports')
    app.register_blueprint(history_bp, url_prefix='/api/history')
    app.register_blueprint(vale_bp, url_prefix='/api/vales')
    app.register_blueprint(calendario_bp, url_prefix='/api/calendario')
    app.register_blueprint(folha_bp, url_prefix='/api/folhas')
    app.register_blueprint(recorrente_bp, url_prefix='/api/recorrentes')
