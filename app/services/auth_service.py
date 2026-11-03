from app.models.domain import User
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func
from app.services.audit_service import AuditService
_HASH_FALSO = generate_password_hash('senha-que-nao-existe')


class AuthService:
    def __init__(self):
        self.audit = AuditService()
        
    def login(self, email, password):

        user = User.query.filter(func.lower(User.email) == str(email).strip().lower()).first()

        # sempre confere uma senha (a falsa quando o e-mail nao existe): o tempo de resposta igual
        # nao entrega quais e-mails estao cadastrados
        confere = check_password_hash(user.password_hash if user else _HASH_FALSO, str(password))
        if not user or user.active is False or not confere:
            return None

        # O log de LOGIN é registrado no auth_controller (após gerar o token).
        # Foi REMOVIDO daqui porque estava gravando a auditoria de login EM DOBRO.
        return user