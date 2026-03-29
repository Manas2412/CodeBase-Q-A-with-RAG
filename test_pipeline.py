import asyncio
import uuid
from sqlalchemy.ext.asyncio import create_async_engine
from app.ingestion.chunker import chunk_file
from app.ingestion.embedder import embed_chunks
from app.ingestion.indexer import upsert_chunks
from app.db.database import DATABASE_URL
from dotenv import load_dotenv

load_dotenv()

async def test_end_to_end():
    # 1. Test Chunker
    print("--- 1. Testing AST Code Chunker ---")
    sample_code = """
def hello_world():
    print("Hello, world!")
    return True

class DemoClass:
    def method(self):
        return "Test"
"""
    chunks = chunk_file("demo.py", sample_code, "python")
    print(f"Generated {len(chunks)} chunks:")
    for c in chunks:
        print(f" - {c.chunk_type}: {c.name} ({c.start_line} to {c.end_line})")

    if not chunks:
        print("Chunker failed to generate chunks!")
        return

    # 2. Test Embedder (Voyage AI)
    print("\n--- 2. Testing Voyage AI Embedder ---")
    try:
        embeddings = await embed_chunks(chunks)
        print(f"Generated {len(embeddings)} embeddings.")
        if embeddings:
            print(f"Embedding dimension: {len(embeddings[0])}")
    except Exception as e:
        print(f"Embedder failed to connect to Voyage AI: {e}")
        return

    # 3. Test Indexer (PostgreSQL vector DB)
    print("\n--- 3. Testing PostgreSQL Vector Indexer ---")
    try:
        engine = create_async_engine(DATABASE_URL)
        async with engine.connect() as conn:
            raw_conn = await conn.get_raw_connection()
            driver_connection = raw_conn.driver_connection
            
            repo_id = uuid.uuid4()
            test_url = f"https://github.com/test/demo-{repo_id}"
            
            # Since chunks has a foreign key to repos, we must insert a repo row first
            await driver_connection.execute(
                "INSERT INTO repos (id, github_url, status) VALUES ($1, $2, $3)", 
                repo_id, test_url, "ready"
            )
            
            # Upsert into chunks
            await upsert_chunks(driver_connection, repo_id, chunks, embeddings)
            print(f"Successfully inserted {len(chunks)} embedded chunks into the database!")
            
            # Clean up test data
            await driver_connection.execute("DELETE FROM chunks WHERE repo_id = $1", repo_id)
            await driver_connection.execute("DELETE FROM repos WHERE id = $1", repo_id)
            print("Successfully cleaned up test data from DB.")
    except Exception as e:
        print(f"DB Indexer failed: {e}")
        return
        
    print("\n✅ End-to-End test completed perfectly!")

if __name__ == "__main__":
    asyncio.run(test_end_to_end())
