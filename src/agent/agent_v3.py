#!/usr/bin/env python3
"""
Ops Agent v3 - HTTP API Version
Accepts tasks via HTTP POST and executes them autonomously
"""
import json
import subprocess
import requests
import shlex
import sys
import logging
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import threading

# ============================================================================
# CONFIGURATION
# ============================================================================

# Logging
LOG_DIR = Path("/home/agent/logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent_api.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# TOOL FUNCTIONS (No whitelists - agent decides)
# ============================================================================

def run_cmd(cmd, timeout=60):
    """Execute shell command with timeout"""
    try:
        logger.info(f"Executing: {cmd}")
        p = subprocess.run(cmd, shell=True, capture_output=True, 
                         text=True, timeout=timeout)
        
        result = {
            "returncode": p.returncode,
            "stdout": p.stdout[-2000:],
            "stderr": p.stderr[-2000:]
        }
        
        logger.info(f"Return code: {p.returncode}")
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {cmd}")
        return {"error": "timeout", "returncode": -1}
    except Exception as e:
        logger.error(f"Exception: {e}")
        return {"error": str(e), "returncode": -1}

def pkg_install(name):
    """Install package via apt"""
    logger.info(f"Installing package: {name}")
    cmd = f"sudo apt-get update && sudo apt-get install -y {shlex.quote(name)}"
    return run_cmd(cmd, timeout=300)

def service(action, name):
    """Manage systemd service"""
    logger.info(f"Service {action}: {name}")
    cmd = f"sudo systemctl {action} {shlex.quote(name)}"
    return run_cmd(cmd)

def read_file(path):
    """Read file content"""
    logger.info(f"Reading file: {path}")
    try:
        with open(path, "r") as f:
            content = f.read(10000)
        return {"content": content}
    except Exception as e:
        logger.error(f"Failed to read {path}: {e}")
        return {"error": str(e)}

def write_file(path, content):
    """Write content to file"""
    logger.info(f"Writing to file: {path}")
    try:
        with open(path, "w") as f:
            f.write(content)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Failed to write {path}: {e}")
        return {"error": str(e)}

def shell(cmd):
    """Execute arbitrary shell command"""
    logger.info(f"Shell command: {cmd}")
    return run_cmd(cmd)

# ============================================================================
# LLM & RAG
# ============================================================================

def rag_search(query):
    """Search knowledge base"""
    logger.info(f"RAG search: {query}")
    try:
        r = requests.get(f"http://127.0.0.1:7000/search?q={query}", timeout=5)
        hits = r.json().get("hits", [])
        context = "\n".join([h["content"][:300] for h in hits[:3]])
        logger.info(f"RAG returned {len(hits)} hits")
        return context
    except Exception as e:
        logger.warning(f"RAG failed: {e}")
        return ""

SYS_PROMPT = """You are an autonomous Linux ops agent. You have these tools:

- pkg_install(name): Install package via apt-get
- service(action, name): Manage service (actions: status/start/stop/restart/enable/disable)
- read_file(path): Read file content
- write_file(path, content): Write content to file
- shell(cmd): Execute shell command

CRITICAL RULES:
1. Your response MUST be ONLY ONE SINGLE JSON object
2. Output ONLY the NEXT tool call - not multiple tool calls
3. No explanations, no markdown, no extra text
4. No multiple JSON objects in the same response

Format: {"tool": "<name>", "args": {...}}

Example valid response:
{"tool": "service", "args": {"action": "status", "name": "nginx"}}

Think step by step internally, but output ONLY ONE JSON object for the next step."""

def llm(msgs):
    """Call LLM API"""
    logger.info("Calling LLM...")
    try:
        r = requests.post(
            "http://127.0.0.1:8080/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "system", "content": SYS_PROMPT}] + msgs,
                "temperature": 0.2,
                "max_tokens": 300
            },
            timeout=180
        )
        content = r.json()["choices"][0]["message"]["content"]
        logger.info(f"LLM response: {content[:200]}")
        return content
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return json.dumps({"error": f"LLM error: {str(e)}"})

# ============================================================================
# AGENT LOOP
# ============================================================================

TOOLS = {
    "pkg_install": pkg_install,
    "service": service,
    "read_file": read_file,
    "write_file": write_file,
    "shell": shell
}

def agent_loop(task, max_steps=15):
    """Main agent loop"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"task_{timestamp}.log"
    
    task_logger = logging.FileHandler(log_file)
    task_logger.setLevel(logging.INFO)
    logger.addHandler(task_logger)
    
    logger.info("=" * 70)
    logger.info(f"NEW TASK: {task}")
    logger.info("=" * 70)
    
    # Get context from RAG
    context = rag_search(task)
    
    history = [
        {"role": "user", "content": f"Context:\n{context}\n\nTask: {task}"}
    ]
    
    results = []
    
    for step in range(1, max_steps + 1):
        logger.info(f"\n--- Step {step}/{max_steps} ---")
        
        # Get LLM decision
        llm_resp = llm(history)
        logger.info(f"LLM: {llm_resp}")
        
        # Parse response - extract JSON from markdown if needed
        try:
            # Try direct JSON parse first
            data = json.loads(llm_resp)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            import re
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', llm_resp, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    logger.info(f"✓ Extracted JSON from markdown: {json_match.group(1)[:100]}")
                except:
                    logger.error(f"❌ Failed to parse extracted JSON: {json_match.group(1)[:200]}")
                    results.append({"step": step, "error": "parse_error", "response": llm_resp})
                    continue
            else:
                # Try to find FIRST JSON object with "tool" key
                # Handle multiple JSON objects by extracting the first one
                json_match = re.search(r'(\{\s*"tool"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{[^}]*\}\s*\})', llm_resp)
                if json_match:
                    try:
                        data = json.loads(json_match.group(1))
                        logger.info(f"✓ Extracted first JSON object: {json_match.group(1)[:100]}")
                        if llm_resp != json_match.group(1):
                            logger.warning(f"⚠️  LLM returned multiple JSON objects, using first one only")
                    except:
                        # Fallback to more lenient regex
                        json_match = re.search(r'(\{[^{]*?"tool"[^}]*?\})', llm_resp, re.DOTALL)
                        if json_match:
                            try:
                                data = json.loads(json_match.group(1))
                                logger.info(f"✓ Extracted JSON from text (fallback): {json_match.group(1)[:100]}")
                            except:
                                logger.error(f"❌ Failed to parse LLM response as JSON: {llm_resp[:300]}")
                                results.append({"step": step, "error": "parse_error", "response": llm_resp})
                                continue
                        else:
                            logger.error(f"❌ No JSON found in LLM response: {llm_resp[:300]}")
                            results.append({"step": step, "error": "parse_error", "response": llm_resp})
                            continue
                else:
                    logger.error(f"❌ No valid JSON object found in LLM response: {llm_resp[:300]}")
                    results.append({"step": step, "error": "parse_error", "response": llm_resp})
                    continue
        
        if "error" in data:
            logger.error(f"LLM error: {data['error']}")
            results.append({"step": step, "error": data["error"]})
            break
        
        # Execute tool
        tool_name = data.get("tool")
        tool_args = data.get("args", {})
        
        if tool_name not in TOOLS:
            logger.error(f"Unknown tool: {tool_name}")
            results.append({"step": step, "error": f"unknown_tool: {tool_name}"})
            continue
        
        tool_fn = TOOLS[tool_name]
        
        # Filter args to only valid parameters
        import inspect
        sig = inspect.signature(tool_fn)
        valid_params = set(sig.parameters.keys())
        filtered_args = {k: v for k, v in tool_args.items() if k in valid_params}
        
        logger.info(f"Calling {tool_name}({filtered_args})")
        result = tool_fn(**filtered_args)
        
        logger.info(f"Result: {json.dumps(result, indent=2)}")
        results.append({
            "step": step,
            "tool": tool_name,
            "args": filtered_args,
            "result": result
        })
        
        # Update history
        history.append({
            "role": "assistant",
            "content": llm_resp
        })
        history.append({
            "role": "user",
            "content": f"Result: {json.dumps(result)}\n\nContinue or report completion."
        })
    
    logger.removeHandler(task_logger)
    task_logger.close()
    
    logger.info("=" * 70)
    logger.info(f"Task completed: {len(results)} steps")
    logger.info(f"Log: {log_file}")
    logger.info("=" * 70)
    
    return {
        "status": "completed",
        "steps": len(results),
        "results": results,
        "log_file": str(log_file)
    }

# ============================================================================
# HTTP API
# ============================================================================

app = FastAPI(title="Ops Agent API", version="3.0")

class TaskRequest(BaseModel):
    task: str
    max_steps: int = 15

class TaskResponse(BaseModel):
    status: str
    message: str
    task_id: str = None

@app.get("/")
def root():
    """Health check"""
    return {
        "service": "Ops Agent API v3",
        "status": "running",
        "endpoints": {
            "POST /execute": "Execute a task",
            "GET /logs": "List recent logs",
            "GET /health": "Health check"
        }
    }

@app.get("/health")
def health():
    """Detailed health check"""
    return {
        "agent": "ok",
        "llm": "ok" if check_llm() else "error",
        "rag": "ok" if check_rag() else "error"
    }

def check_llm():
    try:
        requests.get("http://127.0.0.1:8080/health", timeout=2)
        return True
    except:
        return False

def check_rag():
    try:
        requests.get("http://127.0.0.1:7000/", timeout=2)
        return True
    except:
        return False

@app.post("/execute")
def execute_task(req: TaskRequest):
    """Execute a task asynchronously"""
    task = req.task
    logger.info(f"Received task via API: {task}")
    
    # Run agent in background thread
    def run_agent():
        try:
            agent_loop(task, max_steps=req.max_steps)
        except Exception as e:
            logger.error(f"Agent failed: {e}")
    
    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()
    
    return {
        "status": "accepted",
        "message": "Task execution started",
        "task": task
    }

@app.get("/logs")
def list_logs():
    """List recent task logs"""
    logs = sorted(LOG_DIR.glob("task_*.log"), reverse=True)[:10]
    return {
        "logs": [
            {
                "file": log.name,
                "size": log.stat().st_size,
                "modified": log.stat().st_mtime
            }
            for log in logs
        ]
    }

@app.get("/logs/{filename}")
def get_log(filename: str):
    """Get specific log file"""
    log_path = LOG_DIR / filename
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    
    with open(log_path, "r") as f:
        content = f.read()
    
    return {"filename": filename, "content": content}

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    logger.info("Starting Ops Agent API v3...")
    logger.info(f"Logs directory: {LOG_DIR}")
    
    # Run FastAPI with uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=9000,
        log_level="info"
    )
