#!/bin/bash
# Bazarr+ Installer
# Usage: curl -fsSL https://lavx.github.io/bazarr/install.sh | bash

main() {
set -uo pipefail

# --- Colors (respect NO_COLOR / non-tty) ---
if [[ -z "${NO_COLOR:-}" ]] && [[ -t 2 ]]; then
  RST='\033[0m'; BLD='\033[1m'; RED='\033[31m'; GRN='\033[32m'
  YLW='\033[33m'; BLU='\033[34m'; CYN='\033[36m'; DIM='\033[2m'
else
  RST=''; BLD=''; RED=''; GRN=''; YLW=''; BLU=''; CYN=''; DIM=''
fi

# --- Cleanup ---
SPINNER_PID=""
cleanup() { [[ -n "$SPINNER_PID" ]] && kill "$SPINNER_PID" 2>/dev/null; tput cnorm 2>/dev/null; }
trap cleanup EXIT
trap 'printf "\n"; error "Interrupted"; exit 130' INT

# --- Logging ---
info()    { printf "${BLU}::${RST} %s\n" "$*" >&2; }
success() { printf "${GRN}ok${RST} %s\n" "$*" >&2; }
warn()    { printf "${YLW}!!${RST} %s\n" "$*" >&2; }
error()   { printf "${RED}!!${RST} %s\n" "$*" >&2; }
fatal()   { error "$@"; exit 1; }

# --- Input helpers ---
read_input() {
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    printf "${BLD}%s${RST} [${DIM}%s${RST}]: " "$prompt" "$default" >&2
  else
    printf "${BLD}%s${RST}: " "$prompt" >&2
  fi
  read -r reply </dev/tty
  printf '%s' "${reply:-$default}"
}

confirm() {
  local prompt="$1" default="${2:-n}" reply yn
  [[ "$default" == "y" ]] && yn="Y/n" || yn="y/N"
  printf "${BLD}%s${RST} [%s]: " "$prompt" "$yn" >&2
  read -r reply </dev/tty
  reply="${reply:-$default}"
  [[ "${reply,,}" == "y" ]]
}

# --- Spinner ---
spinner_start() {
  local msg="$1"
  tput civis 2>/dev/null
  (
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏') i=0
    while true; do
      printf "\r${CYN}%s${RST} %s" "${frames[$((i % 10))]}" "$msg" >&2
      ((i++)); sleep 0.1
    done
  ) &
  SPINNER_PID=$!
}

spinner_stop() {
  local status="$1" msg="$2"
  [[ -n "$SPINNER_PID" ]] && kill "$SPINNER_PID" 2>/dev/null; SPINNER_PID=""
  printf "\r\033[K" >&2; tput cnorm 2>/dev/null
  if [[ "$status" == "ok" ]]; then success "$msg"; else error "$msg"; fi
}

run_with_spinner() {
  local msg="$1"; shift
  spinner_start "$msg"
  local out; out=$("$@" 2>&1)
  local rc=$?
  if [[ $rc -eq 0 ]]; then spinner_stop ok "$msg"
  else spinner_stop fail "$msg"; printf '%s\n' "$out" >&2; fi
  return $rc
}

# --- Port validation ---
validate_port() {
  local val="$1"
  [[ "$val" =~ ^[0-9]+$ ]] || fatal "Invalid port: $val"
  (( val >= 1 && val <= 65535 )) || fatal "Port out of range: $val"
}

# --- Port check ---
check_port() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tlnH "sport = :$port" 2>/dev/null | grep -q .
  else
    grep -q "$(printf '%04X' "$port")" /proc/net/tcp 2>/dev/null
  fi
}

find_free_port() {
  local port="$1"
  while check_port "$port"; do ((port++)); done
  printf '%d' "$port"
}

# --- Timezone detection ---
detect_timezone() {
  local tz
  tz=$(timedatectl show -p Timezone --value 2>/dev/null) && [[ -n "$tz" ]] && { printf '%s' "$tz"; return; }
  [[ -f /etc/timezone ]] && tz=$(cat /etc/timezone) && [[ -n "$tz" ]] && { printf '%s' "$tz"; return; }
  [[ -L /etc/localtime ]] && tz=$(readlink /etc/localtime) && tz="${tz#*zoneinfo/}" && [[ -n "$tz" ]] && { printf '%s' "$tz"; return; }
  printf 'Etc/UTC'
}

# --- UID/GID detection ---
detect_puid_pgid() {
  local user="${SUDO_USER:-}"
  if [[ -n "$user" ]]; then
    PUID=$(id -u "$user"); PGID=$(id -g "$user")
  else
    PUID=$(id -u); PGID=$(id -g)
  fi
  if [[ "$PUID" -eq 0 ]]; then
    fatal "Refusing to run containers as root (UID 0). Run this script as a normal user, or via sudo."
  fi
}

# --- Path validation ---
validate_directory() {
  local dir="$1"
  dir="${dir/#\~/$HOME}"
  # Reject system directories
  case "$dir" in
    /|/bin*|/sbin*|/usr*|/etc*|/dev*|/proc*|/sys*|/boot*|/var*|/lib*|/lib64*|/tmp*|/root*)
      fatal "Cannot use system directory: $dir" ;;
  esac
  if [[ -e "$dir" ]] && [[ ! -d "$dir" ]]; then fatal "$dir exists but is not a directory"; fi
  mkdir -p "$dir" 2>/dev/null || fatal "Cannot create directory: $dir"
  [[ -w "$dir" ]] || fatal "Directory not writable: $dir"
  printf '%s' "$(cd "$dir" && pwd)"
}

validate_media_path() {
  local dir="$1"
  [[ -z "$dir" ]] && return 1
  dir="${dir/#\~/$HOME}"
  [[ -d "$dir" ]] || { warn "Path does not exist yet: $dir (will be created by Docker)"; }
  printf '%s' "$dir"
}

# --- Parse .env safely (whitelist keys, no eval) ---
parse_env_file() {
  local file="$1"
  declare -gA ENV_VALS
  while IFS= read -r line; do
    line="${line%%#*}"; line="${line// /}"
    [[ -z "$line" ]] && continue
    local key="${line%%=*}" val="${line#*=}"
    case "$key" in PUID|PGID|TZ|OPENROUTER_API_KEY|OPENSUBTITLES_SCRAPER_URL)
      ENV_VALS["$key"]="$val" ;; esac
  done < "$file"
}

# --- Detect distro ---
detect_distro() {
  [[ -f /etc/os-release ]] || fatal "Cannot detect distribution (missing /etc/os-release)"
  local id id_like
  id=$(. /etc/os-release && echo "${ID:-}")
  id_like=$(. /etc/os-release && echo "${ID_LIKE:-}")
  case "$id" in
    ubuntu|debian|pop|linuxmint|raspbian) DISTRO_FAMILY="apt" ;;
    amzn)                                 DISTRO_FAMILY="amzn" ;;
    fedora|rhel|centos|rocky|alma|ol)     DISTRO_FAMILY="dnf" ;;
    *)
      case "$id_like" in
        *debian*|*ubuntu*) DISTRO_FAMILY="apt" ;;
        *rhel*|*fedora*)   DISTRO_FAMILY="dnf" ;;
        *) fatal "Unsupported distribution: $id. Install Docker manually, then re-run this script." ;;
      esac ;;
  esac
}

# --- Docker installation ---
install_docker() {
  info "Installing Docker via official repository..."
  if [[ "$DISTRO_FAMILY" == "apt" ]]; then
    run_with_spinner "Updating package index" sudo apt-get update -qq || return 1
    run_with_spinner "Installing prerequisites" sudo apt-get install -y -qq ca-certificates curl gnupg || return 1
    sudo install -m 0755 -d /etc/apt/keyrings
    local distro_id; distro_id=$(. /etc/os-release && echo "$ID")
    # Map derivative distros to their Docker repo parent
    case "$distro_id" in
      pop|linuxmint|elementary|zorin|neon) distro_id="ubuntu" ;;
      raspbian) distro_id="debian" ;;
    esac
    if ! curl -fsSL "https://download.docker.com/linux/${distro_id}/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg; then
      fatal "Failed to import Docker GPG key for ${distro_id}"
    fi
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${distro_id} $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    run_with_spinner "Updating package index" sudo apt-get update -qq || return 1
    run_with_spinner "Installing Docker" sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin || return 1
  elif [[ "$DISTRO_FAMILY" == "amzn" ]]; then
    # Amazon Linux 2023: use Amazon's own docker package, then install compose plugin manually
    run_with_spinner "Installing Docker" sudo dnf install -y docker || return 1
    info "Installing Docker Compose plugin..."
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    local arch; arch=$(uname -m)
    [[ "$arch" == "aarch64" ]] && arch="aarch64" || arch="x86_64"
    if ! sudo curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" -o /usr/local/lib/docker/cli-plugins/docker-compose; then
      fatal "Failed to download Docker Compose plugin"
    fi
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    success "Docker Compose plugin installed"
  elif [[ "$DISTRO_FAMILY" == "dnf" ]]; then
    local distro_id; distro_id=$(. /etc/os-release && echo "$ID")
    # Map derivative distros to their Docker repo parent
    case "$distro_id" in
      rocky|alma|ol) distro_id="centos" ;;
    esac
    local dnf_repo="centos"
    [[ "$distro_id" == "fedora" ]] && dnf_repo="fedora"
    run_with_spinner "Adding Docker repository" sudo dnf config-manager --add-repo "https://download.docker.com/linux/${dnf_repo}/docker-ce.repo" || return 1
    run_with_spinner "Installing Docker" sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin || return 1
  fi
  sudo systemctl start docker
  sudo systemctl enable docker
  local target_user="${SUDO_USER:-$USER}"
  if ! id -nG "$target_user" | grep -qw docker; then
    sudo usermod -aG docker "$target_user"
    warn "Added $target_user to docker group. You may need to log out and back in."
  fi
}

ensure_docker() {
  detect_distro
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker is not installed."
    if confirm "Install Docker now?"; then
      install_docker || fatal "Docker installation failed. Install manually: https://docs.docker.com/engine/install/"
      success "Docker installed"
    else
      fatal "Docker is required. Install it from https://docs.docker.com/engine/install/"
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    info "Starting Docker daemon..."
    sudo systemctl start docker 2>/dev/null || fatal "Cannot start Docker. Is the daemon installed?"
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fatal "docker compose v2 is required but not available. Install docker-compose-plugin."
  fi
  success "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+')"
}

# --- Banner ---
banner() {
  printf "\n${CYN}"
  echo '__/\\\\\\\\\\\\\_________________________________________________________________________________________
 _\/\\\/////////\\\_______________________________________________________________________________________
  _\/\\\_______\/\\\______________________________________________________________________________/\\\_____
   _\/\\\\\\\\\\\\\\___/\\\\\\\\\_____/\\\\\\\\\\\__/\\\\\\\\\_____/\\/\\\\\\\___/\\/\\\\\\\______\/\\\_____
    _\/\\\/////////\\\_\////////\\\___\///////\\\/__\////////\\\___\/\\\/////\\\_\/\\\/////\\\__/\\\\\\\\\\\_
     _\/\\\_______\/\\\___/\\\\\\\\\\_______/\\\/______/\\\\\\\\\\__\/\\\___\///__\/\\\___\///__\/////\\\///__
      _\/\\\_______\/\\\__/\\\/////\\\_____/\\\/_______/\\\/////\\\__\/\\\_________\/\\\_____________\/\\\_____
       _\/\\\\\\\\\\\\\/__\//\\\\\\\\/\\__/\\\\\\\\\\\_\//\\\\\\\\/\\_\/\\\_________\/\\\_____________\///_____
        _\/////////////_____\////////\//__\///////////___\////////\//__\///__________\///________________________'
  printf "${RST}\n"
  printf " ${DIM}Repository: https://github.com/LavX/bazarr${RST}\n\n"
}

# --- Upgrade path ---
check_existing() {
  local dir="$1"
  [[ -f "$dir/docker-compose.yml" ]] || return 1
  printf "\n"
  info "Existing installation found in $dir"
  printf "  ${BLD}[u]${RST} Upgrade (pull latest images, restart)\n" >&2
  printf "  ${BLD}[r]${RST} Reinstall (backup config, full setup)\n" >&2
  printf "  ${BLD}[q]${RST} Quit\n\n" >&2
  local choice
  printf "${BLD}Choice${RST}: " >&2; read -r choice </dev/tty
  case "${choice,,}" in
    u) do_upgrade "$dir"; exit 0 ;;
    r) do_backup "$dir" ;;
    *) info "Exiting."; exit 0 ;;
  esac
}

do_backup() {
  local dir="$1" ts; ts=$(date +%Y%m%d_%H%M%S)
  local backup="${dir}/backup_${ts}"
  mkdir -p "$backup"
  cp -a "$dir/docker-compose.yml" "$backup/" 2>/dev/null
  cp -a "$dir/.env" "$backup/" 2>/dev/null
  success "Config backed up to $backup"
}

do_upgrade() {
  local dir="$1"
  do_backup "$dir"
  cd "$dir"
  run_with_spinner "Pulling latest images" docker compose pull || fatal "Pull failed"
  run_with_spinner "Restarting services" docker compose up -d || fatal "Restart failed"
  printf "\n"
  success "Upgrade complete."
  printf "  ${DIM}Logs:${RST}    docker compose -f %s/docker-compose.yml logs -f\n" "$dir"
  printf "  ${DIM}Status:${RST}  docker compose -f %s/docker-compose.yml ps\n\n" "$dir"
}

# --- Generate compose ---
generate_compose() {
  local bazarr_port="$1" scraper_port="$2" movies="$3" tv="$4" translator="$5" translator_port="$6" flaresolverr="$7"
  local movies_vol="" tv_vol="" translator_block="" flare_block="" scraper_depends="" scraper_env="" volumes_block=""

  [[ -n "$movies" ]] && movies_vol="      - ${movies}:/movies"
  [[ -n "$tv" ]]     && tv_vol="      - ${tv}:/tv"

  # FlareSolverr service and scraper dependency
  if [[ "$flaresolverr" == "y" ]]; then
    flare_block="
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    environment:
      - LOG_LEVEL=info
    healthcheck:
      test: [\"CMD\", \"curl\", \"-sf\", \"http://localhost:8191/health\"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    deploy:
      resources:
        limits:
          memory: 1G"
    scraper_depends="    depends_on:
      flaresolverr:
        condition: service_healthy"
    scraper_env="    environment:
      - FLARESOLVERR_URL=http://flaresolverr:8191/v1"
  fi

  # Translator service
  if [[ "$translator" == "y" ]]; then
    translator_block="
  ai-subtitle-translator:
    image: ghcr.io/lavx/ai-subtitle-translator:latest
    container_name: ai-subtitle-translator
    restart: unless-stopped
    ports:
      - \"${translator_port}:8765\"
    env_file:
      - .env
    volumes:
      - translator-data:/app/data
    healthcheck:
      test: [\"CMD\", \"curl\", \"-sf\", \"http://localhost:8765/health\"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s"
    volumes_block="
volumes:
  translator-data:"
  fi

  local template
  template='services:
  bazarr:
    image: ghcr.io/lavx/bazarr:latest
    container_name: bazarr
    restart: unless-stopped
    depends_on:
      opensubtitles-scraper:
        condition: service_healthy
    ports:
      - "__BAZARR_PORT__:6767"
    env_file:
      - .env
    volumes:
      - ./config:/config
__MOVIES_VOLUME__
__TV_VOLUME__
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:6767/_supervisor/status | grep -q '\''\"running\"'\''"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s

  opensubtitles-scraper:
    image: ghcr.io/lavx/opensubtitles-scraper:latest
    container_name: opensubtitles-scraper
    restart: unless-stopped
__SCRAPER_DEPENDS__
    ports:
      - "__SCRAPER_PORT__:8000"
__SCRAPER_ENV__
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
__FLARE_SERVICE__
__TRANSLATOR_SERVICE__
__VOLUMES_BLOCK__'

  template="${template//__BAZARR_PORT__/$bazarr_port}"
  template="${template//__SCRAPER_PORT__/$scraper_port}"
  template="${template//__SCRAPER_DEPENDS__/$scraper_depends}"
  template="${template//__SCRAPER_ENV__/$scraper_env}"
  template="${template//__FLARE_SERVICE__/$flare_block}"
  template="${template//__VOLUMES_BLOCK__/$volumes_block}"
  template="${template//__MOVIES_VOLUME__/$movies_vol}"
  template="${template//__TV_VOLUME__/$tv_vol}"
  template="${template//__TRANSLATOR_SERVICE__/$translator_block}"

  # Remove blank lines from empty volume slots
  printf '%s\n' "$template" | sed '/^$/d'
}

# --- Generate pre-seeded config.yaml ---
generate_config() {
  local config_dir="$1"
  local config_file="${config_dir}/config/config.yaml"

  # Skip if config already exists (upgrade scenario)
  [[ -f "$config_file" ]] && return 0

  mkdir -p "${config_dir}/config"

  # Generate a random API key and flask secret
  local bazarr_apikey flask_secret
  bazarr_apikey=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
  flask_secret=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')

  # Build sonarr/radarr sections based on user input
  local use_sonarr="false" use_radarr="false"
  [[ -n "$SONARR_KEY" ]] && use_sonarr="true"
  [[ -n "$RADARR_KEY" ]] && use_radarr="true"

  # Build translator section
  local translator_type="none" translator_url="" translator_key="" translator_enc_key=""
  if [[ "$TRANSLATOR" == "y" ]]; then
    translator_type="openrouter"
    translator_url="http://ai-subtitle-translator:8765"
    translator_key="$API_KEY"
    translator_enc_key="$ENC_KEY"
  fi

  cat > "$config_file" <<YAML
---
auth:
  apikey: ${bazarr_apikey}
  type: ''
  username: ''
  password: ''
general:
  auto_update: false
  base_url: ''
  branch: master
  concurrent_jobs: 4
  days_to_upgrade_subs: 7
  debug: false
  embedded_subs_show_desired: false
  embedded_subtitles_parser: ffprobe
  enabled_providers:
  - opensubtitlescom
  - opensubtitles
  - embeddedsubtitles
  - podnapisi
  flask_secret_key: ${flask_secret}
  hi_extension: hi
  ip: '*'
  minimum_score: 75
  minimum_score_movie: 70
  movie_default_enabled: true
  movie_default_profile: 1
  multithreading: true
  page_size: 50
  port: 6767
  serie_default_enabled: true
  serie_default_profile: 1
  single_language: false
  theme: auto
  upgrade_frequency: 12
  upgrade_subs: true
  use_embedded_subs: false
  use_provider_priority: true
  use_radarr: ${use_radarr}
  use_sonarr: ${use_sonarr}
  utf8_encode: true
  wanted_search_frequency: 6
  wanted_search_frequency_movie: 6
opensubtitles:
  scraper_service_url: opensubtitles-scraper:8000
  use_web_scraper: true
  ssl: false
  timeout: 15
sonarr:
  apikey: '${SONARR_KEY}'
  base_url: '/'
  full_update: Daily
  http_timeout: 60
  ip: ${SONARR_IP:-localhost}
  only_monitored: false
  port: ${SONARR_PORT}
  series_sync: 60
  series_sync_on_live: true
  ssl: false
  use_ffprobe_cache: true
radarr:
  apikey: '${RADARR_KEY}'
  base_url: '/'
  full_update: Daily
  http_timeout: 60
  ip: ${RADARR_IP:-localhost}
  movies_sync: 60
  movies_sync_on_live: true
  only_monitored: false
  port: ${RADARR_PORT}
  ssl: false
  use_ffprobe_cache: true
translator:
  openrouter_api_key: '${translator_key}'
  openrouter_encryption_key: '${translator_enc_key}'
  openrouter_model: google/gemini-2.5-flash-lite-preview-09-2025
  openrouter_parallel_batches: 4
  openrouter_max_concurrent: 2
  openrouter_temperature: 0.3
  openrouter_url: '${translator_url}'
  translator_type: ${translator_type}
subsync:
  gss: true
  max_offset_seconds: 60
  no_fix_framerate: true
  use_subsync: true
analytics:
  enabled: false
backup:
  day: 6
  folder: /config/backup
  frequency: Weekly
  hour: 3
  retention: 31
YAML

  success "Created config.yaml with pre-configured settings"
}

# --- Write .env atomically with mode 600 ---
write_env() {
  local dir="$1" puid="$2" pgid="$3" tz="$4" api_key="$5" enc_key="${6:-}"
  local tmp; tmp=$(mktemp "${dir}/.env.XXXXXX")
  chmod 600 "$tmp"
  (
    umask 077
    cat > "$tmp" <<EOF
PUID=${puid}
PGID=${pgid}
TZ=${tz}
OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8000
EOF
    [[ -n "$api_key" ]] && printf 'OPENROUTER_API_KEY=%s\n' "$api_key" >> "$tmp"
    [[ -n "$enc_key" ]] && printf 'ENCRYPTION_KEY=%s\n' "$enc_key" >> "$tmp"
  )
  mv "$tmp" "${dir}/.env"
}

# --- Main flow ---
banner
ensure_docker
detect_puid_pgid

# Check for existing install
INSTALL_DIR=$(read_input "Install directory" "./bazarr-plus")
INSTALL_DIR=$(validate_directory "$INSTALL_DIR")
check_existing "$INSTALL_DIR"

# Collect media paths
MOVIES_PATH=$(read_input "Movies path (leave empty to skip)" "")
MOVIES_PATH=$(validate_media_path "$MOVIES_PATH") || MOVIES_PATH=""
TV_PATH=$(read_input "TV shows path (leave empty to skip)" "")
TV_PATH=$(validate_media_path "$TV_PATH") || TV_PATH=""

# Sonarr/Radarr integration
SONARR_IP=""; SONARR_KEY=""; SONARR_PORT=8989
RADARR_IP=""; RADARR_KEY=""; RADARR_PORT=7878

if confirm "Configure Sonarr connection?"; then
  SONARR_IP=$(read_input "  Sonarr hostname or IP" "localhost")
  SONARR_PORT=$(read_input "  Sonarr port" "8989")
  validate_port "$SONARR_PORT"
  SONARR_KEY=$(read_input "  Sonarr API key" "")
  [[ -z "$SONARR_KEY" ]] && warn "Sonarr API key is empty. You can add it later in Settings."
fi

if confirm "Configure Radarr connection?"; then
  RADARR_IP=$(read_input "  Radarr hostname or IP" "localhost")
  RADARR_PORT=$(read_input "  Radarr port" "7878")
  validate_port "$RADARR_PORT"
  RADARR_KEY=$(read_input "  Radarr API key" "")
  [[ -z "$RADARR_KEY" ]] && warn "Radarr API key is empty. You can add it later in Settings."
fi

# FlareSolverr (helps scraper bypass Cloudflare)
FLARESOLVERR="n"
if confirm "Install FlareSolverr? (helps bypass Cloudflare challenges)"; then
  FLARESOLVERR="y"
fi

# AI translator
TRANSLATOR="n"; API_KEY=""
if confirm "Install AI subtitle translator?"; then
  TRANSLATOR="y"
  printf "${BLD}OpenRouter API key${RST}: " >&2
  read -rs API_KEY </dev/tty; printf "\n" >&2
  [[ -z "$API_KEY" ]] && warn "No API key provided. You can add it later in Settings > AI Translator."
fi

# Auto-detect system values
TZ=$(detect_timezone)
BAZARR_PORT=6767
if check_port "$BAZARR_PORT"; then
  BAZARR_PORT=$(read_input "Port 6767 is in use. Bazarr port" "$(find_free_port 6767)")
  validate_port "$BAZARR_PORT"
fi
SCRAPER_PORT=$(find_free_port 8000)
TRANSLATOR_PORT=$(find_free_port 8765)

# --- Confirmation ---
printf "\n"
printf "${BLD}${CYN}Configuration Summary${RST}\n"
printf "${DIM}%-24s${RST} %s\n" "Install directory:" "$INSTALL_DIR"
printf "${DIM}%-24s${RST} %s\n" "Bazarr port:" "$BAZARR_PORT"
printf "${DIM}%-24s${RST} %s\n" "Scraper port:" "$SCRAPER_PORT"
[[ -n "$MOVIES_PATH" ]] && printf "${DIM}%-24s${RST} %s\n" "Movies:" "$MOVIES_PATH"
[[ -n "$TV_PATH" ]]     && printf "${DIM}%-24s${RST} %s\n" "TV shows:" "$TV_PATH"
printf "${DIM}%-24s${RST} %s\n" "Timezone:" "$TZ"
printf "${DIM}%-24s${RST} %s\n" "User/Group:" "${PUID}:${PGID}"
[[ "$FLARESOLVERR" == "y" ]] && printf "${DIM}%-24s${RST} %s\n" "FlareSolverr:" "enabled"
[[ -n "$SONARR_KEY" ]] && printf "${DIM}%-24s${RST} %s\n" "Sonarr:" "${SONARR_IP}:${SONARR_PORT}"
[[ -n "$RADARR_KEY" ]] && printf "${DIM}%-24s${RST} %s\n" "Radarr:" "${RADARR_IP}:${RADARR_PORT}"
if [[ "$TRANSLATOR" == "y" ]]; then
  printf "${DIM}%-24s${RST} %s\n" "AI translator:" "enabled (port ${TRANSLATOR_PORT})"
  [[ -n "$API_KEY" ]] && printf "${DIM}%-24s${RST} %s\n" "API key:" "${API_KEY:0:8}..."
fi
printf "\n"

confirm "Proceed with installation?" "y" || { info "Cancelled."; exit 0; }
printf "\n"

# --- Install ---
cd "$INSTALL_DIR"
mkdir -p config

# Generate shared encryption key for translator communication
ENC_KEY=""
[[ "$TRANSLATOR" == "y" ]] && ENC_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')

write_env "$INSTALL_DIR" "$PUID" "$PGID" "$TZ" "$API_KEY" "$ENC_KEY"
success "Created .env (mode 600)"

generate_config "$INSTALL_DIR"

generate_compose "$BAZARR_PORT" "$SCRAPER_PORT" "$MOVIES_PATH" "$TV_PATH" "$TRANSLATOR" "$TRANSLATOR_PORT" "$FLARESOLVERR" \
  > docker-compose.yml
success "Created docker-compose.yml"

printf '%s\n' ".env" "*.key" > .gitignore

run_with_spinner "Pulling images" docker compose pull || fatal "Failed to pull images"
run_with_spinner "Starting services" docker compose up -d || fatal "Failed to start services"

# Wait for bazarr health
info "Waiting for Bazarr+ to become healthy..."
local elapsed=0
while [[ $elapsed -lt 60 ]]; do
  if curl -sf "http://localhost:${BAZARR_PORT}/_supervisor/status" 2>/dev/null | grep -q '"running"'; then
    break
  fi
  sleep 2; ((elapsed += 2))
done

# --- Success ---
printf "\n"
if [[ $elapsed -lt 60 ]]; then
  success "Bazarr+ is running."
else
  warn "Bazarr+ is still starting. Check logs if it does not come up within a minute."
fi

printf "\n${BLD}${GRN}Bazarr+${RST}  http://localhost:${BAZARR_PORT}\n"
[[ "$TRANSLATOR" == "y" ]] && printf "${BLD}${GRN}Translator${RST}  http://localhost:${TRANSLATOR_PORT}\n"
printf "\n"
printf "${DIM}Useful commands:${RST}\n"
printf "  Logs:      docker compose -f %s/docker-compose.yml logs -f\n" "$INSTALL_DIR"
printf "  Restart:   docker compose -f %s/docker-compose.yml restart\n" "$INSTALL_DIR"
printf "  Stop:      docker compose -f %s/docker-compose.yml down\n" "$INSTALL_DIR"
printf "  Update:    curl -fsSL https://lavx.github.io/bazarr/install.sh | bash\n"
printf "\n"
}

main "$@"
