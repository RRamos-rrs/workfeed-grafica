import os
import re
import uuid
import shutil
import json
import asyncio
import time
from typing import List
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, text, func
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

import unicodedata
from datetime import datetime, timezone, timedelta
import models
from database import engine, get_db

# Garante migração automática de colunas necessárias
try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_id INTEGER REFERENCES users(id);"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS department VARCHAR(100) DEFAULT 'Produção / Pré-Impressão';"))
        conn.execute(text("ALTER TABLE sector_configs ADD COLUMN IF NOT EXISTS fields_schema TEXT;"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sector_id INTEGER;"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sector_name VARCHAR(100);"))
        conn.execute(text("ALTER TABLE posts ADD COLUMN IF NOT EXISTS sector_id INTEGER;"))
        conn.execute(text("ALTER TABLE posts ADD COLUMN IF NOT EXISTS sector_name VARCHAR(100);"))
except Exception as e:
    print(f"Aviso de migração automática: {e}")

for _ddl in ("ALTER TABLE sector_configs ADD COLUMN owner_id INTEGER;",
             "ALTER TABLE posts ADD COLUMN image_urls TEXT;"):
    try:
        with engine.begin() as conn:
            conn.execute(text(_ddl))
    except Exception:
        pass  # coluna já existe

models.Base.metadata.create_all(bind=engine)

# Diretórios necessários para uploads e ficheiros estáticos
os.makedirs("uploads", exist_ok=True)
os.makedirs("static/img", exist_ok=True)

app = FastAPI()

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")
templates.env.globals["post_images"] = lambda p: post_images(p)
templates.env.filters["localtime"] = lambda dt, fmt="%d/%m/%Y %H:%M": fmt_dt(dt, fmt)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=1.0)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

def get_current_user(request: Request, db: Session):
    username = request.cookies.get("user_session")
    if not username:
        return None
    return db.query(models.User).filter(models.User.username == username).first()

MANAGER_ROLES = ["gestor", "manager", "admin"]
ALLOWED_ROLES = ["Gestor", "Colaborador"]
VALID_STATUSES = ["Atribuído", "Em Produção", "Aprovado"]

def is_manager_user(user) -> bool:
    return (user.role or "").strip().lower() in MANAGER_ROLES

def get_team_users(db: Session, user):
    """Utilizadores da equipa (mesma lógica da página inicial)."""
    manager_ref_id = user.id if is_manager_user(user) else user.manager_id
    if manager_ref_id:
        return db.query(models.User).filter(
            or_(models.User.id == manager_ref_id, models.User.manager_id == manager_ref_id)
        ).all()
    return [user]

# Horas são guardadas em UTC; o site mostra no horário de Brasília
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-3))

def to_local_time(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)

def fmt_dt(dt, fmt="%d/%m/%Y %H:%M", default=""):
    local = to_local_time(dt)
    return local.strftime(fmt) if local else default

MAX_POST_IMAGES = 6

def post_images(post) -> List[str]:
    """Todas as fotos do post (carrossel). Posts antigos têm só image_url."""
    urls = []
    raw = getattr(post, "image_urls", None)
    if raw:
        try:
            urls = [u for u in json.loads(raw) if isinstance(u, str)]
        except ValueError:
            urls = []
    if not urls and getattr(post, "image_url", None):
        urls = [post.image_url]
    return urls

def serialize_post(post, created_default="") -> dict:
    return {
        "id": post.id,
        "sector_id": getattr(post, "sector_id", None),
        "sector_name": getattr(post, "sector_name", None),
        "author": post.author,
        "tool_type": post.tool_type,
        "op_number": post.op_number,
        "title": post.title,
        "supplier": post.supplier,
        "due_date": post.due_date,
        "instructions": post.instructions,
        "image_url": post.image_url,
        "images": post_images(post),
        "created_at": fmt_dt(getattr(post, "created_at", None), default=created_default),
        "likes": post.likes or 0,
        "comments": [{"author": c.author, "text": c.text} for c in (post.comments or [])],
    }

def can_manage_post(db: Session, user, post) -> bool:
    if (post.author or "").strip().lower() == (user.full_name or "").strip().lower():
        return True
    return is_manager_user(user) and post.author in get_team_names(db, user)

def get_team_owner_id(user):
    """Id do gestor que representa a equipa do utilizador."""
    return user.id if is_manager_user(user) else user.manager_id

def visible_sectors(db: Session, user):
    """Setores da equipa do utilizador + o setor padrão de Pré-Impressão (partilhado)."""
    owner_id = get_team_owner_id(user)
    result = []
    all_sectors = db.query(models.SectorConfig).order_by(models.SectorConfig.id).all()
    # O setor padrão partilhado é o mais antigo sem dono (não depende do nome, que pode ser editado)
    legacy_ids = [x.id for x in all_sectors if x.owner_id is None]
    shared_id = legacy_ids[0] if legacy_ids else None
    for sec in all_sectors:
        name_norm = unicodedata.normalize("NFKD", sec.name or "").encode("ascii", "ignore").decode().lower()
        shared_default = sec.owner_id is None and (sec.id == shared_id or "impress" in name_norm)
        if shared_default or (owner_id is not None and sec.owner_id == owner_id):
            result.append(sec)
    return result

def get_team_names(db: Session, user) -> List[str]:
    return [u.full_name for u in get_team_users(db, user)]

# Mantém referência às tarefas em segundo plano (evita que o Python as descarte a meio)
_bg_tasks = set()

def spawn_bg(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

# Uploads: só extensões permitidas, nome sem caminho e único, tamanho limitado
UPLOAD_DIR = "uploads"
ALLOWED_UPLOAD_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

def save_upload(upload: UploadFile) -> str:
    """Grava o ficheiro e devolve a URL pública. Lança ValueError se for inválido."""
    original = os.path.basename((upload.filename or "").replace("\\", "/"))
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise ValueError("Tipo de ficheiro não permitido. Use JPG, PNG, GIF, WEBP ou PDF.")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(original)[0])[:60] or "arquivo"
    safe_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{base}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    written = 0
    with open(file_path, "wb") as buffer:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                buffer.close()
                os.remove(file_path)
                raise ValueError("Ficheiro maior que 10 MB.")
            buffer.write(chunk)
    return f"/{UPLOAD_DIR}/{safe_name}"

def unauthorized():
    return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

def ensure_default_sectors(db: Session):
    existing_count = db.query(models.SectorConfig).count()
    if existing_count == 0:
        default_schema_ferramentais = [
            {"id": "tool_type", "label": "Tipo de Ferramental", "type": "select", "options": "Faca Corte e Vinco,Clichê de Relevo / Braille,Fitas de Braille,Matriz Hot Stamping,Gravação de Chapas Offset", "required": True},
            {"id": "op_number", "label": "Nº da OP", "type": "text", "placeholder": "Ex: OP-1042", "required": False},
            {"id": "title", "label": "Trabalho / Embalagem", "type": "text", "placeholder": "Ex: Cartucho 150ml Medicamento", "required": True},
            {"id": "supplier", "label": "Fornecedor Destinatário", "type": "text", "placeholder": "Ex: Facas Precision / Clicheria Alfa", "required": True},
            {"id": "due_date", "label": "Prazo Limite de Entrega", "type": "date", "required": True},
            {"id": "instructions", "label": "Instruções Técnicas", "type": "textarea", "placeholder": "Lâmina 23,80mm, vinco 2pt canaleta 0,4x1,3mm, conferir braille...", "required": False}
        ]

        default_schema_comercial = [
            {"id": "tool_type", "label": "Tipo de Atendimento", "type": "select", "options": "Orçamento de Embalagem,Envio de Amostra,Faturamento de Pedido,Aprovação de Prova", "required": True},
            {"id": "op_number", "label": "Nº do Pedido / Orçamento", "type": "text", "placeholder": "Ex: ORC-5580", "required": False},
            {"id": "title", "label": "Nome do Cliente / Projeto", "type": "text", "placeholder": "Ex: Linha Cosmética Verão", "required": True},
            {"id": "supplier", "label": "Empresa / Cliente Solicitante", "type": "text", "placeholder": "Ex: Farmacêutica Nacional", "required": True},
            {"id": "due_date", "label": "Prazo de Resposta / Entrega", "type": "date", "required": True},
            {"id": "instructions", "label": "Condições e Escopo", "type": "textarea", "placeholder": "Tiragem, acabamentos e observações comerciais...", "required": False}
        ]

        default_schema_expedicao = [
            {"id": "tool_type", "label": "Tipo de Operação", "type": "select", "options": "Cotação de Matéria-Prima,Despacho de Cargas,Entrada de Materiais,Conferência de Paletes", "required": True},
            {"id": "op_number", "label": "Nº da NF / Pedido de Compra", "type": "text", "placeholder": "Ex: NF-44912 / PC-890", "required": False},
            {"id": "title", "label": "Material / Lote de Carga", "type": "text", "placeholder": "Ex: Cartão Duplex 300g / 50 Paletes", "required": True},
            {"id": "supplier", "label": "Transportadora / Fornecedor", "type": "text", "placeholder": "Ex: Papirus / Transportadora Rápida", "required": True},
            {"id": "due_date", "label": "Data Prevista de Coleta / Doca", "type": "date", "required": True},
            {"id": "instructions", "label": "Instruções de Logística", "type": "textarea", "placeholder": "Horário de coleta, doca de entrega, empilhamento...", "required": False}
        ]

        default_schema_manutencao = [
            {"id": "tool_type", "label": "Tipo de Intervenção", "type": "select", "options": "Manutenção Preventiva,Troca de Peça Corretiva,Ajuste de Impressora,Inspeção de Corte e Vinco", "required": True},
            {"id": "op_number", "label": "Cód. da Máquina / Tag", "type": "text", "placeholder": "Ex: OFFSET-02 / CORTE-BOBST-01", "required": False},
            {"id": "title", "label": "Descrição da Intervenção", "type": "text", "placeholder": "Ex: Substituição de rolos de borracha", "required": True},
            {"id": "supplier", "label": "Técnico / Fornecedor de Peça", "type": "text", "placeholder": "Ex: Manutenção Interna / Peças Gráficas", "required": True},
            {"id": "due_date", "label": "Data Programada", "type": "date", "required": True},
            {"id": "instructions", "label": "Procedimento Técnico", "type": "textarea", "placeholder": "Parada programada, checagem de pressão e sensores...", "required": False}
        ]

        default_sectors = [
            models.SectorConfig(
                name="Ferramentais & Pré-Impressão",
                icon="🔪",
                fields_schema=json.dumps(default_schema_ferramentais, ensure_ascii=False)
            ),
            models.SectorConfig(
                name="Comercial & Vendas",
                icon="💼",
                fields_schema=json.dumps(default_schema_comercial, ensure_ascii=False)
            ),
            models.SectorConfig(
                name="Expedição & Compras",
                icon="📦",
                fields_schema=json.dumps(default_schema_expedicao, ensure_ascii=False)
            ),
            models.SectorConfig(
                name="Manutenção Operacional",
                icon="🔧",
                fields_schema=json.dumps(default_schema_manutencao, ensure_ascii=False)
            )
        ]
        db.add_all(default_sectors[:1])  # só o setor de Pré-Impressão; cada equipa cria os seus
        db.commit()
    else:
        sectors = db.query(models.SectorConfig).all()
        for s in sectors:
            if not s.fields_schema or s.fields_schema.strip() == "":
                opts = getattr(s, 'tool_types_csv', None) or "Geral"
                schema = [
                    {"id": "tool_type", "label": "Categoria / Tipo", "type": "select", "options": opts, "required": True},
                    {"id": "op_number", "label": getattr(s, 'ref_label', None) or "Nº da OP", "type": "text", "placeholder": getattr(s, 'ref_placeholder', None) or "Ex: OP-100", "required": False},
                    {"id": "title", "label": getattr(s, 'title_label', None) or "Trabalho", "type": "text", "placeholder": getattr(s, 'title_placeholder', None) or "Ex: Descrição", "required": True},
                    {"id": "supplier", "label": getattr(s, 'entity_label', None) or "Destinatário", "type": "text", "placeholder": getattr(s, 'entity_placeholder', None) or "Ex: Contato", "required": True},
                    {"id": "due_date", "label": "Prazo Limite", "type": "date", "required": True},
                    {"id": "instructions", "label": getattr(s, 'instructions_label', None) or "Instruções", "type": "textarea", "placeholder": getattr(s, 'instructions_placeholder', None) or "Detalhes...", "required": False}
                ]
                s.fields_schema = json.dumps(schema, ensure_ascii=False)
        db.commit()

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login")

    ensure_default_sectors(db)

    role_normalized = (current_user.role or "").strip().lower()
    is_manager = role_normalized in ["gestor", "manager", "admin"]
    manager_ref_id = current_user.id if is_manager else current_user.manager_id

    if manager_ref_id:
        team_users = db.query(models.User).filter(
            or_(models.User.id == manager_ref_id, models.User.manager_id == manager_ref_id)
        ).all()
    else:
        team_users = [current_user]

    team_user_names = [u.full_name for u in team_users]

    all_tasks = db.query(models.Task).filter(
        or_(
            models.Task.assigned_to.in_(team_user_names),
            models.Task.delegated_by.in_(team_user_names)
        )
    ).all()

    # joinedload assegura que os comentários são carregados sem sumir após o refresh
    posts = db.query(models.Post).options(joinedload(models.Post.comments)).filter(
        or_(
            models.Post.author.in_(team_user_names),
            models.Post.delegated_by.in_(team_user_names)
        )
    ).order_by(models.Post.created_at.desc()).all()

    sectors = visible_sectors(db, current_user)

    my_tasks = [t for t in all_tasks if (t.assigned_to or "").strip().lower() == current_user.full_name.strip().lower()]
    my_pending_tasks = [t for t in my_tasks if t.status == "Atribuído"]
    my_in_progress_tasks = [t for t in my_tasks if t.status == "Em Produção"]
    my_completed_posts = [p for p in posts if (p.author or "").strip().lower() == current_user.full_name.strip().lower()]

    collaborators = []
    for u in team_users:
        user_tasks = [t for t in all_tasks if (t.assigned_to or "").strip().lower() == u.full_name.strip().lower()]
        user_posts = [p for p in posts if (p.author or "").strip().lower() == u.full_name.strip().lower()]
        active_count = len([t for t in user_tasks if t.status != "Aprovado"])
        approved_count = len(user_posts)

        collaborators.append({
            "name": u.full_name,
            "username": u.username,
            "role": u.role,
            "department": getattr(u, 'department', 'Geral / Operacional'),
            "active_tasks": active_count,
            "approved_tasks": approved_count,
            "recent_posts": user_posts[:3]
        })

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current_user": current_user,
            "tasks": all_tasks,
            "my_tasks": my_tasks,
            "my_pending_tasks": my_pending_tasks,
            "my_in_progress_tasks": my_in_progress_tasks,
            "my_completed_posts": my_completed_posts,
            "posts": posts,
            "users": team_users,
            "collaborators": collaborators,
            "sectors": sectors
        }
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.post("/api/sectors/save")
async def save_sector(
    request: Request,
    sector_id: int = Form(0),
    name: str = Form(...),
    icon: str = Form("📁"),
    fields_schema_json: str = Form(...),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})
    if not is_manager_user(current_user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Apenas gestores podem editar modelos de setores."})

    clean_name = name.strip()
    if not clean_name:
        return JSONResponse(status_code=400, content={"success": False, "message": "Informe o nome do setor."})
    try:
        json.loads(fields_schema_json)
    except ValueError:
        return JSONResponse(status_code=400, content={"success": False, "message": "Campos do modelo inválidos."})
    sector = None
    if sector_id > 0:
        sector = next((x for x in visible_sectors(db, current_user) if x.id == sector_id), None)

    if not sector:
        sector = models.SectorConfig(
            owner_id=get_team_owner_id(current_user),
            name=clean_name,
            icon=icon.strip() or "📁",
            fields_schema=fields_schema_json.strip()
        )
        db.add(sector)
    else:
        sector.name = clean_name
        sector.icon = icon.strip() or "📁"
        sector.fields_schema = fields_schema_json.strip()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse(status_code=400, content={"success": False, "message": "Já existe um setor com esse nome."})
    spawn_bg(manager.broadcast({"type": "REFRESH", "scope": "sectors"}))
    return JSONResponse(content={"success": True, "message": "Modelo de setor salvo com sucesso!"})

@app.delete("/api/sectors/{sector_id}")
async def delete_sector(request: Request, sector_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    role_normalized = (current_user.role or "").strip().lower()
    if role_normalized not in ["gestor", "manager", "admin"]:
        return JSONResponse(status_code=403, content={"success": False, "message": "Apenas gestores podem remover modelos de setores."})

    sector = next((x for x in visible_sectors(db, current_user) if x.id == sector_id), None)
    if not sector:
        return JSONResponse(status_code=404, content={"success": False, "message": "Setor não encontrado."})

    db.delete(sector)
    db.commit()
    spawn_bg(manager.broadcast({"type": "REFRESH", "scope": "sectors"}))
    return JSONResponse(content={"success": True, "message": "Setor removido com sucesso!"})

@app.get("/api/users/profile/{full_name}")
def get_user_profile(request: Request, full_name: str, db: Session = Depends(get_db)):
    if not get_current_user(request, db):
        return JSONResponse(status_code=401, content={"message": "Sessão expirada."})
    clean_name = full_name.strip()
    # Comparação exata (sem curingas % e _ do ILIKE)
    user = db.query(models.User).filter(func.lower(models.User.full_name) == clean_name.lower()).first()
    if not user:
        return JSONResponse(status_code=404, content={"message": "Colaborador não encontrado"})
    
    tasks = db.query(models.Task).filter(func.lower(models.Task.assigned_to) == clean_name.lower()).all()
    posts = db.query(models.Post).filter(func.lower(models.Post.author) == clean_name.lower()).all()
    
    return {
        "full_name": user.full_name,
        "username": user.username,
        "role": user.role,
        "department": getattr(user, 'department', 'Geral / Operacional'),
        "pending_tasks": len([t for t in tasks if t.status != "Aprovado"]),
        "completed_tasks": len(posts)
    }

@app.get("/api/state")
def get_state(request: Request, db: Session = Depends(get_db)):
    """Dados atuais da equipe (setores, utilizadores e métricas) para o site atualizar sem recarregar."""
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    team_users = get_team_users(db, current_user)
    names = [u.full_name for u in team_users]

    task_rows = db.query(models.Task.assigned_to, models.Task.status).filter(
        or_(models.Task.assigned_to.in_(names), models.Task.delegated_by.in_(names))
    ).all()
    post_authors = [a for (a,) in db.query(models.Post.author).filter(
        or_(models.Post.author.in_(names), models.Post.delegated_by.in_(names))
    ).all()]

    metrics = {}
    for u in team_users:
        key = (u.full_name or "").strip().lower()
        metrics[u.username] = {
            "active_tasks": len([1 for assigned, status in task_rows if (assigned or "").strip().lower() == key and status != "Aprovado"]),
            "approved_tasks": len([1 for author in post_authors if (author or "").strip().lower() == key]),
        }

    sectors = []
    for sec in visible_sectors(db, current_user):
        try:
            fields = json.loads(sec.fields_schema or "[]")
        except ValueError:
            fields = []
        sectors.append({"id": sec.id, "name": sec.name, "icon": sec.icon, "fields": fields})

    users = [{
        "full_name": u.full_name,
        "username": u.username,
        "role": u.role,
        "department": u.department or "Operacional",
    } for u in team_users]

    me = (current_user.full_name or "").strip().lower()
    my_tasks = [{
        "id": t.id, "status": t.status, "tool_type": t.tool_type, "op_number": t.op_number,
        "title": t.title, "supplier": t.supplier, "due_date": t.due_date,
        "instructions": t.instructions, "delegated_by": t.delegated_by, "image_url": t.image_url,
    } for t in db.query(models.Task).filter(
        func.lower(models.Task.assigned_to) == me,
        models.Task.status.in_(["Atribuído", "Em Produção"])
    ).order_by(models.Task.id).all()]

    response = JSONResponse(content={"sectors": sectors, "users": users, "metrics": metrics, "my_tasks": my_tasks})
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )

@app.post("/login")
def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == username, models.User.password == password).first()
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Usuário ou senha incorretos"}
        )
    
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="user_session", value=user.username, httponly=True, samesite="lax")
    return redirect

@app.post("/register")
async def register(
    request: Request,
    full_name: str = Form(...),
    username: str = Form(...),
    role: str = Form("Gestor"),
    department: str = Form("Produção / Pré-Impressão"),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    clean_username = username.strip()
    existing = db.query(models.User).filter(func.lower(models.User.username) == clean_username.lower()).first()
    if existing:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Este nome de usuário já existe"}
        )

    new_user = models.User(
        full_name=full_name.strip(),
        username=clean_username,
        role=role if role in ALLOWED_ROLES else "Colaborador",
        department=department.strip(),
        password=password,
        manager_id=None
    )
    db.add(new_user)
    db.commit()

    spawn_bg(manager.broadcast({"type": "REFRESH", "scope": "team"}))

    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="user_session", value=clean_username, httponly=True, samesite="lax")
    return redirect

@app.post("/api/users/add-collaborator")
async def add_collaborator(
    request: Request,
    full_name: str = Form(...),
    username: str = Form(...),
    role: str = Form("Colaborador"),
    department: str = Form("Produção / Pré-Impressão"),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    if not is_manager_user(current_user):
        return JSONResponse(status_code=403, content={"success": False, "message": "Apenas gestores podem cadastrar colaboradores."})

    manager_ref_id = current_user.id
    clean_username = username.strip()
    clean_full_name = full_name.strip()
    if role not in ALLOWED_ROLES:
        role = "Colaborador"

    existing = db.query(models.User).filter(func.lower(models.User.username) == clean_username.lower()).first()
    
    if existing:
        # Só vincula colaboradores sem gestor (ou já da sua equipe). Nunca altera
        # a conta de outro gestor nem de quem pertence a outra equipe.
        if existing.id == current_user.id or is_manager_user(existing) or existing.manager_id not in (None, current_user.id):
            return JSONResponse(status_code=403, content={"success": False, "message": "Este usuário já existe e não pode ser alterado por você."})
        existing.full_name = clean_full_name
        existing.role = role
        existing.department = department.strip()
        existing.password = password
        existing.manager_id = manager_ref_id
        db.commit()

        spawn_bg(manager.broadcast({"type": "REFRESH", "scope": "team"}))
        return JSONResponse(content={"success": True, "message": "Colaborador vinculado com sucesso!"})

    new_user = models.User(
        full_name=clean_full_name,
        username=clean_username,
        role=role,
        department=department.strip(),
        password=password,
        manager_id=manager_ref_id
    )
    db.add(new_user)
    db.commit()

    spawn_bg(manager.broadcast({"type": "REFRESH", "scope": "team"}))
    return JSONResponse(content={"success": True, "message": "Colaborador adicionado com sucesso!"})

@app.delete("/api/users/{username}")
async def delete_user(request: Request, username: str, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    role_normalized = (current_user.role or "").strip().lower()
    if role_normalized not in ["gestor", "manager", "admin"]:
        return JSONResponse(status_code=403, content={"success": False, "message": "Apenas gestores podem remover colaboradores."})

    clean_username = username.strip()
    if current_user.username.lower() == clean_username.lower():
        return JSONResponse(status_code=400, content={"success": False, "message": "Não pode remover a sua própria conta."})

    user_to_delete = db.query(models.User).filter(func.lower(models.User.username) == clean_username.lower()).first()
    if not user_to_delete:
        return JSONResponse(status_code=404, content={"success": False, "message": "Colaborador não encontrado."})

    if user_to_delete.manager_id != current_user.id:
        return JSONResponse(status_code=403, content={"success": False, "message": "Este colaborador não pertence à sua equipe."})

    del_username = user_to_delete.username
    del_full_name = user_to_delete.full_name

    db.delete(user_to_delete)
    db.commit()

    spawn_bg(manager.broadcast({
        "type": "USER_DELETED",
        "username": del_username,
        "full_name": del_full_name
    }))

    return JSONResponse(content={"success": True, "message": "Colaborador removido com sucesso!"})

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("user_session")
    return response

# ROTA COM PROCESSAMENTO INTELIGENTE DE CAMPOS (ELIMINA CARDS COM DADOS EM BRANCO)
@app.post("/tasks/create")
async def create_task(
    request: Request,
    sector_id: int = Form(0),
    sector_name: str = Form(""),
    op_number: str = Form(""),
    tool_type: str = Form("Geral"),
    title: str = Form("Demanda Operacional"),
    assigned_to: str = Form(""),
    supplier: str = Form("Interno"),
    instructions: str = Form(""),
    due_date: str = Form("A definir"),
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return unauthorized()
    delegator = current_user.full_name.strip()

    form_data = await request.form()

    clean_title = title.strip() if title and title != "Demanda Operacional" else ""
    clean_tool = tool_type.strip() if tool_type and tool_type != "Geral" else ""
    clean_supplier = supplier.strip() if supplier and supplier != "Interno" else ""
    clean_op = op_number.strip()
    clean_due = due_date.strip() if due_date and due_date != "A definir" else ""
    clean_instructions = instructions.strip()

    extra_details = []
    for key, value in form_data.items():
        if key not in ['sector_id', 'sector_name', 'image', 'assigned_to'] and isinstance(value, str):
            val_str = value.strip()
            if not val_str:
                continue
            if not clean_title and key in ['title', 'trabalho', 'descricao']:
                clean_title = val_str
            elif not clean_tool and ('tool' in key or 'tipo' in key or 'categoria' in key):
                clean_tool = val_str
            elif not clean_op and ('op' in key or 'ref' in key or 'pedido' in key):
                clean_op = val_str
            elif not clean_supplier and ('supplier' in key or 'cliente' in key or 'destino' in key or 'fornecedor' in key):
                clean_supplier = val_str
            elif not clean_due and ('date' in key or 'prazo' in key or 'data' in key):
                clean_due = val_str
            elif key.startswith('custom_'):
                extra_details.append(f"{val_str}")

    final_title = clean_title or f"Demanda - {clean_tool or 'Geral'}"
    final_tool = clean_tool or "Geral"
    final_supplier = clean_supplier or "Interno"
    final_op = clean_op  # vazio quando o setor não tem o campo (o cartão não mostra "OP")
    final_due = clean_due or "A definir"

    if extra_details:
        adicional = " | ".join(extra_details)
        clean_instructions = f"{clean_instructions} ({adicional})".strip() if clean_instructions else adicional

    image_url = None
    if image and image.filename:
        try:
            image_url = save_upload(image)
        except ValueError as err:
            return JSONResponse(status_code=400, content={"success": False, "message": str(err)})

    new_task = models.Task(
        sector_id=sector_id if sector_id > 0 else None,
        sector_name=sector_name.strip() if sector_name else None,
        op_number=final_op,
        tool_type=final_tool,
        title=final_title,
        delegated_by=delegator,
        assigned_to=assigned_to.strip() or current_user.full_name,
        supplier=final_supplier,
        instructions=clean_instructions,
        due_date=final_due,
        status="Atribuído",
        image_url=image_url
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    task_payload = {
        "type": "TASK_CREATED",
        "task": {
            "id": new_task.id,
            "sector_id": new_task.sector_id,
            "sector_name": new_task.sector_name,
            "op_number": new_task.op_number,
            "tool_type": new_task.tool_type,
            "title": new_task.title,
            "delegated_by": new_task.delegated_by,
            "assigned_to": new_task.assigned_to,
            "supplier": new_task.supplier,
            "instructions": new_task.instructions,
            "due_date": new_task.due_date,
            "status": new_task.status,
            "image_url": new_task.image_url
        }
    }
    spawn_bg(manager.broadcast(task_payload))

    return JSONResponse(content={"success": True, "task": task_payload["task"]})

@app.post("/posts/create-direct")
async def create_direct_post(
    request: Request,
    title: str = Form(...),
    tool_type: str = Form("Geral"),
    op_number: str = Form(""),
    supplier: str = Form("Interno"),
    instructions: str = Form(""),
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    image_url = None
    if image and image.filename:
        try:
            image_url = save_upload(image)
        except ValueError as err:
            return JSONResponse(status_code=400, content={"success": False, "message": str(err)})

    post = models.Post(
        author=current_user.full_name,
        delegated_by=current_user.full_name,
        supplier=supplier.strip() or "Interno",
        tool_type=tool_type.strip() or "Geral",
        op_number=op_number.strip(),
        title=title.strip(),
        instructions=instructions.strip(),
        due_date="Concluído",
        image_url=image_url
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    post_data = serialize_post(post, created_default="Agora")
    spawn_bg(manager.broadcast({"type": "POST_CREATED", "post": post_data}))

    return JSONResponse(content={"success": True, "post": post_data})

# =========================================================================
# ROTA: CRIAÇÃO MANUAL DE POST NO FEED (ESTILO INSTAGRAM)
# =========================================================================
@app.post("/api/posts/create-manual")
async def create_manual_post(
    request: Request,
    title: str = Form(...),
    tool_type: str = Form("GERAL"),
    op_number: str = Form(""),
    instructions: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    files = [f for f in (images or []) if f and f.filename]
    if len(files) > MAX_POST_IMAGES:
        return JSONResponse(status_code=400, content={"success": False, "message": f"Máximo de {MAX_POST_IMAGES} fotos por post."})

    urls = []
    for f in files:
        try:
            urls.append(save_upload(f))
        except ValueError as err:
            return JSONResponse(status_code=400, content={"success": False, "message": str(err)})

    post = models.Post(
        author=current_user.full_name,
        delegated_by=current_user.full_name,
        supplier=getattr(current_user, 'department', 'Operacional') or "Operacional",
        tool_type=tool_type.strip() or "GERAL",
        op_number=op_number.strip(),
        title=title.strip(),
        instructions=instructions.strip(),
        due_date="Concluído",
        image_url=urls[0] if urls else None,
        image_urls=json.dumps(urls) if urls else None
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    post_data = serialize_post(post, created_default="Agora")
    spawn_bg(manager.broadcast({"type": "NEW_FEED_POST", "post": post_data}))
    return JSONResponse(content={"success": True, "post": post_data})

@app.put("/api/posts/{post_id}")
async def update_post(
    request: Request,
    post_id: int,
    title: str = Form(...),
    tool_type: str = Form("GERAL"),
    op_number: str = Form(""),
    instructions: str = Form(""),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return unauthorized()
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado."})
    if not can_manage_post(db, current_user, post):
        return JSONResponse(status_code=403, content={"success": False, "message": "Só o autor ou o gestor pode editar este post."})
    clean_title = title.strip()
    if not clean_title:
        return JSONResponse(status_code=400, content={"success": False, "message": "Informe o título."})

    post.title = clean_title
    post.tool_type = tool_type.strip() or "GERAL"
    post.op_number = op_number.strip()
    post.instructions = instructions.strip()
    db.commit()
    db.refresh(post)

    post_data = serialize_post(post)
    spawn_bg(manager.broadcast({"type": "POST_UPDATED", "post": post_data}))
    return JSONResponse(content={"success": True, "post": post_data})

@app.delete("/api/posts/{post_id}")
async def delete_post(request: Request, post_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return unauthorized()
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado."})
    if not can_manage_post(db, current_user, post):
        return JSONResponse(status_code=403, content={"success": False, "message": "Só o autor ou o gestor pode apagar este post."})
    db.delete(post)
    db.commit()
    spawn_bg(manager.broadcast({"type": "POST_DELETED", "post_id": post_id}))
    return JSONResponse(content={"success": True})

class StatusUpdate(BaseModel):
    status: str

# ROTA DE ATUALIZAÇÃO BLINDADA COM ROLLBACK SEGURO
@app.put("/api/tasks/{task_id}/status")
async def update_task_status_api(request: Request, task_id: int, payload: StatusUpdate, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    if payload.status not in VALID_STATUSES:
        return JSONResponse(status_code=400, content={"success": False, "message": "Status inválido."})

    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"message": "Tarefa não encontrada"})

    team_names = get_team_names(db, current_user)
    if task.assigned_to not in team_names and task.delegated_by not in team_names:
        return JSONResponse(status_code=403, content={"success": False, "message": "Esta demanda não pertence à sua equipe."})

    old_status = task.status
    new_status = payload.status
    task.status = new_status

    published = False
    new_post_payload = None

    if new_status == "Aprovado" and old_status != "Aprovado":
        try:
            post = models.Post(
                sector_id=getattr(task, 'sector_id', None),
                sector_name=getattr(task, 'sector_name', None),
                author=task.assigned_to or "Colaborador",
                delegated_by=task.delegated_by or "Gestão",
                supplier=task.supplier or "Interno",
                tool_type=task.tool_type or "Geral",
                op_number=task.op_number or "",
                title=task.title or "Demanda Concluída",
                instructions=task.instructions or "",
                due_date=task.due_date or "Concluído",
                image_url=task.image_url
            )
            db.add(post)
            db.commit()
            db.refresh(post)
            published = True

            new_post_payload = serialize_post(post)
        except Exception as err:
            db.rollback()
            print(f"Aviso: Erro ao duplicar Post no Feed, mantendo status da Task: {err}")
            task.status = new_status
            db.commit()
    else:
        db.commit()

    spawn_bg(manager.broadcast({
        "type": "TASK_STATUS_UPDATED",
        "task_id": task_id,
        "old_status": old_status,
        "new_status": new_status,
        "assigned_to": task.assigned_to,
        "published_to_feed": published,
        "post": new_post_payload
    }))

    return {"success": True, "published_to_feed": published, "new_status": new_status}

@app.post("/posts/{post_id}/like")
async def like_post(request: Request, post_id: int, db: Session = Depends(get_db)):
    if not get_current_user(request, db):
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado"})
    
    post.likes = (post.likes or 0) + 1
    db.commit()
    
    spawn_bg(manager.broadcast({
        "type": "POST_LIKED",
        "post_id": post_id,
        "likes": post.likes
    }))

    return JSONResponse(content={"success": True, "likes": post.likes})

# ROTA DE COMENTÁRIOS COM PERSISTÊNCIA DIRETA
@app.post("/posts/{post_id}/comment")
async def add_comment(request: Request, post_id: int, text: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return unauthorized()
    author_name = current_user.full_name.strip()

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse(status_code=400, content={"success": False, "message": "Comentário vazio."})

    if not db.query(models.Post.id).filter(models.Post.id == post_id).first():
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado."})

    comment = models.Comment(post_id=post_id, author=author_name, text=clean_text)
    db.add(comment)
    db.commit()
    db.refresh(comment)

    comment_data = {
        "type": "NEW_COMMENT",
        "post_id": post_id,
        "author": author_name,
        "text": clean_text
    }
    spawn_bg(manager.broadcast(comment_data))

    return JSONResponse(content={"success": True, "comment": comment_data})
