import chromadb
import time

class MemoryBank:
    def __init__(self):
        # Initialize persistent storage in a local folder
        self.client = chromadb.PersistentClient(path="./agent_db")
        # 'distance_func' is implicit (cosine similarity usually)
        self.collection = self.client.get_or_create_collection(name="agent_logs")

    def add_log(self, text):
        """
        Saves a successful interaction log to the database.
        """
        log_id = str(time.time())
        self.collection.add(
            documents=[text],
            ids=[log_id]
        )
        print(f"   [Memory] Log saved: {text[:50]}...")

    def recall(self, query):
        """
        Retrieves relevant past experiences based on the query.
        """
        results = self.collection.query(
            query_texts=[query],
            n_results=2  # Top 2 relevant memories
        )
        
        if results['documents']:
            return results['documents'][0]
        return []