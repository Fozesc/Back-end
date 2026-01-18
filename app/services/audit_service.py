from app import db
from app.models.domain import AuditLog
from flask import request

class AuditService:
    @staticmethod
    def log(user, action, target_type, target_id=None, details=None):
    
        try:
            
            ip = request.remote_addr if request else '127.0.0.1'
            agent = request.headers.get('User-Agent') if request else 'System'
            
            new_log = AuditLog(
                user_id=user.id if hasattr(user, 'id') else None,
                user_name=user.name if hasattr(user, 'name') else str(user),
                action=action.upper(),
                target_type=target_type,
                target_id=str(target_id) if target_id else None,
                details=str(details) if details else '',
                ip_address=ip,
                user_agent=agent
            )
            
            db.session.add(new_log)
            db.session.commit()
        except Exception as e:
            print(f"FALHA AO GRAVAR LOG DE AUDITORIA: {e}")
      