import ollama
import json
from memory import MemoryBank
from tools import execute_tool, AVAILABLE_TOOLS

# Initialize
memory = MemoryBank()
# Change this to your model name
MODEL_NAME = "gpt-oss:20b" 
# MODEL_NAME = "qwen2.5-coder:32b" 

def parse_json_from_response(response_text):
    """
    Extracts and parses JSON from the LLM output.
    Returns (action, args) or (None, None).
    """
    try:
        cleaned = response_text.strip()
        # Find the first '{' and last '}'
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        
        if start != -1 and end != -1:
            json_str = cleaned[start:end]
            data = json.loads(json_str)
            return data.get("action"), data.get("args")
    except Exception:
        pass
    return None, None

def chat_with_llm(messages):
    """
    Sends the message history to Ollama.
    """
    print("   [Thinking] ...")
    response = ollama.chat(model=MODEL_NAME, messages=messages)
    return response['message']['content']

def main():
    print(f"🤖 Agent ({MODEL_NAME}) Initialized. Type 'exit' to quit.")
    
    while True:
        user_input = input("\nUser: ")
        if user_input.lower() == "exit":
            break

        # --- Step 1: Recall (RAG) ---
        relevant_memory = memory.recall(user_input)
        memory_context = "\n".join(relevant_memory) if relevant_memory else "No relevant past memories."
        
        # --- Step 2: Construct System Prompt ---
        tools_list = "\n".join([f"- {k}" for k in AVAILABLE_TOOLS.keys()])
        
        system_prompt = f"""
        You are an autonomous AI Agent with self-correction capabilities.
        
        [AVAILABLE TOOLS]:
        {tools_list}
        
        [YOUR MEMORY / PAST EXPERIENCE]:
        {memory_context}
        
        [INSTRUCTIONS]:
        1. If the user asks a simple question, reply directly.
        2. If you need to use a tool, you MUST output a JSON object strictly in this format:
           {{ "action": "tool_name", "args": "arguments_string" }}
        3. Do NOT output markdown code blocks (like ```json). Just the raw JSON.
        4. If a tool fails, analyze the error and try to fix it (e.g., search for docs, change arguments).
        """

        # Maintain a short conversation history for the current session
        conversation_history = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_input}
        ]
        
        # --- Step 3: Action Loop (Self-Correction) ---
        MAX_RETRIES = 3
        retry_count = 0
        task_completed = False
        
        while retry_count < MAX_RETRIES and not task_completed:
            
            # 1. Get LLM Response
            response_text = chat_with_llm(conversation_history)
            
            # 2. Parse JSON
            action, args = parse_json_from_response(response_text)
            
            if action:
                print(f"   [Action Detected] Tool: {action} | Args: {args}")
                
                # 3. Execute Tool
                result = execute_tool(action, args)
                print(f"   [Tool Output] {result}")
                
                # 4. Check for Success/Failure
                if "Error" in result or "fail" in result.lower():
                    # --- FAILURE: Trigger Self-Correction ---
                    print(f"   ❌ [Error Detected] Retrying... ({retry_count + 1}/{MAX_RETRIES})")
                    
                    # Feed the error back to the LLM
                    error_msg = f"Tool execution failed.\nCommand: {action}\nArguments: {args}\nError Message: {result}\n\nPlease analyze the error. If needed, use 'search_web' to find a solution, then output the CORRECT JSON command."
                    
                    conversation_history.append({'role': 'assistant', 'content': response_text})
                    conversation_history.append({'role': 'user', 'content': error_msg})
                    
                    retry_count += 1
                
                else:
                    # --- SUCCESS ---
                    print("   ✅ [Success] Task completed successfully.")
                    
                    # Memorize the success
                    log_entry = f"User Request: {user_input} -> Action: {action} ({args}) -> Result: Success."
                    memory.add_log(log_entry)
                    
                    # Final response to user
                    print(f"Agent: I have completed the task. Output: {result}")
                    task_completed = True
            
            else:
                # No tool used, just normal chat
                print(f"Agent: {response_text}")
                task_completed = True
        
        if retry_count >= MAX_RETRIES:
            print("   🚫 [Failed] Maximum retries reached. I could not complete the task.")

if __name__ == "__main__":
    main()