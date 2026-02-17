# Examples

## Example 1: Creating a file

**User:** Create a file on my desktop called `notes.txt` with the content "Hello from the agent."

**Assistant:**
The user wants a file on the desktop. I will use the write_file tool.
```json
{"action": "write_file", "args": "~/Desktop/notes.txt|Hello from the agent."}
```

---

## Example 2: Searching the web

**User:** What is the current Bitcoin price?

**Assistant:**
The user wants real-time price data. I need to search the web.
```json
{"action": "search_web", "args": "current bitcoin price USD"}
```

---

## Example 3: Reading a file

**User:** Read the file ~/Desktop/notes.txt

**Assistant:**
The user wants to see the contents of a file. I will use read_file.
```json
{"action": "read_file", "args": "~/Desktop/notes.txt"}
```
