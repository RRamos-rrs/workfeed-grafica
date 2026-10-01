from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(50), default="Colaborador")  # "Gestor" ou "Colaborador"
    password = Column(String(100), nullable=False)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    @property
    def department(self):
        return "Produção / Pré-Impressão"

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    op_number = Column(String(50), nullable=True)
    tool_type = Column(String(100), nullable=False)
    title = Column(String(200), nullable=False)
    delegated_by = Column(String(100))
    assigned_to = Column(String(100), nullable=False)
    supplier = Column(String(100), nullable=False)
    instructions = Column(Text, nullable=True)
    due_date = Column(String(50), nullable=False)
    status = Column(String(50), default="Atribuído")
    image_url = Column(String(500), nullable=True)

class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    author = Column(String(100))
    delegated_by = Column(String(100))
    supplier = Column(String(100))
    tool_type = Column(String(100))
    op_number = Column(String(50))
    title = Column(String(200))
    instructions = Column(Text, nullable=True)
    due_date = Column(String(50))
    image_url = Column(String(500), nullable=True)
    likes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")

class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"))
    author = Column(String(100))
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    post = relationship("Post", back_populates="comments")
