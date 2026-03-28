import asyncpg
from pgvector.asyncpg import register_vector
from app.ingestion.chunker import CodeChunk
import uuid

async def upsert_chunks(
    conn: asyncpg.Connection,
    repo_id: uuid.UUID,
    chunks: list[CodeChunk],
    embeddings: list[list[float]]
) -> None:
    await register_vector(conn)
    await conn.executemany("""
        INSERT INTO code_chunks 
            (repo_id, file_path, language, chunk_type, name,  start_line, end_line, content, context_prefix, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (repo_id, file_path, start_line) DO UPDATE 
            SET content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                context_prefix = EXCLUDED.context_prefix
    """, [
        (repo_id, c.file_path, c.language, c.chunk_type, c.name, c.start_line, c.end_line, c.content, c.context_prefix, emb)
        for c, emb in zip(chunks, embeddings)
    ])