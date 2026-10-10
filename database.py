import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Obtém a URL do PostgreSQL configurada no Render, ou usa SQLite local
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./workfeed.db")

# Ajusta a formatação caso o Render forneça 'postgres://' em vez de 'postgresql://'
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Criação do motor da base de dados
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # pool_pre_ping evita erro em conexões que o servidor fechou; reaproveitar conexões acelera cada pedido
    engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True, pool_recycle=240, pool_size=5, max_overflow=5)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
