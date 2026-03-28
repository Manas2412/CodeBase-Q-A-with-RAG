import pgvector.sqlalchemy as Vector
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, func
import uuid


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    github_url: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # pending|indexing|ready|error
    indexed_at: Mapped[DateTime] = mapped_column(nullable=True)
    created_at: Mapped[DateTime] = mapped_column(server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(index=True)
    file_path: Mapped[str]
    language: Mapped[str]
    chunk_type: Mapped[str]  # function | class | module
    name: [str] = mapped_column(nullable=True)
    start_line: Mapped[int]
    end_line: Mapped[int]
    content: Mapped[str]
    contect_prefix: Mapped[str]  # "src/auth/jwt.py > class JWTService > def verify"
    embedding: Mapped[Vector] = mapped_column(Vector(1024))  # voyage-code-2 dim
