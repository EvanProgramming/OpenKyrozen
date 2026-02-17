import chromadb
import time

class MemoryBank:
    def __init__(self):
        # Initialize persistent storage, data will be stored in the agent_db folder in the current directory
        self.client = chromadb.PersistentClient(path="./agent_db")
        # Create a collection (similar to a SQL table) for storing Agent operation logs
        self.collection = self.client.get_or_create_collection(name="agent_logs")

    def add_log(self, text):
        """
        [MemoryBank] Store successful operation: {text[:30]}...
        """
        # Use timestamp as unique ID
        log_id = str(time.time())
        self.collection.add(
            documents=[text],
            ids=[log_id]
        )
        print(f"   [MemoryBank] Stored experience: {text[:30]}...")

    def recall(self, query):
        """
        [MemoryBank] Recall experience: {query[:30]}...
        """
        results = self.collection.query(
            query_texts=[query],
            n_results=2  # Only recall the most relevant two items
        )
        
        # ChromaDB returns a list of lists, we need to flatten it
        if results['documents']:
            return results['documents'][0]
        return []