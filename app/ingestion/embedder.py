import voyageai
from app.ingestion.chunker import CodeChunk
from dotenv import load_dotenv

load_dotenv()

client = voyageai.AsyncClient()

async def embed_chunks(chunks: list[CodeChunk]) -> list[list[float]]:
    """Embed a list of code chunks using Voyage AI"""
    """Embed in batches of 128. voyage-code-2 max input = 16k tokens."""
    texts = [f"{c.context_prefix}\n\n{c.content}" for c in chunks]
    all_embeddings = []
    
    for i in range(0, len(texts), 128):
        batch = texts[i:i+128]
        result = await client.embed(
            batch,
            model="voyage-code-2",
            input_type="document",  ## "document" for indexing, "query" for quering
        )
        all_embeddings.extend(result.embeddings)
        
    return all_embeddings
    