from app.models.domain import User
from werkzeug.security import check_password_hash
from app.services.audit_service import AuditService
class AuthService:
    def __init__(self):
        self.audit = AuditService()
        
    def login(self, email, password):

        user = User.query.filter_by(email=email).first()
        
 
        if not user or not check_password_hash(user.password_hash, password):
            return None

        # O log de LOGIN é registrado no auth_controller (após gerar o token).
        # Foi REMOVIDO daqui porque estava gravando a auditoria de login EM DOBRO.
        return user