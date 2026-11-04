from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException
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
    
   
    from app.models.domain import TokenBlocklist, User


    if db.session.query(TokenBlocklist.id).filter_by(jti=jti).scalar() is not None:
        return True
    # usuario apagado ou desativado perde o acesso na hora, mesmo com o login ainda valendo
    usuario = db.session.query(User.active).filter_by(id=int(jwt_payload["sub"])).first()
    return usuario is None or usuario.active is False
def _recusar_constante(nome):
    raise ValueError(f'valor {nome} não é aceito')


class JsonSemNaN(DefaultJSONProvider):
    """JSON que recusa NaN/Infinity: o json do Python aceita por padrao, e um "NaN" no limite
    de credito ou no saldo inicial passava a aparecer como NaN no painel inteiro."""
    def loads(self, s, **kwargs):
        kwargs.setdefault('parse_constant', _recusar_constante)
        return super().loads(s, **kwargs)


def create_app():
    app = Flask(__name__)
    app.json = JsonSemNaN(app)
    app.config.from_object(Config)
    # Atras do nginx todo pedido chega com o IP do nginx: sem isto o limite de pedidos valia
    # para o escritorio inteiro somado. O nginx manda o IP de verdade no X-Forwarded-For.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)



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

    # JSON quebrado ou com NaN/Infinity e' recusado (400) antes de chegar na rota: sem isso, as
    # rotas que leem com silent=True recebiam {} e respondiam "salvo" sem salvar nada
    @app.before_request
    def json_valido():
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.mimetype == 'application/json' \
                and request.get_data(cache=True):
            request.get_json()

    # Erro nunca volta como pagina HTML nem com detalhe interno: a tela sempre recebe
    # {"error": "..."} e o banco nao fica com transacao pela metade.
    @app.errorhandler(HTTPException)
    def erro_http(e):
        mensagens = {400: 'Pedido inválido: confira os dados enviados.', 404: 'Não encontrado.',
                     405: 'Operação não permitida.', 413: 'Arquivo grande demais.'}
        if e.code == 429:
            return jsonify({'error': e.description or 'Muitas tentativas. Espere alguns minutos e tente de novo.'}), 429
        return jsonify({'error': mensagens.get(e.code, e.name)}), e.code

    @app.errorhandler(Exception)
    def erro_inesperado(e):
        db.session.rollback()
        app.logger.exception('Erro inesperado em %s %s', request.method, request.path)
        return jsonify({'error': 'Erro interno: nada foi gravado. Tente de novo; se continuar, avise o administrador.'}), 500

 
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
            # lancamento feito por uma despesa recorrente
            'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS recorrente_id INTEGER '
            'REFERENCES recorrentes(id) ON DELETE SET NULL',
            'CREATE INDEX IF NOT EXISTS ix_transactions_recorrente_id ON transactions (recorrente_id)',
            # recorrente: a conta passou a ser so do debito automatico; pagamento parcial
            'ALTER TABLE recorrentes ALTER COLUMN conta DROP NOT NULL',
            'ALTER TABLE recorrentes ADD COLUMN IF NOT EXISTS valor_atual DOUBLE PRECISION',
            'ALTER TABLE recorrentes ADD COLUMN IF NOT EXISTS pago_atual DOUBLE PRECISION NOT NULL DEFAULT 0',
            # botao 'Enviar agora' da aba Backups: o worker de backup atende o pedido
            'ALTER TABLE backup_config ADD COLUMN IF NOT EXISTS envio_pedido_em TIMESTAMP',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_nome VARCHAR(100)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_cnpj VARCHAR(20)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_endereco VARCHAR(200)',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS empresa VARCHAR(10)',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS avalista_nome VARCHAR(100)',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS avalista_documento VARCHAR(20)',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS iof_base DOUBLE PRECISION',
            'ALTER TABLE operations ADD COLUMN IF NOT EXISTS iof_diario DOUBLE PRECISION',
            # dados para contrato (2.14): cliente e as duas empresas
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rg VARCHAR(30)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS nacionalidade VARCHAR(50)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS estado_civil VARCHAR(30)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_nome VARCHAR(100)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_cpf VARCHAR(20)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_rg VARCHAR(30)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_nacionalidade VARCHAR(50)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_estado_civil VARCHAR(30)',
            'ALTER TABLE clients ADD COLUMN IF NOT EXISTS rep_endereco VARCHAR(200)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS email VARCHAR(100)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_nome VARCHAR(100)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_cpf VARCHAR(20)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_rg VARCHAR(30)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_nacionalidade VARCHAR(50)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_estado_civil VARCHAR(30)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS rep_endereco VARCHAR(200)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_email VARCHAR(100)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_telefone VARCHAR(20)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_nome VARCHAR(100)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_cpf VARCHAR(20)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_rg VARCHAR(30)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_nacionalidade VARCHAR(50)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_estado_civil VARCHAR(30)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_rep_endereco VARCHAR(200)',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS esc BOOLEAN NOT NULL DEFAULT TRUE',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS pq_esc BOOLEAN NOT NULL DEFAULT FALSE',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_juros_mora DOUBLE PRECISION NOT NULL DEFAULT 1',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_multa DOUBLE PRECISION NOT NULL DEFAULT 2',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_honorarios DOUBLE PRECISION NOT NULL DEFAULT 10',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_prazo_reembolso INTEGER NOT NULL DEFAULT 30',
            'ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_vias INTEGER NOT NULL DEFAULT 2',
            "ALTER TABLE company_settings ADD COLUMN IF NOT EXISTS contrato_cidade VARCHAR(100) DEFAULT 'Foz do Iguaçu'",
            'ALTER TABLE contratos ADD COLUMN IF NOT EXISTS assinado BOOLEAN NOT NULL DEFAULT FALSE',
            'ALTER TABLE contratos ADD COLUMN IF NOT EXISTS assinado_em TIMESTAMP',
            'ALTER TABLE contratos ADD COLUMN IF NOT EXISTS assinado_por VARCHAR(100)',
            # a auditoria so cresce e a tela sempre ordena/filtra por data: sem indice, cada
            # pagina ordenava a tabela inteira
            'CREATE INDEX IF NOT EXISTS ix_audit_logs_timestamp ON audit_logs (timestamp)',
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
        # Borderô com todos os titulos apagados antes de a exclusao desfazer o caixa (2.5): a
        # saida dele continuava no caixa sem titulo nenhum. Sai agora, e o borderô vazio tambem.
        orfas = db.session.execute(text(
            "DELETE FROM transactions t WHERE t.operation_id IS NOT NULL AND t.check_id IS NULL "
            "AND t.category IN ('Compra de Ativos', 'Comissão', 'Informativo') "
            "AND NOT EXISTS (SELECT 1 FROM checks c WHERE c.operation_id = t.operation_id) "
            "RETURNING t.id, t.operation_id, t.description, t.amount, t.origin, t.date")).all()
        vazios = sorted(i for (i,) in db.session.execute(text(
            "DELETE FROM operations o WHERE NOT EXISTS (SELECT 1 FROM checks c WHERE c.operation_id = o.id) "
            "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.operation_id = o.id) RETURNING o.id")))
        if orfas or vazios:
            db.session.add(domain.AuditLog(
                user_name='Sistema', action='DELETE', target='FluxoCaixa',
                description='Borderô sem títulos (apagados antes da versão 2.5) saiu do caixa: '
                            + '; '.join(f"#{i} '{d}' R$ {v:.2f} ({o}, {dt:%d/%m/%Y})"
                                        for i, _, d, v, o, dt in orfas if v)
                            + (f" | borderô(s) vazio(s) apagado(s): {', '.join(f'#{i}' for i in vazios)}"
                               if vazios else '')))
        db.session.commit()


    return app