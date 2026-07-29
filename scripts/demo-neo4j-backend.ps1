# Demo end-to-end del backend Neo4j di Graphify (Windows / PowerShell 7).
# Prerequisiti: Docker Desktop avviato, uv installato.
# Uso:  pwsh scripts/demo-neo4j-backend.ps1
# Tutto avviene in una cartella temporanea; il container si chiama gfy-demo-neo4j.

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$demo = Join-Path $env:TEMP "graphify-neo4j-demo"

function Invoke-Graphify { uv run --project $repo python -m graphify @args }

# 1. Neo4j locale (porta 17687 per non collidere con un'istanza esistente)
docker rm -f gfy-demo-neo4j 2>$null | Out-Null
docker run -d --name gfy-demo-neo4j -p 17687:7687 -p 17474:7474 `
    -e NEO4J_AUTH=neo4j/demopassword neo4j:5 | Out-Null
Write-Host "Attendo l'avvio di Neo4j..." -ForegroundColor Cyan
while (-not (docker logs gfy-demo-neo4j 2>&1 | Select-String "Started\.")) { Start-Sleep 2 }

# 2. Progetto di prova con un po' di codice
if (Test-Path $demo) { Remove-Item -Recurse -Force $demo }
New-Item -ItemType Directory -Path "$demo/src" -Force | Out-Null
Set-Location $demo
git init -q -b main .
git config user.email demo@demo; git config user.name demo
@'
def alpha():
    return beta()

def beta():
    return 1
'@ | Set-Content src/core.py
@'
from src.core import alpha

def main():
    alpha()
'@ | Set-Content src/app.py
git add -A; git commit -qm init

# 3. Opt-in al backend: da qui Neo4j e' la fonte di verita'
$env:NEO4J_PASSWORD = "demopassword"
Invoke-Graphify backend set neo4j://localhost:17687
Invoke-Graphify backend show

# 4. Primo build (solo AST, nessuna API key): seed del branch 'main' nel DB
Invoke-Graphify update .

# 5. Query — via cache locale e direttamente dal DB
Write-Host "`n--- query (cache locale) ---" -ForegroundColor Cyan
Invoke-Graphify query "what calls alpha"
Write-Host "`n--- query (letta da Neo4j) ---" -ForegroundColor Cyan
Invoke-Graphify query "what calls alpha" --graph neo4j://localhost:17687

# 6. Modifica un file e aggiorna: viene pushato solo il DELTA, in una transazione
Add-Content src/core.py "`ndef gamma():`n    return alpha()`n"
Invoke-Graphify update .

# 7. Branch: ogni branch git ha il suo grafo nello stesso database
git add -A; git commit -qm change
git checkout -qb feature/demo
Invoke-Graphify update .
Write-Host "`n--- branches ---" -ForegroundColor Cyan
Invoke-Graphify branches
git checkout -q main
git branch -qD feature/demo
Invoke-Graphify branches --prune   # rimuove dal DB i branch che non esistono piu' in git

# 8. Server MCP che legge da Neo4j (refresh ogni 10s, GRAPHIFY_NEO4J_TTL)
Write-Host @"

Demo completata. Ora puoi:
 - aprire il Neo4j Browser: http://localhost:17474  (neo4j / demopassword)
   e provare:  MATCH (n:GraphifyNode {branch:'main'})-[r]->(m) RETURN n,r,m
 - avviare il server MCP che segue il DB:
     cd $demo
     `$env:NEO4J_PASSWORD='demopassword'
     uv run --project $repo python -m graphify.serve graphify-out/graph.json
 - modificare un nodo a mano nel Browser (e incrementare GraphifyMeta.version):
   entro ~10s le query MCP vedono la modifica.

Pulizia:  docker rm -f gfy-demo-neo4j ; Remove-Item -Recurse -Force $demo
"@ -ForegroundColor Green
