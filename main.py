import os
import shutil
from typing import List
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, text
from pydantic import BaseModel

import models
from database import engine, get_db

# Garante que a coluna manager_id exista na tabela users ativa antes de qualquer consulta
try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_id INTEGER REFERENCES users(id);"))
except Exception as e:
    print(f"Aviso de migração automática: {e}")

models.Base.metadata.create_all(bind=engine)
os.makedirs("uploads", exist_ok=True)

app = FastAPI()
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
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
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Mantém conexão viva respondendo ao heartbeat do cliente
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

# ROTA PRINCIPAL (Isolamento por equipe e Feed unificado)
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login")

    role_normalized = (current_user.role or "").strip().lower()
    is_manager = role_normalized in ["gestor", "manager", "admin"]
    
    # Identifica o ID do Gestor da equipe
    manager_ref_id = current_user.id if is_manager else current_user.manager_id

    # Busca todos os membros da mesma equipe (gestor + colaboradores)
    if manager_ref_id:
        team_users = db.query(models.User).filter(
            or_(models.User.id == manager_ref_id, models.User.manager_id == manager_ref_id)
        ).all()
    else:
        team_users = [current_user]

    team_user_names = [u.full_name for u in team_users]

    # Tarefas da equipe inteira (atribuídas ou delegadas)
    all_tasks = db.query(models.Task).filter(
        or_(
            models.Task.assigned_to.in_(team_user_names),
            models.Task.delegated_by.in_(team_user_names)
        )
    ).all()

    # Feed unificado da equipe
    posts = db.query(models.Post).filter(
        or_(
            models.Post.author.in_(team_user_names),
            models.Post.delegated_by.in_(team_user_names)
        )
    ).order_by(models.Post.created_at.desc()).all()

    my_tasks = [t for t in all_tasks if t.assigned_to == current_user.full_name]
    my_pending_tasks = [t for t in my_tasks if t.status == "Atribuído"]
    my_in_progress_tasks = [t for t in my_tasks if t.status == "Em Produção"]
    my_completed_posts = [p for p in posts if p.author == current_user.full_name]

    collaborators = []
    for u in team_users:
        user_tasks = [t for t in all_tasks if t.assigned_to == u.full_name]
        user_posts = [p for p in posts if p.author == u.full_name]
        active_count = len([t for t in user_tasks if t.status != "Aprovado"])
        approved_count = len(user_posts)

        collaborators.append({
            "name": u.full_name,
            "username": u.username,
            "role": u.role,
            "department": getattr(u, 'department', 'Produção / Pré-Impressão'),
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
            "collaborators": collaborators
        }
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/api/users/profile/{full_name}")
def get_user_profile(full_name: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.full_name == full_name).first()
    if not user:
        return JSONResponse(status_code=404, content={"message": "Colaborador não encontrado"})
    
    tasks = db.query(models.Task).filter(models.Task.assigned_to == full_name).all()
    posts = db.query(models.Post).filter(models.Post.author == full_name).all()
    
    return {
        "full_name": user.full_name,
        "username": user.username,
        "role": user.role,
        "department": getattr(user, 'department', 'Produção / Pré-Impressão'),
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
        full_name=full_name,
        username=clean_username,
        role=role,
        password=password,
        manager_id=None
    )
    db.add(new_user)
    db.commit()

    await manager.broadcast({"type": "REFRESH"})

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

    existing = db.query(models.User).filter(models.User.username.ilike(clean_username)).first()
    
    if existing:
        existing.full_name = full_name
        existing.role = role
        existing.password = password
        existing.manager_id = manager_ref_id
        db.commit()

        await manager.broadcast({
            "type": "USER_UPSERTED",
            "name": full_name,
            "username": clean_username,
            "role": role,
            "department": department
        })
        return JSONResponse(content={"success": True, "message": "Colaborador vinculado à sua equipe com sucesso!"})

    new_user = models.User(
        full_name=full_name,
        username=clean_username,
        role=role,
        password=password,
        manager_id=manager_ref_id
    )
    db.add(new_user)
    db.commit()

    await manager.broadcast({
        "type": "USER_UPSERTED",
        "name": full_name,
        "username": clean_username,
        "role": role,
        "department": department
    })

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

    # Transmite exclusão granular sem provocar recarregamento da janela
    await manager.broadcast({
        "type": "USER_DELETED",
        "username": del_username,
        "full_name": del_full_name
    })

    return JSONResponse(content={"success": True, "message": "Colaborador removido com sucesso!"})

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("user_session")
    return response

@app.post("/tasks/create")
async def create_task(
    request: Request,
    op_number: str = Form(""),
    tool_type: str = Form(...),
    title: str = Form(...),
    assigned_to: str = Form(...),
    supplier: str = Form(...),
    instructions: str = Form(""),
    due_date: str = Form(...),
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    delegator = current_user.full_name if current_user else "Gestão"

    image_url = None
    if image and image.filename:
        file_path = f"uploads/{image.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        image_url = f"/{file_path}"

    new_task = models.Task(
        op_number=op_number,
        tool_type=tool_type,
        title=title,
        delegated_by=delegator,
        assigned_to=assigned_to,
        supplier=supplier,
        instructions=instructions,
        due_date=due_date,
        status="Atribuído",
        image_url=image_url
    )
    db.add(new_task)
    db.commit()

    task_payload = {
        "type": "TASK_CREATED",
        "task": {
            "id": new_task.id,
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
    await manager.broadcast(task_payload)

    # Se a requisição veio de fetch assíncrono, responde com JSON
    if request.headers.get("accept", "").find("application/json") != -1 or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(content={"success": True, "task": task_payload["task"]})

    return RedirectResponse(url="/", status_code=303)

class StatusUpdate(BaseModel):
    status: str

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
        post = models.Post(
            author=task.assigned_to,
            delegated_by=task.delegated_by,
            supplier=task.supplier,
            tool_type=task.tool_type,
            op_number=task.op_number,
            title=task.title,
            instructions=task.instructions,
            due_date=task.due_date,
            image_url=task.image_url
        )
        db.add(post)
        db.commit()
        published = True

        new_post_payload = {
            "id": post.id,
            "author": post.author,
            "tool_type": post.tool_type,
            "op_number": post.op_number,
            "title": post.title,
            "supplier": post.supplier,
            "due_date": post.due_date,
            "instructions": post.instructions,
            "image_url": post.image_url,
            "likes": 0,
            "comments": []
        }
    else:
        db.commit()

    # Emite atualização seletiva para a interface sem refresh geral
    await manager.broadcast({
        "type": "TASK_STATUS_UPDATED",
        "task_id": task_id,
        "new_status": new_status,
        "assigned_to": task.assigned_to,
        "published_to_feed": published,
        "post": new_post_payload
    })

    return {"success": True, "published_to_feed": published, "new_status": new_status}

# ROTA SILENCIOSA DE CURTIDAS (SEM REDIRECIONAR / SEM RELOAD)
@app.post("/posts/{post_id}/like")
async def like_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return JSONResponse(status_code=404, content={"success": False, "message": "Post não encontrado"})
    
    post.likes += 1
    db.commit()
    
    await manager.broadcast({
        "type": "POST_LIKED",
        "post_id": post_id,
        "likes": post.likes
    })

    return JSONResponse(content={"success": True, "likes": post.likes})

# ROTA SILENCIOSA DE COMENTÁRIOS (SEM REDIRECIONAR / SEM RELOAD)
@app.post("/posts/{post_id}/comment")
async def add_comment(request: Request, post_id: int, text: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    author_name = current_user.full_name if current_user else "Colaborador"

    comment = models.Comment(post_id=post_id, author=author_name, text=text.strip())
    db.add(comment)
    db.commit()

    comment_data = {
        "type": "NEW_COMMENT",
        "post_id": post_id,
        "author": author_name,
        "text": text.strip()
    }
    await manager.broadcast(comment_data)

    return JSONResponse(content={"success": True, "comment": comment_data})
