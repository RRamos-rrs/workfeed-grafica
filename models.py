from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class SectorConfig(Base):
    __tablename__ = "sector_configs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    icon = Column(String(20), default="📁")
    owner_id = Column(Integer, nullable=True)  # gestor dono do setor (None = setor padrão partilhado)
    
    # Armazena a lista dinâmica de campos em JSON
    fields_schema = Column(Text, nullable=False)

    # Colunas legadas de compatibilidade
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
    role = Column(String(50), default="Colaborador")
    department = Column(String(100), default="Produção / Pré-Impressão")
    password = Column(String(100), nullable=False)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    sector_id = Column(Integer, nullable=True)
    sector_name = Column(String(100), nullable=True)
    op_number = Column(String(50), nullable=True)
    tool_type = Column(String(100), nullable=True, default="Geral")
    title = Column(String(200), nullable=False, default="Demanda Operacional")
    delegated_by = Column(String(100), nullable=True)
    assigned_to = Column(String(100), nullable=True, default="Equipe")
    supplier = Column(String(100), nullable=True, default="Interno")
    instructions = Column(Text, nullable=True)
    due_date = Column(String(50), nullable=True, default="A definir")
    status = Column(String(50), default="Atribuído")
    image_url = Column(String(500), nullable=True)

class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    sector_id = Column(Integer, nullable=True)
    sector_name = Column(String(100), nullable=True)
    author = Column(String(100), nullable=True)
    delegated_by = Column(String(100), nullable=True)
    supplier = Column(String(100), nullable=True)
    tool_type = Column(String(100), nullable=True)
    op_number = Column(String(50), nullable=True)
    title = Column(String(200), nullable=True)
    instructions = Column(Text, nullable=True)
    due_date = Column(String(50), nullable=True)
    image_url = Column(String(500), nullable=True)
    image_urls = Column(Text, nullable=True)  # lista JSON com todas as fotos do post (carrossel)
    likes = Column(Integer, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow)

    # Carregamento imediato (lazy='joined') garante que comentários sempre vêm anexados ao Post
    comments = relationship(
        "Comment", 
        back_populates="post", 
        cascade="all, delete-orphan",
        lazy="joined",
        order_by="Comment.created_at.asc()"
    )

class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    author = Column(String(100), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    post = relationship("Post", back_populates="comments")
