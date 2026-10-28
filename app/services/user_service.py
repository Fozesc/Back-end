import re
from app.models.domain import User
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt, get_jwt_identity
from werkzeug.security import generate_password_hash

CARGOS = ('Admin', 'Gerente', 'Operador')
MIN_SENHA = 8
EMAIL = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')


class UserService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def get_all(self):
        users = User.query.order_by(User.name).all()
        return [self._serialize(u) for u in users]

    def _validar(self, data, user=None):
        nome = ' '.join(str(data.get('name') or '').split())[:100]
        email = str(data.get('email') or '').strip().lower()
        if not nome:
            raise ValueError('Informe o nome')
        if len(email) > 100 or not EMAIL.fullmatch(email):
            raise ValueError('E-mail inválido')
        outro = User.query.filter(db.func.lower(User.email) == email).first()
        if outro and outro is not user:
            raise ValueError('Email já cadastrado')
        cargo = data.get('role') or (user.role if user else 'Operador')
        if cargo not in CARGOS:
            raise ValueError('Nível de acesso inválido')
        senha = data.get('password') or ''
        if (senha or not user) and not (MIN_SENHA <= len(str(senha)) <= 128):
            raise ValueError(f'A senha precisa ter pelo menos {MIN_SENHA} caracteres')
        return nome, email, cargo, senha

    def _sobra_admin(self, user):
        """Sem isto o ultimo Admin podia se rebaixar ou ser apagado e ninguem mais entrava
        nas telas de equipe, auditoria e backup."""
        if user.role == 'Admin' and User.query.filter(User.role == 'Admin', User.id != user.id).count() == 0:
            raise ValueError('Este é o único administrador. Promova outra pessoa a Admin antes.')

    def create(self, data):
        nome, email, cargo, senha = self._validar(data)
        user = User(name=nome, email=email, password_hash=generate_password_hash(senha), role=cargo)
        db.session.add(user)
        db.session.commit()
        self.audit.log_action(self._get_current_user(), 'CREATE', 'Usuario', f"Cadastrou usuário: {user.email} ({cargo})")
        return self._serialize(user)

    def update(self, id, data):
        user = db.session.get(User, id)
        if not user:
            raise ValueError("Usuário não encontrado")
        nome, email, cargo, senha = self._validar(data, user)
        if cargo != user.role:
            self._sobra_admin(user)

        mudancas = [f"{rotulo}: {antes} -> {depois}" for rotulo, antes, depois in
                    (('nome', user.name, nome), ('e-mail', user.email, email), ('nível', user.role, cargo))
                    if antes != depois]
        user.name, user.email, user.role = nome, email, cargo
        if senha:
            user.password_hash = generate_password_hash(senha)
            mudancas.append('trocou a senha')

        db.session.commit()
        self.audit.log_action(self._get_current_user(), 'UPDATE', 'Usuario',
                              f"Alterou o usuário {user.email}: " + (' | '.join(mudancas) or 'nada mudou'))
        return self._serialize(user)

    def delete(self, id):
        user = db.session.get(User, id)
        if not user:
            return False
        if str(user.id) == str(get_jwt_identity()):
            raise ValueError('Você não pode apagar o próprio usuário')
        self._sobra_admin(user)

        retrato = f"Removeu o usuário #{id}: {user.name} <{user.email}> ({user.role})"
        db.session.delete(user)
        db.session.commit()
        self.audit.log_action(self._get_current_user(), 'DELETE', 'Usuario', retrato)
        return True

    def _serialize(self, user):
        return {
            'id': user.id,
            'name': user.name,
            'email': user.email,
            'role': user.role,
            'active': user.active
        }
