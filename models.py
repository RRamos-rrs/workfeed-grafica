from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class SectorConfig(Base):
    __tablename__ = "sector_configs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    icon = Column(String(20), default="📁")
    
    # Armazena a lista dinâmica de campos em JSON:
    # [{"id": "tool_type", "label": "Tipo de Ferramental", "type": "select", "options": "Faca,Clichê", "required": true}, ...]
    fields_schema = Column(Text, nullable=False)

    # Colunas de compatibilidade legado (evitam falhas em consultas anteriores)
    tool_types_csv = Column(Text, nullable=True)
    ref_label = Column(String(50), nullable=True)
    ref_placeholder = Column(String(100), nullable=True)
    title_label = Column(String(100), nullable=True)
    title_placeholder = Column(String(100), nullable=True)
    entity_label = Column(String(100), nullable=True)
    entity_placeholder = Column(String(100), nullable=True)
    instructions_label = Column(String(100), nullable=True)
    instructions_placeholder = Column(String(255), nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(50), default="Colaborador")  # "Gestor" ou "Colaborador"
    department = Column(String(100), default="Produção / Pré-Impressão")
    password = Column(String(100), nullable=False)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    sector_id = Column(Integer, nullable=True)
    sector_name = Column(String(100), nullable=True)
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
    sector_id = Column(Integer, nullable=True)
    sector_name = Column(String(100), nullable=True)
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
