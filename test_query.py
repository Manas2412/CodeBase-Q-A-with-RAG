import asyncio
import os
import uuid
import json
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from app.db.database import DATABASE_URL
from app.query.retriever import retrieve
from pgvector.asyncpg import register_vector

load_dotenv()

class MockRedis:
    def __init__(self):
        self.cache = {}
    async def get(self, key):
        return self.cache.get(key)
    async def setex(self, key, time, value):
        self.cache[key] = value

async def test():
    engine = create_async_engine(DATABASE_URL)
    redis_client = MockRedis()
    
    async with engine.connect() as conn:
        raw_conn = await conn.get_raw_connection()
        driver_connection = raw_conn.driver_connection
        
        await register_vector(driver_connection)
        repo_id = str(uuid.uuid4())
        await driver_connection.execute(
            "INSERT INTO repos (id, github_url, status) VALUES ($1, $2, $3)", 
            uuid.UUID(repo_id), f"https://github.com/manas/test-retriever-{repo_id}", "ready"
        )
        # Mock Vector
        dummy_vec = [0.1] * 1536
        
        await driver_connection.execute(
            "INSERT INTO chunks (id, repo_id, file_path, language, chunk_type, name, start_line, end_line, content, context_prefix, embedding) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            uuid.uuid4(), uuid.UUID(repo_id), "main.py", "python", "function", "main", 1, 10, "def main(): print('hello')", "main", dummy_vec
        )

        
        # Test retrieve
        print(f"Testing retrieve on mock repo: {repo_id}")
        query="what is inside the database?"
        try:
            results = await retrieve(
                query=query, 
                repo_id=repo_id, 
                conn=driver_connection, 
                redis_client=redis_client,
                top_k=2
            )
            print("Results length:", len(results))
            for i, r in enumerate(results):
                print(f"[{i+1}] Score: {r['relevance_score']:.4f}")
                print(f"    Prefix: {r['context_prefix']}")
                print(f"    Path: {r['file_path']}")
        except Exception as e:
            print(f"Exception during retrieve: {e!r}")
        finally:
            # Clean up
            await driver_connection.execute("DELETE FROM chunks WHERE repo_id = $1", uuid.UUID(repo_id))
            await driver_connection.execute("DELETE FROM repos WHERE id = $1", uuid.UUID(repo_id))

if __name__ == "__main__":
    asyncio.run(test())
