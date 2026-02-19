from app.models.domain import User
from werkzeug.security import check_password_hash

class AuthService:
    def login(self, email, password):

        user = User.query.filter_by(email=email).first()
        
 
        if not user or not check_password_hash(user.password_hash, password):
            return None
            

        return user