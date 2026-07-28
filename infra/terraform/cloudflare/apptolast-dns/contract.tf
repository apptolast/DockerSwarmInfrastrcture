locals {
  platform_contract = yamldecode(
    file("${path.module}/../../../../config/platform.yml")
  )
  minecraft_contract = yamldecode(
    file("${path.module}/../../../../config/minecraft.yml")
  )
  cloudflare_account_id = "becc1c00820a0f6779e9b278ab150abf"
  cloudflare_zone_id    = "b69e21ff397bd00c35b989fe10068d0e"
  reviewed_dns_keys = toset([
    "edge",
    "kropia",
    "minecraft-stats",
    "minecraft",
    "n8n",
    "openclaw",
    "passbolt",
    "generadorcodigosqr",
    "pablohurtadohg",
    "albertohidalgo",
  ])
}

check "platform_contract" {
  assert {
    condition = (
      can(regex(
        "^([0-9]{1,3}\\.){3}[0-9]{1,3}$",
        local.platform_contract.platform_public_ipv4
      )) &&
      can(cidrhost(
        "${local.platform_contract.platform_public_ipv4}/32",
        0
      )) &&
      can(cidrhost(
        "${local.platform_contract.platform_dns_legacy_ipv4}/32",
        0
      )) &&
      local.platform_contract.platform_dns_legacy_ipv4 !=
      local.platform_contract.platform_public_ipv4 &&
      toset(keys(local.platform_contract.platform_dns_cutover)) ==
      local.reviewed_dns_keys &&
      alltrue([
        for enabled in values(
          local.platform_contract.platform_dns_cutover
        ) : enabled == true || enabled == false
      ]) &&
      local.platform_contract.platform_dns_cutover.edge == true &&
      local.platform_contract.platform_dns_cutover.minecraft ==
      local.platform_contract.platform_minecraft_public_enabled &&
      (
        !local.platform_contract.platform_dns_cutover.minecraft ||
        local.minecraft_contract.minecraft_contract.online_mode == true ||
        local.platform_contract
        .platform_minecraft_offline_public_accepted == true
      ) &&
      can(regex(
        "^[a-z0-9.-]+$",
        local.platform_contract.edge_traefik_hostname
      )) &&
      can(regex(
        "^[a-f0-9]{32}$",
        local.cloudflare_account_id
      )) &&
      can(regex(
        "^[a-f0-9]{32}$",
        local.cloudflare_zone_id
      )) &&
      endswith(
        local.platform_contract.edge_traefik_hostname,
        ".${local.platform_contract.edge_cloudflare_zone}"
      )
    )
    error_message = "The shared edge IPv4, hostname or Cloudflare ID contract is invalid."
  }
}
