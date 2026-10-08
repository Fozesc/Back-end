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
        # coluna nova entra por ALTER idempotente aqui: roda em todo boot e nao precisa
        # de passo manual no servidor (o deploy e' so `docker compose pull && up -d`).
        # So aditivo (ADD COLUMN IF NOT EXISTS). Adotar o Flask-Migrate (ja instalado)
        # exige carimbar o banco do servidor e rodar upgrade no deploy - vale quando
        # precisar de algo alem de coluna nova (renomear, mudar tipo, apagar).
        from sqlalchemy import text
        for comando in (
            'ALTER TABLE checks ADD COLUMN IF NOT EXISTS fora_do_calculo '
            'BOOLEAN NOT NULL DEFAULT FALSE',
            # liga a linha do caixa ao cheque. ON DELETE SET NULL para apagar um cheque
            # nao travar na FK nem apagar o historico do caixa.
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS check_id INTEGER '
            'REFERENCES checks(id) ON DELETE SET NULL',
            # prorrogacao com recebimento parcial
            'ALTER TABLE checks ADD COLUMN IF NOT EXISTS original_amount DOUBLE PRECISION',
            'ALTER TABLE checks ADD COLUMN IF NOT EXISTS juros_pendentes '
            'DOUBLE PRECISION NOT NULL DEFAULT 0',
            'ALTER TABLE check_extensions ADD COLUMN IF NOT EXISTS detalhe JSON',
            # imposto (juros + IOF) cobrado a mais no Receber
            'ALTER TABLE checks ADD COLUMN IF NOT EXISTS imposto_cobrado '
            'DOUBLE PRECISION NOT NULL DEFAULT 0',
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS troca_id INTEGER',
            'CREATE INDEX IF NOT EXISTS ix_transactions_troca_id ON transactions (troca_id)',
            # pagamento parcial de vale: cada pagamento e' uma linha do caixa ligada ao vale
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS vale_id INTEGER '
            'REFERENCES vales(id) ON DELETE SET NULL',
            'CREATE INDEX IF NOT EXISTS ix_transactions_vale_id ON transactions (vale_id)',
            # vale passou a ter so a descricao (o "para quem" ficou so nos antigos)
            'ALTER TABLE vales ALTER COLUMN pessoa DROP NOT NULL',
            # comissao do borderô (parte dos juros que vai para alguem da empresa)
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS comissao DOUBLE PRECISION NOT NULL DEFAULT 0',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS comissao_valor DOUBLE PRECISION NOT NULL DEFAULT 0',
            # comissao sai do caixa no dia, junto com a saida do borderô / prorrogacao
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS grupo_id INTEGER',
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS valor_informativo DOUBLE PRECISION',
        ):
            db.session.execute(text(comando))
        # Contas do caixa com um nome so (Dinheiro / BB / Caixa; 'Sistema (X)' no que o sistema
        # lanca sozinho). So troca o NOME: a regra e' a mesma do saldo, saldo nenhum muda.
        trocas = []
        for (origem,) in db.session.execute(text('SELECT DISTINCT origin FROM transactions')).all():
            nova = domain.conta_padrao(origem)
            if nova != origem:
                n = db.session.execute(text('UPDATE transactions SET origin = :nova '
                                            'WHERE origin IS NOT DISTINCT FROM :velha'),
                                       {'nova': nova, 'velha': origem}).rowcount
                trocas.append(f"'{origem}' -> '{nova}' ({n})")
        if trocas:
            db.session.add(domain.AuditLog(
                user_name='Sistema', action='UPDATE', target='FluxoCaixa',
                description='Contas do caixa padronizadas (o saldo não muda): ' + '; '.join(trocas)))
        # Prorrogado deixou de ser status (pedido de 02/10/2026): o titulo prorrogado fica
        # Aguardando e vira Atrasado sozinho se passar da nova data. A marca "prorrogado"
        # da tela vem do historico em check_extensions, que nao muda.
        ids = [i for (i,) in db.session.execute(text(
            "UPDATE checks SET status = 'Aguardando' WHERE status = 'Prorrogado' RETURNING id"))]
        if ids:
            db.session.add(domain.AuditLog(
                user_name='Sistema', action='UPDATE', target='Cheque',
                description=f"Status Prorrogado virou Aguardando em {len(ids)} cheque(s) "
                            f"(ids: {', '.join(map(str, sorted(ids)))}). A prorrogação continua "
                            f"no histórico de cada cheque."))
        db.session.commit()


    return app