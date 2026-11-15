terraform {
  required_providers {
    # form_type/styling on coder_parameter need terraform-provider-coder >= 2.4 (Coder dynamic parameters).
    coder  = { source = "coder/coder", version = ">= 2.4" }
    docker = { source = "kreuzwerker/docker", version = ">= 3.0" }
  }
}

variable "docker_socket" {
  type        = string
  description = "(Optional) Docker socket URI"
  default     = ""
}

provider "coder" {}
provider "docker" {
  host = var.docker_socket != "" ? var.docker_socket : null
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# ---------------------------------------------------------------- template variables (set by the admin)

variable "image" {
  type        = string
  description = "Prebuilt image in a registry. Empty = build from ./build on the Coder host's Docker"
  default     = ""
}

variable "azure_openai_endpoint" {
  type        = string
  description = "Default Azure OpenAI endpoint, e.g. https://YOUR-RESOURCE.openai.azure.com (users may override)"
  default     = ""
}

variable "realtime_deployment" {
  type        = string
  description = "Default Realtime model deployment name (users may override)"
  default     = "gpt-realtime-2.1-mini"
}

variable "web_search_deployment" {
  type        = string
  description = "Default chat deployment used for web search (Responses API web_search tool). Empty = web search off (users may override)"
  default     = ""
}

variable "app_subdomain" {
  type        = bool
  description = "Serve the app on a subdomain (requires a wildcard access URL). false = path-based app"
  default     = false
}

# ---------------------------------------------------------------- workspace parameters (set by each user)

data "coder_parameter" "azure_openai_api_key" {
  name         = "azure_openai_api_key"
  display_name = "Azure OpenAI API キー（個人用）"
  description  = "あなた専用の Azure OpenAI API キー。このワークスペースのエージェントだけが使い、ブラウザには送られません。"
  type         = "string"
  form_type    = "input"
  mutable      = true
  order        = 1
  styling      = jsonencode({ mask_input = true, placeholder = "API キーを貼り付け" })
  validation {
    regex = "^$|^\\S{16,}$" # empty only during template import; the UI still requires a value (no default)
    error = "API キーを入力してください（空白なし）"
  }
}

data "coder_parameter" "azure_openai_endpoint" {
  name         = "azure_openai_endpoint"
  display_name = "Azure OpenAI エンドポイント"
  description  = "API キーに対応するリソースのエンドポイント。例: https://YOUR-RESOURCE.openai.azure.com"
  type         = "string"
  form_type    = "input"
  mutable      = true
  order        = 2
  default      = var.azure_openai_endpoint
}

data "coder_parameter" "realtime_deployment" {
  name         = "realtime_deployment"
  display_name = "Realtime モデルのデプロイ名"
  type         = "string"
  form_type    = "input"
  mutable      = true
  order        = 3
  default      = var.realtime_deployment
}

data "coder_parameter" "web_search_deployment" {
  name         = "web_search_deployment"
  display_name = "Web 検索に使うモデルのデプロイ名"
  description  = "Responses API の web_search ツールに対応したチャットモデル（例: gpt-6.1-terra）。空にすると Web 検索を無効にします。"
  type         = "string"
  form_type    = "input"
  mutable      = true
  order        = 5
  default      = var.web_search_deployment
}

data "coder_parameter" "realtime_voice" {
  name         = "realtime_voice"
  display_name = "音声"
  type         = "string"
  form_type    = "dropdown"
  mutable      = true
  order        = 4
  default      = "marin"
  dynamic "option" {
    for_each = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]
    content {
      name  = option.value
      value = option.value
    }
  }
}

# ---------------------------------------------------------------- image

# With var.image empty, the image is built from the app source bundled in ./build
# (Dockerfile, entrypoint.sh, requirements.txt, app/) on the provisioner's Docker daemon.
# No registry is needed. The tag changes whenever the source changes, which triggers a rebuild.
locals {
  build_dir   = "${path.module}/build"
  build_files = sort(tolist(fileset(local.build_dir, "**")))
  build_hash  = sha1(join("", [for f in local.build_files : "${f}:${filesha1("${local.build_dir}/${f}")}"]))
}

resource "docker_image" "agent" {
  count        = var.image == "" ? 1 : 0
  name         = "personal-ai-agent:tpl-${substr(local.build_hash, 0, 12)}"
  keep_locally = true # other workspaces share the same image
  build {
    context = local.build_dir
  }
  triggers = {
    source = local.build_hash
  }
}

locals {
  image = var.image != "" ? var.image : docker_image.agent[0].image_id
}

# ---------------------------------------------------------------- resources

resource "docker_volume" "data" {
  name = "coder-${data.coder_workspace.me.id}-agent-data"
  lifecycle { prevent_destroy = true }
  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = data.coder_provisioner.me.arch
  auth = "token"
  dir  = "/srv"

  metadata {
    display_name = "Agent API"
    key          = "agent_api"
    script       = "curl -fsS -m 3 http://localhost:8000/health >/dev/null && echo OK || echo DOWN"
    interval     = 30
    timeout      = 5
  }
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name
  image    = local.image
  must_run = true
  # Always-on: Docker restarts the container after crashes and daemon/host restarts.
  restart = "unless-stopped"

  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    # The image entrypoint runs this in the background (downloads and starts the Coder agent).
    "CODER_AGENT_INIT_SCRIPT=${replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")}",
    "AZURE_OPENAI_ENDPOINT=${data.coder_parameter.azure_openai_endpoint.value}",
    "AZURE_OPENAI_API_KEY=${data.coder_parameter.azure_openai_api_key.value}",
    "AZURE_OPENAI_REALTIME_DEPLOYMENT=${data.coder_parameter.realtime_deployment.value}",
    "REALTIME_VOICE=${data.coder_parameter.realtime_voice.value}",
    "WEB_SEARCH_DEPLOYMENT=${data.coder_parameter.web_search_deployment.value}",
    "APP_TIMEZONE=Asia/Tokyo",
  ]

  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }

  volumes {
    volume_name    = docker_volume.data.name
    container_path = "/data"
  }

  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
}

resource "coder_app" "web" {
  agent_id     = coder_agent.main.id
  slug         = "personal-agent"
  display_name = "Personal AI Agent"
  icon         = "/emojis/1f916.png"
  url          = "http://localhost:8000"
  share        = "owner"
  subdomain    = var.app_subdomain
  open_in      = "tab"
  healthcheck {
    url       = "http://localhost:8000/health"
    interval  = 10
    threshold = 3
  }
}
