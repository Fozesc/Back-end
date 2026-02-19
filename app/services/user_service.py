from app.models.domain import User
from app import db

from werkzeug.security import generate_password_hash

class UserService:
    def get_all(self):
        users = User.query.all()
        return [self._serialize(u) for u in users]

    def create(self, data):
        if User.query.filter_by(email=data.get('email')).first():
            raise ValueError("Email já cadastrado")

        
        hashed_password = generate_password_hash(data['password'])
        
        user = User(
            name=data['name'],
            email=data['email'],
            password_hash=hashed_password,
            role=data.get('role', 'Operador')
        )
        
        db.session.add(user)
        db.session.commit()
        return self._serialize(user)

    def update(self, id, data):
        user = User.query.get(id)
        if not user:
            raise ValueError("Usuário não encontrado")

        if 'name' in data: user.name = data['name']
        if 'email' in data: user.email = data['email']
        if 'role' in data: user.role = data['role']
        
       
        if 'password' in data and data['password']:
            user.password_hash = generate_password_hash(data['password'])

        db.session.commit()
        return self._serialize(user)

    def delete(self, id):
        user = User.query.get(id)
        if not user:
            return False
        
        db.session.delete(user)
        db.session.commit()
        return True

    def _serialize(self, user):
        return {
            'id': user.id,
            'name': user.name,
            'email': user.email,
            'role': user.role,
            'active': user.active
        }