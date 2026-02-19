from app.models.domain import AuditLog, User
from app import db
from sqlalchemy import or_, desc

class AuditService:
    
    def log_action(self, user_name, action, target, description):
        
     
        user_display_name = str(user_name)

        try:
            
            if user_display_name.isdigit():
                user = db.session.get(User, int(user_display_name))
                if user:
                    user_display_name = user.name
            
            
            log = AuditLog(
                user_name=user_display_name,
                action=action,
                target=target,
                description=description
            )
            db.session.add(log)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Falha ao salvar audit: {e}")

    def get_paginated(self, page, per_page, search=None, action=None, date_start=None, date_end=None):
        query = AuditLog.query

        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(
                    AuditLog.user_name.ilike(term),
                    AuditLog.target.ilike(term),
                    AuditLog.description.ilike(term)
                )
            )

        if action and action != 'TODOS':
            query = query.filter(AuditLog.action == action)

        if date_start:
            query = query.filter(AuditLog.timestamp >= date_start)
        if date_end:
            query = query.filter(AuditLog.timestamp <= f"{date_end} 23:59:59")

        query = query.order_by(desc(AuditLog.timestamp))

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            'items': [self._serialize(log) for log in pagination.items],
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def _serialize(self, log):
      
        display_user = log.user_name
        
        
        if display_user and display_user.isdigit():
            try:
                user = db.session.get(User, int(display_user))
                if user:
                    display_user = user.name
            except:
                pass 
        
        return {
            'id': log.id,
            'date': log.timestamp.isoformat(),
            'user': display_user,
            'action': log.action,
            'target': log.target,
            'details': log.description
        }