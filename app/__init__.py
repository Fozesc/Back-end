from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_marshmallow import Marshmallow
from flask_cors import CORS
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_bcrypt import Bcrypt
from .config import Config
from flask_jwt_extended import JWTManager




db = SQLAlchemy()
ma = Marshmallow()
migrate = Migrate()
bcrypt = Bcrypt()
jwt = JWTManager()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000 per day", "600 per hour"] 
)

@jwt.token_in_blocklist_loader
def check_if_token_revoked(jwt_header, jwt_payload):
    jti = jwt_payload["jti"]
    
   
    from app.models.domain import TokenBlocklist 


    token = db.session.query(TokenBlocklist.id).filter_by(jti=jti).scalar()
    return token is not None
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)



    CORS(app, resources={r"/api/*": {"origins": "*"}}) 

    Talisman(app, force_https=False, content_security_policy=None)

    db.init_app(app)
    ma.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    limiter.init_app(app)
    jwt.init_app(app)
    


    from .models import domain
    from .controllers import register_blueprints
    register_blueprints(app)

 
    with app.app_context():
        db.create_all()
        # ponytail: este projeto nao usa Alembic (nao existe pasta migrations/) e o
        # create_all() cria tabela nova mas NUNCA altera tabela que ja existe. Por isso
        # a coluna nova entra por um ALTER idempotente aqui: roda em todo boot, nao
        # precisa de passo manual no servidor. Se aparecer uma 3a coluna, vale adotar
        # o Flask-Migrate (ja esta instalado) em vez de empilhar ALTERs aqui.
        from sqlalchemy import text
        db.session.execute(text(
            'ALTER TABLE checks ADD COLUMN IF NOT EXISTS fora_do_calculo '
            'BOOLEAN NOT NULL DEFAULT FALSE'
        ))
        db.session.commit()


    return app