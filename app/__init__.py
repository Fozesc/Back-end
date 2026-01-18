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


db = SQLAlchemy()
ma = Marshmallow()
migrate = Migrate()
bcrypt = Bcrypt()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"] 
)

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # * = dominio
    CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5173"]}}) 


    Talisman(app, force_https=False, content_security_policy=None)

    db.init_app(app)
    ma.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    limiter.init_app(app)

    from .models import domain
    from .controllers import register_blueprints
    register_blueprints(app)

    return app