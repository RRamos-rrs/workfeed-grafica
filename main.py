import os
import shutil
import json
import asyncio
from typing import List
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, text
from pydantic import BaseModel

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

models.Base.metadata.create_all(bind=engine)

# Diretórios necessários para uploads e ficheiros estáticos
os.makedirs("uploads", exist_ok=True)
os.makedirs("static/img", exist_ok=True)

app = FastAPI()

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

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
        db.add_all(default_sectors)
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

    sectors = db.query(models.SectorConfig).all()

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

    clean_name = name.strip()
    sector = db.query(models.SectorConfig).filter(models.SectorConfig.id == sector_id).first() if sector_id > 0 else None

    if not sector:
        sector = models.SectorConfig(
            name=clean_name,
            icon=icon.strip() or "📁",
            fields_schema=fields_schema_json.strip()
        )
        db.add(sector)
    else:
        sector.name = clean_name
        sector.icon = icon.strip() or "📁"
        sector.fields_schema = fields_schema_json.strip()

    db.commit()
    asyncio.create_task(manager.broadcast({"type": "REFRESH"}))
    return JSONResponse(content={"success": True, "message": "Modelo de setor salvo com sucesso!"})

@app.delete("/api/sectors/{sector_id}")
async def delete_sector(request: Request, sector_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "message": "Sessão expirada."})

    role_normalized = (current_user.role or "").strip().lower()
    if role_normalized not in ["gestor", "manager", "admin"]:
        return JSONResponse(status_code=403, content={"success": False, "message": "Apenas gestores podem remover modelos de setores."})

    sector = db.query(models.SectorConfig).filter(models.SectorConfig.id == sector_id).first()
    if not sector:
        return JSONResponse(status_code=404, content={"success": False, "message": "Setor não encontrado."})

    db.delete(sector)
    db.commit()
    asyncio.create_task(manager.broadcast({"type": "REFRESH"}))
    return JSONResponse(content={"success": True, "message": "Setor removido com sucesso!"})

@app.get("/api/users/profile/{full_name}")
def get_user_profile(full_name: str, db: Session = Depends(get_db)):
    clean_name = full_name.strip()
    user = db.query(models.User).filter(models.User.full_name.ilike(clean_name)).first()
    if not user:
        return JSONResponse(status_code=404, content={"message": "Colaborador não encontrado"})
    
    tasks = db.query(models.Task).filter(models.Task.assigned_to.ilike(clean_name)).all()
    posts = db.query(models.Post).filter(models.Post.author.ilike(clean_name)).all()
    
    return {
        "full_name": user.full_name,
        "username": user.username,
        "role": user.role,
        "department": getattr(user, 'department', 'Geral / Operacional'),
        "pending_tasks": len([t for t in tasks if t.status != "Aprovado"]),
        "completed_tasks": len(posts)
    }

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
    redirect.set_cookie(key="user_session", value=user.username, httponly=True)
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
    existing = db.query(models.User).filter(models.User.username.ilike(clean_username)).first()
    if existing:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Este nome de usuário já existe"}
        )

    new_user = models.User(
        full_name=full_name.strip(),
        username=clean_username,
        role=role,
        department=department.strip(),
        password=password,
        manager_id=None
    )
    db.add(new_user)
    db.commit()

    asyncio.create_task(manager.broadcast({"type": "REFRESH"}))

    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="user_session", value=clean_username, httponly=True)
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

    role_normalized = (current_user.role or "").strip().lower()
    is_manager = role_normalized in ["gestor", "manager", "admin"]
    manager_ref_id = current_user.id if is_manager else current_user.manager_id
    clean_username = username.strip()
    clean_full_name = full_name.strip()

    existing = db.query(models.User).filter(models.User.username.ilike(clean_username)).first()
    
    if existing:
        existing.full_name = clean_full_name
        existing.role = role
        existing.department = department.strip()
        existing.password = password
        existing.manager_id = manager_ref_id
        db.commit()

        asyncio.create_task(manager.broadcast({"type": "REFRESH"}))
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

    asyncio.create_task(manager.broadcast({"type": "REFRESH"}))
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

    user_to_delete = db.query(models.User).filter(models.User.username.ilike(clean_username)).first()
    if not user_to_delete:
        return JSONResponse(status_code=404, content={"success": False, "message": "Colaborador não encontrado."})

    if user_to_delete.manager_id != current_user.id:
        return JSONResponse(status_code=403, content={"success": False, "message": "Este colaborador não pertence à sua equipe."})

    del_username = user_to_delete.username
    del_full_name = user_to_delete.full_name

    db.delete(user_to_delete)
    db.commit()

    asyncio.create_task(manager.broadcast({
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
    delegator = current_user.full_name.strip() if current_user else "Gestão"

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
    final_op = clean_op or "S/N"
    final_due = clean_due or "A definir"

    if extra_details:
        adicional = " | ".join(extra_details)
        clean_instructions = f"{clean_instructions} ({adicional})".strip() if clean_instructions else adicional

    image_url = None
    if image and image.filename:
        file_path = f"uploads/{image.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        image_url = f"/{file_path}"

    new_task = models.Task(
        sector_id=sector_id if sector_id > 0 else None,
        sector_name=sector_name.strip() if sector_name else None,
        op_number=final_op,
        tool_type=final_tool,
        title=final_title,
        delegated_by=delegator,
        assigned_to=assigned_to.strip() or (current_user.full_name if current_user else "Equipe"),
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
    asyncio.create_task(manager.broadcast(task_payload))

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
        file_path = f"uploads/{image.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        image_url = f"/{file_path}"

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

    post_payload = {
        "type": "POST_CREATED",
        "post": {
            "id": post.id,
            "author": post.author,
            "tool_type": post.tool_type,
            "op_number": post.op_number,
            "title": post.title,
            "supplier": post.supplier,
            "due_date": post.due_date,
            "instructions": post.instructions,
            "image_url": post.image_url,
            "created_at": post.created_at.strftime('%d/%m/%Y %H:%M') if getattr(post, 'created_at', None) else "",
            "likes": 0,
            "comments": []
        }
    }
    asyncio.create_task(manager.broadcast(post_payload))

    return JSONResponse(content={"success": True, "post": post_payload["post"]})

class StatusUpdate(BaseModel):
    status: str

# ROTA DE ATUALIZAÇÃO BLINDADA COM ROLLBACK SEGURO
@app.put("/api/tasks/{task_id}/status")
async def update_task_status_api(task_id: int, payload: StatusUpdate, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"message": "Tarefa não encontrada"})

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

            new_post_payload = {
                "id": post.id,
                "sector_id": getattr(post, 'sector_id', None),
                "sector_name": getattr(post, 'sector_name', None),
                "author": post.author,
                "tool_type": post.tool_type,
                "op_number": post.op_number,
                "title": post.title,
                "supplier": post.supplier,
                "due_date": post.due_date,
                "instructions": post.instructions,
                "image_url": post.image_url,
                "created_at": post.created_at.strftime('%d/%m/%Y %H:%M') if getattr(post, 'created_at', None) else "",
                "likes": 0,
                "comments": []
            }
        except Exception as err:
            db.rollback()
            print(f"Aviso: Erro ao duplicar Post no Feed, mantendo status da Task: {err}")
            task.status = new_status
            db.commit()
    else:
        db.commit()

    asyncio.create_task(manager.broadcast({
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
async def like_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado"})
    
    post.likes = (post.likes or 0) + 1
    db.commit()
    
    asyncio.create_task(manager.broadcast({
        "type": "POST_LIKED",
        "post_id": post_id,
        "likes": post.likes
    }))

    return JSONResponse(content={"success": True, "likes": post.likes})

# ROTA DE COMENTÁRIOS COM PERSISTÊNCIA DIRETA
@app.post("/posts/{post_id}/comment")
async def add_comment(request: Request, post_id: int, text: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    author_name = current_user.full_name.strip() if current_user else "Colaborador"

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse(status_code=400, content={"success": False, "message": "Comentário vazio."})

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
    asyncio.create_task(manager.broadcast(comment_data))

    return JSONResponse(content={"success": True, "comment": comment_data})
