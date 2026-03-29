# text.py
import asyncio
from app.query.hyde import hyde_expand
from app.query.answerer import stream_answer

async def test():
    print("--- 1. Testing HyDE Expansion (`hyde.py`) ---")
    query = "where is JWT verification handled?"
    print(f"Original Query: {query}")
    
    hyde_result = await hyde_expand(query)
    print(f"\nHyDE Generated Code Chunk (Hypothetical):\n{hyde_result}\n\n")

    print("="*60 + "\n")

    print("--- 2. Testing Streaming Answerer (`answerer.py`) ---")
    # Fake a retrieved chunk to test streaming without hitting the DB yet
    fake_chunks = [{
        "context_prefix": "src/auth/jwt.py > JWTService > verify",
        "content": "def verify(token: str) -> dict:\n    return jwt.decode(token, SECRET)",
        "start_line": 42,
    }]
    
    print("Answer streamed from Ollama: ", end="")
    async for token in stream_answer("how does JWT verification work?", fake_chunks):
        print(token, end="", flush=True)
    print("\n")

if __name__ == "__main__":
    asyncio.run(test())