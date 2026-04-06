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
            
        self.audit.log_action(user.name, 'LOGIN', 'Sistema', f"Usuário {email} realizou login.")
        return user