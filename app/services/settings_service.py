import re
from datetime import datetime, timedelta

from app.models.domain import AuditLog, BackupConfig, CompanySettings, User
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt, get_jwt_identity
from werkzeug.security import check_password_hash

FREQUENCIAS = {'diario': 'todo dia útil', 'semanal': 'toda semana', 'mensal': 'todo mês', 'desligado': 'não envia'}
ARQUIVOS = {'freq_banco': 'banco', 'freq_planilha': 'planilha', 'freq_relatorio': 'relatório'}
EMAIL = re.compile(r'[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>".]+')
MAX_DESTINATARIOS = 20
# o que o worker de backup grava na auditoria (gerar_backup.auditar), separado pelo comeco do texto
TIPOS_BACKUP = (('banco', 'Backup %', 'A cópia do banco'), ('email', 'E-mail do backup%', 'O envio do backup por e-mail'),
                ('restauracao', 'Teste de restauração%', 'O teste de restauração do backup'))
BACKUP_PARADO_HORAS = 48   # o worker faz copia todo dia as 12h e as 15h


class SettingsService:

    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
       
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def get_settings(self):
      
        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings(
                company_name="Minha Fatoring",
                default_monthly_rate=4.0,
                default_compensation_days=2
            )
            db.session.add(settings)
            db.session.commit()
        
        return self._serialize(settings)

    def update_settings(self, data):
        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings()
            db.session.add(settings)
       
       
        mudancas = []

   
        campos = {
            'nomeEmpresa': ('Nome', 'company_name'),
            'cnpj': ('CNPJ', 'cnpj'),
            'telefone': ('Telefone', 'phone'),
            'endereco': ('Endereço', 'address'),
            'taxaPadrao': ('Taxa Mensal', 'default_monthly_rate'),
            'diasCompensacaoPadrao': ('Dias Comp.', 'default_compensation_days'),
            'iof_rate': ('IOF', 'iof_rate'),
            'iof_daily_rate': ('IOF Diário', 'iof_daily_rate'),
            'extension_rate': ('Juros Prorrogação', 'extension_rate'),
            'fine_rate': ('Multa Devolução', 'fine_rate')
        }

        for chave_json, (label, atributo) in campos.items():
            if chave_json in data:
                valor_novo = data[chave_json]
                valor_antigo = getattr(settings, atributo)

             
                if isinstance(valor_antigo, float): valor_novo = float(valor_novo)
                if isinstance(valor_antigo, int): valor_novo = int(valor_novo)

                if valor_novo != valor_antigo:
                    mudancas.append(f"{label}: {valor_antigo} -> {valor_novo}")
                    setattr(settings, atributo, valor_novo)

        db.session.commit()

   
        if mudancas:
            resumo = "Alterou: " + " | ".join(mudancas)
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Configuracao', resumo)
        
        return self._serialize(settings)

    def _serialize(self, s):
        return {
            'nomeEmpresa': s.company_name,
            'cnpj': s.cnpj,
            'telefone': s.phone,
            'endereco': s.address,
            'taxaPadrao': s.default_monthly_rate,
            'diasCompensacaoPadrao': s.default_compensation_days,
            'iof_rate': s.iof_rate,
            'iof_daily_rate': s.iof_daily_rate,
            'extension_rate': s.extension_rate,
            'fine_rate': s.fine_rate
        }

    def status_backup(self):
        """O aviso da tela inicial: o ultimo registro de cada tipo (copia do banco, e-mail, teste de
        restauracao) que foi FALHA, e o backup parado ha mais de 2 dias. So conta a partir do
        primeiro registro do worker, para nao avisar antes de ele rodar a 1a vez."""
        do_worker = AuditLog.query.filter(AuditLog.target == 'Backup', AuditLog.action.in_(('BACKUP', 'FALHA')))
        alertas, ultimo_ok = [], None
        for tipo, padrao, nome in TIPOS_BACKUP:
            ultimo = do_worker.filter(AuditLog.description.like(padrao)).order_by(AuditLog.timestamp.desc()).first()
            if ultimo and ultimo.action == 'FALHA':
                alertas.append({'tipo': tipo, 'quando': ultimo.timestamp.isoformat(timespec='minutes'),
                                'texto': f"{nome} falhou", 'detalhe': (ultimo.description or '')[:300]})
        ok = do_worker.filter(AuditLog.action == 'BACKUP', AuditLog.description.like('Backup %'))\
            .order_by(AuditLog.timestamp.desc()).first()
        if ok:
            ultimo_ok = ok.timestamp.isoformat(timespec='minutes')
            if ok.timestamp < datetime.now() - timedelta(hours=BACKUP_PARADO_HORAS) and \
                    not any(a['tipo'] == 'banco' for a in alertas):
                alertas.append({'tipo': 'parado', 'quando': ultimo_ok, 'texto': 'O backup parou de rodar',
                                'detalhe': f"A última cópia do banco que deu certo foi em {ok.timestamp:%d/%m/%Y às %H:%M}."})
        return {'alertas': alertas, 'ultimo_ok': ultimo_ok}

    def get_backup(self):
        return self._serialize_backup(db.session.get(BackupConfig, 1) or BackupConfig(destinatarios=[]))

    def _exigir_admin(self):
        usuario = db.session.get(User, int(get_jwt_identity()))
        if not usuario or usuario.role != 'Admin':
            raise PermissionError('Só o administrador mexe no backup')
        return usuario

    def pedir_envio_backup(self):
        self._exigir_admin()
        cfg = db.session.get(BackupConfig, 1)
        if not cfg or not cfg.destinatarios:
            raise ValueError('Salve a lista de e-mails antes de enviar')
        if not cfg.envio_pedido_em:
            cfg.envio_pedido_em = datetime.now()
            db.session.commit()
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Backup',
                                  'Pediu o envio do backup completo agora para: '
                                  + ', '.join(d['email'] for d in cfg.destinatarios))
        return self._serialize_backup(cfg)

    def update_backup(self, data):
        usuario = self._exigir_admin()
        if not usuario or not check_password_hash(usuario.password_hash, str(data.get('senha_login') or '')):
            self.audit.log_action(self._get_current_user(), 'NEGADO', 'Backup',
                                  'Senha incorreta ao tentar mudar a configuração do backup')
            raise PermissionError('Senha incorreta - nada foi alterado')

        lista = data.get('destinatarios')
        if not isinstance(lista, list) or len(lista) > MAX_DESTINATARIOS:
            raise ValueError(f'Informe até {MAX_DESTINATARIOS} e-mails')
        destinatarios = []
        for d in lista:
            email = str(d.get('email') or '').strip().lower() if isinstance(d, dict) else ''
            if len(email) > 120 or not EMAIL.fullmatch(email):
                raise ValueError(f'E-mail inválido: {email or "(vazio)"}')
            if any(x['email'] == email for x in destinatarios):
                raise ValueError(f'E-mail repetido: {email}')
            destinatarios.append({'email': email, 'banco': d.get('banco') is True})

        if any(data.get(campo) not in FREQUENCIAS for campo in ARQUIVOS):
            raise ValueError('Frequência inválida')

        nova = data.get('senha_arquivos') or ''
        if not isinstance(nova, str) or (nova and not (12 <= len(nova) <= 64 and nova.isprintable())):
            raise ValueError('A senha dos arquivos precisa ter de 12 a 64 caracteres')

        cfg = db.session.get(BackupConfig, 1)
        if not cfg:
            cfg = BackupConfig(id=1, destinatarios=[], freq_banco='diario', freq_planilha='semanal',
                               freq_relatorio='mensal')
            db.session.add(cfg)
        antes = {d['email']: d['banco'] for d in (cfg.destinatarios or [])}
        depois = {d['email']: d['banco'] for d in destinatarios}

        mudancas = [f"incluiu {e}" + (' (com o banco)' if b else '') for e, b in depois.items() if e not in antes]
        mudancas += [f"retirou {e}" for e in antes if e not in depois]
        mudancas += [f"{e} {'passou a receber' if b else 'deixou de receber'} o banco"
                     for e, b in depois.items() if e in antes and antes[e] != b]
        for campo, nome in ARQUIVOS.items():
            if getattr(cfg, campo) != data[campo]:
                mudancas.append(f"{nome}: {FREQUENCIAS.get(getattr(cfg, campo), getattr(cfg, campo))} -> {FREQUENCIAS[data[campo]]}")
                setattr(cfg, campo, data[campo])
        if nova and nova != cfg.senha:
            mudancas.append('trocou a senha dos arquivos')
            cfg.senha = nova
        cfg.destinatarios = destinatarios
        db.session.commit()

        if mudancas:
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Backup', 'Backup: ' + ' | '.join(mudancas))
        return self._serialize_backup(cfg)

    def _serialize_backup(self, c):
        return {
            'destinatarios': c.destinatarios or [],
            'freq_banco': c.freq_banco or 'diario',
            'freq_planilha': c.freq_planilha or 'semanal',
            'freq_relatorio': c.freq_relatorio or 'mensal',
            'tem_senha': bool(c.senha),
            'configurado': c.id is not None,
            'ultimo_envio': c.ultimo_envio,
            'envio_pedido_em': c.envio_pedido_em.strftime('%Y-%m-%d %H:%M') if c.envio_pedido_em else None,
        }
