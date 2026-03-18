import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from matching_system.database import db_manager
from matching_system.ai_services import ai_service

async def seed_community_embeddings():
    print("Initializing database...")
    await db_manager.initialize_postgres()
    db_manager.initialize_pinecone()

    print("Fetching communities...")
    communities = await db_manager.fetch_all_communities()

    for community in communities:
        payload = f"""
        Name: {community['community_name']}
        Category: {community['category']}
        Description: {community.get('description', '')}
        """

        print(f"Generating embedding for {community['community_id']}")

        vector = ai_service.generate_embedding(payload)

        db_manager.upsert_vector(
            vector_id=community["community_id"],
            vector=vector,
            metadata={
                "community_id": community["community_id"],
                "category": community["category"],
                "city": community["city"],
                "timezone": community["timezone"]
            }
        )

    print("Seeding completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed_community_embeddings())
