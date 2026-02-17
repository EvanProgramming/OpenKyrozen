import os
import subprocess
from duckduckgo_search import DDGS
import ast

# --- Safety Check Function ---
def is_safe_command(command_str):
    """
    Simple keyword-based safety check. 
    You can expand this with AST analysis for Python code.
    """
    # Blacklist of dangerous commands/keywords
    blacklist = [
        "rm -rf", "format", "mkfs", ":(){:|:&};:",  # System destruction
        "os.system", "shutil.rmtree"                # Python risky ops
    ]
    
    for word in blacklist:
        if word in command_str:
            print(f"   [SAFETY ALERT] Blocked dangerous command: {word}")
            return False
    return True

# --- Tool Definitions ---

def write_file(args):
    """
    Args format: "filename|content"
    """
    try:
        if "|" not in args:
            return "Error: Arguments must be 'filename|content'"
        
        filename, content = args.split("|", 1)
        
        # Safety: Don't overwrite critical files (basic check)
        if filename.endswith(".py") or filename == "main.py":
            user_input = input(f"\n⚠️ [SAFETY] Agent wants to modify/create '{filename}'. Allow? (y/n): ")
            if user_input.lower() != 'y':
                return "Error: User denied permission to write this file."

        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Success: File '{filename}' written."
    except Exception as e:
        return f"Error writing file: {str(e)}"

def read_file(args):
    """
    Args format: "filename"
    """
    try:
        if os.path.exists(args):
            with open(args, "r", encoding="utf-8") as f:
                return f.read()
        return "Error: File not found."
    except Exception as e:
        return f"Error reading file: {str(e)}"

def run_cmd(args):
    """
    Args format: "command" (e.g., 'dir', 'python --version')
    """
    if not is_safe_command(args):
        return "Error: Command blocked by safety filter."
    
    # Double check with user for ANY shell command
    print(f"\n⚠️ [SAFETY] Agent wants to run shell command: {args}")
    # Uncomment the next two lines to force manual approval for every command
    # if input("Allow? (y/n): ").lower() != 'y':
    #     return "Error: User denied permission."

    try:
        # shell=True is risky, but necessary for complex commands. Sandbox is crucial.
        result = subprocess.run(args, shell=True, capture_output=True, text=True)
        output = result.stdout if result.stdout else result.stderr
        return output.strip()
    except Exception as e:
        return f"Error executing command: {str(e)}"

def search_web(args):
    """
    Args format: "search_query"
    Uses DuckDuckGo to find info/docs for error fixing.
    """
    try:
        print(f"   [Tools] Searching web for: {args}")
        with DDGS() as ddgs:
            # Get top 3 results
            results = [r for r in ddgs.text(args, max_results=3)]
            if not results:
                return "No search results found."
            
            summary = "\n".join([f"- Title: {r['title']}\n  Snippet: {r['body']}" for r in results])
            return summary
    except Exception as e:
        return f"Search Error: {str(e)}"

# --- Registry ---
AVAILABLE_TOOLS = {
    "write_file": write_file,
    "read_file": read_file,
    "run_cmd": run_cmd,
    "search_web": search_web
}

def execute_tool(tool_name, tool_args):
    if tool_name in AVAILABLE_TOOLS:
        return AVAILABLE_TOOLS[tool_name](tool_args)
    return f"Error: Tool '{tool_name}' not found."