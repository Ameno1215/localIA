# Installe et configure OpenCode sur un poste Windows (équivalent de installer-opencode.sh).
#
#   powershell -ExecutionPolicy Bypass -File scripts\installer-opencode.ps1 `
#       -Serveur https://spark1.lan -Contexte 262144 -JetonMcp <jeton d'équipe> -Ca E:\spark-ca.crt
#
# Relançable sans risque : il met à jour la config. La clé LiteLLM est demandée si elle n'est pas déjà enregistrée.
# ⚠ Non testé sur un vrai poste Windows.
param(
  [string]$Serveur = "https://spark1.lan",
  [int]$Contexte = 262144,
  [string]$Mcp = "",
  [string]$JetonMcp = "",
  [string]$Ca = ""
)
$ErrorActionPreference = "Stop"
if ($Ca) { $Ca = (Resolve-Path $Ca).Path }   # avant le changement de dossier
Set-Location (Split-Path $PSScriptRoot -Parent)

$VersionOpencode = "1.18.33"
$Serveur = $Serveur.TrimEnd("/")
# Sur le Spark, la recherche dans les normes passe par Caddy, sur le même nom que LiteLLM
if (-not $Mcp) { $Mcp = "$Serveur/mcp" }
$Dossier = Join-Path $HOME ".config\ia-locale"
New-Item -ItemType Directory -Force $Dossier | Out-Null

function Enregistrer($Nom, $Valeur) {
  [Environment]::SetEnvironmentVariable($Nom, $Valeur, "User")
  Set-Item "env:$Nom" $Valeur
}

Write-Host "==> OpenCode"
# Déjà installé (par winget ou npm) : on le garde
if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
  if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or [int](node.exe -p "process.versions.node.split('.')[0]") -lt 20) {
    Write-Host "Installe OpenCode (winget) ou Node.js >= 20 (winget install OpenJS.NodeJS.LTS), rouvre PowerShell, puis relance ce script."
    exit 1
  }
  npm.cmd install -g "opencode-ai@$VersionOpencode"
}
if (-not (Get-Command rg.exe -ErrorAction SilentlyContinue)) {
  Write-Host "   Conseil : installe ripgrep (winget install BurntSushi.ripgrep.MSVC), sinon OpenCode le télécharge."
}

Write-Host "==> Certificat HTTPS interne du Spark"
# Garder le certificat déjà enregistré si on ne le repasse pas
$CaPoste = Join-Path $Dossier "spark-ca.crt"
if ($Ca) { Copy-Item $Ca $CaPoste -Force }
if (Test-Path $CaPoste) {
  # Magasin Windows (navigateur, curl.exe) + variable lue par OpenCode
  certutil.exe -addstore -user Root $CaPoste | Out-Null
  Enregistrer "NODE_EXTRA_CA_CERTS" $CaPoste
  Write-Host "   $CaPoste"
} else {
  Write-Host "   Aucun certificat (-Ca) : normal seulement si le serveur est en http."
}

Write-Host "==> Clé LiteLLM et variables d'environnement"
$Cle = [Environment]::GetEnvironmentVariable("LITELLM_API_KEY", "User")
if (-not $Cle) {
  $Saisie = Read-Host "Colle ta clé LiteLLM (sk-..., créée avec ./scripts/creer-cle-utilisateur.sh)" -AsSecureString
  $Cle = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($Saisie))
}
$Cle = $Cle.Trim()
if (-not $Cle.StartsWith("sk-")) { Write-Host "La clé doit commencer par sk-"; exit 1 }
Enregistrer "LITELLM_API_KEY" $Cle
# Garder le jeton MCP déjà enregistré si on ne le repasse pas
if (-not $JetonMcp) { $JetonMcp = [Environment]::GetEnvironmentVariable("MCP_TOKEN", "User") }
if ($JetonMcp) { Enregistrer "MCP_TOKEN" $JetonMcp }
# Pas d'appels réseau superflus (préparation du réseau fermé)
foreach ($Nom in "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_DEFAULT_PLUGINS", "OPENCODE_DISABLE_LSP_DOWNLOAD") {
  Enregistrer $Nom "1"
}
Write-Host "   Variables enregistrées pour ton compte Windows (LITELLM_API_KEY, MCP_TOKEN, NODE_EXTRA_CA_CERTS, OPENCODE_*)"

Write-Host "==> Vérification du serveur ($Serveur)"
# --ssl-no-revoke : le certificat interne de Caddy ne publie pas de liste de révocation
$Reponse = (curl.exe -s -m 10 --ssl-no-revoke "$Serveur/v1/models" -H "Authorization: Bearer $Cle") -join ""
if ($Reponse -match "qwen-local") {
  Write-Host "   OK : la clé fonctionne et qwen-local est disponible."
} else {
  Write-Host "   ATTENTION : $Serveur ne répond pas, ou la clé est refusée. La config est écrite quand même."
}

Write-Host "==> Configuration OpenCode"
$Config = Join-Path $HOME ".config\opencode\opencode.json"
New-Item -ItemType Directory -Force (Split-Path $Config) | Out-Null
$Texte = (Get-Content -Raw -Encoding UTF8 "clients\opencode.json").Replace("__SERVEUR__", $Serveur).Replace("__CONTEXTE__", "$Contexte").Replace("__MCP__", $Mcp)
# UTF-8 sans BOM : OpenCode lit du JSON strict
[IO.File]::WriteAllText($Config, $Texte, (New-Object Text.UTF8Encoding $false))
Write-Host "   $Config (serveur $Serveur, contexte $Contexte, MCP $Mcp)"

Write-Host ""
Write-Host "Terminé. Ouvre un NOUVEAU terminal (les variables ne sont lues qu'au démarrage), puis :"
Write-Host "   cd ton-projet"
Write-Host "   copy `"$PWD\clients\AGENTS.md`" .     # consignes de l'agent, à adapter"
Write-Host "   opencode"
Write-Host "Si PowerShell refuse de lancer opencode (scripts désactivés) : Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
