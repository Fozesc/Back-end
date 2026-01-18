def register_blueprints(app):
    from .client_controller import bp as client_bp
    from .operation_controller import bp as operation_bp 
    
    app.register_blueprint(client_bp)
    app.register_blueprint(operation_bp) 