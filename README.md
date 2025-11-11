# Autonomous Agent Server - Demo & Documentation

En komplett demo og dokumentasjon av en autonom AI-agent som kjører på en lokal server med full sudo-tilgang.

## 📋 Oversikt

Dette prosjektet dokumenterer utviklingen og debuggingen av en autonom Linux-operasjonsagent som:
- Kjører i en Multipass VM (Ubuntu 22.04)
- Bruker Qwen2.5-3B LLM via llama.cpp
- Har RAG (Retrieval-Augmented Generation) med nginx-dokumentasjon
- Eksponerer HTTP API for å motta oppgaver
- Har full sudo-tilgang for systemoperasjoner

## 🏗️ Arkitektur

```
┌─────────────────────────────────────────────────────┐
│                 macOS Host                          │
│                                                     │
│  Port Forwarding:                                   │
│  • localhost:8080 → VM:80   (nginx)                │
│  • localhost:8081 → VM:8080 (LLM API)              │
│  • localhost:9000 → VM:9000 (Agent API)            │
└─────────────────┬───────────────────────────────────┘
                  │
                  │ SSH tunnel via sshpass
                  ↓
┌─────────────────────────────────────────────────────┐
│         Multipass VM (192.168.64.5)                 │
│                                                     │
│  ┌──────────────────────────────────────────────┐  │
│  │  LLM Service (port 8080)                     │  │
│  │  • llama.cpp server                          │  │
│  │  • Qwen2.5-3B-Instruct-Q4_K_M                │  │
│  │  • ctx_size: 2048, parallel: 2               │  │
│  └──────────────────────────────────────────────┘  │
│                      ↑                              │
│  ┌──────────────────────────────────────────────┐  │
│  │  RAG Service (port 7000)                     │  │
│  │  • SQLite FTS5 full-text search              │  │
│  │  • 1965 nginx documentation chunks           │  │
│  └──────────────────────────────────────────────┘  │
│                      ↑                              │
│  ┌──────────────────────────────────────────────┐  │
│  │  Agent API (port 9000)                       │  │
│  │  • FastAPI HTTP server                       │  │
│  │  • Tool-calling agent loop                   │  │
│  │  • Full sudo access (NOPASSWD: ALL)          │  │
│  │                                               │  │
│  │  Tools:                                       │  │
│  │  • pkg_install(name)                         │  │
│  │  • service(action, name)                     │  │
│  │  • read_file(path)                           │  │
│  │  • write_file(path, content)                 │  │
│  │  • shell(cmd)                                │  │
│  └──────────────────────────────────────────────┘  │
│                                                     │
│  Nginx (port 80) - Test target for agent           │
└─────────────────────────────────────────────────────┘
```

## 🔧 Teknisk Stack

- **Virtualisering**: Multipass (Canonical)
- **OS**: Ubuntu 22.04 LTS (ARM64)
- **LLM**: Qwen2.5-3B-Instruct-Q4_K_M via llama.cpp
- **Agent Framework**: Python FastAPI + custom tool-calling loop
- **RAG**: SQLite FTS5 full-text search
- **Service Management**: systemd

## 📚 Dokumentasjon

### Hovedfiler
- [SESSION_2025-11-11.md](docs/SESSION_2025-11-11.md) - Komplett logg av debugging-sesjonen
- [TECHNICAL_FIXES.md](docs/TECHNICAL_FIXES.md) - Detaljert teknisk dokumentasjon av fiksene
- [KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) - Kjente problemer og begrensninger

### Kildekode
- [src/agent/agent_v3.py](src/agent/agent_v3.py) - Agent med forbedret JSON parsing
- [src/systemd/llm.service](src/systemd/llm.service) - Systemd service for LLM
- [src/systemd/agent-api.service](src/systemd/agent-api.service) - Systemd service for agent (med sudo-fix)

## 🐛 Hva vi fikset (11. november 2025)

### Problem 1: LLM returnerte multiple JSON-objekter
**Symptom**: Agenten feilet å parse LLM-respons når modellen returnerte flere tool calls samtidig.

**Løsning**:
- Forbedret system prompt med "CRITICAL RULES" som eksplisitt forbyr multiple objekter
- Implementert 3-tier fallback JSON parser med regex
- Parser ekstraherer nå første gyldige objekt og logger advarsel

**Resultat**: ✅ Ingen flere parse-feil

### Problem 2: Sudo blokkert av NoNewPrivileges
**Symptom**: Agent kunne ikke kjøre sudo-kommandoer til tross for `NOPASSWD: ALL` i sudoers.

**Rotårsak**: systemd service hadde `NoNewPrivileges=yes` som blokkerer privilege escalation.

**Løsning**:
```ini
# /etc/systemd/system/agent-api.service
NoNewPrivileges=false      # Endret fra true
ProtectSystem=false        # Endret fra strict
ProtectHome=false          # Endret fra read-only
```

**Resultat**: ✅ Sudo fungerer perfekt

### Bonus: Nginx dokumentasjon til RAG
- Lastet ned komplett nginx admin-guide (81 markdown-filer)
- Indeksert 1965 chunks i SQLite FTS5
- RAG-service kan nå svare på nginx-spørsmål

## 🚀 Hvordan bruke systemet

### 1. Start port forwarding
```bash
~/agent-tunnel.sh start
~/llm-tunnel.sh start
~/nginx-tunnel.sh start
```

### 2. Send en oppgave til agenten
```bash
curl -X POST http://localhost:9000/execute \
  -H "Content-Type: application/json" \
  -d '{"task": "Install PostgreSQL and ensure it is running"}'
```

### 3. Overvåk status
```bash
# Real-time status
curl -s http://localhost:9000/status | jq

# Follow logs
ssh root@192.168.64.5 "journalctl -u agent-api.service -f"
```

### 4. Hent resultater
```bash
# List logs
curl -s http://localhost:9000/logs | jq

# Get specific log
curl -s http://localhost:9000/logs/task_20251111_112037.log | jq -r '.content'
```

## ⚠️ Kjente begrensninger

### 1. write_file() mangler sudo-støtte
`write_file()` er en Python-funksjon som ikke kan skrive til systemfiler som `/etc/nginx/nginx.conf`. Agenten må bruke `shell()` med `sudo tee` eller `sudo sed` i stedet.

### 2. RAG søk er for strikt
FTS5 full-text search krever at ALLE ord matcher (AND-logikk). Lange oppgavebeskrivelser som "Stop Nginx, change the port from 80 to 90" får 0 treff fordi "Stop" og "change" ikke finnes i dokumentasjonen.

**Workaround**: Bruk kortere, mer spesifikke søkeord.

### 3. 3B-modellen er liten
Qwen2.5-3B sliter med:
- Komplekse multi-step oppgaver
- Å tilpasse seg når et verktøy feiler
- Å resonnere rundt alternativer (f.eks. bruke `shell` når `write_file` feiler)

**Anbefaling**: Oppgrader til Qwen2.5-7B eller større for bedre ytelse.

## 📊 Resultater

### Suksessmetrikker
- ✅ **JSON parsing**: 100% success rate etter fix
- ✅ **Sudo execution**: Fungerer perfekt (returnkode 0)
- ✅ **RAG indexing**: 1965 chunks indeksert
- ✅ **System uptime**: Alle services kjører stabilt

### Typiske oppgaver agenten kan løse
- ✅ Stoppe/starte systemd services
- ✅ Installere pakker via apt-get
- ✅ Lese systemfiler
- ⚠️ Skrive til systemfiler (må bruke shell + sudo tee)
- ⚠️ Komplekse konfigurasjonsendringer (modellen er liten)

## 🔮 Neste steg

1. **Oppgrader til større modell**: Qwen2.5-7B eller Llama3-8B for bedre reasoning
2. **Forbedre RAG søk**: Implementer OR-logikk eller keyword-ekstraksjon
3. **Legg til sudo-støtte i write_file()**: Wrapper som automatisk bruker sudo for systemfiler
4. **Real-time monitoring**: WebSocket eller SSE for live progress updates
5. **Multi-agent koordinering**: La flere agenter samarbeide om komplekse oppgaver

## 📖 Lær mer

Se [SESSION_2025-11-11.md](docs/SESSION_2025-11-11.md) for en komplett, kronologisk gjennomgang av hele debugging-prosessen.

## 🙏 Credits

- **LLM**: Qwen2.5 av Alibaba Cloud
- **Runtime**: llama.cpp av Georgi Gerganov
- **VM**: Multipass av Canonical
- **OS**: Ubuntu 22.04 LTS

---

**Status**: 🟢 Fungerende (med kjente begrensninger)
**Sist oppdatert**: 11. november 2025
**Versjon**: v3.0
