# Improvement Plan: Teaching Agent to Use Sudo

**Date**: 2025-11-11
**Issue**: Agent doesn't adapt when write_file() fails with Permission denied

## Problem Analysis

### Current Behavior
When agent encounters Permission denied:
1. Keeps retrying the same failed tool call
2. Doesn't recognize error means "needs root access"
3. Doesn't know to switch to shell() with sudo
4. Exhausts max_steps without completing task

### Root Causes
1. **Small model (3B)**: Limited reasoning ability
2. **No sudo examples in system prompt**: Agent doesn't know this pattern
3. **No Linux basics in RAG**: No context about permissions, sudo, etc.
4. **Tool doesn't auto-escalate**: write_file() could handle this automatically

## Proposed Solutions

### Solution 1: Add Linux/Ubuntu Documentation to RAG (Easy)

**What to add**:
```bash
# On VM
cd /home/agent/docs
mkdir -p linux-basics

# Create basic Linux docs
cat > linux-basics/permissions.md <<EOF
# Linux File Permissions

## Understanding Permission Errors

When you see "Permission denied" (Errno 13), it means:
- The user doesn't have rights to access/modify the file
- System files in /etc/, /var/, /usr/ require root access
- Use sudo to run commands as root

## Using sudo

sudo allows running commands as root:

\`\`\`bash
# Read protected file
sudo cat /etc/shadow

# Write to protected file
echo "content" | sudo tee /etc/nginx/nginx.conf

# Edit file with elevated privileges
sudo nano /etc/hosts
\`\`\`

## Common Patterns

### Writing to system files
\`\`\`bash
# Don't do this (will fail):
echo "config" > /etc/app/config.conf

# Do this instead:
echo "config" | sudo tee /etc/app/config.conf
\`\`\`

### Modifying system config
\`\`\`bash
# Using sed with sudo
sudo sed -i 's/old/new/g' /etc/nginx/nginx.conf

# Using bash with sudo
sudo bash -c 'echo "line" >> /etc/hosts'
\`\`\`
EOF

cat > linux-basics/sudo-basics.md <<EOF
# Sudo Command Basics

## What is sudo?

sudo (superuser do) executes commands with root privileges.

## Common sudo patterns for ops tasks

### 1. File operations
\`\`\`bash
# Read
sudo cat /etc/shadow

# Write entire file
echo "content" | sudo tee /etc/app/config

# Append to file
echo "line" | sudo tee -a /var/log/app.log

# Edit in-place with sed
sudo sed -i 's/port 80/port 90/g' /etc/nginx/sites-available/default
\`\`\`

### 2. Service management
\`\`\`bash
sudo systemctl start nginx
sudo systemctl stop nginx
sudo systemctl restart nginx
sudo systemctl status nginx
\`\`\`

### 3. Package management
\`\`\`bash
sudo apt-get update
sudo apt-get install -y package-name
sudo apt-get remove package-name
\`\`\`

## Troubleshooting

### "Permission denied" error
**Solution**: Add sudo before the command

### "Command not found" with sudo
**Solution**: Use full path or ensure command exists
EOF

# Re-index RAG
cd /home/agent/rag
python3 build_index.py
```

**Estimated improvement**: +10-15% success rate for system file tasks

**Limitations**: 3B model may still not reason correctly even with docs.

---

### Solution 2: Improve System Prompt with Sudo Examples (Medium)

**Current system prompt** lacks concrete sudo examples.

**Improved system prompt**:

```python
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

Think step by step internally, but output ONLY ONE JSON object for the next step.

IMPORTANT PATTERNS:

## Writing to system files (requires sudo)
Files in /etc/, /var/, /usr/ need root access. write_file() will fail.

If write_file() returns Permission denied:
{"tool": "shell", "args": {"cmd": "echo 'content' | sudo tee /etc/path/file"}}

Example:
Previous step failed: write_file("/etc/nginx/nginx.conf", "...")
→ Result: [Errno 13] Permission denied
Next step: Use shell with sudo tee:
{"tool": "shell", "args": {"cmd": "echo 'server { listen 90; }' | sudo tee /etc/nginx/nginx.conf"}}

## Modifying config files
Use sed with sudo:
{"tool": "shell", "args": {"cmd": "sudo sed -i 's/port 80/port 90/g' /etc/nginx/sites-available/default"}}

## Reading protected files
Use shell with sudo cat:
{"tool": "shell", "args": {"cmd": "sudo cat /etc/shadow"}}
"""
```

**Estimated improvement**: +20-30% success rate

**Key additions**:
- Explicit "If write_file() fails with Permission denied → use shell + sudo tee"
- Concrete before/after example
- Common sudo patterns

---

### Solution 3: Auto-Sudo in write_file() (Hard, Best Solution)

**Make write_file() smart enough to use sudo automatically.**

**Implementation**:

```python
def write_file(path, content):
    """Write content to file, automatically using sudo if needed"""
    logger.info(f"Writing to file: {path}")

    # First, try normal write (fast path for files we own)
    try:
        with open(path, "w") as f:
            f.write(content)
        logger.info(f"✓ Wrote to {path} (no sudo needed)")
        return {"status": "ok"}
    except PermissionError:
        # Permission denied - try with sudo
        logger.info(f"⚠️  Permission denied, retrying with sudo for {path}")
        try:
            # Use sudo tee to write file
            result = subprocess.run(
                ["sudo", "tee", path],
                input=content,
                text=True,
                capture_output=True,
                timeout=10
            )

            if result.returncode == 0:
                logger.info(f"✓ Wrote to {path} with sudo")
                return {"status": "ok", "used_sudo": True}
            else:
                logger.error(f"❌ Sudo write failed: {result.stderr}")
                return {
                    "error": f"Failed even with sudo: {result.stderr}",
                    "returncode": result.returncode
                }
        except Exception as e:
            logger.error(f"❌ Exception during sudo write: {e}")
            return {"error": f"Sudo write exception: {str(e)}"}
    except Exception as e:
        logger.error(f"❌ Failed to write {path}: {e}")
        return {"error": str(e)}
```

**Benefits**:
- Agent doesn't need to learn this pattern
- Works immediately, no reasoning required
- Backward compatible (normal files still work)
- Cleaner agent logs

**Estimated improvement**: +40-50% success rate for system file tasks

---

## Recommended Approach

**Phase 1 (Immediate - Easy)**:
1. ✅ Add Solution 2 (Improved system prompt) - 10 minutes
2. ✅ Test with nginx task - 5 minutes
3. ✅ Measure improvement

**Phase 2 (Short-term - Medium)**:
1. Add Linux basics docs to RAG
2. Re-index RAG database
3. Test again with improved context

**Phase 3 (Long-term - Best)**:
1. Implement auto-sudo in write_file()
2. Deploy to VM
3. Test extensively
4. Document new behavior

## Testing Plan

### Test Cases

**Test 1: Direct system file write**
```bash
curl -X POST http://localhost:9000/execute \
  -d '{"task": "Write text 'Hello' to /etc/motd"}'
```

**Expected with current code**: Fails (Permission denied loop)
**Expected with Solution 2**: ~30% chance of using sudo tee
**Expected with Solution 3**: 100% success

**Test 2: Nginx config modification**
```bash
curl -X POST http://localhost:9000/execute \
  -d '{"task": "Change nginx port from 80 to 8080 in /etc/nginx/sites-available/default"}'
```

**Expected with current code**: Fails or loops
**Expected with Solution 2**: ~40% chance of using sudo sed
**Expected with Solution 3**: Higher success (but depends on agent using correct sed syntax)

**Test 3: Multi-step with file write**
```bash
curl -X POST http://localhost:9000/execute \
  -d '{"task": "Stop nginx, change listen port to 8080, start nginx"}'
```

**Expected with current code**: Stops nginx, fails at config change
**Expected with Solution 2**: 30-40% chance of completing
**Expected with Solution 3**: 60-70% chance of completing

## Implementation Priority

| Solution | Effort | Impact | Priority |
|----------|--------|--------|----------|
| Solution 2: System prompt | 10 min | Medium (+20%) | 🔴 High |
| Solution 1: Linux docs RAG | 30 min | Low (+10%) | 🟡 Medium |
| Solution 3: Auto-sudo | 2 hours | High (+40%) | 🟢 Low (requires testing) |

## Success Metrics

**Before fixes**:
- System file write tasks: ~10% success
- Agent adapts to Permission denied: 0%

**Target after all solutions**:
- System file write tasks: ~70% success
- Agent adapts to Permission denied: 100% (via auto-sudo)

---

## Next Steps

1. Implement Solution 2 (improved prompt) first
2. Test and measure improvement
3. If still insufficient, implement Solution 3
4. Solution 1 is nice-to-have but not critical

**Decision**: Solution 3 (auto-sudo) is the "right" long-term fix, but Solution 2 is fastest to test if LLM can learn the pattern.

---

**Author**: Claude Code
**Status**: Proposed (not yet implemented)
**Last Updated**: 2025-11-11
