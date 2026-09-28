import os
import shutil
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel

import models
from database import engine, get_db

models.Base.metadata.create_all(bind=engine)
os.makedirs("uploads", exist_ok=True)

app = FastAPI()
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

# Obter utilizador com sessão ativa via cookie
def get_current_user(request: Request, db: Session):
    username = request.cookies.get("user_session")
    if not username:
        return None
    return db.query(models.User).filter(models.User.username == username).first()

# ROTA PRINCIPAL (Protegida)
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login")

    all_tasks = db.query(models.Task).all()
    posts = db.query(models.Post).order_by(models.Post.created_at.desc()).all()
    all_users = db.query(models.User).all()

    # Tarefas específicas do colaborador logado
    my_tasks = [t for t in all_tasks if t.assigned_to == current_user.full_name]
    my_pending_tasks = [t for t in my_tasks if t.status == "Atribuído"]
    my_in_progress_tasks = [t for t in my_tasks if t.status == "Em Produção"]
    my_completed_posts = [p for p in posts if p.author == current_user.full_name]

    # Métricas para a secção de equipa
    collaborators = []
    for u in all_users:
        user_tasks = [t for t in all_tasks if t.assigned_to == u.full_name]
        user_posts = [p for p in posts if p.author == u.full_name]
        active_count = len([t for t in user_tasks if t.status != "Aprovado"])
        approved_count = len(user_posts)

        collaborators.append({
            "name": u.full_name,
            "role": u.role,
            "active_tasks": active_count,
            "approved_tasks": approved_count,
            "recent_posts": user_posts[:3]
        })

    return templates.TemplateResponse(
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
            "users": all_users,
            "collaborators": collaborators
        }
    )

# ROTAS DE AUTENTICAÇÃO
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
            context={"error": "Utilizador ou palavra-passe incorretos"}
        )
    
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="user_session", value=user.username, httponly=True)
    return redirect

@app.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    username: str = Form(...),
    role: str = Form("Colaborador"),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Este nome de utilizador já existe"}
        )

    new_user = models.User(full_name=full_name, username=username, role=role, password=password)
    db.add(new_user)
    db.commit()

    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(key="user_session", value=username, httponly=True)
    return redirect

@app.post("/api/users/add-collaborator")
def add_collaborator(
    full_name: str = Form(...),
    username: str = Form(...),
    role: str = Form("Colaborador"),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Este nome de utilizador já existe!"}
        )

    new_user = models.User(
        full_name=full_name,
        username=username,
        role=role,
        password=password
    )
    db.add(new_user)
    db.commit()

    return JSONResponse(content={"success": True, "message": "Colaborador adicionado com sucesso!"})
    
    new_user = models.User(
        full_name=full_name,
        username=username,
        role=role,
        password=password
    )
    db.add(new_user)
    db.commit()
    
    return JSONResponse(content={"success": true, "message": "Colaborador adicionado com sucesso!"})
@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("user_session")
    return response

# ROTA DE CRIAÇÃO DE TAREFA (DELEGAÇÃO)
@app.post("/tasks/create")
def create_task(
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
    delegator = current_user.full_name if current_user else "Robson Ramos (Gestão)"

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
    return RedirectResponse(url="/", status_code=303)

class StatusUpdate(BaseModel):
    status: str

@app.put("/api/tasks/{task_id}/status")
def update_task_status_api(task_id: int, payload: StatusUpdate, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"message": "Tarefa não encontrada"})

    old_status = task.status
    new_status = payload.status
    task.status = new_status

    published = False
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
        published = True

    db.commit()
    return {"success": True, "published_to_feed": published}

@app.post("/posts/{post_id}/like")
def like_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post:
        post.likes += 1
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/posts/{post_id}/comment")
def add_comment(request: Request, post_id: int, text: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    author_name = current_user.full_name if current_user else "Robson Ramos"

    comment = models.Comment(post_id=post_id, author=author_name, text=text)
    db.add(comment)
    db.commit()
    return RedirectResponse(url="/", status_code=303)
