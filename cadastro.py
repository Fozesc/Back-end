from app import db
from app.models.domain import User
from werkzeug.security import generate_password_hash


from run import app 

with app.app_context():
    email_admin = "admin@fozesc.com"
    senha_admin = "123456"


    usuario_existente = User.query.filter_by(email=email_admin).first()
    
    if usuario_existente:
        print(f"⚠️ O usuário {email_admin} já existe no banco de dados!")

        usuario_existente.password_hash = generate_password_hash(senha_admin)
        db.session.commit()
        print(f"✅ Senha resetada para: {senha_admin}")
    else:
        
        novo_admin = User(
            name="Administrador Fozesc",
            email=email_admin,
            password_hash=generate_password_hash(senha_admin),
            role="Admin",
            active=True
        )
        db.session.add(novo_admin)
        db.session.commit()
        print(f"✅ Administrador criado com sucesso!")
        print(f"📧 Login: {email_admin}")
        print(f"🔑 Senha: {senha_admin}")