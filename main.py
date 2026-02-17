import ollama
import json
from memory import MemoryBank
from tools import execute_tool, AVAILABLE_TOOLS

# Initialize
memory = MemoryBank()
MODEL_NAME = "gpt-oss:20b"  # Make sure you have this model in ollama

def chat_with_llm(user_input, past_memories):
    """
    Build the prompt and call Ollama.
    """
    
    # Dynamically generate tool list description
    tools_desc = "\n".join([f"- {k}" for k in AVAILABLE_TOOLS.keys()])
    
    # --- Core Prompt (most important part) ---
    system_prompt = f"""
    You are a super AI that can self-program.

[Your superpower]:
    If you find that a tool is missing (e.g. check weather, check stock price), you can:
1. Use 'write_file' to write a Python script (must include def main(args): and use print for output).
2. Use 'load_tool' to load that script.
3. Then call the new tool by name directly!

    You are an AI Agent with self-learning capabilities.

    [Available tools]:
    {tools_desc}

    [Your memory/experience] (use these to decide how to act):
    {past_memories}

    [Instructions]:
    1. If the user's question is simple, answer directly.
    2. If you need to use a tool, you **must** output only a single JSON object in this format:
       {{ "action": "tool_name", "args": "argument_string" }}
    3. Do not wrap in markdown code blocks (e.g. ```json). Output raw JSON only.
    """

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_input}
    ]

    print("   [Thinking] ...")
    response = ollama.chat(model=MODEL_NAME, messages=messages)
    return response['message']['content']

def main():
    print(f"🤖 Agent started ({MODEL_NAME}). Type 'exit' to quit.")
    
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() == "exit":
            break

        # --- Step 1: Recall ---
        # Before answering, search memory: have I seen a similar question before? How did I solve it?
        relevant_memory = memory.recall(user_input)
        context_str = "\n".join(relevant_memory) if relevant_memory else "No relevant memory yet."
        
        # --- Step 2: Think ---
        response = chat_with_llm(user_input, context_str)
        
        # --- Step 3: Parse & Act ---
        # Try to parse whether the model output JSON
        try:
            # Trim the string to avoid leading/trailing whitespace from the model
            cleaned_response = response.strip()
            
            # Simple heuristic: does it look like JSON?
            if "{" in cleaned_response and "}" in cleaned_response:
                # Extract the JSON portion
                start = cleaned_response.find("{")
                end = cleaned_response.rfind("}") + 1
                json_str = cleaned_response[start:end]
                
                command = json.loads(json_str)
                action = command.get("action")
                args = command.get("args")
                
                print(f"   [Action detected] Tool: {action} | Args: {args}")
                
                # Execute tool
                result = execute_tool(action, args)
                print(f"   [Tool output] {result}")
                
                # --- Step 4: Learn ---
                # If the tool succeeded (no error in result), store this experience.
                # Step 1 will recall it when similar questions come up.
                if "error" not in result.lower() and "fail" not in result.lower():
                    learning_log = f"User asked: '{user_input}' -> I used tool '{action}' with args '{args}' -> success."
                    memory.add_log(learning_log)
                
                print(f"Agent: Task completed. Result: {result}")
                
            else:
                # Normal conversation reply
                print(f"Agent: {response}")

        except json.JSONDecodeError:
            # Model tried to output JSON but format was wrong, or it's just text with braces
            print(f"Agent: {response}")
        except Exception as e:
            print(f"   [System error] {e}")

if __name__ == "__main__":
    main()