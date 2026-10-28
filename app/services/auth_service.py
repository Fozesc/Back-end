from app.models.domain import User
from werkzeug.security import check_password_hash
from sqlalchemy import func
from app.services.audit_service import AuditService
class AuthService:
    def __init__(self):
        self.audit = AuditService()
        
    def login(self, email, password):

        user = User.query.filter(func.lower(User.email) == str(email).strip().lower()).first()

        if not user or user.active is False or not check_password_hash(user.password_hash, str(password)):
            return None

        # O log de LOGIN é registrado no auth_controller (após gerar o token).
        # Foi REMOVIDO daqui porque estava gravando a auditoria de login EM DOBRO.
        return user